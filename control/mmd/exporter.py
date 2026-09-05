"""The Prometheus exposition endpoint's body.

Lifted out of `app.py`, which had grown to 3,972 lines and owned authentication,
billing, file transfer, the terminal bridge and this. The exporter was the
cleanest ~330 lines to separate: it has one caller, its own tests, and it reads
the database without writing to it, so nothing else depends on when it runs.

The route itself stays in `app.py` - it needs the app's bearer-token check and
its session plumbing. Everything below is the rendering.

Two rules hold throughout, and both are enforced at the call site rather than
here:

  * **Labels stay bounded.** Route TEMPLATES never paths, status CLASSES never
    codes, provisioner VERBS from the fixed allowlist. `username` is the one
    label that grows with the business, which is a known and written-down cost.
  * **Counters keep their last value; gauges tell the truth now.** A stopped
    machine reports zero memory but retains its CPU counter, because a counter
    that goes backwards is a counter reset to Prometheus.
"""
from __future__ import annotations

import subprocess
import threading
import time
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import backup as backuplib
from . import metrics as m
from . import service as svc
from .models import (AiUsageMark, CreditAccount, CreditTransaction,
                     ExposedPort, Notification, OpenRouterAccount, Operation,
                     Setting, SmsMessage, Ticket, TicketStatus, UsageSample,
                     User, UserStatus, Workspace, WorkspaceState)


def _metric_label(value) -> str:
    return str(value or "").replace("\\", "").replace('"', "").replace("\n", " ")


# Which units to report process metrics for. Named rather than discovered:
# these four ARE the platform, and a discovered list would quietly stop
# covering one that was renamed.
_UNITS = ("mmd-api", "mmd-worker", "mmd-provisioner", "mmd-vhosts")

def customer_metrics(db: Session) -> list[str]:
    """Render every account and machine series from one fleet query.

    Prometheus scrapes once a minute. Querying four relations separately for
    every customer was harmless at 21 accounts but made scrape cost grow with
    a large multiplier. The outer joins deliberately keep account-only users,
    because owning no machine is a supported product journey.
    """
    port_counts = (
        select(ExposedPort.workspace_id,
               func.count(ExposedPort.id).label("published_ports"))
        .group_by(ExposedPort.workspace_id).subquery()
    )
    rows = db.execute(
        select(User, CreditAccount.balance_micro,
               OpenRouterAccount.usage_usd,
               OpenRouterAccount.key.is_not(None),
               OpenRouterAccount.credit_blocked,
               Workspace,
               func.coalesce(port_counts.c.published_ports, 0))
        .outerjoin(CreditAccount, CreditAccount.user_id == User.id)
        .outerjoin(OpenRouterAccount, OpenRouterAccount.user_id == User.id)
        .outerjoin(Workspace, Workspace.user_id == User.id)
        .outerjoin(port_counts, port_counts.c.workspace_id == Workspace.id)
        .order_by(User.id)
    ).all()
    out: list[str] = []
    user_counts = {status: 0 for status in UserStatus}
    workspace_counts = {state: 0 for state in WorkspaceState}
    for user, balance, usage, router_ready, blocked, ws, published_ports in rows:
        user_counts[user.status] += 1
        if ws:
            workspace_counts[ws.state] += 1
        labels = f'username="{_metric_label(user.username)}"'
        out.extend((
            f"mmd_user_credit_micro_toman{{{labels}}} {balance or 0}",
            f"mmd_user_openrouter_usage_usd{{{labels}}} {usage or 0}",
            f"mmd_user_openrouter_ready{{{labels}}} {1 if router_ready else 0}",
            f"mmd_user_openrouter_blocked{{{labels}}} {1 if blocked else 0}",
            f"mmd_user_workspace_present{{{labels}}} {1 if ws else 0}",
        ))
        if not ws:
            continue
        out.extend((
            f"mmd_workspace_desired_on{{{labels}}} {1 if ws.desired_on else 0}",
            f"mmd_workspace_cpu_millicores{{{labels}}} {ws.cpu_milli}",
            f"mmd_workspace_memory_mib{{{labels}}} {ws.mem_mib}",
            f"mmd_workspace_disk_limit_gib{{{labels}}} {ws.disk_gib}",
            f"mmd_workspace_disk_used_mib{{{labels}}} {ws.disk_used_mib or 0}",
            f"mmd_workspace_published_ports{{{labels}}} {published_ports}",
            f'mmd_workspace_state{{{labels},state="{ws.state.value}"}} 1',
        ))
        for name in ("hermes", "openclaw", "opencode", "openwebui"):
            out.append(f'mmd_workspace_service_ready{{{labels},service="{name}"}} '
                       f'{1 if getattr(ws, f"{name}_installed") else 0}')
    for status, count in user_counts.items():
        out.append(f'mmd_users{{status="{status.value}"}} {count}')
    for state, count in workspace_counts.items():
        out.append(f'mmd_workspaces{{state="{state.value}"}} {count}')
    return out

def usage_metrics(db: Session) -> list[str]:
    """What the fleet is ACTUALLY consuming, plus the pool and the headroom.

    Everything here replaces a chart the admin pages used to draw themselves.
    The configured allowance was already exported; this is the other half - a
    tier of 2 GiB tells you nothing without the 300 MiB actually in use.

    CPU is exported as the raw monotonic counter rather than a computed rate.
    Prometheus is built to differentiate counters and handles restarts and
    gaps correctly; a rate computed here would be an average over whatever
    window happened to be handy, and wrong at every other window.
    """
    out: list[str] = []

    # The newest sample per workspace, in one query rather than one per row.
    newest = (select(UsageSample.workspace_id,
                     func.max(UsageSample.ts).label("ts"))
              .group_by(UsageSample.workspace_id).subquery())
    rows = db.execute(
        select(User.username, UsageSample.cpu_seconds_total,
               UsageSample.mem_bytes, UsageSample.ts, Workspace.state)
        .join(newest, (UsageSample.workspace_id == newest.c.workspace_id)
              & (UsageSample.ts == newest.c.ts))
        .join(Workspace, Workspace.id == UsageSample.workspace_id)
        .join(User, User.id == Workspace.user_id)).all()
    for username, cpu_seconds, mem_bytes, ts, state in rows:
        lab = f'{{username="{_metric_label(username)}"}}'
        running = state == WorkspaceState.ON
        # CPU is a COUNTER, so it keeps its last value while the machine is
        # off. That is correct and required: a counter must never decrease,
        # and `rate()` over a flat counter is already zero.
        out.append(f"mmd_workspace_cpu_seconds_total{lab} {cpu_seconds:g}")
        # Memory is a GAUGE, and a stopped machine is using none. Reporting
        # the last sample instead meant a workspace switched off yesterday
        # still claimed 429 MB today - a reading from whenever it last ran,
        # published as though it were current.
        out.append(f"mmd_workspace_memory_bytes{lab} {mem_bytes if running else 0}")
        # Only meaningful for a machine that is supposed to be sampling. For a
        # stopped one the age just counts up forever and says nothing, so it
        # is reported as -1: "not applicable" rather than a huge number that
        # looks like a fault.
        out.append(f"mmd_workspace_sample_age_seconds{lab} "
                   f"{_age_seconds(ts) if running else -1}")

    # Per-model AI usage, summed across a customer's sessions.
    #
    # These are the high-water marks the billing path already keeps, so they
    # are cumulative and monotonic - exactly what a Prometheus counter wants.
    # `increase(...[1h])` then answers "which models did this customer use in
    # the last hour", which a cumulative line cannot.
    #
    # Grouped in SQL rather than exported per session: a session id is
    # unbounded and would mint a series per conversation.
    rows = db.execute(
        select(User.username, AiUsageMark.service, AiUsageMark.model,
               func.sum(AiUsageMark.input_tokens),
               func.sum(AiUsageMark.output_tokens),
               func.sum(AiUsageMark.cache_read_tokens),
               func.sum(AiUsageMark.billed_micro))
        .join(Workspace, Workspace.id == AiUsageMark.workspace_id)
        .join(User, User.id == Workspace.user_id)
        .group_by(User.username, AiUsageMark.service, AiUsageMark.model)).all()
    for username, service, model, tin, tout, tcache, billed in rows:
        lab = (f'username="{_metric_label(username)}",'
               f'service="{_metric_label(service)}",'
               f'model="{_metric_label(model)}"')
        out.append(f'mmd_ai_tokens_total{{{lab},direction="input"}} {tin or 0}')
        out.append(f'mmd_ai_tokens_total{{{lab},direction="output"}} {tout or 0}')
        out.append(f'mmd_ai_tokens_total{{{lab},direction="cache_read"}} {tcache or 0}')
        out.append(f"mmd_ai_billed_micro_toman_total{{{lab}}} {billed or 0}")

    # Headroom: what the scheduler will and will not admit.
    try:
        from .scheduler.admission import host_capacity
        cap = host_capacity(svc.get_settings(db))
        running = svc.running_tiers(db)
        out.append(f"mmd_capacity_total_cores {cap.total_cores:g}")
        out.append(f"mmd_capacity_total_memory_gib {cap.total_mem_gib:g}")
        out.append(f"mmd_capacity_schedulable_cores {cap.schedulable_cores:g}")
        out.append(f"mmd_capacity_schedulable_memory_gib {cap.schedulable_mem_gib:g}")
        out.append(f"mmd_capacity_used_cores {sum(c for c, _ in running):g}")
        out.append(f"mmd_capacity_used_memory_gib {sum(m for _, m in running):g}")
        out.append(f"mmd_capacity_running {len(running)}")
    except Exception:  # noqa: BLE001
        # Capacity is derived from host inspection; a failure there must not
        # take the whole scrape down with it.
        pass

    # The ZFS pool, as the worker last measured it. Read from settings rather
    # than measured here: it costs a provisioner round trip.
    for key, name in (("pool_total_gib", "mmd_pool_total_gib"),
                      ("pool_used_gib", "mmd_pool_used_gib"),
                      ("pool_free_gib", "mmd_pool_free_gib")):
        row = db.scalar(select(Setting).where(Setting.key == key))
        try:
            out.append(f"{name} {float(row.value) if row and row.value else -1:g}")
        except (TypeError, ValueError):
            out.append(f"{name} -1")
    # Summed in Python: `disk_gib` is a derived property on the model, not a
    # column, so SQL cannot add it up.
    committed = sum(w.disk_gib for w in db.scalars(select(Workspace)))
    out.append(f"mmd_pool_committed_gib {committed:g}")
    return out

def operations_metrics(db: Session) -> list[str]:
    """Everything in the catalogue's 'Product operations' section that is a
    question about stored state rather than an event."""
    out: list[str] = []

    # Operations by kind and status, plus the queue depth that says whether
    # the worker is keeping up.
    rows = db.execute(
        select(Operation.kind, Operation.status, func.count(Operation.id))
        .group_by(Operation.kind, Operation.status)).all()
    for kind, status, count in rows:
        out.append(f'mmd_operations{{kind="{_metric_label(kind)}",'
                   f'status="{_metric_label(status)}"}} {count}')
    backlog = db.scalar(select(func.count(Operation.id))
                        .where(Operation.status.in_(("queued", "running")))) or 0
    out.append(f"mmd_operations_backlog {backlog}")

    # Support. `unread` and the age of the oldest open ticket are the two that
    # actually describe an SLO being missed.
    for status in TicketStatus:
        count = db.scalar(select(func.count(Ticket.id))
                          .where(Ticket.status == status)) or 0
        out.append(f'mmd_tickets{{status="{status.value}"}} {count}')
    oldest = db.scalar(select(func.min(Ticket.created_at))
                       .where(Ticket.status.notin_((TicketStatus.CLOSED,))))
    out.append("mmd_ticket_oldest_open_seconds "
               f"{_age_seconds(oldest)}")
    unread = db.scalar(select(func.count(Ticket.id))
                       .where(Ticket.staff_unread.is_(True))) or 0 \
        if hasattr(Ticket, "staff_unread") else 0
    out.append(f"mmd_tickets_unread_staff {unread}")

    # Notifications: created by kind, and how stale the oldest unread is.
    rows = db.execute(
        select(Notification.kind, func.count(Notification.id))
        .where(Notification.read_at.is_(None))
        .group_by(Notification.kind)).all()
    for kind, count in rows:
        out.append(f'mmd_notifications_unread{{kind="{_metric_label(kind)}"}} {count}')
    oldest_note = db.scalar(select(func.min(Notification.created_at))
                            .where(Notification.read_at.is_(None)))
    out.append(f"mmd_notification_oldest_unread_seconds {_age_seconds(oldest_note)}")

    # SMS outbox, which is now a delivery path worth watching.
    rows = db.execute(
        select(SmsMessage.kind, SmsMessage.status, func.count(SmsMessage.id))
        .group_by(SmsMessage.kind, SmsMessage.status)).all()
    for kind, status, count in rows:
        out.append(f'mmd_sms_messages{{kind="{_metric_label(kind)}",'
                   f'status="{_metric_label(status)}"}} {count}')

    # Backups: age is the number that matters. A backup system reports "on"
    # long after it has stopped producing anything.
    cfg = backuplib.config(db)
    out.append(f"mmd_backup_enabled {1 if cfg['enabled'] else 0}")
    out.append(f"mmd_backup_last_size_bytes {cfg['last_size']}")
    out.append(f"mmd_backup_failing {1 if cfg['last_error'] else 0}")
    last_ok = None
    if cfg["last_ok_at"]:
        try:
            last_ok = datetime.fromisoformat(cfg["last_ok_at"])
        except ValueError:
            last_ok = None
    out.append(f"mmd_backup_age_seconds {_age_seconds(last_ok)}")

    # The worker, as seen from outside it.
    for key, name in (("worker_heartbeat", "mmd_worker_heartbeat_age_seconds"),
                      ("worker_last_success", "mmd_worker_last_success_age_seconds")):
        row = db.scalar(select(Setting).where(Setting.key == key))
        when = None
        if row and row.value:
            try:
                when = datetime.fromisoformat(row.value)
            except ValueError:
                when = None
        out.append(f"{name} {_age_seconds(when)}")

    # Ledger and money, which the catalogue files under accounts but which is
    # the same kind of question: a stored aggregate.
    rows = db.execute(
        select(CreditTransaction.kind, func.count(CreditTransaction.id),
               func.coalesce(func.sum(CreditTransaction.amount_micro), 0))
        .group_by(CreditTransaction.kind)).all()
    for kind, count, total in rows:
        label = _metric_label(getattr(kind, "value", str(kind)))
        out.append(f'mmd_ledger_entries{{kind="{label}"}} {count}')
        out.append(f'mmd_ledger_micro_toman{{kind="{label}"}} {total}')

    # Schema patches that did not apply are silent by design; this makes them
    # loud. See db.py, where each patch runs in its own transaction.
    out.append(f"mmd_schema_patch_failures {int(_schema_failures())}")
    return out

def _age_seconds(when) -> float:
    if when is None:
        return -1.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - when).total_seconds())

def _schema_failures() -> int:
    from .db import SCHEMA_PATCH_FAILURES
    return len(SCHEMA_PATCH_FAILURES)

_UNIT_CACHE_SECONDS = 55.0
_unit_cache_at = 0.0
_unit_cache: dict[str, tuple[int, int]] = {}
_unit_cache_lock = threading.Lock()


def _unit_identities() -> dict[str, tuple[int, int]]:
    """Read stable service identifiers at most once per scrape interval."""
    global _unit_cache_at, _unit_cache
    now = time.monotonic()
    with _unit_cache_lock:
        if _unit_cache and now - _unit_cache_at < _UNIT_CACHE_SECONDS:
            return dict(_unit_cache)
        _unit_cache = {unit: (_unit_pid(unit), _unit_restarts(unit))
                       for unit in _UNITS}
        _unit_cache_at = now
        return dict(_unit_cache)


def process_metrics() -> list[str]:
    """CPU, memory, descriptors and uptime for each service.

    Read from /proc rather than through a library: /proc/<pid>/stat is
    world-readable, so the unprivileged API process can see the root
    provisioner's numbers without any new permission.
    """
    out: list[str] = []
    boot = m.boot_time()
    for unit, (pid, restarts) in _unit_identities().items():
        if not pid:
            out.append(f'mmd_process_up{{unit="{unit}"}} 0')
            continue
        sample = m.process_sample(pid)
        if sample is None:
            out.append(f'mmd_process_up{{unit="{unit}"}} 0')
            continue
        lab = f'{{unit="{unit}"}}'
        out.append(f"mmd_process_up{lab} 1")
        out.append(f"mmd_process_cpu_seconds_total{lab} {sample['cpu_seconds']:g}")
        out.append(f"mmd_process_resident_bytes{lab} {sample['rss_bytes']:g}")
        out.append(f"mmd_process_threads{lab} {sample['threads']}")
        out.append(f"mmd_process_open_fds{lab} {sample['open_fds']}")
        if boot:
            started = boot + sample["start_ticks"] / m.CLOCK_TICKS
            out.append(f"mmd_process_uptime_seconds{lab} "
                       f"{max(0.0, time.time() - started):g}")
        out.append(f"mmd_process_restarts_total{lab} {restarts}")
    return out

def _systemctl(unit: str, prop: str) -> str:
    try:
        return subprocess.run(["systemctl", "show", unit, "-p", prop, "--value"],
                              capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""

def _unit_pid(unit: str) -> int:
    try:
        return int(_systemctl(unit, "MainPID") or 0)
    except ValueError:
        return 0

def _unit_restarts(unit: str) -> int:
    try:
        return int(_systemctl(unit, "NRestarts") or 0)
    except ValueError:
        return 0
