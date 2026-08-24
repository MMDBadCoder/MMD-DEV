# MMD-DEV

**Cloud development machines, billed by the hour in Toman.**

Every customer gets what feels like their own Ubuntu server — root access,
`apt`, Docker, AI coding tools preinstalled — reachable from a Persian web
dashboard, and able to power fully off to near-zero cost without losing a byte.
The whole product runs on **one** host.

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="docs/ARCHITECTURE.md">Architecture</a> ·
  <a href="docs/BILLING.md">Billing</a> ·
  <a href="docs/API.md">API</a> ·
  <a href="docs/OPERATIONS.md">Operations</a> ·
  <a href="docs/DECISIONS.md">Decisions</a> ·
  <a href="AGENTS.md">For AI agents</a>
</p>

---

## What a customer gets

- **A real machine.** Root, `apt install` anything, `docker run` anything. It is
  a full Ubuntu 24.04 system, not a sandbox with holes cut in it.
- **Power off to zero.** No CPU, no RAM, no charge for either — while every
  file, package, config edit and Docker volume survives untouched.
- **Ways in:** a browser terminal, SSH with managed public keys, a full XFCE
  desktop over RDP, and a rich file manager with editing, upload, download and
  recursive zip.
- **Two permanent addresses.** SSH and RDP ports are reserved for the life of
  the account and never change, so a saved config keeps working.
- **AI tools, already signed in.** One click installs Claude Code and carries
  the platform's sign-in across, so the developer never logs in.
- **Publish a port** so what they build stays reachable.
- **Honest billing.** Itemised Toman cost per hour, a spend chart, a full
  ledger, and a hard gate that refuses to start an hour the balance cannot cover.
- **Support built in.** Threaded tickets with a staff queue.

Everything is Persian and right-to-left. Established technical terms stay Latin,
because translating "Docker" helps nobody.

## What an operator gets

Signup → admin approval → automatic provisioning. A capacity view, an editable
rate card, per-account credit grants, a full audit trail of every action, and a
verification suite that checks the running host rather than the source.

---

## Why it is built this way

The host has **no hardware virtualization** — no `/dev/kvm`, no `vmx`/`svm`,
and the CPU reports itself as "QEMU Virtual CPU". That rules out every VM-based
design on performance grounds: QEMU falls back to software emulation, and
Firecracker, Kata, Cloud Hypervisor and `incus launch --vm` all require KVM.

**Unprivileged Incus system containers on ZFS** is the only approach that
delivers near-bare-metal speed *and* `apt`, Docker-in-workspace, and
power-off-to-zero. Measured on this host, CPU and memory throughput inside a
workspace are within noise of the host itself.

The trade-off is stated rather than hidden: containers share the host kernel, so
a kernel privilege-escalation bug crosses a boundary a hypervisor would resist.
Admin approval of signups is the compensating control, and it is load-bearing.

---

## Architecture at a glance

```
Browser ── nginx ──► mmd-api ──► restricted Incus cert ──► Incus ──► ws-1 ws-2 …
                        │
                        └─────► unix socket ──► mmd-provisioner (root, no network)
                        │
                     PostgreSQL
   mmd-worker ── metering · hourly settlement · reconciliation
```

The internet-facing service holds a **restricted** Incus certificate. Incus
itself — not application logic — refuses it privileged containers, host-path
disks and custom idmaps, so a compromise of the web app cannot reach the host.
Genuinely privileged work goes through a root daemon with **no network
listener**, a `SO_PEERCRED` check and a fixed verb allowlist.

Full detail in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Layout

| Path | What |
|---|---|
| `host/` | One-time host setup, numbered in run order |
| `image/` | Golden workspace image, and the apt fixups every workspace needs |
| `workspace/` | `ws-create` / `ws-reset` / `ws-destroy` — the definition of a workspace |
| `control/mmd/` | FastAPI control plane: Incus client, billing, admission, API |
| `control/provisioner/` | The only root component; unix socket, allowlisted verbs |
| `web/` | Persian RTL single-page app — no build step, no framework |
| `tests/` | Unit tests: pytest for the backend, `node:test` for the interface |
| `verify/` | Suites that check the **running host**, not the source |
| `deploy/` | Hardened systemd units |
| `docs/` | Architecture, billing, API, operations, and every decision made |

Roughly 13,000 lines, excluding vendored assets: ~7,400 Python, ~3,800
JavaScript, ~2,100 shell.

---

## Quick start

Requires a fresh Ubuntu host with root. Every script is idempotent and refuses
to proceed if the host cannot support what it is about to do.

```bash
sudo bash host/00-preflight.sh                # read-only; reports what it finds
sudo bash host/10-remove-lxd-install-incus.sh
sudo bash host/20-storage.sh                  # ZFS pool on a preallocated file vdev
sudo bash host/30-network-nftables.sh         # workspace isolation
sudo bash host/40-swap-zram.sh
sudo bash host/50-harden.sh
sudo bash host/60-control-plane.sh            # Postgres, services, systemd units
sudo bash host/70-reverse-proxy.sh            # nginx + TLS

sudo bash image/build-golden-image.sh         # ~10 min
```

The first account to sign up becomes the administrator. After that, signups wait
for approval, and approving one provisions a machine automatically.

```bash
bash tests/run.sh              # every unit test, ~5 seconds, no infrastructure
bash verify/run-all.sh 1       # every host check, against workspace 1
```

See [docs/OPERATIONS.md](docs/OPERATIONS.md) for deploying changes, backups and
recovery.

---

## The interface

Real URLs via the History API, so refreshing or sharing a link works.

| Path | What |
|---|---|
| `/` | Public page — what the product is, live prices |
| `/console` | Overview: power, status, usage charts |
| `/console/connections` | Browser terminal · SSH · RDP desktop |
| `/console/files` | File manager: edit, upload, download, zip, preview |
| `/console/resources` | Size, and the factory reset |
| `/console/tools` | Install toolsets |
| `/console/ai` | Claude Code, installed and signed in |
| `/console/ports` | Publish a port to a reserved public address |
| `/console/billing` | Balance, itemised Toman rates, spend chart, ledger |
| `/console/activity` | Every action recorded on the account |
| `/console/security` | Account details and password |
| `/console/support` | Tickets |
| `/console/admin` | People, capacity, pricing, toolsets, support queue |

No build step and no framework: ES modules, a History-API router, CodeMirror for
editing and xterm.js for the terminal. Light and dark themes, remembered per
browser.

---

## Billing in one paragraph

Disk bills **every** hour regardless of power state, because a stopped workspace
still holds its reservation. CPU and memory bill **only while on**, as a
reservation component plus a measured usage component. Settlement is in arrears;
before each hour the balance must cover that hour **at full capacity** or the
machine is not allowed to start. At zero credit the machine is archived,
restorable for 30 days, then deleted.

The precise rules — and why each one is the way it is — are in
[docs/BILLING.md](docs/BILLING.md).

---

## Measured, not claimed

| | host | workspace | |
|---|---|---|---|
| CPU (sysbench, 1 thread) | 4791 ev/s | 5154 ev/s | **107.6%** |
| Memory (sysbench) | 8211 MiB/s | 8264 MiB/s | **100.6%** |

CPU and memory are within noise of the host — the container layer costs nothing
measurable. Behavioural results from `verify/`:

- 4 busy threads on a 1-core tier deliver exactly **1.00 cores**
- A 2 GiB allocation against a 1 GiB tier stays **capped at 809 MiB resident**
- Powered off: cgroup gone, no processes, **Incus reports 0 bytes memory**
- Across a power cycle: packages, files, `/etc` edits, Docker images, volumes
  and containers **all survive**
- Inside, `nproc` = 1 and `free` = 1024 MiB while the host is 4 cores / 7936 MiB
- Host, Incus API, cloud metadata, provider LAN and RFC1918 **all unreachable**;
  internet and DNS work

---

## Capacity on this host

```
4 cores / 7.75 GiB   less host reserve (1 core / 2 GiB)
                     times overcommit (cpu ×2, memory ×1 — never oversubscribed)
  ⇒ 6.0 cores / 5.75 GiB schedulable
  ⇒ 5 concurrent workspaces at the 1 core / 1 GiB default
  ⇒ ~6 total accounts, capped by the 68 GiB pool at 10 GiB each
```

Disk caps total accounts because the reservation is held even when off. A second
block device is the scale path.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). If you are an AI agent, start with
[AGENTS.md](AGENTS.md) — it front-loads the traps that are not visible in the
code.

## Security

Please report vulnerabilities privately. See [SECURITY.md](SECURITY.md).

## Licence

[MIT](LICENSE).
