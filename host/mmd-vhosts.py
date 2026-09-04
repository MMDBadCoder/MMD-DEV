#!/usr/bin/env python3
"""Publish each customer's Hermes dashboard at hermes.<username>.<domain>.

Why this is its own root unit
-----------------------------
Two jobs here need root and the internet: obtaining a certificate, and writing
nginx configuration. Neither can go where it would otherwise belong.

  * mmd-provisioner is the root component, but it runs with IPAddressDeny=any
    precisely so a compromise cannot phone home. ACME needs the internet, so
    putting it there would mean removing the single control that makes a root
    daemon acceptable.
  * mmd-worker has the internet but runs as `mmd`, and cannot write to
    /etc/nginx or run certbot.

So this is a third, small, dumb reconciler on a timer. It reads the database,
makes the host match it, and does nothing else - no network listener, nothing
it does depends on data a customer controls beyond two DNS labels, both of
which are validated before they are stored and re-validated here.

Certificates: wildcard first
----------------------------
A wildcard certificate for *.<domain> covers ONE label. hermes.ali.<domain> is
two deep, so the dashboard's own certificate does not cover it. That leaves two
ways to certify these names:

  * DNS-01 for `*.<username>.<domain>` - ONE certificate per customer, issued
    once, covering every name that customer will ever have under it. Needs an
    API-driven DNS provider: MMD_ACME_DNS_PLUGIN and MMD_ACME_DNS_CREDENTIALS.
  * HTTP-01 per host - one certificate per name, needing no DNS credentials at
    all. Let's Encrypt allows 50 NEW certificates per registered domain per
    week, and RENEWALS are exempt, so at roughly one name per customer the cost
    is one certificate each, once. Comfortably inside the limit.

The wildcard is an optimisation rather than a prerequisite: it removes the
issuance wait and bounds churn. It is tried whenever it is configured, and
everything else in this file is identical either way.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import time

sys.path.insert(0, "/opt/mmd/control")

NGINX_DIR = pathlib.Path("/etc/nginx/sites-enabled")
STATE_DIR = pathlib.Path("/var/lib/mmd/hermes")
WEBROOT = "/var/www/acme"

# One file per CUSTOMER, not per host. `nginx -t` and a reload re-read every
# file in this directory, so grouping by customer keeps the cost of a change
# proportional to the number of customers rather than the number of names.
PREFIX = "mmd-vhost-"
OLD_PREFIX = "mmd-hermes-"     # what mmd-hermes-vhosts.py wrote; cleaned up

HERMES_PORT = 9119
# The OpenClaw gateway's fixed port inside a workspace. Must agree with
# provisioner.OPENCLAW_PORT and app.OPENCLAW_PORT.
OPENCLAW_PORT = 18789
OPENCODE_PORT = 4096
OPENWEBUI_PORT = 3001

# A failed issuance is retried on a growing delay. Let's Encrypt's limit is per
# registered domain, so one customer whose DNS is wrong could otherwise spend
# every other customer's quota retrying every two minutes.
BACKOFF = (5 * 60, 30 * 60, 2 * 3600, 12 * 3600)

# NEW certificates we will start in any trailing 7 days. Ten below Let's
# Encrypt's 50 so the dashboard's own certificate, and any manual issuance,
# still have room.
#
# Renewals do not count against that limit, so this is not a cap on how many
# names may exist - it is a cap on how fast NEW ones may appear. Names that sit
# there cost one certificate each, once; names created and destroyed every day
# would exhaust the quota and take renewal down with it, which is what this
# bounds.
ISSUE_BUDGET = 40
ISSUE_LOG = "issued.json"

# Matches what usernames.py already guarantees. Re-checked here anyway: this
# value is interpolated into a config file that root reloads, and a validator
# living in another process is not a guarantee, it is an assumption.
SAFE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def sh(*args: str, timeout: int = 300) -> tuple[int, str]:
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()


# The platform's own ports, listed rather than detected.
#
# Detection alone is not enough, and the reason is worth recording. The control
# plane binds 127.0.0.1:8000; nginx binds 0.0.0.0:8000 for a customer who
# published internal port 8000. Both SUCCEED - each sets SO_REUSEADDR, and a
# wildcard and a loopback bind can coexist - and loopback keeps reaching the API
# only because the more specific bind wins. That is luck, not design: it depends
# on which process started first, and if the API ever restarted into a taken
# socket its own requests would be proxied into a customer's workspace.
#
# So these are refused outright, whoever happens to hold them at the time.
PLATFORM_PORTS = {
    22,     # the operator's sshd - the way back into the host
    53,     # the bridge's DNS
    80, 443,  # the dashboard itself
    5432,   # PostgreSQL
    8000,   # mmd-api (uvicorn, loopback)
    8443,   # the Incus API
    9101,   # Incus metrics
}


def foreign_listeners() -> set[int]:
    """TCP ports held by something OTHER than nginx.

    The distinction matters and a plain bind probe cannot make it. nginx
    already listens on every application port this reconciler has published,
    so "is the port busy" answers yes for exactly the ports that are working -
    and skipping those withdraws the routes it just created. Measured: a first
    version of this check removed two customers' live addresses on the pass
    that introduced it.

    What must be skipped is a port some OTHER service holds - uvicorn on 8000,
    sshd on 22, Postgres on 5432 - because nginx cannot bind it, and a failed
    bind makes nginx abandon the entire reload rather than that one server
    block, freezing every later change including certbot's renewal hook.
    """
    rc, out = sh("ss", "-tlnpH")
    if rc != 0:
        return set()
    busy: set[int] = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        try:
            port = int(local.rsplit(":", 1)[1])
        except (IndexError, ValueError):
            continue
        if "nginx" not in line:
            busy.add(port)
    return busy


def load_env(path: str = "/etc/mmd/api.env") -> None:
    try:
        for line in pathlib.Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


# --- issuance accounting ---------------------------------------------------
def _issue_log() -> list[float]:
    try:
        data = json.loads((STATE_DIR / ISSUE_LOG).read_text())
        return [float(t) for t in data][-500:]
    except (OSError, ValueError, TypeError):
        return []


def budget_left() -> int:
    week_ago = time.time() - 7 * 86400
    return ISSUE_BUDGET - len([t for t in _issue_log() if t >= week_ago])


def note_issue() -> None:
    week_ago = time.time() - 7 * 86400
    kept = [t for t in _issue_log() if t >= week_ago] + [time.time()]
    (STATE_DIR / ISSUE_LOG).write_text(json.dumps(kept))


def backoff_ok(key: str) -> bool:
    f = STATE_DIR / f"{key}.fail"
    if not f.exists():
        return True
    try:
        n, last = f.read_text().split()
        n, last = int(n), float(last)
    except (ValueError, OSError):
        return True
    return time.time() - last >= BACKOFF[min(n, len(BACKOFF) - 1)]


def note_failure(key: str) -> None:
    f = STATE_DIR / f"{key}.fail"
    n = 0
    if f.exists():
        try:
            n = int(f.read_text().split()[0])
        except (ValueError, OSError, IndexError):
            n = 0
    f.write_text(f"{n + 1} {time.time()}")


def clear_failure(key: str) -> None:
    (STATE_DIR / f"{key}.fail").unlink(missing_ok=True)


# --- certificates ----------------------------------------------------------
def live(name: str) -> tuple[pathlib.Path, pathlib.Path]:
    d = pathlib.Path("/etc/letsencrypt/live") / name
    return d / "fullchain.pem", d / "privkey.pem"


def dns_plugin() -> tuple[str, str] | None:
    """The configured DNS-01 plugin and its credentials file, if any."""
    plugin = os.environ.get("MMD_ACME_DNS_PLUGIN", "").strip()
    creds = os.environ.get("MMD_ACME_DNS_CREDENTIALS", "").strip()
    if plugin and creds and pathlib.Path(creds).exists():
        return plugin, creds
    return None


def acme_account_args(email: str) -> list[str]:
    return (["--email", email] if email
            else ["--register-unsafely-without-email"])


def obtain_wildcard(username: str, domain: str, email: str) -> str | None:
    """One certificate covering every name this customer will ever publish.

    Returns the certbot --cert-name on success. Issued once and then left to
    certbot's renewal timer, so a second name under the same customer costs no
    issuance at all - which is the whole reason this path exists.
    """
    plugin = dns_plugin()
    if plugin is None:
        return None
    name = f"wildcard-{username}"
    full, _ = live(name)
    if full.exists():
        return name
    if not backoff_ok(name):
        return None

    which, creds = plugin
    rc, out = sh(
        "certbot", "certonly", f"--dns-{which}",
        f"--dns-{which}-credentials", creds,
        "--dns-propagation-seconds",
        os.environ.get("MMD_ACME_DNS_PROPAGATION", "60"),
        "--cert-name", name,
        "-d", f"*.{username}.{domain}", "-d", f"{username}.{domain}",
        "--non-interactive", "--agree-tos", "--keep-until-expiring",
        *acme_account_args(email), timeout=600)
    note_issue()
    if rc != 0 or not full.exists():
        note_failure(name)
        print(f"  wildcard {username}: FAILED "
              f"({out.splitlines()[-1][:120] if out else 'no output'})")
        return None
    clear_failure(name)
    print(f"  wildcard *.{username}.{domain}: obtained")
    return name


def obtain_single(host: str, email: str) -> str | None:
    """The fallback: one HTTP-01 certificate for one host."""
    full, _ = live(host)
    if full.exists():
        return host
    if not backoff_ok(host):
        return None
    if budget_left() <= 0:
        # Deliberately not an error. The port still works; only the pretty name
        # is late. Burning the quota would take out certificate renewal for
        # every customer, which is far worse than one address arriving slowly.
        print(f"  cert {host}: DEFERRED (weekly issuance budget spent)")
        return None

    rc, out = sh("certbot", "certonly", "--webroot", "-w", WEBROOT, "-d", host,
                 "--non-interactive", "--agree-tos", "--keep-until-expiring",
                 *acme_account_args(email), timeout=180)
    note_issue()
    if rc != 0 or not full.exists():
        note_failure(host)
        print(f"  cert {host}: FAILED "
              f"({out.splitlines()[-1][:120] if out else 'no output'})")
        return None
    clear_failure(host)
    print(f"  cert {host}: obtained")
    return host


# --- nginx -----------------------------------------------------------------
def proxy_location(ip: str, port: int) -> str:
    return f"""
    location / {{
        proxy_pass http://{ip}:{port};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_buffering off;
        client_max_body_size 0;
    }}
"""


def server_block(host: str, cert: str, ip: str, port: int) -> str:
    full, key = live(cert)
    return f"""
server {{
    listen 80;
    server_name {host};
    location ^~ /.well-known/acme-challenge/ {{ root {WEBROOT}; }}
    location / {{ return 301 https://{host}$request_uri; }}
}}

server {{
    listen 443 ssl;
    http2 on;
    server_name {host};

    ssl_certificate     {full};
    ssl_certificate_key {key};

    # Deliberately NO Strict-Transport-Security here. The dashboard's own policy
    # is sent without includeSubDomains so that customers' plain-HTTP services on
    # published ports keep working; adding one here would re-introduce exactly
    # that problem for this name.

    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;

    # The workspace is reachable only from this host: nothing DNATs to it on
    # this port and the isolation table drops bridge-to-bridge traffic, so this
    # proxy is the only route in.
{proxy_location(ip, port)}
}}
"""


def application_server_block(host: str, ip: str, port: int) -> str:
    """Plain HTTP by design; raw TCP/UDP stays on the external-port route."""
    return f"""
server {{
    listen {port};
    server_name {host};
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;
{proxy_location(ip, port)}
}}
"""


def desired(db, CONFIG, usernames) -> dict[str, list[tuple[str, str, int, int | None]]]:
    """Every host that should exist, grouped by the customer who owns it.

    Grouped rather than flat because the nginx file is per customer, and
    because the certificate decision is per customer too.
    """
    from mmd.models import ExposedPort, PortKind, Workspace
    from sqlalchemy import select

    # Computed ONCE per pass rather than probed per port: one `ss` call instead
    # of a bind attempt each, and - the part that matters - it can tell nginx's
    # own listeners from another service's.
    busy = PLATFORM_PORTS | foreign_listeners()
    out: dict[str, list[tuple[str, str, int]]] = {}
    domain = CONFIG.domain

    for ws in db.execute(select(Workspace)).scalars().all():
        user = ws.user
        if not user or not user.username or not SAFE.match(user.username):
            continue
        # Hermes: only once the worker has actually minted a key and the
        # dashboard has credentials. Publishing the name earlier would serve a
        # 502 at an address we had just told the customer was ready.
        if (ws.hermes_enabled and ws.hermes_installed
                and ws.hermes_dash_user and ws.hermes_dash_password):
            host = usernames.hermes_host(user.username, domain)
            out.setdefault(user.username, []).append(
                (host, f"10.42.0.{ws.idx + 10}", HERMES_PORT, None))
        # OpenClaw: the same rule as Hermes, and for the same reason. `enabled`
        # is only the customer's intent; `installed` is the worker having
        # actually started the gateway, and the password is what makes the
        # address usable at all. Publishing on intent alone would serve a 502
        # at a name we had just told the customer was ready.
        if (ws.openclaw_enabled and ws.openclaw_installed
                and ws.openclaw_password):
            out.setdefault(user.username, []).append(
                (f"openclaw.{user.username}.{domain}",
                 f"10.42.0.{ws.idx + 10}", OPENCLAW_PORT, None))
        if ws.opencode_enabled and ws.opencode_installed:
            out.setdefault(user.username, []).append(
                (usernames.opencode_host(user.username, domain),
                 f"10.42.0.{ws.idx + 10}", OPENCODE_PORT, None))
        if ws.openwebui_enabled and ws.openwebui_installed:
            out.setdefault(user.username, []).append(
                (usernames.openwebui_host(user.username, domain),
                 f"10.42.0.{ws.idx + 10}", OPENWEBUI_PORT, None))
        for port in db.execute(select(ExposedPort).where(
                ExposedPort.workspace_id == ws.id,
                ExposedPort.kind == PortKind.USER)).scalars():
            # Never emit `listen <port>;` for a port the host already holds.
            # nginx cannot bind it, and a failed bind abandons the ENTIRE
            # reload - freezing every later configuration change, certbot's
            # renewal hook included, not merely losing this one address.
            if port.internal_port in busy:
                print(f"  skipping {user.username}:{port.internal_port}"
                      f" - reserved by the host or already in use")
                continue
            host = usernames.application_host(
                user.username, port.internal_port, domain)
            out.setdefault(user.username, []).append(
                (host, f"10.42.0.{ws.idx + 10}", port.internal_port,
                 port.internal_port))

    return out


def mark_readiness(db, published_hosts: set[str], domain: str, usernames) -> None:
    """Expose a dashboard link only after its exact nginx host exists."""
    from mmd.models import ExposedPort, PortKind, Workspace
    from sqlalchemy import select

    for ws in db.execute(select(Workspace)).scalars().all():
        user = ws.user
        host = (usernames.hermes_host(user.username, domain)
                if user and user.username else "")
        ws.hermes_vhost_ready = host in published_hosts
        if not user or not user.username:
            continue
        for port in db.execute(select(ExposedPort).where(
                ExposedPort.workspace_id == ws.id,
                ExposedPort.kind == PortKind.USER)).scalars():
            address = f"{usernames.application_host(user.username, port.internal_port, domain)}:{port.internal_port}"
            port.web_ready = address in published_hosts
    db.commit()


def main() -> int:
    load_env()
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

    from mmd import usernames
    from mmd.config import CONFIG
    from mmd.db import SessionLocal

    email = os.environ.get("MMD_ACME_DNS_EMAIL") or os.environ.get("MMD_ACME_EMAIL", "")

    with SessionLocal() as db:
        wanted = desired(db, CONFIG, usernames)

    # --- decide the file contents, obtaining certificates as needed --------
    bodies: dict[pathlib.Path, str] = {}
    published_hosts: set[str] = set()
    for username, hosts in sorted(wanted.items()):
        wildcard = None
        blocks = []
        for host, ip, port, public_port in sorted(hosts,
                                                  key=lambda h: (h[0], h[2])):
            if public_port:
                blocks.append(application_server_block(host, ip, port))
            else:
                wildcard = wildcard or obtain_wildcard(
                    username, CONFIG.domain, email)
                cert = wildcard or obtain_single(host, email)
                if cert is None:
                    continue
                blocks.append(server_block(host, cert, ip, port))
            published_hosts.add(f"{host}:{public_port}" if public_port else host)
        if blocks:
            bodies[NGINX_DIR / f"{PREFIX}{username}.conf"] = (
                "# Managed by mmd-vhosts. Edits are overwritten.\n"
                + "".join(blocks))

    # --- apply, with the old state kept so a bad config can be undone ------
    # The previous version of this reconciler left a broken file on disk when
    # `nginx -t` failed, so the next unrelated reload - a certbot renewal, say -
    # would fail too, long after the log that explained why had scrolled past.
    stale = [f for f in NGINX_DIR.glob(f"{PREFIX}*") if f not in bodies]
    stale += list(NGINX_DIR.glob(f"{OLD_PREFIX}*"))     # one-time migration
    snapshot: dict[pathlib.Path, str | None] = {}
    changed = False

    for path, body in bodies.items():
        if path.exists() and path.read_text() == body:
            continue
        snapshot[path] = path.read_text() if path.exists() else None
        path.write_text(body)
        changed = True
        print(f"  wrote {path.name}")

    for path in stale:
        snapshot[path] = path.read_text()
        path.unlink()
        changed = True
        print(f"  removed {path.name}")

    if not changed:
        with SessionLocal() as db:
            mark_readiness(db, published_hosts, CONFIG.domain, usernames)
        return 0

    rc, out = sh("nginx", "-t")
    if rc != 0:
        for path, previous in snapshot.items():
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(previous)
        print(f"  nginx -t FAILED, rolled back and did not reload:\n{out}")
        return 1

    # CHECKED, not fired and forgotten. `nginx -t` validates syntax without
    # binding anything, so a configuration that cannot take a port passes the
    # test and then fails the reload - and this used to print "nginx reloaded"
    # and mark the addresses ready regardless, which is how a broken reload
    # went unnoticed for days on the live host.
    rc, out = sh("systemctl", "reload", "nginx")
    if rc != 0:
        print(f"  nginx reload FAILED, addresses left unpublished:\n{out[-500:]}")
        return 1
    with SessionLocal() as db:
        mark_readiness(db, published_hosts, CONFIG.domain, usernames)
    print("  nginx reloaded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
