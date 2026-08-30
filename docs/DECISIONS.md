# Decisions and the reasons behind them

Things that are not obvious from the code, and the failures that shaped them.

## Destructive UI describes impact; onboarding follows observable progress

Account deletion and factory reset are not ordinary confirmation prompts. The
interface names the affected identity, separates deleted from retained data,
states recoverability, and requires a typed identifier for account deletion.
This does not replace server authorization; it prevents acting on an adjacent
row in a dense administrator list.

First-run progress uses server-observable facts (positive credit, machine
power, an SSH key, a published port) except opening the browser terminal, which
has no durable server-side effect and is recorded locally after a running
terminal is rendered. The checklist disappears only after all steps complete.

Below 720px, primary navigation moves to a horizontally scrollable bottom rail
and destructive dialogs become bottom sheets. Tables retain their semantic
structure and scroll horizontally; turning unrelated columns into ad-hoc cards
would discard headings and make technical values harder to compare.

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

## The terminal was clipping its own last line

Reported by a customer: *"the bottom of the terminal is not visible and I cannot
see the last line."* Reproduced and measured in a headless browser rather than
guessed at.

xterm's FitAddon sizes the terminal from `getComputedStyle(parent).height` and
subtracts only the padding of **xterm's own element** - never the parent's. The
stylesheet had:

```css
#term{height:440px;padding:10px 12px}     /* under a global *{box-sizing:border-box} */
```

Chrome reports that element's computed height as the full **440px** while its
content box is **420px**, so the addon laid out ~12px more terminal than there
was room for and `.term-shell{overflow:hidden}` cut it off. The clipped strip is
the bottom line - which in a shell is the prompt, the one line you are always
looking at.

Measured before the fix, overflow past the visible area:

| font | 9 | 12 | 14 | 16 | 20 | 26 |
|---|---|---|---|---|---|---|
| overflow | **+20px** | +8px | **+12px** | +6px | +12px | 0px |

At font 9 a whole line was lost. At 26 it happened to land on zero, which is
presumably why it survived a manual look.

The fix is one declaration: `#term{box-sizing:content-box;height:420px}`. Under
content-box the declared height *is* the content height, so what the addon
measures and what the box can show are the same number. Re-measured across every
font size from 9 to 26: overflow ≤ 0 everywhere.

Two related deferrals went in at the same time, being the same
measure-before-layout mistake: fitting now happens on the next animation frame
after a font change and after the terminal first opens, rather than in the same
tick, because xterm re-measures its cell size asynchronously.

## Factory reset

A rebuild, not a snapshot rollback. A snapshot taken at provision time would
drift from the current golden image, so "factory reset" would restore whatever
the factory looked like months ago rather than what a new customer receives
today. `ws-reset.sh` deletes the instance and the Docker volume and builds both
again, keeping the project, its restrictions, its profile and its IP - so the
customer's reserved ports still point at the same machine and the addresses they
saved keep working.

The instance definition moved into `ws-lib.sh` (`ws_create_instance`,
`ws_attach_docker_volume`, `ws_wait_booted`) so create and reset cannot drift. A
second copy of that config block would eventually produce a reset machine that
is subtly not what the customer bought; a test asserts the block exists exactly
once.

Deleting the instance releases the Docker device but **not** the volume, so it
has to be removed explicitly or images and containers survive a reset the
customer was told is clean.

Kept across a reset - all of it lives in the dashboard, not on the machine, and
losing it would make a reset feel like an account closure: the reserved SSH and
RDP ports, the saved public keys, the machine's size, the credit balance and the
ledger. The service *switches* are turned off, because they described software on
a filesystem that no longer exists.

A machine in `ERROR` **can** be reset. That is the state where starting over is
most useful, and refusing there would leave the one situation with no
self-service way out. It is also how a reset that died partway is retried, since
every removal in the script is conditional.

Measured: a full reset takes ~14 seconds.

### The confirmation

Three separate, non-interchangeable things, all re-checked server-side:

1. **One acknowledgement per category of loss**, ticked individually - so the
   list is read rather than dismissed.
2. **The account's own email, typed.** Unlike a fixed phrase it cannot be copied
   out of the dialog, and unlike a checkbox it has to be produced.
3. **The account password**, verified by the server. This is the only part a
   stranger at an unlocked browser cannot supply, which makes it the one that
   carries real weight rather than ceremony.

The button stays disabled until all three hold and re-locks the moment any one is
undone. Escape cancels; **nothing confirms on Enter**, and a backdrop click does
not dismiss - a misplaced click should not throw away a half-filled dialog, and a
destructive action should never be reachable from a keystroke someone was
already making. A refused attempt is written to the account's activity log, so
someone probing an unlocked browser leaves a trace.

Verified by driving the dialog in a real browser: disabled on open, still
disabled with only the boxes ticked, still disabled with boxes and email, enabled
only once the password is entered, and re-disabled by changing the email or
unticking a box.

## A verification that cried wolf

The first version of the P6 leak check asserted that a workspace's `~/.claude`
contained *only* the credential file. It failed immediately on a live workspace -
because that customer had signed into Claude Code themselves and accumulated
their own history, projects and cache. Their data, in their machine, exactly as
intended.

The check had the direction wrong. What matters is not "the customer has nothing
extra" but "**the operator has nothing here**". It now hashes every private file
in the operator's `~/.claude` and asserts none of those hashes appears in the
workspace - only hashes are compared, no content is read out of either side.

That rewrite surfaced a second false positive: 389 identical files under
`plugins/marketplaces/claude-plugins-official/`, which is a **public** repository
both machines clone independently. Excluded by path. A check that reports 389
leaks when there are none is worse than no check, because it trains whoever reads
it to ignore the result.

Current reading: 425 operator files compared, 0 present in the customer's
machine.

## English prose must never reach a Persian interface

A customer opened a ticket whose entire body was a quote of what the dashboard
had shown them:

```
You need at least 510.00 credits to run for another hour. Your balance is 0.00.
```

Three faults in one sentence: English in a Persian interface, "credits" where
the product charges **Toman**, and Western digits. The cause was structural, not
a missed translation - the API built a finished English sentence and the page
printed it verbatim, so no amount of translating in the interface could have
helped.

The contract is now explicit and enforced: **the API returns stable codes and
numbers; the interface builds the sentence.** `fail()` messages were always fine,
because api.js resolves those by code and never displays the English. Everything
else that carried prose was converted:

| was | now |
|---|---|
| `blocked_reason: "You need at least…"` | `blocked: {code, need, have}` |
| `blocked_reason: adm.reason` | `blocked: {code: "capacity_memory"\|"capacity_cpu"}` |
| `message: "Your account is awaiting approval."` | `code: "pending_approval"` |
| `message: "Administrator account created…"` | `code: "admin_created"` |
| `warning: "Port 22 inside your machine…"` | `warning_code: "discouraged_port"` |
| `send_text("Your machine is switched off…")` | close code 4409, page prints Persian |
| `message: "Password changed."` | removed; nothing consumed it |

`AdmissionResult` had carried a `resource` field since it was written, with a
comment saying it existed "so the interface can say so in its own language
rather than parsing the English reason". Nothing had ever used it.

The sign-up page was the sharpest example of why prose in an API is a trap: it
decided which Persian message to show by **comparing the server's exact English
sentence**. Rewording that sentence - or adding a second caller - would have
silently shown the wrong message.

`tests/test_no_english_prose.py` walks the AST of every decorated request handler
and fails on any English sentence in a returned dict, including f-string pieces,
plus any English written into the terminal websocket. It was checked against a
deliberately reintroduced bug and reported it by function and key. The word
threshold is **two**, not four: "Password changed." slipped past a stricter one.

## A breadcrumb that printed `//home/dev`

Reported by a customer. The root crumb's *label* is `/`, and every crumb was
**also** joined with a `/` separator - so the root's own text and the first
separator were the same character, printed back to back:

```
"/home/dev"  ->  "//home/dev"
"/home"      ->  "//home"
"/"          ->  "/"            (the only path that looked right)
```

The separator now goes between the named parts only; the root supplies the
leading slash by itself.

`crumbs()` and `join()` moved into `web/js/paths.js`, a module with no DOM
imports, because files.js cannot be loaded in Node - which is why neither
function had ever had a test. Extracting them turned up a second, unreported
instance of the same bug: `join()` did not strip a trailing slash from the
directory, so creating a file while the server reported `/home/dev/` produced
`/home/dev//newfile`.

Ten tests now cover both, including the escaping of a directory name containing
markup.

## Real TLS on mmd-ai.ir, and the trap HSTS sets for published ports

The dashboard ran on a self-signed certificate bound to the bare IP. That is a
starting point, not a destination: a permanent browser warning trains users to
click through exactly the dialog phishing depends on.

`host/70-reverse-proxy.sh` now takes `MMD_DOMAIN` and `MMD_ACME_EMAIL` and
obtains a Let's Encrypt certificate. Without a domain it still falls back to
self-signed, so a fresh host comes up either way.

Issuance is **two-phase**, and has to be. nginx will not start with a
certificate path that does not exist, and certbot's HTTP-01 challenge needs
nginx already serving. So: write a config that serves `/.well-known/` with
whatever certificate is available, obtain the real one, then rewrite the config
to use it. The ACME location is excluded from the HTTPS redirect permanently,
because renewal needs it on port 80 too.

**`certonly --webroot`, not `--nginx`.** The nginx plugin rewrites the config
file, and this script owns and regenerates that file. Keeping issuance out of
the config means re-running the script cannot clobber the certificate setup.

Renewal is certbot's own timer plus a **deploy hook that reloads nginx**.
Without the hook a renewed certificate sits on disk while nginx keeps serving
the old one until someone happens to restart it — which is how a certificate
that "renews automatically" still expires.

One canonical origin. `www` gets a 301 rather than a second copy of the site:
the session cookie is scoped to the host that set it, so serving both names
means signing in on `www` and then following a link to the apex silently logs
you out.

### HSTS covers a host on every port

This is the part that nearly broke the product, and it is not obvious.

`Strict-Transport-Security` applies to a **host**, not to a host-and-port. Once
a browser has seen the header for `mmd-ai.ir`, it rewrites *any*
`http://mmd-ai.ir:<anything>` to `https://` — including port 29562.

Customers publish their own services on high ports, and the ports page had just
started advertising them as `mmd-ai.ir:29562`, because the address is built from
`request.url.hostname` and the request now arrives on the domain. So every
customer serving plain HTTP on a published port would have found it unreachable
from any browser that had ever visited the dashboard, with a TLS error that
looks like their own bug.

Two changes:

- **Everything a customer connects TO is advertised on `CONFIG.endpoint_host`**
  (`MMD_ENDPOINT_HOST`), which defaults to the machine's public IP detected from
  the routing table. In production it is `ports.mmd-ai.ir` — see the section
  below.
- **HSTS is sent without `includeSubDomains` and without `preload`.**
  `includeSubDomains` would extend the same trap to any name a customer's app
  might later be served from, and forecloses `apps.<domain>` as the prettier
  answer. `preload` is effectively irreversible and this is one host.

SSH and RDP addresses still follow the request host, and should: they are not
HTTP, HSTS does not apply, and `mmd-ai.ir:23409` is friendlier than an IP.

Two tests hold the line: one asserts a published port address never uses the
dashboard host, the other asserts the HSTS header claims neither subdomains nor
preload.


## One host for everything a customer connects to

The ports page showed bare IP addresses, which is ugly and unmemorable, so the
ask was to show the domain with `http://` in front. The obvious version of that
is wrong, and it is worth recording why.

**Measured in a real browser**, after visiting the dashboard so its HSTS policy
was stored, against a plain-HTTP listener on port 28999:

| asked for | browser did | result |
|---|---|---|
| `http://mmd-ai.ir:28999` | rewrote to `https://` | failed, `chrome-error://` |
| `http://ports.mmd-ai.ir:28999` | left it alone | loaded |

So the apex cannot carry customer ports while the dashboard sends HSTS. A
**subdomain** can, precisely because that header goes out without
`includeSubDomains` — which is what turns an earlier judgement call into a
load-bearing one. A test asserts the directive stays absent.

The wildcard `*.mmd-ai.ir` record already pointed at the host, so no DNS change
was needed.

`CONFIG.endpoint_host` now serves **published ports, SSH and RDP alike**, rather
than only the ports page. Two pages showing different addresses for the same
machine is a support ticket waiting to happen, and SSH and RDP have to live
wherever the published ports live. It defaults to the detected public IP so a
deployment with no domain still hands out something that works.

Published ports also gain a `url` field — `http://host:port` — which the page
renders as a clickable link. It is set only for **TCP** ports of kind **USER**:
a scheme in front of the reserved SSH and RDP rows would be wrong rather than
merely unhelpful, and `http://` makes no sense for UDP at all.

## CUPS was listening on the internet

Found while checking what else on the host answered plain HTTP. A `cups` snap
was running `cupsd` and `cups-browsed`, serving an unauthenticated web interface
on `0.0.0.0:631` — `/` and `/printers` both returned 200 — on a server with zero
printers configured.

Removed with `snap remove --purge cups`. Nothing about the product needed it; it
had simply arrived as a snap dependency and never been noticed.

## Publishing a port is free

It used to cost 15 Toman/hour, on the reasoning that "a published port holds a
scarce resource". It does not: it hands out an nftables DNAT rule and a number
from a range of ten thousand. Charging for it discouraged exactly the thing the
product exists to let people do — run something and keep it reachable.

The rate is gone from `DEFAULT_RATES`, `Rates`, and every formula;
`ports_micro()` no longer exists and `port_count` is no longer a parameter of
the gate, the settlement or the quote. A test asserts none of it comes back, by
checking the attributes are absent rather than merely that the number is zero.

Ledger rows written while it *was* charged keep their `ports` breakdown.
History is not rewritten because a price changed.

## Charts in real units, over a window you choose

The overview drew CPU and memory as **percentages** over a fixed six-hour
window. Both were wrong for the question people actually open that page with.

A percentage hides the two things worth knowing — how big the machine is and how
much is spare — and "90%" reads identically on half a core and on three. The
charts are now **cores** and **gigabytes**, scaled against the tier rather than
against the tallest sample, so a quiet machine draws a low line instead of a
dramatic one.

Six hours was the wrong default: in front of a running machine the question is
"what is it doing now". The default window is **5 minutes**, with a picker
offering 5m / 15m / 1h / 6h / 24h, remembered per browser.

### What that cost, and what it fixed

Five minutes at the old 60-second sampling is five points — not a chart. So
`TICK_SECONDS` went to **20**, and `SETTLE_EVERY`/`RECONCILE_EVERY` were set so
settlement still runs every 5 minutes and reconciliation every 15. That
preservation is the point: tripling the metering rate must not silently triple
how often money moves. A test pins the arithmetic.

`usage_samples` had **no pruning at all**, despite the plan claiming seven-day
retention — the table grew forever, and tripling the sample rate would have
tripled the rate it grew at. `prune_samples_once()` now runs with settlement.

The interface is told `sample_seconds` and refreshes in step, replacing only the
two chart bodies rather than re-rendering the page — a full redraw every 20
seconds would fight with anyone mid-edit to move two lines by a pixel.

`fmtFa` defaults to **zero** decimal places, which rendered 0.31 cores as "۰" and
made every quiet machine look idle. Precision is now chosen from magnitude.

## The admin capacity panel shows two different things

"ظرفیت سرور" was renamed, because it showed only what is **reserved** —
capacity promised to customers whether or not they touch it — and that is half
the question. An operator also needs to know what is actually being *used*: the
first says whether more machines can be sold, the second says whether the host
is comfortable.

`/api/admin/metrics` sums real consumption across every workspace, in the same
units and with the same picker as the customer view. Samples are written per
workspace within milliseconds of each other, so they are bucketed to the
sampling interval before summing — otherwise each workspace lands in its own
bucket and the total reads as a sawtooth of individual machines rather than a
host total. It scales against *schedulable* capacity, not the raw host: the host
reserve is not for sale.

## A page shipped broken because `node --check` cannot see a missing import

The overview page threw a `ReferenceError` and rendered nothing for every
customer. The cause: a shared chart module was added, and the edit that was
supposed to insert `import { usageChart, savedWindow, … } from "../usagechart.js"`
into `pages/machine.js` targeted the line `import { t } from "../i18n.js";` —
which does not exist in that file, since it reads `import { t, CURRENCY }`. The
replacement matched nothing and was never applied. Every one of those names was
then simply undefined.

Nothing in the existing checks could catch it:

- **`node --check` passes.** The file parses perfectly; an undefined identifier
  is not a syntax error.
- **Loading the module passes.** An undefined *identifier* is a runtime
  `ReferenceError`, not a link error, so imports resolving is not the same as
  names existing.
- **The console check on the landing page passed**, because `machine.js` is only
  *evaluated* when the overview renders, and the landing page never renders it.
- The unit suite never rendered the page at all.

`tests/web/imports.test.mjs` closes the gap: it collects every name exported by
any module under `web/js`, and for each file flags any of those names used as a
bare identifier without being imported or declared locally. Verified against the
bug by removing the import again — it names both symbols and where they come
from, while `node --check` still passes on the same file.

The wider lesson, which had already been written down here twice and was not
applied: **a page is not verified until it has been rendered.** Checking that a
module parses, that its assets return 200, and that some *other* page loads
cleanly proves nothing about it. The fix was verified by creating a disposable
workspace, binding a test account to it, and loading `/console` and
`/console/admin` in a real browser — which also confirmed the picker requests
the right window, marks the right button active, and remembers the choice across
a reload.

## Three things customers noticed that we had not

All three were reported through the support system, which is itself a good sign
that it works.

**Chart labels ran together.** «بیشترین ۰٫۵۱ هسته» — the word and the number
with nothing between them. `billing.chart.peak` was the bare word; it now
carries the colon, because every use site puts a value straight after it.
`billing.chart.now` deliberately did **not** change: it is a standalone label at
the right-hand end of a bar chart's time axis, with nothing following, and a
colon there would be dangling punctuation. A test pins both halves of that.

Not reported, but on the same line: the *current* value had no label at all —
just a bare number. It now reads «اکنون: ۰٫۳۱ هسته از ۲٫۰۰».

**"Your reply" when you wrote last.** The ticket page labelled its box
«پاسخ شما» regardless of who had spoken most recently, so a customer adding to
their own message was invited to reply to themselves. It now reads «پیام شما»
when their message is the latest and «پاسخ شما» when support's is. The staff
queue keeps «پاسخ پشتیبانی», because an operator genuinely is always replying.

**The unread badge was invisible.** This is the interesting one. The marker was
in the DOM the whole time — `<span class="badge on">جدید</span>` on every unread
row — but `.badge` had a rule **only** as `.tabs2 .badge`, scoped to the
Connections tab bar. On the support list it inherited nothing and rendered as
plain inline text. The nav counter said "1" and the list looked untouched.

A styled-only-in-one-place class is worse than a missing one: the markup reads
as correct in review, and only the rendered page shows otherwise. Fixed with a
bare `.badge` rule plus `.badge.new`, and the row itself is now tinted with a
coloured edge so the eye finds it without reading every subject.

Verified by creating two tickets in the two states — one where the customer
wrote last, one answered by staff — and reading the computed styles back out of
a real browser rather than trusting the markup.

## The header was drawn from a session object fetched once

A customer: *"I had 2 unread messages, I read them one by one, and the red circle
still showed ۲."*

`state.me` was fetched at page load, and the route guard only refetched it when
`state.me === null` — that is, only when signed **out**. Everything in the header
is drawn from that object: the support badge, the admin badge, and the credit
chip. Opening a ticket marked it read on the server correctly; the browser
simply kept showing a number from earlier in the session until a hard reload.

The credit chip had the same staleness and nobody had reported it: the balance
did not move after powering a machine on or off, or as credit was spent.

Now refetched on every navigation, plus immediately after a thread is opened —
the guard runs *before* the view marks the ticket read, so without the second
call the badge would only catch up on the following navigation.

That makes `/api/me` a hot path, so `_unread_count` stopped being N+1. It walked
the tickets in Python and touched `tk.messages` per ticket, lazy-loading a query
each time; it is now a single query joining each ticket to its last message
(`max(id)`, not `max(created_at)` — ids are monotonic and two messages can share
a timestamp).

Verified in a browser: ۲ → ۱ → gone, as each ticket was opened.

## Remaining time reads in days

«زمان باقی‌مانده» was shown in hours. At the default tier a funded account has
several hundred, and «۳۵۷٫۱ ساعت» is not a number anyone can act on. It now
reads «۱۵ روز».

`hours_remaining` stays in the payload — it is the honest unit the figure is
derived in, and days are a presentation choice on top. Both the overview and the
billing page were changed: they show the same figure under the same label, and
leaving one in hours would read as a contradiction.

## Destroying a workspace left its firewall rules behind

Found while cleaning up after a test. `sync_published_ports` rewrites the entire
DNAT rule set from the mappings it is handed — it was designed to be
self-healing — but it was only ever *called* when a port changed. Deleting a
workspace cascades its port rows away and nothing re-synced, so four DNAT rules
pointing at a machine that no longer existed sat in the kernel.

The worker now pushes the full set on every reconciliation pass rather than only
when it allocated something. Idempotent by construction, and it is the only
thing that removes rules for a workspace that is gone.

## The zip download staged customer data in host RAM

A customer asked whether the zip download in the file manager checks a limit
before running, because a 10 GB folder "could damage everything". It did not,
and they were understating it.

The order of operations was:

1. `incus file pull -r` the entire requested tree to the host
2. zip it, counting bytes
3. abort if the total exceeded `MAX_TRANSFER_BYTES`

**The guard was step 3. The damage was step 1.** And the staging directory was
`SPOOL = /run/mmd/spool` — `/run` is a **1.6 GiB tmpfs**, i.e. RAM, on a host
with 7.8 GiB total, and it also holds `provisioner.sock` and Incus's config.

So one click on "download zip" over a home directory could consume host memory
and fill the filesystem systemd, sshd and the provisioner's own socket live in.
Every tenant, from one customer's UI action. A workspace root is 6 GiB, so
reaching it required nothing unusual. The single-file path had the same shape —
`incus file pull`, then `os.path.getsize()`.

Three changes:

- **Measure first.** `_measure()` runs `du -sb` *inside the workspace* before
  anything is copied. Refusing now costs one `du`; refusing after the pull cost
  however much was asked for. Applied to both download paths.
- **The spool moved to `/var/lib/mmd/spool`** — disk, not RAM. A future mistake
  should cost disk, which is measurable and recoverable, rather than the memory
  the whole host depends on.
- The refusal carries the measured size and the limit, so the page can say how
  far over the folder is instead of reporting a generic failure.

`du -sb` is apparent size and does not follow symlinks, which matches what the
archiver actually writes. It is re-checked against the file that lands, because
a measurement is of the past and a file can grow in between.

Measured on a disposable workspace: a 629 MB folder is refused in **60 ms** with
nothing written to the spool; a small folder still zips; a 629 MB single file is
refused and a small one is not.

The limit stays 512 MB. It is now enforced where it can actually prevent
something.

## Nothing switches off a workspace the customer has paid for

There was an idle auto-stop: sixty minutes without activity and the machine was
powered off, commented as "protects the user's credit directly". The operator's
instruction was blunt and correct — *we must not touch their space without their
allowance*.

It went, and it deserved to go on the facts as well as the principle.

**What it measured was wrong.** `last_activity` is written in exactly two
places: power-on, and opening the browser terminal. Nothing else. So a customer
running a long build with no terminal attached, working in the file manager, or
serving traffic on a published port registered as idle and had their machine
switched off mid-work. It probed for live SSH and RDP sessions first — but only
when those services were switched on, which for most machines they are not.

**And it was firing.** Seven times in the week before removal: six on the
operator's own workspace and once on a customer's.

The argument for it was that it protects credit. Billing is hourly and in
arrears, so a machine left running is simply paid for — which makes leaving it
on the customer's decision, and one they are already paying to make. Guessing on
their behalf, from a signal that only sees one of the five ways into the
machine, is not protection.

Removed with it: `MMD_IDLE_STOP_MINUTES`, and the `probe_sessions` provisioner
verb, which existed solely so the timer would not kill a live SSH session. A
verb that serves nothing is surface for no benefit.

`last_activity` is still recorded and is now purely informational.

**What still stops a machine, and why that is different:** running out of credit.
The hourly gate stops a workspace whose balance cannot cover the coming hour, and
a zero balance archives it (recoverable for 30 days). Those are the billing model
rather than a guess about what the customer wants, and they were explicitly kept.

`tests/test_no_auto_stop.py` asserts there is no idle timer, that every
destructive action in the lifecycle pass is reached through a balance check,
that the one stop in the settlement pass is guarded by the affordability check,
and that nothing in the API stops a workspace outside a request handler.

## `node --check` does not check ES modules

While fixing something else, a stray closing brace went into `pages/machine.js`.
`bash tests/run.sh` passed. So did `node --check web/js/pages/machine.js`.

Node treats a `.js` file as CommonJS. When it meets an `import` statement it
stops rather than failing, so **the syntax gate was passing every file under
`web/js`** — all of which are modules. Demonstrated with identical content:

```
ctrl.js   (import + stray brace)  ->  node --check exits 0
ctrl.mjs  (same bytes)            ->  node --check exits 1, SyntaxError
```

That is the second page-breaking bug to reach production past that gate; the
first was a missing import, which `node --check` also cannot see because an
undefined identifier is a runtime error. The gate now copies each file to `.mjs`
before checking, which forces module parsing. Verified by reintroducing the
brace: the suite fails and names the file.

Two lessons, both already written here and both re-learned the hard way:
editing by string index eats adjacent code, and a check that has never failed is
not evidence that it works.

## Numbers were rendered with tabular figures everywhere

A customer: *"on various pages the numbers — remaining credit, hourly costs —
use an unsuitable font, the characters and digits are too far apart."*

`font-variant-numeric: tabular-nums` pads every digit to one advance width so
columns line up. On Persian numerals that stretches the narrow ones, and it was
applied to the header credit chip and the big overview values as well as to
table columns. Measured on «۴۹۹٬۳۲۷ تومان» at 24px: **160.9px tabular vs 145.4px
proportional — 9.6% wider**, showing as gaps between the digits.

Tabular figures are right for a column read vertically and wrong for a single
value read as a phrase. Display values are now proportional; `td.num` keeps
tabular, and a test asserts both — removing it there would make the ledger
ragged, which is the same mistake pointing the other way.

Two things found alongside: the unit («تومان») could wrap away from its number,
and `font-feature-settings:"ss01","ss02"` carried a comment saying those read
better *off* while the syntax turned them **on** — a bare tag means 1. Measured
at zero width difference either way; they change the drawn shapes of ۴ ۵ ۶ only.
Now off, as the comment always claimed.

## The admin list was the hardest place to read machine state

It printed the state as bare text — «روشن · 1.0 vCPU · 1.0 GB» — while every
other view, including the account-status column immediately beside it in the
same table, used a pill with a coloured dot. The one screen showing every
machine at once was the one you had to read word by word.

`statePill()` moved to `ui.js` so there is a single definition: green running,
amber mid-change, red needs attention, grey stopped. A test asserts no page
redefines it and that every state the API can return has a tone.

## Billing AI tokens: two facts that decide the whole design

The ask was to meter Claude Code usage per workspace from
`~/.claude/projects/<project>/<session>.jsonl`, price it per model, and take it
from the customer's credit as they go. Reading the real logs first changed two
things about how it had to work.

### One API response is written to the log many times

Claude Code appends an assistant record **per content block** — the text, the
thinking, each `tool_use` — and every one of them repeats the *same*
`message.usage` totals. Measured on one real session: **2,317 assistant records
for 1,122 actual messages**, which inflates output tokens by **2.37×**.

Usage is therefore attributed once per `message.id`. Summing rows would have
billed every customer roughly double, and it would have looked entirely
plausible.

### Cache tokens are the bill

The ticket said "input and output tokens". On that same session, base input was
**2,240** tokens against **526 million** cache reads:

| category | tokens | rate | USD |
|---|---|---|---|
| input | 2,240 | $5/M | $0.01 |
| cache write (1h) | 10,088,199 | $10/M | $100.88 |
| cache read | 526,112,225 | $0.50/M | $263.06 |
| output | 1,284,858 | $25/M | $32.12 |
| | | | **$396.07** |

Billing input and output only would have charged **8%** of what the usage cost.
All four categories are metered, and the two cache-write durations are kept
apart because a 1-hour write is 2× base input where a 5-minute write is 1.25×.
Where an older record carries only the combined figure it is treated as the
cheaper 5-minute write — the choice that cannot overcharge.

### Counting happens inside the workspace

38 MB of session logs here, largest file 19.9 MB. Copying them to the host to
parse would repeat the mistake the zip download made, and would put customers'
conversations on the host for no reason. A scanner is piped to `python3 -` in
the container and returns only totals — 0.25 s for the lot. It is piped rather
than written, so nothing is left behind in a machine the customer owns.

### High-water marks, not offsets

The scanner reports **cumulative** totals per session; the control plane stores
what it last saw and charges the difference. That makes the whole path
idempotent — a pass that runs twice, or dies halfway, cannot double-bill — and
it is what answers the operator's question about a customer destroying their
workspace. The session files go; the marks do not. A vanished session simply
stops producing deltas.

Deltas are **clamped at zero** and marks only ever move **up**, so a rebuilt
workspace reporting smaller numbers produces no charge and no refund rather than
a negative one.

A model with no price is deliberately left **unmarked**: its tokens stay
uncounted, so they bill correctly once someone sets a price instead of being
silently given away. The admin panel surfaces those models.

### The chain, all of it editable

tokens → USD at Anthropic's per-model rates → Toman at an admin-set exchange
rate → a discount multiplier. Seeded at 200,000 Toman per USD and 90% off, i.e.
the customer pays a tenth. The discount is stored as the *percentage* because
that is what an operator says out loud, and converted in one place. It is
clamped to 0–100 so a typo cannot invert a charge into a credit — a test covers
that.

Charged every 5 minutes, matching the worker tick, with the bucket as the
idempotency key. Running often is the point: the platform's own subscription is
what is being spent, so the gap between usage and payment is the window in which
an empty account keeps spending.

## Hermes on a shared OpenRouter account: three problems, one answer

The operator asked for a second agent, Hermes, backed by a single OpenRouter
account — and named the three things that made it hard. Researching OpenRouter's
own capabilities answered all three without building a proxy.

**"If I expose my secret api key to the spaces it will be revealed."** Correct,
and unavoidable if the key is handed out. So it is not: the account key is used
as a **Management key** that never leaves the host, and every workspace gets its
own key minted from it (`POST /api/v1/keys`). A workspace key IS visible to its
owner — that cannot be prevented, the agent has to read it — but it spends only
their budget, is capped by its own `limit`, is model-restricted, and is revoked
with one call. The master key is never exposed at all.

**"Different spaces must have a distinguishing parameter."** OpenRouter meters
per key: `usage`, `usage_daily/weekly/monthly`, `limit_remaining`. Attribution
comes from OpenRouter's own billing rather than from a log inside a machine the
customer controls, so it cannot be under-reported by tampering — a strictly
better position than the Claude metering, which reads files the customer could
edit.

**"Users are not able to select each model, it can lead to very high price."** A
**guardrail** attached to the key carries a model allowlist; anything else is
refused with 403 upstream, before a token is spent. The spread justifies the
worry: `openai/o1-pro` is **$600/Mtok** output against **$10** for
`claude-sonnet-5`. Sixty times.

### The allowlist is a price ceiling, not a list

OpenRouter carries 417 models today and adds more constantly. A hand-written
list is wrong within a month, and the failure mode of a stale list is a customer
reaching a model nobody meant to sell them. `build_allowlist()` therefore
generates the list from a **ceiling in USD per million output tokens**, so it
holds whatever appears.

At $40 it blocks 52 models — o1-pro, the gpt-5.x-pro family, opus-4.7-fast,
o3-pro, opus-4.1 — and leaves 365, including claude-opus-5 at $25 and everything
cheaper. Named families are denied outright as well, because a ceiling alone
would admit a cheap member of an expensive family.

### Zero credit disables the key; credit restoration re-enables it

An OpenRouter limit of zero is not a reliable stop signal, and the earlier
minimum-cap safeguard meant an empty account retained a small amount of real
spending power. The worker now meters the final observed usage and sets the
supplier key's `disabled` flag whenever the Toman balance is zero or negative.
It records that state locally, allowing the fast worker pass to notice a credit
transition without polling every upstream key continuously. Adding credit sends
`disabled=false` with a new cap, so the same customer key works again without
being exposed or copied a second time.

OpenRouter limits are cumulative over a key's lifetime. A restored cap is
therefore `usage already reported + affordable new headroom`; using only the
new balance would leave a topped-up key blocked as soon as its historical usage
exceeded that number.

Unpriced models are excluded rather than assumed free: an entry with no price is
usually one whose cost is not published yet, and guessing in the customer's
favour there is guessing with the operator's money.

### The spend cap closes the window that token metering leaves open

Each key's `limit` is set to what the customer can actually afford. OpenRouter
then refuses the request the moment they run out, instead of us noticing at the
next poll. For Claude the exposure is bounded by the five-minute charge cycle;
here it is zero.

### The Management key lives with the worker, not the API

`mmd-api` faces the internet. A key that can mint spending capability does not
belong in the process most likely to be attacked, so the worker holds it and
reconciles: mint on enable, sync the cap when the balance moves, poll usage,
revoke on disable. The provisioner cannot hold it either — it runs with
`IPAddressDeny=any` and has no route to OpenRouter at all, which is exactly the
property that makes it safe to run as root.

## Enabling Hermes answered with a schema error

Clicking the toggle printed, at the customer:

    action String should match pattern '^(install|unlink)$'

`/api/workspace/ai/hermes` validated its body with `AiAction`, a model written
for Claude Code, whose verbs are `install` and `unlink`. The handler two lines
below asked for the opposite vocabulary — `ws.hermes_enabled = (body.action ==
"enable")` — so Pydantic rejected every request with a 422 before the code that
wanted the word `enable` ever ran. The feature could not be switched on at all.

Two models now, `AiAction` and `HermesAction`, because the two services really
do have different verbs: Claude Code is *installed* on a machine, while Hermes
is *enabled* as an intent the worker reconciles. Sharing one model made a
sentence about one service into a wall in front of the other. The handler's own
`if body.action not in (...)` check is gone with it: it was unreachable, and an
unreachable guard is what let the mismatch look plausible in review.

Worth noting the failure was also a leak of the kind `test_no_english_prose.py`
exists to catch — a raw Pydantic error reaching a Persian interface. That test
walks `fail()` and `return`, not FastAPI's own 422 bodies, which is why it did
not fire.

## A vhost reconciler that cannot leave nginx broken

`mmd-hermes-vhosts.py` wrote its files, ran `nginx -t`, and on failure logged
and returned — leaving the file that failed to parse exactly where it was. The
next unrelated reload, a certbot renewal say, would then fail too, long after
the log explaining why had scrolled past.

`mmd-vhosts.py` replaces it. It snapshots what it is about to change and
restores it when `nginx -t` fails, writes one file per customer rather than one
per host (both `nginx -t` and a reload re-read the whole directory), and can
obtain a DNS-01 wildcard per customer when `MMD_ACME_DNS_PLUGIN` is configured.

### A correction about Let's Encrypt

The wildcard path was initially justified on the grounds that HTTP-01 per host
could not scale, because Let's Encrypt allows 50 certificates per registered
domain per week. **That reasoning was wrong**: renewals are exempt from that
limit — ARI-based renewals are exempt from every limit — so 50/week applies only
to *genuinely new* names. One name per customer costs one certificate, once,
and then renews for free. Confirmed in production:
`hermes.heidary13794.mmd-ai.ir` was issued and served in **12 seconds**.

So the wildcard is an optimisation, not a prerequisite. `ISSUE_BUDGET = 40`
remains, and it bounds *churn* rather than capacity: exhausting the quota would
take renewal down for every customer.

## The reconciler was never in a provisioning script

`mmd-hermes-vhosts.service` and its timer existed on the one host somebody had
run the install commands on, and in no script. A rebuild would have come up with
every customer's dashboard silently unpublished. `60-control-plane.sh` now
installs and enables the unit, and disables the superseded one.


## Why SSH and RDP cannot be addressed by name

Asked repeatedly, and worth recording with the measurement rather than the
assertion, because the answer is counter-intuitive: HTTP applications *can* be
served at `<name>.<username>.<domain>`, so why can't SSH?

Captured from a real OpenSSH client, connecting to two different hostnames:

    you type:      ssh ssh.heidary13794.mmd-ai.ir
    client sends:  b'SSH-2.0-OpenSSH_10.2p1 Ubuntu-2ubuntu3.5\r\n'

    you type:      ssh ssh.test-1.mmd-ai.ir
    client sends:  b'SSH-2.0-OpenSSH_10.2p1 Ubuntu-2ubuntu3.5\r\n'

Byte-identical. The same capture for HTTP:

    b'GET / HTTP/1.1\r\nHost: api.heidary13794.mmd-ai.ir:39222\r\n...'
    b'GET / HTTP/1.1\r\nHost: api.test-1.mmd-ai.ir:39222\r\n...'

HTTP puts the hostname **inside** the connection as text, which is the only
reason many names can share one IP address. The SSH client resolves the name to
an address and then discards it; nothing on the wire says which workspace was
meant. TLS solves this with SNI, but SSH is not TLS, and RDP does not open with
a TLS ClientHello either — there is an X.224 preamble first, and mstsc only
sends SNI in some configurations.

So on ONE IPv4 address, `<username>.<domain>:22` cannot be routed per customer.
Not a missing rule: nftables can match destination address and port, and both
are identical for every name. **The information a rule would need is not in the
connection.**

What would work, in order of preference:

1. **IPv6.** A `/64` gives every workspace its own global address; an `AAAA` per
   name and port 22 works with no proxy at all. This host has no IPv6 — no
   address, no default route — so it is a question for the provider. Iranian
   clients without IPv6 would still need the port, so it is an additional path
   rather than a replacement.
2. **More IPv4 addresses**, one per workspace. Does not scale past a handful.
3. **Route on the SSH username** — `ssh <username>@<domain>`, via a bastion such
   as sshpiper. Works on one address because the username travels inside the
   protocol, the way `Host:` does. Costs a new privileged daemon on port 22,
   displacing the host's own sshd, and does nothing for RDP.

None of these is built. SSH and RDP keep `CONFIG.endpoint_host:PORT`.


## `ping` did not work in a workspace

Reported from inside a machine:

    dev@ws:~$ ping google.com
    ping: socktype: SOCK_RAW
    ping: socket: Operation not permitted
    ping: => missing cap_net_raw+p capability or setuid?

`ping` needs one of two things, and a fresh workspace had neither. Measured
rather than guessed:

| | host | workspace |
|---|---|---|
| `net.ipv4.ping_group_range` | `0 2147483647` | `65534 65534` |
| `getcap /bin/ping` | — | *(none)* |
| `dev` gid | | `1002` |

1. The **unprivileged ICMP datagram** route needs the caller's gid inside
   `ping_group_range`. A new network namespace does **not** inherit the host's
   value, and `1002` is outside `65534 65534`. That sysctl also cannot be fixed
   from inside: `/proc/sys/net` is not writable from the container's user
   namespace and `sysctl -w` is refused — verified.
2. The **SOCK_RAW** route needs `cap_net_raw` on the binary. Ubuntu's
   `iputils-ping` postinst sets exactly that with `setcap`, and **that call
   fails silently when dpkg runs inside an unprivileged container**, so the file
   arrives with no capability at all.

`image/net-fixups.sh` sets the capability from inside the workspace. Confirmed:
`2 packets transmitted, 2 received, 0% packet loss`.

### Why it cannot go in the golden image

Because it would not survive, and the reason is worth writing down. Workspaces
run with **`security.idmap.isolated=true`**, so every one has its own uid range
(one measured at `Hostid 1065536`). A v3 `security.capability` xattr embeds the
**rootid of the namespace it was set in**, so a capability baked into the shared
image carries the *build* container's rootid and does not apply in a workspace
mapped anywhere else.

So it is applied per workspace, by the provisioner, at provision, at reset — a
reset rebuilds the filesystem from the image and takes the capability with it —
and via the existing repair verb. It must also be re-appliable because
`apt upgrade` replaces `/bin/ping` and drops the capability again.

`libcap2-bin` became an explicit package rather than a transitive dependency: it
provides `setcap`, which is the tool the whole fixup is built around.

### One mechanism, not two

`_apply_apt_fixups` grew a sibling, so the plumbing became
`_apply_fixup_script(project, path, slug)` and both call it. The two families
share a cause — dpkg cannot do privileged things inside an unprivileged
container — which is also why `apt_repair` now runs both: an operator reaching
for "repair" wants the machine working, not one named subsystem.


## Every free model was blocked, and the fix blocked the routers

Reported from a customer's agent:

    ❯ tell me scope of this host...
    ┊ HTTP 404: No endpoints available matching your guardrail restrictions
      and data policy

on `nvidia/nemotron-3-ultra-550b-a55b:free`. Read as a data-policy problem at
first glance. It was ours.

`build_allowlist` carried `if pin <= 0 and pout <= 0: continue`, written to
exclude models whose price OpenRouter has not published — the reasoning being
that guessing in the customer's favour is guessing with the operator's money.
But `_num` turns an **absent** price into `0.0` as well, so "unknown" and
"free" were the same value. Measured against the live catalogue: **17 `:free`
models, 0 of them in the allowlist.**

Three states share one representation, and collapsing any two of them breaks
something:

| pricing | meaning | verdict |
|---|---|---|
| absent / `""` | not published yet | exclude — could be anything |
| `"0"` | published, and free | **include** — cheapest thing on the menu |
| `"-1"` | no fixed price; a router that picks a model at request time | exclude — a ceiling checked now means nothing later |

`is_priced()` now separates them.

### The fix caused a second outage, which is the more interesting half

Admitting zero-priced models also admitted the `"-1"` routers, and the guardrail
**refuses those by name**:

    HTTP 400 Invalid allowed_models: openrouter/auto, openrouter/auto-beta,
    openrouter/bodybuilder, openrouter/free, openrouter/fusion, openrouter/pareto-code

They had been excluded by accident — `-1 <= 0` satisfied the old rule — so
removing the rule removed the accident with it.

A rejected PATCH does not fail loudly: **the previous allowlist stays in force**,
which looks configured and is not. That is now the second time this has happened
(floating `~` aliases were the first, and are excluded for the same reason). So
two changes, not one:

- the whole `openrouter/` namespace joins `ALWAYS_DENY`;
- `_push_allowlist()` reads the rejected ids back out of the 400 and **retries
  once without them**. "The catalogue grew something new" is not a problem that
  stops recurring, and a policy missing one model beats a policy silently months
  out of date.

Result: 374 models allowed, all 17 free ones among them, no routers, and the
expensive families still blocked.

### Note on free endpoints and the data policy

The error names the data policy as well as the guardrail, and that half is real:
OpenRouter's free endpoints are often served by providers that may train on
inputs, and an account that forbids those sees exactly this 404 with a perfectly
correct allowlist. Worth deciding deliberately — routing customers' code through
training-enabled endpoints is a privacy question, not a billing one.


## "Resync sign-in" reported success and changed nothing

Reported as: clicking «تازه‌سازی ورود» succeeded, and `claude` in the workspace
still opened a login flow.

The credentials were never the problem. They were present, valid, and
`claude -p 'reply ok'` answered normally — which is why nothing non-interactive
ever caught this. What was missing was one key in `~/.claude.json`:

    hasCompletedOnboarding: true

Measured with two HOMEs identical but for that key:

| | first interactive screen |
|---|---|
| without | `Welcome to Claude Code v2.1.240 / Let's get started. / Choose the text style…` |
| with | straight to the normal trust-folder prompt |

The first is the onboarding wizard, and a customer meeting it reasonably reports
it as being asked to log in.

### Why the key stayed missing, which is the actual bug

The install path wrote the config like this:

    [ -e /home/dev/.claude.json ] && exit 0

*Write it only when the file is absent.* Claude Code creates that file on its
very first run, before the customer has finished anything — so from the moment
they typed `claude` once, resync could never set the key again, while continuing
to report success on every click.

The evidence was visible across the host:

    ws-1  11 keys  hasCompletedOnboarding absent   <- reported the bug
    ws-8   1 key   hasCompletedOnboarding true     <- file was absent; ours landed
    ws-3  44 keys  hasCompletedOnboarding true     <- onboarded by hand

Only the machine where the customer had never run `claude` got the key.

It is a **merge** now, not all-or-nothing. The old comment's instinct — *the
customer may have their own settings by now* — was right; the implementation was
what was wrong. Verified on the reporting workspace: 11 keys became 12,
`oauthAccount`, `userID` and `machineID` all intact, and `claude` goes straight
to the prompt.

Corrupt configs are moved to `.claude.json.corrupt` rather than overwritten, and
a JSON array — which `json.load` accepts and `.update()` would choke on — counts
as corrupt.

### The button lied because success was measured wrong

`_claude_status` derived `linked` from `[ -s .credentials.json ]` — the file is
non-empty. That is "a file exists", not "this works", and install returned
`ok: st["linked"]`. So the one machine where the wizard still opened was
precisely the machine that reported success.

Status now carries `onboarded` separately, install requires both, and the page
has a third state — «نیازمند تنظیم اولیه» — for signed-in-but-not-configured.
Calling that "ready" is what made the bug invisible for so long.


## A username can name a published port, but it cannot route it

Each customer-published port is now shown at both
`MMD_ENDPOINT_HOST:<external>` and `<username>.<domain>:<external>`. Both names
resolve to the same IPv4 address and both reach the same nftables DNAT rule.
The readable customer name is useful, but it is important not to assign it a
power it does not have: the globally unique **external port** still selects the
workspace.

Arbitrary TCP and UDP packets do not carry the DNS name the client resolved.
Therefore `ali.<domain>:25000` and `sara.<domain>:25000` are indistinguishable
after resolution when both names point at this host. Customers cannot reuse the
same public port on one IPv4 address merely because their hostnames differ.
HTTP can do that through its `Host` header and TLS can do it through SNI; raw
TCP and UDP cannot in general.

Customer-published rows also no longer ask for a protocol. One reservation is
expanded into TCP and UDP rules before crossing the provisioner boundary. The
provisioner continues to accept only the concrete words `tcp` and `udp`, which
keeps privileged validation simple. SSH and RDP reservations remain TCP-only
because those services do not speak UDP.

Existing customer rows are widened during schema patching. The old interface
allowed the same internal port to be published separately for TCP and UDP, so
such pairs are collapsed before conversion; otherwise both would become
`protocol=both` and violate the uniqueness constraint.


## The automatic stop, and why it is not the idle timer coming back

Ticket #17, from a customer:

> امکان تنظیم کردن خاموشی خودکار ماشین یا دادن الرت برای خاموش کردن برای عدم
> مصرف زیادی منابع **البته باید پرسیده شود که میخواهیم ماشین روشن بماند یا خیر**

The emphasis is theirs, and it is the whole design: *of course it must ask
whether we want the machine to stay on.*

This reverses **"Nothing switches off a workspace the customer has paid for"**
above, so the difference has to be stated precisely, because the earlier
decision was right about the thing it was reacting to.

| | the removed idle stop | this |
|---|---|---|
| trigger | 60 min without browser-terminal activity | 12 h since power-on |
| signal | `last_activity` — written in exactly two places | elapsed time |
| consent | none | stated up front, waivable in one click |
| a long build | **killed**, because it looked idle | runs to the deadline like anything else |

The old one failed because it tried to tell a busy machine from an abandoned one
and could not: `last_activity` never saw SSH, RDP, the file manager or a
published port, so it killed real work. **This one does not try.** It treats
every run the same and asks the customer instead. That is why the operator's
original instruction — *we must not touch their space without their allowance* —
is satisfied rather than overridden: the allowance is now explicitly requested,
and the customer who wants a machine to keep running says so.

### The waiver is per run, and that is the load-bearing part

`auto_stop_at` is set on **every** transition into ON — the power button, and
the reconciler restoring a machine after host downtime. "Keep it running" clears
it for that run only.

A persistent *"never stop this machine"* preference was the obvious alternative
and is worse. It would be ticked once, by exactly the customer most likely to
forget a machine, and would then hand the cost straight back — which is the cost
this feature exists to prevent. Resetting on every start means the choice is
always made about a machine somebody has just decided to run.

### No warning before the stop

Considered and rejected: a customer who is not watching gets no benefit from a
notice thirty minutes out, and one who is watching already sees the countdown on
the machine page. What matters is that the rule is **stated while the machine is
running and something can still be done about it** — a full-width coloured bar
above the fold, with the remaining time and the button, not a line of grey text.

### Kept apart from the credit path

`auto_stop_once()` is its own worker pass, not a branch in `lifecycle_once()`.
Every stop in the lifecycle pass is a consequence of an empty balance, and
`tests/test_auto_stop.py` asserts that is still true — a credit path that
quietly grew a second reason to stop a funded workspace is how the first version
of this went wrong. It also runs on **every tick** rather than every settlement,
so a machine due at 12h00m does not bill on to the next five-minute boundary,
and it settles the part-hour before flipping the state, exactly as the Stop
button does.

`last_activity` is still recorded and still read by nothing.


## Capture DOM event targets before opening an asynchronous dialog

The machine's power-off button worked while power-on appeared dead. Power-on
first awaited the new cost confirmation and only then read
`event.currentTarget`; browsers clear that dispatch-only property before the
promise resolves, so the handler dereferenced `null` and never reached the API.
Capture the element before any `await`. The regression test asserts this
ordering because an API test cannot observe browser event lifetime.

A blocked power action is also left clickable. Disabling it gave no response at
all and looked like the same bug; clicking now renders the structured Persian
reason supplied by the workspace response.


## Recovery actions, connections and notifications share global primitives

An error sentence without a next action still leaves the customer stranded.
`recoveryNote()` maps stable API codes to the state that resolves them—billing,
power, SSH keys, resources, retry or support—and preserves page-owned retry
logic through an explicit callback. Unknown failures lead to support rather
than inventing a remedy. High-risk machine, resource, package, port and
connection journeys use this shared component first.

Connections remains detailed in its Terminal, SSH and RDP tabs, but now begins
with one launcher showing all four access families, including published
applications. Each card answers the same questions: is it available, what is
missing, and what is the next direct action. The API returns published mappings
with the service state so that overview cannot drift from the Ports page.

Notifications are durable rows, not longer-lived toasts. Operation results and
support replies are written at the transaction that makes them true. Changing
conditions—low balance, approaching auto-stop, archive deletion and session
expiry—are idempotently materialised on read using a per-user dedupe key, then
resolved when no longer true. Reading is independent of resolution: a critical
condition remains discoverable after its badge has been cleared.

Factory reset now treats Hermes like the optional software it is. It clears
intent before revoking the old supplier key, preventing another worker pass
from minting a replacement if the image rebuild fails. A revocation failure
stops reset in a retryable ERROR state rather than losing the only key identity
or claiming a clean machine. On success, installation flags, key, dashboard
credentials and error state are all cleared; enabling Hermes again is an
explicit customer choice.


## Destructive and long operations are durable state, not HTTP requests

Factory reset, package installation and account deletion can take minutes and
cross process boundaries. Running them inside the request made a closed browser,
proxy timeout or API restart indistinguishable from failure. Worse, account
deletion used to erase its database identity before every external side effect
was known to be complete, leaving no safe way to retry a leaked resource.

They now enter an `operations` table and the worker advances them. Reset and
installation retain their terminal result for the customer; the dashboard polls
that resource globally, so changing page or refreshing does not lose progress.
Only one operation may be active for a workspace at a time.

Account deletion has a stricter order:

1. mark the account `deleting`, which invalidates its sessions;
2. revoke its OpenRouter key;
3. rebuild the public-port firewall without its rules;
4. destroy its Incus project and storage;
5. erase tickets, messages, metrics, keys, ports, ledger, audits, workspace,
   operation and user rows.

Every external step is idempotent. If one fails, the identity and operation are
kept and a later worker pass retries; failed cleanup is ordered behind ordinary
queued work so an unavailable upstream cannot starve every customer's reset or
installation. Database erasure is the final step, never the first. A
published-port removal follows the same rule on a smaller scale:
the firewall is successfully rebuilt without the mapping before its reservation
row is deleted.

A factory reset preserves control-plane property (size, balance, ledger,
reserved addresses and saved public keys), but clears facts about the filesystem
that was destroyed (installed/enabled service flags, cached authorized-keys
content, activity and automatic-stop timestamps). Keeping those flags was the
source of a particularly misleading state: the dashboard could say software was
installed when its disk no longer existed.
