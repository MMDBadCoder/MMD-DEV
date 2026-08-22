#!/usr/bin/env bash
# Can a fully compromised control plane escape to the host?
#
# This is the load-bearing security test of the whole design. The web app holds
# a RESTRICTED Incus certificate; every attempt below must be refused by INCUS
# ITSELF, not by application logic - because application logic is exactly what
# an attacker with RCE already controls.
set -u
cd "$(dirname "$0")/.."
set -a; . /etc/mmd/api.env; set +a
/opt/mmd/venv/bin/python - "$@" <<'EOF'
import asyncio, sys
sys.path.insert(0, "control")
from mmd.incus.client import IncusClient, IncusConfig, IncusError
from mmd.config import CONFIG

PROJECT = "ws-1"
passed = failed = 0

def ok(msg, detail=""):
    global passed; passed += 1
    print(f"  \033[1;32mREFUSED\033[0m  {msg}")
    if detail: print(f"            {detail[:110]}")

def bad(msg):
    global failed; failed += 1
    print(f"  \033[1;31mALLOWED\033[0m  {msg}  <-- ESCALATION POSSIBLE")

async def attempt(c, label, coro):
    try:
        await coro
        bad(label)
    except IncusError as e:
        ok(label, str(e))

async def main():
    c = IncusClient(IncusConfig(CONFIG.incus_url, CONFIG.incus_client_cert,
                                CONFIG.incus_client_key, CONFIG.incus_server_cert))
    print("Attempting privilege escalation with the control plane's own credential\n")
    try:
        # 1. Mount the host filesystem into the workspace. The classic escape:
        #    if this works, the tenant reads /etc/shadow and every other
        #    tenant's data.
        await attempt(c, "mount host / into the workspace",
            c._request("PUT", f"/1.0/instances/ws", project=PROJECT, json={
                "devices": {"escape": {"type": "disk", "source": "/", "path": "/mnt/host"}}}))

        # 2. Privileged container: root inside would be real root on the host.
        await attempt(c, "set security.privileged=true",
            c._request("PATCH", "/1.0/instances/ws", project=PROJECT,
                       json={"config": {"security.privileged": "true"}}))

        # 3. Custom idmap - map container root onto host uid 0.
        await attempt(c, "set a custom idmap (raw.idmap uid 0)",
            c._request("PATCH", "/1.0/instances/ws", project=PROJECT,
                       json={"config": {"raw.idmap": "both 0 0"}}))

        # 4. raw.lxc lets you set arbitrary LXC config, bypassing everything.
        await attempt(c, "inject raw.lxc config",
            c._request("PATCH", "/1.0/instances/ws", project=PROJECT,
                       json={"config": {"raw.lxc": "lxc.apparmor.profile=unconfined"}}))

        # 5. Create a second instance - the project caps containers at 1.
        await attempt(c, "create a second instance in the project",
            c._request("POST", "/1.0/instances", project=PROJECT, json={
                "name": "escape", "source": {"type": "image", "alias": "mmd-workspace"}}))

        # 6. Reach a project the certificate is not scoped to.
        await attempt(c, "operate in the 'default' project",
            c._request("GET", "/1.0/instances", project="default"))

        # 7. Create a brand new, unrestricted project.
        await attempt(c, "create a new unrestricted project",
            c._request("POST", "/1.0/projects", json={"name": "escape-proj", "config": {}}))

        # 8. Add a host device node (e.g. the raw disk).
        await attempt(c, "attach a host unix-block device",
            c._request("PUT", "/1.0/instances/ws", project=PROJECT, json={
                "devices": {"vda": {"type": "unix-block", "source": "/dev/vda"}}}))

        # 9. Widen its own trust: grant itself an unrestricted certificate.
        await attempt(c, "modify the trust store",
            c._request("POST", "/1.0/certificates", json={
                "type": "client", "certificate": "x", "name": "backdoor"}))

        # 10. Turn off syscall interception restrictions on the project itself.
        await attempt(c, "relax its own project restrictions",
            c._request("PATCH", f"/1.0/projects/{PROJECT}",
                       json={"config": {"restricted": "false"}}))
    finally:
        await c.aclose()

    print(f"\n  {passed} refused, {failed} allowed")
    sys.exit(1 if failed else 0)

asyncio.run(main())
EOF
