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
import base64
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
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WS_CREATE = REPO / "workspace" / "ws-create.sh"
WS_DESTROY = REPO / "workspace" / "ws-destroy.sh"
WS_RESET = REPO / "workspace" / "ws-reset.sh"
AI_USAGE_SCAN = Path(__file__).resolve().parent / "scan_ai_usage.py"
CODEX_USAGE_SCAN = Path(__file__).resolve().parent / "scan_codex_usage.py"
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
EnvironmentFile=/home/dev/.hermes/.env
ExecStart=/home/dev/.local/bin/hermes gateway
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""

# Hermes ships its own `hermes gateway install`, which writes a *user* unit of
# the exact same name into ~/.config/systemd/user and turns on lingering so it
# outlives every logout. Ours is a system unit. Two managers, one unit name -
# and `systemctl disable --now hermes-gateway` as root reports success having
# touched only its own copy, so the customer's gateway keeps polling.
#
# That is not merely untidy. Telegram's getUpdates serves exactly one poller
# per bot token; a second one gets HTTP 409 and, from the customer's side, the
# bot simply stops answering. So whichever gateway we did not start has to be
# gone before ours comes up, and gone again when the service is turned off.
#
# runuser + XDG_RUNTIME_DIR, not `su - dev`: a login shell for a lingering user
# does not necessarily export the runtime dir, and without it `systemctl --user`
# cannot find the bus and exits non-zero having done nothing.
HERMES_USER_GATEWAY_PURGE = (
    "U=$(id -u dev); "
    "runuser -u dev -- env XDG_RUNTIME_DIR=/run/user/$U "
    "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$U/bus "
    "systemctl --user disable --now hermes-gateway >/dev/null 2>&1 || true; "
    "rm -f /home/dev/.config/systemd/user/hermes-gateway.service "
    "/home/dev/.config/systemd/user/default.target.wants/hermes-gateway.service; "
    "runuser -u dev -- env XDG_RUNTIME_DIR=/run/user/$U "
    "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$U/bus "
    "systemctl --user daemon-reload >/dev/null 2>&1 || true; "
    # `disable --now` on a Restart=always unit leaves it in failed state, and a
    # failed unit of that name would make a later `is-active` check ambiguous.
    "runuser -u dev -- env XDG_RUNTIME_DIR=/run/user/$U "
    "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$U/bus "
    "systemctl --user reset-failed hermes-gateway >/dev/null 2>&1 || true; "
    # The vendor unit is Restart=always, so a unit that loses its manager can
    # still leave the process behind. Its argv is distinct from our system
    # unit's (`-m hermes_cli.main` vs the `hermes` console script), so this
    # cannot reap the gateway we are about to start.
    "pkill -u dev -f 'hermes_cli.main gateway' >/dev/null 2>&1 || true; "
)

VERBS = {"provision", "archive", "restore", "destroy",
         "expose_port", "unexpose_port",
         "service_ssh", "service_rdp", "service_hermes",
         "fs_list", "fs_pull", "fs_push", "fs_mkdir", "fs_delete",
         "fs_archive", "apt_repair", "ai_claude", "ai_codex", "ai_openclaw",
         "ai_managed_web",
         "ai_usage", "codex_usage", "disk_usage", "reset", "ping"}

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


# --- Codex ----------------------------------------------------------------
# Same shape as the Claude block above and for the same reasons: ONE file is
# read, only named keys survive, and everything else the CLI keeps beside it is
# never opened.
#
# What lives in ~/.codex and must never leave the host: history.jsonl is the
# operator's own prompts, config.toml their settings, and cache/, log/ and
# goals_*.sqlite are working state. None of it is needed to be signed in.
CODEX_HOST_HOME = Path(os.environ.get("MMD_CODEX_HOST_HOME", "/root"))
CODEX_AUTH_FILE = "auth.json"

# The whole of what signs a machine in. `auth_mode` says which of the two the
# account uses (a ChatGPT OAuth grant, or a plain API key); `tokens` carries the
# grant; `last_refresh` is what the CLI compares against to decide when to renew.
CODEX_AUTH_KEYS = ("auth_mode", "OPENAI_API_KEY", "tokens", "last_refresh")

# Never touched. Listed rather than inferred, so a reviewer can see what was
# considered - the same discipline as CLAUDE_NEVER_COPY.
CODEX_NEVER_COPY = (
    "history.jsonl", "config.toml", "installation_id", "cache", "log",
    "sessions", ".tmp", "goals_1.sqlite",
)


def _codex_credentials() -> tuple[dict | None, str]:
    """Build the credential document to place in a workspace.

    Reads exactly one file and rebuilds it from the allowlisted keys, so
    anything a future Codex release adds beside them is dropped by
    construction rather than by remembering to add it to a blocklist.
    """
    src = CODEX_HOST_HOME / ".codex" / CODEX_AUTH_FILE
    try:
        raw = json.loads(src.read_text())
    except FileNotFoundError:
        return None, "the platform account is not signed in on this host"
    except (OSError, ValueError) as e:  # noqa: BLE001
        return None, f"the platform credentials are unreadable: {e}"
    if not isinstance(raw, dict):
        return None, "the platform credentials are malformed"

    out = {k: raw[k] for k in CODEX_AUTH_KEYS if k in raw}
    tokens = out.get("tokens")
    has_oauth = isinstance(tokens, dict) and tokens.get("access_token")
    if not has_oauth and not out.get("OPENAI_API_KEY"):
        return None, "the platform account is not signed in on this host"
    return out, ""


def _jwt_exp(token: str) -> int | None:
    """The `exp` claim, read WITHOUT verifying the signature.

    Only ever used to print a date in the interface. The token is not trusted
    here and is not being authenticated - it is the host's own credential, and
    a forged expiry would mislead nobody but the operator reading the page.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
        return int(exp) if isinstance(exp, (int, float)) else None
    except Exception:  # noqa: BLE001
        return None


def _codex_expiry() -> int | None:
    creds, _ = _codex_credentials()
    if not creds:
        return None
    tokens = creds.get("tokens")
    if isinstance(tokens, dict) and tokens.get("access_token"):
        return _jwt_exp(tokens["access_token"])
    return None


def _codex_status(project: str) -> dict:
    rc, out, _ = _run_split(
        ["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
         "v=$(su - dev -c 'codex --version' 2>/dev/null | head -1); "
         "printf 'version=%s\nlinked=%s\n' "
         "  \"${v:-}\" "
         "  \"$([ -s /home/dev/.codex/auth.json ] && echo yes || echo no)\""],
        timeout=120)
    info = dict(ln.split("=", 1) for ln in (out or "").splitlines() if "=" in ln)
    version = (info.get("version") or "").strip()
    return {"installed": bool(version), "version": version or None,
            "linked": info.get("linked", "no").strip() == "yes"}


def _verb_ai_codex(project: str, req: dict) -> dict:
    action = req.get("action")
    if action not in ("status", "install", "unlink"):
        return {"ok": False, "error": "action must be status, install or unlink"}

    if action == "status":
        st = _codex_status(project)
        creds, why = _codex_credentials()
        return {"ok": True, **st, "available": creds is not None,
                "unavailable_reason": why or None,
                "expires_at": _codex_expiry()}

    if action == "unlink":
        ok, out = _run(["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
                        "rm -f /home/dev/.codex/auth.json"], timeout=120)
        return {"ok": ok, "output": (out or "")[-300:], **_codex_status(project)}

    # --- install -----------------------------------------------------------
    creds, why = _codex_credentials()
    if creds is None:
        return {"ok": False, "error": why}

    st = _codex_status(project)
    if not st["installed"]:
        # The golden image installs it already; this covers machines built
        # before that, and any customer who removed it.
        ok, out = _run(["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
                        "command -v npm >/dev/null || exit 90; "
                        "npm install -g --no-fund --no-audit @openai/codex"],
                       timeout=900)
        if not ok:
            return {"ok": False, "error": "install failed",
                    "output": (out or "")[-800:]}

    # Through stdin, so no token reaches a command line or this process's log.
    ok, out = _run(["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
                    "install -d -m 0700 -o dev -g dev /home/dev/.codex && "
                    "install -m 0600 -o dev -g dev /dev/null "
                    "  /home/dev/.codex/auth.json && "
                    "cat > /home/dev/.codex/auth.json"],
                   timeout=120, stdin_text=json.dumps(creds))
    if not ok:
        return {"ok": False, "error": "could not write credentials",
                "output": (out or "")[-300:]}

    # A starting model, if the operator has chosen one. Written ONLY when the
    # file does not already exist: the customer owns their config afterwards,
    # and rewriting it on every repair would silently undo their choice.
    model = (req.get("model") or "").strip()
    if model and re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", model):
        _run(["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
              "test -e /home/dev/.codex/config.toml || "
              "{ install -m 0600 -o dev -g dev /dev/stdin "
              "/home/dev/.codex/config.toml; }"],
             timeout=60, stdin_text=f'model = "{model}"\n')
    return {"ok": True, **_codex_status(project), "expires_at": _codex_expiry()}


# --- OpenClaw --------------------------------------------------------------
# A self-hosted agent with its own web dashboard and messaging channels
# (github.com/openclaw/openclaw). Architecturally it is Hermes again: it runs
# INSIDE the customer's workspace, serves a dashboard on a fixed port, and
# spends the customer's own capped OpenRouter key - so the platform meters and
# caps it exactly as it already does for Hermes, with no second supplier
# integration and no second way for a customer to spend money.
#
# 18789 is the vendor's default. Fixed rather than allocated, because the vhost
# reconciler has to know where to proxy and one port per workspace is a number
# the customer never has to see.
OPENCLAW_PORT = 18789
OPENCLAW_HOME = "/home/dev/.openclaw"
# `openclaw.json`, which is the CLI's OWN default - not a name of our choosing.
# The gateway can be pointed anywhere with OPENCLAW_CONFIG_PATH, but the CLI the
# customer types in their terminal reads the default, and a config only the
# service can see is one the customer cannot manage. Found the hard way:
# `openclaw devices list` reported `Config: /home/dev/.openclaw/openclaw.json`
# while the running gateway was using a different file entirely.
OPENCLAW_CONFIG = f"{OPENCLAW_HOME}/openclaw.json"
# The bridge address the host reaches the workspace from - nginx's source. The
# gateway will not treat a proxied connection as local without it, and a
# non-local Control UI connection is made to pair a device before it may talk.
# Safe to trust: nothing DNATs this port, and the bridge isolation table drops
# workspace-to-workspace traffic, so our reverse proxy is the only route in.
OPENCLAW_TRUSTED_PROXY = "10.42.0.1"
OPENCLAW_UNIT = "/etc/systemd/system/openclaw-gateway.service"
# The convention OpenClaw itself uses. A FILE rather than a token in the config,
# so the credential is not sitting in a document a customer might paste into a
# support ticket - and so `openclaw channels status` reports `token:tokenFile`
# rather than an inline secret.
OPENCLAW_SECRETS = f"{OPENCLAW_HOME}/secrets"
OPENCLAW_TG_TOKEN = f"{OPENCLAW_SECRETS}/telegram-default.token"

MANAGED_WEB = {
    "opencode": {"port": 4096, "unit": "mmd-opencode", "install":
        "test -x /home/dev/.opencode/bin/opencode || "
        "runuser -u dev -- env HOME=/home/dev bash -c 'curl -fsSL https://opencode.ai/install | bash'"},
    "openwebui": {"port": 3001, "unit": "mmd-openwebui", "install":
        "command -v docker >/dev/null"},
}


def _verb_ai_managed_web(project: str, req: dict) -> dict:
    service = (req.get("service") or "").strip()
    action = req.get("action")
    spec = MANAGED_WEB.get(service)
    if spec is None or action not in ("install", "disable"):
        return {"ok": False, "error": "invalid managed web request"}
    unit = spec["unit"]
    if action == "disable":
        command = (f"systemctl disable --now {unit} 2>/dev/null; "
                   f"rm -f /etc/systemd/system/{unit}.service; "
                   + ("docker rm -f mmd-openwebui 2>/dev/null; docker volume rm open-webui 2>/dev/null; "
                      if service == "openwebui" else "")
                   + "systemctl daemon-reload; true")
        ok, out = _run(["incus", "exec", "ws", "--project", project,
                        "--", "bash", "-lc", command], timeout=300)
        return {"ok": ok, "output": (out or "")[-300:]}

    key = (req.get("openrouter_key") or "").strip()
    password = (req.get("password") or "").strip()
    admin_email = (req.get("admin_email") or "").strip()
    # One model id arrives; each service wants a different shape of it.
    # OpenCode addresses OpenRouter models as `openrouter/<id>`; Open WebUI
    # talks to OpenRouter's OpenAI-compatible endpoint and wants the bare id.
    model = (req.get("model") or "").strip()
    if model and not re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", model):
        model = ""
    if service == "openwebui":
        model = model.removeprefix("openrouter/")
    elif model and not model.startswith("openrouter/"):
        model = f"openrouter/{model}"
    if not key or not password or (service == "openwebui" and "@" not in admin_email):
        return {"ok": False, "error": "credentials are required"}
    ok, out = _run(["incus", "exec", "ws", "--project", project,
                    "--", "bash", "-lc", spec["install"]], timeout=1800)
    if not ok:
        return {"ok": False, "error": "install failed", "output": (out or "")[-500:]}

    if service == "opencode":
        unit_text = f"""[Unit]\nDescription=OpenCode web\nAfter=network-online.target\n[Service]\nUser=dev\nWorkingDirectory=/home/dev\nEnvironment=HOME=/home/dev\nEnvironment=OPENCODE_SERVER_PASSWORD={password}\nExecStart=/home/dev/.opencode/bin/opencode web --hostname 0.0.0.0 --port 4096\nRestart=always\n[Install]\nWantedBy=multi-user.target\n"""
        auth = json.dumps({"openrouter": {"type": "api", "key": key}})
        # Incus passes stdin as a pipe.  `install /dev/stdin` tries to reopen
        # that pipe by pathname and fails, even though reading the inherited
        # descriptor works.  Create the file first and stream into it, matching
        # the credential-write pattern used by Codex and Hermes.
        script = ("install -d -m 700 -o dev -g dev /home/dev/.local/share/opencode && "
                  "umask 077 && cat > /home/dev/.local/share/opencode/auth.json && "
                  "chown dev:dev /home/dev/.local/share/opencode/auth.json && "
                  "chmod 600 /home/dev/.local/share/opencode/auth.json")
        ok, out = _run(["incus", "exec", "ws", "--project", project, "--",
                        "bash", "-lc", script], timeout=120, stdin_text=auth)
        # The credential alone is not enough. With a key but no model OpenCode
        # has nothing to call: it starts, accepts a prompt, and answers with
        # something that reads like the prompt echoed back. Measured on a live
        # workspace, whose opencode.jsonc held only a `$schema` line.
        if ok and model:
            cfg = json.dumps({"$schema": "https://opencode.ai/config.json",
                              "model": model}, indent=2)
            ok, out = _run(
                ["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
                 "install -d -m 755 -o dev -g dev /home/dev/.config/opencode && "
                 "cat > /home/dev/.config/opencode/opencode.jsonc && "
                 "chown dev:dev /home/dev/.config/opencode/opencode.jsonc"],
                timeout=120, stdin_text=cfg)
    else:
        unit_text = f"""[Unit]\nDescription=Open WebUI\nAfter=docker.service\nRequires=docker.service\n[Service]\nExecStartPre=-/usr/bin/docker rm -f mmd-openwebui\nExecStart=/usr/bin/docker run --name mmd-openwebui -p 0.0.0.0:3001:8080 -e OPENAI_API_BASE_URL=https://openrouter.ai/api/v1 -e OPENAI_API_KEY={key} -e WEBUI_ADMIN_EMAIL={admin_email} -e WEBUI_ADMIN_PASSWORD={password} -e WEBUI_ADMIN_NAME=MMD -e ENABLE_SIGNUP=false -e ENABLE_OLLAMA_API=false -e ENABLE_BASE_MODELS_CACHE=true -e DEFAULT_MODELS={model} -v open-webui:/app/backend/data ghcr.io/open-webui/open-webui:v0.11.1\nExecStop=/usr/bin/docker stop mmd-openwebui\nRestart=always\n[Install]\nWantedBy=multi-user.target\n"""
    if not ok:
        return {"ok": False, "error": "credential write failed",
                "output": (out or "")[-500:]}
    ok, out = _run(["incus", "exec", "ws", "--project", project, "--",
                    "bash", "-lc", f"cat > /etc/systemd/system/{unit}.service && chmod 600 /etc/systemd/system/{unit}.service && systemctl daemon-reload && systemctl enable --now {unit}"],
                   timeout=1800, stdin_text=unit_text)
    return {"ok": ok, "output": (out or "")[-500:]}

# A SYSTEM unit running as `dev`, rather than the `openclaw gateway install`
# user service the vendor documents. A user service needs lingering enabled and
# a live XDG_RUNTIME_DIR - exactly the kind of thing that works while a human is
# logged in and fails on a machine powered on by an API call. The workspace
# already runs systemd; sshd and xrdp are managed the same way.
OPENCLAW_SERVICE = f"""[Unit]
Description=OpenClaw gateway
After=network-online.target
# GIVE UP rather than loop forever. A misconfiguration the gateway rejects at
# startup is not something a restart can fix, and without a limit systemd
# retries every five seconds indefinitely - observed at restart counter 127,
# twenty minutes of a workspace's CPU spent re-reading a config that was never
# going to be accepted. Five attempts in two minutes rides out a slow boot;
# past that the failure is real and belongs in the journal.
#
# In [Unit], not [Service]: systemd moved these in v230 and silently ignores
# them in the wrong section, which would look exactly like working.
StartLimitBurst=5
StartLimitIntervalSec=120

[Service]
Type=simple
User=dev
Group=dev
WorkingDirectory=/home/dev
Environment=HOME=/home/dev
Environment=OPENCLAW_CONFIG_PATH={OPENCLAW_CONFIG}
Environment=OPENCLAW_GATEWAY_PORT={OPENCLAW_PORT}
ExecStart=/usr/bin/env openclaw gateway run
# ALWAYS, not on-failure. The gateway restarts itself by exiting CLEANLY and
# expecting its supervisor to bring it back:
#
#     [gateway] restart mode: full process restart (supervisor restart)
#     [shutdown] completed cleanly in 250ms
#
# With on-failure that exit code 0 is "finished successfully" and systemd leaves
# it stopped - so any config change the gateway applies to itself takes the
# dashboard down until something else notices. The start limit in [Unit] is what
# keeps this from becoming an infinite loop on a genuine failure.
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""


def _openclaw_status(project: str) -> dict:
    probe = (
        "v=$(su - dev -c 'openclaw --version' 2>/dev/null | head -1); "
        "a=$(systemctl is-active openclaw-gateway 2>/dev/null); "
        "h=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "
        f"     http://127.0.0.1:{OPENCLAW_PORT}/ 2>/dev/null); "
        # The model and Telegram state come from the config the gateway is
        # actually running, not from what we believe we wrote. A page that
        # reports our intent rather than the machine's state is how "enabled"
        # and "working" drift apart.
        f"m=$(python3 -c \"import json;print(json.load(open('{OPENCLAW_CONFIG}'))"
        f"      .get('agents',{{}}).get('defaults',{{}}).get('model',{{}}).get('primary',''))\" 2>/dev/null); "
        f"tg=$(python3 -c \"import json;c=json.load(open('{OPENCLAW_CONFIG}'))"
        f"      .get('channels',{{}}).get('telegram',{{}});"
        f"print('yes' if c.get('enabled') and c.get('allowFrom') else 'no')\" 2>/dev/null); "
        "printf 'version=%s\\nactive=%s\\nhttp=%s\\nconfigured=%s\\nmodel=%s\\ntelegram=%s\\n' "
        '  "${v:-}" "${a:-}" "${h:-0}" '
        f'  "$([ -s {OPENCLAW_CONFIG} ] && echo yes || echo no)" '
        '  "${m:-}" "${tg:-no}"'
    )
    _rc, out, _err = _run_split(
        ["incus", "exec", "ws", "--project", project, "--", "bash", "-lc", probe],
        timeout=120)
    info = dict(ln.split("=", 1) for ln in (out or "").splitlines() if "=" in ln)
    version = (info.get("version") or "").strip()
    http = (info.get("http") or "0").strip()
    return {"installed": bool(version), "version": version or None,
            "configured": info.get("configured", "no").strip() == "yes",
            "model": (info.get("model") or "").strip() or None,
            "telegram": info.get("telegram", "no").strip() == "yes",
            "service_active": info.get("active", "").strip() == "active",
            # Any HTTP answer counts, including 401: the gateway requires auth
            # by default, so a challenge means it is up and protecting itself.
            "responding": http not in ("0", "000", "")}


def _openclaw_approve_devices(project: str, password: str) -> dict:
    """Approve every device waiting to pair with this customer's gateway.

    OpenClaw treats a browser reaching the Control UI as a DEVICE, and a device
    that is not "local" must be approved out of band before it may talk - even
    with the right password. `trustedProxies` restores local treatment for
    connections arriving through our proxy, which is the normal path; this is
    for the cases it does not cover, so the answer to a pairing prompt is a
    button rather than "SSH into your machine and run this command".

    Approving in bulk is defensible precisely because of who is asking: the
    request reaches here only from a customer already signed in to their own
    dashboard, looking at their own workspace. A request pending on THEIR
    gateway is one they just caused.
    """
    # `approve` takes the REQUEST id. Which field carries it is read
    # tolerantly rather than guessed: the paired entries use `deviceId`, the
    # table prints a separate "Request" column, and a pending list was not
    # available to observe when this was written. Trying the plausible names in
    # order costs nothing and cannot pick a wrong one - only a missing one.
    reader = (
        "import json,sys\\n"
        "d=json.load(sys.stdin)\\n"
        "out=[]\\n"
        "for x in (d.get('pending') or []):\\n"
        "    v=x.get('requestId') or x.get('id') or x.get('deviceId')\\n"
        "    if v: out.append(str(v))\\n"
        "print(' '.join(out))\\n"
    )
    script = (
        f"export OPENCLAW_CONFIG_PATH={OPENCLAW_CONFIG}; "
        f"printf '%b' \"{reader}\" > /tmp/.oc_pending.py; "
        "ids=$(openclaw devices list --json --password \"$OC_PW\" 2>/dev/null "
        "  | python3 /tmp/.oc_pending.py 2>/dev/null); "
        "rm -f /tmp/.oc_pending.py; "
        "[ -z \"$ids\" ] && { echo 'none pending'; exit 0; }; "
        "for i in $ids; do openclaw devices approve \"$i\" --password \"$OC_PW\" "
        "  2>&1 | tail -1; done"
    )
    rc, out, _err = _run_split(
        ["incus", "exec", "ws", "--project", project,
         "--env", f"OC_PW={password}", "--",
         "su", "-", "dev", "-c", script], timeout=180)
    return {"ok": rc == 0, "output": (out or "")[-500:]}


# The ZFS pool the workspaces live on. Read here rather than passed in: it is a
# property of the host, and the caller has no business naming a pool.
ZPOOL = os.environ.get("MMD_ZPOOL", "mmdpool")


def _verb_disk_usage(_req: dict) -> dict:
    """Real disk consumption for every workspace, in one pass.

    ONE `zfs list` for all workspaces rather than a call each: this runs on a
    timer over every workspace on the host, and a subprocess per workspace would
    make the cost of the check scale with the thing it is checking.

    Two numbers per workspace and they measure different things:

      * root   - USEDDS, the bytes this container has actually written. NOT
                 `used`, which includes the refreservation and therefore reads
                 as a constant 6 GiB whether the machine is empty or full, and
                 NOT `refer`, which counts the golden-image blocks it shares
                 with every other workspace and would bill each of them for the
                 same data.
      * docker - `used` on the zvol, which is thin, so this is real too.

    The quota figures come back alongside so the caller does not have to hold a
    second opinion about how big a workspace is supposed to be.
    """
    out: dict[str, dict] = {}

    rc, text, err = _run_split(
        ["zfs", "list", "-Hp", "-o", "name,usedds,used,available",
         "-r", f"{ZPOOL}/containers"], timeout=60)
    if rc != 0:
        return {"ok": False, "error": (err or "zfs list failed")[-300:]}
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 4 or not parts[0].endswith("_ws"):
            continue
        name = parts[0].rsplit("/", 1)[-1]          # ws-3_ws
        idx = name[3:-3]
        if not idx.isdigit():
            continue
        try:
            out.setdefault(idx, {})["root_used"] = int(parts[1])
            out[idx]["root_free"] = int(parts[3])
        except ValueError:
            continue

    rc, text, _err = _run_split(
        ["zfs", "list", "-Hp", "-o", "name,used,volsize",
         "-r", f"{ZPOOL}/custom"], timeout=60)
    if rc == 0:
        for line in (text or "").splitlines():
            parts = line.split("\t")
            if len(parts) < 3 or not parts[0].endswith("_docker"):
                continue
            name = parts[0].rsplit("/", 1)[-1]      # ws-3_docker
            idx = name[3:-7]
            if not idx.isdigit():
                continue
            try:
                out.setdefault(idx, {})["docker_used"] = int(parts[1])
                out[idx]["docker_size"] = int(parts[2])
            except ValueError:
                continue

    # Pool-wide headroom. With reservations gone this is the number that decides
    # whether the host is in trouble, and no per-workspace figure can show it.
    pool = {}
    rc, text, _err = _run_split(
        ["zfs", "list", "-Hp", "-o", "used,available", ZPOOL], timeout=60)
    if rc == 0 and text.strip():
        try:
            used, avail = text.split()[:2]
            pool = {"used": int(used), "available": int(avail)}
        except ValueError:
            pool = {}

    return {"ok": True, "workspaces": out, "pool": pool}


def _verb_ai_openclaw(project: str, req: dict) -> dict:
    action = req.get("action")
    if action not in ("status", "install", "disable", "approve_devices"):
        return {"ok": False,
                "error": "action must be status, install, disable or approve_devices"}

    if action == "status":
        return {"ok": True, **_openclaw_status(project)}

    if action == "approve_devices":
        password = (req.get("password") or "").strip()
        if not password:
            return {"ok": False, "error": "password is required"}
        return _openclaw_approve_devices(project, password)

    if action == "disable":
        # The credential goes with it. Leaving a spendable key in a config file
        # for a service the customer has just switched off would be careless.
        ok, out = _run(["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
                        "systemctl disable --now openclaw-gateway 2>/dev/null; "
                        f"rm -f {OPENCLAW_UNIT} {OPENCLAW_CONFIG}; "
                        "systemctl daemon-reload 2>/dev/null; true"], timeout=180)
        return {"ok": ok, "output": (out or "")[-300:], **_openclaw_status(project)}

    # --- install ------------------------------------------------------------
    key = (req.get("openrouter_key") or "").strip()
    password = (req.get("password") or "").strip()
    origin = (req.get("origin") or "").strip()
    model = (req.get("model") or "").strip()
    telegram_token = (req.get("telegram_token") or "").strip()
    # Comma-separated numeric Telegram user ids, exactly as Hermes takes them.
    telegram_users = [u for u in re.split(r"[,\s]+",
                                          (req.get("telegram_users") or "").strip())
                      if u.isdigit()]
    telegram_enabled = (bool(req.get("telegram_enabled")) and bool(telegram_token)
                        and bool(telegram_users))
    if not key or not password:
        return {"ok": False, "error": "openrouter_key and password are required"}
    if not model:
        return {"ok": False, "error": "model is required"}

    st = _openclaw_status(project)
    if not st["installed"]:
        ok, out = _run(["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
                        "command -v npm >/dev/null || exit 90; "
                        "npm install -g --no-fund --no-audit "
                        "  --allow-scripts=openclaw openclaw@latest"],
                       timeout=1800)
        if not ok:
            return {"ok": False, "error": "install failed",
                    "output": (out or "")[-800:]}

    # The config carries a spendable key and a dashboard password, so it goes in
    # through stdin at 0600 - never a command line, never this process's log.
    config = {
        "gateway": {
            "port": OPENCLAW_PORT,
            # Both of these were learned from the gateway refusing to start,
            # and neither is guessable from the address it ends up on.
            #
            # `bind` is a MODE, not an address: "loopback", "lan", "tailnet",
            # "auto" or "custom". Passing "0.0.0.0" is rejected outright. Of the
            # valid modes only "lan" actually gives 0.0.0.0 here - "auto" is
            # documented to detect a container and widen, but in this one it
            # resolved to loopback, which nginx cannot reach. Measured, not
            # assumed: with "auto" the gateway answered on 127.0.0.1:18789 and
            # nothing else; with "lan" it answers on 0.0.0.0:18789 and the host
            # reaches it over the bridge.
            "bind": "lan",
            # Without this the gateway refuses to start at all: "existing config
            # is missing gateway.mode. Treat this as suspicious or clobbered
            # config." It is a deliberate guard against a half-written file, and
            # writing the config ourselves is exactly the case it suspects.
            "mode": "local",
            # Reachable only from this host regardless: nothing DNATs to this
            # port, and the bridge isolation table drops workspace-to-workspace
            # traffic, so the reverse proxy is the only route in.
            # `mode` alongside the password: the gateway records which auth
            # scheme is in force, and leaving it to be inferred is how a
            # config that looks complete behaves as though auth were absent.
            "auth": {"password": password, "mode": "password"},
            # Without this the Control UI loads, then refuses to open its
            # websocket: "The Gateway rejected this page origin". It checks the
            # browser's Origin against an explicit list - no wildcards - and a
            # dashboard published under the customer's own name is by
            # definition not the gateway host.
            "controlUi": {"allowedOrigins": [origin]} if origin else {},
            # Restores "local client" treatment behind the reverse proxy.
            # Without it the gateway logs "Proxy headers detected from
            # untrusted address" and makes every browser pair as a new device
            # before it may talk.
            "trustedProxies": [OPENCLAW_TRUSTED_PROXY],
        },
        "models": {
            "providers": {
                "openrouter": {
                    "baseUrl": "https://openrouter.ai/api/v1",
                    "apiKey": key,
                }
            }
        },
        # Having a KEY is not the same as being signed in. The CLI keeps an auth
        # profile per provider, and `openclaw models auth paste-api-key` is the
        # interactive way to create one - which a provisioner cannot use. This
        # is the same record, written directly.
        "auth": {"profiles": {"openrouter:default":
                              {"provider": "openrouter", "mode": "api_key"}}},
        # THE DEFAULT MODEL, which was the gap. Without it the gateway starts,
        # answers, and quietly uses whatever OpenClaw's own default is - which
        # may not be an OpenRouter model at all, and so may be unreachable with
        # the customer's key and unbillable to their account. It fails as
        # working software, which is the worst way to fail.
        #
        # Provider-prefixed: OpenRouter's `z-ai/glm-5.2` is
        # `openrouter/z-ai/glm-5.2` here. See mmd/openclaw.normalise_model.
        "agents": {"defaults": {
            "workspace": f"{OPENCLAW_HOME}/workspace",
            "model": {"primary": model},
        }},
        "plugins": {"entries": {
            "openrouter": {"enabled": True},
            # Enabled whether or not a token is configured: the plugin being
            # available is what lets a customer turn Telegram on later without
            # a reinstall.
            "telegram": {"enabled": True},
        }},
    }

    # Telegram, by the same arrangement Hermes uses: the token comes from the
    # customer's own account settings, and it is written to a 0600 FILE that the
    # config points at rather than inlined. OpenClaw supports `tokenFile`
    # natively, so the token never sits in the config a customer might paste
    # into a support ticket.
    if telegram_enabled:
        config["channels"] = {"telegram": {
            "enabled": True,
            "tokenFile": OPENCLAW_TG_TOKEN,
            # An allowlist, never open. A bot with no policy answers ANY
            # Telegram user who finds it, and this one is wired to an agent
            # with a shell in the customer's workspace.
            "dmPolicy": "allowlist",
            "allowFrom": telegram_users,
        }}
        # BOTH lists, and this is the one that is easy to miss. `allowFrom`
        # decides who may send a direct message; `ownerAllowFrom` separately
        # decides who may use owner-scoped slash commands, and it is namespaced
        # by channel. Setting only the first leaves the customer able to talk to
        # their own agent but answered with "You are not authorized to use this
        # command" the moment they try one - configured, connected, and useless.
        config["commands"] = {"ownerAllowFrom": [f"telegram:{u}"
                                                 for u in telegram_users]}
    if telegram_enabled:
        # Through stdin at 0600, like every other credential here: never a
        # command line, never this process's log.
        ok, out = _run(["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
                        f"install -d -m 0700 -o dev -g dev {OPENCLAW_HOME} && "
                        f"install -d -m 0700 -o dev -g dev {OPENCLAW_SECRETS} && "
                        f"install -m 0600 -o dev -g dev /dev/null {OPENCLAW_TG_TOKEN} && "
                        f"cat > {OPENCLAW_TG_TOKEN}"],
                       timeout=120, stdin_text=telegram_token)
        if not ok:
            return {"ok": False, "error": "could not write the Telegram token",
                    "output": (out or "")[-300:]}
    else:
        # Removed rather than left behind: a customer who switches Telegram off
        # should not leave a live bot token on their disk.
        _run(["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
              f"rm -f {OPENCLAW_TG_TOKEN}"], timeout=60)

    ok, out = _run(["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
                    f"install -d -m 0700 -o dev -g dev {OPENCLAW_HOME} && "
                    f"install -m 0600 -o dev -g dev /dev/null {OPENCLAW_CONFIG} && "
                    f"cat > {OPENCLAW_CONFIG}"],
                   timeout=120, stdin_text=json.dumps(config))
    if not ok:
        return {"ok": False, "error": "could not write configuration",
                "output": (out or "")[-300:]}

    ok, out = _run(["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
                    f"cat > {OPENCLAW_UNIT} && systemctl daemon-reload && "
                    "systemctl enable --now openclaw-gateway"],
                   timeout=300, stdin_text=OPENCLAW_SERVICE)
    if not ok:
        return {"ok": False, "error": "could not start the gateway",
                "output": (out or "")[-800:]}

    # Wait for the dashboard to actually ANSWER before reporting success.
    # `systemctl enable --now` returning 0 only means systemd accepted the unit;
    # the same mistake made the Hermes Telegram toggle report success on
    # machines where nothing had started.
    for _ in range(20):
        st = _openclaw_status(project)
        if st["responding"]:
            return {"ok": True, **st}
        time.sleep(3)

    _rc, log, _e = _run_split(
        ["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
         "journalctl -u openclaw-gateway -n 40 --no-pager 2>/dev/null"], timeout=60)
    return {"ok": False, "error": "the gateway did not answer on its port",
            "output": (log or "")[-800:], **_openclaw_status(project)}


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

    # A starting model, if the operator has chosen one. settings.json is the
    # customer's own file - deliberately never copied from the host - so it is
    # created only when absent, and never rewritten.
    model = (req.get("model") or "").strip()
    if model and re.fullmatch(r"[A-Za-z0-9._:/-]{1,128}", model):
        _run(["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
              "test -e /home/dev/.claude/settings.json || "
              "{ install -m 0600 -o dev -g dev /dev/stdin "
              "/home/dev/.claude/settings.json; }"],
             timeout=60, stdin_text=json.dumps({"model": model}) + "\n")

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

    if verb in ("ai_usage", "codex_usage"):
        # Counted INSIDE the workspace. The session logs run to tens of
        # megabytes; pulling them to the host to parse them here would repeat
        # the mistake the zip download made, and would put a customer's
        # conversations on the host for no reason. Only totals come back.
        codex = verb == "codex_usage"
        scanner = CODEX_USAGE_SCAN if codex else AI_USAGE_SCAN
        target = "~/.codex/sessions" if codex else "~/.claude/projects"
        try:
            script = scanner.read_text()
        except OSError as e:  # noqa: BLE001
            return {"ok": False, "error": f"cannot read {scanner}: {e}"}

        # Piped to `python3 -` rather than written to the workspace: this runs
        # every few minutes and leaving a file behind on a machine the customer
        # owns, to be found and wondered about, is worse than passing it on
        # stdin each time.
        rc, out, err = _run_split(
            ["incus", "exec", "ws", "--project", project, "--",
             "su", "-", "dev", "-c", f"python3 - {target}"],
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

    if verb == "ai_codex":
        return _verb_ai_codex(project, req)

    if verb == "ai_openclaw":
        return _verb_ai_openclaw(project, req)

    if verb == "ai_managed_web":
        return _verb_ai_managed_web(project, req)

    if verb == "disk_usage":
        return _verb_disk_usage(req)

    if verb == "apt_repair":
        # Repairs both families. They share a cause - dpkg cannot do privileged
        # things inside an unprivileged container - and an operator reaching for
        # "repair" wants the machine working, not one named subsystem.
        net_ok, net_out = _apply_net_fixups(project)
        ok, out = _apply_apt_fixups(project)
        if not net_ok:
            out = f"{net_out}\n{out}"
        return {"ok": ok, "output": out}

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
                            "systemctl reset-failed hermes-dashboard hermes-gateway "
                            ">/dev/null 2>&1; "
                            + HERMES_USER_GATEWAY_PURGE +
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
                 HERMES_USER_GATEWAY_PURGE +
                 "cat > /etc/systemd/system/hermes-gateway.service && "
                 "chmod 0644 /etc/systemd/system/hermes-gateway.service && "
                 "systemctl daemon-reload && systemctl enable hermes-gateway >/dev/null 2>&1 && "
                 "systemctl restart hermes-gateway && sleep 6 && "
                 "systemctl is-active hermes-gateway && "
                 # Two failures look identical to the customer - a silent bot -
                 # so both are treated as a failed enable rather than reported
                 # as success. The second is Telegram refusing a duplicate
                 # poller, which is the symptom the purge above exists to stop
                 # and the one check that would notice it coming back.
                 "! journalctl -u hermes-gateway -n 40 --no-pager | "
                 "grep -qE 'No messaging platforms enabled|terminated by other getUpdates'"],
                timeout=120, stdin_text=HERMES_GATEWAY_UNIT)
            if rc != 0 or "active" not in gout.splitlines():
                return {"ok": False, "error": "telegram gateway failed",
                        "output": (gout + gerr)[-900:]}
        else:
            _, gout = _run(
                ["incus", "exec", "ws", "--project", project, "--", "bash", "-lc",
                 HERMES_USER_GATEWAY_PURGE +
                 "systemctl disable --now hermes-gateway >/dev/null 2>&1 || true; "
                 "systemctl is-active --quiet hermes-gateway && echo gateway=1 || echo gateway=0; "
                 "systemctl reset-failed hermes-gateway >/dev/null 2>&1; "
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
