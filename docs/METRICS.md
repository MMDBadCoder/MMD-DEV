# Exported metrics

Every series `/internal/metrics` exposes: **67 metric families**.
Generated from a live scrape, so this is what the endpoint actually
emits rather than what it was meant to.

Scraped by Prometheus on loopback `:9091` every 60 seconds and read by Grafana
on loopback `:3002`. This is independent of five-minute business settlement.
The endpoint requires
`Authorization: Bearer $MMD_PROMETHEUS_TOKEN`. See
[OBSERVABILITY.md](OBSERVABILITY.md) for the stack and its guardrails.

## Reading the label column

Labels are bounded on purpose. `route` is always a route **template**
(`/api/tickets/{ticket_id}`), never a requested path; `status` is a
**class** (`2xx`), never a code; `verb` comes from the provisioner's
fixed allowlist. `username` is the one label that grows with the
business — one series per customer per metric — which is fine at the
current scale and is the first thing to revisit at a few hundred
accounts.

Never labels: raw URLs, prompts, ticket text, IP addresses, secrets,
operation IDs.

## Platform

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `mmd_build_info` | gauge | `version` | Deployed release, as a label on a constant 1. |
| `mmd_schema_patch_failures` | gauge | — | Schema patches that could not be applied on this boot. Silent by design, so counted here. |

## Accounts and money

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `mmd_users` | gauge | `status` | Accounts by approval status. |
| `mmd_user_credit_micro_toman` | gauge | `username` | Balance in integer micro-Toman. Integers because the ledger is the source of truth and floats drift. |
| `mmd_ledger_entries` | gauge | `kind` | Ledger rows by kind. |
| `mmd_ledger_micro_toman` | gauge | `kind` | Ledger totals by kind. Negative is revenue. |

## OpenRouter

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `mmd_user_openrouter_ready` | gauge | `username` | 1 when a supplier key exists for this customer. |
| `mmd_user_openrouter_blocked` | gauge | `username` | 1 when that key is disabled for non-payment. Different question from `ready`, and the one that explains an agent going quiet. |
| `mmd_user_openrouter_usage_usd` | gauge | `username` | Cumulative supplier spend, as OpenRouter reports it. |
| `mmd_ai_tokens_total` | counter | `username`, `service`, `model`, `direction` | Tokens consumed per model, split into input / output / cache_read because they are priced differently. Cumulative, so use `increase(...[1h])` to ask which models were used in an hour. Covers Claude and Codex, which report per-model usage from inside the workspace; OpenRouter bills one figure per key and cannot be split. |
| `mmd_ai_billed_micro_toman_total` | counter | `username`, `service`, `model` | Toman actually charged for those tokens. |

## Workspaces and capacity

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `mmd_workspaces` | gauge | `state` | Workspaces by lifecycle state, fleet-wide. |
| `mmd_workspace_state` | gauge | `state`, `username` | Lifecycle state **per customer**. The fleet aggregate says three are in error; this says which three. |
| `mmd_user_workspace_present` | gauge | `username` | 1 when the customer has compute at all. An approved account without a workspace is a supported state, not a fault. |
| `mmd_workspace_desired_on` | gauge | `username` | Intended power state, which may differ from actual during a transition. |
| `mmd_workspace_cpu_millicores` | gauge | `username` | Configured CPU allowance. |
| `mmd_workspace_memory_mib` | gauge | `username` | Configured memory allowance. |
| `mmd_workspace_disk_limit_gib` | gauge | `username` | Configured disk allowance (ZFS refquota). |
| `mmd_workspace_disk_used_mib` | gauge | `username` | Disk actually written. Thin provisioning means the sum of limits legitimately exceeds the pool. |
| `mmd_workspace_published_ports` | gauge | `username` | Ports this customer has published. |
| `mmd_workspace_service_ready` | gauge | `service`, `username` | Managed agent installed, by service: hermes, openclaw, opencode, openwebui. |
| `mmd_workspace_cpu_seconds_total` | counter | `username` | CPU seconds consumed by the workspace. A raw counter — Prometheus differentiates it, so the rate window is the reader's choice rather than one baked in at export. |
| `mmd_workspace_memory_bytes` | gauge | `username` | Memory actually in use. The configured allowance alone says nothing without it. |
| `mmd_workspace_sample_age_seconds` | gauge | `username` | Seconds since the last Incus reading. A workspace whose samples stopped looks identical to an idle one without this. |
| `mmd_capacity_total_cores` | gauge | — | Physical cores on the host. |
| `mmd_capacity_total_memory_gib` | gauge | — | Physical memory. |
| `mmd_capacity_schedulable_cores` | gauge | — | What the admission check will allow, after the overcommit ratio. |
| `mmd_capacity_schedulable_memory_gib` | gauge | — | As above, for memory. |
| `mmd_capacity_used_cores` | gauge | — | Cores allocated to running workspaces. |
| `mmd_capacity_used_memory_gib` | gauge | — | Memory allocated to running workspaces. |
| `mmd_capacity_running` | gauge | — | Workspaces currently powered on. |
| `mmd_pool_total_gib` | gauge | — | ZFS pool size, as the worker last measured it. |
| `mmd_pool_used_gib` | gauge | — | Pool space allocated. |
| `mmd_pool_free_gib` | gauge | — | Pool space free. The disk guard acts below 8 GiB. |
| `mmd_pool_committed_gib` | gauge | — | Sum of every workspace's disk allowance. Legitimately exceeds the pool — that is thin provisioning. |

## API

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `mmd_http_requests_total` | counter | `method`, `route`, `status` | API requests by route **template**, method and status **class**. |
| `mmd_http_exceptions_total` | counter | `method` | Requests that raised out of the handler. |
| `mmd_http_request_seconds` | histogram | `method`, `route` | API latency histogram. |
| `mmd_auth_failures_total` | counter | `method`, `reason` | Failed sign-ins by method and reason. Never by account. |
| `mmd_registration_conflicts_total` | counter | `field` | Rejected sign-ups by the field that conflicted. |

## Operations and the provisioner

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `mmd_operations` | gauge | `kind`, `status` | Durable operations by kind and status. |
| `mmd_operations_backlog` | gauge | — | Queued plus running. The number that says whether the worker is keeping up. |
| `mmd_provisioner_calls_total` | counter | `result`, `verb` | Privileged provisioner calls by verb and result. |
| `mmd_provisioner_seconds` | histogram | `verb` | Provisioner latency histogram by verb. |

## The worker

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `mmd_worker_ticks_total` | counter | `result` | Worker loop iterations by result. |
| `mmd_worker_tick_failures_total` | counter | — | Loop iterations that raised. |
| `mmd_worker_tick_seconds` | histogram | — | Loop duration histogram. |
| `mmd_worker_last_tick_seconds` | gauge | — | Duration of the most recent iteration. |
| `mmd_worker_heartbeat_age_seconds` | gauge | — | Seconds since the worker last ticked. `-1` means never. |
| `mmd_worker_last_success_age_seconds` | gauge | — | Seconds since the last iteration that completed without raising. |
| `mmd_worker_metrics_snapshot_age_seconds` | gauge | — | Age of the worker counter snapshot folded into this scrape. `-1` means missing or unreadable. |

## Support and notifications

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `mmd_tickets` | gauge | `status` | Support tickets by status. |
| `mmd_ticket_oldest_open_seconds` | gauge | — | Age of the oldest ticket not closed. The SLO number. |
| `mmd_tickets_unread_staff` | gauge | — | Tickets with unread customer messages. |
| `mmd_notifications_unread` | gauge | `kind` | Unread in-app notifications by kind. |
| `mmd_notification_oldest_unread_seconds` | gauge | — | Age of the oldest unread notification. |

## Delivery

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `mmd_sms_messages` | gauge | `kind`, `status` | SMS outbox by kind and status. Historical `skipped` rows came from the retired trial allowlist. |
| `mmd_backup_enabled` | gauge | — | 1 when the backup schedule is armed. |
| `mmd_backup_age_seconds` | gauge | — | Seconds since the last successful backup. The number that matters; `enabled` keeps reading 1 long after delivery stops. |
| `mmd_backup_last_size_bytes` | gauge | — | Size of the last delivered dump. |
| `mmd_backup_failing` | gauge | — | 1 when the last attempt recorded an error. |

## Processes

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `mmd_process_up` | gauge | `unit` | 1 when the unit has a live main process. |
| `mmd_process_cpu_seconds_total` | gauge | `unit` | CPU seconds consumed, per unit. |
| `mmd_process_resident_bytes` | gauge | `unit` | Resident memory, per unit. |
| `mmd_process_threads` | gauge | `unit` | Thread count, per unit. |
| `mmd_process_open_fds` | gauge | `unit` | Open file descriptors, per unit. A steady climb is a leak. |
| `mmd_process_uptime_seconds` | gauge | `unit` | Seconds since the unit's main process started. |
| `mmd_process_restarts_total` | gauge | `unit` | Restarts since boot. A climbing line is a crash loop. |

## Histograms

Three families are histograms — `mmd_http_request_seconds`,
`mmd_provisioner_seconds`, `mmd_worker_tick_seconds`. Each exposes
`_bucket{le=...}`, `_sum` and `_count`. Buckets run from 5 ms to 900 s,
chosen so that both a sub-second API call and a provisioner verb that
legitimately takes minutes land inside the range instead of piling into
`+Inf`, where they would say nothing.

```promql
histogram_quantile(0.95, sum by (le, route) (rate(mmd_http_request_seconds_bucket[5m])))
```

## Where each metric comes from

Two sources, merged into one scrape:

- **The API process** renders on demand: database aggregates, `/proc`
  readings for all four services, and its own in-process counters.
- **The worker** is a separate process, so its counters cannot be read
  directly. It writes a snapshot to `/var/lib/mmd/worker-metrics.json`
  each tick and the exporter folds that in **for rendering only**.

That merge is deliberately non-mutating. Folding the worker's cumulative
counters into the exporter's own registry would add one worker-lifetime
per scrape, and the graphs would climb forever on their own.

Worker counters reset to zero when the worker restarts, so a merged
total can go **down**. That is a normal counter reset and `rate()`
already handles it.

## Counters seeded at zero

`mmd_auth_failures_total`, `mmd_registration_conflicts_total` and
`mmd_worker_tick_failures_total` are pre-registered with zero values so
the series exists before the first failure. Otherwise the panel reads
"No data", which on an operations dashboard is ambiguous in the worst
way: it looks identical whether nothing is wrong or the exporter is
broken. A flat zero line says "measured, and fine".
