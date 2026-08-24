# Changelog

Notable changes. Dates are the day the work landed on the production host.

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

[1.0.0]: https://github.com/MMDBadCoder/MMD-DEV/releases/tag/v1.0.0
