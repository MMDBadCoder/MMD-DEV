"""Billing and lifecycle worker.

Runs three loops:

  * meter   - scrape /1.0/metrics every TICK_SECONDS into usage_samples
  * settle  - at each hour boundary, charge the completed hour IN ARREARS,
              then apply the credit gate to the hour about to start
  * lifecycle - archive at zero credit, purge after retention. NOTHING here
    stops a funded workspace; only running out of credit does.

Reconciliation, not scheduling, is the design here: the worker compares what
the database says should be true against what Incus reports and closes the
gap. That is also how a host reboot is handled - Incus is told
boot.autostart=false for every workspace, so nothing comes back on its own and
the worker restores the intended set only after re-checking credit and
capacity. A workspace the user deliberately powered off to stop spending never
silently comes back.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import time
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, delete, func, or_, select, update

from . import backup as backuplib
from . import metrics as m
from . import sms as smslib
from . import hermes
from . import notifications
from . import operations as oplib
from . import ports as portalloc
from . import service as svc
from .config import CONFIG
from .db import SessionLocal, init_db
from .incus.client import IncusClient, IncusConfig, IncusError
from .incus.metrics import MetricsClient
from .openrouter import OpenRouter, OpenRouterError
from .billing.pricing import MICRO
from .models import (AiUsageMark, AuditLog, CreditAccount, CreditTransaction,
                     ExposedPort, Notification, Operation, PortKind, SshKey, Ticket,
                     Setting, TicketMessage, UsageSample, User, UserStatus,
                     OpenRouterAccount, Workspace, WorkspaceState)

UTC = timezone.utc
log = logging.getLogger("mmd.worker")


def _incus() -> IncusClient:
    return IncusClient(IncusConfig(CONFIG.incus_url, CONFIG.incus_client_cert,
                                   CONFIG.incus_client_key, CONFIG.incus_server_cert))


def _metrics() -> MetricsClient:
    return MetricsClient(CONFIG.incus_metrics_url, CONFIG.metrics_cert,
                         CONFIG.metrics_key, CONFIG.incus_server_cert)


hour_floor = svc.hour_floor


# --- metering ------------------------------------------------------------
async def meter_once() -> None:
    m = _metrics()
    try:
        sample = await m.sample()
    except Exception as exc:  # noqa: BLE001
        log.warning("metrics scrape failed: %s", exc)
        return
    finally:
        await m.aclose()

    with SessionLocal() as db:
        for ws in db.scalars(select(Workspace).where(Workspace.state == WorkspaceState.ON)):
            v = sample.get((ws.incus_project, ws.instance))
            if v is None:
                continue
            db.add(UsageSample(workspace_id=ws.id, ts=svc.now(),
                               cpu_seconds_total=v["cpu_seconds"],
                               mem_bytes=int(v["mem_bytes"])))
        db.commit()


def usage_for_period(db, ws: Workspace, start: datetime, end: datetime) -> tuple[float, float]:
    """Consumed CPU core-hours and mean memory GiB-hours over a window.

    CPU comes from the delta of a monotonic counter; memory is the mean of the
    gauge samples. If a workspace restarted mid-window the counter resets, so a
    negative delta is clamped to zero rather than credited.
    """
    rows = list(db.scalars(
        select(UsageSample)
        .where(UsageSample.workspace_id == ws.id,
               UsageSample.ts >= start, UsageSample.ts <= end)
        .order_by(UsageSample.ts)))
    if len(rows) < 2:
        return 0.0, 0.0
    cpu_delta = rows[-1].cpu_seconds_total - rows[0].cpu_seconds_total
    if cpu_delta < 0:
        cpu_delta = 0.0
    span_h = max((rows[-1].ts - rows[0].ts).total_seconds() / 3600.0, 1e-6)
    mean_mem_gib = sum(r.mem_bytes for r in rows) / len(rows) / 1073741824.0
    return cpu_delta / 3600.0, mean_mem_gib * span_h


# --- settlement + the pre-hour gate --------------------------------------
async def settle_once() -> None:
    now = svc.now()
    this_hour = hour_floor(now)
    client = _incus()
    try:
        with SessionLocal() as db:
            for ws in db.scalars(select(Workspace).where(
                    Workspace.state.in_([WorkspaceState.ON, WorkspaceState.OFF,
                                         WorkspaceState.ARCHIVED]))):
                start = ws.period_start or hour_floor(ws.created_at or now)
                if hour_floor(start) >= this_hour:
                    continue  # the hour in progress is not yet payable

                period = hour_floor(start)
                powered = ws.state == WorkspaceState.ON
                cpu_h, mem_h = (usage_for_period(db, ws, period, period + timedelta(hours=1))
                                if powered else (0.0, 0.0))
                charged = svc.settle_period(db, ws, period, powered_on=powered,
                                            cpu_core_hours=cpu_h, mem_gib_hours=mem_h)
                if charged:
                    log.info("settled %s hour %s: %.4f credits",
                             ws.incus_project, period.isoformat(), charged / MICRO)
                ws.period_start = this_hour
                db.commit()

                # The gate for the hour that is now beginning.
                if ws.state == WorkspaceState.ON:
                    affordable, have, need = svc.can_afford_next_hour(db, ws)
                    if not affordable:
                        log.info("stopping %s: balance %.2f < required %.2f",
                                 ws.incus_project, have / MICRO, need / MICRO)
                        try:
                            await client.stop(ws.instance, ws.incus_project)
                            ws.state = WorkspaceState.OFF
                            ws.desired_on = False
                            _text(db, ws.user, "stopped_no_credit",
                                  dedupe_key=f"nocredit:{ws.id}:{this_hour:%Y%m%d%H}")
                            db.commit()
                        except IncusError as exc:
                            log.error("stop failed for %s: %s", ws.incus_project, exc)
    finally:
        await client.aclose()


# --- the automatic stop --------------------------------------------------
async def auto_stop_once() -> None:
    """Switch off machines that have reached the end of their run.

    Deliberately its OWN pass, not a branch inside lifecycle_once. The stops in
    that pass are all consequences of an empty balance; this one is a schedule
    the customer agreed to when they started the machine. Keeping them apart is
    what lets `tests/test_auto_stop.py` state, and check, that the credit path
    has not quietly grown a second reason to stop a funded workspace.

    Note what is NOT here: any notion of idleness. The deadline is measured from
    power-on, so a machine running a twelve-hour build and a machine nobody has
    touched are treated identically - which is the point. The predecessor to
    this feature tried to tell them apart from `last_activity` and killed real
    work; see docs/DECISIONS.md.
    """
    client = _incus()
    try:
        with SessionLocal() as db:
            for ws in db.scalars(select(Workspace).where(
                    Workspace.state == WorkspaceState.ON,
                    Workspace.auto_stop_at.isnot(None))):
                if not svc.due_for_auto_stop(ws):
                    continue
                log.info("auto-stopping %s: run reached its %dh limit",
                         ws.incus_project, CONFIG.auto_stop_hours)
                try:
                    await client.stop(ws.instance, ws.incus_project)
                except IncusError as exc:
                    # Left armed on purpose: the next pass tries again. A
                    # machine that failed to stop is still over its limit.
                    log.error("auto-stop failed for %s: %s", ws.incus_project, exc)
                    continue
                # Settle the part-hour before the state changes, exactly as the
                # customer pressing Stop would - otherwise the elapsed minutes
                # are billed at the next boundary as though it were still on.
                svc.settle_elapsed(db, ws, powered_on=True)
                ws.state = WorkspaceState.OFF
                ws.desired_on = False
                ws.period_start = None
                svc.disarm_auto_stop(ws)
                _text(db, ws.user, "auto_stopped",
                      dedupe_key=f"autostop:{ws.id}:{svc.now():%Y%m%d%H%M}")
                db.commit()
                svc.audit(db, None, "auto_stop", ws.incus_project,
                          hours=CONFIG.auto_stop_hours)
    finally:
        await client.aclose()


# --- lifecycle -----------------------------------------------------------
async def lifecycle_once() -> None:
    now = svc.now()
    client = _incus()
    try:
        with SessionLocal() as db:
            for ws in db.scalars(select(Workspace)):
                # NOTHING here stops a funded workspace. There used to be an
                # idle auto-stop - 60 minutes without browser-terminal activity
                # and the machine was switched off "to protect the customer's
                # credit". It was removed on the operator's instruction, and it
                # deserved to be: `last_activity` only ever tracked the browser
                # terminal, so a customer running a long build, working in the
                # file manager, or serving traffic on a published port read as
                # idle and had their machine killed mid-work. Billing is hourly,
                # so a machine left running is simply paid for - that is the
                # customer's decision to make, not ours.
                #
                # The blocks below act only when an account has run out of
                # credit. A funded workspace is never touched.

                # Archive at zero credit: frees the CPU, memory AND the disk
                # reservation, while keeping the tenant's data recoverable.
                if (ws.state in (WorkspaceState.OFF, WorkspaceState.ON)
                        and svc.balance_micro(db, ws.user_id) <= 0):
                    log.info("archiving %s (credit exhausted)", ws.incus_project)
                    ws.state = WorkspaceState.ARCHIVING
                    db.commit()
                    resp = svc.call_provisioner({"verb": "archive", "idx": ws.idx})
                    if resp.get("ok"):
                        ws.state = WorkspaceState.ARCHIVED
                        ws.archived_at = now
                        ws.purge_after = now + timedelta(days=CONFIG.archive_retention_days)
                    else:
                        # Not implemented yet -> leave it off rather than
                        # pretending it was archived.
                        ws.state = WorkspaceState.OFF
                        ws.error = resp.get("error")
                    db.commit()

                # Permanent deletion after the retention window.
                if (ws.state == WorkspaceState.ARCHIVED and ws.purge_after
                        and now > ws.purge_after):
                    log.warning("purging %s (retention expired)", ws.incus_project)
                    resp = svc.call_provisioner({"verb": "destroy", "idx": ws.idx})
                    if resp.get("ok"):
                        ws.state = WorkspaceState.DELETED
                        db.commit()
    finally:
        await client.aclose()


# The cheap intent pass runs every five seconds so a paid OpenRouter key gains
# headroom before the customer can reasonably return to their external client.
# Metrics retain their measured 20-second resolution; coupling these clocks
# would quadruple the sample table merely to make one supplier update faster.
LOOP_SECONDS = 5
METRICS_EVERY = 4          # 4 x 5s = 20 seconds
TICK_SECONDS = 20          # public metrics cadence, imported conceptually by API
SETTLE_EVERY = 60          # 60 x 5s = 5 minutes
RECONCILE_EVERY = 180      # 180 x 5s = 15 minutes

# Samples are only used for drawing charts and for settling the CURRENT hour;
# once an hour is settled its charge is in the ledger and the raw samples are
# just history. Keeping them forever is how a table quietly becomes the biggest
# thing in the database - this one had no pruning at all.
# Two days, not seven. Settlement only ever reads the hour it is closing, so
# everything older exists for CHARTS - and the admin charts now come from
# Prometheus. Two days covers the widest window a customer can ask for on
# their own machine page (24 hours) with a day of margin.
#
# This table was 96% of the database at seven days, growing ~8,000 rows a day.
SAMPLE_RETENTION_DAYS = 2

# AI tokens are pay-as-you-go and bill against the platform's own Claude
# subscription, so the gap between usage and payment is real exposure. Five
# minutes, matching the charge bucket in service.AI_PERIOD_SECONDS.
AI_EVERY = 60              # 60 x 5s = 5 minutes


def meter_ai_once() -> None:
    """Charge every running workspace for the AI tokens it has used.

    Pay-as-you-go, every AI_EVERY ticks. Running often is the point: the
    platform's own Claude subscription is what is being spent, so the window
    between a customer using tokens and paying for them is the window in which
    an empty account can keep spending. Five minutes bounds that.

    A workspace that is off cannot be scanned and cannot be using anything, so
    it is skipped rather than treated as an error.
    """
    with SessionLocal() as db:
        for ws in db.scalars(select(Workspace).where(
                Workspace.state == WorkspaceState.ON)):
            resp = svc.call_provisioner({"verb": "ai_usage", "idx": ws.idx}, timeout=300)
            if not resp.get("ok"):
                log.warning("ai usage scan failed for %s: %s",
                            ws.incus_project, str(resp.get("error"))[:200])
                continue
            try:
                out = svc.meter_ai_usage(db, ws, resp)
            except Exception:  # noqa: BLE001
                log.exception("ai metering failed for %s", ws.incus_project)
                db.rollback()
                continue
            if out.get("charged_micro"):
                log.info("%s: AI usage %.2f Toman (%s)", ws.incus_project,
                         out["charged_micro"] / MICRO,
                         ", ".join(f"{m} {v:.2f}" for m, v in out["models"].items()))
            if out.get("unpriced"):
                log.warning("%s: no price set for model(s) %s - tokens left uncounted "
                            "until one is", ws.incus_project, ", ".join(out["unpriced"]))


def meter_codex_once() -> None:
    """The same pass as meter_ai_once, for the other host-authenticated CLI.

    Separate rather than a loop over both services because the two can fail
    independently: a Codex scanner error must not stop Claude being billed, and
    the log line has to name which supplier went wrong.

    Codex starts with NO prices configured, so until an operator sets them this
    charges nothing and reports the models it saw as unpriced. That is the
    deliberate direction to fail in - tokens are held uncounted and bill
    correctly once a price exists, rather than being billed at a rate nobody
    chose or given away silently.
    """
    with SessionLocal() as db:
        for ws in db.scalars(select(Workspace).where(
                Workspace.state == WorkspaceState.ON)):
            resp = svc.call_provisioner({"verb": "codex_usage", "idx": ws.idx},
                                        timeout=300)
            if not resp.get("ok"):
                log.warning("codex usage scan failed for %s: %s",
                            ws.incus_project, str(resp.get("error"))[:200])
                continue
            try:
                out = svc.meter_ai_usage(db, ws, resp, service=svc.CODEX_SERVICE)
            except Exception:  # noqa: BLE001
                log.exception("codex metering failed for %s", ws.incus_project)
                db.rollback()
                continue
            if out.get("charged_micro"):
                log.info("%s: Codex usage %.2f Toman (%s)", ws.incus_project,
                         out["charged_micro"] / MICRO,
                         ", ".join(f"{m} {v:.2f}" for m, v in out["models"].items()))
            if out.get("unpriced"):
                log.warning("%s: no Codex price set for model(s) %s - tokens left "
                            "uncounted until one is", ws.incus_project,
                            ", ".join(out["unpriced"]))


HERMES_EVERY = 60          # 60 x 5s = 5 minutes, same cadence as Claude metering


def hermes_once(meter_usage: bool = True) -> None:
    """Provision, cap and meter every approved customer's OpenRouter key.

    Reconciliation rather than event handling, for the same reason power state
    is: the customer's toggle records intent in the database, and this closes
    the gap against OpenRouter. A failed mint is retried next pass instead of
    leaving a workspace permanently half-enabled because one HTTP call lost a
    race with a restart.

    Runs ONLY here. mmd-api has no access to the management key.
    """
    if not CONFIG.openrouter_key:
        return
    with SessionLocal() as db:
        users = list(db.scalars(select(User).where(User.status == UserStatus.APPROVED)))
        accounts = []
        for user in users:
            account = db.get(OpenRouterAccount, user.id)
            if account is None:
                account = OpenRouterAccount(user_id=user.id, credit_blocked=True,
                                            limit_dirty=True)
                db.add(account)
                db.flush()
            accounts.append(account)
        db.commit()
        if not meter_usage:
            accounts = [a for a in accounts if not a.key_hash or a.limit_dirty
                        or a.credit_blocked != (svc.balance_micro(db, a.user_id) <= 0)]
        workspaces = list(db.scalars(select(Workspace).where(or_(
            Workspace.hermes_enabled.is_(True),
            Workspace.hermes_installed.is_(True)))))
        if not accounts and not workspaces:
            return
        try:
            with OpenRouter(CONFIG.openrouter_key) as client:
                # Existing keys were minted into the old guarded workspace.
                # Clearing its allowlist preserves every exposed secret while
                # making the migration effective immediately. New keys are
                # minted without a workspace and never enter this path.
                if hermes._get(db, hermes.SETTING_GUARDRAIL_REMOVED) != "1":
                    guardrail_id = (hermes._get(db, hermes.SETTING_GUARDRAIL_ID)
                                    or hermes._get(db, hermes.LEGACY_GUARDRAIL_ID))
                    if guardrail_id:
                        client.clear_model_restrictions(guardrail_id)
                    hermes._set(db, hermes.SETTING_GUARDRAIL_REMOVED, "1")
                    db.commit()
                    log.info("OpenRouter model restrictions removed")

                usd_rate, discount = svc.ai_settings(db, hermes.SERVICE)
                for account in accounts:
                    try:
                        _openrouter_account(db, account, client, usd_rate,
                                            discount, meter_usage)
                    except OpenRouterError as e:
                        # One customer's failure must not stop the others being
                        # metered - unbilled spend is the expensive outcome.
                        account.error = str(e)[:500]
                        db.commit()
                        log.warning("OpenRouter user %s: %s", account.user_id, e)
                for ws in workspaces:
                    _hermes_install(db, ws)
        except OpenRouterError as e:
            log.warning("hermes unavailable: %s", e)


def _openrouter_account(db, account: OpenRouterAccount, client: OpenRouter,
                        usd_rate: float, discount: float,
                        meter_usage: bool = True) -> None:
    balance = svc.balance_micro(db, account.user_id)
    credit_blocked = balance <= 0
    cap = hermes.affordable_usd(balance, usd_rate, discount)
    if hermes.ensure_key(db, account, client, cap):
        # The key exists from approval even at zero credit, but is unusable
        # until funds arrive. This gives the customer one stable credential.
        if credit_blocked:
            client.update_key(account.key_hash, limit_usd=cap, disabled=True)
        account.credit_blocked = credit_blocked
        account.limit_dirty = False
        account.limit_usd = cap
        account.limit_synced_at = svc.now()
        account.error = None
        db.commit()
        return
    if (not meter_usage and not account.limit_dirty
            and account.credit_blocked == credit_blocked):
        return
    info = client.get_key(account.key_hash)
    if meter_usage:
        hermes.meter(db, account, info, usd_rate, discount)

    # Re-read the cap against the balance AFTER metering, so a customer who has
    # just spent down is capped at what is actually left.
    balance = svc.balance_micro(db, account.user_id)
    credit_blocked = balance <= 0
    # OpenRouter limits are cumulative for the lifetime of a key. New headroom
    # starts after the spend already reported upstream; otherwise a topped-up
    # key can remain blocked because its historical usage exceeds its new cap.
    cap = float(info.usage_usd or 0.0) + hermes.affordable_usd(
        balance, usd_rate, discount)
    limit_changed = (info.limit_usd is None
                     or abs(float(info.limit_usd) - cap) > 0.01)
    if limit_changed or info.disabled != credit_blocked:
        # The cap is the hard stop: OpenRouter refuses the request when the
        # customer runs out, instead of us noticing minutes later and billing
        # for spend that has already happened.
        client.update_key(account.key_hash, limit_usd=cap,
                          disabled=credit_blocked)
    # Only on the transition. The flag is recomputed every pass, so texting on
    # the value rather than the change would repeat for as long as the balance
    # stayed at zero.
    if credit_blocked and not account.credit_blocked:
        _text(db, account.user, "key_blocked",
              dedupe_key=f"keyblocked:{account.user_id}:{svc.now():%Y%m%d%H}")
    account.credit_blocked = credit_blocked
    account.limit_dirty = False
    account.limit_usd = cap
    account.limit_synced_at = svc.now()
    account.error = None
    db.commit()


def _hermes_install(db, ws: Workspace) -> None:
    """Install the agent and start its dashboard inside the workspace.

    Only when the machine is running - there is nowhere to install to otherwise,
    and this is retried every pass, so a customer who enables Hermes while
    powered off gets it the moment they power on.
    """
    if not ws.hermes_enabled:
        if ws.state == WorkspaceState.ON:
            resp = svc.call_provisioner({"verb": "service_hermes", "idx": ws.idx,
                                         "action": "disable"}, timeout=180)
            if not resp.get("ok"):
                ws.hermes_error = (resp.get("error") or "disable failed")[-300:]
                db.commit()
                return
        ws.hermes_installed = False
        ws.hermes_vhost_ready = False
        ws.hermes_telegram_installed = False
        ws.hermes_error = None
        db.commit()
        return
    account = db.get(OpenRouterAccount, ws.user_id)
    if ws.state != WorkspaceState.ON or account is None or not account.key:
        return
    if not ws.hermes_dash_user:
        ws.hermes_dash_user = ws.user.username if ws.user else f"user{ws.user_id}"
    if not ws.hermes_dash_password:
        ws.hermes_dash_password = hermes.make_password()
    db.commit()
    try:
        resp = svc.call_provisioner({
            "verb": "service_hermes", "idx": ws.idx, "action": "enable",
            "install": not ws.hermes_installed, "api_key": account.key,
            "model": hermes.default_model(db),
            "dash_user": ws.hermes_dash_user,
            "dash_password": ws.hermes_dash_password,
            "telegram_enabled": ws.hermes_telegram_enabled,
            # Account defaults remain available after the one-shot delivery
            # field is cleared, so a gateway repair can always be reconciled.
            "telegram_token": ((ws.user.telegram_bot_token if ws.user else None)
                               or ws.hermes_telegram_token),
            "telegram_users": ((ws.user.telegram_user_id if ws.user else None)
                               or ws.hermes_telegram_users),
            "ip": svc.workspace_ip(ws)}, timeout=1800)
    except Exception as e:  # noqa: BLE001
        log.warning("hermes install ws %s: %s", ws.id, e)
        return
    if resp.get("ok"):
        ws.hermes_installed = True
        ws.hermes_telegram_installed = bool(resp.get("telegram_installed"))
        ws.hermes_telegram_token = None
        ws.hermes_telegram_error = None
        ws.hermes_error = None
        log.info("hermes dashboard up for ws %s", ws.id)
    else:
        # Surfaced to the customer rather than only logged: "enabled but the
        # dashboard never appeared" is otherwise indistinguishable from a hang.
        ws.hermes_error = (resp.get("error") or resp.get("output") or "install failed")[-300:]
        if ws.hermes_telegram_enabled:
            ws.hermes_telegram_error = ws.hermes_error
        log.warning("hermes install ws %s failed: %s", ws.id, ws.hermes_error)
    db.commit()


GIB = 1024 ** 3


async def disk_once() -> None:
    """Sample every workspace's real disk use, warn its owner, guard the pool.

    Three jobs, one ZFS pass, because they need the same numbers.

    Why the pool guard exists at all
    --------------------------------
    Every workspace has a hard `refquota`, so none of them can overrun its own
    allowance - a customer who fills up gets write errors inside their own
    machine and nobody else notices. What the quota does NOT bound is the SUM:
    once reservations are dropped the allowances are deliberately overcommitted,
    and enough customers filling up at once exhausts the pool.

    A full ZFS pool is not a polite failure. It does not fall on the tenant who
    caused it - every workspace loses writes simultaneously, and on this host
    the control plane's own PostgreSQL is on the same disk. So the guard stops
    the largest consumers to buy space back, which is a bad outcome deliberately
    chosen over a worse one.

    Stopping is ordered by CONSUMPTION, not by who grew last: the point is to
    free the most space with the fewest machines stopped, and "most recently
    grown" would punish activity rather than size.
    """
    resp = svc.call_provisioner({"verb": "disk_usage", "idx": 1}, timeout=120)
    if not resp.get("ok"):
        log.warning("disk usage scan failed: %s", str(resp.get("error"))[:200])
        return

    per_ws = resp.get("workspaces") or {}
    pool = resp.get("pool") or {}
    free_gib = (pool.get("available") or 0) / GIB
    used_gib = (pool.get("used") or 0) / GIB
    # Persisted for the exporter. The pool figures come from a provisioner
    # call, which is far too heavy to make on every 60-second scrape; this
    # pass already has them, so it writes them down instead.
    with SessionLocal() as sdb:
        _set_setting(sdb, "pool_free_gib", f"{free_gib:.3f}")
        _set_setting(sdb, "pool_used_gib", f"{used_gib:.3f}")
        _set_setting(sdb, "pool_total_gib", f"{free_gib + used_gib:.3f}")
        sdb.commit()

    with SessionLocal() as db:
        rows = list(db.scalars(select(Workspace)))
        by_idx = {str(w.idx): w for w in rows}

        for idx, d in per_ws.items():
            ws = by_idx.get(idx)
            if ws is None:
                continue
            used = int(d.get("root_used") or 0) + int(d.get("docker_used") or 0)
            ws.disk_used_mib = used // (1024 * 1024)
            ws.disk_checked_at = svc.now()

            limit_mib = ws.disk_gib * 1024
            pct = (ws.disk_used_mib / limit_mib * 100) if limit_mib else 0
            key = f"disk:{ws.id}"
            if pct >= CONFIG.disk_warn_percent:
                first_time = db.scalar(select(Notification).where(
                    Notification.user_id == ws.user_id,
                    Notification.dedupe_key == key,
                    Notification.resolved_at.is_(None))) is None
                notifications.emit(
                    db, user_id=ws.user_id, kind="disk", code="disk_nearly_full",
                    severity="warn" if pct < 100 else "error",
                    detail={"percent": round(pct), "used_mib": ws.disk_used_mib,
                            "limit_gib": ws.disk_gib},
                    href="/console/files", dedupe_key=key)
                # Once per crossing, like low credit: a customer parked at 90%
                # must not be texted on every disk pass.
                if first_time:
                    _text(db, ws.user, "disk_high",
                          dedupe_key=f"{key}:{svc.now():%Y%m%d%H%M%S}")
            else:
                notifications.resolve(db, ws.user_id, key, svc.now())
        db.commit()

        if free_gib >= CONFIG.pool_floor_gib:
            return

        # --- the pool is low ------------------------------------------------
        log.error("pool free %.1f GiB is below the %.1f GiB floor - stopping "
                  "the largest running workspaces", free_gib, CONFIG.pool_floor_gib)
        text_admins(db, "admin_pool_low", detail={"free": f"{free_gib:.1f}"},
                    dedupe_key=f"poollow:{svc.now():%Y%m%d%H}")
        running = sorted(
            (w for w in rows if w.state == WorkspaceState.ON),
            key=lambda w: -(w.disk_used_mib or 0))

        client = _incus()
        try:
            for ws in running:
                if free_gib >= CONFIG.pool_floor_gib:
                    break
                try:
                    await client.stop(ws.instance, ws.incus_project)
                except IncusError as exc:
                    log.error("pool guard could not stop %s: %s",
                              ws.incus_project, exc)
                    continue
                svc.settle_elapsed(db, ws, powered_on=True)
                ws.state = WorkspaceState.OFF
                ws.desired_on = False
                ws.period_start = None
                svc.disarm_auto_stop(ws)
                notifications.emit(
                    db, user_id=ws.user_id, kind="disk", code="stopped_pool_full",
                    severity="error",
                    detail={"used_mib": ws.disk_used_mib, "limit_gib": ws.disk_gib},
                    href="/console/files", dedupe_key=f"poolstop:{ws.id}")
                _text(db, ws.user, "pool_stopped",
                      dedupe_key=f"poolstop:{ws.id}:{svc.now():%Y%m%d%H}")
                svc.audit(db, None, "disk_pool_stop", ws.incus_project,
                          used_mib=ws.disk_used_mib, pool_free_gib=round(free_gib, 1))
                db.commit()
                log.warning("stopped %s to protect the pool (%d MiB used)",
                            ws.incus_project, ws.disk_used_mib or 0)
                # Stopping does not itself free space; it stops the machine
                # WRITING more. Re-read rather than assume a figure.
                again = svc.call_provisioner({"verb": "disk_usage", "idx": 1},
                                             timeout=120)
                free_gib = ((again.get("pool") or {}).get("available") or 0) / GIB
        finally:
            await client.aclose()


def openclaw_once() -> None:
    """Make each workspace match what its customer asked for.

    The same shape as the Hermes pass and for the same reason: installing means
    an npm download and a service start inside the workspace, which is minutes
    of work. Retried every pass, so a customer who enables it while powered off
    gets it the moment they power on, and a failed install heals itself.
    """
    with SessionLocal() as db:
        for ws in db.scalars(select(Workspace)):
            want = bool(ws.openclaw_enabled)
            if not want and not ws.openclaw_installed:
                continue
            if ws.state != WorkspaceState.ON:
                continue

            if not want:
                resp = svc.call_provisioner({"verb": "ai_openclaw", "idx": ws.idx,
                                             "action": "disable"}, timeout=300)
                if resp.get("ok"):
                    ws.openclaw_installed = False
                    ws.openclaw_password = None
                    ws.openclaw_telegram_installed = False
                    ws.openclaw_error = None
                    ws.openclaw_telegram_error = None
                    log.info("openclaw removed for ws %s", ws.id)
                    db.commit()
                continue

            # The password is part of "installed", not a detail beside it: it is
            # what the config was written with and what the customer signs in
            # with, so a row that has one without the other is not finished.
            #
            # These two CAN disagree. Disabling clears the password immediately
            # so the interface stops showing a secret, while `installed` is only
            # cleared once the worker has actually removed the service - so a
            # customer who switches off and straight back on lands here with
            # installed=True and no password. Checking `installed` alone made
            # that state skip forever: the gateway was never reinstalled, no
            # password was ever generated, and the page said "installing" for
            # good.
            if (ws.openclaw_installed and ws.openclaw_password
                    and not ws.openclaw_error
                    # A Telegram toggle changes the config, so it has to
                    # reconcile like any other intent rather than being skipped
                    # as "already installed".
                    and ws.openclaw_telegram_installed == bool(ws.openclaw_telegram_enabled)):
                continue
            account = db.get(OpenRouterAccount, ws.user_id)
            if account is None or not account.key:
                # It spends the managed OpenRouter key. Waiting is right: the
                # key arrives on its own reconciler and this pass runs again.
                continue

            # Generated once and kept, so the address a customer wrote down
            # keeps working across a repair or a re-install.
            if not ws.openclaw_password:
                ws.openclaw_password = secrets.token_urlsafe(18)
                db.commit()

            try:
                from . import openclaw as oclib
                from . import usernames as unames
                resp = svc.call_provisioner({
                    "verb": "ai_openclaw", "idx": ws.idx, "action": "install",
                    "openrouter_key": account.key,
                    "password": ws.openclaw_password,
                    # Without a default model the gateway starts and quietly
                    # answers with whatever OpenClaw's own default is, which
                    # may not be reachable with this customer's key.
                    "model": oclib.default_model(db),
                    "telegram_enabled": bool(ws.openclaw_telegram_enabled),
                    # The account-level token, the same one Hermes reuses. A
                    # customer sets it once under Account and both services
                    # take it from there.
                    "telegram_token": (ws.user.telegram_bot_token
                                       if ws.user else None),
                    # Who may talk to the bot. Without it the provisioner
                    # refuses to enable the channel rather than configuring a
                    # bot that answers any Telegram user who finds it - and
                    # this one is wired to an agent with a shell in the
                    # customer's workspace.
                    "telegram_users": (ws.user.telegram_user_id
                                       if ws.user else None),
                    # The gateway rejects any browser Origin it was not told
                    # about, so the address we publish and the address it
                    # accepts have to be the same string.
                    "origin": "https://" + unames.openclaw_host(
                        ws.user.username, CONFIG.domain)},
                    timeout=1800)
            except Exception as e:  # noqa: BLE001
                log.warning("openclaw install ws %s: %s", ws.id, e)
                continue

            if resp.get("ok"):
                ws.openclaw_installed = True
                # Reported by the machine, not assumed from the request: the
                # difference between "we asked for Telegram" and "Telegram is
                # running" is the whole reason this is a separate column.
                ws.openclaw_telegram_installed = bool(resp.get("telegram"))
                ws.openclaw_error = None
                # The gateway came up but the channel did not. Left unsaid,
                # this reconciles forever behind a "preparing" pill, because
                # enabled and installed never converge and nothing explains
                # why. Hermes reports this; so does OpenClaw now.
                ws.openclaw_telegram_error = (
                    (resp.get("telegram_error") or "telegram channel failed")[-300:]
                    if ws.openclaw_telegram_enabled
                    and not ws.openclaw_telegram_installed else None)
                log.info("openclaw gateway up for ws %s", ws.id)
            else:
                # Surfaced to the customer rather than only logged: "enabled but
                # the dashboard never appeared" is otherwise indistinguishable
                # from a hang.
                ws.openclaw_installed = False
                ws.openclaw_error = (resp.get("error") or resp.get("output")
                                     or "install failed")[-300:]
                log.warning("openclaw install ws %s failed: %s", ws.id, ws.openclaw_error)
            db.commit()


def managed_web_once() -> None:
    """Reconcile the two OpenRouter-backed web applications."""
    with SessionLocal() as db:
        for ws in db.scalars(select(Workspace)):
            if ws.state != WorkspaceState.ON:
                continue
            account = db.get(OpenRouterAccount, ws.user_id)
            for name in ("opencode", "openwebui"):
                enabled = bool(getattr(ws, f"{name}_enabled"))
                installed = bool(getattr(ws, f"{name}_installed"))
                if not enabled and not installed:
                    continue
                if not enabled:
                    setattr(ws, f"{name}_vhost_ready", False)
                    resp = svc.call_provisioner({"verb": "ai_managed_web", "idx": ws.idx,
                                                 "service": name, "action": "disable"}, timeout=300)
                    if resp.get("ok"):
                        setattr(ws, f"{name}_installed", False)
                        setattr(ws, f"{name}_password", None)
                        setattr(ws, f"{name}_error", None)
                        db.commit()
                    continue
                # Open WebUI's Python backend is killed by the kernel while
                # starting in a 1 GiB workspace. Do not publish a permanently
                # restarting container as ready or let it consume the machine.
                # Intent stays enabled, so a memory resize heals it automatically.
                if name == "openwebui" and ws.mem_mib < 2048:
                    setattr(ws, f"{name}_vhost_ready", False)
                    if installed:
                        svc.call_provisioner({"verb": "ai_managed_web", "idx": ws.idx,
                                              "service": name, "action": "disable"},
                                             timeout=300)
                    setattr(ws, f"{name}_installed", False)
                    setattr(ws, f"{name}_error", "insufficient memory")
                    db.commit()
                    continue
                if installed and not getattr(ws, f"{name}_error"):
                    continue
                if account is None or not account.key:
                    continue
                password = getattr(ws, f"{name}_password")
                if not password:
                    password = secrets.token_urlsafe(18)
                    setattr(ws, f"{name}_password", password)
                    db.commit()
                try:
                    resp = svc.call_provisioner({"verb": "ai_managed_web", "idx": ws.idx,
                        "service": name, "action": "install", "openrouter_key": account.key,
                        "password": password,
                        "admin_email": f"{ws.user.username}@mmd.local",
                        # The one model every OpenRouter-backed service starts
                        # on, chosen on the OpenRouter tab. The provisioner
                        # reshapes it per service.
                        "model": hermes.default_model(db)}, timeout=1800)
                except Exception as exc:  # noqa: BLE001
                    log.warning("%s install ws %s: %s", name, ws.id, exc)
                    continue
                setattr(ws, f"{name}_installed", bool(resp.get("ok")))
                setattr(ws, f"{name}_error", None if resp.get("ok") else
                        (resp.get("error") or resp.get("output") or "install failed")[-300:])
                db.commit()


def prune_samples_once() -> None:
    cutoff = svc.now() - timedelta(days=SAMPLE_RETENTION_DAYS)
    with SessionLocal() as db:
        n = db.query(UsageSample).filter(UsageSample.ts < cutoff).delete(
            synchronize_session=False)
        db.commit()
        if n:
            log.info("pruned %d usage samples older than %d days",
                     n, SAMPLE_RETENTION_DAYS)


def reserve_service_ports_once() -> None:
    """Ensure every workspace holds its two permanent addresses.

    The Connections page promises an SSH and an RDP port reserved from the
    moment the machine exists. Doing this only when someone opens the page
    would make the reservation a consequence of looking at it - and a machine
    provisioned before this existed would sit with blank addresses until its
    owner happened to visit. The API self-heals too; this is what makes the
    invariant true without anyone present.

    Idempotent: reserve_service_ports returns an existing reservation rather
    than replacing it, so an address a customer has already written down is
    never moved.
    """
    with SessionLocal() as db:
        fixed = []
        for ws in db.scalars(select(Workspace).where(
                Workspace.state.notin_([WorkspaceState.DELETED,
                                        WorkspaceState.DELETING]))):
            have = db.scalars(select(ExposedPort).where(
                ExposedPort.workspace_id == ws.id,
                ExposedPort.kind.in_([PortKind.SSH, PortKind.RDP]))).all()
            if len(have) < 2:
                portalloc.reserve_service_ports(db, ws.id)
                db.commit()
                fixed.append(ws.incus_project)
        if fixed:
            log.info("reserved service ports for %s", ", ".join(fixed))
        # Pushed on EVERY pass, not only when something changed. The provisioner
        # rewrites the whole rule set from the mappings it is handed, so this is
        # idempotent - and it is the only thing that removes rules belonging to a
        # workspace that has been destroyed. Deleting a workspace cascades its
        # port rows away but nothing re-synced the firewall, so DNAT entries for
        # a machine that no longer exists sat there until the next unrelated
        # port change.
        svc.sync_published_ports(db)


# --- durable operations -------------------------------------------------
def _purge_account(db, op: Operation) -> None:
    """Remove one account only after every external resource is gone.

    Ordering is the safety property: revoke spend, remove public reachability,
    destroy compute, then erase database identity. A failure before the last
    step leaves enough state for the next worker pass to retry.
    """
    user = db.get(User, op.user_id) if op.user_id else None
    if user is None:
        db.delete(op)
        db.commit()
        return
    ws = db.scalar(select(Workspace).where(Workspace.user_id == user.id))

    account = db.get(OpenRouterAccount, user.id)
    if account and account.key_hash:
        oplib.progress(db, op, "revoking_ai")
        if not CONFIG.openrouter_key:
            raise RuntimeError("OpenRouter management key unavailable")
        with OpenRouter(CONFIG.openrouter_key) as client:
            hermes.revoke_key(db, account, client, strict=True)
        db.commit()

    if ws:
        oplib.progress(db, op, "removing_addresses")
        fw = svc.sync_published_ports(db, exclude_workspace_id=ws.id)
        if not fw.get("ok"):
            raise RuntimeError("could not remove published-port rules")

        oplib.progress(db, op, "destroying_machine")
        resp = svc.call_provisioner({"verb": "destroy", "idx": ws.idx})
        if not resp.get("ok"):
            raise RuntimeError("could not destroy workspace")

    oplib.progress(db, op, "erasing_account")
    ticket_ids = list(db.scalars(select(Ticket.id).where(Ticket.user_id == user.id)))
    if ticket_ids:
        db.execute(delete(TicketMessage).where(TicketMessage.ticket_id.in_(ticket_ids)))
    # An administrator may have written in somebody else's thread. The user's
    # request is full erasure, so authorship is removed there as well.
    db.execute(delete(TicketMessage).where(TicketMessage.author_id == user.id))
    db.execute(delete(Ticket).where(Ticket.user_id == user.id))

    if ws:
        for model in (UsageSample, ExposedPort, SshKey, AiUsageMark):
            db.execute(delete(model).where(model.workspace_id == ws.id))
        db.execute(delete(CreditTransaction).where(
            CreditTransaction.workspace_id == ws.id))
    db.execute(delete(CreditTransaction).where(CreditTransaction.user_id == user.id))
    db.execute(delete(CreditAccount).where(CreditAccount.user_id == user.id))
    db.execute(delete(Notification).where(Notification.user_id == user.id))

    targets = [user.username]
    if ws:
        targets.append(ws.incus_project)
    db.execute(delete(AuditLog).where(or_(
        AuditLog.actor_id == user.id, AuditLog.target.in_(targets))))
    # A deleted administrator must not remain as another account's approver.
    db.execute(update(User).where(User.approved_by == user.id).values(approved_by=None))

    # Operations are operational state, not a historical tombstone. Delete all
    # of them, including this one, before deleting the workspace and account.
    db.execute(delete(Operation).where(Operation.user_id == user.id))
    if ws:
        db.execute(delete(Operation).where(Operation.workspace_id == ws.id))
        db.delete(ws)
        db.flush()
    db.delete(user)
    db.commit()


async def _factory_reset(db, op: Operation, ws: Workspace) -> None:
    # The API and worker are separate processes. Always begin from the state
    # the API committed instead of trusting an identity-map copy that may have
    # been loaded before the reset request.
    db.refresh(ws)
    # Reset means optional AI is no longer selected, regardless of whether the
    # image rebuild later succeeds. Clear intent first so another worker pass
    # cannot mint a replacement key after this one has been revoked.
    ws.hermes_enabled = False
    # OpenClaw goes with it, and for a sharper reason: it lives entirely inside
    # the filesystem about to be destroyed, and it holds a copy of the
    # OpenRouter key that is revoked below. Leaving the flags set would have the
    # worker reinstall it on the rebuilt machine with a key that no longer
    # exists, and present the result as ready.
    ws.openclaw_enabled = False
    ws.openclaw_installed = False
    ws.openclaw_password = None
    ws.openclaw_error = None
    for name in ("opencode", "openwebui"):
        setattr(ws, f"{name}_enabled", False)
        setattr(ws, f"{name}_installed", False)
        setattr(ws, f"{name}_vhost_ready", False)
        setattr(ws, f"{name}_password", None)
        setattr(ws, f"{name}_error", None)
    db.commit()
    oplib.progress(db, op, "rebuilding_machine")
    detail = op.detail or {}
    resp = svc.call_provisioner({
        "verb": "reset", "idx": ws.idx,
        "cores": detail.get("cores", max(1, round(ws.cpu_milli / 1000))),
        "mem_mib": detail.get("mem_mib", ws.mem_mib),
        "root_gib": detail.get("root_gib", ws.root_gib),
        "docker_gib": detail.get("docker_gib", ws.docker_gib)}, timeout=1800)
    if not resp.get("ok"):
        ws.state = WorkspaceState.ERROR
        ws.error = (resp.get("error") or resp.get("output", ""))[-500:]
        oplib.fail(db, op, "reset_failed", svc.now())
        svc.audit(db, op.actor_id, "reset_failed", ws.incus_project)
        return

    oplib.progress(db, op, "stopping_machine")
    client = _incus()
    try:
        await client.stop(ws.instance, ws.incus_project)
    except Exception:  # noqa: BLE001
        ws.state = WorkspaceState.ERROR
        ws.error = "post-reset stop did not complete"
        oplib.fail(db, op, "reset_stop_failed", svc.now())
        return
    finally:
        await client.aclose()

    ws.state = WorkspaceState.OFF
    ws.desired_on = False
    ws.period_start = None
    ws.started_at = None
    ws.last_activity = None
    svc.disarm_auto_stop(ws)
    ws.ssh_enabled = False
    ws.ssh_keys = None
    ws.rdp_enabled = False
    ws.rdp_installed = False
    # Reset is a new machine, not a request to reinstall optional AI software.
    # The account-level OpenRouter key deliberately survives this operation.
    ws.hermes_installed = False
    ws.hermes_vhost_ready = False
    # Legacy duplicates migrated to OpenRouterAccount at startup. Clear them
    # from the rebuilt machine row without touching the account credential.
    ws.hermes_key = None
    ws.hermes_key_hash = None
    ws.hermes_credit_blocked = False
    ws.hermes_limit_dirty = False
    ws.hermes_telegram_enabled = False
    ws.hermes_telegram_installed = False
    ws.hermes_telegram_token = None
    ws.hermes_telegram_users = None
    ws.hermes_telegram_error = None
    ws.hermes_dash_user = None
    ws.hermes_dash_password = None
    ws.hermes_error = None
    ws.error = None
    db.commit()
    svc.audit(db, op.actor_id, "reset_done", ws.incus_project)
    oplib.finish(db, op, svc.now())


async def _workspace_create(db, op: Operation, ws: Workspace) -> None:
    """Materialise the optional machine requested by an approved customer."""
    oplib.progress(db, op, "creating_machine")
    resp = svc.call_provisioner({
        "verb": "provision", "idx": ws.idx,
        "cores": max(1, round(ws.cpu_milli / 1000)), "mem_mib": ws.mem_mib,
        "root_gib": ws.root_gib, "docker_gib": ws.docker_gib}, timeout=1800)
    if not resp.get("ok"):
        ws.state = WorkspaceState.ERROR
        ws.error = (resp.get("error") or resp.get("output", ""))[-500:]
        oplib.fail(db, op, "provision_failed", svc.now())
        return
    oplib.progress(db, op, "stopping_machine")
    client = _incus()
    try:
        await client.stop(ws.instance, ws.incus_project)
    except Exception:  # noqa: BLE001
        ws.state = WorkspaceState.ERROR
        ws.error = "post-provision stop did not complete"
        oplib.fail(db, op, "provision_stop_failed", svc.now())
        return
    finally:
        await client.aclose()
    ws.state = WorkspaceState.OFF
    ws.desired_on = False
    ws.error = None
    portalloc.reserve_service_ports(db, ws.id)
    db.commit()
    svc.sync_published_ports(db)
    svc.audit(db, op.actor_id, "workspace_create_done", ws.incus_project)
    oplib.finish(db, op, svc.now())


def _workspace_delete(db, op: Operation, ws: Workspace) -> None:
    """Remove every machine-bound resource but keep account-level products."""
    oplib.progress(db, op, "removing_addresses")
    fw = svc.sync_published_ports(db, exclude_workspace_id=ws.id)
    if not fw.get("ok"):
        raise RuntimeError("could not remove published-port rules")
    oplib.progress(db, op, "destroying_machine")
    resp = svc.call_provisioner({"verb": "destroy", "idx": ws.idx})
    if not resp.get("ok"):
        raise RuntimeError("could not destroy workspace")
    target = ws.incus_project
    for model in (UsageSample, ExposedPort, SshKey, AiUsageMark):
        db.execute(delete(model).where(model.workspace_id == ws.id))
    db.execute(update(CreditTransaction).where(
        CreditTransaction.workspace_id == ws.id).values(workspace_id=None))
    op.workspace_id = None
    db.delete(ws)
    db.commit()
    svc.audit(db, op.actor_id, "workspace_delete_done", target)
    oplib.finish(db, op, svc.now())


async def operations_once() -> None:
    """Advance one durable operation; running work is safe to retry."""
    with SessionLocal() as db:
        # Failed account erasure is special: unlike a failed reset, it cannot be
        # abandoned because external resources may still exist. Retry it, but
        # only after ordinary queued work so one unavailable upstream does not
        # hold every customer's operation behind it forever.
        retryable_cleanup = (Operation.kind.in_(("account_delete", "workspace_delete"))) & (
            Operation.status == "failed")
        op = db.scalar(select(Operation).where(or_(
            Operation.status.in_(("queued", "running")), retryable_cleanup))
            .order_by(case((Operation.status == "failed", 1), else_=0),
                      Operation.id).limit(1))
        if op is None:
            return
        try:
            if op.kind == "account_delete":
                _purge_account(db, op)
                return                    # the operation is erased with account
            ws = db.get(Workspace, op.workspace_id) if op.workspace_id else None
            if ws is None:
                oplib.fail(db, op, "no_workspace", svc.now())
                return
            if op.kind == "factory_reset":
                await _factory_reset(db, op, ws)
                return
            if op.kind == "workspace_create":
                await _workspace_create(db, op, ws)
                return
            if op.kind == "workspace_delete":
                _workspace_delete(db, op, ws)
                return
            oplib.fail(db, op, "unknown_operation", svc.now())
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            current = db.get(Operation, op.id)
            if current is not None:
                # Failed cleanups stay visible to the admin and are retried
                # after ordinary queued work. The progress code says where the
                # failure occurred without exposing exception prose.
                current.status = "failed"
                current.error_code = "cleanup_failed"
                current.finished_at = svc.now()
                db.commit()
            log.exception("operation %s (%s) failed", op.id, op.kind)


async def reconcile_once() -> None:
    """Make the database and Incus agree about what is running."""
    client = _incus()
    try:
        with SessionLocal() as db:
            # ERROR, STARTING and STOPPING are included deliberately. A power
            # change that raised - a timeout on a slow stop, say - leaves the
            # workspace parked in one of them, and reconciling only ON and OFF
            # meant nothing ever moved it back: the customer saw a permanently
            # broken machine and had no control that could fix it. Incus knows
            # what is actually running; that is the authority worth trusting
            # here, and a transient failure should heal itself within a tick.
            for ws in db.scalars(select(Workspace).where(
                    Workspace.state.in_([WorkspaceState.ON, WorkspaceState.OFF,
                                         WorkspaceState.ERROR,
                                         WorkspaceState.STARTING,
                                         WorkspaceState.STOPPING]))):
                try:
                    st = await client.state(ws.instance, ws.incus_project)
                except IncusError:
                    continue
                actually_on = st.get("status") == "Running"

                if ws.state in (WorkspaceState.ERROR, WorkspaceState.STARTING,
                                WorkspaceState.STOPPING):
                    log.info("%s was %s; Incus reports %s - adopting that",
                             ws.incus_project, ws.state.value,
                             "running" if actually_on else "stopped")
                    ws.state = WorkspaceState.ON if actually_on else WorkspaceState.OFF
                    ws.error = None
                    # Billing follows the observed state, not the stuck one: a
                    # machine recorded as running must have a period to bill
                    # from, and a stopped one must not keep accruing.
                    if actually_on and ws.period_start is None:
                        ws.period_start = svc.now()
                    if not actually_on:
                        ws.period_start = None
                    db.commit()
                if actually_on and (ws.state == WorkspaceState.OFF
                                    or not ws.desired_on):
                    # Running but the ledger says off - it is being billed as
                    # off, or a requested stop failed. Stop it rather than give
                    # away compute or leave a reset/provision machine running.
                    log.warning("%s running against desired state; stopping",
                                ws.incus_project)
                    try:
                        await client.stop(ws.instance, ws.incus_project)
                        ws.state = WorkspaceState.OFF
                        ws.period_start = None
                        svc.disarm_auto_stop(ws)
                        db.commit()
                    except IncusError:
                        pass
                elif not actually_on and ws.state == WorkspaceState.ON:
                    # Off but the ledger says on - after a host reboot, say.
                    # Restore it only if it is still affordable and there is room.
                    affordable, _, _ = svc.can_afford_next_hour(db, ws)
                    adm = svc.check_admission(db, ws)
                    if ws.desired_on and affordable and adm.allowed:
                        log.info("restoring %s after downtime", ws.incus_project)
                        try:
                            await client.start(ws.instance, ws.incus_project)
                            # A restored machine is a NEW run, so it gets a new
                            # deadline. Without this a machine that was opted
                            # out before a host reboot would come back with no
                            # deadline at all and never stop.
                            svc.arm_auto_stop(ws)
                        except IncusError:
                            ws.state = WorkspaceState.OFF
                    else:
                        ws.state = WorkspaceState.OFF
                    db.commit()
    finally:
        await client.aclose()


def _text(db, user, kind: str, detail: dict | None = None,
          dedupe_key: str | None = None) -> None:
    """Queue one customer message, honouring their preferences.

    Wrapped because every worker event needs the same three things - a user
    that may be missing, a preference check, and a failure that must not stop
    the pass it is running inside.
    """
    if user is None:
        return
    try:
        smslib.queue(db, user_id=user.id, phone=user.phone, kind=kind,
                     detail=detail, user=user, dedupe_key=dedupe_key)
    except smslib.SmsError as e:
        log.warning("sms %s not queued for %s: %s", kind, user.username, e)


def low_credit_once() -> None:
    """Text a customer once, the first time their balance falls low.

    ONCE PER CROSSING, not once per pass. The arming state is the notification
    itself: an unresolved `low_credit` row means this customer has already been
    told and is still low, so nothing is sent. Topping up resolves it, which
    re-arms the warning for the next time. Without that, a customer sitting at
    40,000 Toman would be texted every five seconds.

    Deliberately fires ABOVE zero. At zero the machine has already been stopped
    and the supplier key already blocked - a warning that arrives then is a
    receipt, not a warning.
    """
    threshold = CONFIG.sms_low_credit_toman * MICRO
    if threshold <= 0:
        return
    now = svc.now()
    with SessionLocal() as db:
        for user in db.scalars(select(User).where(
                User.status == UserStatus.APPROVED)):
            balance = svc.balance_micro(db, user.id)
            key = f"lowcredit:{user.id}"
            if 0 < balance <= threshold:
                already = db.scalar(select(Notification).where(
                    Notification.user_id == user.id,
                    Notification.dedupe_key == key,
                    Notification.resolved_at.is_(None)))
                if already is not None:
                    continue
                note = notifications.emit(
                    db, user_id=user.id, kind="credit", code="low_credit",
                    severity="warn", detail={"balance_micro": balance},
                    href="/console/billing", dedupe_key=key)
                try:
                    # Keyed off the notification's creation time, which `emit`
                    # resets each time the condition re-activates. A wall-clock
                    # stamp would collide when a customer crosses the threshold
                    # twice inside one second and silently drop the second
                    # message - which is exactly what the re-arm test caught.
                    smslib.queue(db, user_id=user.id, phone=user.phone,
                                 kind="low_credit", user=user,
                                 dedupe_key=f"{key}:{note.created_at.isoformat()}")
                except smslib.SmsError as e:
                    log.warning("low-credit sms not queued for %s: %s",
                                user.username, e)
                db.commit()
            elif balance > threshold:
                notifications.resolve(db, user.id, key, now)
                db.commit()


def _admins(db):
    return list(db.scalars(select(User).where(
        User.is_admin.is_(True), User.status == UserStatus.APPROVED)))


def text_admins(db, kind: str, detail: dict | None = None,
                dedupe_key: str | None = None) -> None:
    """Operator alerts go to every administrator, not a configured number.

    The number that matters is whoever is on call, and a single hard-coded
    recipient goes stale the moment that person leaves.
    """
    for admin in _admins(db):
        try:
            smslib.queue(db, user_id=admin.id, phone=admin.phone, kind=kind,
                         detail=detail, user=admin,
                         dedupe_key=(f"{dedupe_key}:{admin.id}"
                                     if dedupe_key else None))
        except smslib.SmsError:
            continue
    db.commit()


def credit_step_once() -> None:
    """Text once when a customer's configured integer balance band changes.

    The watermark moves in the same transaction as the outbox insert. A crash
    therefore leaves either both changes or neither, so retry cannot duplicate
    a message. First sight and step edits establish a silent baseline.
    """
    with SessionLocal() as db:
        user_ids = db.scalars(select(User.id).where(
            User.status == UserStatus.APPROVED)).all()
        for user_id in user_ids:
            user = db.scalar(select(User).where(
                User.id == user_id).with_for_update())
            if user is None or user.status != UserStatus.APPROVED:
                db.rollback()
                continue
            step_toman = (user.sms_credit_step_toman
                          or smslib.DEFAULT_CREDIT_STEP_TOMAN)
            step_micro = step_toman * MICRO
            balance = svc.balance_micro(db, user.id)
            band = balance // step_micro
            previous = user.sms_credit_band
            if previous is None:
                user.sms_credit_band = band
                db.commit()
                continue
            if band == previous:
                continue
            user.sms_credit_band = band
            _text(db, user, "credit_step",
                  detail={"increased": band > previous,
                          "balance": f"{round(balance / MICRO):,}"})
            db.commit()


def _setting(db, key: str, default: str = "") -> str:
    row = db.scalar(select(Setting).where(Setting.key == key))
    return row.value if row and row.value is not None else default


def _set_setting(db, key: str, value: str) -> None:
    row = db.scalar(select(Setting).where(Setting.key == key))
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
    db.flush()


def heartbeat_once(duration: float = 0.0, failed: bool = False) -> None:
    """Record that the worker is alive, for the watchdog and the exporter.

    Written by the loop it proves, which is the point: a heartbeat produced by
    anything else would keep ticking through exactly the stall it exists to
    detect.

    The metric snapshot rides along because the worker is a separate process
    from the exporter. One scrape target, two processes.
    """
    m.gauge("mmd_worker_last_tick_seconds", duration)
    m.inc("mmd_worker_ticks_total", {"result": "failed" if failed else "ok"})
    with SessionLocal() as db:
        now = svc.now()
        _set_setting(db, "worker_heartbeat", now.isoformat())
        if not failed:
            _set_setting(db, "worker_last_success", now.isoformat())
        db.commit()
    m.write_snapshot()


def sms_once() -> None:
    """Send whatever is queued in the SMS outbox.

    Only this process holds the provider key, so this is the only place a
    message can actually leave. Failures are recorded on the row and retried
    with backoff rather than raised - one unreachable number must not stop the
    rest of the queue.
    """
    if not CONFIG.kavenegar_key:
        return
    with SessionLocal() as db:
        for row in smslib.due(db):
            smslib.deliver(db, row)


async def backup_once() -> None:
    """Send a database backup if one is owed.

    The due check is a settings read, so it runs every tick and the configured
    interval is honoured to within a tick rather than rounded up to the next
    settlement. The backup itself is a dump plus an upload - seconds to minutes
    of blocking work - so it goes to a thread; running it inline would stall
    metering, auto-stop and every reconciler behind it.
    """
    with SessionLocal() as db:
        if not backuplib.due(db):
            return
    # A fresh session inside the thread: this one is used from another thread
    # and outlives the check above by however long the upload takes.
    def _run() -> None:
        with SessionLocal() as db:
            result = backuplib.run_once(db)
            # A backup that fails silently is the exact thing backups exist to
            # prevent, so the operator hears about it on the same pass.
            if not result.get("ok"):
                text_admins(db, "admin_backup_failed",
                            dedupe_key=f"backupfail:{svc.now():%Y%m%d}")

    await asyncio.to_thread(_run)


# Same reason as the API's seeds: a failure counter that has never fired must
# render as zero, not as an absent series.
m.inc("mmd_worker_tick_failures_total", None, 0)
m.inc("mmd_worker_ticks_total", {"result": "failed"}, 0)


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    init_db()
    log.info("worker started")
    tick = 0
    reserve_service_ports_once()
    await reconcile_once()
    while True:
        tick_started = time.monotonic()
        tick_failed = False
        try:
            await operations_once()
            if tick % METRICS_EVERY == 0:
                await meter_once()
            if tick % AI_EVERY == 0:
                meter_ai_once()
                meter_codex_once()
            # Provisioning is checked every tick so a toggle takes seconds;
            # metering stays on the five-minute cadence money moves at.
            if tick % HERMES_EVERY == 0:
                hermes_once()
            else:
                hermes_once(meter_usage=False)
            # Same cadence as the Hermes provisioning check: a toggle should
            # take seconds to be picked up, and the pass is a no-op for every
            # workspace that has not asked for it.
            openclaw_once()
            managed_web_once()
            # Every tick, not every settlement: a machine that should have
            # stopped at 12h00m must not keep billing until the next five-minute
            # boundary. The query is indexed and matches almost nothing.
            await auto_stop_once()
            # Every tick for the same reason as auto-stop: the interval is the
            # admin's, and a backup set to 15 minutes should not wait for a
            # five-minute settlement boundary. Almost every call returns
            # immediately from the due check.
            await backup_once()
            # Cheap when idle: one indexed query that matches nothing. Kept on
            # every tick so an approved customer is texted in seconds, which is
            # while they are still looking at the page that told them to wait.
            # Settlement cadence, not every tick: a balance only moves when an
            # hour is charged, so checking faster would re-read the ledger for
            # every customer to find nothing changed.
            if tick % SETTLE_EVERY == 0:
                await asyncio.to_thread(low_credit_once)
            await asyncio.to_thread(
                heartbeat_once, time.monotonic() - tick_started, tick_failed)
            # Every settlement, not every tick: a ZFS pass over the pool is
            # cheap but not free, and disk fills over minutes, not seconds.
            if tick % SETTLE_EVERY == 0:
                await disk_once()
            if tick % SETTLE_EVERY == 0:
                await settle_once()
                await lifecycle_once()
                prune_samples_once()
            # Credit can change through an admin grant, AI metering, or machine
            # settlement. Checking after every money-producing pass gives an
            # external OpenRouter user a notification within one worker tick.
            await asyncio.to_thread(credit_step_once)
            # Drain after every producer above, especially the credit-band
            # comparison, so a detected crossing reaches Kavenegar in this
            # tick rather than waiting for the next one.
            await asyncio.to_thread(sms_once)
            if tick % RECONCILE_EVERY == 0:
                await reconcile_once()
                reserve_service_ports_once()
        except Exception:  # noqa: BLE001
            tick_failed = True
            m.inc("mmd_worker_tick_failures_total")
            log.exception("worker tick failed")
        m.observe("mmd_worker_tick_seconds", time.monotonic() - tick_started)
        tick += 1
        await asyncio.sleep(LOOP_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
