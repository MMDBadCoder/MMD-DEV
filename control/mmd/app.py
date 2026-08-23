"""MMD-DEV control plane API.

Every user-facing string here avoids naming the technology underneath. The
product promise is "your own isolated Ubuntu machine"; the customer should
never meet the words container, Incus or ZFS - including in error messages.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from . import ports as portalloc
from . import presets as presetlib
from . import service as svc
from .billing import pricing
from .billing.pricing import MICRO, InvalidTier, Tier
from .config import CONFIG
from .db import get_session, init_db
from .incus.client import IncusClient, IncusConfig, IncusError
from .incus.execws import open_exec
from .models import (AuditLog, CreditAccount, CreditTransaction, ExposedPort,
                     Setting, TxKind, UsageSample, User, UserStatus, Workspace,
                     WorkspaceState)
from .security import hash_password, verify_password

log = logging.getLogger("mmd.api")
app = FastAPI(title="MMD-DEV", docs_url=None, redoc_url=None)

_serializer = URLSafeTimedSerializer(CONFIG.secret_key or "dev-only-insecure-key",
                                     salt="mmd-session")
COOKIE = "mmd_session"
WEB = Path(__file__).resolve().parents[2] / "web"


def fail(status: int, code: str, message: str, **extra) -> None:
    """Raise an error the interface can translate.

    The UI is Persian; the API is not. Shipping a stable `code` alongside the
    English `message` lets the interface show its own wording without parsing
    English prose, while logs and developers still get something readable.
    """
    raise HTTPException(status, {"code": code, "message": message, **extra})


def _incus() -> IncusClient:
    return IncusClient(IncusConfig(
        base_url=CONFIG.incus_url, client_cert=CONFIG.incus_client_cert,
        client_key=CONFIG.incus_client_key, server_cert=CONFIG.incus_server_cert))


# --- auth plumbing -------------------------------------------------------
def current_user(request: Request, db: Session = Depends(get_session)) -> User:
    raw = request.cookies.get(COOKIE)
    if not raw:
        fail(401, "not_signed_in", "Not signed in")
    try:
        uid = _serializer.loads(raw, max_age=CONFIG.session_hours * 3600)
    except BadSignature:
        fail(401, "session_expired", "Your session has expired.")
    user = db.get(User, int(uid))
    if user is None or user.status == UserStatus.REJECTED:
        fail(401, "not_signed_in", "Not signed in")
    if user.status == UserStatus.SUSPENDED:
        fail(403, "suspended", "Your account is suspended.")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        fail(403, "admin_required", "Administrator access required")
    return user


def my_workspace(db: Session, user: User) -> Workspace:
    ws = db.scalar(select(Workspace).where(Workspace.user_id == user.id))
    if ws is None:
        fail(404, "no_workspace", "You do not have a machine yet.")
    return ws


# --- schemas -------------------------------------------------------------
class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=200)


class LoginBody(BaseModel):
    email: EmailStr
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10, max_length=200)


class PowerRequest(BaseModel):
    on: bool


class TierRequest(BaseModel):
    cpu_milli: int
    mem_mib: int


class PortRequest(BaseModel):
    internal_port: int = Field(ge=1, le=65535)
    protocol: str = "tcp"
    note: str | None = Field(default=None, max_length=120)


class PresetRequest(BaseModel):
    presets: list[str] = []
    packages: list[str] = []


class AdminFlag(BaseModel):
    is_admin: bool


class CreditGrant(BaseModel):
    credits: float
    note: str = ""


@app.on_event("startup")
def _startup() -> None:
    logging.basicConfig(level=logging.INFO)
    init_db()


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


# --- auth ----------------------------------------------------------------
@app.post("/api/auth/register")
def register(body: Credentials, db: Session = Depends(get_session)) -> dict:
    if db.scalar(select(User).where(User.email == body.email.lower())):
        # Identical to the success response on purpose: differing replies would
        # let anyone enumerate which addresses hold accounts.
        return {"status": "pending", "message": "Your account is awaiting approval."}
    first = db.scalar(select(User).limit(1)) is None
    user = User(
        email=body.email.lower(), password_hash=hash_password(body.password),
        is_admin=first,
        status=UserStatus.APPROVED if first else UserStatus.PENDING)
    db.add(user)
    db.commit()
    db.add(CreditAccount(user_id=user.id, balance_micro=0))
    db.commit()
    svc.audit(db, user.id, "register", user.email, first_account=first)
    return {"status": user.status.value,
            "message": ("Administrator account created. You can sign in now."
                        if first else "Your account is awaiting approval.")}


@app.post("/api/auth/login")
def login(body: LoginBody, response: Response,
          db: Session = Depends(get_session)) -> dict:
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    if user is None or not verify_password(body.password, user.password_hash):
        fail(401, "bad_credentials", "Incorrect email or password")
    response.set_cookie(COOKIE, _serializer.dumps(str(user.id)), httponly=True,
                        samesite="lax", secure=True,
                        max_age=CONFIG.session_hours * 3600)
    svc.audit(db, user.id, "sign_in", user.email)
    return {"status": user.status.value, "is_admin": user.is_admin}


@app.post("/api/auth/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.post("/api/auth/password")
def change_password(body: PasswordChange, user: User = Depends(current_user),
                    db: Session = Depends(get_session)) -> dict:
    if not verify_password(body.current_password, user.password_hash):
        fail(401, "wrong_password", "Your current password is not correct")
    user.password_hash = hash_password(body.new_password)
    db.commit()
    svc.audit(db, user.id, "password_change", user.email)
    return {"ok": True, "message": "Password changed."}


@app.get("/api/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_session)) -> dict:
    acct = db.get(CreditAccount, user.id)
    ws = db.scalar(select(Workspace).where(Workspace.user_id == user.id))
    return {
        "email": user.email, "status": user.status.value, "is_admin": user.is_admin,
        "credits": (acct.balance_micro / MICRO) if acct else 0.0,
        "has_workspace": ws is not None,
        "member_since": user.created_at.isoformat() if user.created_at else None,
    }


# --- sizes ---------------------------------------------------------------
@app.get("/api/tiers")
def tiers(user: User = Depends(current_user),
          db: Session = Depends(get_session)) -> dict:
    """The size menu plus what each option costs. One source of truth."""
    r = svc.rates(db)
    ws = db.scalar(select(Workspace).where(Workspace.user_id == user.id))
    disk = ws.disk_gib if ws else (pricing.DEFAULT_ROOT_GIB + pricing.DEFAULT_DOCKER_GIB)
    npub = svc.port_count(db, ws) if ws else 0
    options = []
    for cm in pricing.CPU_OPTIONS_MILLI:
        for mm in pricing.MEM_OPTIONS_MIB:
            t = Tier(cm, mm, disk)
            q = pricing.quote(t, r, port_count=npub)
            options.append({"cpu_milli": cm, "mem_mib": mm, "label": t.label,
                            "comfortable": t.is_comfortable,
                            "max_per_hour": q["max_per_hour"],
                            "idle_per_hour": q["idle_per_hour"]})
    return {"catalogue": pricing.catalogue(), "options": options,
            "current": ({"cpu_milli": ws.cpu_milli, "mem_mib": ws.mem_mib}
                        if ws else None),
            "rates": r.as_dict()}


# --- public ---------------------------------------------------------------
@app.get("/api/public/pricing")
def public_pricing(db: Session = Depends(get_session)) -> dict:
    """Prices for the marketing page. No authentication.

    Deliberately served from the same pricing module the billing engine uses,
    so the advertised price cannot drift from the charged one.
    """
    r = svc.rates(db)
    disk = pricing.DEFAULT_ROOT_GIB + pricing.DEFAULT_DOCKER_GIB
    plans = []
    for cm, mm in ((500, 512), (1000, 1024), (2000, 2048), (3000, 6144)):
        tier = Tier(cm, mm, disk)
        q = pricing.quote(tier, r)
        plans.append({
            "cpu_cores": tier.cpu_cores, "mem_gib": tier.mem_gib,
            "disk_gib": disk, "label": tier.label,
            "max_per_hour": q["max_per_hour"],
            "idle_per_hour": q["idle_per_hour"],
            "off_per_hour": q["off_per_hour"],
            "max_per_month": q["max_per_hour"] * 720,
            "comfortable": tier.is_comfortable,
        })
    return {"currency": pricing.CURRENCY, "plans": plans,
            "catalogue": pricing.catalogue()}


# --- toolsets ------------------------------------------------------------
@app.get("/api/presets")
def list_presets(_: User = Depends(current_user)) -> dict:
    return {"presets": presetlib.catalogue(),
            "max_packages": presetlib.MAX_PACKAGES}


@app.post("/api/workspace/presets")
def install_presets(body: PresetRequest, user: User = Depends(current_user),
                    db: Session = Depends(get_session)) -> dict:
    """Install a toolset into the running machine."""
    ws = my_workspace(db, user)
    if ws.state != WorkspaceState.ON:
        fail(409, "machine_off", "The machine must be running to install software.")
    try:
        packages = presetlib.resolve(body.presets, body.packages)
    except presetlib.PresetError as exc:
        fail(400, "bad_package", str(exc))
    if not packages:
        fail(400, "no_packages", "Nothing selected to install.")

    resp = svc.call_provisioner({"verb": "install_packages", "idx": ws.idx,
                                 "packages": packages}, timeout=900)
    if not resp.get("ok"):
        log.error("package install failed for %s: %s", ws.incus_project, resp)
        fail(500, "install_failed", "The software could not be installed.",
             output=(resp.get("output") or resp.get("error", ""))[-400:])
    svc.audit(db, user.id, "packages_installed", ws.incus_project,
              presets=body.presets, count=len(packages))
    return {"ok": True, "packages": packages}


# --- the machine ---------------------------------------------------------
@app.get("/api/workspace")
def workspace_status(user: User = Depends(current_user),
                     db: Session = Depends(get_session)) -> dict:
    if user.status == UserStatus.PENDING:
        return {"status": "pending",
                "message": "Your account is awaiting approval by an administrator."}
    ws = db.scalar(select(Workspace).where(Workspace.user_id == user.id))
    if ws is None:
        return {"status": "none", "message": "No machine has been created yet."}

    r = svc.rates(db)
    tier = svc.tier_of(ws)
    npub = svc.port_count(db, ws)
    q = pricing.quote(tier, r, port_count=npub)
    affordable, have, need = svc.can_afford_next_hour(db, ws)
    adm = svc.check_admission(db, ws)

    blocked = None
    if ws.state == WorkspaceState.OFF:
        if not affordable:
            blocked = (f"You need at least {need / MICRO:.2f} credits to run for "
                       f"another hour. Your balance is {have / MICRO:.2f}.")
        elif not adm.allowed:
            blocked = adm.reason

    return {
        "status": ws.state.value,
        "powered_on": ws.state == WorkspaceState.ON,
        "cpu_milli": ws.cpu_milli, "cpu_cores": ws.cpu_cores,
        "memory_mb": ws.mem_mib, "memory_gb": ws.mem_gib,
        "disk_gb": ws.disk_gib, "label": tier.label,
        "comfortable": tier.is_comfortable,
        "credits": have / MICRO,
        "rate_on_per_hour": q["max_per_hour"],
        "rate_idle_per_hour": q["idle_per_hour"],
        "rate_off_per_hour": q["off_per_hour"],
        "hours_remaining": (have / MICRO) / q["max_per_hour"] if q["max_per_hour"] else 0,
        "published_ports": npub,
        "can_power_on": bool(ws.state == WorkspaceState.OFF and affordable and adm.allowed),
        "blocked_reason": blocked,
        "started_at": ws.started_at.isoformat() if ws.started_at else None,
        "archived_until": ws.purge_after.isoformat() if ws.purge_after else None,
    }


@app.post("/api/workspace/power")
async def workspace_power(body: PowerRequest, user: User = Depends(current_user),
                          db: Session = Depends(get_session)) -> dict:
    ws = my_workspace(db, user)
    client = _incus()
    try:
        if body.on:
            if ws.state == WorkspaceState.ON:
                return {"ok": True, "status": "on"}
            if ws.state == WorkspaceState.ARCHIVED:
                fail(409, "archived", "This machine is archived.")
            if ws.state != WorkspaceState.OFF:
                fail(409, "busy", f"The machine is {ws.state.value}.", state=ws.state.value)

            affordable, have, need = svc.can_afford_next_hour(db, ws)
            if not affordable:
                fail(402, "insufficient_credit",
                     "Not enough credit to cover one hour at full capacity.",
                     needed=need / MICRO, balance=have / MICRO)
            adm = svc.check_admission(db, ws)
            if not adm.allowed:
                fail(503, "no_capacity", adm.reason, resource=adm.resource)

            ws.state = WorkspaceState.STARTING
            db.commit()
            # Apply the size before starting. A change made while the machine
            # was off only updated the database - without this the machine
            # would come back with its OLD limits while being billed for the
            # new ones.
            await client.patch_config(ws.instance, ws.incus_project,
                                      svc.tier_of(ws).incus_config())
            await client.start(ws.instance, ws.incus_project)
            ws.state = WorkspaceState.ON
            ws.desired_on = True
            ws.started_at = svc.now()
            ws.period_start = svc.now()
            ws.last_activity = svc.now()
            db.commit()
            svc.audit(db, user.id, "power_on", ws.incus_project)
            return {"ok": True, "status": "on"}

        if ws.state == WorkspaceState.OFF:
            return {"ok": True, "status": "off"}
        ws.state = WorkspaceState.STOPPING
        db.commit()
        await client.stop(ws.instance, ws.incus_project)
        svc.settle_elapsed(db, ws, powered_on=True)
        ws.state = WorkspaceState.OFF
        ws.desired_on = False
        ws.period_start = None
        db.commit()
        svc.audit(db, user.id, "power_off", ws.incus_project)
        return {"ok": True, "status": "off"}
    except HTTPException:
        raise
    except IncusError as exc:
        log.error("power change failed for %s: %s", ws.incus_project, exc)
        ws.state = WorkspaceState.ERROR
        ws.error = str(exc)
        db.commit()
        fail(500, "power_failed", "The machine could not be changed.")
    except Exception as exc:  # noqa: BLE001
        log.exception("unexpected error changing power for %s", ws.incus_project)
        ws.state = WorkspaceState.ERROR
        ws.error = f"{type(exc).__name__}: {exc}"
        db.commit()
        fail(500, "power_failed", "The machine could not be changed.")
    finally:
        await client.aclose()


@app.post("/api/workspace/tier")
async def workspace_tier(body: TierRequest, user: User = Depends(current_user),
                         db: Session = Depends(get_session)) -> dict:
    """Change the machine's size.

    Allowed while running, with two safeguards that matter:

      * BILLING. The elapsed part of the current hour is settled at the OLD
        size before the change takes effect, and a fresh period starts.
        Without that, a customer could run at the largest size for 59 minutes,
        drop to the smallest, and be billed for the whole hour at the smallest,
        because settlement reads whatever size is current when it runs.

      * SAFETY. Memory may be raised live but not lowered live. Shrinking the
        limit under a process that is already using more forces immediate
        reclaim and can have the kernel kill a running coding agent - exactly
        the failure this product exists to avoid. CPU may move either way; the
        worst case there is slowness.
    """
    ws = my_workspace(db, user)
    try:
        new_tier = pricing.parse_tier(body.cpu_milli, body.mem_mib, ws.disk_gib)
    except InvalidTier as exc:
        fail(400, "invalid_size", str(exc))

    old_tier = svc.tier_of(ws)
    if (new_tier.cpu_milli, new_tier.mem_mib) == (old_tier.cpu_milli, old_tier.mem_mib):
        return {"ok": True, "unchanged": True}

    running = ws.state == WorkspaceState.ON
    if running and new_tier.mem_mib < old_tier.mem_mib:
        fail(409, "mem_shrink_running",
             "Memory cannot be reduced while the machine is running.")

    if running and (new_tier.cpu_milli > old_tier.cpu_milli
                    or new_tier.mem_mib > old_tier.mem_mib):
        adm = svc.check_admission(db, ws, tier=new_tier)
        if not adm.allowed:
            fail(503, "no_capacity", adm.reason, resource=adm.resource)

    client = _incus()
    try:
        if running:
            # Close the books on the old size BEFORE applying the new one.
            svc.settle_elapsed(db, ws, powered_on=True, tier=old_tier)
            ws.period_start = svc.now()
            await client.patch_config(ws.instance, ws.incus_project,
                                      new_tier.incus_config())
        ws.cpu_milli, ws.mem_mib = new_tier.cpu_milli, new_tier.mem_mib
        db.commit()
        svc.audit(db, user.id, "size_change", ws.incus_project,
                  cpu_milli=new_tier.cpu_milli, mem_mib=new_tier.mem_mib,
                  applied_live=running)
        q = pricing.quote(new_tier, svc.rates(db), port_count=svc.port_count(db, ws))
        return {"ok": True, "applied_live": running, "label": new_tier.label,
                "cpu_milli": ws.cpu_milli, "memory_mb": ws.mem_mib,
                "rate_on_per_hour": q["max_per_hour"]}
    except HTTPException:
        raise
    except IncusError as exc:
        log.error("size change failed for %s: %s", ws.incus_project, exc)
        fail(500, "resize_failed", "The size could not be changed.")
    finally:
        await client.aclose()


# --- published ports -----------------------------------------------------
@app.get("/api/workspace/ports")
def list_ports(request: Request, user: User = Depends(current_user),
               db: Session = Depends(get_session)) -> dict:
    ws = my_workspace(db, user)
    host = request.url.hostname
    rows = db.scalars(select(ExposedPort)
                      .where(ExposedPort.workspace_id == ws.id)
                      .order_by(ExposedPort.internal_port))
    return {
        "host": host,
        "max_ports": portalloc.MAX_PORTS_PER_WORKSPACE,
        "rate_per_hour": svc.rates(db).port,
        "ports": [{
            "id": p.id, "internal_port": p.internal_port,
            "external_port": p.external_port, "protocol": p.protocol,
            "note": p.note, "address": f"{host}:{p.external_port}",
            "created_at": p.created_at.isoformat() if p.created_at else None,
        } for p in rows],
    }


@app.post("/api/workspace/ports")
def create_port(body: PortRequest, request: Request,
                user: User = Depends(current_user),
                db: Session = Depends(get_session)) -> dict:
    ws = my_workspace(db, user)
    try:
        row = portalloc.allocate(db, ws.id, body.internal_port,
                                 body.protocol, body.note)
    except portalloc.PortError as exc:
        fail(400, exc.code, str(exc))

    # The forwarding rule is installed whether or not the machine is running:
    # the RESERVATION is what the customer is paying for, and an address that
    # moved every power cycle would be useless for a webhook or a demo link.
    # While the machine is off the port simply refuses connections.
    resp = svc.sync_published_ports(db)
    if not resp.get("ok"):
        portalloc.release(db, row)
        svc.sync_published_ports(db)      # put the firewall back as it was
        log.error("publishing port failed: %s", resp)
        fail(500, "port_failed", "The port could not be published.")

    svc.audit(db, user.id, "port_publish", ws.incus_project,
              internal=row.internal_port, external=row.external_port)
    return {"ok": True, "internal_port": row.internal_port,
            "external_port": row.external_port, "protocol": row.protocol,
            "address": f"{request.url.hostname}:{row.external_port}",
            "warning": ("Port 22 inside your machine is usually its SSH service; "
                        "publishing it exposes it to the internet."
                        if row.internal_port in portalloc.DISCOURAGED_INTERNAL else None)}


@app.delete("/api/workspace/ports/{port_id}")
def delete_port(port_id: int, user: User = Depends(current_user),
                db: Session = Depends(get_session)) -> dict:
    ws = my_workspace(db, user)
    row = db.get(ExposedPort, port_id)
    if row is None or row.workspace_id != ws.id:
        fail(404, "no_such_port", "No such published port")
    svc.audit(db, user.id, "port_unpublish", ws.incus_project,
              internal=row.internal_port, external=row.external_port)
    portalloc.release(db, row)
    svc.sync_published_ports(db)
    return {"ok": True}


# --- billing -------------------------------------------------------------
@app.get("/api/billing/summary")
def billing_summary(user: User = Depends(current_user),
                    db: Session = Depends(get_session)) -> dict:
    ws = db.scalar(select(Workspace).where(Workspace.user_id == user.id))
    r = svc.rates(db)
    have = svc.balance_micro(db, user.id)
    spent = db.scalar(select(func.coalesce(func.sum(CreditTransaction.amount_micro), 0))
                      .where(CreditTransaction.user_id == user.id,
                             CreditTransaction.amount_micro < 0)) or 0
    granted = db.scalar(select(func.coalesce(func.sum(CreditTransaction.amount_micro), 0))
                        .where(CreditTransaction.user_id == user.id,
                               CreditTransaction.amount_micro > 0)) or 0
    out = {"credits": have / MICRO, "total_spent": -spent / MICRO,
           "total_granted": granted / MICRO, "rates": r.as_dict()}
    if ws is not None:
        q = pricing.quote(svc.tier_of(ws), r, port_count=svc.port_count(db, ws))
        out["quote"] = q
        out["hours_remaining"] = ((have / MICRO) / q["max_per_hour"]
                                  if q["max_per_hour"] else 0)
    return out


@app.get("/api/billing/transactions")
def billing_transactions(limit: int = 100, offset: int = 0,
                         user: User = Depends(current_user),
                         db: Session = Depends(get_session)) -> dict:
    limit = max(1, min(limit, 500))
    total = db.scalar(select(func.count(CreditTransaction.id))
                      .where(CreditTransaction.user_id == user.id)) or 0
    rows = db.scalars(select(CreditTransaction)
                      .where(CreditTransaction.user_id == user.id)
                      .order_by(desc(CreditTransaction.created_at))
                      .limit(limit).offset(offset))
    return {"total": total, "limit": limit, "offset": offset,
            "transactions": [{
                "id": t.id, "kind": t.kind.value,
                "amount": t.amount_micro / MICRO,
                "period_start": t.period_start.isoformat() if t.period_start else None,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "detail": {k: (v / MICRO if k in ("disk", "ports", "reservation", "usage")
                               and isinstance(v, (int, float)) else v)
                           for k, v in (t.detail or {}).items()},
            } for t in rows]}


@app.get("/api/billing/usage")
def billing_usage(hours: int = 24, user: User = Depends(current_user),
                  db: Session = Depends(get_session)) -> dict:
    """Spend per hour, for the chart."""
    hours = max(1, min(hours, 24 * 30))
    since = svc.now() - timedelta(hours=hours)
    rows = db.execute(
        select(func.date_trunc("hour", CreditTransaction.created_at).label("h"),
               func.sum(CreditTransaction.amount_micro).label("amt"))
        .where(CreditTransaction.user_id == user.id,
               CreditTransaction.amount_micro < 0,
               CreditTransaction.created_at >= since)
        .group_by("h").order_by("h")).all()
    return {"hours": hours,
            "series": [{"hour": r.h.isoformat(), "spent": -r.amt / MICRO} for r in rows]}


# --- activity ------------------------------------------------------------
@app.get("/api/activity")
def activity(limit: int = 100, offset: int = 0,
             user: User = Depends(current_user),
             db: Session = Depends(get_session)) -> dict:
    limit = max(1, min(limit, 500))
    q = select(AuditLog).where(AuditLog.actor_id == user.id)
    total = db.scalar(select(func.count(AuditLog.id))
                      .where(AuditLog.actor_id == user.id)) or 0
    rows = db.scalars(q.order_by(desc(AuditLog.ts)).limit(limit).offset(offset))
    return {"total": total, "limit": limit, "offset": offset,
            "events": [{"id": e.id, "ts": e.ts.isoformat() if e.ts else None,
                        "action": e.action, "target": e.target,
                        "detail": e.detail or {}} for e in rows]}


# --- browser terminal ----------------------------------------------------
@app.websocket("/api/workspace/terminal")
async def terminal(sock: WebSocket) -> None:
    await sock.accept()
    db = next(get_session())
    client = None
    try:
        raw = sock.cookies.get(COOKIE)
        if not raw:
            await sock.close(code=4401); return
        try:
            uid = int(_serializer.loads(raw, max_age=CONFIG.session_hours * 3600))
        except BadSignature:
            await sock.close(code=4401); return
        user = db.get(User, uid)
        if user is None or user.status != UserStatus.APPROVED:
            await sock.close(code=4403); return
        ws = db.scalar(select(Workspace).where(Workspace.user_id == user.id))
        if ws is None:
            await sock.close(code=4404); return
        if ws.state != WorkspaceState.ON:
            await sock.send_text("\r\n\x1b[33mYour machine is switched off. "
                                 "Turn it on to open a terminal.\x1b[0m\r\n")
            await sock.close(code=4409); return

        ws.last_activity = svc.now()
        db.commit()
        project, instance = ws.incus_project, ws.instance
        client = _incus()
        async with open_exec(client, instance, project, user="dev") as session:

            async def pump_out() -> None:
                while True:
                    data = await session.recv()
                    if not data:
                        break
                    await sock.send_bytes(data)

            async def pump_in() -> None:
                while True:
                    msg = await sock.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    text = msg.get("text")
                    if text is not None:
                        if text.startswith('{"resize"'):
                            dims = _json.loads(text)["resize"]
                            await session.resize(dims["cols"], dims["rows"])
                            continue
                        await session.send(text.encode())
                    elif (blob := msg.get("bytes")) is not None:
                        await session.send(blob)

            done, pending = await asyncio.wait(
                {asyncio.create_task(pump_out()), asyncio.create_task(pump_in())},
                return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except Exception:
        log.debug("terminal session ended", exc_info=True)
    finally:
        if client is not None:
            await client.aclose()
        db.close()
        try:
            await sock.close()
        except Exception:
            pass


# --- administration ------------------------------------------------------
@app.get("/api/admin/users")
def admin_users(_: User = Depends(require_admin),
                db: Session = Depends(get_session)) -> list[dict]:
    out = []
    for u in db.scalars(select(User).order_by(desc(User.created_at))):
        w = db.scalar(select(Workspace).where(Workspace.user_id == u.id))
        acct = db.get(CreditAccount, u.id)
        out.append({
            "id": u.id, "email": u.email, "status": u.status.value,
            "is_admin": u.is_admin,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "credits": (acct.balance_micro / MICRO) if acct else 0.0,
            "workspace": None if w is None else {
                "state": w.state.value, "cpu_milli": w.cpu_milli,
                "cpu_cores": w.cpu_cores, "memory_mb": w.mem_mib,
                "disk_gb": w.disk_gib},
        })
    return out


@app.post("/api/admin/users/{user_id}/approve")
def admin_approve(user_id: int, body: PresetRequest | None = None,
                  admin: User = Depends(require_admin),
                  db: Session = Depends(get_session)) -> dict:
    user = db.get(User, user_id)
    if user is None:
        fail(404, "no_such_user", "No such user")
    if db.scalar(select(Workspace).where(Workspace.user_id == user.id)):
        fail(409, "already_has_machine", "That user already has a machine")

    idx = svc.next_free_idx(db)
    ws = Workspace(user_id=user.id, idx=idx, incus_project=f"ws-{idx}",
                   state=WorkspaceState.PROVISIONING)
    db.add(ws)
    user.status = UserStatus.APPROVED
    user.approved_at = svc.now()
    user.approved_by = admin.id
    db.commit()

    resp = svc.call_provisioner({
        "verb": "provision", "idx": idx,
        "cores": max(1, round(ws.cpu_milli / 1000)), "mem_mib": ws.mem_mib,
        "root_gib": ws.root_gib, "docker_gib": ws.docker_gib})
    if not resp.get("ok"):
        ws.state = WorkspaceState.ERROR
        ws.error = resp.get("error") or resp.get("output", "")[-500:]
        db.commit()
        svc.audit(db, admin.id, "provision_failed", user.email, error=ws.error)
        fail(500, "provision_failed", "The machine could not be created.")

    # ws-create leaves it running so it can be checked; hand it back OFF. A new
    # customer has no credit, and a running machine would bill them into debt
    # before they ever signed in.
    #
    # Toolsets are installed while the machine is still running from
    # provisioning, before it is handed back switched off - so the customer's
    # first start already has everything, and they are not billed for the
    # install time.
    installed = []
    if body and (body.presets or body.packages):
        try:
            installed = presetlib.resolve(body.presets, body.packages)
        except presetlib.PresetError as exc:
            svc.audit(db, admin.id, "preset_rejected", user.email, error=str(exc))
            installed = []
        if installed:
            r = svc.call_provisioner({"verb": "install_packages", "idx": idx,
                                      "packages": installed}, timeout=900)
            if not r.get("ok"):
                svc.audit(db, admin.id, "preset_install_failed", user.email,
                          error=(r.get("output") or r.get("error", ""))[-300:])
                installed = []

    client = _incus()
    try:
        asyncio.run(client.stop(ws.instance, ws.incus_project))
    except Exception as exc:  # noqa: BLE001
        svc.audit(db, admin.id, "post_provision_stop_failed", user.email, error=str(exc))
    finally:
        try:
            asyncio.run(client.aclose())
        except Exception:
            pass

    ws.state = WorkspaceState.OFF
    ws.desired_on = False
    ws.period_start = None
    db.commit()
    svc.audit(db, admin.id, "approve", user.email, idx=idx, packages=len(installed))
    return {"ok": True, "workspace": ws.incus_project, "state": ws.state.value,
            "installed": installed}


@app.post("/api/admin/users/{user_id}/reject")
def admin_reject(user_id: int, admin: User = Depends(require_admin),
                 db: Session = Depends(get_session)) -> dict:
    user = db.get(User, user_id)
    if user is None:
        fail(404, "no_such_user", "No such user")
    user.status = UserStatus.REJECTED
    db.commit()
    svc.audit(db, admin.id, "reject", user.email)
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/admin")
def admin_set_admin(user_id: int, body: AdminFlag,
                    admin: User = Depends(require_admin),
                    db: Session = Depends(get_session)) -> dict:
    user = db.get(User, user_id)
    if user is None:
        fail(404, "no_such_user", "No such user")
    if not body.is_admin:
        remaining = db.scalar(select(func.count(User.id)).where(
            User.is_admin.is_(True), User.id != user_id))
        if not remaining:
            fail(409, "last_admin", "This is the only administrator.")
    user.is_admin = body.is_admin
    if body.is_admin and user.status != UserStatus.APPROVED:
        user.status = UserStatus.APPROVED
    db.commit()
    svc.audit(db, admin.id, "set_admin", user.email, is_admin=body.is_admin)
    return {"ok": True, "email": user.email, "is_admin": user.is_admin}


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, admin: User = Depends(require_admin),
                      db: Session = Depends(get_session)) -> dict:
    if user_id == admin.id:
        fail(409, "cannot_delete_self", "You cannot delete your own account")
    user = db.get(User, user_id)
    if user is None:
        fail(404, "no_such_user", "No such user")
    ws = db.scalar(select(Workspace).where(Workspace.user_id == user_id))
    if ws is not None:
        resp = svc.call_provisioner({"verb": "destroy", "idx": ws.idx})
        if not resp.get("ok"):
            fail(500, "destroy_failed", "Could not remove the machine.")
    email = user.email
    db.delete(user)
    db.commit()
    svc.audit(db, admin.id, "delete_user", email)
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/credit")
def admin_credit(user_id: int, body: CreditGrant,
                 admin: User = Depends(require_admin),
                 db: Session = Depends(get_session)) -> dict:
    user = db.get(User, user_id)
    if user is None:
        fail(404, "no_such_user", "No such user")
    svc.post_transaction(db, user_id=user.id, workspace_id=None,
                         kind=TxKind.GRANT, amount_micro=round(body.credits * MICRO),
                         detail={"note": body.note, "by": admin.email})
    svc.audit(db, admin.id, "grant_credit", user.email, credits=body.credits)
    return {"ok": True, "balance": svc.balance_micro(db, user.id) / MICRO}


@app.get("/api/admin/settings")
def admin_get_settings(_: User = Depends(require_admin),
                       db: Session = Depends(get_session)) -> dict:
    from .scheduler.admission import DEFAULTS as CAP_DEFAULTS
    stored = svc.get_settings(db)
    return {**{k: str(v) for k, v in pricing.DEFAULT_RATES.items()},
            **{k: str(v) for k, v in CAP_DEFAULTS.items()}, **stored}


@app.put("/api/admin/settings")
def admin_put_settings(body: dict[str, str], admin: User = Depends(require_admin),
                       db: Session = Depends(get_session)) -> dict:
    for k, v in body.items():
        row = db.get(Setting, k)
        if row is None:
            db.add(Setting(key=k, value=str(v)))
        else:
            row.value = str(v)
    db.commit()
    svc.audit(db, admin.id, "settings_update", None, keys=sorted(body))
    return svc.get_settings(db)


@app.get("/api/admin/capacity")
def admin_capacity(_: User = Depends(require_admin),
                   db: Session = Depends(get_session)) -> dict:
    from .scheduler.admission import host_capacity
    cap = host_capacity(svc.get_settings(db))
    running = svc.running_tiers(db)
    return {"total_cores": cap.total_cores,
            "total_mem_gib": round(cap.total_mem_gib, 2),
            "schedulable_cores": cap.schedulable_cores,
            "schedulable_mem_gib": round(cap.schedulable_mem_gib, 2),
            "used_cores": sum(c for c, _ in running),
            "used_mem_gib": round(sum(m for _, m in running), 2),
            "running": len(running)}


@app.get("/api/admin/activity")
def admin_activity(limit: int = 200, _: User = Depends(require_admin),
                   db: Session = Depends(get_session)) -> dict:
    limit = max(1, min(limit, 1000))
    rows = db.scalars(select(AuditLog).order_by(desc(AuditLog.ts)).limit(limit))
    return {"events": [{"id": e.id, "ts": e.ts.isoformat() if e.ts else None,
                        "actor_id": e.actor_id, "action": e.action,
                        "target": e.target, "detail": e.detail or {}} for e in rows]}


# --- static frontend -----------------------------------------------------
if WEB.is_dir():
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    @app.get("/{full_path:path}")
    def spa(full_path: str) -> FileResponse:
        """Serve the app shell for every non-API path.

        The dashboard uses real URLs (/billing, /security, ...) via the History
        API, so a refresh or a pasted link must return the shell rather than a
        404 and let the client router resolve the route.
        """
        if full_path.startswith("api/"):
            raise HTTPException(404, "Not found")
        return FileResponse(WEB / "index.html")
