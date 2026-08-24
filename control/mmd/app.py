"""MMD-DEV control plane API.

Every user-facing string here avoids naming the technology underneath. The
product promise is "your own isolated Ubuntu machine"; the customer should
never meet the words container, Incus or ZFS - including in error messages.
"""
from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import posixpath
import secrets
import shutil
from datetime import timedelta
from pathlib import Path

from fastapi import (Depends, FastAPI, File, HTTPException, Request, Response,
                     UploadFile, WebSocket)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from . import ports as portalloc
from . import sshkeys
from . import presets as presetlib
from . import service as svc
from .billing import pricing
from .billing.pricing import MICRO, InvalidTier, Tier
from .config import CONFIG
from .db import get_session, init_db
from .incus.client import IncusClient, IncusConfig, IncusError
from .incus.execws import open_exec
from .models import (AuditLog, CreditAccount, CreditTransaction, ExposedPort,
                     PortKind, Setting, SshKey, Ticket, TicketMessage,
                     TicketStatus, TxKind, UsageSample, User, UserStatus,
                     Workspace, WorkspaceState)
from .security import hash_password, verify_password
from .tickets import is_unread

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


class SshToggle(BaseModel):
    enabled: bool


class RdpRequest(BaseModel):
    enabled: bool
    # No min_length here on purpose. Pydantic would reject a short password with
    # a 422 whose body the UI cannot translate; the handler checks the length
    # itself and answers with a proper error code instead.
    password: str | None = Field(default=None, max_length=128)


class ResetRequest(BaseModel):
    """Deliberately awkward to construct by accident.

    Both fields are required and both are checked server-side, so the
    confirmation is not something a stray click - or a script poking the API -
    can satisfy. The typed value is the account's own email address: unlike a
    fixed phrase, it cannot be copied from documentation, and unlike a checkbox
    it has to be produced rather than dismissed.
    """
    confirm: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class TicketCreate(BaseModel):
    subject: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=1, max_length=4000)


class TicketReply(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class TicketStatusChange(BaseModel):
    status: str = Field(pattern="^(open|in_progress|answered|closed)$")


class AiAction(BaseModel):
    action: str = Field(pattern="^(install|unlink)$")


class SshKeyAdd(BaseModel):
    public_key: str = Field(min_length=10, max_length=8192)


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
        return {"status": "pending", "code": "pending_approval"}
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
    # A CODE, not a sentence. The interface is Persian and translates by code;
    # returning English prose here meant the sign-up page had to compare the
    # server's exact wording to decide what to show - so a reworded string, or a
    # second caller, would silently print English at a customer.
    return {"status": user.status.value,
            "code": "admin_created" if first else "pending_approval"}


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
    # No message: the page says so in Persian. Nothing consumed this, and a
    # sentence sitting in a response is a sentence waiting to be displayed.
    return {"ok": True}


@app.get("/api/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_session)) -> dict:
    acct = db.get(CreditAccount, user.id)
    ws = db.scalar(select(Workspace).where(Workspace.user_id == user.id))
    return {
        "email": user.email, "status": user.status.value, "is_admin": user.is_admin,
        "credits": (acct.balance_micro / MICRO) if acct else 0.0,
        "has_workspace": ws is not None,
        "member_since": user.created_at.isoformat() if user.created_at else None,
        # Nav badges. A support system nobody notices a reply in is a support
        # system that looks like it never answers.
        "unread_tickets": _unread_count(db, user, staff=False),
        "unread_staff_tickets": (_unread_count(db, user, staff=True)
                                 if user.is_admin else 0),
    }


def _unread_count(db: Session, user: User, *, staff: bool) -> int:
    q = select(Ticket)
    if not staff:
        q = q.where(Ticket.user_id == user.id)
    else:
        q = q.where(Ticket.status != TicketStatus.CLOSED)
    n = 0
    for tk in db.scalars(q):
        last = tk.messages[-1] if tk.messages else None
        if is_unread(last.from_staff if last else None,
                     last.created_at if last else None,
                     tk.staff_read_at if staff else tk.user_read_at, staff=staff):
            n += 1
    return n


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
        return {"status": "pending", "code": "pending_approval"}
    ws = db.scalar(select(Workspace).where(Workspace.user_id == user.id))
    if ws is None:
        return {"status": "none", "code": "no_machine"}

    r = svc.rates(db)
    tier = svc.tier_of(ws)
    npub = svc.port_count(db, ws)
    q = pricing.quote(tier, r, port_count=npub)
    affordable, have, need = svc.can_afford_next_hour(db, ws)
    adm = svc.check_admission(db, ws)

    # Structured, not prose. This is the string a customer opened a ticket about:
    # it reached the dashboard as English, said "credits" where the product
    # charges Toman, and printed Western digits in a Persian interface. The
    # interface can render all three correctly - but only if it is given the
    # numbers rather than a finished sentence.
    blocked = None
    if ws.state == WorkspaceState.OFF:
        if not affordable:
            blocked = {"code": "insufficient_credit",
                       "need": need / MICRO, "have": have / MICRO}
        elif not adm.allowed:
            # `resource` exists precisely so the interface does not have to
            # parse the English reason. It was already there; nothing used it.
            blocked = {"code": f"capacity_{adm.resource or 'general'}"}

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
        "blocked": blocked,
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
    rows = list(db.scalars(select(ExposedPort)
                           .where(ExposedPort.workspace_id == ws.id)
                           .order_by(ExposedPort.kind, ExposedPort.internal_port)))
    return {
        "host": CONFIG.endpoint_host,
        "max_ports": portalloc.MAX_PORTS_PER_WORKSPACE,
        "rate_per_hour": svc.rates(db).port,
        "ports": [{
            "id": p.id, "internal_port": p.internal_port,
            "external_port": p.external_port, "protocol": p.protocol,
            "kind": p.kind.value,
            # Reserved ports are part of the machine, not something the
            # customer published, so they cannot be handed back.
            "removable": p.kind is PortKind.USER,
            "note": p.note,
            "address": f"{CONFIG.endpoint_host}:{p.external_port}",
            # A ready-to-click URL for the ports a customer publishes, which are
            # almost always HTTP. Not for UDP, and not for the reserved SSH and
            # RDP rows - prefixing those with a scheme would be simply wrong.
            "url": (f"http://{CONFIG.endpoint_host}:{p.external_port}"
                    if p.kind is PortKind.USER and p.protocol == "tcp" else None),
            "created_at": p.created_at.isoformat() if p.created_at else None,
        } for p in rows],
        "user_port_count": sum(1 for p in rows if p.kind is PortKind.USER),
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
            "address": f"{CONFIG.endpoint_host}:{row.external_port}",
            "url": (f"http://{CONFIG.endpoint_host}:{row.external_port}"
                    if row.protocol == "tcp" else None),
            "warning_code": ("discouraged_port"
                             if row.internal_port in portalloc.DISCOURAGED_INTERNAL
                             else None)}


@app.delete("/api/workspace/ports/{port_id}")
def delete_port(port_id: int, user: User = Depends(current_user),
                db: Session = Depends(get_session)) -> dict:
    ws = my_workspace(db, user)
    row = db.get(ExposedPort, port_id)
    if row is None or row.workspace_id != ws.id:
        fail(404, "no_such_port", "No such published port")
    if row.kind is not PortKind.USER:
        fail(409, "port_reserved", "Reserved ports cannot be removed.")
    svc.audit(db, user.id, "port_unpublish", ws.incus_project,
              internal=row.internal_port, external=row.external_port)
    portalloc.release(db, row)
    svc.sync_published_ports(db)
    return {"ok": True}


# --- connections ----------------------------------------------------------
def _service_ports(db: Session, ws: Workspace) -> dict[str, int | None]:
    """The workspace's two permanent addresses, allocating them if missing.

    The promise made on the Connections page is that these ports are reserved
    from the moment the machine exists and never change - so the customer can
    save an SSH config or an RDP shortcut before either service has ever been
    switched on. That only holds if the rows exist, and a workspace provisioned
    before this was wired up has none: both tabs then render an empty address,
    which reads as a broken product rather than an unfinished one.

    So this allocates on read. It is idempotent - portalloc.reserve_service_ports
    returns an existing reservation rather than replacing it - and it costs one
    query on the common path where both rows are already there.
    """
    rows = list(db.scalars(select(ExposedPort).where(
        ExposedPort.workspace_id == ws.id,
        ExposedPort.kind.in_([PortKind.SSH, PortKind.RDP]))))
    if len(rows) < 2:
        portalloc.reserve_service_ports(db, ws.id)
        db.commit()
        # The reservation is only half real until the host firewall knows about
        # it; without this the customer sees an address that nothing answers on.
        svc.sync_published_ports(db)
        rows = list(db.scalars(select(ExposedPort).where(
            ExposedPort.workspace_id == ws.id,
            ExposedPort.kind.in_([PortKind.SSH, PortKind.RDP]))))
    out: dict[str, int | None] = {"ssh": None, "rdp": None}
    for r in rows:
        out[r.kind.value] = r.external_port
    return out


@app.get("/api/workspace/services")
def services(request: Request, user: User = Depends(current_user),
             db: Session = Depends(get_session)) -> dict:
    ws = my_workspace(db, user)
    # The endpoint host, not the dashboard's. Two pages showing different
    # addresses for the same machine is a support ticket waiting to happen, and
    # SSH and RDP have to live wherever the published ports live.
    host = CONFIG.endpoint_host
    reserved = _service_ports(db, ws)
    keys = _keys(db, ws)
    running = ws.state == WorkspaceState.ON

    return {
        "host": host,
        "machine_running": running,
        "ssh": {
            "enabled": ws.ssh_enabled,
            "port": reserved["ssh"],
            "address": f"{host}:{reserved['ssh']}" if reserved["ssh"] else None,
            "command": (f"ssh -p {reserved['ssh']} dev@{host}"
                        if reserved["ssh"] else None),
            "keys": [{"id": k.id, "type": k.key_type, "comment": k.comment,
                      "fingerprint": k.fingerprint,
                      "created_at": k.created_at.isoformat() if k.created_at else None}
                     for k in keys],
            "key_count": len(keys),
            "can_enable": bool(keys) and running,
            "user": "dev",
        },
        "rdp": {
            "enabled": ws.rdp_enabled,
            "installed": ws.rdp_installed,
            "port": reserved["rdp"],
            "address": f"{host}:{reserved['rdp']}" if reserved["rdp"] else None,
            "available": True,
            "min_memory_mb": 2048,
            "memory_ok": ws.mem_mib >= 2048,
            "can_enable": ws.mem_mib >= 2048 and running,
            "install_mb": 236,
            "user": "dev",
        },
    }


def _keys(db: Session, ws: Workspace) -> list[SshKey]:
    return list(db.scalars(select(SshKey)
                           .where(SshKey.workspace_id == ws.id)
                           .order_by(SshKey.created_at)))


def _push_keys(db: Session, ws: Workspace) -> None:
    """Write the current key set into the machine.

    Only meaningful while SSH is switched on; when it is off the keys are just
    stored, and get written the moment it is switched on. Keeping the two
    separate is why adding a key does not silently open a listener.
    """
    if not ws.ssh_enabled:
        return
    keys = _keys(db, ws)
    if not keys:
        return
    body = ("# Managed by MMD-DEV. Edits here are replaced when keys change.\n"
            + "\n".join(k.line for k in keys) + "\n")
    resp = svc.call_provisioner({"verb": "service_ssh", "idx": ws.idx,
                                 "action": "enable", "authorized_keys": body},
                                timeout=300)
    if not resp.get("ok"):
        log.error("pushing keys failed for %s: %s", ws.incus_project, resp)
        fail(500, "ssh_failed", "The keys could not be applied.")


@app.post("/api/workspace/ssh/keys")
def add_ssh_key(body: SshKeyAdd, user: User = Depends(current_user),
                db: Session = Depends(get_session)) -> dict:
    """Add one public key. Independent of whether SSH is switched on."""
    ws = my_workspace(db, user)
    try:
        parsed = sshkeys.normalise(body.public_key)
    except sshkeys.KeyError_ as exc:
        fail(400, exc.code, str(exc))
    if len(parsed) != 1:
        fail(400, "one_key_at_a_time", "Add one key at a time.")
    k = parsed[0]

    existing = db.scalar(select(SshKey).where(
        SshKey.workspace_id == ws.id, SshKey.fingerprint == k["fingerprint"]))
    if existing is not None:
        fail(409, "key_exists", "That key is already registered.")
    if len(_keys(db, ws)) >= sshkeys.MAX_KEYS:
        fail(400, "too_many_ssh_keys", "Too many keys.")

    row = SshKey(workspace_id=ws.id, key_type=k["type"], body=k["body"],
                 comment=k["comment"] or None, fingerprint=k["fingerprint"])
    db.add(row)
    db.commit()
    _push_keys(db, ws)
    svc.audit(db, user.id, "ssh_key_added", ws.incus_project,
              fingerprint=k["fingerprint"])
    return {"ok": True, "id": row.id, "fingerprint": row.fingerprint}


@app.delete("/api/workspace/ssh/keys/{key_id}")
def remove_ssh_key(key_id: int, user: User = Depends(current_user),
                   db: Session = Depends(get_session)) -> dict:
    ws = my_workspace(db, user)
    row = db.get(SshKey, key_id)
    if row is None or row.workspace_id != ws.id:
        fail(404, "no_such_key", "No such key.")

    remaining = [k for k in _keys(db, ws) if k.id != key_id]
    if ws.ssh_enabled and not remaining:
        # Removing the last key while the listener is up would leave a service
        # nobody can authenticate to. Make the customer switch it off first, so
        # the consequence is a decision rather than a surprise.
        fail(409, "last_key", "This is the only key and SSH is switched on.")

    fp = row.fingerprint
    db.delete(row)
    db.commit()
    _push_keys(db, ws)
    svc.audit(db, user.id, "ssh_key_removed", ws.incus_project, fingerprint=fp)
    return {"ok": True}


@app.post("/api/workspace/services/rdp")
def set_rdp(body: RdpRequest, user: User = Depends(current_user),
            db: Session = Depends(get_session)) -> dict:
    """Switch the remote desktop on or off.

    The first enable also installs it (~236 MB, a few minutes). Afterwards the
    packages stay and enabling is just systemd, so the toggle is cheap.
    """
    ws = my_workspace(db, user)
    if ws.state != WorkspaceState.ON:
        fail(409, "machine_off", "The machine must be running to change this.")

    if body.enabled:
        # A desktop session plus the customer's own work does not fit in less
        # than 2 GB; below that it swaps and feels broken.
        if ws.mem_mib < 2048:
            fail(409, "rdp_needs_memory",
                 "The remote desktop needs at least 2 GB of memory.",
                 min_memory_mb=2048, current_memory_mb=ws.mem_mib)
        # xrdp authenticates through PAM, and the image creates `dev` with no
        # password at all - so without one set, nobody can ever log in.
        #
        # Required on EVERY enable, not only the first. Letting a later enable
        # silently reuse whatever was set during an earlier session hands the
        # customer a desktop on a public port guarded by a credential they may
        # no longer remember - and, if the machine changed hands, one a
        # previous holder still knows. Retyping it is cheap; a password nobody
        # can account for is not.
        if not body.password:
            fail(400, "rdp_needs_password",
                 "Choose a desktop password before switching the desktop on.")
        if len(body.password) < 8:
            fail(400, "rdp_password_short",
                 "The desktop password must be at least 8 characters.",
                 min_length=8)

        payload = {"verb": "service_rdp", "idx": ws.idx, "action": "enable",
                   "install": not ws.rdp_installed,
                   "password": body.password}
        resp = svc.call_provisioner(payload, timeout=1800)
        if not resp.get("ok"):
            log.error("rdp enable failed for %s: %s", ws.incus_project, resp)
            fail(500, "rdp_failed", "The desktop could not be switched on.",
                 output=(resp.get("output") or resp.get("error", ""))[-300:])
        ws.rdp_installed = True
        ws.rdp_enabled = True
        db.commit()
        svc.audit(db, user.id, "rdp_enabled", ws.incus_project,
                  installed=bool(resp.get("installed")))
        return {"ok": True, "enabled": True, "installed": True}

    resp = svc.call_provisioner({"verb": "service_rdp", "idx": ws.idx,
                                 "action": "disable"}, timeout=300)
    if not resp.get("ok"):
        log.error("rdp disable failed for %s: %s", ws.incus_project, resp)
        fail(500, "rdp_failed", "The desktop could not be switched off.")
    ws.rdp_enabled = False
    db.commit()
    svc.audit(db, user.id, "rdp_disabled", ws.incus_project)
    return {"ok": True, "enabled": False}


@app.post("/api/workspace/reset")
async def workspace_reset(body: ResetRequest, user: User = Depends(current_user),
                          db: Session = Depends(get_session)) -> dict:
    """Rebuild the machine from the golden image. Everything on it is destroyed.

    Kept: the reserved SSH and RDP ports, the saved public keys, the machine's
    size and the credit balance. All of those live in the dashboard rather than
    on the machine, and losing them would make a reset feel like an account
    closure.

    Gone: the filesystem, every installed package, every configuration change,
    and all Docker images, containers and volumes.
    """
    ws = my_workspace(db, user)
    # ERROR is allowed on purpose. A machine wedged by a failed operation is
    # exactly when a customer wants to start over, and refusing there would
    # leave the one state with no self-service way out. ws-reset.sh tolerates a
    # half-destroyed workspace, so this is also how a reset that died partway
    # is retried.
    if ws.state not in (WorkspaceState.ON, WorkspaceState.OFF, WorkspaceState.ERROR):
        fail(409, "reset_bad_state",
             "The machine must be running or stopped to be reset.",
             state=ws.state.value)

    # --- the two confirmations ---------------------------------------------
    # Checked in this order on purpose: the typed value is the cheap check and
    # answering it first means a wrong password is only ever reported to someone
    # who already demonstrated they know whose account this is.
    if body.confirm.strip().lower() != user.email.lower():
        fail(400, "reset_confirm_mismatch",
             "The typed confirmation does not match your email address.")
    if not verify_password(body.password, user.password_hash):
        # Same code the sign-in page uses, so a wrong password reads the same
        # here as anywhere else.
        svc.audit(db, user.id, "reset_refused", ws.incus_project, reason="password")
        fail(403, "bad_password", "That password is not correct.")

    was_on = ws.state == WorkspaceState.ON
    if was_on:
        # Settle before destroying. The machine ran for part of an hour and that
        # time was real; skipping it would quietly make "reset" the cheapest way
        # to avoid a bill.
        svc.settle_elapsed(db, ws, powered_on=True)

    ws.state = WorkspaceState.RESETTING
    ws.error = None
    db.commit()
    svc.audit(db, user.id, "reset_started", ws.incus_project, was_on=was_on)

    resp = svc.call_provisioner({
        "verb": "reset", "idx": ws.idx,
        "cores": max(1, round(ws.cpu_milli / 1000)), "mem_mib": ws.mem_mib,
        "root_gib": ws.root_gib, "docker_gib": ws.docker_gib}, timeout=1800)

    if not resp.get("ok"):
        log.error("reset failed for %s: %s", ws.incus_project, resp)
        ws.state = WorkspaceState.ERROR
        ws.error = (resp.get("error") or resp.get("output", ""))[-500:]
        db.commit()
        svc.audit(db, user.id, "reset_failed", ws.incus_project, error=ws.error[:200])
        # ws-reset.sh tolerates a half-destroyed workspace, so retrying is the
        # recovery path rather than something only an operator can unstick.
        fail(500, "reset_failed", "The machine could not be reset.",
             output=(resp.get("output") or "")[-300:])

    # ws-reset.sh leaves it running so it can be checked; hand it back off, the
    # same as a new machine. Restarting it is the customer's decision and their
    # credit.
    client = _incus()
    try:
        await client.stop(ws.instance, ws.incus_project)
    except Exception as exc:  # noqa: BLE001
        log.warning("post-reset stop failed for %s: %s", ws.incus_project, exc)
    finally:
        await client.aclose()

    ws.state = WorkspaceState.OFF
    ws.desired_on = False
    ws.period_start = None
    ws.started_at = None
    ws.last_activity = None
    # The services were installed on a filesystem that no longer exists. Leaving
    # these true would show the customer a desktop switch for software that is
    # not there, and an SSH toggle for a machine with no keys pushed.
    ws.ssh_enabled = False
    ws.ssh_keys = None
    ws.rdp_enabled = False
    ws.rdp_installed = False
    db.commit()

    # The keys themselves are kept - they are the customer's, not the machine's -
    # and go back on the machine the moment SSH is switched on again.
    svc.audit(db, user.id, "reset_done", ws.incus_project)
    return {"ok": True, "state": ws.state.value}


# --- AI tools -------------------------------------------------------------
# Workspaces are sold with the coding agents already signed in, against the
# platform's own Claude subscription. The provisioner does the copying and is
# the component that decides what may be copied; this half only decides who may
# ask. See CLAUDE_AUTH_FILE / CLAUDE_NEVER_COPY in provisioner.py.
def _ai_state(ws: Workspace) -> dict:
    if ws.state != WorkspaceState.ON:
        # Nothing can be inspected inside a stopped machine, and saying so is
        # more useful than reporting "not installed" about a machine that may
        # well have it.
        return {"machine_running": False, "installed": False, "version": None,
                "linked": False, "available": True, "expires_at": None}
    resp = svc.call_provisioner({"verb": "ai_claude", "idx": ws.idx,
                                 "action": "status"}, timeout=180)
    if not resp.get("ok"):
        log.error("claude status failed for %s: %s", ws.incus_project, resp)
        fail(502, "ai_status_failed", "The AI tool status could not be read.")
    return {"machine_running": True,
            "installed": bool(resp.get("installed")),
            "version": resp.get("version"),
            "linked": bool(resp.get("linked")),
            "available": bool(resp.get("available")),
            "expires_at": resp.get("expires_at")}


@app.get("/api/workspace/ai")
def ai_status(user: User = Depends(current_user),
              db: Session = Depends(get_session)) -> dict:
    ws = my_workspace(db, user)
    return {"claude": _ai_state(ws)}


@app.post("/api/workspace/ai/claude")
def ai_claude(body: AiAction, user: User = Depends(current_user),
              db: Session = Depends(get_session)) -> dict:
    ws = my_workspace(db, user)
    if ws.state != WorkspaceState.ON:
        fail(409, "machine_off", "The machine must be running to change this.")

    # npm install of the CLI is the slow part on a machine that does not have it
    # yet; the sign-in itself is a single small file.
    resp = svc.call_provisioner({"verb": "ai_claude", "idx": ws.idx,
                                 "action": body.action}, timeout=1200)
    if not resp.get("ok"):
        log.error("claude %s failed for %s: %s", body.action, ws.incus_project, resp)
        err = resp.get("error") or ""
        if "not signed in" in err:
            fail(409, "ai_host_unlinked",
                 "The platform account is not signed in on this host.")
        fail(500, "ai_failed", "The AI tool could not be set up.",
             output=(resp.get("output") or err)[-300:])

    svc.audit(db, user.id, f"ai_claude_{body.action}", ws.incus_project)
    return {"ok": True, "claude": _ai_state(ws)}


@app.post("/api/workspace/services/ssh")
def set_ssh(body: SshToggle, user: User = Depends(current_user),
            db: Session = Depends(get_session)) -> dict:
    """Switch the listener on or off. Keys are managed separately."""
    ws = my_workspace(db, user)
    if ws.state != WorkspaceState.ON:
        fail(409, "machine_off", "The machine must be running to change this.")

    if body.enabled:
        keys = _keys(db, ws)
        if not keys:
            fail(400, "no_ssh_key", "Add a public key before switching SSH on.")
        body_text = ("# Managed by MMD-DEV. Edits here are replaced when keys change.\n"
                     + "\n".join(k.line for k in keys) + "\n")
        resp = svc.call_provisioner({
            "verb": "service_ssh", "idx": ws.idx, "action": "enable",
            "authorized_keys": body_text}, timeout=300)
        if not resp.get("ok"):
            log.error("ssh enable failed for %s: %s", ws.incus_project, resp)
            fail(500, "ssh_failed", "SSH could not be switched on.")
        ws.ssh_enabled = True
        db.commit()
        svc.audit(db, user.id, "ssh_enabled", ws.incus_project, keys=len(keys))
        return {"ok": True, "enabled": True, "keys": len(keys)}

    resp = svc.call_provisioner({"verb": "service_ssh", "idx": ws.idx,
                                 "action": "disable"}, timeout=180)
    if not resp.get("ok"):
        log.error("ssh disable failed for %s: %s", ws.incus_project, resp)
        fail(500, "ssh_failed", "SSH could not be switched off.")
    ws.ssh_enabled = False
    db.commit()
    svc.audit(db, user.id, "ssh_disabled", ws.incus_project)
    return {"ok": True, "enabled": False}


@app.get("/api/workspace/metrics")
def workspace_metrics(hours: int = 6, user: User = Depends(current_user),
                      db: Session = Depends(get_session)) -> dict:
    """Recent CPU and memory samples, for the overview sparklines."""
    ws = my_workspace(db, user)
    hours = max(1, min(hours, 48))
    since = svc.now() - timedelta(hours=hours)
    rows = list(db.scalars(
        select(UsageSample)
        .where(UsageSample.workspace_id == ws.id, UsageSample.ts >= since)
        .order_by(UsageSample.ts)))

    cpu, mem = [], []
    for prev, cur in zip(rows, rows[1:]):
        span = (cur.ts - prev.ts).total_seconds()
        if span <= 0:
            continue
        delta = cur.cpu_seconds_total - prev.cpu_seconds_total
        # A restart resets the counter; a negative delta is not a refund.
        used = max(0.0, delta) / span
        cpu.append({"ts": cur.ts.isoformat(),
                    "value": round(min(used, ws.cpu_cores) / ws.cpu_cores * 100, 1)})
        mem.append({"ts": cur.ts.isoformat(),
                    "value": round(cur.mem_bytes / (ws.mem_mib * 1048576) * 100, 1)})
    return {"hours": hours, "cpu": cpu, "memory": mem,
            "cpu_cores": ws.cpu_cores, "memory_mb": ws.mem_mib,
            "samples": len(rows)}


# --- files ---------------------------------------------------------------
# Routed through the root provisioner because a RESTRICTED Incus certificate is
# denied the file API outright (403). Content moves via a spool directory
# rather than through the socket, so a large file is streamed rather than held
# in memory twice.
SPOOL = "/run/mmd/spool"
MAX_EDIT_BYTES = 2 * 1024 * 1024

TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".ini", ".cfg",
    ".conf", ".toml", ".json", ".jsonc", ".yaml", ".yml", ".xml", ".html",
    ".htm", ".css", ".scss", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".py", ".pyi", ".rb", ".go", ".rs", ".java", ".kt", ".c", ".h", ".cpp",
    ".hpp", ".cs", ".php", ".sh", ".bash", ".zsh", ".fish", ".sql", ".env",
    ".gitignore", ".dockerignore", ".editorconfig", ".lock", ".properties",
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"}


class PathBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class SaveBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    content: str = Field(max_length=MAX_EDIT_BYTES)


def _clean_path(path: str) -> str:
    """Absolute and normalised, or refuse.

    The machine is the customer's own, so this is about the SHAPE of the
    argument - a relative path or an embedded newline could confuse the argv
    boundary further down - not about limiting where they may look.
    """
    if not path or "\0" in path or "\n" in path or "\r" in path:
        fail(400, "bad_path", "Invalid path.")
    if not path.startswith("/"):
        fail(400, "bad_path", "Path must be absolute.")
    norm = posixpath.normpath(path)
    if not norm.startswith("/"):
        fail(400, "bad_path", "Invalid path.")
    return norm


def _token() -> str:
    return secrets.token_urlsafe(24).replace("-", "_")


def _spool(token: str) -> str:
    return os.path.join(SPOOL, token)


def _drop(token: str) -> None:
    try:
        os.unlink(_spool(token))
    except OSError:
        pass


def _running_ws(db: Session, user: User) -> Workspace:
    ws = my_workspace(db, user)
    if ws.state != WorkspaceState.ON:
        fail(409, "machine_off", "The machine must be running to browse files.")
    return ws


@app.get("/api/workspace/files")
def list_files(path: str = "/home/dev", user: User = Depends(current_user),
               db: Session = Depends(get_session)) -> dict:
    ws = _running_ws(db, user)
    p = _clean_path(path)
    resp = svc.call_provisioner({"verb": "fs_list", "idx": ws.idx, "path": p},
                                timeout=120)
    if not resp.get("ok"):
        if resp.get("error") == "not found":
            fail(404, "no_such_path", "That folder does not exist.")
        fail(500, "fs_failed", "The folder could not be read.")

    entries = []
    for e in resp.get("entries", []):
        suffix = posixpath.splitext(e["name"])[1].lower()
        entries.append({**e,
                        "editable": e["type"] == "file" and (
                            suffix in TEXT_SUFFIXES or "." not in e["name"]),
                        "image": e["type"] == "file" and suffix in IMAGE_SUFFIXES,
                        "path": posixpath.join(p, e["name"])})
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    parent = posixpath.dirname(p) if p != "/" else None
    return {"path": p, "parent": parent, "entries": entries,
            "partial": bool(resp.get("partial")), "home": "/home/dev"}


@app.get("/api/workspace/files/content")
def read_file(path: str, user: User = Depends(current_user),
              db: Session = Depends(get_session)) -> dict:
    ws = _running_ws(db, user)
    p = _clean_path(path)
    token = _token()
    resp = svc.call_provisioner({"verb": "fs_pull", "idx": ws.idx,
                                 "path": p, "token": token}, timeout=600)
    if not resp.get("ok"):
        fail(404, "no_such_path", "That file could not be read.")
    try:
        if resp.get("size", 0) > MAX_EDIT_BYTES:
            fail(413, "file_too_large",
                 "This file is too large to edit here. Download it instead.")
        raw = open(_spool(token), "rb").read()
    finally:
        _drop(token)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        fail(415, "not_text", "This file is not text and cannot be edited here.")
    return {"path": p, "content": text, "size": len(raw)}


@app.put("/api/workspace/files/content")
def save_file(body: SaveBody, user: User = Depends(current_user),
              db: Session = Depends(get_session)) -> dict:
    ws = _running_ws(db, user)
    p = _clean_path(body.path)
    token = _token()
    data = body.content.encode("utf-8")
    if len(data) > MAX_EDIT_BYTES:
        fail(413, "file_too_large", "This file is too large to save here.")
    os.makedirs(SPOOL, exist_ok=True)
    with open(_spool(token), "wb") as fh:
        fh.write(data)
    os.chmod(_spool(token), 0o660)
    try:
        resp = svc.call_provisioner({"verb": "fs_push", "idx": ws.idx,
                                     "path": p, "token": token}, timeout=600)
    finally:
        _drop(token)
    if not resp.get("ok"):
        fail(500, "fs_failed", "The file could not be saved.")
    svc.audit(db, user.id, "file_saved", ws.incus_project, path=p, bytes=len(data))
    return {"ok": True, "size": len(data)}


def _stream_and_delete(token: str, filename: str, media: str) -> FileResponse:
    spool = _spool(token)

    class _Cleanup(FileResponse):
        async def __call__(self, scope, receive, send):
            try:
                await super().__call__(scope, receive, send)
            finally:
                # The spool copy exists only for the length of this response.
                try:
                    os.unlink(spool)
                except OSError:
                    pass

    return _Cleanup(spool, media_type=media, filename=filename)


@app.get("/api/workspace/files/download")
def download_file(path: str, user: User = Depends(current_user),
                  db: Session = Depends(get_session)):
    ws = _running_ws(db, user)
    p = _clean_path(path)
    token = _token()
    resp = svc.call_provisioner({"verb": "fs_pull", "idx": ws.idx,
                                 "path": p, "token": token}, timeout=1800)
    if not resp.get("ok"):
        _drop(token)
        fail(404, "no_such_path", "That file could not be downloaded.")
    return _stream_and_delete(token, posixpath.basename(p) or "download",
                              "application/octet-stream")


@app.get("/api/workspace/files/archive")
def download_archive(path: str, user: User = Depends(current_user),
                     db: Session = Depends(get_session)):
    """Zip a folder, recursively.

    Built on the host from a recursive pull, so the workspace needs no archiver
    installed and the customer's own disk quota is not spent making their
    download.
    """
    ws = _running_ws(db, user)
    p = _clean_path(path)
    token = _token()
    resp = svc.call_provisioner({"verb": "fs_archive", "idx": ws.idx,
                                 "path": p, "token": token}, timeout=1800)
    if not resp.get("ok"):
        _drop(token)
        if "too large" in str(resp.get("error", "")):
            fail(413, "archive_too_large", "This folder is too large to download as a zip.")
        fail(500, "fs_failed", "The folder could not be packaged.")
    name = (posixpath.basename(p) or "workspace") + ".zip"
    svc.audit(db, user.id, "folder_downloaded", ws.incus_project, path=p)
    return _stream_and_delete(token, name, "application/zip")


@app.post("/api/workspace/files/upload")
async def upload_file(path: str, file: UploadFile = File(...),
                      user: User = Depends(current_user),
                      db: Session = Depends(get_session)) -> dict:
    ws = _running_ws(db, user)
    folder = _clean_path(path)
    name = posixpath.basename(file.filename or "")
    if not name or name in (".", "..") or "/" in name:
        fail(400, "bad_name", "Invalid file name.")
    dest = posixpath.join(folder, name)

    token = _token()
    os.makedirs(SPOOL, exist_ok=True)
    written = 0
    with open(_spool(token), "wb") as fh:
        while chunk := await file.read(1024 * 1024):
            written += len(chunk)
            if written > 512 * 1024 * 1024:
                fh.close(); _drop(token)
                fail(413, "file_too_large", "That file is too large to upload.")
            fh.write(chunk)
    os.chmod(_spool(token), 0o660)
    try:
        resp = svc.call_provisioner({"verb": "fs_push", "idx": ws.idx,
                                     "path": dest, "token": token}, timeout=1800)
    finally:
        _drop(token)
    if not resp.get("ok"):
        fail(500, "fs_failed", "The file could not be uploaded.")
    svc.audit(db, user.id, "file_uploaded", ws.incus_project, path=dest, bytes=written)
    return {"ok": True, "path": dest, "size": written}


@app.post("/api/workspace/files/mkdir")
def make_dir(body: PathBody, user: User = Depends(current_user),
             db: Session = Depends(get_session)) -> dict:
    ws = _running_ws(db, user)
    p = _clean_path(body.path)
    resp = svc.call_provisioner({"verb": "fs_mkdir", "idx": ws.idx, "path": p},
                                timeout=120)
    if not resp.get("ok"):
        fail(500, "fs_failed", "The folder could not be created.")
    return {"ok": True, "path": p}


@app.post("/api/workspace/files/new")
def new_file(body: PathBody, user: User = Depends(current_user),
             db: Session = Depends(get_session)) -> dict:
    ws = _running_ws(db, user)
    p = _clean_path(body.path)
    token = _token()
    os.makedirs(SPOOL, exist_ok=True)
    open(_spool(token), "wb").close()
    os.chmod(_spool(token), 0o660)
    try:
        resp = svc.call_provisioner({"verb": "fs_push", "idx": ws.idx,
                                     "path": p, "token": token}, timeout=300)
    finally:
        _drop(token)
    if not resp.get("ok"):
        fail(500, "fs_failed", "The file could not be created.")
    return {"ok": True, "path": p}


@app.delete("/api/workspace/files")
def delete_path(path: str, user: User = Depends(current_user),
                db: Session = Depends(get_session)) -> dict:
    ws = _running_ws(db, user)
    p = _clean_path(path)
    resp = svc.call_provisioner({"verb": "fs_delete", "idx": ws.idx, "path": p},
                                timeout=300)
    if not resp.get("ok"):
        if "system path" in str(resp.get("error", "")):
            fail(409, "protected_path", "That folder cannot be deleted.")
        fail(500, "fs_failed", "It could not be deleted.")
    svc.audit(db, user.id, "file_deleted", ws.incus_project, path=p)
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
            # No text. Writing English into the customer's terminal stream was
            # the one place the interface could not translate, since it arrives
            # as terminal output rather than as data. The close code carries the
            # meaning and the page prints it in Persian.
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


# --- support tickets ------------------------------------------------------
# One conversation per ticket, with the two automatic status transitions
# described on the model: a customer message reopens, a staff message answers.
MAX_OPEN_TICKETS = 10


def _ticket_json(tk: Ticket, *, staff: bool, messages: bool = False) -> dict:
    last = tk.messages[-1] if tk.messages else None
    # "Unread" means: the other side has written since this side last looked.
    unread = is_unread(last.from_staff if last else None,
                       last.created_at if last else None,
                       tk.staff_read_at if staff else tk.user_read_at,
                       staff=staff)
    out = {
        "id": tk.id,
        "subject": tk.subject,
        "status": tk.status.value,
        "created_at": tk.created_at.isoformat() if tk.created_at else None,
        "updated_at": tk.updated_at.isoformat() if tk.updated_at else None,
        "message_count": len(tk.messages),
        "last_from_staff": bool(last.from_staff) if last else None,
        "last_at": last.created_at.isoformat() if last and last.created_at else None,
        "unread": unread,
    }
    if staff:
        out["user_email"] = tk.user.email if tk.user else None
        out["user_id"] = tk.user_id
    if messages:
        out["messages"] = [{
            "id": m.id,
            "from_staff": m.from_staff,
            "body": m.body,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        } for m in tk.messages]
    return out


def _mark_read(db: Session, tk: Ticket, *, staff: bool) -> None:
    """Record that this side has seen the thread up to its last message.

    Marked against the last MESSAGE's timestamp rather than the wall clock, so
    the comparison in is_unread() is between two values that came from the same
    place. Marking with "now" instead leaves the result depending on clock
    precision, and a reply written in the same second as a read can silently
    never show as unread.
    """
    db.flush()  # so a message added in this request has its timestamp
    last = tk.messages[-1] if tk.messages else None
    when = last.created_at if last and last.created_at else svc.now()
    if staff:
        tk.staff_read_at = when
    else:
        tk.user_read_at = when


def _my_ticket(db: Session, user: User, ticket_id: int) -> Ticket:
    tk = db.get(Ticket, ticket_id)
    # Same answer for "does not exist" and "belongs to someone else", so the
    # endpoint cannot be used to count other people's tickets.
    if tk is None or tk.user_id != user.id:
        fail(404, "no_such_ticket", "No such ticket")
    return tk


@app.get("/api/tickets")
def list_tickets(user: User = Depends(current_user),
                 db: Session = Depends(get_session)) -> dict:
    rows = db.scalars(select(Ticket).where(Ticket.user_id == user.id)
                      .order_by(Ticket.updated_at.desc())).all()
    return {"tickets": [_ticket_json(t, staff=False) for t in rows],
            "max_open": MAX_OPEN_TICKETS}


@app.post("/api/tickets")
def create_ticket(body: TicketCreate, user: User = Depends(current_user),
                  db: Session = Depends(get_session)) -> dict:
    open_count = db.scalar(select(func.count()).select_from(Ticket).where(
        Ticket.user_id == user.id, Ticket.status != TicketStatus.CLOSED))
    if (open_count or 0) >= MAX_OPEN_TICKETS:
        fail(409, "too_many_tickets",
             "You already have the maximum number of open tickets.",
             max_open=MAX_OPEN_TICKETS)

    tk = Ticket(user_id=user.id, subject=body.subject.strip(),
                status=TicketStatus.OPEN)
    tk.messages.append(TicketMessage(author_id=user.id, from_staff=False,
                                     body=body.body.strip()))
    db.add(tk)
    _mark_read(db, tk, staff=False)
    db.commit()
    svc.audit(db, user.id, "ticket_opened", f"#{tk.id}", subject=tk.subject)
    return {"ok": True, "ticket": _ticket_json(tk, staff=False, messages=True)}


@app.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: int, user: User = Depends(current_user),
               db: Session = Depends(get_session)) -> dict:
    tk = _my_ticket(db, user, ticket_id)
    _mark_read(db, tk, staff=False)
    db.commit()
    return {"ticket": _ticket_json(tk, staff=False, messages=True)}


@app.post("/api/tickets/{ticket_id}/messages")
def reply_ticket(ticket_id: int, body: TicketReply,
                 user: User = Depends(current_user),
                 db: Session = Depends(get_session)) -> dict:
    tk = _my_ticket(db, user, ticket_id)
    tk.messages.append(TicketMessage(author_id=user.id, from_staff=False,
                                     body=body.body.strip()))
    # Writing to a closed ticket reopens it. The alternative - refusing the
    # message - makes the customer open a duplicate that has lost all the
    # context of the original.
    tk.status = TicketStatus.OPEN
    _mark_read(db, tk, staff=False)
    tk.updated_at = svc.now()
    db.commit()
    return {"ok": True, "ticket": _ticket_json(tk, staff=False, messages=True)}


@app.get("/api/admin/tickets")
def admin_list_tickets(status: str | None = None,
                       _: User = Depends(require_admin),
                       db: Session = Depends(get_session)) -> dict:
    q = select(Ticket)
    if status in {s.value for s in TicketStatus}:
        q = q.where(Ticket.status == TicketStatus(status))
    rows = db.scalars(q.order_by(Ticket.updated_at.desc()).limit(300)).all()
    counts = dict(db.execute(select(Ticket.status, func.count())
                             .group_by(Ticket.status)).all())
    return {"tickets": [_ticket_json(t, staff=True) for t in rows],
            "counts": {s.value: counts.get(s, 0) for s in TicketStatus}}


@app.get("/api/admin/tickets/{ticket_id}")
def admin_get_ticket(ticket_id: int, _: User = Depends(require_admin),
                     db: Session = Depends(get_session)) -> dict:
    tk = db.get(Ticket, ticket_id)
    if tk is None:
        fail(404, "no_such_ticket", "No such ticket")
    _mark_read(db, tk, staff=True)
    db.commit()
    return {"ticket": _ticket_json(tk, staff=True, messages=True)}


@app.post("/api/admin/tickets/{ticket_id}/messages")
def admin_reply_ticket(ticket_id: int, body: TicketReply,
                       admin: User = Depends(require_admin),
                       db: Session = Depends(get_session)) -> dict:
    tk = db.get(Ticket, ticket_id)
    if tk is None:
        fail(404, "no_such_ticket", "No such ticket")
    tk.messages.append(TicketMessage(author_id=admin.id, from_staff=True,
                                     body=body.body.strip()))
    # An answer that leaves the ticket looking unanswered is the failure mode
    # worth designing out; the operator can still override the status after.
    if tk.status is not TicketStatus.CLOSED:
        tk.status = TicketStatus.ANSWERED
    _mark_read(db, tk, staff=True)
    tk.updated_at = svc.now()
    db.commit()
    svc.audit(db, admin.id, "ticket_replied", f"#{tk.id}")
    return {"ok": True, "ticket": _ticket_json(tk, staff=True, messages=True)}


@app.put("/api/admin/tickets/{ticket_id}/status")
def admin_ticket_status(ticket_id: int, body: TicketStatusChange,
                        admin: User = Depends(require_admin),
                        db: Session = Depends(get_session)) -> dict:
    tk = db.get(Ticket, ticket_id)
    if tk is None:
        fail(404, "no_such_ticket", "No such ticket")
    tk.status = TicketStatus(body.status)
    tk.updated_at = svc.now()
    db.commit()
    svc.audit(db, admin.id, "ticket_status", f"#{tk.id}", status=body.status)
    return {"ok": True, "ticket": _ticket_json(tk, staff=True, messages=True)}


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

    # The two permanent addresses are part of what the customer is buying, so
    # they are allocated with the machine rather than on first use. _service_ports
    # can also do this lazily, but a reservation that only appears once someone
    # visits a page is not a reservation.
    portalloc.reserve_service_ports(db, ws.id)
    db.commit()
    svc.sync_published_ports(db)

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


@app.post("/api/admin/workspaces/{workspace_id}/apt-repair")
def admin_apt_repair(workspace_id: int, admin: User = Depends(require_admin),
                     db: Session = Depends(get_session)) -> dict:
    """Re-apply the apt configuration inside one machine.

    Machines provisioned before image/apt-fixups.sh existed still have Ubuntu's
    snap-backed firefox stub, so `apt install firefox` fails partway and leaves
    dpkg wedged. New machines get this at provision time; this is how the ones
    already out there are brought up to date without rebuilding the image.
    """
    ws = db.get(Workspace, workspace_id)
    if ws is None:
        fail(404, "no_such_workspace", "No such workspace")
    if ws.state != WorkspaceState.ON:
        fail(409, "machine_off", "The machine must be running to change this.")
    resp = svc.call_provisioner({"verb": "apt_repair", "idx": ws.idx}, timeout=900)
    if not resp.get("ok"):
        log.error("apt repair failed for %s: %s", ws.incus_project, resp)
        fail(500, "apt_repair_failed", "The package configuration could not be repaired.",
             output=(resp.get("output") or resp.get("error", ""))[-300:])
    svc.audit(db, admin.id, "apt_repair", ws.incus_project)
    return {"ok": True, "output": (resp.get("output") or "")[-300:]}


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
