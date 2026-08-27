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
    location / {{
        proxy_pass http://{ip}:{port};
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

        # A customer's own service, on their own machine, with their own quota.
        client_max_body_size 0;
    }}
}}
"""


def desired(db, CONFIG, usernames) -> dict[str, list[tuple[str, str, int]]]:
    """Every host that should exist, grouped by the customer who owns it.

    Grouped rather than flat because the nginx file is per customer, and
    because the certificate decision is per customer too.
    """
    from mmd.models import Workspace
    from sqlalchemy import select

    out: dict[str, list[tuple[str, str, int]]] = {}
    domain = CONFIG.domain

    for ws in db.execute(select(Workspace)).scalars().all():
        user = ws.user
        if not user or not user.username or not SAFE.match(user.username):
            continue
        # Hermes: only once the worker has actually minted a key and the
        # dashboard has credentials. Publishing the name earlier would serve a
        # 502 at an address we had just told the customer was ready.
        if (ws.hermes_enabled and ws.hermes_key_hash
                and ws.hermes_dash_user and ws.hermes_dash_password):
            host = usernames.hermes_host(user.username, domain)
            out.setdefault(user.username, []).append(
                (host, f"10.42.0.{ws.idx + 10}", HERMES_PORT))

    return out


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
    for username, hosts in sorted(wanted.items()):
        wildcard = obtain_wildcard(username, CONFIG.domain, email)
        blocks = []
        for host, ip, port in sorted(hosts):
            cert = wildcard or obtain_single(host, email)
            if cert is None:
                continue          # try again next pass; the port still works
            blocks.append(server_block(host, cert, ip, port))
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

    sh("systemctl", "reload", "nginx")
    print("  nginx reloaded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
