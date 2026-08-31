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
| `POST` | `/api/auth/register` | `{email, username, password, full_name, phone}`. Phone is an 11-digit Iranian mobile beginning `09`. The first account ever created becomes admin. Returns `{status, code}`; duplicate email and phone responses are indistinguishable from success so accounts cannot be enumerated |
| `POST` | `/api/auth/login` | `{username, password}`. Username is the only login identifier; email and phone are profile data. Sets the session cookie |
| `POST` | `/api/auth/logout` | |
| `POST` | `/api/auth/password` | Requires the current password |
| `GET` | `/api/me` | Identity, admin flag, balance, unread ticket counts, application version, Telegram configured flag and user ID; never the bot token |
| `PUT` | `/api/profile` | `{email, full_name, phone, current_password}`. Changes customer identity after password confirmation |
| `PUT` | `/api/profile/telegram` | `{bot_token?, user_id?, clear?}`. Stores or clears reusable account-level Telegram settings. A blank token preserves the existing one |

## Public

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/health` | Health and application version |
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
| `POST` | `/api/workspace/ports` | `{internal_port, note?}`. External port allocated from 20000–29999 and forwarded over both TCP and UDP. Returns `ports.<domain>:<external>` plus `http://<username>.<domain>:<internal>`; `web_ready` becomes true after nginx reconciliation |
| `DELETE` | `/api/workspace/ports/{id}` | Reserved SSH/RDP ports refuse with `port_reserved` |

## Tools and AI

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/workspace/ai` | Claude Code state: installed, version, signed in, expiry |
| `POST` | `/api/workspace/ai/codex` | `{action}` — `install` or `unlink`. Copies the host's allowlisted Codex grant into the workspace. `ai_host_unlinked` when the platform is not signed in |
| `POST` | `/api/workspace/ai/openclaw` | `{action}` — `enable` or `disable`. Records intent; the worker installs. `needs_openrouter` until the managed key exists |
| `POST` | `/api/workspace/ai/claude` | `{action: "install"｜"unlink"}`. Installs the CLI and carries the platform sign-in across |
| `POST` | `/api/workspace/ai/hermes` | `{enabled, telegram_enabled?, telegram_token?, telegram_users?}`. Enables Hermes and optionally its Telegram gateway. A token and comma-separated numeric sender allowlist are required when first enabling Telegram; the token is never returned |

Administrator AI configuration is separated by responsibility:

| Method | Path | Notes |
|---|---|---|
| `GET/PUT` | `/api/admin/openrouter` | USD-to-Toman conversion, OpenRouter workspace/guardrail IDs and model-price ceiling. OpenRouter has no platform discount |
| `GET/PUT` | `/api/admin/hermes` | Hermes default model and adoption state |
| `GET/POST/PUT/DELETE` | `/api/admin/ai-pricing` | Claude Code model prices; Claude's discount is stored through the restricted settings endpoint |

The AI state includes `hermes.telegram_enabled`, `telegram_ready`,
`telegram_users`, and `telegram_error`. The control plane keeps the bot token
only until the worker has delivered it to the workspace's protected environment
file. Omitting `telegram_enabled` preserves the current gateway intent, making a
repeated Hermes enable request idempotent. Explicitly setting it to `false`
removes the gateway service and Telegram credentials without disabling Hermes.

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
| `PUT` | `/api/admin/users/{id}/profile` | Change the customer's email, full name and phone; audited |
| `POST` | `/api/admin/users/{id}/approve` | Provisions a machine and hands it back **off** |
| `POST` | `/api/admin/users/{id}/reject` | |
| `POST` | `/api/admin/users/{id}/admin` | Promote or demote |
| `POST` | `/api/admin/users/{id}/credit` | Grant credit. Marks an active Hermes key for supplier-cap refresh on the next worker pass |
| `DELETE` | `/api/admin/users/{id}` | |
| `GET` | `/api/admin/capacity` | Reserved against schedulable capacity |
| `GET` | `/api/admin/metrics` | `?minutes=`. Actual usage summed across every workspace, in cores and GB, scaled against sellable capacity |
| `GET`/`PUT` | `/api/admin/settings` | Rate card, overcommit ratios, host reserve |
| `GET` | `/api/admin/activity` | Global audit log |
| `GET` | `/api/admin/metrics/per-user` | Per-customer usage series |
| `GET` | `/api/admin/operations` | In-flight and recent long operations |
| `GET` | `/api/admin/storage` | Pool total/used/free, per-workspace usage, overcommit ratio |
| `GET`/`PUT` | `/api/admin/openrouter` | Exchange rate, per-workspace spend cap, model guardrail |
| `GET` | `/api/admin/hermes` | Adoption counts and the managed-key state |
| `PUT` | `/api/admin/hermes` | Hermes' default model |
| `GET`/`PUT` | `/api/admin/openclaw` | OpenClaw's default model (provider-prefixed) and adoption counts |
| `GET` | `/api/admin/ai-pricing` | Per-service price tables; `?service=claude｜codex` |
| `POST`/`PUT`/`DELETE` | `/api/admin/ai-pricing[/{id}]` | Add, change or remove a model price |
| `GET`/`PUT` | `/api/admin/backup` | Database-backup schedule. The bot token is **never returned** — only `bot_token_set` and the last four characters |
| `POST` | `/api/admin/backup/run` | Dump and send one backup immediately; returns `{ok, error?, config}` |
| `GET` | `/api/admin/tickets` | Queue, filterable by status, with counts |
| `GET` | `/api/admin/tickets/{id}` | |
| `POST` | `/api/admin/tickets/{id}/messages` | Reply; marks the ticket answered |
| `PUT` | `/api/admin/tickets/{id}/status` | `open ｜ in_progress ｜ answered ｜ closed` |
| `POST` | `/api/admin/workspaces/{id}/apt-repair` | Re-apply the apt configuration |
| `POST` | `/api/admin/workspaces/{id}/power-off` | Stop a running customer workspace, settle elapsed usage, and preserve its data |

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
| `backup_invalid` | Backup token, chat id, or enabling with neither stored |
