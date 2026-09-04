# Observability

Prometheus listens on loopback port 9091, scrapes the control plane every five minutes, and Grafana reads only
Prometheus. The exporter requires `Authorization: Bearer $MMD_PROMETHEUS_TOKEN`;
the token file used by Prometheus must contain the same value. Never make
Prometheus, Grafana, or the exporter public. The admin embed must be protected
by the dashboard's existing administrator session at the reverse proxy.
Grafana listens on loopback port 3002 because an older Docker monitoring stack
already owns the host's port 3000.
Install `observability/prometheus.default` as `/etc/default/prometheus`; port
9091 is intentional because the host already runs an unrelated Docker-managed
Prometheus on 9090.

## Metric catalogue

**Status.** The bold core below shipped first; the *Product operations*
section and the per-customer credit-block and lifecycle-state series have since
been added, bringing the exporter to 51 metric families. The complete
list of what is actually emitted — with types, labels and meanings — is
[METRICS.md](METRICS.md), generated from a live scrape rather than written by
hand. The plain entries that remain here are still the ordered backlog. Labels must remain bounded: `username`, state,
service, operation kind, ledger kind, model family, HTTP route template and
status class are acceptable; raw URLs, prompts, ticket text, IPs, secrets and
operation IDs are not.

### Accounts and money

- **Credit balance per username in integer micro-Toman.**
- Credit granted, charged, adjusted and refunded, by ledger kind.
- Hourly compute rate and projected remaining runtime per username.
- Negative-balance and low-credit account counts; duration below zero.
- Approval state, administrator flag, account age and approval latency.
- Five-minute and daily revenue, compute revenue and AI revenue.
- Ledger settlement lag, duplicate-charge prevention hits and failed settlements.
- Exchange-rate value and age; catalogue price revision timestamp.

### OpenRouter and AI usage

- **Supplier usage in USD, key readiness and credit-block state per username.**
- Supplier limit, remaining supplier allowance and limit-sync lag.
- Key create/update/revoke attempts, failures and request latency.
- Tokens by username, service, model family and direction (input/output/cache).
- Requests, supplier cost and customer charge by service and model family.
- Unpriced model observations and usage-counter regressions.
- Claude/Codex credential availability, expiry, sync success and sync age.
- **Hermes, OpenClaw, OpenCode and Open WebUI readiness per username.**
- Service enable intent, install duration, retry count and last-error presence.
- Telegram requested/ready mismatch and gateway health.

### Workspaces and capacity

- **Workspace presence, lifecycle state and desired power state per username.**
- **Configured millicores, memory MiB and disk GiB per username.**
- Latest CPU seconds/rate, memory working set and network receive/transmit bytes.
- **Observed disk use**, root use, Docker use and sample age per username.
- Host CPU load, memory available, swap use and filesystem capacity.
- ZFS pool used/free, fragmentation, health, snapshot age and failed snapshots.
- Allocated/active CPU, memory and disk; catalogue and policy ceilings.
- Admission rejections by resource, archive count and nearest purge deadline.
- Power-on duration, auto-stop deadline, boot failures and state-transition age.
- Incus API latency/errors, instance count and metrics scrape freshness.

### Connectivity and customer features

- **Published-port count per username**, allocation failures and pool exhaustion.
- TCP/UDP rule convergence, vhost readiness and nginx reload failures.
- Certificate expiry days, issuance failures, retry backoff and weekly budget.
- SSH enabled/key count, RDP enabled/installed and connection launch failures.
- Browser terminal sessions, active websocket count and abnormal disconnects.
- File operations by kind, bytes transferred, failures and latency.
- Apt repair attempts/results and service health checks.

### Product operations

- API requests by route template, method, status class and latency histogram.
- Active sessions, authentication failures and registration conflicts by field.
- Operations queued/running/completed/failed and duration by kind.
- Worker loop success timestamp/duration, reconciliation failures and backlog.
- Provisioner calls by allowlisted verb, result and duration.
- Support tickets by state, first-response time, resolution time and unread count.
- Notifications created/read by kind and age of oldest unread notification.
- Backup age, duration, bytes, verification result and restore drill age.
- Database pool use, query latency, transaction failures and table growth.
- Process CPU/RSS/file descriptors/restarts for API, worker, provisioner and vhosts.
- Product build version, deploy timestamp and schema-patch failures.

## Dashboard layout

The provisioned admin dashboard should contain: fleet health and stale-worker
alerts; capacity and ZFS headroom; revenue/credit exposure; per-customer drill
down; AI spend/tokens; operation failures; connectivity/certificates; support
SLOs; and backup/recovery. Default time range is 24 hours, with a username
variable. Alerts should link back to the corresponding MMD admin customer page.
