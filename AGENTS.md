# Working on MMD-DEV as an AI agent

Read this first. It is the fastest path from cold start to making a correct
change, and it records the things that are **not** discoverable from the code —
the decisions that look arbitrary until you know what went wrong without them.

If you read nothing else, read [Ten things that will bite you](#ten-things-that-will-bite-you).

---

## What this is

A commercial product, already sold to real customers, running on **one** Ubuntu
server. Each customer buys an isolated Ubuntu machine — root, `apt`, Docker, AI
coding tools preinstalled — billed by the hour in **Toman**, controllable from a
Persian web dashboard, able to power fully off to near-zero cost without losing
a byte.

Three facts shape almost every decision:

1. **The host has no hardware virtualization.** No `/dev/kvm`, no `vmx`/`svm`.
   Every VM design is off the table on performance grounds. The product is built
   on unprivileged **Incus system containers**, and the shared-kernel risk is
   accepted and documented rather than pretended away.
2. **It is a live system with paying customers.** Workspaces on this host hold
   people's work. Nothing in this repository should be tested by running it
   against a customer's machine.
3. **The interface is Persian; the codebase is English.** Both rules are
   enforced by tests. See [Language](#language).

---

## Orientation, in the order that makes sense

| Read | Why |
|---|---|
| `README.md` | What the product is and how it is brought up |
| `docs/ARCHITECTURE.md` | How the pieces fit and why the boundaries sit where they do |
| **`docs/DECISIONS.md`** | **The most valuable file in the repository.** Every non-obvious decision and every bug that has bitten, with the reasoning. Read it before proposing a redesign |
| `docs/BILLING.md` | The charge model. Precise, customer-specified, and easy to get subtly wrong |
| `docs/OPERATIONS.md` | Runbook: deploy, diagnose, recover |
| `docs/API.md` | Every endpoint |
| `docs/DEVELOPMENT.md` | Conventions, test layout, how to verify a change |

`docs/DECISIONS.md` is append-only in spirit. When you fix something whose cause
was not obvious, add a section. That file exists because the same traps were
being rediscovered.

---

## The shape of the system

```
Browser (Persian RTL SPA, no build step)
   │  HTTPS, session cookie
   ▼
nginx ──► mmd-api (FastAPI, user `mmd`)
              │  restricted Incus client cert   ──►  Incus (127.0.0.1:8443)
              │  unix socket, allowlisted verbs ──►  mmd-provisioner (root)
              ▼                                          │
          PostgreSQL                                     ▼
                                              Incus projects ws-1, ws-2, …
mmd-worker (user `mmd`) ── metering, hourly settlement, reconciliation
```

**The privilege split is the whole security design.** `mmd-api` is the
internet-facing component and holds a *restricted* Incus certificate: Incus
itself refuses it privileged containers, host-path disks and custom idmaps. It
cannot escalate. Anything genuinely privileged goes through `mmd-provisioner`,
which runs as root, has **no network listener**, checks `SO_PEERCRED`, and
accepts a fixed allowlist of verbs.

If you are adding a capability that needs root, add a **verb**, validate its
arguments independently inside the provisioner, and never widen the API's Incus
certificate.

---

## Ten things that will bite you

These are real. Each one cost hours.

1. **`pipefail` + `grep -q` gives false failures.** `grep -q` exits at the first
   match and SIGPIPEs its producer, which `pipefail` reports as a failed
   pipeline. The verify scripts deliberately do **not** set `pipefail` and say so
   in a comment. This has bitten twice.

2. **`pkill -f` matches its own parent.** The pattern you are searching for is in
   the command line of the shell running `pkill`, so it kills itself and the verb
   returns nothing. Use `pkill -x` (match process *name*). This has bitten twice.

3. **The Incus CLI hangs on non-TTY stdin.** `incus project create` reads a YAML
   definition from stdin whenever stdin is not a terminal, so under the
   provisioner it blocks forever with no output and no error. Every script that
   the provisioner calls does `exec </dev/null` once, at the top.

4. **`dns.mode: none` on the bridge is load-bearing.** Every workspace instance
   is named `ws` inside its own project, and Incus registers DNS names **per
   network**, not per project. Without this the *second* workspace to start is
   refused with "Instance DNS name already used on network" and simply will not
   boot. A one-tenant test never sees it.

5. **Never let anything run `nft flush ruleset`.** Incus owns its own nftables
   table including the masquerade that gives workspaces internet. Ubuntu's
   `/etc/nftables.conf` begins with `flush ruleset`, so `nftables.service` is
   **masked**. The symptom is nasty: DNS keeps working (dnsmasq is local) while
   everything else times out.

6. **`getComputedStyle(el).height` returns the border-box height under
   `box-sizing: border-box` in Chrome.** This clipped the terminal's last line
   for months. `#term` is deliberately `content-box`. Do not "tidy" it.

7. **The API must never return English prose.** It returns stable **codes**;
   `web/js/i18n.js` builds the Persian sentence. A customer opened a ticket about
   an English message that reached the dashboard. `tests/test_no_english_prose.py`
   walks the AST of every handler and fails on prose, including f-string pieces.

8. **Billing charges must stay idempotent** on `(workspace_id, period_start,
   kind)`. A worker restart mid-hour would otherwise double-charge a customer.

9. **Project ceilings are the top of the catalogue, not the customer's current
   size.** Pinning them to the size at creation made "change size" a one-way door
   — Incus refused any increase with "Reached maximum aggregate value".

10. **A restricted certificate cannot use the Incus file API** (403). That is why
    every file-manager operation goes through the root provisioner's `fs_*` verbs
    rather than talking to Incus directly.

---

## Language

Two rules, both enforced by tests:

- **Customer-facing text is Persian**, in `web/js/i18n.js` and nowhere else.
  Established technical terms stay Latin (CPU, Docker, SSH, vCPU, Claude Code) —
  translating them makes the product *harder* for its own users. Money is always
  labelled **تومان**, and numbers use Persian digits via `fmtMoney`/`fmtFa`.
  `tests/web/i18n.test.mjs` fails on any key whose value is not Persian, and on
  any key a page uses but the catalogue lacks.

- **Everything developer-facing is English**: this file, `docs/`, code comments,
  commit messages, log lines, test names, API error messages.

Adding a customer-visible string means adding a key to `i18n.js` and returning a
**code** from the API. Never a sentence.

---

## Conventions that are not negotiable

**Money is integer micro-Toman.** `MICRO = 1_000_000`. Floats drift and the
ledger stops agreeing with itself. `control/mmd/billing/pricing.py` is the sole
authority on cost — no other module may compute a price.

**Comments explain *why*, never *what*.** The codebase is dense with comments
that record a decision or a trap. Match that. A comment restating the code is
noise; a comment saying "NB: the key is `security.guestapi`, not
`security.devlxd` — Incus renamed it in the fork" is the reason the next person
does not lose an afternoon.

**One definition of a workspace.** `workspace/ws-lib.sh` holds the instance
config. `ws-create.sh` and `ws-reset.sh` both call it. A second copy would drift
and produce a reset machine subtly unlike a new one; a test asserts the config
block appears exactly once.

**Tests describe behaviour, not implementation.** Names read as sentences:
`test_a_customer_reply_reopens_a_closed_ticket`. Where a rule is subtle, the
docstring says why it matters, not what the code does.

---

## How to verify a change

```bash
bash tests/run.sh            # ~5s, no infrastructure. Run this always.
bash verify/p0-foundation.sh # host invariants (needs the real host)
bash verify/run-all.sh 1     # everything, against workspace 1
```

`tests/` needs nothing but the repo. `verify/` inspects the **running host** and
is how you check that a change actually took effect in the world rather than
only in the database.

Before claiming something works, run it. This repository has a history of bugs
that unit tests passed and reality did not — the SSH lockout, the two-workspace
DNS collision, the clipped terminal line. Where a claim is measurable, measure
it: there is a headless-Chromium harness pattern in `docs/DEVELOPMENT.md` for
checking rendered geometry and console errors.

**Never test against a customer's workspace.** Create a disposable one:

```bash
sudo bash workspace/ws-create.sh 9 1 1024 6 4
# ... test ...
sudo bash workspace/ws-destroy.sh 9 --yes
```

---

## Deploying

The services run from `/opt/mmd`, **not** from the repository:

```bash
sudo rm -rf /opt/mmd/{control,workspace,host,web,image}
sudo cp -r control workspace host web image /opt/mmd/
sudo systemctl restart mmd-api mmd-worker mmd-provisioner
```

`image/` is a **runtime** dependency, not just a build one — the provisioner
runs `image/apt-fixups.sh` inside workspaces and resolves it relative to its own
install directory. Forgetting to copy it breaks `apt_repair` silently.

Static assets are served at **`/static/js/...`**, not `/js/...`. A request to
`/js/anything.js` returns the SPA shell with a `200`, so checking status codes
alone will happily confirm a file that is not there.

---

## Things that are deliberately unfinished

Stated plainly so you do not "fix" a decision or assume a gap is an oversight:

- **Single host, no HA.** One failure domain. ZFS snapshots exist; off-host
  backups do not.
- **Docker volumes are sparse zvols** with no `refreservation`, so the 4 GiB
  Docker allocation is not truly reserved the way the 6 GiB rootfs is. Known;
  changing it alters disk accounting on live volumes, so it is the operator's
  call.
- **Shared Claude subscription.** The AI feature copies the *platform's* OAuth
  grant into customer machines. Any customer with root — which is all of them —
  can read that token. There is no way to give a container a credential and also
  withhold it. The UI says so; fair use is a policy control, not a technical one.
- **Tokens rotate**, so copied credentials drift out of date and the AI page
  offers a re-sync rather than pretending the sign-in is permanent.
- **No SMTP.** Approval and low-credit notices are in-dashboard only.
- **No Content-Security-Policy header.** Everything is same-origin and there
  are no inline handlers, so the gap is small, but it is a gap.

---

## When you are unsure

- A behaviour that looks wrong is often a decision. Search `docs/DECISIONS.md`
  and the surrounding comments before changing it.
- A number that looks arbitrary was usually measured. `docs/DECISIONS.md`
  records the measurements.
- If you cannot determine intent, say so and ask, rather than guessing at a
  change to a system that bills real people by the hour.
