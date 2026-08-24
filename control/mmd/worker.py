"""Billing and lifecycle worker.

Runs three loops:

  * meter   - scrape /1.0/metrics every 60s into usage_samples
  * settle  - at each hour boundary, charge the completed hour IN ARREARS,
              then apply the credit gate to the hour about to start
  * lifecycle - idle auto-stop, archive at zero credit, purge after retention

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

from sqlalchemy import select

from . import ports as portalloc
from . import service as svc
from .config import CONFIG
from .db import SessionLocal, init_db
from .incus.client import IncusClient, IncusConfig, IncusError
from .incus.metrics import MetricsClient
from .billing.pricing import MICRO
from .models import (ExposedPort, PortKind, UsageSample, Workspace,
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


# --- lifecycle -----------------------------------------------------------
async def lifecycle_once() -> None:
    now = svc.now()
    client = _incus()
    try:
        with SessionLocal() as db:
            for ws in db.scalars(select(Workspace)):
                # Idle auto-stop: protects the user's credit directly.
                #
                # `last_activity` only tracks the browser terminal. Someone
                # working over SSH or RDP never touches it, so acting on that
                # timestamp alone would switch the machine off underneath a
                # live session. Ask the machine before deciding.
                idle = (ws.state == WorkspaceState.ON and CONFIG.idle_stop_minutes > 0
                        and ws.last_activity
                        and now - ws.last_activity > timedelta(minutes=CONFIG.idle_stop_minutes))
                if idle and (ws.ssh_enabled or ws.rdp_enabled):
                    probe = svc.call_provisioner(
                        {"verb": "probe_sessions", "idx": ws.idx}, timeout=90)
                    if probe.get("active"):
                        log.info("%s is idle in the browser but has %s SSH / %s RDP "
                                 "session(s); not stopping",
                                 ws.incus_project, probe.get("ssh"), probe.get("rdp"))
                        ws.last_activity = now      # count it as activity
                        db.commit()
                        idle = False
                if idle:
                    log.info("idle-stopping %s", ws.incus_project)
                    try:
                        await client.stop(ws.instance, ws.incus_project)
                        ws.state = WorkspaceState.OFF
                        ws.desired_on = False
                        db.commit()
                    except IncusError as exc:
                        log.error("idle stop failed: %s", exc)

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
            # An address nothing answers on is not a reservation.
            svc.sync_published_ports(db)


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
                    continue
                if actually_on and ws.state == WorkspaceState.OFF:
                    # Running but the ledger says off - it is being billed as
                    # off. Stop it rather than give away compute.
                    log.warning("%s running but marked off; stopping", ws.incus_project)
                    try:
                        await client.stop(ws.instance, ws.incus_project)
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
            await meter_once()
            if tick % 5 == 0:
                await settle_once()
                await lifecycle_once()
            if tick % 15 == 0:
                await reconcile_once()
                reserve_service_ports_once()
        except Exception:  # noqa: BLE001
            log.exception("worker tick failed")
        tick += 1
        await asyncio.sleep(60)


if __name__ == "__main__":
    asyncio.run(main())
