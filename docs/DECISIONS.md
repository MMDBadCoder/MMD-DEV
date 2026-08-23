# Decisions and the reasons behind them

Things that are not obvious from the code, and the failures that shaped them.

## Why containers and not VMs

Not a preference — a constraint. This host reports `systemd-detect-virt = kvm`,
has no `/dev/kvm`, no `vmx`/`svm` CPU flags, and a CPU model of "QEMU Virtual
CPU version 2.5+". It is itself a VM with nesting disabled. Every VM-based
option (libvirt/QEMU, Firecracker, Cloud Hypervisor, Kata, `incus launch --vm`)
either requires `/dev/kvm` or falls back to software emulation at roughly
10–50× slowdown. With "close to bare metal" as a hard requirement, unprivileged
Incus system containers were the only remaining option.

Measured result: **106.7% of host CPU throughput** inside a workspace. The
isolation layer costs nothing measurable on CPU.

## Why `boot.autostart=false` on every workspace

The obvious setting is `true` — and it is wrong here. Under credit billing, a
workspace the developer deliberately switched off to stop spending would come
back **on** after a host reboot and silently resume charging them. Instead the
database owns power state and the worker restores it after re-checking credit
and capacity.

## Why `zfs.reserve_space=true` and not just `size`

`size` sets a ZFS **quota**, which caps its owner but does nothing to stop other
tenants consuming the pool's free space first. The PRD asks for storage that is
*reserved and theirs*. `zfs.reserve_space=true` adds a `refreservation`, which
is the part that actually guarantees it. Verified: `refquota=6GiB
refreservation=6GiB` on every workspace dataset.

## Why Docker gets a separate block-mode volume

On a ZFS-backed rootfs Docker selects the `zfs` graph driver and fails — the
container has no `zfs` binary and no delegated dataset. A `zfs.block_mode=true`
volume formatted ext4 and mounted at `/var/lib/docker` gives Docker a real block
device, so `overlay2` works natively. Verified: Docker 29.7.2, `overlay2` on
`extfs`, surviving power cycles with images, volumes and containers intact.

## Failures worth remembering

**Docker's leftover nftables rules silently killed all workspace networking.**
Purging the Docker packages does not remove rules already loaded in the kernel,
and Docker's `FORWARD` chain in `table ip filter` has `policy drop` while
accepting only `docker0` traffic. It sits on the same hook as Incus's rules, so
every workspace lost outbound connectivity — apt and Docker Hub both dead — with
nothing anywhere pointing at Docker as the cause. `host/30-network-nftables.sh`
now deletes those tables explicitly.

**`pipefail` + `grep -q` produces false test failures.** `grep -q` exits on the
first match, the producer dies of SIGPIPE (141), and `pipefail` reports the
pipeline as failed even though the match succeeded. This bit the preflight and
the P0 suite before being caught.

**`/dev/zero` cannot test a quota on a compressed pool.** lz4 compresses zeros
to nothing, so a fill test writes at 3.4 GB/s and never reaches the limit. The
quota test uses `/dev/urandom`. The flip side is a genuine benefit: because
`refquota` accounts compressed bytes, compressible data stretches a tenant's
slice.

**The Incus CLI blocks on stdin when it is not a TTY**, waiting for a YAML
definition that never arrives. Under the provisioner this looked like a hang
with no output and no error. `ws-create.sh` now detaches stdin explicitly.

**`ProtectHome=yes` breaks the Incus CLI**, which insists on creating
`/root/.config` and fails with a read-only filesystem error that surfaces as a
misleading "golden image not found". Fixed with `INCUS_CONF` pointing at the
runtime directory rather than by dropping the hardening.

**systemd has no `RuntimeDirectoryGroup=`.** Runtime-directory ownership comes
from `User=`/`Group=`, so `/run/mmd` was `root:root 0750` and the API could not
traverse into it to reach the provisioner socket — a bare EACCES pointing
nowhere.

**cloud-init's sshd drop-in outranks a `60-` file.** sshd takes the *first*
value it sees, and `50-cloud-init.conf` sets `PasswordAuthentication yes`. The
hardening file is named `01-` so it always wins, including after cloud-init
rewrites its own file on a later boot.

**A newly approved user was handed a *running* workspace.** With no credit yet,
billing started immediately and put them into debt before their first sign-in.
Provisioning now hands back an **off** workspace.

## Why size changes are allowed while running (mostly)

Incus can update `limits.cpu`, `limits.cpu.allowance` and `limits.memory` on a
running container, so a resize does not have to interrupt anyone. Two things
had to be handled before allowing it:

**Billing.** Settlement reads whatever size is current when it runs, so a
customer could run at 3 vCPU for 59 minutes, drop to 0.5, and be billed for the
whole hour at 0.5. The elapsed part-hour is now settled at the OLD size before
the new one is applied, and a fresh period starts.

**Safety.** Memory may be raised live but not lowered live. Shrinking the limit
under a process already using more forces immediate reclaim and can have the
kernel kill a running coding agent - the exact failure this product exists to
avoid. CPU moves either way; the worst case is slowness.

## Why published ports use nftables, not Incus proxy devices

The obvious mechanism is an Incus `proxy` device. It cannot be used: the project
sets `restricted.devices.proxy=block`, and that restriction binds the PROJECT
rather than merely restricted certificates, so even root is refused with "Proxy
devices are forbidden".

Relaxing it would be worse than the inconvenience. A proxy device can listen on
*any* host port, so anything holding the control plane's certificate could bind
443 or 22 and intercept traffic. Plain nftables DNAT does the same job entirely
in the kernel, is faster than a userspace relay, and needs no Incus privilege.
The API owns the mapping table and hands the provisioner the COMPLETE desired
set on every change, so the ruleset is rebuilt wholesale and self-heals rather
than drifting through a sequence of deltas.

## More failures worth remembering

**The hardening script locked the operator out of their own server.** It
disabled SSH password authentication whenever `/root/.ssh/authorized_keys` was
non-empty. A cloud image or hosting provider routinely injects a key, so its
presence is not evidence the operator logs in with one. The dashboard stayed up
while the only administrative route in was gone. Password auth is now left
enabled unless `MMD_DISABLE_SSH_PASSWORDS=yes` is set explicitly - and the P0
suite, which had asserted the lockout was *correct*, now asserts that a working
login method still exists. A check that treats losing your own access as a pass
is worse than no check.

**The isolation ruleset had no `established,related` rule.** Return traffic for
host-initiated connections was dropped, so the host could not reach its own
workspaces at all - not even ping. It does not weaken the boundary: conntrack
only matches flows the host started, and workspace-to-host is still refused.

**Project ceilings were pinned to the size a workspace was created at**, so
`limits.cpu=1` made "change size" a one-way door - Incus rejected every increase
with "Reached maximum aggregate value". The ceilings are the top of the
catalogue; they exist to stop a compromised control plane asking for 64 cores,
not to fix a customer at whatever they first chose.

**A size change made while the machine was off never reached Incus.** Only the
database was updated, so the machine came back with its old limits while being
billed for the new size. The tier is now applied at power-on.

**Anti-enumeration made a silent no-op look like success.** Registering an email
that already exists returns the same response as a real signup, by design. When
a script re-registered an existing address to set a known password, it reported
success and changed nothing - and the password handed over was never valid.
