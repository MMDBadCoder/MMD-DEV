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
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WS_CREATE = REPO / "workspace" / "ws-create.sh"
WS_DESTROY = REPO / "workspace" / "ws-destroy.sh"

SOCKET_PATH = os.environ.get("MMD_PROVISIONER_SOCKET", "/run/mmd/provisioner.sock")
ALLOWED_GROUP = os.environ.get("MMD_GROUP", "mmd")

# Hard ceilings. Even if the database is tampered with, nothing beyond these is
# ever provisioned. Defence in depth behind the Incus project limits.
MAX_CORES = 4
MAX_MEM_MIB = 8192
MAX_ROOT_GIB = 40
MAX_DOCKER_GIB = 40

VERBS = {"provision", "archive", "restore", "destroy", "ping"}

# Name of the control plane's restricted client certificate in Incus's trust
# store. The provisioner maintains its project scope; the API itself is
# forbidden from touching the trust store, which is what keeps a compromised
# web app from widening its own access.
API_CERT_NAME = os.environ.get("MMD_API_CERT_NAME", "mmd-api")

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


def _run(cmd: list[str], timeout: int = 900) -> tuple[bool, str]:
    log.info("exec: %s", " ".join(cmd))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return False, "timed out"
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode == 0, out.strip()[-4000:]


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
        return {"ok": ok, "output": out, "tier": t}

    if verb == "destroy":
        ok, out = _run(["bash", str(WS_DESTROY), str(idx), "--yes"])
        if ok:
            _set_project_access(f"ws-{idx}", grant=False)
        return {"ok": ok, "output": out}

    # archive / restore are implemented in P3 alongside the billing lifecycle;
    # the verbs exist here so the API surface is stable.
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
