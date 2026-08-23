"""Business logic shared by the API, the worker and the provisioner client.

Kept out of the route handlers so the worker enforces exactly the same rules on
its own schedule - above all the credit gate, which must be applied both when a
user presses Power and at every hour boundary.

No cost arithmetic lives here. Money comes from billing.pricing, which is the
only module allowed to compute it.
"""
from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .billing import pricing
from .billing.pricing import MICRO, Rates, Tier
from .config import CONFIG
from .models import (AuditLog, CreditAccount, CreditTransaction, ExposedPort,
                     Setting, TxKind, User, Workspace, WorkspaceState)
from .scheduler.admission import can_start, host_capacity

UTC = timezone.utc


def now() -> datetime:
    return datetime.now(UTC)


def hour_floor(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


# --- settings ------------------------------------------------------------
def get_settings(db: Session) -> dict[str, str]:
    return {s.key: s.value for s in db.scalars(select(Setting))}


def rates(db: Session) -> Rates:
    return Rates.from_settings(get_settings(db))


def tier_of(ws: Workspace) -> Tier:
    return Tier(cpu_milli=ws.cpu_milli, mem_mib=ws.mem_mib, disk_gib=ws.disk_gib)


def port_count(db: Session, ws: Workspace) -> int:
    return db.scalar(select(func.count(ExposedPort.id))
                     .where(ExposedPort.workspace_id == ws.id)) or 0


# --- credit --------------------------------------------------------------
def balance_micro(db: Session, user_id: int) -> int:
    acct = db.get(CreditAccount, user_id)
    return acct.balance_micro if acct else 0


def post_transaction(db: Session, *, user_id: int, workspace_id: int | None,
                     kind: TxKind, amount_micro: int,
                     period_start: datetime | None = None,
                     detail: dict | None = None) -> CreditTransaction | None:
    """Post a ledger entry and move the balance.

    Returns None when this exact (workspace, period, kind) was already posted -
    the unique constraint turns a retry after a worker crash into a no-op
    rather than a second charge.
    """
    tx = CreditTransaction(
        user_id=user_id, workspace_id=workspace_id, kind=kind,
        amount_micro=amount_micro, period_start=period_start, detail=detail or {})
    db.add(tx)
    acct = db.get(CreditAccount, user_id)
    if acct is None:
        acct = CreditAccount(user_id=user_id, balance_micro=0)
        db.add(acct)
    acct.balance_micro += amount_micro
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return None
    return tx


# --- the credit gate -----------------------------------------------------
def gate_micro(db: Session, ws: Workspace) -> int:
    return pricing.max_hour_micro(tier_of(ws), rates(db),
                                  port_count=port_count(db, ws))


def can_afford_next_hour(db: Session, ws: Workspace) -> tuple[bool, int, int]:
    need = gate_micro(db, ws)
    have = balance_micro(db, ws.user_id)
    return have >= need, have, need


# --- admission -----------------------------------------------------------
def running_tiers(db: Session, exclude_ws_id: int | None = None) -> list[tuple[float, float]]:
    q = select(Workspace).where(Workspace.state.in_(
        [WorkspaceState.ON, WorkspaceState.STARTING]))
    return [(w.cpu_cores, w.mem_gib) for w in db.scalars(q) if w.id != exclude_ws_id]


def check_admission(db: Session, ws: Workspace, tier: Tier | None = None):
    t = tier or tier_of(ws)
    return can_start(host_capacity(get_settings(db)),
                     running_tiers(db, exclude_ws_id=ws.id),
                     t.cpu_cores, t.mem_gib)


# --- provisioner IPC -----------------------------------------------------
def call_provisioner(payload: dict, timeout: float = 900.0) -> dict:
    """Ask the root provisioner to act. The only privileged path in the app."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(CONFIG.provisioner_socket)
        s.sendall((json.dumps(payload) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode() or "{}")
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"provisioner unreachable: {exc}"}
    finally:
        s.close()


# --- settlement ----------------------------------------------------------
def settle_period(db: Session, ws: Workspace, period_start: datetime, *,
                  powered_on: bool, cpu_core_hours: float = 0.0,
                  mem_gib_hours: float = 0.0, fraction: float = 1.0,
                  tier: Tier | None = None) -> int:
    """Charge one period, in arrears. Idempotent per (workspace, period, kind).

    `tier` may be passed explicitly so a size change can settle the elapsed
    part of the hour at the size that was actually in force for it.
    """
    amount, detail = pricing.settle_micro(
        tier or tier_of(ws), rates(db),
        powered_on=powered_on,
        archived=(ws.state == WorkspaceState.ARCHIVED),
        cpu_core_hours=cpu_core_hours, mem_gib_hours=mem_gib_hours,
        port_count=port_count(db, ws), fraction=fraction)
    if amount <= 0:
        return 0
    kind = TxKind.CHARGE_HOUR if fraction >= 0.999 else TxKind.CHARGE_PARTIAL
    post_transaction(db, user_id=ws.user_id, workspace_id=ws.id, kind=kind,
                     amount_micro=-amount, period_start=period_start, detail=detail)
    return amount


def settle_elapsed(db: Session, ws: Workspace, *, powered_on: bool,
                   tier: Tier | None = None) -> int:
    """Close off the part-hour running right now, at the given tier.

    Called on power-off and on a size change. Without this a user could run at
    the largest size for 59 minutes, drop to the smallest, and be billed for
    the whole hour at the smallest - because settlement reads the CURRENT tier.
    """
    if not ws.period_start:
        return 0
    elapsed_h = (now() - ws.period_start).total_seconds() / 3600.0
    if elapsed_h <= 0.0005:
        return 0
    return settle_period(db, ws, ws.period_start, powered_on=powered_on,
                         fraction=min(elapsed_h, 1.0), tier=tier)


def workspace_ip(ws: Workspace) -> str:
    """Deterministic address, matching workspace/ws-lib.sh's ws_ip()."""
    return f"10.42.0.{ws.idx + 10}"


def sync_published_ports(db: Session) -> dict:
    """Push the complete set of published ports to the host firewall.

    Sends every mapping for every workspace, not a delta. A full rewrite is
    idempotent and self-heals after a crash, a restart, or a rule someone
    removed by hand - which a sequence of add/remove calls cannot do.
    """
    rows = db.execute(
        select(ExposedPort, Workspace)
        .join(Workspace, Workspace.id == ExposedPort.workspace_id)).all()
    mappings = [{
        "external_port": p.external_port,
        "internal_port": p.internal_port,
        "protocol": p.protocol,
        "ip": workspace_ip(w),
    } for p, w in rows]
    return call_provisioner({"verb": "expose_port", "idx": 1, "mappings": mappings},
                            timeout=60)


def next_free_idx(db: Session) -> int:
    used = {w.idx for w in db.scalars(select(Workspace))}
    i = 1
    while i in used:
        i += 1
    return i


def audit(db: Session, actor_id: int | None, action: str,
          target: str | None = None, **detail) -> None:
    db.add(AuditLog(actor_id=actor_id, action=action, target=target, detail=detail))
    db.commit()
