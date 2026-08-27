# Changelog

Notable changes. Dates are the day the work landed on the production host.

## [1.3.0] — 2026-08-26

Live on the production host, except where noted. Almost entirely bug fixes,
most of them reported by customers — `ping`, the Claude Code sign-in, the AI
model allowlist, the Hermes toggle. No feature changes: the ports page, and the
SSH and RDP addresses, are exactly as they were.

Two of these were invisible for a reason worth naming. A deploy never restarted
anything, and a failed schema patch logged below the level the app prints — so
both a fix and a migration could be "shipped" and have no effect, with nothing
saying so.

### Fixed

- **Enabling Hermes was impossible.** The endpoint validated its body with
  `AiAction`, whose vocabulary belongs to Claude Code, so every `enable` came
  back as `action String should match pattern '^(install|unlink)$'` — a raw
  Pydantic error, in English, in front of a Persian interface. Separate models
  now.
- **Enabling Hermes then toasted "Claude Code آمادهٔ استفاده است"** — the wrong
  product. Same root assumption as the above: that two AI features are one
  feature. Hermes has its own sentences, pinned by a test.
- **The deploy script never restarted anything.** It ended with
  `systemctl start`, a no-op on an already-running unit, so a deploy copied new
  code into `/opt/mmd` and left every service executing the old code from
  memory. This is why the Hermes fix appeared not to work: the API had been up
  for eleven hours and had never reloaded. Now `systemctl restart`.
- **Schema patches shared one transaction.** Postgres aborts a whole
  transaction after any failed command, so one unsupported patch silently
  skipped every patch after it — and logged at `debug` while the app logs at
  `info`, so nothing said so. One transaction per statement now, and skips log
  at `warning` with the reason.
- **`30-network-nftables.sh` deleted `docker0` on hosts that legitimately run
  Docker.** The block exists for the bridge a *purged* Docker leaves behind but
  fired on the mere existence of the interface. This host runs a StarRocks
  cluster, Grafana and Prometheus on Docker networks. Now guarded on `dockerd`
  being absent.
- **The vhost reconciler was in no provisioning script**, so a rebuilt host
  would have come up with every Hermes dashboard unpublished. It is installed
  and enabled by `60-control-plane.sh` now.
- **A config that failed `nginx -t` stayed on disk**, breaking the next
  unrelated reload. The reconciler snapshots and rolls back.

- **`ping` did not work inside a workspace.** Both routes to an ICMP socket
  were closed: a new netns does not inherit the host's
  `net.ipv4.ping_group_range` (and it cannot be set from inside an isolated
  idmap), and `iputils-ping`'s `setcap` call fails silently under unprivileged
  dpkg, so the binary arrived with no `cap_net_raw`. `image/net-fixups.sh` now
  grants it per workspace — it cannot be baked into the golden image, because a
  capability xattr embeds the rootid of the namespace it was set in and every
  workspace has its own uid range.
- **Every free model was blocked.** `build_allowlist` could not tell a
  published price of zero from no published price, so all 17 of OpenRouter's
  `:free` models were excluded from the guardrail and an agent using one got
  `HTTP 404: No endpoints available matching your guardrail restrictions`.
  Fixing that admitted the `openrouter/*` routers, which the guardrail rejects
  by name — failing the whole PATCH and leaving a stale allowlist in force — so
  they are denied explicitly and the sync now retries once without whatever ids
  OpenRouter rejects.
- **"Resync sign-in" reported success and changed nothing.** The config was
  written only when `~/.claude.json` was absent, and Claude Code creates that
  file on its first run — so once a customer had typed `claude` once, resync
  could never set `hasCompletedOnboarding` again and every run opened the
  first-run wizard, which reads as being asked to log in. It merges now,
  preserving the customer's own settings. Success was also measured as "the
  credentials file is non-empty", so the button reported success on exactly the
  machines that were still broken; status now reports `onboarded` separately and
  the page has a distinct state for it.
- **The sign-in page asked for a username.** Copy-pasted from sign-up, never
  sent to `/api/auth/login`, and it ran the availability check — so a returning
  customer typing their own username was told it was taken.

### Changed

- Hermes connection details are laid out one value per row at full width. The
  auto-fit grid packed an API key, a URL and a password into ~260px boxes that
  each scrolled sideways.
- `mmd-hermes-vhosts` superseded by **`mmd-vhosts`**: one nginx file per
  customer instead of one per host, and an optional DNS-01 wildcard per customer
  (`MMD_ACME_DNS_PLUGIN`) in place of per-host HTTP-01.
- `server_names_hash_bucket_size` raised to 128.

### Still outstanding

- **Tenant-to-tenant isolation is not deployed.** The bridge-family nftables
  table that blocks workspace↔workspace traffic is in the repo, but the rules
  file on the host predates it, so one customer's machine can reach another's —
  verified by reading a neighbour's SSH banner. Fixed by re-running
  `30-network-nftables.sh`, which is now safe on this host. See OPERATIONS.

## [1.1.0] — 2026-08-24

Live on **https://mmd-ai.ir** with a real certificate. Everything in this
release either came from a customer ticket or was found while fixing one.

### Security and delivery

- **Real TLS.** Let's Encrypt for the apex and `www`, HTTP→HTTPS on every path,
  HSTS, and renewal on a timer with a deploy hook that reloads nginx — without
  the hook a renewed certificate sits on disk while the expired one keeps being
  served. `MMD_DOMAIN` / `MMD_ACME_EMAIL` make it reproducible; no domain still
  falls back to self-signed so a fresh host comes up.
- One canonical origin: `www`, the bare IP and plain HTTP all 301 to
  `https://mmd-ai.ir`, path preserved.
- **Removed a CUPS print server** that was serving an unauthenticated web
  interface on `0.0.0.0:631` — on a host with no printers.
- Destroying a workspace no longer leaves its DNAT rules in the kernel.

### Billing

- **Publishing a port is now free.** It hands out a firewall rule and a number
  from a range of ten thousand; charging for it discouraged the thing the
  product exists for.
- «زمان باقی‌مانده» reads in **days** rather than hours — a funded account had
  several hundred hours, which is not a number anyone can act on.

### Usage charts

- **Cores and gigabytes, not percentages.** "90%" reads identically on half a
  core and on three, and hides how much room is left.
- Window picker — 5m / 15m / 1h / 6h / 24h, defaulting to **5 minutes** and
  remembered per browser — replacing a fixed six-hour view.
- Sampling every **20 seconds** instead of 60, with settlement still at 5
  minutes and reconciliation at 15. Charts refresh in step.
- `usage_samples` is finally pruned; it had grown without limit despite the
  docs claiming seven-day retention.
- The admin panel's capacity section now distinguishes what is **reserved** from
  what is **used**, with host-wide charts for the latter.

### Interface

- Connection addresses use the domain rather than a bare IP. Published ports sit
  on a subdomain deliberately: HSTS covers a host on *every* port, so the apex
  would have force-upgraded customers' plain-HTTP apps to HTTPS and broken them.
- The header stays live — the support badge and the credit chip were drawn from
  a session object fetched once at page load.
- The unread badge on the ticket list is visible at last; it had been styled
  only inside the Connections tab bar, so it rendered as invisible plain text.
- The ticket reply box says «پیام شما» when your own message is last and
  «پاسخ شما» when support's is.
- Chart labels carry their colon; the current value is labelled at all.
- The console logo goes to the homepage.
- File-manager paths no longer render as `//home/dev`.
- The browser terminal stopped clipping its own last line.
- No English prose reaches the Persian interface, enforced by a test that walks
  the AST of every request handler.

### Tests

298 backend and 45 interface tests, up from 274 and 24. Two new guards worth
naming: one fails on English prose in an API response, and one catches a module
using a name it never imported — which `node --check` cannot see, and which had
already shipped a blank overview page to every customer.

## [1.0.0] — 2026-08-24

First release. The product is live and serving paying customers.

### The machine

- Unprivileged Incus system containers on ZFS, one per customer, in a
  per-tenant Incus project whose restrictions are the security boundary
- Power off to genuinely zero CPU and memory with complete state retention:
  files, packages, `/etc` edits, Docker images, volumes and containers
- Live resize between 0.5–3 vCPU and 0.5–6 GiB, applied without a restart
- Docker inside the workspace on a dedicated ext4-on-zvol volume, so `overlay2`
  works natively
- The technology is invisible from inside — `nproc` and `free` report the tier,
  not the host
- **Factory reset**: rebuild from the golden image behind a confirmation that
  requires three separate things, including the account password

### Ways in

- Browser terminal (xterm.js), with full-screen, adjustable font and a themed
  scrollbar; it connects only when asked
- SSH with managed public keys — a list with individual add and remove, platform
  help for finding a key, and a listener that refuses to start with no keys
- Full XFCE desktop over RDP, installed on demand
- Two permanently reserved ports per workspace, one for each service, which
  never change
- Rich file manager: browse, edit with syntax highlighting, upload, download,
  recursive zip, image preview, create and delete
- Publish a port to a reserved public address

### AI

- Claude Code installed into a workspace and signed in from the platform's
  account, so the developer never logs in
- The copy is an allowlist: one file opened, one key kept, re-serialised into a
  fresh document, delivered on stdin. Nothing else of the operator's crosses —
  enforced in code, asserted by a test that checks exactly one filename is read,
  and narrowed again by a systemd sandbox

### Billing

- Toman throughout, integer micro-Toman internally
- Disk bills in every state; CPU and memory bill only while on, as reservation
  plus measured usage
- Settlement in arrears, with a pre-hour gate at full capacity
- Idempotent charges on `(workspace_id, period_start, kind)`, so a worker
  restart cannot double-charge
- Archive at zero credit, restorable for 30 days, then deleted
- Itemised rates, spend chart and full ledger in the dashboard; editable rate
  card in the admin panel

### Interface

- Persian, right-to-left, no build step and no framework
- Light and dark themes
- Landing page, console, admin panel, activity log
- Support tickets with a threaded conversation, per-side unread marks and a
  staff queue with status control

### Operations

- Three services with three separate credentials and three blast radii
- The only root component has no network listener and a fixed verb allowlist
- nftables isolation: host, Incus API, cloud metadata, provider LAN, RFC1918 and
  other tenants all unreachable
- Verification suites that check the **running host**, not the source
- 274 backend tests and 34 interface tests, no infrastructure required

### Measured

| | host | workspace | |
|---|---|---|---|
| CPU (sysbench, 1 thread) | 4791 ev/s | 5154 ev/s | 107.6% |
| Memory (sysbench) | 8211 MiB/s | 8264 MiB/s | 100.6% |

Full-desktop install costs 236 MB; Firefox 314 MB; a factory reset takes about
14 seconds.

### Fixed during the run-up to 1.0

Each of these was found in production, several reported by customers:

- An SSH lockout caused by the hardening script disabling password
  authentication when the only key present was the provider's
- Two workspaces could not run at once — Incus registers DNS names per network,
  not per project, so the second instance named `ws` was refused
- Docker's leftover nftables rules silently killed all workspace egress while
  DNS kept working
- `apt install firefox` failed and wedged dpkg, because Ubuntu ships a stub that
  installs a snap and snaps cannot run in an unprivileged container
- The reserved SSH and RDP ports were never actually allocated, so newly
  approved customers saw blank addresses
- A slow stop raised `ReadTimeout` and parked the workspace in `error`, which
  nothing reconciled — the customer had no control that could fix it
- The browser terminal clipped its own last line, because the fit addon measured
  the border-box height and laid out more terminal than fit
- English text reached the Persian dashboard, saying "credits" where the product
  charges Toman
- The file manager displayed `//home/dev`

[1.1.0]: https://github.com/MMDBadCoder/MMD-DEV/releases/tag/v1.1.0
[1.0.0]: https://github.com/MMDBadCoder/MMD-DEV/releases/tag/v1.0.0
