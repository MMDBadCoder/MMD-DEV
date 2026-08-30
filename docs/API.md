# HTTP API

Session-cookie authenticated, JSON in and out. Served under `/api`; anything else
returns the single-page app shell.

## Conventions

**Errors carry a code, never a sentence for display.**

```json
{ "detail": { "code": "insufficient_credit", "message": "…", "needed": 480, "balance": 0 } }
```

The `message` field is English and exists for logs and API consumers. The Persian
interface resolves `code` against `web/js/i18n.js` and never shows the English.
Extra keys carry the values the sentence needs.

**Successful responses carry codes too, where a message would otherwise be
needed.** `{"status": "pending", "code": "pending_approval"}` rather than a
finished sentence. This is enforced by `tests/test_no_english_prose.py`.

**Money** is Toman throughout, as a number. The interface formats it.

**Authorisation.** Endpoints under `/api/admin/` require `is_admin`. Everything
else requires an approved session, except `/api/health`, `/api/public/pricing`,
and the auth endpoints. Accessing another account's resource returns **404**, not
403 — a 403 confirms existence.

---

## Authentication

| Method | Path | Notes |
|---|---|---|
| `POST` | `/api/auth/register` | The first account ever created becomes admin. Returns `{status, code}`; the response is identical whether or not the address already exists, so accounts cannot be enumerated |
| `POST` | `/api/auth/login` | Sets the session cookie |
| `POST` | `/api/auth/logout` | |
| `POST` | `/api/auth/password` | Requires the current password |
| `GET` | `/api/me` | Email, admin flag, balance, unread ticket counts |

## Public

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/health` | |
| `GET` | `/api/public/pricing` | Live rate card for the landing page |

## The machine

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/workspace` | State, size, rates, `blocked`, `can_power_on` |
| `POST` | `/api/workspace/power` | `{on: bool}`. Runs the affordability gate and the admission check |
| `POST` | `/api/workspace/tier` | `{cpu_milli, mem_mib}`. Live-applied when running; memory cannot shrink while on |
| `POST` | `/api/workspace/reset` | **Factory reset.** `{confirm, password}` — validates immediately, then returns a durable `operation` |
| `GET` | `/api/workspace/metrics` | `?minutes=` (default 5, one of 5/15/60/360/1440). CPU in **cores** and memory in **GB** — absolute, never percentages — plus the tier so a chart can show headroom, and `sample_seconds` so it can refresh in step |
| `GET` | `/api/tiers` | Size catalogue with the price of each option |

`blocked` is structured, not prose:

```json
{ "blocked": { "code": "insufficient_credit", "need": 480, "have": 0 } }
{ "blocked": { "code": "capacity_memory" } }
```

### Factory reset

`POST /api/workspace/reset` queues a durable operation that destroys the machine
and rebuilds it from the golden image. Both fields are required and both are
checked server-side before anything is queued:

```json
{ "confirm": "owner@example.com", "password": "…" }
```

`confirm` must equal the account's own email (case and surrounding whitespace are
forgiven, nothing else). `password` is the account password. A refused attempt is
written to the audit log. Permitted from `on`, `off` and `error` — `error` is
where starting over is most useful.

Kept: reserved ports, saved public keys, size, credit, ledger. Gone: filesystem,
packages, all Docker data, Hermes installation, its OpenRouter key and dashboard
credentials. Reset returns Hermes to unselected; the customer may enable it
again after the rebuilt machine is ready.

## Operations

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/operations` | The signed-in customer's 20 newest resets and package installations, including progress and failure codes |
| `GET` | `/api/admin/operations` | The 100 newest operations across all accounts |

Factory reset and package installation return an `operation` rather than
holding an HTTP request open. The worker advances `queued` through `running` to
`succeeded` or `failed`; the global dashboard strip polls this resource, so
progress survives navigation and a browser refresh. Account deletion is also a
durable operation, but its row is deliberately erased with the account after
all external resources have been removed.

## Notifications

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/notifications` | Up to 100 active customer notifications plus unread count; materialises deduplicated balance, auto-stop, archive and session conditions |
| `POST` | `/api/notifications/{id}/read` | Mark one owned notification read |
| `POST` | `/api/notifications/read-all` | Mark all owned notifications read |

Operation completion/failure and staff ticket replies create durable events.
Read state belongs to the recipient and is separate from whether the underlying
condition has been resolved.

## Connections

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/workspace/services` | SSH and RDP state, reserved ports, addresses, keys. Allocates the two reservations if missing |
| `POST` | `/api/workspace/services/ssh` | `{enabled}`. Refuses to switch on with zero keys; refuses to remove the last key while on |
| `POST` | `/api/workspace/services/rdp` | `{enabled, password}`. **The password is required on every switch-on**, not only the first. Needs ≥ 2 GB memory |
| `POST` | `/api/workspace/ssh/keys` | `{public_key}`. Parsed and validated, never sanitised; option prefixes such as `command="…"` are rejected |
| `DELETE` | `/api/workspace/ssh/keys/{id}` | |
| `WS` | `/api/workspace/terminal` | Bridged to the Incus exec websocket. Closes `4403` denied, `4404` no machine, `4409` machine off — the page prints Persian for each |

## Files

All routed through the root provisioner, because a restricted Incus certificate
is denied the file API. Paths are validated server-side.

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/workspace/files?path=` | Directory listing |
| `GET` | `/api/workspace/files/content?path=` | Text content |
| `PUT` | `/api/workspace/files/content` | Save |
| `POST` | `/api/workspace/files/new` | Create a file |
| `POST` | `/api/workspace/files/mkdir` | Create a directory |
| `POST` | `/api/workspace/files/upload?path=` | Multipart |
| `GET` | `/api/workspace/files/download?path=` | |
| `GET` | `/api/workspace/files/archive?path=` | Recursive zip |
| `DELETE` | `/api/workspace/files?path=` | |

## Ports

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/workspace/ports` | Published and reserved ports |
| `POST` | `/api/workspace/ports` | `{internal_port, note?}`. External port allocated from 20000–29999 and forwarded over both TCP and UDP. Free. Returns the ordinary endpoint plus `<username>.<domain>:<external_port>`, and `warning_code: "discouraged_port"` for port 22 |
| `DELETE` | `/api/workspace/ports/{id}` | Reserved SSH/RDP ports refuse with `port_reserved` |

## Tools and AI

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/presets` | Toolset bundles |
| `POST` | `/api/workspace/presets` | Install. Package names are validated against a strict pattern on both sides |
| `GET` | `/api/workspace/ai` | Claude Code state: installed, version, signed in, expiry |
| `POST` | `/api/workspace/ai/claude` | `{action: "install"｜"unlink"}`. Installs the CLI and carries the platform sign-in across |

## Billing and activity

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/billing/summary` | Balance, current rates, projections |
| `GET` | `/api/billing/transactions` | Ledger, paginated |
| `GET` | `/api/billing/usage` | Spend per hour, for the chart |
| `GET` | `/api/activity` | Audit log for this account |

## Support

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/tickets` | Own tickets, with unread marks |
| `POST` | `/api/tickets` | `{subject, body}`. Capped at 10 open tickets |
| `GET` | `/api/tickets/{id}` | Marks read |
| `POST` | `/api/tickets/{id}/messages` | Reply. Reopens a closed ticket |

## Administration

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/admin/users` | |
| `POST` | `/api/admin/users/{id}/approve` | Provisions a machine, optionally with toolsets, hands it back **off** |
| `POST` | `/api/admin/users/{id}/reject` | |
| `POST` | `/api/admin/users/{id}/admin` | Promote or demote |
| `POST` | `/api/admin/users/{id}/credit` | Grant credit |
| `DELETE` | `/api/admin/users/{id}` | |
| `GET` | `/api/admin/capacity` | Reserved against schedulable capacity |
| `GET` | `/api/admin/metrics` | `?minutes=`. Actual usage summed across every workspace, in cores and GB, scaled against sellable capacity |
| `GET`/`PUT` | `/api/admin/settings` | Rate card, overcommit ratios, host reserve |
| `GET` | `/api/admin/activity` | Global audit log |
| `GET` | `/api/admin/tickets` | Queue, filterable by status, with counts |
| `GET` | `/api/admin/tickets/{id}` | |
| `POST` | `/api/admin/tickets/{id}/messages` | Reply; marks the ticket answered |
| `PUT` | `/api/admin/tickets/{id}/status` | `open ｜ in_progress ｜ answered ｜ closed` |
| `POST` | `/api/admin/workspaces/{id}/apt-repair` | Re-apply the apt configuration |

---

## Error codes

Selected; each has Persian text in `web/js/i18n.js` under `err.<code>`.

| Code | Meaning |
|---|---|
| `not_signed_in`, `session_expired`, `suspended` | Session |
| `bad_credentials`, `wrong_password`, `bad_password` | Authentication |
| `no_workspace`, `no_such_workspace` | No machine |
| `machine_off`, `busy`, `archived` | Wrong state |
| `insufficient_credit` | Carries `needed` and `balance` |
| `no_capacity` | Carries `resource`: `cpu` or `memory` |
| `invalid_size`, `mem_shrink_running` | Resize |
| `rdp_needs_password`, `rdp_password_short`, `rdp_needs_memory` | Desktop |
| `port_reserved`, `port_in_use`, `too_many_ports` | Ports |
| `last_key` | Removing the only key while SSH is on |
| `reset_confirm_mismatch`, `reset_bad_state`, `reset_failed` | Factory reset |
| `ai_host_unlinked`, `ai_failed`, `ai_status_failed` | AI tools |
| `no_such_ticket`, `too_many_tickets` | Support |
