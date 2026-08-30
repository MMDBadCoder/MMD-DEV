# Architecture

How the pieces fit, and why each boundary sits where it does. Decisions that
have a story behind them are recorded in [DECISIONS.md](DECISIONS.md); this
document describes the result.

---

## The constraint that shaped everything

The host reports `systemd-detect-virt = kvm`, has no `/dev/kvm`, no `vmx` or
`svm` CPU flags, and a CPU that identifies as "QEMU Virtual CPU 2.5+". It is
itself a VM with nesting disabled.

That eliminates every hypervisor-based design:

| Approach | Why not |
|---|---|
| libvirt / QEMU | Falls back to TCG software emulation, 10–50× slower |
| Firecracker, Cloud Hypervisor, Kata | All require `/dev/kvm` |
| `incus launch --vm` | Requires `/dev/kvm` |
| gVisor | Runs without KVM but imposes heavy syscall overhead and does not cleanly support `apt` plus Docker-in-workspace |
| Docker containers | No `apt`-managed init, no clean power-off-with-state |

**Unprivileged Incus system containers on ZFS** is what remains, and it happens
to satisfy the requirements well: a full Ubuntu userland, near-native speed,
stop/start with complete state retention, and per-tenant kernel-enforced limits.

The residual risk is real and is not papered over: containers share the host
kernel, so a kernel privilege-escalation bug crosses a boundary that a
hypervisor would resist. Mitigations are unprivileged containers, isolated
idmaps, AppArmor, seccomp and unattended upgrades. The compensating control is
**admin approval of every signup**, which is load-bearing rather than
decorative.

---

## Components

```
                     Internet
                        │  443
                        ▼
                     ┌──────┐
                     │nginx │  TLS, static assets at /static/…
                     └──┬───┘
                        │ 127.0.0.1:8000
        ┌───────────────▼──────────────────┐
        │ mmd-api   (FastAPI, user `mmd`)  │
        │  · sessions, authorisation       │
        │  · billing gate, admission       │
        │  · terminal websocket bridge     │
        └───┬──────────────┬───────────────┘
            │              │
   restricted client   unix socket (0770 root:mmd)
   certificate        + SO_PEERCRED
            │              │
            ▼              ▼
     ┌────────────┐   ┌───────────────────────────┐
     │   Incus    │   │ mmd-provisioner (root)    │
     │ 127.0.0.1  │◄──┤  no network listener      │
     │   :8443    │   │  fixed verb allowlist     │
     └─────┬──────┘   └───────────────────────────┘
           │
   ┌───────┴────────┬────────────────┐
   ▼                ▼                ▼
 project ws-1    project ws-2     project ws-N
 instance "ws"   instance "ws"    instance "ws"

 mmd-worker (user `mmd`) ── metrics scrape · hourly settlement
                            lifecycle · reconciliation
 PostgreSQL ── the authority on desired state and on money
```

### Three credentials, three blast radii

| Service | Runs as | Incus credential | Can do |
|---|---|---|---|
| `mmd-api` | `mmd` | **restricted** client cert, scoped to `ws-*` | start, stop, exec, resize, read metrics |
| `mmd-worker` | `mmd` | metrics cert + the restricted client cert | scrape usage, stop on exhaustion |
| `mmd-provisioner` | `root` | unix socket (full admin) | create, reset, archive, restore, destroy |
| `mmd-vhosts` | `root` | none — reads the database only | write `/etc/nginx/sites-enabled`, run certbot |

`mmd-vhosts` is a fourth component for one reason: it needs root *and* the
internet, and neither of the others can give it both. `mmd-provisioner` is root
but runs with `IPAddressDeny=any`, which is the single control that makes a root
daemon acceptable — ACME issuance there would mean deleting it. `mmd-worker` has
the internet but runs as `mmd` and cannot write `/etc/nginx` or run certbot. So
it is a small reconciler on a two-minute timer with no listener, whose only
input is a username already validated as a DNS label.

`mmd-api` is the component exposed to the internet, so its authority is capped
by construction rather than by trust. It **cannot** create a project, set
`security.privileged=true`, attach a host-path disk or define a custom idmap —
Incus refuses those regardless of what the process asks for. An RCE in FastAPI
therefore does not become host compromise.

Incus listens on loopback only (`core.https_address=127.0.0.1:8443`,
`core.metrics_address=127.0.0.1:9101`). Nothing but the dashboard and published
customer ports is reachable from outside.

### The provisioner

The one root component. It has no network listener at all — only a unix socket —
verifies the caller's uid via `SO_PEERCRED`, accepts a fixed allowlist of verbs,
and re-validates every argument rather than trusting the caller. It shells out to
`workspace/ws-*.sh` so there is exactly one definition of what a workspace is.

Current verbs:

```
provision  reset  archive  restore  destroy
expose_port  unexpose_port  apt_repair
service_ssh  service_rdp  probe_sessions  ai_claude
fs_list  fs_pull  fs_push  fs_mkdir  fs_delete  fs_archive
ping
```

The `fs_*` verbs exist because a **restricted certificate is denied the Incus
file API** (403). File-manager operations therefore route through root rather
than talking to Incus directly.

The systemd unit keeps `ProtectHome=yes`, so `/root` is empty to the
provisioner. Exactly one directory is bound back in **read-only**
(`/root/.claude` → `/var/lib/mmd/host-claude/.claude`) for the Claude Code
sign-in feature. It can never write to the operator's home, and can see nothing
else under it.

---

## The workspace

### Per-tenant Incus project

The security boundary. These are project restrictions, enforced by Incus:

```
restricted = true
restricted.containers.nesting     = allow    # Docker inside
restricted.containers.privilege   = unprivileged
restricted.containers.lowlevel    = block
restricted.containers.interception= allow    # mknod, setxattr, sysinfo only
restricted.devices.disk           = managed  # pool volumes only, never host paths
restricted.devices.nic            = managed
restricted.devices.proxy/gpu/pci/usb/unix-* = block
restricted.networks.access        = incusbr0
restricted.idmap.uid = / restricted.idmap.gid =   (empty ⇒ custom idmaps denied)
limits.containers        = 1
limits.virtual-machines  = 0
limits.cpu / limits.memory / limits.disk         # ceilings the app cannot exceed
```

Project ceilings are **the top of the catalogue, not the customer's current
size**. Pinning them to the size at creation made resizing a one-way door.

### Per-instance configuration

```
security.nesting                    = true    # Docker inside
security.privileged                 = false
security.guestapi                   = false   # cannot read or alter its own config
security.idmap.isolated             = true    # unique uid range per tenant
security.syscalls.intercept.mknod   = true
security.syscalls.intercept.setxattr= true
security.syscalls.intercept.sysinfo = true    # free/nproc report the tier
limits.cpu            = <cores>
limits.cpu.allowance  = <cores×100>ms/100ms   # hard cap, live-updatable
limits.memory         = <mib>MiB
limits.memory.enforce = hard
limits.processes      = 4096
boot.autostart        = false                 # the DB is the authority
```

`boot.autostart=false` matters under billing: a workspace powered off to stop
burning credit must not return **on** after a host reboot and resume charging.
The worker reconciles desired state after boot with a fresh credit and capacity
check.

Combined with lxcfs, `sysinfo` interception makes `/proc/meminfo`,
`/proc/cpuinfo` and `free` report the tier rather than the host — which is what
sells "your own machine" and keeps host specs invisible.

### Storage

A ZFS pool on a **preallocated, non-sparse** file vdev, so ZFS can never believe
it has space the host lacks. Per-volume defaults are `volume.zfs.use_refquota`
and `volume.zfs.reserve_space` — a quota alone caps the owner but does not stop
other tenants consuming free space; a real reservation needs `refreservation`.

Docker gets a **separate ext4-on-zvol volume** mounted at `/var/lib/docker`. On a
ZFS-backed rootfs Docker selects the `zfs` graph driver and fails outright, since
the container has no `zfs` binary and no delegated dataset. Block mode gives it a
real block device, so `overlay2` works natively and the volume survives
stop/start exactly like the rootfs.

### Network

`incusbr0` NAT bridge at `10.42.0.1/24`, one static IP per workspace
(`index + 10`), with `security.ipv4_filtering` and `security.mac_filtering` on
each NIC to stop ARP and IP spoofing between tenants.

An `inet mmd_isolation` nftables table (priority −10, drop-only) then blocks
traffic from `incusbr0` to:

- host addresses, except DNS/DHCP on `10.42.0.1`
- `169.254.169.254` — cloud metadata, a direct path to provider credentials
- RFC1918 and the provider's own LAN
- other workspaces

Outbound internet is allowed and masqueraded, because developers need `apt` and
Docker Hub. Published customer ports are plain nftables **DNAT** in a separate
`ip mmd_ports` table: Incus proxy devices are blocked at project level, and DNAT
is faster than a userspace relay and needs no Incus privilege at all. The API
sends the complete desired mapping set on every change, so it is idempotent and
self-healing rather than a sequence of deltas that can drift.

---

## Control plane

**FastAPI + PostgreSQL + SQLAlchemy 2.0.** The Incus REST client and its
exec-websocket are hand-written — there is no official Python client, and the
terminal protocol is the one piece where the Python choice costs real
implementation effort.

### Data model

`users` · `workspaces` · `credit_accounts` · `credit_transactions` ·
`usage_samples` · `exposed_ports` · `ssh_keys` · `settings` · `audit_log` ·
`tickets` · `ticket_messages`

Money is **integer micro-Toman** (`MICRO = 1_000_000`). Billing accrues per
minute and settles hourly, so floats would drift and eventually disagree with
the ledger.

`credit_transactions` is unique on `(workspace_id, period_start, kind)`. That
idempotency key is what makes a worker restart mid-hour safe rather than a
double charge.

### State machine

```
provisioning ──► off ⇄ on
                  │      │
                  │      └──► error ──┐   (reconciler adopts reality)
                  ├──► resetting ─────┤
                  └──► archiving ──► archived ──► deleting ──► deleted
```

`state` is what Incus actually reports; `desired_on` is what the customer asked
for. The worker reconciles one toward the other, which is also how a host reboot
restores the right set of machines — with a fresh credit and capacity check
rather than blind autostart.

The reconciler examines `on`, `off`, `error`, `starting` and `stopping`. Earlier
it looked only at `on` and `off`, so a workspace parked in `error` by a transient
failure stayed broken forever with no control that could fix it.

### The terminal

```
POST /1.0/instances/ws/exec?project=ws-N
  {"command":["login","-f","dev"], "interactive":true,
   "wait-for-websocket":true, "environment":{"TERM":"xterm-256color"}}
→ 202 + operation id, metadata.fds = {"0": secret, "control": secret}
→ dial wss://…/1.0/operations/{uuid}/websocket?secret=…   (twice: tty + control)
   control frame: {"command":"window-resize","args":{"width":"80","height":"24"}}
```

FastAPI validates the session cookie, workspace ownership and `state == on`
before dialing, then bridges browser websocket ↔ Incus websocket.

---

## Interface

A single-page app with **no build step and no framework**: ES modules served
directly, a History-API router, CodeMirror 5 for editing, xterm.js for the
terminal, and Vazirmatn as a variable font.

Persian and right-to-left throughout. Layout uses logical properties
(`inline-start`/`inline-end`) so nothing needs mirroring by hand, and the few
deliberately left-to-right islands — terminal output, addresses, package names,
file paths — are marked `dir="ltr"` so the bidi algorithm does not reverse them.

**The API returns codes; the interface builds sentences.** `web/js/i18n.js` is
the only place customer-visible text exists. This is enforced:
`tests/test_no_english_prose.py` walks the AST of every request handler and
fails on English prose in a response, and `tests/web/i18n.test.mjs` fails on any
key a page uses that the catalogue lacks, or any catalogue value that is not
Persian.

---

## Verification

`tests/` are unit tests: fast, no infrastructure, safe anywhere.

`verify/` inspects the **running host** — the pool, the firewall, the systemd
sandbox, what is actually inside a workspace. It is how a change is checked
against the world rather than against the database.

| Suite | Proves |
|---|---|
| `p0-foundation.sh` | The host is built correctly |
| `p1-workspace.sh` | PRD behaviour, and that the host is unreachable from inside |
| `p1-limits.sh` | CPU, memory and disk bounds are really enforced |
| `p1-persistence.sh` | Power off to zero, lose nothing |
| `p2-escalation.sh` | A compromised control plane cannot escape |
| `p2-cert-scope.sh` | The restricted certificate reaches only what it should |
| `p5-reboot-readiness.sh` | Everything returns after a restart |
| `p6-integrations.sh` | AI sign-in boundary and package configuration |
