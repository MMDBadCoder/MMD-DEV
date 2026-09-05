# MMD-DEV 1.8 — full project review

**Review date:** 2026-09-05
**Reviewed revision:** `83ef3e3` (`v1.8.0`, `main`)
**Method:** the running production host was inspected alongside the source.
Every finding below carries the command output or code location it came from.
Nothing was changed during this review.

---

## 1. Where 1.8 stands

1.8 is a genuinely healthier release than 1.7. The 1.7 review listed 74
findings; a large share of the P0/P1 set is now closed, and I verified the
important ones against the code rather than the changelog:

| 1.7 finding | State in 1.8 | Evidence |
|---|---|---|
| F-01 lost balance updates | **Fixed** | `service.py` issues `balance_micro = balance_micro + :delta` in SQL |
| F-02 double AI charges | **Fixed** | both metering paths pass `commit=False`; charge and checkpoint share a transaction |
| F-10 sessions survive password change | **Fixed** | `User.session_version` compared in `current_user` |
| F-16 login before status check | **Fixed** | `_ensure_login_allowed()` in `login()` |
| F-09 phone change unverified | **Fixed** | one-time code required for the new number |
| F-07 managed hosts land on homepage | **Fixed** | readiness waits for the exact vhost |
| F-05/F-06 races | **Partly fixed** | `with_for_update()` in two places — but see §5.1, it is untested |

Health at the time of review: **624 tests pass**, all five services active, no
errors in any service log in the past hour, host disk 75%, ZFS pool 49%.

### Implementation update — 2026-09-05

The concrete, low-risk findings from this review have now been implemented and
deployed. The suite has grown from 624 to **631 backend tests**, with all browser
and static checks passing.

| Finding | Current state |
|---|---|
| Missing password recovery / legacy phones (§2.2) | **Resolved safely.** SMS recovery is purpose-scoped, single-use, enumeration-safe and revokes old sessions. Legacy users are prompted to verify a phone; identity data was not invented by an admin backfill. |
| Audit log has no UI (§2.3) | **Resolved.** Searchable, paginated admin page with actor, action, target and detail. |
| Inconsistent machine noun (§2.5) | **Resolved and guarded.** Customer copy uses “machine”; the physical host is always “host server.” |
| Dead supplier secrets (§3.1/§4.4) | **Resolved, with correction.** The five superseded OpenRouter key/limit fields were migrated and dropped. Active Hermes installation, dashboard and Telegram state was correctly retained. |
| Mutating GETs / unused privileged storage GET (§3.3) | **Resolved.** Ticket read state has explicit POST endpoints and the unused storage handler was deleted. |
| Dead translation catalogue (§4.3) | **Resolved.** 94 confirmed-unused entries were removed and an unreferenced-key test now prevents recurrence. `/api/health` is not dead: deployment uses it as the schema/startup barrier. |
| Accidental Prometheus retention (§6.2) | **Resolved.** Retention is explicitly 15 days. Directory-size integration with the privileged pool guard remains a separate design item. |
| Silent state adoption (§6.3) | **Resolved.** Every adoption writes an audit event and increments `mmd_workspace_state_adoptions_total`. |
| Incomplete background announcements (§7) | **Resolved.** Operations, new notifications, refresh failure and recovery all use polite live regions. |
| Expired-session polling (found during deployment verification) | **Resolved.** A 401 clears local session state, stops the active pass and routes the stale tab to sign-in. |

The detailed sections below remain as review evidence. The final section is the
short active list; completed work is no longer repeated there.

---

## 2. Product

### 2.1 The product is far wider than its usage — **high value, low cost to act on**

Live counts:

```
accounts 21   approved 21   workspaces 14   running 2   tickets 16
AI services enabled:  hermes 2   openclaw 1   opencode 1   openwebui 1
```

Seven AI tabs are built, documented, tested, priced and maintained. **At most
two customers use any one of them, and three of the seven have exactly one
user each.** Meanwhile 12 of 14 machines are switched off at any moment.

This is the single most important thing in this review. It is not a bug; it is
a signal that engineering effort and product surface have decoupled from what
customers do. Before building anything new, it is worth learning why 12
machines sit idle and why the agents are unused — whether it is price,
discoverability, or that customers wanted a server rather than an agent.

**Suggested:** instrument activation properly (what fraction of approved
accounts ever power on, ever open a terminal, ever enable an agent), and be
willing to retire a service that nobody adopts. Four of the seven could be
collapsed into one "AI tools" page with a chooser.

### 2.2 There is no password recovery, and 7 of 21 accounts cannot use SMS — **resolved**

Confirmed by grep: **no forgotten-password endpoint or page exists.** Phone is
now the sole contact identity and a sign-in credential, so the intended
recovery route is an SMS code.

But 7 approved accounts have `phone IS NULL`:

```
alishazaee, momtahen, imohamadreza7, matinyousefi,
karimiehsan901, seyfoori-amir-h, alireza-sm      (created 2026-08-24 … 08-27)
```

They predate mandatory phone and were never backfilled. For them:

* password sign-in works;
* SMS sign-in is impossible;
* every SMS notification is silently skipped — the worker log shows
  `sms spend_milestone not queued for karimiehsan901: invalid phone number`;
* **if they forget their password they are permanently locked out**, because
  there is no recovery path at all and no second identity.

That is a third of the customer base one forgotten password away from a manual
database intervention.

**Suggested:** (a) prompt those seven for a phone at next sign-in and verify it;
(b) build the forgotten-password journey on the existing `smscode` module,
which already has hashing, expiry, attempt limits and rate limiting — the
mechanism is done, only the journey is missing.

### 2.3 The audit log is written but cannot be read — **resolved**

`audit_log` holds **637 rows across 55 action types** and `/api/admin/activity`
exists to serve it — but no admin page fetches it. `/console/activity` is the
*customer's* own history and calls `/api/activity`.

An operator investigating "who deleted this workspace" or "who granted this
credit" has to open psql. For a platform handling other people's money and
machines, the audit trail should be reachable by the person accountable for it.

**Suggested:** one admin page over the existing endpoint. Filters by actor,
action and target. Low cost — the data and API already exist.

### 2.4 Support has no response-time commitment and no alert

Two tickets are open; the oldest has been waiting **4 days 17 hours**. The
metric `mmd_ticket_oldest_open_seconds` is exported and on a dashboard, but
nothing alerts on it (§6.1), so nothing tells the operator except opening the
page.

**Suggested:** state a first-response target in the support UI, and alert when
the oldest open ticket crosses it.

### 2.5 One concept, three names — **resolved**

The same thing is called three different things in customer-facing Persian:

| Term | Occurrences in `i18n.js` |
|---|---|
| ماشین | 167 |
| فضا | 55 |
| فضای کاری | 33 |

The landing page promises a **فضای کاری** (`landing.step.workspace.title`), the
success notification says **فضای کاری شما آماده است**, and the console
navigation then calls it **ماشین**. `سرور` appears 23 times, sometimes meaning
the customer's machine and sometimes the host it runs on
(`blocked.capacity_memory`: "حافظهٔ آزاد کافی روی سرور نیست" — that is the
*host*, but a customer reads it as *their* server).

**Suggested:** pick one customer-facing noun — **ماشین** is already dominant and
matches the nav — and use it everywhere. Reserve **سرور میزبان** for the host,
and never bare **سرور**. This is a find-and-replace with a test, not a redesign.

---

## 3. Security

### 3.1 Dead columns still hold live supplier keys — **resolved**

The `hermes_*` columns on `workspaces` were superseded by
`openrouter_accounts` in 1.7. Nothing reads them any more. They still contain
production secrets:

```
workspaces with hermes_key            : 2   (identical to the live key in openrouter_accounts)
workspaces with hermes_dash_password  : 4
```

So the live OpenRouter key exists in two places, one of which has no owner, no
rotation path, and no code that would ever clear it. Every database dump
therefore carries a second copy — and by explicit product decision those dumps
go to Telegram unencrypted.

**Suggested:** null the 16 legacy `hermes_*` columns in one migration, then drop
them. Confirm no read path first (`grep` shows only `db.py` patch statements).
This is a ten-minute change that measurably shrinks the blast radius of the
backup decision you have already made deliberately.

### 3.2 The metrics token and Grafana password are a shared operator credential

Grafana has one `admin` account whose password is shown to every administrator
on the overview page. There is no per-operator identity, so a Grafana action
cannot be attributed, and removing one administrator's access means rotating a
credential everyone uses.

**Suggested:** acceptable at one or two operators; revisit before a third.
Worth writing down as a known limit rather than discovering it during an
offboarding.

### 3.3 Read endpoints that mutate or reach into the privileged path — **resolved where unsafe**

Seven `GET` handlers either commit or call the root provisioner:

```
list_files, read_file, download_file, download_archive, admin_storage
                                             → call_provisioner
get_ticket, admin_get_ticket                 → db.commit()
```

The file ones are defensible: the provisioner is the only route into a
workspace. `get_ticket` committing on GET means any prefetch or crawler marks a
ticket read. `admin_storage` calls the privileged provisioner on a GET **and
has no caller left in the SPA** (§4.3).

**Suggested:** move the read-marking out of `get_ticket` into an explicit POST
the page already makes, and delete `admin_storage`.

---

## 4. Architecture and clean code

### 4.1 `app.py` is 3,972 lines and owns everything

```
lines 3972   routes 101   functions 146   pydantic models 32   db.commit() calls 63
```

It contains authentication, the metrics exporter, billing endpoints, file
transfer, the terminal websocket, admin configuration and the Prometheus text
renderer. The longest functions are `_usage_metrics` (102 lines) and
`_operations_metrics` (91) — the exporter alone is roughly 300 lines inside the
web application.

63 separate `db.commit()` calls mean transaction ownership is decided
per-handler rather than by a boundary. That is exactly the condition that
produced F-02, and it is still the shape of the file.

**Suggested (incremental, low risk):** lift the exporter into
`control/mmd/exporter.py` first — it is self-contained, has its own tests, and
is ~300 lines out immediately. Then the admin routes. Splitting authentication
or billing is a larger change and should wait.

### 4.2 The exporter is N+1 and shells out, on a 60-second timer

```python
for user in db.scalars(select(User)):
    account = db.get(CreditAccount, user.id)
    router  = db.get(OpenRouterAccount, user.id)
    ws      = db.scalar(select(Workspace).where(Workspace.user_id == user.id))
```

Roughly four queries per user per scrape, plus a per-workspace port count, plus
**eight `systemctl` subprocess spawns** for process metrics. Measured at
**0.108s** today with 21 users — genuinely fine. It scales linearly: at 500
customers this is ~2,000 queries and 8 spawns every minute inside the
internet-facing process.

**Suggested:** three grouped queries instead of a loop, and cache the
`systemctl` PID lookups for the scrape interval. Not urgent; note it before the
customer count changes by an order of magnitude.

### 4.3 Dead code that is still routed — **resolved or classified**

Confirmed unreferenced by the SPA and by any probe:

```
/api/admin/storage      (also calls the provisioner — §3.3)
/api/admin/activity     (the audit log — has no UI, §2.3)
/api/admin/operations
/api/health             (no monitoring probe references it)
```

`/api/admin/activity` should gain a UI rather than be deleted. The other three
are either dead or should be wired to something.

**94 translation keys were also unreferenced** — verified after accounting for
concatenated *and* template-literal key construction. They cluster around the
pages whose charts moved to Grafana (`adm.storage.*`, `adm.mon.*`, `adm.obs.*`,
`adm.win.*`) and the two onboarding steps that were removed
(`onboarding.ssh`, `onboarding.publish`). That is ~8% of a 1,264-key catalogue
carried as dead weight, and it makes "is this string used?" unanswerable by
eye.

**Suggested:** a test that fails on an unreferenced key, then delete the 99.

### 4.4 Legacy schema has not been retired — **resolved with corrected scope**

The original review over-counted this finding: eleven `hermes_*` columns are
active service state and must remain. The five genuinely superseded key, hash,
usage and limit fields have now been migrated to `openrouter_accounts` where
needed and dropped. Fresh installs no longer create them.

**Suggested:** one migration that clears and drops them, and remove the
corresponding patch lines.

---

## 5. Testing

### 5.1 The new row locks are completely unexercised

1.8 added `with_for_update()` in two places (`app.py:959`, `worker.py:1345`).
Every test runs on SQLite. Compiled for each dialect:

```
PostgreSQL : ... WHERE users.id = %(id_1)s FOR UPDATE
SQLite     : ... WHERE users.id = ?
```

**`FOR UPDATE` compiles to nothing on SQLite.** The tests pass identically
whether the lock is present or absent, so the fix for the concurrency findings
is asserted by no test at all. The same is true of the atomic balance
increment: SQLite serialises writes, so the race it prevents cannot occur in
the test environment.

I checked the weaker claim too, and it is *not* true: the idempotency unique
constraints **are** created in the SQLite test schema, because they are
declared in `__table_args__` rather than only in `SCHEMA_PATCHES`. Duplicate
charge protection is genuinely tested. Locking is not.

**Suggested:** one PostgreSQL-backed test module — a container or a test
database on the host — running two concurrent transactions and asserting
`balance == sum(ledger)`. This is the highest-value test the project does not
have, because it is the only one that can fail when the financial invariant
breaks.

### 5.2 A third of the suite asserts on source text

35 test files read source as a string, with **225 assertions** of the form
`assert "..." in src` or `assert.match(src, /.../)`.

These are cheap and they do catch real drift — several caught my own mistakes
during this session. But they assert that code *looks* a certain way, not that
it *behaves* a certain way. They pass when a feature is deleted and its comment
survives, and they fail on a rename that changed nothing.

**Suggested:** keep them for documentation-drift guards, where they are
genuinely the right tool. Replace them with behavioural tests wherever they
stand in for one — particularly around billing and provisioning.

---

## 6. Operations and observability

### 6.1 Dashboards exist; alerting does not

`observability/grafana/provisioning/` contains only `dashboards` and
`datasources`. No alert rules, no Alertmanager, no `rule_files` in
`prometheus.yml`.

Every failure the platform can detect is therefore visible **only if someone
opens the page**. The exceptions are the SMS paths I know are wired: worker
stall, backup failure and pool-low. Everything else — 5xx rate, operation
backlog, certificate expiry, ticket age, a workspace stuck in `error` — is
silent.

**Suggested:** six Grafana alert rules routed to the existing SMS path, which
already exists and is proven: 5xx rate, worker heartbeat age, backup age,
pool free, oldest open ticket, operation backlog.

### 6.2 Prometheus retention is undefined — **retention resolved**

`observability/prometheus.default` sets no `--storage.tsdb.retention.*` flag,
so retention is the 15-day default. The TSDB is **5.5 MB** today.

At 60-second scraping with per-customer labels, series count grows with the
customer base. 15 days is a reasonable default, but it is currently accidental
rather than chosen, and nothing watches the directory's size.

**Suggested:** set retention explicitly, and add the TSDB path to the disk
guard's awareness.

### 6.3 A workspace silently changed state — **resolved**

The worker log shows:

```
ws-14 was error; Incus reports running - adopting that
```

The reconciler recovered correctly — that is the system working. But a
workspace entering `error` and being adopted back produced no notification, no
audit row and no alert. Nobody knows it happened unless they read the journal.

**Suggested:** audit-log state adoptions, and count them as a metric.

---

## 7. Accessibility and interface

1.8 did real work here — `aria-invalid`, `aria-describedby`, a skip link, tab
semantics, reduced-motion support and responsive row-cards all landed. Two
things are worth noting as incomplete rather than absent:

* **`aria-live` appears once.** Toasts and async results are announced in one
  place; operation progress, background failures and the polling status are
  not. A screen-reader user gets the same silence a hidden tab does.
* **Navigation carries 11 primary and 11 admin destinations**, and the AI page
  is a flat strip of **seven tabs**. Mobile now has a More sheet, which helps.
  The AI strip does not scale and is the place a chooser would help most
  (§2.1).

---

## 8. What should be preserved

Worth saying explicitly, because a review that only lists problems misleads:

* **The privilege split.** `mmd-api` cannot reach the host; the provisioner is
  root with `IPAddressDeny=any`, a fixed verb allowlist, `SO_PEERCRED` and
  validated `idx` bounds. I tried to find a hole in the verb dispatch and did
  not.
* **Integer micro-Toman accounting with idempotency keys**, now with an atomic
  SQL increment. The invariant holds live: balance equals ledger sum for all 21
  accounts.
* **The comments.** This codebase explains *why* far better than most. The
  reasoning about counter-vs-gauge, UCS-2 segment counting, and non-mutating
  metric merges is the kind of thing that is normally lost.
* **`docs/METRICS.md` generated from a live scrape** rather than written by
  hand, so it cannot drift.
* **Worker-only credentials via `LoadCredential`**, keeping money-spending keys
  out of the internet-facing process.

---

## 9. Remaining items — resolved

All seven were worked through after the operator's decisions. What changed:

| # | Item | Outcome |
|---|---|---|
| 1 | AI product focus | **Closed by decision.** Left as-is; not a defect, revisit with more customers. |
| 2 | Support target and alert route | **Done.** 24-hour ticket target, plus a sustained 5xx alert. Both text every administrator through the existing SMS outbox. Pool-free already existed. Verified live: the alert fired on the real 4-day-old ticket and delivered. |
| 3 | PostgreSQL concurrency tests | **Done, and proven.** Four tests against a real database. Validated by injecting the old Python read-modify-write and confirming the suite **fails** — a concurrency test that cannot fail is worthless. |
| 4 | Split `app.py` | **Done.** The exporter moved to `control/mmd/exporter.py`. `app.py` 3,972 → 3,680 lines. Verified output-identical: all 75 metric families present before and after. |
| 5 | Exporter scale | **Done.** One grouped fleet query replaced the per-customer loop. Scrape **0.108s → 0.023s**, measured. |
| 6 | Per-operator Grafana identity | **Closed by decision, documented.** Accepted at two operators, with an explicit trigger to revisit before a third. |
| 7 | Prometheus TSDB accounting | **Closed by decision, documented.** Retention is an explicit 15 days; the size trigger is written down rather than wired into the root-owned pool guard, which would cross a privilege boundary. |

### A bug the new tests found

The concurrency suite immediately caught something reasoning had not. When a
customer has no `credit_accounts` row yet, two simultaneous first charges both
see `rowcount == 0` and both `INSERT`; the loser takes a primary-key violation
and a charge that should simply have applied is aborted instead.

Invisible on SQLite, which serialises writers — which is exactly why the
PostgreSQL suite was worth building. The insert now runs in a `SAVEPOINT`, so
losing the race does not poison the transaction the ledger row is already in,
and the retry is the ordinary atomic `UPDATE`.

The invariant holds live: balance equals ledger sum for all 21 accounts.

### Still deliberately excluded

Automated payment, backup encryption, off-host workspace backup, and worker or
migration-system restructuring remain excluded by product decision.
