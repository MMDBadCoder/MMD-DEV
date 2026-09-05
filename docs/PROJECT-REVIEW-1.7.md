# MMD-DEV 1.7 — Repository-wide product and engineering review

**Review date:** 2026-09-04
**Reviewed revision:** `80806dc` (`v1.7.0`, `main`)
**Implementation status updated:** 2026-09-05
**Scope:** product, customer journeys, Persian copy, UI/UX, accessibility,
architecture, security, billing, lifecycle, operations, observability, testing,
performance, and maintainability.

This is a decision document, not a change list. No production behaviour was
changed during the review. Findings are ranked so that a later implementation
can be selected deliberately instead of treating every suggestion as equal.

## Executive summary

MMD-DEV has an unusually clear core for a young commercial infrastructure
product. Its strongest decisions are the Incus privilege split, integer
micro-Toman accounting, idempotency keys for hourly charges, durable background
operations, a no-build Persian SPA, and extensive behaviour-oriented tests.
Version 1.7 also establishes a much healthier product boundary: an account and
its OpenRouter key can exist without a workspace.

The largest remaining risks are not visual polish. They are correctness at
concurrent transaction boundaries and disaster recovery:

1. Capacity admission, workspace creation, and operation creation use
   check-then-write sequences without serialization.
2. SMS-code consumption and issuance still need PostgreSQL-safe concurrency;
   authentication also lacks layered abuse controls and password recovery.
3. PostgreSQL backups contain plaintext customer/API credentials and are sent
   to Telegram without application-level encryption; workspace data has no
   off-host backup at all. The owner has accepted Telegram transport for now,
   but the workspace recovery gap remains.
4. Dependency and managed-tool installation are not fully reproducible, while
   certificate fallback capacity may not match continued hostname growth.
5. Account erasure, SMS retention, audit history, logs, metrics, and backups do
   not yet share an approved retention/anonymisation contract.
6. Large control-plane modules, the sequential worker, duplicated service state
   machines, and stringly typed settings remain long-term change risks.

The first-run journey now treats OpenRouter-only use and creating a machine as
equal choices. Mobile navigation, dialogs, tabs, asynchronous feedback, control
labels, chart alternatives, reduced motion, and keyboard access have also been
improved. Remaining UI work is narrower: the seven-item AI information
architecture, mobile data tables, secondary-form validation, just-in-time secret
reveal, and rendered browser geometry/contrast gates.

The recommended next order is: serialize capacity and destructive operations;
make SMS-code concurrency safe; decide retention and recovery policies; harden
authentication and installer reproducibility; then address the remaining mobile,
AI-navigation, and modularity work.

**Current-state note (2026-09-05):** the review findings above describe the
1.7.0 baseline. Since then, atomic balance movement (F-01), atomic AI charge
checkpoints (F-02), managed-route readiness (F-07), verified phone changes and
session revocation (F-09/F-10/F-16), the two-path first-run journey (F-19), and
most of the low-risk accessibility/reliability findings have been implemented
and deployed. The authoritative current status is the disposition near the end
of this document; the original evidence remains intact for audit history.

**Backlog progress:** 24 of the original 74 findings are implemented, three are
closed by explicit owner decision, and 47 remain open or partially implemented.
The active count must decrease whenever work lands; completed findings stay in
the implementation table for traceability but no longer belong to the active
backlog.

## Method and confidence

The review covered the repository rather than a single feature. It read the
orientation documents, the decision log, billing rules, API and operations
runbooks, backend models/routes/workers, privileged provisioner, host vhost
reconciler, SPA pages and design primitives, deployment units, observability
configuration, and tests.

The baseline suite passed:

- 600 backend tests
- all frontend JavaScript tests
- all static/invariant checks in `tests/run.sh`

Passing tests establish a strong baseline, but do not invalidate findings about
database races, crash boundaries, browser accessibility, external delivery,
or disaster recovery. Those require integration, concurrency, browser, or
restore testing that the fast suite intentionally does not perform.

Severity used below:

- **P0:** can lose money, corrupt billing, destroy or irrecoverably expose data.
- **P1:** material security, availability, or core-journey failure.
- **P2:** meaningful UX, operability, maintainability, or scale issue.
- **P3:** polish, clarity, or low-risk consistency issue.

“Confirmed” means the behaviour follows directly from the current code or
configuration. “Risk” means a realistic failure mode that needs an integration
or production-safe validation before calling it a bug. “Decision” means the
current behaviour may be intentional but its trade-off should be explicit.

## Prioritized findings

### P0 — correct before expanding the product

#### F-01 — Balance writes can be lost under concurrent billing and credit grants

**Status:** Confirmed.
**Evidence:** `control/mmd/service.py::post_transaction` reads a
`CreditAccount`, increments `balance_micro` in Python, then commits. There is no
row lock, optimistic version, advisory lock, or atomic SQL increment. The API
and worker are separate processes and both write balances.

Two transactions can read the same balance, independently apply a grant or
charge, and let the last commit overwrite the first. The ledger rows may both
exist while the cached balance represents only one of them. This violates the
central financial invariant.

**Recommendation:** make ledger insertion and `balance_micro = balance_micro +
:delta` one database transaction. Lock the account row or use an atomic update;
retain the unique charge key; explicitly handle serialization/uniqueness
retries. Add PostgreSQL concurrency tests that run a grant and charge in
parallel and assert `balance == sum(ledger)`.

#### F-02 — AI usage can be charged twice after a process crash

**Status:** Confirmed.
**Evidence:** both `control/mmd/service.py::meter_ai_usage` and
`control/mmd/hermes.py::meter` call `post_transaction`, which commits, then
advance the usage mark/account usage in a later commit.

If the worker dies after the charge commit but before the checkpoint commit,
the retry sees the old checkpoint. The duplicate charge key prevents one exact
bucket from being inserted, but the checkpoint remains stale and a later bucket
can charge the same delta again.

**Recommendation:** insert the transaction, mutate the balance, and advance the
usage checkpoint in the same transaction. `post_transaction` should flush, not
own the commit. Add crash-injection tests at every write boundary for Claude,
Codex, and OpenRouter metering.

#### F-03 — Database backups expose the product’s most sensitive data

**Status:** Confirmed design risk.
**Evidence:** OpenRouter keys, Telegram bot tokens, managed-dashboard passwords,
phone numbers, password hashes, and financial history live in PostgreSQL.
`control/mmd/backup.py` creates a database dump and sends it as a Telegram
document. There is no application-level encryption step.

Transport encryption is not enough: the Telegram account/cloud and retained
documents become another copy of production credentials. A database restore
also restores usable supplier keys.

**Recommendation:** encrypt every archive locally with an operator-controlled
`age`/GPG recipient before it leaves the host; never send the decryption key
through Telegram; define retention and deletion; test restore into an isolated
environment; rotate supplier/customer secrets after any suspected backup
exposure. Prefer encrypted object storage with immutability and lifecycle rules,
using Telegram only for status notifications.

#### F-04 — Customer workspace data has no off-host recovery path

**Status:** Confirmed known gap.
**Evidence:** the documented backup covers PostgreSQL. ZFS snapshots remain on
the same single host. The project explicitly has no off-host workspace backup.

Disk/pool loss, host loss, theft, or destructive operator error can permanently
remove paying customers’ work. “Power off without losing a byte” describes
persistence across stop/start, not durability, but customers may reasonably
read it as the latter.

**Recommendation:** define an RPO/RTO, implement incremental encrypted ZFS send
to a separate failure domain, monitor backup age, perform scheduled restore
drills, and make the product copy distinguish persistence from backup. Until
then, state plainly that users must keep source in external version control.

### P1 — core correctness, security, and journey failures

#### F-05 — Workspace admission is race-prone

**Status:** Confirmed.
**Evidence:** power-on checks running/starting workspaces and then commits the
new `STARTING` state. Concurrent requests can both observe free capacity before
either state becomes visible.

**Impact:** configured host ceilings can be exceeded, causing memory pressure,
failed starts, or host instability.

**Recommendation:** serialize admission with a PostgreSQL advisory lock or a
dedicated capacity row locked `FOR UPDATE`. Recalculate inside the lock and add
a parallel-start integration test.

#### F-06 — Workspace and operation creation use unsafe check-then-insert flows

**Status:** Confirmed.
**Evidence:** `/api/workspace` checks “no workspace,” obtains `next_free_idx`,
then inserts. Operation creation similarly checks for an active operation before
inserting, without a database uniqueness rule that represents “one active
operation.”

Concurrent clicks/retries can select the same slot, return an internal error,
or queue conflicting destructive operations.

**Recommendation:** express the invariants in PostgreSQL (unique partial index
for active operations where possible), lock allocation, catch and translate
conflicts to stable API codes, and add concurrent request tests.

#### F-07 — OpenCode/OpenWebUI readiness does not include hostname readiness

**Status:** Confirmed.
**Evidence:** only Hermes has `hermes_vhost_ready`; user ports have `web_ready`.
The vhost reconciler updates those fields, but OpenCode/OpenWebUI API readiness
is based on installation/password state. Their buttons can therefore appear
ready before nginx/certificate reconciliation has published the hostname.

**Impact:** the launcher can send a customer to the main homepage, a 404, or a
gateway failure—the exact experience previously reported for managed tools.

**Recommendation:** model routing readiness for every managed web service,
probe the expected upstream and hostname, and expose separate states:
installing, installed/waiting for address, ready, and failed. Never label a
service ready solely because its process was installed.

#### F-08 — Authentication endpoints lack adequate abuse controls

**Status:** Confirmed gap.
**Evidence:** password sign-in has no repository-level IP/account limiter. SMS
codes are limited per phone/purpose only. An attacker can rotate phone numbers
to spend the operator’s SMS budget. Availability endpoints are also publicly
probeable.

**Recommendation:** add layered limits per IP, subnet, account/phone, and global
provider budget; exponential delay for repeated password failures; generic
responses; proxy-aware client-IP handling with a trusted-proxy boundary; metrics
and alerts; and a provider-side daily spend ceiling. Avoid CAPTCHAs until the
lower-friction controls prove insufficient.

#### F-09 — Changing the account phone does not verify the new phone

**Status:** Confirmed.
**Evidence:** `/api/profile` requires the current password and validates format
and uniqueness, but never sends or verifies a code for the replacement phone.
Phone is the sole contact identity and a sign-in credential.

**Impact:** a typo or attacker with a password can redirect SMS login and
security notices, potentially locking out the owner.

**Recommendation:** stage the new number, verify a code sent to it, notify the
old number, then commit the change. Require a recent re-authentication and add a
cooldown/recovery process.

#### F-10 — Password changes do not revoke existing sessions

**Status:** Confirmed.
**Evidence:** sessions are stateless signed user IDs with a fixed lifetime.
Changing `password_hash` does not alter or revoke already-issued cookies.

**Recommendation:** add a `session_version`/`credentials_changed_at` claim or a
server-side session table. Password changes and account suspension should revoke
all sessions; expose device/session history and “sign out everywhere.”

#### F-11 — SMS one-time codes have concurrency races

**Status:** Confirmed from transaction structure; reproduce on PostgreSQL.
**Evidence:** issue and verify select unconsumed rows without locking. Two
simultaneous valid verifications can both observe the code as unconsumed; two
simultaneous issues can both survive.

**Recommendation:** consume with one conditional `UPDATE ... WHERE consumed_at
IS NULL ... RETURNING`, lock issue rows/advisory-key by phone and purpose, and
test parallel verification.

#### F-12 — There is no complete forgotten-password journey

**Status:** Confirmed product gap.
SMS lets a user enter the account, but changing the password still requires the
old password. A user who forgot it can remain permanently unable to restore
password login.

**Recommendation:** add a dedicated SMS-verified password reset with short
reauthentication lifetime, revoke all sessions, notify the phone, and rate-limit
it separately.

#### F-13 — First-administrator bootstrapping is race-prone

**Status:** Confirmed.
Registration decides `first = select(User).limit(1) is None`. Simultaneous first
registrations can both decide they are the initial administrator.

**Recommendation:** remove public first-user promotion in production. Bootstrap
with a one-time deployment token/CLI or serialize it with a locked singleton
row and a database constraint.

#### F-14 — Dependency/install paths are not reproducible enough

**Status:** Confirmed.
Runtime Python dependencies use compatible-release ranges, and workspace tool
installers include network-fetched scripts/package-manager installs that can
resolve to new upstream versions. OpenCode’s curl-to-shell path is especially
sensitive. OpenWebUI is pinned, which demonstrates the safer pattern.

**Impact:** the same MMD release can build different machines tomorrow; an
upstream compromise or breaking release reaches root-capable customer systems.

**Recommendation:** lock exact versions and hashes for the control plane; pin
every managed tool; download to a file, verify checksum/signature, then execute;
stage upgrades in a disposable workspace; record installed versions in the API
and UI; retain a known-good rollback artifact.

#### F-15 — Account erasure and retained records have contradictory semantics

**Status:** Confirmed policy conflict.
The deletion worker explicitly removes transactions, account state,
notifications, tickets, matching audit rows, operations, and workspace state.
However `SmsMessage` deliberately uses `SET NULL` and retains phone/body after
user deletion. Backups also retain historical copies until manually removed.

This conflicts with the earlier product expectation that deletion leaves no
trace, while financial/audit obligations may require exactly the opposite.

**Recommendation:** decide and document a legal retention policy by data class.
Choose between deletion, irreversible anonymisation, and time-limited retention;
apply it consistently to SMS/provider IDs, audit detail/targets, logs, metrics,
backups, support exports, and third parties. The UI must state what is deleted,
retained, for how long, and whether recovery is possible.

#### F-16 — Login succeeds before account status is enforced

**Status:** Confirmed UX/security ambiguity.
Password and SMS login issue a session and send a login notification before
checking rejected/deleting/suspended status. `current_user` later rejects some
statuses, while pending users can enter portions of the console.

**Recommendation:** define an explicit status/access matrix. Return the same
credential error where enumeration matters, but do not mint a normal session or
send “successful login” notices for blocked accounts. If pending-account access
is intentional, give it a focused approval-waiting experience rather than the
normal console shell.

#### F-17 — Public phone availability exposes customer membership

**Status:** Confirmed privacy trade-off.
`/api/auth/phone-available` reports whether a number exists. This contradicts
the anti-enumeration design of login-code requests and makes bulk customer
membership discovery easy.

**Recommendation:** remove live phone availability or heavily rate-limit it.
Report a generic conflict only after submission/verification. Username
availability can remain public because usernames become public hostnames.

#### F-18 — Certificate fallback capacity does not match managed-hostname growth

**Status:** Confirmed operational risk.
The HTTP-01 fallback deliberately limits issuance to 40 per week, but a customer
can now enable multiple managed hostnames plus published applications. The old
“roughly one name per customer” assumption no longer holds.

**Recommendation:** treat wildcard DNS-01 as a production requirement, expose
certificate queue/failure state to customers and admins, alert on issuance age,
and validate renewal. Keep rate-limit-safe backoff for the fallback.

## Product and customer journeys

#### F-19 — The product needs two explicit first-run paths

**Priority:** P1.
Version 1.7 correctly permits an approved user to own only an OpenRouter key,
but onboarding still naturally centres the workspace. The first screen should
offer two equal outcomes:

- **Use AI API:** add credit, reveal/copy key, choose discounted models, view
  spend and limits—without creating a machine.
- **Create a development machine:** choose capacity, understand hourly cost,
  create, power on, and connect.

Remember the choice without hiding the other path. A workspace deletion should
return to this chooser while preserving the OpenRouter product.

#### F-20 — Payment/credit is an operator-mediated bottleneck

**Priority:** P1 product gap.
The repository exposes admin credit grants but no self-service payment,
invoice, payment status, or automatic reconciliation journey. This adds latency
at the moment an external OpenRouter user has hit a limit and wants to resume.

**Recommendation:** add idempotent payment intents, a supported Iranian payment
provider, signed callback verification, pending/succeeded/failed states,
receipts, reconciliation, and immediate dirty-limit propagation. Never infer a
successful payment from a browser redirect alone.

#### F-21 — The credit model needs a visible “effective now” contract

**Priority:** P2.
External OpenRouter usage makes limit-update latency customer-visible. Show the
current balance, supplier cap/status, last synchronized time, and a degraded
state when OpenRouter rejects an update. On a credit grant, attempt synchronous
cap refresh with a short timeout, then durable worker retry; the ledger grant
must not fail merely because the supplier is unavailable.

#### F-22 — A leaked OpenRouter key lacks a clear self-service response

**Priority:** P1.
The key is intentionally portable and displayed to its owner, but the journey
needs an obvious “rotate key” action that explains the impact on Hermes,
OpenClaw, OpenCode, OpenWebUI, and external clients.

**Recommendation:** create the replacement, update managed services, validate
them, then revoke the old key; provide an emergency immediate-revoke option and
audit/notify both actions.

#### F-23 — The smallest default workspace conflicts with promoted features

**Priority:** P2.
The default 1 GiB machine cannot comfortably support every promoted managed
service; some features require 2 GiB. A new user can choose the cheapest tier,
then encounter disabled actions during the happy path.

**Recommendation:** mark 2 GiB as “recommended for AI tools,” show a capability
matrix before creation, preserve the lower-cost advanced choice, and quote the
maximum and typical hourly cost beside each tier.

#### F-24 — One default model is not valid for every AI workload

**Priority:** P2.
A single model selection is reused across agent/chat services, while the
OpenRouter catalogue includes modality-specific models such as TTS. A model
being available does not mean Hermes or OpenWebUI can use it as a chat model.

**Recommendation:** filter by required capability per service, explain why a
model is incompatible, permit per-service defaults, and keep the unrestricted
key available for arbitrary external models. Monitor removal/rate-limit status
of the platform default.

#### F-25 — Destructive-action copy must match real retention and recovery

**Priority:** P1.
Reset, workspace deletion, account deletion, archival purge, key rotation, and
managed-tool removal have different retained data. The current reusable dialogs
are a good foundation, but accuracy depends on resolving F-15 and backup policy.

**Recommendation:** maintain one tested lifecycle matrix used by API docs,
Persian confirmation copy, and tests. Include external effects (OpenRouter key,
Telegram records, certificates, DNS, firewall, backups), not only database rows.

#### F-26 — No customer-visible service health or incident communication

**Priority:** P2.
Customers can see operation failures but not whether Incus capacity,
OpenRouter, SMS, DNS/certificates, terminal, or managed dashboards are globally
degraded.

**Recommendation:** add a small in-product status surface and incident banner
fed by bounded operational checks; publish maintenance windows; avoid exposing
sensitive host metrics.

#### F-27 — Terms, privacy, acceptable use, and shared-credential risk are not a complete signup contract

**Priority:** P1 product/legal.
The product sells root containers, exposes internet services, stores phone/API
credentials, and shares platform Claude/Codex OAuth grants that root customers
can copy. These are material terms.

**Recommendation:** add versioned terms/privacy/acceptable-use consent,
retention and refund language, shared-kernel disclosure, shared-subscription
limits, abuse handling, and a record of consent. Obtain local legal review.

#### F-28 — “Never lose a byte” can overstate durability

**Priority:** P1 copy risk.
The phrase is accurate for powering off but not host/pool loss. Qualify it
wherever used: stopped workspaces retain disks; they are not currently an
off-host backup.

## UI, UX, accessibility, and Persian content

#### F-29 — Primary navigation is overcrowded, especially on mobile

**Priority:** P1 UX.
The customer shell has roughly eleven destinations plus admin. At narrower
widths labels disappear and the bottom navigation remains a horizontally
scrolling collection rather than a comprehensible hierarchy. Important items
can sit off-screen with weak affordance that more exist.

**Recommendation:** keep four or five primary mobile destinations and a
labelled “more” sheet; group machine-related destinations, AI, account/billing,
and help; preserve badges; validate at 320, 360, 390, 768, and desktop widths.

#### F-30 — Seven AI tabs do not scale as a flat tab strip

**Priority:** P1 UX.
Brand icons improved recognition, but seven peers with readiness badges are too
dense and mix different concepts: shared subscriptions, account API, agents,
and hosted web applications.

**Recommendation:** use a compact AI overview with service cards and categories
(API key, coding agents, web apps), then a secondary detail route. Keep service
state visible without forcing all names into one strip.

#### F-31 — Dialog accessibility is incomplete

**Priority:** P1 accessibility.
The reusable confirm/destructive/danger dialogs are inconsistent. They do not
all provide dialog semantics, labelled titles, initial focus, a focus trap,
Escape handling, or focus restoration.

**Recommendation:** replace them with one accessible dialog primitive; prevent
background interaction; announce destructive consequences; return focus to the
trigger; test keyboard-only and screen-reader flows.

#### F-32 — Tabs are visual links rather than accessible tabs

**Priority:** P2 accessibility.
Tab groups generally lack `tablist`/`tab` semantics, `aria-selected`, controlled
panel relationships, and arrow-key navigation.

**Recommendation:** either implement the complete tabs pattern or treat them as
ordinary page navigation with clear current-page semantics. Do not implement a
partial ARIA pattern.

#### F-33 — Async feedback is not reliably announced

**Priority:** P2 accessibility.
Toasts and operation-state changes do not consistently use `aria-live`,
`role=status`, or `role=alert`. A screen-reader user may receive no indication
that copying, saving, installing, or failing completed.

**Recommendation:** add polite and assertive live regions to the design system
and route all transient feedback through them.

#### F-34 — Icon-only controls are inconsistently named

**Priority:** P2 accessibility.
Copy, reveal, close, notification, sort, and delete controls often rely on an
SVG or visual tooltip. SVGs are not consistently hidden and buttons do not
consistently have an accessible name.

**Recommendation:** require `aria-label`/visible text for every icon-only
control, `aria-hidden=true` on decorative SVGs, and automated axe plus manual
keyboard tests.

#### F-35 — Form errors are not connected to their fields

**Priority:** P2 accessibility/UX.
Forms usually render a general note but do not consistently set
`aria-invalid`, `aria-describedby`, move focus to the first error, or retain
server-side values after failure.

**Recommendation:** standardize field-level validation, error summary, focus,
loading/submission state, and retry behaviour. Keep API codes as the source of
Persian wording.

#### F-36 — Some connection errors disappear immediately

**Priority:** P1 UX bug.
In connection toggles, the catch path writes an inline message and then an
unconditional page rerender replaces it. The customer sees an action fail with
little or no recoverable explanation.

**Recommendation:** return after rendering the error, or preserve the error in
page state across refresh. Add browser tests that force every connection API to
fail and assert the message/action remains visible.

#### F-37 — Navigation has async render races

**Priority:** P2 UX risk.
Routes start asynchronous page loaders without a shared abort/navigation token.
A slow request from the prior route can finish after navigation and replace the
new page.

**Recommendation:** use an `AbortController` per navigation or compare a route
generation before rendering. Abort page polling on route change and tab
visibility changes.

#### F-38 — API failures can leave pages blank or stale

**Priority:** P1 UX.
Some initial page loads use `Promise.all` without a local recovery boundary
(resources is one example). A rejected request can become an unhandled promise
and provide no retry action.

**Recommendation:** give every route a common loading/error/retry shell, retain
the last good data when safe, and log a correlation ID—not server prose—to the
support path.

#### F-39 — Polling is unnecessarily aggressive in hidden tabs

**Priority:** P2 performance/UX.
The shell repeatedly fetches operations and notifications, while feature pages
also poll. Hidden tabs and multiple open tabs continue generating work.

**Recommendation:** pause on `document.hidden`, use exponential backoff after
errors/idle periods, deduplicate in-flight calls, and later consider SSE for
operations/notifications. Measure before introducing WebSockets broadly.

#### F-40 — Mobile tables remain desktop tables in a scroller

**Priority:** P2 UX.
Horizontal scrolling prevents outright overflow but is cumbersome for admin
users, billing history, tickets, and technical addresses.

**Recommendation:** define priority columns, convert row actions to a bottom
sheet, use labelled cards for narrow screens, keep tables for larger widths,
and test long Persian names plus long Latin identifiers.

#### F-41 — Reduced-motion preference is not respected

**Priority:** P3 accessibility.
Pulses, spinners, transitions, and sheets have no unified
`prefers-reduced-motion` treatment.

**Recommendation:** disable nonessential motion globally for users requesting
it while keeping state changes perceivable.

#### F-42 — There is no skip link or efficient keyboard route past navigation

**Priority:** P3 accessibility.
The large sticky navigation precedes page content on every route.

**Recommendation:** add a visible-on-focus “skip to main content” link and a
stable main landmark with focus management after SPA navigation.

#### F-43 — Sort state and visual charts need nonvisual equivalents

**Priority:** P2 accessibility.
Sortable admin headers do not consistently expose `aria-sort`; CSS bar charts
primarily communicate through geometry/tooltips.

**Recommendation:** announce sort direction and provide a compact text/table
summary for each customer-facing chart.

#### F-44 — Customer-visible strings are scattered outside the catalogue

**Priority:** P2 clean UI architecture.
The stated rule is that customer text lives in `web/js/i18n.js`, but reusable UI
defaults, relative-time helpers, the landing page, and router titles contain
literal strings. `router.js` visibly appends the English word “Workspace” to
Persian page titles.

**Recommendation:** move every customer-visible literal to the catalogue and
add a static test for literals in page/UI modules. Change browser titles to the
Persian brand. Technical terms may remain Latin where the language policy
allows them.

#### F-45 — Design primitives exist, but are not yet one design system

**Priority:** P2.
There are multiple destructive-dialog implementations and pages still compose
loading, empty, error, copy, and secret states differently.

**Recommendation:** document and enforce one primitive for buttons, fields,
notes, dialogs, tabs, tables/cards, loading skeletons, empty states, recoverable
errors, copyable values, secret reveal, status pills, and operation progress.
Include RTL/LTR isolation and Persian/Latin numeral rules in component tests.

#### F-46 — Secrets are placed in the DOM even while visually masked

**Priority:** P2 security UX.
Some reveal/copy components retain plaintext in `data-*` attributes. This is
honest in source comments and convenient, but browser extensions or any future
XSS can read them immediately. The absence of CSP raises the consequence.

**Recommendation:** fetch a secret only after an explicit, recently
reauthenticated reveal; expire it from memory/DOM; never place it in hidden
markup; add CSP after inventorying required sources. This cannot protect a root
workspace owner from their own workspace credential, but it narrows dashboard
exposure.

#### F-47 — Disabled controls hide recovery paths

**Priority:** P2 UX.
Several unavailable actions are disabled with explanatory text nearby. Disabled
controls cannot receive normal keyboard focus and users may not connect the note
to the action.

**Recommendation:** where an action has a recoverable prerequisite, keep it
actionable and open the exact recovery step (add credit, power on, resize, add
key). Reserve disabled state for actions that truly cannot be attempted and tie
the explanation with `aria-describedby`.

#### F-48 — Warning/note spacing must be regression-tested geometrically

**Priority:** P3.
Global note margins were previously inconsistent near cards and fields. Static
CSS tests cannot prove the rendered gap.

**Recommendation:** define spacing through parent layout `gap` rather than
ad-hoc note margins and add headless-browser geometry assertions for each note
variant at desktop/mobile widths.

## Architecture and clean code

#### F-49 — Core modules are too large to preserve their own boundaries

**Priority:** P1 maintainability.
`control/mmd/app.py` is about 3,900 lines with roughly 100 routes;
`provisioner.py` exceeds 2,100 lines; `worker.py` exceeds 1,500 lines; the
translation catalogue and AI page are also very large. Their internal sections
are thoughtful, but size makes authorization, transaction ownership, and
lifecycle invariants hard to review.

**Recommendation:** incrementally split FastAPI routers and domain use-cases by
account, workspace, access, billing, AI, support, and admin. Split worker jobs
behind explicit transaction boundaries and privileged verbs into independently
validated handlers. Do not weaken the API/provisioner privilege boundary.

#### F-50 — Transaction ownership is implicit and inconsistent

**Priority:** P1.
Helpers such as `post_transaction`, SMS verification, notification routines,
and audit functions commit at different levels. A caller cannot safely compose
several domain mutations atomically, which directly causes F-02.

**Recommendation:** route/use-case layer owns commit/rollback; domain helpers
flush and return values. Name the few intentionally independent outbox
transactions. Add a short transaction-boundary section to `ARCHITECTURE.md`.

#### F-51 — Startup schema patching is not a migration system

**Priority:** P1 operations.
`SCHEMA_PATCHES` applies raw statements independently and logs/skips failures so
SQLite tests can boot. There is no ordered schema version, transactional
migration plan, downgrade, or hard production gate. The API can start with a
partially upgraded schema.

**Recommendation:** adopt Alembic or an equivalent versioned migration runner;
run it as a deployment step under an advisory lock; fail deployment on
production migration errors; keep SQLite-specific test setup separate.

#### F-52 — Workspace still contains legacy OpenRouter/Hermes credential fields

**Priority:** P2.
`OpenRouterAccount` is now the correct account-level authority, but migrated
workspace columns and cleanup code remain. Two conceptual homes invite a future
path to read or clear the wrong one.

**Recommendation:** complete a measured data migration, assert no live rows
need legacy fields, remove reads/writes, then drop columns in a versioned
migration. Preserve only service-specific Hermes installation state.

#### F-53 — Settings are stringly typed with asymmetric validation

**Priority:** P2.
Runtime settings are arbitrary string rows. Pricing has fallback parsing, while
other capacity/AI paths convert directly and may fail on malformed values.
Cross-field invariants are spread between endpoints and consumers.

**Recommendation:** define a typed registry with parser, range, unit, owner,
default, sensitivity, and restart/reconciliation effect. Validate the complete
candidate configuration before commit and keep an audit history/version.

#### F-54 — The worker’s single sequential loop couples unrelated products

**Priority:** P1 availability.
Operations, metering, AI reconciliation, managed-service installation, backups,
SMS, disk checks, settlement, lifecycle, and general reconciliation share one
loop. Long supplier/provisioner operations or a failure before later jobs delays
unrelated billing and notifications. One broad catch ends the rest of that tick.

**Recommendation:** give each job independent scheduling, timeout, error
boundary, last-success metric, and overlap lock. A small process supervisor or
separate systemd workers is preferable to introducing a large queue platform
prematurely.

#### F-55 — Provisioner concurrency is unbounded and not resource-scoped

**Priority:** P1 risk.
The privileged Unix-socket server starts a daemon thread per accepted request.
There is no bounded pool or per-workspace lock around conflicting Incus/filesystem
verbs.

**Recommendation:** bound concurrent handlers, serialize mutating verbs per
workspace, reject oversized/duplicate work, set end-to-end deadlines, and add
metrics for queue depth and active verbs.

#### F-56 — Read endpoints cause privileged mutations

**Priority:** P2 API design.
Service-port reservation can occur during state retrieval/backfill. A GET may
therefore allocate database ports and cause firewall reconciliation.

**Recommendation:** allocate reserved ports during workspace creation or an
explicit idempotent reconciliation job. Keep GET safe and side-effect free so
prefetching, monitoring, and retries cannot change host state.

#### F-57 — Managed-service state machines are duplicated

**Priority:** P2.
Hermes, OpenClaw, OpenCode, and OpenWebUI repeat enabled/installed/password/error
columns and branching, while differing subtly in routing readiness and memory
requirements—the source of F-07.

**Recommendation:** define a declarative service catalogue plus a shared state
machine for prerequisites, install, process health, route health, upgrade, and
removal. Keep service-specific adapters, secrets, and UI copy explicit; avoid a
generic abstraction that hides important differences.

#### F-58 — Application configuration and product catalogue can drift

**Priority:** P2.
Disk-size comments/catalogue values still reflect earlier 10 GiB assumptions,
while current workspace defaults total 14 GiB (6 GiB root + 8 GiB Docker).
Capability memory thresholds and model defaults appear in more than one layer.

**Recommendation:** establish one typed, API-exposed product catalogue consumed
by pricing, validation, admin configuration, and UI. Add invariants that every
off/on quote uses the actual persisted disk allocation.

#### F-59 — Root-capable customers make shared AI credentials a product-wide blast radius

**Priority:** P1 accepted risk.
Every workspace owner is root and can read copied platform Claude/Codex OAuth
grants. The repository documents that technical isolation cannot hide a
credential from root, but one abusive tenant can still trigger upstream account
suspension affecting everyone.

**Recommendation:** revisit supplier terms, separate credentials/accounts where
commercially possible, enforce policy and anomaly monitoring, make the risk
prominent before activation, and maintain a rapid rotate/revoke/re-sync runbook.
Do not claim technical secrecy that containers cannot provide.

#### F-60 — Threat modelling should cover tenant-data compromise, not only host escape

**Priority:** P2.
The restricted Incus certificate successfully limits host escalation, but an
API compromise can still act across customer containers within allowed verbs
and access the customer database. The current architecture language can be read
as stronger isolation than that.

**Recommendation:** publish an internal threat model for dashboard compromise,
worker compromise, provisioner compromise, malicious customer root, database
theft, supplier-key theft, and nginx/vhost attacks. State which customer data
each principal can reach and test negative capabilities.

## Operations, observability, and performance

#### F-61 — Dashboards exist without a complete alerting system

**Priority:** P1 operations.
Prometheus/Grafana coverage is broad and the worker watchdog is useful, but
there is no complete Alertmanager/rule/runbook path for API availability,
provisioner latency, database exhaustion, pool capacity, certificate expiry,
backup age, billing drift, supplier failures, or queue backlog.

**Recommendation:** define a small SLO set, actionable alerts with owners and
runbooks, inhibition/deduplication, and synthetic checks for customer-critical
paths. Alert on symptoms and sustained failure, not every transient exception.

#### F-62 — Prometheus history and capacity are not defined

**Priority:** P2.
Scraping is configured at 60 seconds, but retention, storage sizing, backup, and
cardinality budgets are not part of the documented operating model.

**Recommendation:** state retention and disk budget, monitor TSDB growth, bound
all labels, and decide whether operational history itself needs off-host
retention. Record dashboard compatibility with metric changes.

#### F-63 — The metrics endpoint performs substantial synchronous work

**Priority:** P2 performance risk.
An API request builds database aggregates and samples host/process state.
Prometheus therefore consumes API and database capacity and can time out during
the incidents it is meant to explain.

**Recommendation:** measure scrape duration/query plans; cache an asynchronously
built snapshot; separate expensive collectors; impose scrape timeout and
concurrency limits. Keep the endpoint private and fail individual collectors
without losing the whole scrape.

#### F-64 — Grafana uses a shared administrator credential

**Priority:** P1 security/operations.
The app surfaces one Grafana admin login to MMD administrators. Actions in
Grafana are not attributable to individual MMD admins, and removing an MMD admin
does not inherently revoke a credential they memorized.

**Recommendation:** use per-admin SSO/OAuth or proxy auth with least-privilege
viewer/editor roles; reserve local Grafana admin for break-glass; rotate and
audit it; keep embedded dashboards read-only.

#### F-65 — Metrics are in-process and worker persistence is snapshot-based

**Priority:** P2.
API counters reset on restart; worker metrics cross a process boundary via a
snapshot. Prometheus handles resets for rates, but short-lived failures and
snapshot corruption can be lost.

**Recommendation:** document reset semantics, export process start time and
snapshot age, validate atomic snapshot writes, and make dashboards distinguish
zero from missing/stale.

#### F-66 — No explicit database connection/capacity operating envelope

**Priority:** P2 risk.
The system is single-host and multiple services plus Grafana/Prometheus use its
database. The documentation does not state pool sizes, statement timeouts,
slow-query thresholds, or saturation alerts.

**Recommendation:** define pool/timeout settings, enable bounded slow-query
visibility, alert on connections/locks/transaction age, and load-test the
expected customer count plus admin dashboards.

#### F-67 — Public plain HTTP application addresses are a deliberate but costly limitation

**Priority:** P2 product decision.
The current direction intentionally supports HTTP rather than HTTPS on customer
application addresses. This avoids certificate complexity but creates browser
warnings, prevents secure-context APIs, causes mixed-content restrictions, and
blocks integrations that require HTTPS callbacks.

**Recommendation:** keep current HTTP behaviour truthful; later offer an
opt-in TLS hostname on standard port 443 through the gateway rather than
promising HTTPS on arbitrary `username:internal-port` addresses.

#### F-68 — Log/data retention and secret redaction need one policy

**Priority:** P1.
Identity and credentials can appear across PostgreSQL, SMS bodies, audit detail,
systemd journals, provisioner output, metrics snapshots, and backups. Controls
are currently local to each component.

**Recommendation:** inventory fields, classify them, redact at logging
boundaries, prohibit tokens/passwords in errors and metrics, set retention per
store, and test representative failures for leakage.

## Testing and quality engineering

#### F-69 — Fast tests are broad but production semantics are under-tested

**Priority:** P1.
SQLite tests cannot reproduce PostgreSQL row locking, isolation, partial indexes,
timezone behaviour, or production schema patches. Mocks cannot prove Incus,
nginx, nftables, systemd, certificate, or supplier behaviour.

**Recommendation:** keep the five-second suite, then add CI layers:

1. PostgreSQL integration tests for transactions/concurrency/migrations.
2. Contract tests for OpenRouter, Kavenegar, Incus, and provisioner IPC.
3. Disposable-workspace smoke tests on a quarantined host.
4. Headless-browser journey/accessibility tests.
5. Scheduled backup restore and certificate renewal drills.

Never run them against a customer workspace.

#### F-70 — Crash consistency is not systematically tested

**Priority:** P1.
Durable operations and unique ledger keys are good foundations, but there is no
general test matrix that kills the worker after each external action/commit and
then proves reconciliation converges exactly once.

**Recommendation:** add failpoints around key creation/revocation, billing,
workspace create/reset/delete, firewall publication, tool installation, SMS,
and backup upload. Assert repeated replay reaches the same final state.

#### F-71 — Browser geometry and accessibility need automated coverage

**Priority:** P2.
Static JS tests catch catalogue and structural rules, not clipped content,
overlapping tabs, focus loss, touch targets, contrast, or screen-reader names.

**Recommendation:** use the documented headless-Chromium harness for canonical
journeys and widths; add axe as a signal (not a substitute for manual testing);
capture screenshots on failure; assert no horizontal page overflow and minimum
touch-target size.

#### F-72 — Security tooling is absent from the routine suite

**Priority:** P2.
No routine dependency vulnerability scan, secret scan, Python static security
scan, shell analysis, or container/image SBOM is evident.

**Recommendation:** add pinned, low-noise checks (for example pip-audit,
Semgrep/Bandit rules chosen for this architecture, ShellCheck, gitleaks, and
image/SBOM scanning). Triage exceptions in code with reasons; do not make an
unowned wall of warnings.

#### F-73 — Coverage is not measured against critical invariants

**Priority:** P3.
A test count is useful but can hide untouched paths. Line percentage alone is
also insufficient.

**Recommendation:** measure branch coverage as a diagnostic, then maintain an
explicit invariant matrix: money, status transitions, deletion retention,
authorization, root-verb validation, secret non-disclosure, and Persian copy.

#### F-74 — API error recovery should be tested as a product contract

**Priority:** P2.
Stable error codes are strong, but tests should also prove each customer-visible
failure has a specific next action and that rerendering does not erase it.

**Recommendation:** map every endpoint code to Persian title, explanation, and
one of retry/add credit/power off/resize/add key/repair/contact support. Fail the
suite for unmapped codes and exercise representative browser failures.

## Documentation and copy drift

These are confirmed inconsistencies, not redesign suggestions:

| ID | Priority | Current inconsistency | Correction |
|---|---:|---|---|
| D-01 | P2 | `docs/BILLING.md` and the `UsageSample` model say raw samples live about 7 days; worker/changelog use 2 days. | Document the actual 2-day value or make retention one configuration constant referenced by docs/tests. |
| D-02 | P2 | `docs/OBSERVABILITY.md`/`METRICS.md` say five-minute collection; `prometheus.yml`, operations docs, and changelog use 60 seconds. | State 60-second Prometheus scrape separately from five-minute business metering. |
| D-03 | P3 | Observability prose mentions 51 metric families while the current catalogue says 67. | Generate/count this statement from the catalogue or avoid a brittle number. |
| D-04 | P3 | README estimates roughly 13,000 lines; current core source is materially larger. | Remove the line-count claim; it adds no customer value. |
| D-05 | P2 | README architecture describes vhosts mainly as Hermes, despite multiple managed tools and published apps. | Update the diagram and hostname lifecycle. |
| D-06 | P2 | README says SSH/RDP ports live for the account; deletion releases them because they belong to the workspace. | Say “life of the workspace”; distinguish reset from delete/recreate. |
| D-07 | P2 | API reset example still uses `owner@example.com` although email was removed. | Replace it with a username and audit all examples for the phone-only identity model. |
| D-08 | P2 | API/admin credit prose still describes an active “Hermes key.” | Use account-level OpenRouter key terminology and state that Hermes is a consumer. |
| D-09 | P3 | `i18n.js`/test comments refer to five AI tabs although there are seven. | Update comments or describe the category without a count. |
| D-10 | P3 | CSS/admin comments contain stale section counts. | Remove fragile counts from comments. |
| D-11 | P2 | Pricing/catalogue commentary still refers to a 10 GiB machine while persisted defaults total 14 GiB. | Reconcile the catalogue, comments, quotes, and admin presentation. |
| D-12 | P3 | `requirements.txt` still includes `email-validator` after email removal. | Confirm no transitive use, remove it, and lock dependencies. |
| D-13 | P2 | API documentation does not fully present all managed-AI endpoints and states introduced in 1.7. | Regenerate or systematically compare routes to docs. |
| D-14 | P2 | “Grafana/Prometheus are not reachable from the internet” is ambiguous when nginx exposes authenticated Grafana paths. | Say exactly which listener/path is public, which auth layers protect it, and that Prometheus remains private. |
| D-15 | P2 | Current deletion comments simultaneously claim audit/SMS survival and full erasure in different paths. | Link every statement to the agreed retention matrix from F-15. |
| D-16 | P3 | The passing test run emits FastAPI startup-event and TestClient/httpx deprecation warnings. | Move startup work to a FastAPI lifespan handler and plan the supported TestClient/httpx migration before dependency upgrades turn warnings into failures. |

Historical entries in `DECISIONS.md` should not be rewritten merely because
they mention superseded email or Hermes-era behaviour. Add a “superseded by”
note or a new decision where needed so the history remains useful.

## What is already strong and should be preserved

- The public API/root provisioner split, restricted Incus certificate, Unix
  peer-credential check, fixed verb allowlist, and systemd hardening are sound
  architectural anchors.
- Integer micro-Toman accounting and one pricing authority prevent an entire
  class of drift bugs.
- Unique charge identities, durable operations, reconciliation, and explicit
  state machines show the right reliability mindset, even though transaction
  boundaries need strengthening.
- Optional workspace/account-level OpenRouter separation is the correct product
  model.
- Stable API codes plus Persian UI translation is the right language boundary.
- The append-only decision log captures real operational lessons exceptionally
  well.
- The fast test suite is unusually broad and makes safe iteration possible.
- Service credentials loaded into worker-only systemd credential mounts are
  materially better than putting supplier management keys in the API process.
- `dns.mode: none`, protected terminal sizing, nftables ownership, project
  ceilings, and file-manager privilege routing are exactly the kind of hard-won
  invariants that a refactor must retain.

## Implementation disposition — 2026-09-05

This is the authoritative status ledger for this review. The findings above
preserve evidence as it existed at review time; “Confirmed” there does not mean
the finding remains unfixed after this implementation programme.

The owner explicitly chose three current constraints: payments remain manual,
database backups may continue through Telegram without added encryption, and
published customer applications remain HTTP-only. Fundamental/high-regression
changes and findings needing an owner, legal, retention, SLO, or pricing
decision are kept for a later programme rather than mixed into this low-risk
release.

### Implemented in the current working release

| Findings | Result |
|---|---|
| F-01 | Ledger rows and balance changes now share one transaction, and the balance moves through an atomic SQL increment instead of a process-local read/modify/write. Duplicate charge rejection happens before the balance changes. |
| F-02 | Claude, Codex, and account-level OpenRouter charges use `commit=False` so each charge and the usage checkpoint proving what was billed commit as one fact. Crash-window regression tests protect the boundary. |
| F-07 | OpenCode and Open WebUI now include explicit vhost-ready state; installation alone cannot make their launcher ready. |
| F-09, F-10, F-16 | Replacement phones are SMS-verified; credential/status changes advance a session version; rejected/suspended accounts cannot obtain a working login cookie. |
| F-19 | Accounts without compute see two equal first-run paths: portable OpenRouter API or an optional development machine. |
| F-21 | The OpenRouter tab separates local credit from the last supplier-confirmed cap, shows synchronization time/failure, and polls the durable five-second retry. The management key intentionally remains worker-only instead of adding a supplier call to the public API. |
| F-25 | Reset, workspace deletion, and account deletion use impact-specific confirmations separating preserved data from permanent loss. |
| F-28 | Public durability wording states the real single-host and backup boundaries instead of making an absolute promise. |
| F-29 | Mobile navigation keeps four primary destinations visible and moves secondary destinations into an accessible “more” sheet. |
| F-31 through F-34 | Dialog focus/trapping, tab semantics, live async feedback, decorative SVG handling, and accessible control names are standardized. |
| F-36 through F-38 | Connection failures remain visible, stale async routes cannot overwrite a newer route, and route failures render a shared retry/recovery state. |
| F-39 | Global operation/notification polling is single-flight, preserves last-good state, runs at three seconds only during active work, slows to ten seconds while idle, backs failures off to one minute, and pauses in hidden tabs. Cross-tab coordination or SSE is deliberately excluded until measurements justify more architecture. |
| F-41 through F-44 | Reduced motion, skip navigation, `aria-sort`, chart data tables, Persian browser titles, and catalogue ownership of customer text are enforced. |
| F-47 | Recoverable machine, AI, SSH, RDP, and backup prerequisites remain actionable and lead directly to credit, power, resources, OpenRouter, key entry, or Telegram configuration. Only truly impossible/current-state actions remain disabled. |

### Partially implemented; bounded follow-up remains

| Finding | Completed now | Remaining boundary |
|---|---|---|
| F-35 | Shared errors set `aria-invalid`/`aria-describedby`, retain values, and focus the invalid field on authentication, account security, support-ticket creation/reply, and admin profile editing. | Migrate configuration and lower-frequency admin forms when those pages are next changed. |
| F-40 | Billing transactions, customer tickets, and the admin customer list become labelled row cards at phone widths while retaining desktop tables. | Convert lower-priority technical/admin tables only when real mobile use justifies each layout. |
| F-45 | `docs/DESIGN-SYSTEM.md` and shared dialog, note, secret, status, empty, recovery, validation, and mobile-card table patterns define the contract. | Remove remaining page-local compositions when those pages are next changed. |
| F-50 | Financial callers can now choose explicit transaction ownership, and usage metering uses it to commit charge plus checkpoint together. | Other helpers still mix commit-owning and caller-owned styles; complete this only during bounded module extraction. |
| F-65 | Process uptime was already exported; worker snapshots are atomic and now expose age so stale counters differ from zero. | In-process API counters still reset by design; changing that needs an observability architecture decision. |
| F-71 | Structural tests cover focus, names, tabs, charts, motion, mobile navigation, and recovery. | Real geometry/contrast needs a pinned browser/axe CI environment; no browser runtime is installed in the repository test environment. |
| F-73 | `docs/TEST-INVARIANTS.md` maps critical promises to their primary tests. | Add branch coverage after choosing a CI coverage budget. |
| F-74 | Stable translations and representative recovery actions are enforced for high-risk journeys. | Exhaustive endpoint-to-action coverage should grow with F-35, not through a generic support assertion. |

### Deliberately retained by owner decision

| Finding | Current decision |
|---|---|
| F-03 | Keep the existing unencrypted Telegram database-backup transport; its disclosed risk is accepted for now. |
| F-20 | Keep credit/payment operator-mediated and manual; do not add a payment gateway. |
| F-67 | Keep published application addresses HTTP-only; do not reintroduce an HTTPS promise the routing model cannot meet. |

### Active review backlog — 47 findings

This is the only list to use when selecting more work. The long finding text
above is historical evidence; completed and owner-closed items are excluded
from this shrinking backlog.

| State | Findings |
|---|---|
| Partially implemented | F-35, F-40, F-45, F-50, F-65, F-71, F-73, F-74 |
| Deferred: fundamental/high-risk | F-04, F-05, F-06, F-08, F-11 through F-14, F-18, F-22, F-46, F-48, F-49, F-51 through F-60, F-64, F-66, F-69, F-70, F-72 |
| Awaiting product/operations decision | F-15, F-17, F-23, F-24, F-26, F-27, F-30, F-61 through F-63, F-68 |

When one of these findings is completed, remove it from this table, increment
the implemented count above, and add its result to the implementation table.

### Deferred: fundamental, high-cost, or high-regression work

| Findings | Why deferred |
|---|---|
| F-05, F-06, F-11 | Correct fixes change admission/allocation locking, active-operation uniqueness, or SMS-code consumption semantics and need an isolated PostgreSQL integration environment. |
| F-04 | Off-host workspace recovery changes storage, transfer, retention, restore, and cost architecture. |
| F-08, F-12, F-13 | Abuse control, password recovery, and bootstrap changes can lock out real users/admins unless proxy trust, SMS budget, and recovery operations are designed together. |
| F-14, F-18 | Reproducible supply-chain installation and wildcard certificate automation require staged infrastructure and rollback testing. |
| F-22, F-46 | Key rotation and just-in-time secret reveal affect external clients and every managed consumer; a partial implementation would create false confidence. |
| F-48 | Geometric browser gates require a pinned headless-browser baseline. Existing structural spacing tests remain. |
| F-49, F-51 through F-60 | These are control-plane boundary, migration, state-machine, privilege, and threat-model redesigns—the category excluded from this low-risk programme. F-50 has a bounded partial improvement above. |
| F-64, F-66 | Per-admin Grafana identity and a database capacity envelope require identity/infrastructure and measured-load work. |
| F-69, F-70, F-72 | PostgreSQL/Incus crash laboratories and security/SBOM tooling require owned CI infrastructure and quarantined disposable workspaces. |

### Deferred pending a concrete product or operating decision

| Findings | Decision required before implementation |
|---|---|
| F-15, F-68 | Legal retention/anonymisation periods for ledger, audit, SMS, support, journals, metrics, and backups. |
| F-17 | Whether signup feedback or resistance to customer-membership enumeration has priority, plus the abuse budget. |
| F-23 | Whether the promoted default should cost more at 2 GiB, and which AI capabilities each sellable tier promises. |
| F-24, F-30 | Per-workload model policy and the next AI information hierarchy as the catalogue grows. |
| F-26, F-61 | Incident communication, SLOs, alert recipients/owners, escalation, inhibition, and runbook policy. |
| F-27 | Terms, privacy, acceptable-use, refund, shared-kernel, and shared-subscription language needs product/legal approval. |
| F-62 | Prometheus retention duration, TSDB disk budget, and whether monitoring history is backed up off-host. |
| F-63 | Measure scrape cost and query plans first; snapshot/caching work is unjustified until the operating envelope is known. |

### Documentation drift disposition

| Items | Status |
|---|---|
| D-01 through D-14 | Implemented: retention/scrape wording, brittle counts, architecture/address lifecycle, phone-only examples, OpenRouter terminology, disk totals, removed dependency, API coverage, and Grafana exposure were corrected. |
| D-15 | Deferred with F-15 until the owner approves one retention matrix; contradictory absolute-erasure wording was removed where behavior is unambiguous. |
| D-16 | Partially implemented: FastAPI startup uses lifespan. One upstream TestClient/httpx warning remains for controlled dependency-upgrade work in F-14/F-72. |

### Additional improvements discovered after the review

These changes arose from production-journey review after the original finding
catalogue was frozen, so they do not receive retroactive finding numbers:

| Area | Result |
|---|---|
| SMS recipient coverage | Removed the temporary two-number delivery allowlist. Every valid Iranian mobile number can now receive queued messages; phone validation, per-message preferences, retries, and code abuse controls remain. Historical `skipped` trial rows are intentionally retained and are not replayed. |
| Credit-change SMS | Replaced unconditional grant messages and the global cumulative-spend milestone with a per-customer step `n`, configured on Console → SMS. The worker sends one optional message with direction and new balance whenever `floor(balance / n)` changes. First deployment sight and step edits establish a silent baseline. The user-row lock, band update, and outbox insert prevent configuration races and duplicate delivery. |
| Secondary recovery UX | Support and admin-profile validation identify and focus the exact field. SSH, RDP, and manual backup prerequisites remain keyboard-accessible and lead directly to the missing key, power, memory, or Telegram configuration instead of disappearing behind disabled buttons. |
| Polling efficiency | The global operation/notification refresh is single-flight, preserves last-known-good state, slows down while idle, backs off transient failures, pauses in hidden tabs, and resumes immediately when visible. |
| Priority mobile tables | Billing history, customer tickets, and the admin customer list render as labelled cards at phone widths without changing the desktop table or removing row actions. |
| Verification | The current complete suite passes 622 backend tests plus every frontend and static check. Production deployment established baselines for all 20 approved accounts and queued zero false `credit_step` messages. |

Every implemented batch passed `bash tests/run.sh` and was deployed API-first
with a health barrier before restarting the worker, provisioner, and vhost
timer. No customer workspace was used for verification.

## Deferred implementation programme

This sequence now covers only unresolved work from the disposition above. It
does not reopen the owner's decisions on manual payment, Telegram backup
transport, or HTTP-only application publication.

### Phase 1 — financial and destructive safety

Implement F-05, F-06, and F-11 with PostgreSQL concurrency tests, and finish
the remaining transaction-ownership cleanup in F-50 only where those changes
require it. Keep F-01/F-02 invariants covered while doing so. Then define the
lifecycle retention matrix (F-15) so future deletion work has a stable contract.

### Phase 2 — recovery and secrets

Prove database restore through the owner-approved Telegram path, evaluate
off-host workspace backup (F-04), centralize secret/redaction policy (F-68), add
OpenRouter rotation (F-22), and replace shared Grafana administration (F-64).

### Phase 3 — authentication hardening

Implement abuse limiting, forgotten password, safe admin bootstrap, SMS-code
concurrency, and phone-enumeration changes (F-08, F-11 through F-13, F-17). Add
the legal/product contract in F-27 alongside retention work.

### Phase 4 — truthful managed services

Pin installers, expose versions, scale certificate operations, and consolidate
service state machines (F-14, F-18, F-24, F-57). A launcher should not offer
“open” until a synthetic request reaches the intended service.

### Phase 5 — customer journey and mobile UX

Add safe key rotation and settle the default tier and AI information hierarchy
(F-22 through F-24 and F-30). Continue secondary-form and lower-priority mobile
table work only alongside changes to those pages (F-35, F-40, F-47).

### Phase 6 — accessibility and design system

Finish secondary-form migration and just-in-time secret reveal (F-35, F-45,
F-46), then validate with keyboard, screen reader, and rendered geometry
(F-48, F-71)—not structural tests only.

### Phase 7 — modularity and operations maturity

Introduce versioned migrations, split monoliths incrementally, isolate worker
jobs, bound provisioner concurrency, eliminate read-side mutations, establish
alerts/SLOs, and add layered integration/security tests (F-49, F-51 through
F-63, F-69 through F-74).

## Suggested acceptance gates for future releases

A release should not be called ready solely because `tests/run.sh` passes.
For changes in the affected domains, require:

1. `balance_micro == SUM(credit_transactions.amount_micro)` after concurrent
   grants, charges, duplicate retries, and injected crashes.
2. No two active workspace operations violate the capacity or lifecycle
   invariant under parallel requests.
3. Reset/delete/recreate results match the documented retained/deleted matrix,
   including supplier keys, ports, DNS, certificates, SMS, audit, and backups.
4. Every managed launcher is reachable through its displayed address before it
   says “ready.”
5. A full database backup can be restored in isolation and its age is alerted.
6. Signup, approval, OpenRouter-only use, workspace creation, power, all
   connection methods, managed-service install, low-credit recovery, reset,
   workspace deletion, and account deletion pass as browser journeys at desktop
   and mobile widths.
7. Keyboard-only navigation completes every destructive dialog and primary
   journey; automated accessibility checks have no serious/critical findings.
8. No customer-facing English prose or uncatalogued Persian literal is added.
9. No secret appears in logs, errors, metrics, HTML before reveal, or outside
   the owner-approved Telegram backup channel.
10. Documentation and the deployed `/opt/mmd` revision identify the same
    release, schema version, configuration catalogue, and operational behaviour.

## Decision checklist for the product owner

Before selecting implementation items, decide these product policies because
code cannot resolve them correctly by inference:

- Required RPO/RTO and whether MMD or the customer owns workspace backup.
- Legal retention for ledger, audit, SMS delivery, support, metrics, logs, and
  Telegram backups after account deletion.
- Whether pending users may enter a limited console and use any account-level
  feature before approval.
- Whether shared Claude/Codex credentials comply with supplier terms and the
  acceptable-use model.
- Whether 1 GiB remains the default or 2 GiB becomes the recommended first
  machine.
- Which services are first-class products versus optional integrations, which
  determines navigation hierarchy and support expectations.

Once those are answered, the implementation phases above can be converted into
small, testable batches without reopening foundational decisions during each
feature.
