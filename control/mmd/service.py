"""Business logic shared by the API and the worker.

Kept out of the route handlers so the worker can enforce exactly the same
rules on its own schedule - in particular the credit gate, which must be
applied both when a user presses Power and at every hour boundary.
"""
from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .billing.rates import (MICRO, Rates, Tier, max_hour_cost_micro,
                            off_hour_cost_micro, settle_hour_micro)
from .config import CONFIG
from .models import (CreditAccount, CreditTransaction, Setting, TxKind, User,
                     UserStatus, Workspace, WorkspaceState)
from .scheduler.admission import can_start, host_capacity

UTC = timezone.utc


def now() -> datetime:
    return datetime.now(UTC)


# --- settings ------------------------------------------------------------
def get_settings(db: Session) -> dict[str, str]:
    return {s.key: s.value for s in db.scalars(select(Setting))}


def rates(db: Session) -> Rates:
    return Rates.from_settings(get_settings(db))


def tier_of(ws: Workspace) -> Tier:
    return Tier(cores=ws.cores, mem_mib=ws.mem_mib, disk_gib=ws.disk_gib)


# --- credit --------------------------------------------------------------
def balance_micro(db: Session, user_id: int) -> int:
    acct = db.get(CreditAccount, user_id)
    return acct.balance_micro if acct else 0


def post_transaction(db: Session, *, user_id: int, workspace_id: int | None,
                     kind: TxKind, amount_micro: int,
                     period_start: datetime | None = None,
                     detail: dict | None = None) -> CreditTransaction | None:
    """Post a ledger entry and move the balance.

    Returns None if this exact (workspace, period, kind) was already posted -
    the unique constraint makes a retry after a worker crash a no-op rather
    than a second charge.
    """
    from sqlalchemy.exc import IntegrityError

    tx = CreditTransaction(
        user_id=user_id, workspace_id=workspace_id, kind=kind,
        amount_micro=amount_micro, period_start=period_start,
        detail=detail or {},
    )
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
    """What the next hour could cost at full capacity."""
    return max_hour_cost_micro(tier_of(ws), rates(db))


def can_afford_next_hour(db: Session, ws: Workspace) -> tuple[bool, int, int]:
    need = gate_micro(db, ws)
    have = balance_micro(db, ws.user_id)
    return have >= need, have, need


# --- admission -----------------------------------------------------------
def running_tiers(db: Session, exclude_ws_id: int | None = None) -> list[tuple[float, float]]:
    q = select(Workspace).where(Workspace.state.in_(
        [WorkspaceState.ON, WorkspaceState.STARTING]))
    return [(float(w.cores), w.mem_gib) for w in db.scalars(q)
            if w.id != exclude_ws_id]


def check_admission(db: Session, ws: Workspace, cores: int | None = None,
                    mem_mib: int | None = None):
    cap = host_capacity(get_settings(db))
    return can_start(
        cap, running_tiers(db, exclude_ws_id=ws.id),
        float(cores if cores is not None else ws.cores),
        (mem_mib if mem_mib is not None else ws.mem_mib) / 1024.0,
    )


# --- provisioner IPC -----------------------------------------------------
def call_provisioner(payload: dict, timeout: float = 900.0) -> dict:
    """Ask the root provisioner to do something. The only privileged path."""
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
def settle_period(db: Session, ws: Workspace, period_start: datetime,
                  *, powered_on: bool, cpu_core_hours: float, mem_gib_hours: float,
                  fraction: float = 1.0) -> int:
    """Charge one completed hour, in arrears. Idempotent per (ws, hour)."""
    r = rates(db)
    amount, detail = settle_hour_micro(
        tier_of(ws), r, powered_on=powered_on,
        cpu_core_hours=cpu_core_hours, mem_gib_hours=mem_gib_hours,
        fraction=fraction,
        archived=(ws.state == WorkspaceState.ARCHIVED),
    )
    if amount <= 0:
        return 0
    kind = TxKind.CHARGE_HOUR if fraction >= 1.0 else TxKind.CHARGE_PARTIAL
    post_transaction(db, user_id=ws.user_id, workspace_id=ws.id, kind=kind,
                     amount_micro=-amount, period_start=period_start, detail=detail)
    return amount


def next_free_idx(db: Session) -> int:
    used = {w.idx for w in db.scalars(select(Workspace))}
    i = 1
    while i in used:
        i += 1
    return i
