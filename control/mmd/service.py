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
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .billing import pricing
from .billing import aipricing
from .billing.pricing import MICRO, Rates, Tier
from . import metrics
from . import ports
from .config import CONFIG
from .models import (AuditLog, CreditAccount, CreditTransaction, ExposedPort,
                     Setting, TxKind, User, Workspace, WorkspaceState, AiModelPrice, AiUsageMark)
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
                     detail: dict | None = None,
                     scope_key: str | None = None) -> CreditTransaction | None:
    """Post a ledger entry and move the balance.

    Returns None when this exact (workspace, period, kind) was already posted -
    the unique constraint turns a retry after a worker crash into a no-op
    rather than a second charge.
    """
    tx = CreditTransaction(
        user_id=user_id, workspace_id=workspace_id, kind=kind,
        amount_micro=amount_micro, period_start=period_start, detail=detail or {},
        scope_key=scope_key)
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
    return pricing.max_hour_micro(tier_of(ws), rates(db))


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
    """Ask the root provisioner to act. The only privileged path in the app.

    Timed and counted by VERB, which is safe to use as a label precisely
    because the provisioner accepts a fixed allowlist of them - the same
    property that makes this boundary auditable makes it bounded.
    """
    verb = str(payload.get("verb") or "unknown")
    started = time.monotonic()
    result = "error"
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
        reply = json.loads(buf.decode() or "{}")
        result = "ok" if reply.get("ok") else "failed"
        return reply
    except (OSError, json.JSONDecodeError) as exc:
        result = "unreachable"
        return {"ok": False, "error": f"provisioner unreachable: {exc}"}
    finally:
        s.close()
        # In `finally`, so a verb that raised still reports the time it burned;
        # a timeout that vanished from the histogram would make a hung
        # provisioner look idle.
        metrics.observe("mmd_provisioner_seconds", time.monotonic() - started,
                        {"verb": verb})
        metrics.inc("mmd_provisioner_calls_total",
                    {"verb": verb, "result": result})


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
        cpu_core_hours=cpu_core_hours, mem_gib_hours=mem_gib_hours, fraction=fraction)
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


# --- the automatic stop --------------------------------------------------
def arm_auto_stop(ws: Workspace) -> None:
    """Schedule the end of this power-on cycle.

    Called on EVERY transition into ON, including the reconciler restoring a
    machine after host downtime. That is what makes the customer's "keep it
    running" choice apply to one run rather than forever: the deadline comes
    back the next time the machine starts, so a machine left on by accident is
    always bounded, while a machine someone is deliberately using is one click
    from staying up.
    """
    ws.auto_stop_at = now() + timedelta(hours=CONFIG.auto_stop_hours)


def disarm_auto_stop(ws: Workspace) -> None:
    """The customer has asked for this run to continue until they stop it."""
    ws.auto_stop_at = None


def due_for_auto_stop(ws: Workspace, at: datetime | None = None) -> bool:
    """Is this machine past the deadline it was given when it started?

    A NULL deadline is not "stop now" - it means the customer opted out for
    this run, or the machine predates the feature. Reading it either way round
    is the difference between a feature and an outage.
    """
    return (ws.state == WorkspaceState.ON
            and ws.auto_stop_at is not None
            and ws.auto_stop_at <= (at or now()))


def workspace_ip(ws: Workspace) -> str:
    """Deterministic address, matching workspace/ws-lib.sh's ws_ip()."""
    return f"10.42.0.{ws.idx + 10}"


def sync_published_ports(db: Session, *, exclude_workspace_id: int | None = None,
                         exclude_port_id: int | None = None) -> dict:
    """Push the complete set of published ports to the host firewall.

    Sends every mapping for every workspace, not a delta. A full rewrite is
    idempotent and self-heals after a crash, a restart, or a rule someone
    removed by hand - which a sequence of add/remove calls cannot do.
    """
    q = select(ExposedPort, Workspace).join(
        Workspace, Workspace.id == ExposedPort.workspace_id)
    if exclude_workspace_id is not None:
        q = q.where(Workspace.id != exclude_workspace_id)
    if exclude_port_id is not None:
        q = q.where(ExposedPort.id != exclude_port_id)
    rows = db.execute(q).all()
    # One row can stand for both protocols, and the provisioner deliberately
    # accepts only "tcp" or "udp" - it re-validates everything and does not know
    # about our shorthand. Expanding here keeps that boundary narrow.
    mappings = [{
        "external_port": p.external_port,
        "internal_port": p.internal_port,
        "protocol": proto,
        "ip": workspace_ip(w),
    } for p, w in rows for proto in ports.expand(p.protocol)]
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


# --- AI token metering ----------------------------------------------------
AI_SERVICE = "claude"
CODEX_SERVICE = "codex"

# Which ledger kind each service posts under. NOT one shared kind: the ledger's
# idempotency key is (workspace, period, kind), so two services charging the
# same five-minute bucket under one kind would collide and the second would be
# silently dropped.
AI_TX_KIND = {AI_SERVICE: TxKind.CHARGE_AI, CODEX_SERVICE: TxKind.CHARGE_CODEX}
AI_PERIOD_SECONDS = 300          # the charge bucket; matches the worker's cadence

_MARK_FIELDS = {
    "input": "input_tokens",
    "cache_write_5m": "cache_write_5m_tokens",
    "cache_write_1h": "cache_write_1h_tokens",
    "cache_read": "cache_read_tokens",
    "output": "output_tokens",
}


def ai_prices(db: Session, service: str = AI_SERVICE) -> dict[str, aipricing.ModelPrice]:
    """The price table for one service, seeding itself on first use so a fresh
    host bills correctly before anyone has visited the admin panel.

    Both suppliers seed now. Codex started empty on purpose while its rates
    were unknown - an unpriced model is held UNCOUNTED rather than given away,
    so the failure mode of an unconfigured supplier was "not yet billed" rather
    than "billed wrongly". The rates are known and recorded now, so the same
    reasoning that seeds Claude applies: a fresh host should bill correctly
    before anyone has visited the admin panel.

    A model still unmatched by any row is still held uncounted, and still
    listed in the admin panel as unpriced."""
    rows = list(db.scalars(select(AiModelPrice).where(AiModelPrice.service == service)))
    if not rows and service in aipricing.SEED_BY_SERVICE:
        for model, (i, w5, w1h, r, o) in aipricing.SEED_BY_SERVICE[service].items():
            db.add(AiModelPrice(service=service, model=model, input_usd=i,
                                cache_write_5m_usd=w5, cache_write_1h_usd=w1h,
                                cache_read_usd=r, output_usd=o))
        db.commit()
        rows = list(db.scalars(select(AiModelPrice).where(AiModelPrice.service == service)))
    return {r.model: aipricing.ModelPrice(r.model, r.input_usd, r.cache_write_5m_usd,
                                          r.cache_write_1h_usd, r.cache_read_usd,
                                          r.output_usd) for r in rows}


def ai_settings(db: Session, service: str = AI_SERVICE) -> tuple[float, float]:
    """(usd_to_toman, discount_percent) for one service.

    The discount is per service on purpose. Claude is a flat subscription, so a
    customer's marginal token costs the operator nothing and selling at a tenth
    of list is margin. OpenRouter is metered, so the same rate would collect ten
    cents for every dollar spent. One number cannot be right for both, and the
    failure is silent - it just loses money per token.
    """
    s = get_settings(db)

    def g(key: str, default: float) -> float:
        try:
            return float(s.get(key, default))
        except (TypeError, ValueError):
            return default

    usd = g("usd_to_toman", aipricing.DEFAULT_AI_SETTINGS["usd_to_toman"])
    # OpenRouter is pay-as-you-go supplier spend. It is converted at the
    # configured exchange rate, never discounted. Keeping this invariant here
    # prevents a stale setting or a second admin form from changing the bill.
    if service == "openrouter":
        return usd, 0.0
    key = aipricing.discount_key(service)
    default = aipricing.DEFAULT_AI_SETTINGS.get(key, 0.0)
    # `ai_discount_percent` was the single global setting before the split.
    # Honoured as the Claude default so an operator who had tuned it does not
    # silently get 90% back when this deploys.
    if key not in s and service == "claude" and "ai_discount_percent" in s:
        return usd, g("ai_discount_percent", default)
    return usd, g(key, default)


def meter_ai_usage(db: Session, ws: Workspace, report: dict,
                   service: str = AI_SERVICE) -> dict:
    """Charge for whatever tokens are new since the last pass.

    `report` is the scanner's CUMULATIVE totals per session per model. What gets
    billed is the difference from the stored high-water mark, so this is safe to
    run as often as you like and safe to re-run after a crash.

    Deltas are clamped at zero. That is what makes a customer destroying and
    rebuilding their workspace harmless: the session files vanish, the marks do
    not, and a rebuilt machine reporting smaller numbers produces no charge and
    no refund rather than a negative one.

    A model with no price is deliberately left UNMARKED - its tokens stay
    uncounted so they bill correctly once someone sets a price, instead of being
    silently given away.
    """
    prices = ai_prices(db, service)
    usd_rate, discount = ai_settings(db, service)
    period = _ai_period(now())

    marks = {(m.session_id, m.model): m for m in db.scalars(
        select(AiUsageMark).where(AiUsageMark.workspace_id == ws.id,
                                  AiUsageMark.service == service))}

    total_micro = 0
    per_model: dict[str, dict] = {}
    unpriced: set[str] = set()
    touched: list[tuple[AiUsageMark, dict[str, int]]] = []

    for session_id, models in (report.get("sessions") or {}).items():
        for model, totals in models.items():
            price = aipricing.resolve(model, prices)
            if price is None:
                unpriced.add(model)
                continue

            mark = marks.get((session_id, model))
            if mark is None:
                # The counters are set here rather than left to the column
                # defaults: those apply at INSERT, so an unflushed object reads
                # back None and the first delta would raise instead of billing.
                mark = AiUsageMark(workspace_id=ws.id, service=service,
                                   session_id=session_id[:96], model=model[:96],
                                   billed_micro=0,
                                   **{f: 0 for f in _MARK_FIELDS.values()})
                db.add(mark)
                marks[(session_id, model)] = mark

            delta = {c: max(0, int(totals.get(c, 0)) - (getattr(mark, f) or 0))
                     for c, f in _MARK_FIELDS.items()}
            if not any(delta.values()):
                continue

            micro = aipricing.toman_micro(delta, price, usd_rate, discount)
            total_micro += micro

            acc = per_model.setdefault(price.model, {"tokens": dict.fromkeys(_MARK_FIELDS, 0),
                                                     "micro": 0})
            for c in _MARK_FIELDS:
                acc["tokens"][c] += delta[c]
            acc["micro"] += micro
            # The mark only ever moves UP - that is what makes a rebuilt
            # workspace reporting smaller totals harmless.
            touched.append((mark, {c: max(int(totals.get(c, 0)), getattr(mark, f) or 0)
                                   for c, f in _MARK_FIELDS.items()}))
            mark.billed_micro = (mark.billed_micro or 0) + micro

    if not touched:
        return {"charged_micro": 0, "models": {}, "unpriced": sorted(unpriced)}

    # The marks move only if the charge lands. Advancing them first and failing
    # to post would hand the customer free tokens; posting first and failing to
    # advance would bill them twice on the next pass.
    tx = post_transaction(db, user_id=ws.user_id, workspace_id=ws.id,
                          kind=AI_TX_KIND[service], amount_micro=-total_micro,
                          period_start=period,
                          detail={"service": service,
                                  "usd_to_toman": usd_rate,
                                  "discount_percent": discount,
                                  "models": {m: {"tokens": v["tokens"],
                                                 "toman": v["micro"] / MICRO}
                                             for m, v in per_model.items()}})
    if tx is None:
        # Already charged for this bucket. Leave the marks alone; the tokens
        # roll into the next one rather than being lost or double-billed.
        db.rollback()
        return {"charged_micro": 0, "models": {}, "skipped": "already charged this period",
                "unpriced": sorted(unpriced)}

    for mark, newest in touched:
        for c, f in _MARK_FIELDS.items():
            setattr(mark, f, newest[c])
    db.commit()

    return {"charged_micro": total_micro,
            "models": {m: v["micro"] / MICRO for m, v in per_model.items()},
            "unpriced": sorted(unpriced)}


def _ai_period(ts: datetime) -> datetime:
    """Floor to the charge bucket, so a pass that runs twice is a no-op."""
    epoch = int(ts.timestamp()) // AI_PERIOD_SECONDS * AI_PERIOD_SECONDS
    return datetime.fromtimestamp(epoch, UTC)
