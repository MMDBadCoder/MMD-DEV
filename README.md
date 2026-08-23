# MMD-DEV

Multi-tenant developer workspaces on a single Ubuntu host, with credit billing
and a web dashboard. Each user gets what feels like their own isolated Ubuntu
machine — free to `apt install`, run Docker, and modify anything — bounded in
CPU, memory and disk, able to power fully off (zero CPU, zero RAM) without
losing a single byte, and reachable only through the dashboard.

## Why this shape

This host has **no hardware virtualization** (`systemd-detect-virt` = kvm, no
`/dev/kvm`, no `vmx`/`svm`, CPU reports as "QEMU Virtual CPU"). That rules out
every VM-based design on performance grounds — QEMU falls back to software
emulation, and Firecracker, Kata, Cloud Hypervisor and `incus launch --vm` all
require `/dev/kvm`. Unprivileged **Incus system containers on ZFS** is the only
approach that delivers near-bare-metal speed *and* apt, Docker, and power-off-to-zero.

The trade-off is stated plainly in `docs/` and in the plan: containers share the
host kernel, so a kernel privilege-escalation bug crosses the boundary in a way
a hypervisor would resist. Admin approval of signups is the compensating control.

## Layout

| Path | What |
|---|---|
| `host/` | One-time host setup, numbered in run order |
| `image/` | Golden workspace image build |
| `workspace/` | `ws-create` / `ws-destroy` — the definition of a workspace |
| `control/mmd/` | FastAPI control plane (Incus client, billing, admission) |
| `control/provisioner/` | The only root component; unix socket, 4 allowlisted verbs |
| `control/worker/` | Metering, hourly settlement, lifecycle |
| `web/` | Dashboard SPA: real URLs, light/dark, xterm.js terminal |
| `deploy/` | Hardened systemd units |
| `verify/` | Automated verification suites |

## Bring-up

```bash
sudo bash host/00-preflight.sh              # read-only; refuses to proceed if the host can't support this
sudo bash host/10-remove-lxd-install-incus.sh
sudo bash host/20-storage.sh                # ZFS pool on a preallocated file vdev
sudo bash host/30-network-nftables.sh       # workspace isolation
sudo bash host/40-swap-zram.sh
sudo bash host/50-harden.sh
sudo bash image/build-golden-image.sh       # ~10 min
sudo bash workspace/ws-create.sh 1          # first workspace
```

## The dashboard

Real URLs via the History API, so refreshing or sharing a link works:

| Path | What |
|---|---|
| `/signin`, `/signup` | Separate pages; signup confirms the password |
| `/` | The machine: power, status, terminal |
| `/resources` | Size: 0.5/1/2/3 vCPU, 0.5-6 GB, validated server-side |
| `/ports` | Publish a port to a permanently reserved public address |
| `/billing` | Balance, itemised cost per hour, spend chart, full ledger |
| `/activity` | Every action recorded on the account |
| `/security` | Account details and password change |
| `/admin` | People, capacity, pricing |

Light and dark themes, chosen by the viewer and remembered. The terminal has a
full-screen toggle, adjustable font size, and a scrollbar styled to match.

## Verification

```bash
bash verify/run-all.sh 1          # every suite, 90 checks
bash verify/bench.sh 1            # overhead vs the host
```

| Suite | Checks | What it proves |
|---|---|---|
| `p0-foundation.sh` | 23 | The host is built correctly |
| `p1-workspace.sh` | 18 | PRD behaviour, and the host is unreachable |
| `p1-limits.sh` | 7 | CPU, memory and disk bounds are really enforced |
| `p1-persistence.sh` | 14 | Power off to zero, lose nothing |
| `p2-escalation.sh` | 10 | A compromised control plane cannot escape |
| `p5-reboot-readiness.sh` | 18 | Everything returns after a restart |

**Current status: all suites passing.** Measured on this host:

### Overhead vs the host (`verify/bench.sh`)

| | host | workspace | ratio |
|---|---|---|---|
| CPU (sysbench, 1 thread) | 4791 ev/s | 5154 ev/s | **107.6%** |
| Memory (sysbench) | 8211 MiB/s | 8264 MiB/s | **100.6%** |
| Disk (fio 4k randread) | 8272 IOPS | 22634 IOPS | 273.6% * |

CPU and memory are the honest isolation-overhead numbers: within noise of the
host, i.e. the container layer costs nothing measurable. **Do not quote the
disk figure as a 2.7x speed-up** — ZFS does not honour `O_DIRECT` the way ext4
does, so those reads are served from the ARC while the host figure goes to the
device. It shows ZFS caching, not faster hardware.

### Behaviour

- 4 busy threads on a 1-core tier deliver exactly **1.00 cores**
- A 2 GiB allocation against a 1 GiB tier stays **resident-capped at 809 MiB**
- Over-quota writes hit **ENOSPC**; `refquota` *and* `refreservation` both set
- Powered off: cgroup removed, no processes, **API reports 0 bytes memory**
- Across a power cycle: apt packages, home files, `/etc` edits, Docker images,
  volumes and containers **all survive**, overlay2 intact
- Inside the workspace `nproc`=1 and `free`=1024 MiB while the host is 4 cores
  / 7936 MiB — the technology is invisible
- Host, Incus API, cloud metadata (169.254.169.254), the provider LAN and
  RFC1918 are all **unreachable**; internet and DNS work

## Capacity on this host

```
4 cores / 7.75 GiB   less host reserve (1 core / 2 GiB)
                     times overcommit (cpu x2, mem x1)
  => 6.0 cores / 5.75 GiB schedulable
  => 5 concurrent workspaces at the 1 core / 1 GiB default tier
  => ~6 total accounts, capped by the 68 GiB pool at 10 GiB each
```

Disk is the cap on total accounts because a reservation is held even when a
workspace is off. Attaching a second block device is the scale path.

## Billing

Disk bills every hour regardless of power state. CPU and memory bill only while
on, as a reservation component plus a measured usage component. Settlement is
in arrears; before each hour the balance must cover that hour **at full
capacity** or the workspace is not allowed to start.

At the default tier: **23.00 credits/hr** power-on gate, 16.00 for a fully idle
on-hour, 1.00 off, 0.50 archived.
