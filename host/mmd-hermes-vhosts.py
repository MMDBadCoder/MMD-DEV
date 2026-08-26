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
makes the host match it, and does nothing else - no network listener, no input
from customers, nothing it does depends on data a customer controls beyond the
username, which is validated as a DNS label before it is ever stored.

Why per-user certificates
-------------------------
A wildcard certificate for *.<domain> covers ONE label. hermes.ali.<domain> is
two deep, so the existing certificate does not cover it and cannot be made to
without DNS-01 and a wildcard-of-wildcards. DNS already resolves two labels, so
each customer gets their own HTTP-01 certificate instead - about a dozen certs
against a 50/week limit, with backoff so a failing name cannot burn the quota.
"""
from __future__ import annotations

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
PREFIX = "mmd-hermes-"
PORT = 9119

# A failed issuance is retried on a growing delay. Let's Encrypt's limit is per
# registered domain, so one customer whose DNS is wrong could otherwise spend
# every other customer's quota retrying every two minutes.
BACKOFF = (5 * 60, 30 * 60, 2 * 3600, 12 * 3600)

# Matches what usernames.py already guarantees. Re-checked here anyway: this
# value is interpolated into a config file that root reloads, and a validator
# living in another process is not a guarantee, it is an assumption.
SAFE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


def sh(*args: str, timeout: int = 300) -> tuple[int, str]:
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()


def load_env(path: str = "/etc/mmd/api.env") -> None:
    try:
        for line in pathlib.Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


def cert_paths(host: str) -> tuple[pathlib.Path, pathlib.Path]:
    live = pathlib.Path("/etc/letsencrypt/live") / host
    return live / "fullchain.pem", live / "privkey.pem"


def backoff_ok(host: str) -> bool:
    f = STATE_DIR / f"{host}.fail"
    if not f.exists():
        return True
    try:
        n, last = f.read_text().split()
        n, last = int(n), float(last)
    except (ValueError, OSError):
        return True
    return time.time() - last >= BACKOFF[min(n, len(BACKOFF) - 1)]


def note_failure(host: str) -> None:
    f = STATE_DIR / f"{host}.fail"
    n = 0
    if f.exists():
        try:
            n = int(f.read_text().split()[0])
        except (ValueError, OSError, IndexError):
            n = 0
    f.write_text(f"{n + 1} {time.time()}")


def clear_failure(host: str) -> None:
    (STATE_DIR / f"{host}.fail").unlink(missing_ok=True)


def obtain_cert(host: str, email: str) -> bool:
    full, _key = cert_paths(host)
    if full.exists():
        return True
    if not backoff_ok(host):
        return False
    args = ["certbot", "certonly", "--webroot", "-w", WEBROOT, "-d", host,
            "--non-interactive", "--agree-tos", "--keep-until-expiring"]
    args += (["--email", email] if email else ["--register-unsafely-without-email"])
    rc, out = sh(*args, timeout=180)
    if rc != 0 or not full.exists():
        note_failure(host)
        print(f"  cert {host}: FAILED ({out.splitlines()[-1][:120] if out else 'no output'})")
        return False
    clear_failure(host)
    print(f"  cert {host}: obtained")
    return True


def vhost(host: str, ip: str) -> str:
    full, key = cert_paths(host)
    return f"""# Managed by mmd-hermes-vhosts. Edits are overwritten.
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

    # The dashboard is a development tool, not a public page.
    add_header X-Frame-Options SAMEORIGIN always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy no-referrer always;

    # No auth_basic here, and that is deliberate rather than an omission.
    # Hermes serves its own sign-in page and REFUSES to bind a non-loopback
    # address without an auth provider configured - there is no unauthenticated
    # public-bind option at all. It does not accept HTTP basic credentials
    # (measured: a correct Authorization header still lands on /login), so an
    # nginx gate could not share the browser's one prompt and would mean two
    # separate logins with the same username and password. That is friction
    # customers would report as a bug, in exchange for a second check on a
    # credential the first check already validates.
    #
    # The workspace is reachable only from this host: nothing DNATs port {PORT}
    # and the isolation table drops bridge-to-bridge traffic, so this proxy is
    # the only route in.
    location / {{
        proxy_pass http://{ip}:{PORT};
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;

        # The dashboard embeds a terminal over a websocket; without these the
        # page loads and the shell silently never connects.
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_buffering off;
    }}
}}
"""


def main() -> int:
    load_env()
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

    from mmd import usernames
    from mmd.config import CONFIG
    from mmd.db import SessionLocal
    from mmd.models import Workspace
    from sqlalchemy import select

    email = os.environ.get("MMD_ACME_EMAIL", "")
    wanted: dict[str, str] = {}

    with SessionLocal() as db:
        rows = db.execute(select(Workspace).where(
            Workspace.hermes_enabled.is_(True),
            Workspace.hermes_key_hash.isnot(None))).scalars().all()
        for ws in rows:
            user = ws.user
            if not user or not user.username or not SAFE.match(user.username):
                continue
            if not ws.hermes_dash_user or not ws.hermes_dash_password:
                continue
            host = usernames.hermes_host(user.username, CONFIG.domain)
            wanted[host] = f"10.42.0.{ws.idx + 10}"

    changed = False
    for host, ip in wanted.items():
        conf = NGINX_DIR / f"{PREFIX}{host}"
        if not obtain_cert(host, email):
            continue
        body = vhost(host, ip)
        if not conf.exists() or conf.read_text() != body:
            conf.write_text(body)
            changed = True
            print(f"  vhost {host} -> {ip}:{PORT}")

    # Withdraw the ones no longer enabled. The certificate is deliberately kept:
    # it is valid for months, re-issuing costs quota, and a customer toggling
    # the feature off and on again is the common case.
    for conf in NGINX_DIR.glob(f"{PREFIX}*"):
        host = conf.name[len(PREFIX):]
        if host not in wanted:
            conf.unlink()
            changed = True
            print(f"  removed {host}")

    if changed:
        rc, out = sh("nginx", "-t")
        if rc != 0:
            # Never reload a configuration that does not parse: it would take
            # the dashboard down for every customer, not just this one.
            print(f"  nginx -t FAILED, not reloading:\n{out}")
            return 1
        sh("systemctl", "reload", "nginx")
        print("  nginx reloaded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
