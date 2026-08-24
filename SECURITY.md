# Security Policy

## Reporting a vulnerability

**Please do not open a public issue.** Use GitHub's private reporting —
*Security* → *Report a vulnerability* on this repository — or contact the
maintainer directly.

Please include what you found, how to reproduce it, and what an attacker could
do with it. If you have a proof of concept, say so; do not run it against the
production host or against any workspace that is not your own.

You will get an acknowledgement. This is a small project run by one person, so
please be realistic about response times, but a genuine isolation break or
authentication bypass will be treated as urgent.

---

## What this system is trying to protect

Each customer gets root inside their own container. That is the product, not a
flaw. The boundaries that matter are:

1. **Workspace → host.** A customer must not reach the host, the Incus API, the
   cloud metadata endpoint, the provider's LAN, or RFC1918.
2. **Workspace → workspace.** Tenants must not reach each other, by IP, by ARP
   spoofing, or through shared storage.
3. **Control plane → host.** An RCE in the web application must not become host
   compromise.
4. **Customer → another customer's data.** Ownership is checked on every
   resource; a missing check is a vulnerability even if nothing is displayed.

Things in scope include: container escape, reaching another tenant, escalating
from the API's restricted certificate, reading another account's data, forging a
session, bypassing the billing gate, and anything that lets a customer reach the
operator's own files.

---

## Known and accepted risks

Stated so nobody has to rediscover them, and so a report about one of these is
not mistaken for news:

- **Shared kernel.** The host has no hardware virtualization, so containers are
  the only viable isolation. A kernel privilege-escalation bug crosses a
  boundary that a hypervisor would resist. Mitigated by unprivileged containers,
  isolated idmaps, AppArmor, seccomp and unattended upgrades — not eliminated.
  Admin approval of every signup is the compensating control, and it is
  load-bearing.

- **`security.nesting=true`** is required for Docker inside a workspace and
  relaxes some AppArmor rules. It is the price of the Docker requirement.

- **The shared AI subscription.** The Claude Code feature copies the
  *platform's* OAuth grant into a customer's machine so they do not have to log
  in. Any customer with root — which is all of them — can read that token and use
  it elsewhere. There is no way to hand a container a credential and also
  withhold it. The interface says so; fair use is a policy control.

  What is *not* accepted: anything of the operator's beyond that one credential
  reaching a workspace. The copy is an allowlist — one file opened, one key kept,
  re-serialised into a fresh document — enforced in code, verified by a test that
  asserts exactly one filename is ever read, and narrowed again by a systemd
  sandbox that shows the provisioner nothing else under `/root`.

- **Single host, no HA, no off-host backup.** One failure domain for every
  tenant and their data.

- **No Content-Security-Policy header yet.** The interface loads only
  same-origin assets and has no inline event handlers, so the exposure is small,
  but a CSP would make that a guarantee rather than a property of the current
  code.

---

## Design notes a reviewer may find useful

- The internet-facing service holds a **restricted** Incus certificate. Incus
  itself — not application logic — refuses it privileged containers, host-path
  disks and custom idmaps.
- The only root component has **no network listener**, checks `SO_PEERCRED`, and
  accepts a fixed verb allowlist, re-validating every argument.
- Incus listens on loopback only.
- Accessing another account's resource returns **404, not 403** — a 403 confirms
  existence and permits enumeration.
- Registration returns an identical response whether or not the address already
  has an account.
- Destructive actions require the account password, re-checked server-side.
- Secrets are never placed in argv, where `ps` would expose them to every
  process on the host; they travel on stdin.

`verify/p2-escalation.sh`, `verify/p2-cert-scope.sh` and
`verify/p6-integrations.sh` assert several of these against the running system.
