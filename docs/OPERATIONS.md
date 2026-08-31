# Operations

Runbook for whoever is holding this. Everything here has been run on the real
host.

---

## Layout on the host

| Path | What |
|---|---|
| `/opt/mmd/` | The **deployed** copy of the code that the services actually run |
| `/opt/mmd/venv/` | Python virtualenv |
| `/etc/mmd/api.env` | Secrets: database URL, session key. Not in the repository |
| `/var/lib/mmd/certs/` | Incus client and metrics certificates |
| `/var/lib/mmd/host-claude/` | Read-only bind of the operator's Claude sign-in |
| `/run/mmd/provisioner.sock` | The provisioner's unix socket, `0770 root:mmd` |
| `/var/lib/incus/` | Incus state |
| `mmdpool` | ZFS pool on a preallocated file vdev |

Services: `mmd-api`, `mmd-worker`, `mmd-provisioner`, plus `incus`, `postgresql`,
`nginx`.

---

## Deploying a change

The services run from `/opt/mmd`, **not** from the repository.

```bash
cd /path/to/MMD-DEV
bash tests/run.sh || exit 1                 # never deploy a failing tree

sudo rm -rf /opt/mmd/{control,workspace,host,web,image}
sudo cp -r control workspace host web image /opt/mmd/
sudo systemctl restart mmd-api mmd-worker mmd-provisioner

systemctl is-active mmd-api mmd-worker mmd-provisioner
bash verify/p0-foundation.sh
```

Two traps:

- **`image/` is a runtime dependency.** The provisioner runs
  `image/apt-fixups.sh` inside workspaces and resolves the path relative to its
  own install directory. Omitting it breaks `apt_repair` silently.
- **New provisioner verbs need a provisioner restart.** Restarting only
  `mmd-api` leaves the daemon on old code, and the verb comes back as
  `unknown verb`.

Restarting `mmd-api` drops open terminal websockets. It does not affect running
workspaces.

---

## Checking health

```bash
systemctl is-active mmd-api mmd-worker mmd-provisioner incus postgresql nginx
bash verify/p0-foundation.sh          # 29 host invariants
bash verify/p6-integrations.sh 1      # AI boundary + package config
bash verify/run-all.sh 1              # everything
journalctl -u mmd-api -u mmd-worker -f
```

Quick database view:

```sql
SELECT w.incus_project, u.email, w.state, w.desired_on,
       c.balance_micro/1000000.0 AS toman
  FROM workspaces w
  JOIN users u ON u.id = w.user_id
  LEFT JOIN credit_accounts c ON c.user_id = u.id
 ORDER BY w.idx;
```

---

## Common tasks

### Create and destroy a disposable workspace

Never test against a customer's machine.

```bash
sudo bash workspace/ws-create.sh 9 1 1024 6 4   # idx cores mem_mib root_gib docker_gib
sudo bash workspace/ws-destroy.sh 9 --yes
```

### Repair a workspace's apt configuration

Ubuntu ships `firefox` as a stub that installs a snap, and snaps cannot run in an
unprivileged container. New workspaces get the fix at provision time; older ones
need it applied.

```bash
# as an admin, through the API
POST /api/admin/workspaces/{id}/apt-repair
```

### Talk to the provisioner directly

Root only, and it is the same socket the API uses. Useful for diagnosis.

```bash
python3 - <<'PY'
import json, socket
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); s.settimeout(600)
s.connect("/run/mmd/provisioner.sock")
s.sendall(json.dumps({"verb": "ping", "idx": 1}).encode() + b"\n")
print(s.recv(65536).decode())
PY
```

### Grant credit

Admin panel, or `POST /api/admin/users/{id}/credit`.

### Change the rate card

Admin panel. Values live in the `settings` table and take effect immediately;
`pricing.py` only supplies the defaults for keys that are absent.

---

## Diagnosing

### A workspace is stuck in `error`

The reconciler adopts whatever Incus reports within one tick (runs every 15
minutes, or on worker restart). Force it:

```bash
sudo systemctl restart mmd-worker
journalctl -u mmd-worker -n 20 | grep adopting
```

If the instance no longer exists at all, the customer can factory-reset from
`/console/resources`; `ws-reset.sh` tolerates a half-destroyed workspace.

### A power operation timed out

`/1.0/operations/{id}/wait` is a long poll. The client sets a per-request read
timeout of `timeout + 15`; if a machine legitimately takes longer to stop — an
RDP session plus SSH logins holding processes — Incus still completes it and the
reconciler adopts the result.

### Workspaces have no internet, but DNS works

Something ran `nft flush ruleset` and destroyed Incus's masquerade. Usually
`nftables.service` (Ubuntu's `/etc/nftables.conf` begins with `flush ruleset`) —
it should be **masked**. Also check for leftover Docker tables.

```bash
nft list table inet incus | grep masquerade
systemctl is-enabled nftables          # expect: masked
sudo systemctl restart incus
```

### One workspace can reach another

Tenant-to-tenant isolation lives in a **bridge-family** nftables table, because
two workspaces on the same bridge exchange bridged frames that never traverse
the ip/inet forward hook. If `table bridge mmd_bridge_isolation` is missing, the
`inet` rules will look correct and isolation will still be off.

```bash
nft list tables | grep mmd_bridge_isolation     # expect a match
# reproduce, from one workspace against another's address:
incus exec ws --project ws-1 -- timeout 4 bash -c 'exec 3<>/dev/tcp/10.42.0.20/22'
```

The fix is to re-run the rules generator. Note it also deletes `docker0` when
Docker is installed on the **host**, which is disruptive if anything is running
there — check first, and if so apply only the rules file:

```bash
sudo bash host/30-network-nftables.sh          # full, deletes docker0
nft -c -f /etc/nftables/mmd-isolation.nft      # dry run before applying
```

### A Hermes dashboard is not being served

`mmd-vhosts.timer` reconciles the Hermes dashboards every two minutes. It
writes one file per customer and never reloads a config that does not parse.

```bash
systemctl list-timers mmd-vhosts.timer
journalctl -u mmd-vhosts.service -n 50
ls /etc/nginx/sites-enabled/mmd-vhost-*
```

A dashboard that stays unreachable is almost always the certificate. On the
HTTP-01 fallback, check whether the weekly issuance budget is spent —
`/var/lib/mmd/hermes/issued.json` — and whether a host is in backoff
(`*.fail` in the same directory). Configuring `MMD_ACME_DNS_PLUGIN` and
`MMD_ACME_DNS_CREDENTIALS` switches to one wildcard certificate per customer and
makes the problem go away permanently.

### The second workspace will not start

`Instance DNS name "ws" already used on network`. The bridge needs
`dns.mode=none`; every instance is named `ws` in its own project and Incus
registers DNS per **network**.

```bash
incus network get incusbr0 dns.mode    # expect: none
```

### `apt install <anything>` fails after a snap-backed package

dpkg is wedged mid-transaction. Run the repair verb, which clears it and installs
Mozilla's real `.deb` repository.

### The dashboard shows a raw translation key

A page used `t("some.key")` that the catalogue lacks. `bash tests/run.sh` catches
this; it means an untested deploy.

---

## TLS

The dashboard is served on **https://mmd-ai.ir**, with a Let's Encrypt
certificate for the apex and `www`. Everything else — `www`, the bare IP, plain
HTTP — 301s to that one canonical origin.

```bash
# reissue or reconfigure (idempotent; skips issuance if a cert already exists)
sudo MMD_DOMAIN=mmd-ai.ir MMD_ACME_EMAIL=you@example.com bash host/70-reverse-proxy.sh

# inspect
openssl x509 -in /etc/letsencrypt/live/mmd-ai.ir/fullchain.pem -noout -subject -dates
certbot certificates

# renewal
systemctl list-timers certbot.timer
certbot renew --dry-run
```

Renewal runs from `certbot.timer`, and
`/etc/letsencrypt/renewal-hooks/deploy/10-reload-nginx.sh` reloads nginx
afterwards. Without that hook a renewed certificate sits on disk while nginx
keeps serving the expired one.

**Do not add `includeSubDomains` or `preload` to the HSTS header.** HSTS applies
to a host on *every* port, and this was measured, not assumed: with the policy
stored, Chrome turns `http://mmd-ai.ir:28999` into `https://` and the page
fails, while `http://ports.mmd-ai.ir:28999` loads normally.

That is why everything a customer connects to — published ports, SSH and RDP —
is advertised on **`MMD_ENDPOINT_HOST`** (`/etc/mmd/api.env`, currently
`ports.mmd-ai.ir`) rather than the dashboard's own hostname. It falls back to
the machine's public IP when unset. Adding `includeSubDomains` would extend the
dashboard's policy over that subdomain and break every plain-HTTP app behind it.

```bash
grep MMD_ENDPOINT_HOST /etc/mmd/api.env
sudo systemctl restart mmd-api        # required after changing it
```

## Backups

### PostgreSQL — automated, to Telegram

**Admin → پشتیبان‌گیری** (`/console/admin/backup`) runs a full `pg_dump` on a
schedule and sends the file to an administrator's Telegram chat. Off-host is the
point: the platform runs on one host, and a dump on that host's disk survives a
dropped table and nothing else.

Configuration is a bot token, a chat id and an interval in minutes (5 minutes to
7 days). Use a **dedicated** bot and your own private chat — this file contains
password hashes, provider keys and every customer's ledger. Do not reuse the bot
customers wire to Hermes or OpenClaw; those are attached to agents with a shell
in someone's workspace.

Operational notes:

- The interval is measured from the last **success**, so a failing backup is
  retried on the normal cadence rather than skipped.
- The page leads with **last successful delivery** and **last error**, because
  "enabled" does not mean "arriving". Check those two, not the toggle.
- **Send one now** tests the token and chat id immediately — do this after any
  change rather than finding out days later.
- Telegram caps bot uploads at **50 MB**. The size is checked before the upload
  and reported as a size error. The dump was 1.8 MB as of 1.6.0; if it ever
  approaches the cap, move to an off-host object store.
- The token is stored in `settings` and is never sent to the browser — the page
  shows only its last four characters.
- Format is `pg_dump --format=custom`, already compressed. Restore with:
  ```bash
  pg_restore -d mmd --clean --if-exists mmd-YYYYmmdd-HHMMSS.dump
  ```

Manual equivalent, if the worker is down:

```bash
sudo -u postgres pg_dump mmd | gzip > mmd-$(date +%F).sql.gz
```

### Still manual

1. **`/etc/mmd/api.env`** — losing the session key logs everyone out; losing the
   database password locks the app out of its own data.
2. **`/var/lib/mmd/certs/`** — regenerable, but regenerating means re-trusting
   with Incus.
3. **Workspace filesystems** — ZFS snapshots exist as a mechanism; there is no
   off-host copy. This is the largest known gap.

```bash
zfs snapshot -r mmdpool@$(date +%F)          # local only
zfs list -t snapshot
```

---

## Recovery

### After a host reboot

Everything should return on its own: services are enabled, the isolation table
reloads via `mmd-isolation.service`, and the worker reconciles desired state with
a fresh credit and capacity check. Workspaces do **not** autostart —
`boot.autostart=false` is deliberate, because a machine powered off to save
credit must not come back on and resume charging.

```bash
bash verify/p5-reboot-readiness.sh
```

### If the operator loses SSH access to the host

This has happened once, caused by a hardening script disabling password
authentication when the only key present was the provider's. `host/50-harden.sh`
now refuses to remove a login method unless `MMD_DISABLE_SSH_PASSWORDS=yes` is
set explicitly, and `verify/p0-foundation.sh` asserts that a working login method
**exists** rather than that password auth is off.

Recovery is the provider's console or rescue mode.

### If the control plane loses access to all workspaces

The restricted certificate's project scope was emptied — this happened once when
a project was deleted. The provisioner maintains the scope; restarting it
re-grants:

```bash
sudo systemctl restart mmd-provisioner
bash verify/p2-cert-scope.sh
```

---

## Capacity

```
4 cores / 7.75 GiB   less host reserve (1 core / 2 GiB)
                     times overcommit (cpu ×2, memory ×1)
  ⇒ 6.0 cores / 5.75 GiB schedulable
  ⇒ 5 concurrent workspaces at the default tier
  ⇒ ~6 total accounts, capped by the 68 GiB pool
```

Disk caps total accounts, because the reservation is held even when a workspace
is off. Growing means attaching a second block device — the pool is loop-backed
today only because `/dev/vda1` fills the disk.

Watch:

```bash
zpool list mmdpool
zfs list -o name,used,avail,refreservation -r mmdpool
free -h
```

---

## Support

Tickets live in the database and are answered from `/console/admin/tickets`. A
customer reply reopens a ticket; a staff reply marks it answered; answering a
closed ticket leaves it closed.

Reading another customer's ticket returns **404, not 403** — a 403 would confirm
the ticket exists and allow enumeration.
