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
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, delete, or_, select, update

from . import hermes
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
                     ExposedPort, Operation, PortKind, SshKey, Ticket,
                     TicketMessage, UsageSample, User, Workspace,
                     WorkspaceState)

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


# How often a metrics sample is taken. Was 60s, which gave a five-minute chart
# only five points - too coarse to read. The multipliers below are set so that
# settlement, lifecycle and reconciliation keep the cadence they have always
# had (5 and 15 minutes) rather than silently running three times as often.
TICK_SECONDS = 20
SETTLE_EVERY = 15          # 15 x 20s = 5 minutes
RECONCILE_EVERY = 45       # 45 x 20s = 15 minutes

# Samples are only used for drawing charts and for settling the CURRENT hour;
# once an hour is settled its charge is in the ledger and the raw samples are
# just history. Keeping them forever is how a table quietly becomes the biggest
# thing in the database - this one had no pruning at all.
SAMPLE_RETENTION_DAYS = 7

# AI tokens are pay-as-you-go and bill against the platform's own Claude
# subscription, so the gap between usage and payment is real exposure. Five
# minutes, matching the charge bucket in service.AI_PERIOD_SECONDS.
AI_EVERY = 15              # 15 x 20s = 5 minutes


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


HERMES_EVERY = 15          # 15 x 20s = 5 minutes, same cadence as Claude metering
POLICY_EVERY = 90          # 90 x 20s = 30 minutes; the catalogue changes slowly


def hermes_once(sync_policy: bool = False, meter_usage: bool = True) -> None:
    """Provision, cap, meter and revoke the OpenRouter side.

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
        rows = db.execute(select(Workspace).where(
            (Workspace.hermes_enabled.is_(True)) |
            (Workspace.hermes_key_hash.isnot(None)))).scalars().all()
        if not meter_usage:
            # Provisioning-only pass: keep just the workspaces with outstanding
            # work. Enabling is a click, and waiting five minutes for a key to
            # appear reads as broken - but polling OpenRouter once per workspace
            # every twenty seconds to discover there is nothing to do would be
            # rude to the supplier and slow. So the fast pass touches the
            # network ONLY when a toggle is actually outstanding.
            rows = [w for w in rows
                    if (w.hermes_enabled and not w.hermes_key_hash and
                        (svc.balance_micro(db, w.user_id) > 0
                         or not w.hermes_credit_blocked))
                    or (not w.hermes_enabled and w.hermes_key_hash)
                    or (w.hermes_enabled and w.hermes_key_hash and
                        w.hermes_credit_blocked !=
                        (svc.balance_micro(db, w.user_id) <= 0))]
        if not rows:
            return
        try:
            with OpenRouter(CONFIG.openrouter_key) as client:
                platform = hermes.ensure_platform(db, client)
                if sync_policy:
                    try:
                        n = hermes.sync_policy(db, client, platform)
                        log.info("hermes policy: %s models allowed", n)
                    except OpenRouterError as e:
                        log.warning("hermes policy sync failed: %s", e)

                usd_rate, discount = svc.ai_settings(db, hermes.SERVICE)
                for ws in rows:
                    try:
                        _hermes_workspace(db, ws, client, platform, usd_rate,
                                          discount, meter_usage)
                    except OpenRouterError as e:
                        # One customer's failure must not stop the others being
                        # metered - unbilled spend is the expensive outcome.
                        ws.hermes_error = str(e)[:500]
                        db.commit()
                        log.warning("hermes ws %s: %s", ws.id, e)
        except OpenRouterError as e:
            log.warning("hermes unavailable: %s", e)


def _hermes_workspace(db, ws: Workspace, client: OpenRouter,
                      platform, usd_rate: float, discount: float,
                      meter_usage: bool = True) -> None:
    if not ws.hermes_enabled:
        if ws.hermes_key_hash:
            # Meter the final spend BEFORE revoking. Deleting the key first
            # would destroy the only record of what was spent since the last
            # pass, and that spend has already left the operator's account.
            try:
                hermes.meter(db, ws, client.get_key(ws.hermes_key_hash), usd_rate, discount)
            except OpenRouterError as e:
                log.warning("hermes final meter ws %s: %s", ws.id, e)
            # Tear the dashboard down inside the machine too. Revoking the
            # key upstream is what stops the spending, but leaving a dead
            # dashboard listening - and the key file on disk - is untidy at
            # best and misleading at worst.
            if ws.state == WorkspaceState.ON:
                try:
                    svc.call_provisioner({"verb": "service_hermes", "idx": ws.idx,
                                          "action": "disable"}, timeout=180)
                except Exception as e:  # noqa: BLE001
                    log.warning("hermes teardown ws %s: %s", ws.id, e)
            hermes.revoke_key(db, ws, client)
            db.commit()
        return

    balance = svc.balance_micro(db, ws.user_id)
    credit_blocked = balance <= 0
    if not ws.hermes_key_hash and credit_blocked:
        # A zero-dollar OpenRouter limit is not a stop signal. Do not mint a
        # usable key until the account can pay for its first request.
        ws.hermes_credit_blocked = True
        ws.hermes_error = None
        db.commit()
        return
    cap = hermes.affordable_usd(balance, usd_rate, discount)

    if hermes.ensure_key(db, ws, client, platform, cap):
        db.commit()
        _hermes_install(db, ws)
        return

    if not ws.hermes_installed:
        # Retried on later passes: the usual reason it has not happened yet is
        # simply that the machine is off, and a customer who enables Hermes then
        # powers on should not have to toggle it again.
        _hermes_install(db, ws)

    if not meter_usage and ws.hermes_credit_blocked == credit_blocked:
        return
    info = client.get_key(ws.hermes_key_hash)
    hermes.meter(db, ws, info, usd_rate, discount)

    # Re-read the cap against the balance AFTER metering, so a customer who has
    # just spent down is capped at what is actually left.
    balance = svc.balance_micro(db, ws.user_id)
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
        client.update_key(ws.hermes_key_hash, limit_usd=cap,
                          disabled=credit_blocked)
    ws.hermes_credit_blocked = credit_blocked
    ws.hermes_error = None
    db.commit()


def _hermes_install(db, ws: Workspace) -> None:
    """Install the agent and start its dashboard inside the workspace.

    Only when the machine is running - there is nowhere to install to otherwise,
    and this is retried every pass, so a customer who enables Hermes while
    powered off gets it the moment they power on.
    """
    if ws.state != WorkspaceState.ON or not ws.hermes_key:
        return
    try:
        resp = svc.call_provisioner({
            "verb": "service_hermes", "idx": ws.idx, "action": "enable",
            "install": True, "api_key": ws.hermes_key,
            "model": hermes.default_model(db),
            "dash_user": ws.hermes_dash_user,
            "dash_password": ws.hermes_dash_password,
            "ip": svc.workspace_ip(ws)}, timeout=1800)
    except Exception as e:  # noqa: BLE001
        log.warning("hermes install ws %s: %s", ws.id, e)
        return
    if resp.get("ok"):
        ws.hermes_installed = True
        ws.hermes_error = None
        log.info("hermes dashboard up for ws %s", ws.id)
    else:
        # Surfaced to the customer rather than only logged: "enabled but the
        # dashboard never appeared" is otherwise indistinguishable from a hang.
        ws.hermes_error = (resp.get("error") or resp.get("output") or "install failed")[-300:]
        log.warning("hermes install ws %s failed: %s", ws.id, ws.hermes_error)
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

    if ws and ws.hermes_key_hash:
        oplib.progress(db, op, "revoking_ai")
        if not CONFIG.openrouter_key:
            raise RuntimeError("OpenRouter management key unavailable")
        with OpenRouter(CONFIG.openrouter_key) as client:
            hermes.revoke_key(db, ws, client, strict=True)
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

    targets = [user.email]
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
    ws.hermes_installed = False
    ws.error = None
    db.commit()
    svc.audit(db, op.actor_id, "reset_done", ws.incus_project)
    oplib.finish(db, op, svc.now())


def _install_packages(db, op: Operation, ws: Workspace) -> None:
    if ws.state != WorkspaceState.ON:
        oplib.fail(db, op, "machine_off", svc.now())
        return
    packages = list((op.detail or {}).get("packages") or [])
    oplib.progress(db, op, "installing_packages")
    resp = svc.call_provisioner({"verb": "install_packages", "idx": ws.idx,
                                 "packages": packages}, timeout=900)
    if not resp.get("ok"):
        oplib.fail(db, op, "install_failed", svc.now())
        return
    svc.audit(db, op.actor_id, "packages_installed", ws.incus_project,
              presets=(op.detail or {}).get("presets") or [], count=len(packages))
    oplib.finish(db, op, svc.now())


async def operations_once() -> None:
    """Advance one durable operation; running work is safe to retry."""
    with SessionLocal() as db:
        # Failed account erasure is special: unlike a failed reset, it cannot be
        # abandoned because external resources may still exist. Retry it, but
        # only after ordinary queued work so one unavailable upstream does not
        # hold every customer's operation behind it forever.
        retryable_cleanup = (Operation.kind == "account_delete") & (
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
            if op.kind == "install_packages":
                _install_packages(db, op, ws)
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


async def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    init_db()
    log.info("worker started")
    tick = 0
    reserve_service_ports_once()
    await reconcile_once()
    while True:
        try:
            await operations_once()
            await meter_once()
            if tick % AI_EVERY == 0:
                meter_ai_once()
            # Provisioning is checked every tick so a toggle takes seconds;
            # metering stays on the five-minute cadence money moves at.
            if tick % HERMES_EVERY == 0:
                hermes_once(sync_policy=(tick % POLICY_EVERY == 0))
            else:
                hermes_once(meter_usage=False)
            # Every tick, not every settlement: a machine that should have
            # stopped at 12h00m must not keep billing until the next five-minute
            # boundary. The query is indexed and matches almost nothing.
            await auto_stop_once()
            if tick % SETTLE_EVERY == 0:
                await settle_once()
                await lifecycle_once()
                prune_samples_once()
            if tick % RECONCILE_EVERY == 0:
                await reconcile_once()
                reserve_service_ports_once()
        except Exception:  # noqa: BLE001
            log.exception("worker tick failed")
        tick += 1
        await asyncio.sleep(TICK_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
