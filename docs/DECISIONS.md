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

## Why Escape does not exit the terminal's fullscreen

The obvious shortcut is Escape, and it was the first implementation. It is
wrong: Escape belongs to whatever is running inside the terminal. Binding it at
the page level breaks vim, less, and every interactive prompt - the programs
developers spend the most time in.

The primary way out is a button that lives INSIDE the fullscreen container, so
it stays visible when everything outside goes away (the earlier version put it
in the card header, which vanished on fullscreen - that is how there came to be
no visible way out at all). `Ctrl+Alt+F` is offered as a keyboard alternative
because no shell program claims it, and a hint naming both appears on entry.

## Why the terminal does not connect automatically

Opening the console used to open a live session immediately. That holds a
websocket and a pty nobody asked for, and surprises anyone who came to read
their balance. The session now starts on an explicit action and can be
disconnected without leaving the page.

## Currency

Prices are Toman, held as integer micro-Toman so partial-hour arithmetic stays
exact, and rounded to whole Toman for display - a fraction of a Toman is not
something anyone can pay. Persian digits and grouping are used for money and
for prose-like numbers; Latin digits are kept for anything a developer copies
into a shell (ports, package names, sizes), where Persian digits would be
wrong.

## Language split

Persian is the customer's language, English is the developer's. The interface
holds its own catalogue (`web/js/i18n.js`) and the API answers in English with
a stable `code` field; the interface translates by code. That way server logs
stay readable to whoever is on call, no page has to parse English prose, and a
test asserts every code the API can emit has Persian text - so a new error can
never reach a customer untranslated.

## Two workspaces could not run at the same time

Found while testing RDP, not by any earlier test - because every earlier test
ran exactly one workspace. Each instance is named `ws` inside its own project,
and Incus registers DNS names PER NETWORK rather than per project, so the
second workspace to start was refused outright:

    Failed start validation for device "eth0":
    Instance DNS name "ws" already used on network

That is a total multi-tenancy failure that only appears once a second customer
exists. Fixed with `dns.mode=none` on the bridge, which is also correct on its
own terms: workspaces are firewalled from each other, so publishing their
hostnames into a shared zone serves nothing and leaks the existence of other
tenants. Outbound resolution is unaffected - dnsmasq still forwards. The P0
suite now asserts it.

Worth recording as a testing lesson: "it works" measured on one tenant says
nothing about a multi-tenant system.

## Measured cost of SSH and RDP inside a workspace

Tested in a real unprivileged container on this host, not estimated.

| State | Memory | Disk |
|---|---|---|
| sshd listening, nobody connected | 5.2 MB | already in the image |
| xrdp listening, nobody connected | 4 MB | — |
| lean XFCE + xrdp installed | — | **236 MB** |
| active RDP session (Xorg + XFCE) | ~100 MB over idle | — |

The listeners are effectively free; the expense is the SESSION, and the disk.
That inverts the intuition that "turning the listener on costs resources".

(An earlier note here said ~800 MB of disk. That was wrong: it compared against
an assumed 1 GB base image rather than a measured one. Measured properly -
before install, after install, after `apt-get clean` - the incremental cost is
236 MB.)

### Which desktop is actually lightest

Counter-intuitive, and worth recording. Installed size including xrdp and Xorg:

| | Size | Packages |
|---|---|---|
| xrdp + Xorg alone (irreducible) | 197 MB | 57 |
| fluxbox + xterm | 294 MB | 115 |
| **XFCE lean** | **299 MB** | 151 |
| icewm + xterm | 299 MB | 126 |
| openbox + tint2 + pcmanfm | 352 MB | 153 |
| XFCE full (+ goodies) | 355 MB | 230 |
| LXDE core | 364 MB | 179 |
| LXQt core | 381 MB | 247 |

The X server and xrdp account for 197 MB of whatever you choose, so swapping
XFCE for a minimal window manager saves about 5 MB - and once a minimal setup
grows a panel and a file manager it costs MORE than XFCE. "Pick something
lighter than XFCE" is a false economy here.

`xrdp` with the **Xorg backend** (`xorgxrdp`) works in an unprivileged
container - `Session started successfully for user dev on display 10`, with
Xorg, xfwm4, xfce4-panel and xfdesktop all running. No Xvnc fallback needed,
and no privileged container.

One trap: `systemctl disable --now xrdp xrdp-sesman` does NOT reclaim the
session. sesman deliberately leaves the X server and desktop running so a
disconnected client can reattach, so disabling must also end the session
explicitly or ~100 MB stays resident with no listener to reach it.

## Keys and the SSH switch are separate controls

The first version put a textarea and a single button on the card, so it was
unclear what pasting a key would do or which control committed it. Worse, the
two concerns were entangled: the only way to add a key was to switch SSH on.

They are now independent:

* **Keys** live in their own table, one row each, added one at a time through
  an input with an explicit Add button and removed individually. Adding a key
  never opens a listener.
* **The switch** only starts or stops sshd. It refuses to switch on with zero
  keys, because that would publish a service nobody can authenticate to.
* Removing the *last* key while SSH is on is refused rather than silently
  locking the customer out - they must switch SSH off first, so the
  consequence is a decision instead of a surprise.

The help text names where the public key lives on Linux, macOS and Windows,
because "paste your public key" assumes knowledge most people do not have, and
the failure mode of guessing is pasting a PRIVATE key into a web form.

## `apt install firefox` and why snaps can never work here

Ubuntu's `firefox` package is a 77 kB transitional stub whose only job is to
install the firefox **snap**. Inside an unprivileged container the snap install
hook dies with:

```
error: cannot perform the following tasks:
- Run install hook of "firefox" snap if present
  (run hook "install": cannot fstatat canonical snap directory: Permission denied)
dpkg: error processing archive .../firefox_1%3a1snap1-0ubuntu5_amd64.deb (--unpack)
```

snap-confine needs mount and AppArmor privileges the container does not have,
and must never be given - they are the isolation the product rests on. So this
is not a bug to fix in snapd; snaps are simply unavailable in this design.

The failure is worse than it looks. dpkg is left mid-transaction, so the
customer's *next* apt command fails too and the machine appears broken rather
than the package unavailable.

`image/apt-fixups.sh` replaces the stub with **Mozilla's own APT repository**,
which ships Firefox as a real `.deb`. Two details that are easy to get wrong:

* **The pin is mandatory, and 1000 is the number.** Ubuntu's stub is version
  `1:1snap1-0ubuntu5` - the *epoch* sorts it above any plain Mozilla version, so
  a priority of 990 looks reasonable and changes nothing. Only `>= 1000`, which
  apt documents as "install even if this is a downgrade", actually wins.
* **The wreckage is cleared first.** A machine that already hit the bug needs
  `dpkg --remove --force-remove-reinstreq firefox` and `dpkg --configure -a`
  before anything else, or repairing forward leaves it just as stuck.

snapd itself is purged: it cannot function, it runs two daemons a 1 GiB machine
cannot spare, and leaving it installed makes snap-backed packages look available
right up until they fail.

The same file is the single source of truth for both paths - the golden image
runs it at build time, and the provisioner runs it inside an existing machine
(`apt_repair`) so workspaces created before it existed are repaired without an
image rebuild. That made `image/` a **runtime** dependency of the control plane,
so `host/60-control-plane.sh` now deploys it to `/opt/mmd` alongside `control/`.

Measured: Firefox 154 installs in ~40 s, costs 314 MB, and renders a page
headlessly inside the container.

## Claude Code sign-in: an allowlist, not a copy

Workspaces are sold with the coding agents already signed in, using the
platform's own subscription. That means the provisioner reads out of the
operator's home directory - and `~/.claude` also holds every repository they
have opened, every conversation, every plan, and a 59 kB `~/.claude.json` that
is almost entirely account record and history.

So nothing is copied. `_claude_credentials()` opens exactly one file,
`.credentials.json`, keeps exactly one key, `claudeAiOauth`, and **re-serialises
it into a fresh document**. Anything that file gains in a future release -
telemetry ids, machine identifiers, session pointers - is dropped by
construction rather than by someone remembering to add it to a blocklist. The
workspace's `~/.claude.json` is *generated* (`{"hasCompletedOnboarding": true}`),
never copied, purely to skip the first-run wizard.

The boundary is enforced at three levels, on purpose:

1. **systemd.** The provisioner keeps `ProtectHome=yes`, so `/root` is empty to
   it. One directory is bound back in **read-only** at
   `/var/lib/mmd/host-claude/.claude`. It cannot write to the operator's home
   at all, and can see nothing else under it.
2. **Code.** One filename, one key, re-serialised.
3. **Tests.** `tests/test_claude_auth.py` asserts the allowlist, and monkeypatches
   `Path.read_text` to assert that the list of files actually opened is exactly
   `[".credentials.json"]` - not merely that the output looks right.

The credential travels to the workspace **on stdin**, never in argv, where it
would be visible in `ps` to every process on the host. It lands `0600 dev:dev`
under a `0700` directory.

Why the whole `~/.claude` directory is bound in rather than the single file:
Claude Code rewrites credentials atomically (write, then rename), and a bind
mount of a *file* would keep pointing at the replaced inode and serve an expired
token forever. The narrowing therefore happens in code, where it is testable.

Two consequences worth stating plainly, both surfaced in the UI:

* **It is the platform's account, not the customer's.** Anyone with root in
  their own workspace - which every customer has, by design - can read the token
  and use it elsewhere. There is no way to hand a container a credential and
  also withhold it. The AI page says so, and fair-use is a policy control, not a
  technical one.
* **Tokens rotate.** A refresh in one place eventually invalidates copies
  elsewhere, so the page offers "تازه‌سازی ورود" rather than pretending the
  sign-in is permanent.

## The desktop password is required every time

It used to be required only on the first enable, then reused. That hands the
customer a desktop on a public port guarded by a credential they may not
remember, and - if the machine ever changed hands - one a previous holder still
knows. Retyping eight characters is cheap; a password nobody can account for is
not.

`RdpRequest.password` deliberately carries **no** `min_length`: Pydantic would
answer a short password with a 422 whose body the Persian interface cannot
translate, so the handler checks the length itself and returns
`rdp_password_short`.

## A reservation that only exists once you look at it is not a reservation

The Connections page promises SSH and RDP ports reserved from the moment the
machine exists. `reserve_service_ports()` was written for exactly that - and
never called from anywhere. Workspaces provisioned through the admin approval
flow got no rows, so both tabs rendered a blank address. It went unnoticed
because the first workspace had been given its ports by hand during testing.

Now held in three places, because the invariant matters more than any one path:

* at **provision**, so the addresses exist with the machine;
* in the **worker**, every fifteen minutes, so a machine created before this
  existed is repaired with nobody present;
* on **read** in `/api/workspace/services`, so the page is never blank.

All three are idempotent - an existing reservation is returned, never replaced -
because a customer may already have saved the address in an SSH config.

## A slow stop is not a failure

`/1.0/operations/{id}/wait?timeout=90` is a **long poll**: Incus holds the
connection open. The httpx client's default read timeout was 30 s, so a machine
that legitimately took longer to stop - one with an RDP session and a few SSH
logins holding processes open - raised `ReadTimeout` while Incus went on to
complete the stop successfully. The API then recorded a failure that had not
happened and parked the workspace in `ERROR`.

`_wait()` now sets a per-request read timeout of `timeout + 15`.

The second half of the bug was worse: `reconcile_once()` only looked at `ON` and
`OFF`, so a workspace in `ERROR` (or `STARTING`/`STOPPING`) was never examined
again. The customer saw a permanently broken machine and had no control that
could fix it. The reconciler now includes those states and adopts whatever Incus
reports, clearing the error - a transient failure heals within a tick.

Observed in production on a live workspace, which is how it was found.

## Support tickets

A conversation, not a form. Two status transitions are automatic because leaving
them manual loses messages: a **customer** message always moves a ticket back to
`open` (including reopening a closed one), and a **staff** message moves it to
`answered`. Everything else is the operator's to set.

Answering a *closed* ticket leaves it closed - the operator closed it
deliberately, and a follow-up note should not silently requeue it. A customer
writing to a closed ticket does reopen it, because the alternative is refusing
the message and pushing them into a duplicate that has lost all its context.

`from_staff` is recorded at write time rather than derived from the author's
current role: a customer later promoted to admin must not have their old
questions retroactively become staff answers.

Unread marks are per side and are stamped with the **last message's timestamp**,
not the wall clock. Marking with `now()` leaves the result depending on clock
precision - and with SQL-side `now()` on one side and Python's on the other, a
reply written in the same second as a read could never show as unread. That is
also why `mmd/tickets.py` normalises timezone awareness before comparing:
PostgreSQL returns aware timestamps and SQLite naive ones, and comparing the two
raises.

Reading someone else's ticket returns **404, not 403** - a 403 confirms the
ticket exists, which is enough to enumerate how many other customers there are
and when they wrote.
