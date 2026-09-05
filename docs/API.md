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
| `POST` | `/api/auth/register` | `{username, password, full_name, phone, code}`. Phone is a unique 11-digit Iranian mobile beginning `09` and `code` must verify it. The first account ever created becomes admin. Duplicate username or phone returns a field-specific conflict code |
| `POST` | `/api/auth/login` | `{identifier, password}`. `identifier` is either username or phone. Sets the session cookie |
| `POST` | `/api/auth/request-code` | `{phone, purpose}` where purpose is `signup`, `login`, `profile`, or `recovery`; login and recovery responses avoid account enumeration |
| | | **Never reveals whether a number is registered.** A signup request for an existing number is answered as success and the owner is sent a sign-in reminder instead, so only the number's holder learns anything |
| `POST` | `/api/auth/login-sms` | `{phone, code}`. Sets the same versioned session cookie as password login |
| `POST` | `/api/auth/reset-password` | `{phone, code, new_password}`. Consumes a recovery code, changes the password and revokes existing sessions |
| `GET` | `/api/auth/username-available` | Checks public-hostname syntax, reserved names and uniqueness |
| `POST` | `/api/auth/logout` | |
| `POST` | `/api/auth/password` | Requires the current password |
| `GET` | `/api/me` | Identity, admin flag, balance, unread ticket counts, application version, Telegram configured flag and user ID; never the bot token |
| `PUT` | `/api/profile` | `{full_name, phone, current_password, code?}`. A changed phone must first be verified with a `profile` code; changing the phone revokes older sessions |
| `PUT` | `/api/profile/telegram` | `{bot_token?, user_id?, clear?}`. Stores or clears reusable account-level Telegram settings. A blank token preserves the existing one |
| `GET` | `/api/account/sms` | Optional notification switches plus the customer's balance-step amount and accepted range |
| `PUT` | `/api/account/sms` | `{prefs?: {kind: bool}, credit_step_toman?: int}`. The step must be a multiple of 1,000 between 1,000 and 1,000,000,000 Toman; changing it silently resets the current balance band |

## Public

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/health` | Health and application version |
| `GET` | `/api/public/pricing` | Live rate card for the landing page |

## The machine

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/workspace` | State, size, rates, `blocked`, `can_power_on` |
| `POST` | `/api/workspace` | Create the customer's optional workspace. `{cpu_milli, mem_mib}`; returns a durable operation |
| `POST` | `/api/workspace/delete` | Permanently remove the workspace after username/password confirmation while preserving the account and OpenRouter key |
| `POST` | `/api/workspace/power` | `{on: bool}`. Runs the affordability gate and the admission check |
| `POST` | `/api/workspace/keep-running` | Extends or clears the automatic stop deadline for the current power-on cycle |
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
{ "confirm": "owner-username", "password": "…" }
```

`confirm` must equal the account's own username (case and surrounding whitespace are
forgiven, nothing else). `password` is the account password. A refused attempt is
written to the audit log. Permitted from `on`, `off` and `error` — `error` is
where starting over is most useful.

Kept: reserved ports, saved public keys, size, credit, ledger and the
account-level OpenRouter key. Gone: filesystem, packages, all Docker data,
Hermes/OpenClaw installations and their dashboard credentials. Reset returns
workspace applications to unselected without disrupting external OpenRouter use.

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
| `GET` | `/api/workspace/ai` | Account-level OpenRouter state plus optional workspace service states. Works without a workspace. OpenRouter includes `limit_sync_pending`, last supplier-confirmed `limit_usd`, and `limit_synced_at` |
| `POST` | `/api/workspace/ai/codex` | `{action}` — `install` or `unlink`. Copies the host's allowlisted Codex grant into the workspace. `ai_host_unlinked` when the platform is not signed in |
| `POST` | `/api/workspace/managed-ai/{service}` | `{action}` — `enable` or `disable`; `service` is `opencode` or `openwebui`. The worker installs and the state remains not-ready until nginx publishes the exact hostname |
| `POST` | `/api/workspace/ai/openclaw` | `{action}` — `enable` or `disable`. Records intent; the worker installs. `needs_openrouter` until the managed key exists |
| `POST` | `/api/workspace/ai/openclaw/telegram` | `{action}` — enable/disable Telegram using the account-level token and user ID |
| `POST` | `/api/workspace/ai/openclaw/devices` | Approves devices waiting to pair with the customer's OpenClaw dashboard |
| `POST` | `/api/workspace/ai/claude` | `{action: "install"｜"unlink"}`. Installs the CLI and carries the platform sign-in across |
| `POST` | `/api/workspace/ai/hermes` | `{action, telegram_enabled?, telegram_token?, telegram_users?}` where action is `enable` or `disable`. Hermes uses the account OpenRouter key and can optionally enable Telegram from account defaults or supplied values; the token is never returned |
| `GET` | `/api/workspace/ai/usage?service=` | Per-model token counts and billed Toman for `claude` or `codex` |

Administrator AI configuration is separated by responsibility:

| Method | Path | Notes |
|---|---|---|
| `GET/PUT` | `/api/admin/openrouter` | USD-to-Toman conversion. OpenRouter has no platform discount or model restriction; account credit controls the key's supplier-side spend cap |
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
| `GET` | `/api/tickets/{id}` | Read the thread without changing it |
| `POST` | `/api/tickets/{id}/read` | Mark the thread read |
| `POST` | `/api/tickets/{id}/messages` | Reply. Reopens a closed ticket |

## Administration

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/admin/users` | |
| `PUT` | `/api/admin/users/{id}/profile` | Change the customer's full name and phone; audited |
| `POST` | `/api/admin/users/{id}/approve` | Activates the account and schedules its OpenRouter key. Does not create compute |
| `POST` | `/api/admin/users/{id}/reject` | |
| `POST` | `/api/admin/users/{id}/admin` | Promote or demote |
| `POST` | `/api/admin/users/{id}/credit` | Grant credit. Immediately attempts to refresh the account-level OpenRouter cap, while durable worker retry handles supplier failure; Hermes is only one possible consumer |
| `DELETE` | `/api/admin/users/{id}` | |
| `GET`/`PUT` | `/api/admin/settings` | Rate card, overcommit ratios, host reserve |
| `GET` | `/api/admin/activity` | Paginated global audit log. Accepts `limit`, `offset`, and a `q` search across actor username, action and target |
| `GET` | `/api/admin/operations` | In-flight and recent long operations |
| `GET` | `/api/admin/users/{id}` | One customer's identity, balance, workspace and service detail |
| `GET` | `/api/admin/grafana` | Authenticated Grafana base path, dashboard map and masked/admin credential metadata |
| `GET`/`PUT` | `/api/admin/agent-model/{service}` | Install-time default model for `claude` or `codex`; never rewrites an existing customer choice |
| `GET`/`PUT` | `/api/admin/openrouter` | Exchange rate; customer keys have credit-derived spend caps but unrestricted model choice |
| `GET` | `/api/admin/hermes` | Adoption counts and the managed-key state |
| `PUT` | `/api/admin/hermes` | Hermes' default model |
| `GET`/`PUT` | `/api/admin/openclaw` | OpenClaw's default model (provider-prefixed) and adoption counts |
| `GET` | `/api/admin/ai-pricing` | Per-service price tables; `?service=claude｜codex` |
| `POST`/`PUT`/`DELETE` | `/api/admin/ai-pricing[/{id}]` | Add, change or remove a model price |
| _(removed)_ | `/api/admin/metrics`, `/api/admin/metrics/per-user`, `/api/admin/capacity` | Superseded by Prometheus. The same readings are exported at `/internal/metrics` as `mmd_workspace_cpu_seconds_total`, `mmd_workspace_memory_bytes` and `mmd_capacity_*`, and shown on the embedded dashboards. See [METRICS.md](METRICS.md) |
| `GET`/`PUT` | `/api/admin/backup` | Database-backup schedule. The bot token is **never returned** — only `bot_token_set` and the last four characters |
| `POST` | `/api/admin/backup/run` | Dump and send one backup immediately; returns `{ok, error?, config}` |
| `GET` | `/api/admin/tickets` | Queue, filterable by status, with counts |
| `GET` | `/api/admin/tickets/{id}` | Read the thread without changing it |
| `POST` | `/api/admin/tickets/{id}/read` | Mark the thread read |
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
