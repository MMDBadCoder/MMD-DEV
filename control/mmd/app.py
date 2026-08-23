"""MMD-DEV control plane.

Every user-facing string here deliberately avoids naming the technology
underneath. The product promise is "your own isolated Ubuntu machine"; the
user should never see the words Incus, container or ZFS, including in errors.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import logging
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pathlib import Path

log = logging.getLogger("mmd.api")
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import service as svc
from .config import CONFIG
from .db import get_session, init_db
from .incus.client import IncusClient, IncusConfig, IncusError
from .incus.execws import open_exec
from .models import (MICRO, AuditLog, CreditAccount, Setting, TxKind, User,
                     UserStatus, Workspace, WorkspaceState)
from .security import hash_password, verify_password

app = FastAPI(title="MMD-DEV", docs_url=None, redoc_url=None)

_serializer = URLSafeTimedSerializer(CONFIG.secret_key or "dev-only-insecure-key",
                                     salt="mmd-session")
COOKIE = "mmd_session"


def _incus() -> IncusClient:
    return IncusClient(IncusConfig(
        base_url=CONFIG.incus_url,
        client_cert=CONFIG.incus_client_cert,
        client_key=CONFIG.incus_client_key,
        server_cert=CONFIG.incus_server_cert,
    ))


# --- auth plumbing -------------------------------------------------------
def current_user(request: Request, db: Session = Depends(get_session)) -> User:
    raw = request.cookies.get(COOKIE)
    if not raw:
        raise HTTPException(401, "Not signed in")
    try:
        uid = _serializer.loads(raw, max_age=CONFIG.session_hours * 3600)
    except BadSignature:
        raise HTTPException(401, "Session expired")
    user = db.get(User, int(uid))
    if user is None or user.status == UserStatus.REJECTED:
        raise HTTPException(401, "Not signed in")
    if user.status == UserStatus.SUSPENDED:
        raise HTTPException(403, "Your account is suspended.")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(403, "Administrator access required")
    return user


def audit(db: Session, actor: int | None, action: str, target: str | None = None,
          **detail) -> None:
    db.add(AuditLog(actor_id=actor, action=action, target=target, detail=detail))
    db.commit()


# --- schemas -------------------------------------------------------------
class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=200)


class PowerRequest(BaseModel):
    on: bool


class TierRequest(BaseModel):
    cores: int = Field(ge=1, le=4)
    mem_mib: int = Field(ge=512, le=8192)


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10, max_length=200)


class AdminFlag(BaseModel):
    is_admin: bool


class CreditGrant(BaseModel):
    credits: float
    note: str = ""


# --- lifecycle -----------------------------------------------------------
@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


# --- static frontend -----------------------------------------------------
_WEB = Path(__file__).resolve().parents[2] / "web"
if _WEB.is_dir():
    app.mount("/static", StaticFiles(directory=_WEB), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_WEB / "index.html")


# --- auth ----------------------------------------------------------------
@app.post("/api/auth/register")
def register(body: Credentials, response: Response,
             db: Session = Depends(get_session)) -> dict:
    if db.scalar(select(User).where(User.email == body.email.lower())):
        # Deliberately the same message as success would give: revealing which
        # addresses are registered is a free user-enumeration oracle.
        return {"status": "pending",
                "message": "Your account is awaiting approval."}
    first = db.scalar(select(User).limit(1)) is None
    user = User(
        email=body.email.lower(),
        password_hash=hash_password(body.password),
        # Bootstrap: the very first account is the administrator and is
        # auto-approved, otherwise nobody could approve anybody.
        is_admin=first,
        status=UserStatus.APPROVED if first else UserStatus.PENDING,
    )
    db.add(user)
    db.commit()
    db.add(CreditAccount(user_id=user.id, balance_micro=0))
    db.commit()
    audit(db, user.id, "register", user.email, admin=first)
    return {"status": user.status.value,
            "message": ("Administrator account created."
                        if first else "Your account is awaiting approval.")}


@app.post("/api/auth/login")
def login(body: Credentials, response: Response,
          db: Session = Depends(get_session)) -> dict:
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Incorrect email or password")
    response.set_cookie(
        COOKIE, _serializer.dumps(str(user.id)),
        httponly=True, samesite="lax",
        # The dashboard is served over TLS (self-signed for now), so the
        # session cookie must never travel in the clear.
        secure=True,
        max_age=CONFIG.session_hours * 3600,
    )
    return {"status": user.status.value, "is_admin": user.is_admin}


@app.post("/api/auth/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.post("/api/auth/password")
def change_password(body: PasswordChange, user: User = Depends(current_user),
                    db: Session = Depends(get_session)) -> dict:
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(401, "Your current password is not correct")
    user.password_hash = hash_password(body.new_password)
    db.commit()
    audit(db, user.id, "password_change", user.email)
    return {"ok": True, "message": "Password changed."}


@app.get("/api/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_session)) -> dict:
    acct = db.get(CreditAccount, user.id)
    return {
        "email": user.email,
        "status": user.status.value,
        "is_admin": user.is_admin,
        "credits": (acct.balance_micro / MICRO) if acct else 0.0,
    }


# --- the developer's dashboard ------------------------------------------
def _my_workspace(db: Session, user: User) -> Workspace:
    ws = db.scalar(select(Workspace).where(Workspace.user_id == user.id))
    if ws is None:
        raise HTTPException(404, "You do not have a workspace yet.")
    return ws


@app.get("/api/workspace")
def workspace_status(user: User = Depends(current_user),
                     db: Session = Depends(get_session)) -> dict:
    if user.status == UserStatus.PENDING:
        return {"status": "pending",
                "message": "Your account is awaiting approval by an administrator."}
    ws = db.scalar(select(Workspace).where(Workspace.user_id == user.id))
    if ws is None:
        return {"status": "none", "message": "No workspace has been created yet."}

    r = svc.rates(db)
    tier = svc.tier_of(ws)
    from .billing.rates import off_hour_cost_micro
    affordable, have, need = svc.can_afford_next_hour(db, ws)
    adm = svc.check_admission(db, ws)

    return {
        "status": ws.state.value,
        "powered_on": ws.state == WorkspaceState.ON,
        "cores": ws.cores,
        "memory_mb": ws.mem_mib,
        "disk_gb": ws.disk_gib,
        "credits": svc.balance_micro(db, user.id) / MICRO,
        "rate_on_per_hour": svc.gate_micro(db, ws) / MICRO,
        "rate_off_per_hour": off_hour_cost_micro(tier, r) / MICRO,
        "can_power_on": bool(affordable and adm.allowed
                             and ws.state in (WorkspaceState.OFF,)),
        "blocked_reason": (
            None if ws.state == WorkspaceState.ON else
            (f"You need at least {need / MICRO:.2f} credits to run for another "
             f"hour; you have {have / MICRO:.2f}." if not affordable
             else (adm.reason if not adm.allowed else None))
        ),
        "archived_until": ws.purge_after.isoformat() if ws.purge_after else None,
    }


@app.post("/api/workspace/power")
async def workspace_power(body: PowerRequest, user: User = Depends(current_user),
                          db: Session = Depends(get_session)) -> dict:
    ws = _my_workspace(db, user)
    client = _incus()
    try:
        if body.on:
            if ws.state == WorkspaceState.ON:
                return {"ok": True, "status": "on"}
            if ws.state == WorkspaceState.ARCHIVED:
                raise HTTPException(409, "This workspace is archived. Add credit to restore it.")
            if ws.state != WorkspaceState.OFF:
                raise HTTPException(409, f"Workspace is {ws.state.value}; try again shortly.")

            # Gate 1: can they afford a full hour at full capacity?
            affordable, have, need = svc.can_afford_next_hour(db, ws)
            if not affordable:
                raise HTTPException(402, (
                    f"Not enough credit. Starting requires {need / MICRO:.2f} "
                    f"credits to cover one hour at full capacity; your balance "
                    f"is {have / MICRO:.2f}."))

            # Gate 2: is there room on the machine right now? Capacity is
            # claimed at power-on and released at power-off, so this is a
            # normal outcome, not an error.
            adm = svc.check_admission(db, ws)
            if not adm.allowed:
                raise HTTPException(503, adm.reason)

            ws.state = WorkspaceState.STARTING
            db.commit()
            await client.start(ws.instance, ws.incus_project)
            ws.state = WorkspaceState.ON
            ws.desired_on = True
            ws.started_at = svc.now()
            ws.period_start = svc.now()
            ws.last_activity = svc.now()
            db.commit()
            audit(db, user.id, "power_on", ws.incus_project)
            return {"ok": True, "status": "on"}

        # power off
        if ws.state == WorkspaceState.OFF:
            return {"ok": True, "status": "off"}
        ws.state = WorkspaceState.STOPPING
        db.commit()
        await client.stop(ws.instance, ws.incus_project)
        # Settle the partial hour before clearing the period, so the user is
        # billed pro-rata for the time actually used.
        if ws.period_start:
            elapsed = (svc.now() - ws.period_start).total_seconds() / 3600.0
            if elapsed > 0.001:
                svc.settle_period(db, ws, ws.period_start, powered_on=True,
                                  cpu_core_hours=0.0, mem_gib_hours=0.0,
                                  fraction=min(elapsed, 1.0))
        ws.state = WorkspaceState.OFF
        ws.desired_on = False
        ws.period_start = None
        db.commit()
        audit(db, user.id, "power_off", ws.incus_project)
        return {"ok": True, "status": "off"}
    except IncusError as exc:
        log.error("power change failed for %s: %s", ws.incus_project, exc)
        ws.state = WorkspaceState.ERROR
        ws.error = str(exc)
        db.commit()
        raise HTTPException(500, "The workspace could not be changed. Please try again.")
    except Exception as exc:  # noqa: BLE001
        # Without this, anything that is not an IncusError became a bare 500
        # with no message anywhere - undiagnosable from the outside.
        log.exception("unexpected error changing power for %s", ws.incus_project)
        ws.state = WorkspaceState.ERROR
        ws.error = f"{type(exc).__name__}: {exc}"
        db.commit()
        raise HTTPException(500, "The workspace could not be changed. Please try again.")
    finally:
        await client.aclose()


@app.post("/api/workspace/tier")
async def workspace_tier(body: TierRequest, user: User = Depends(current_user),
                         db: Session = Depends(get_session)) -> dict:
    ws = _my_workspace(db, user)
    growing = body.cores > ws.cores or body.mem_mib > ws.mem_mib
    if growing and ws.state == WorkspaceState.ON:
        adm = svc.check_admission(db, ws, cores=body.cores, mem_mib=body.mem_mib)
        if not adm.allowed:
            raise HTTPException(503, adm.reason)

    client = _incus()
    try:
        if ws.state == WorkspaceState.ON:
            # These keys are live-updatable on a running container, so the
            # developer's session is not interrupted by a resize.
            await client.patch_config(ws.instance, ws.incus_project, {
                "limits.cpu": str(body.cores),
                "limits.cpu.allowance": f"{body.cores * 100}ms/100ms",
                "limits.memory": f"{body.mem_mib}MiB",
            })
        ws.cores, ws.mem_mib = body.cores, body.mem_mib
        db.commit()
        audit(db, user.id, "tier_change", ws.incus_project,
              cores=body.cores, mem_mib=body.mem_mib)
        return {"ok": True, "cores": ws.cores, "memory_mb": ws.mem_mib,
                "rate_on_per_hour": svc.gate_micro(db, ws) / MICRO}
    except IncusError:
        raise HTTPException(500, "The size could not be changed. Please try again.")
    finally:
        await client.aclose()


# --- browser terminal ----------------------------------------------------
@app.websocket("/api/workspace/terminal")
async def terminal(sock: WebSocket) -> None:
    await sock.accept()
    db = next(get_session())
    client = None
    try:
        # WebSockets do not run FastAPI dependencies, so authenticate here.
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
            await sock.send_text("\r\n\x1b[33mYour workspace is switched off. "
                                 "Turn it on to open a terminal.\x1b[0m\r\n")
            await sock.close(code=4409); return

        ws.last_activity = svc.now()
        db.commit()

        client = _incus()
        async with open_exec(client, ws.instance, ws.incus_project,
                             user="dev") as session:

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
                    if (text := msg.get("text")) is not None:
                        # Control frames (resize) arrive as JSON text; raw
                        # keystrokes arrive as bytes.
                        if text.startswith('{"resize"'):
                            import json as _json
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
        pass
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
    for u in db.scalars(select(User).order_by(User.created_at.desc())):
        w = db.scalar(select(Workspace).where(Workspace.user_id == u.id))
        acct = db.get(CreditAccount, u.id)
        out.append({
            "id": u.id, "email": u.email, "status": u.status.value,
            "is_admin": u.is_admin,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "credits": (acct.balance_micro / MICRO) if acct else 0.0,
            "workspace": None if w is None else {
                "state": w.state.value, "cores": w.cores,
                "memory_mb": w.mem_mib, "disk_gb": w.disk_gib,
            },
        })
    return out


@app.post("/api/admin/users/{user_id}/approve")
def admin_approve(user_id: int, admin: User = Depends(require_admin),
                  db: Session = Depends(get_session)) -> dict:
    """Approving a user provisions their workspace.

    Provisioning takes ~30s and needs root, so it is handed to the provisioner
    daemon rather than performed here. This process holds only a restricted
    Incus certificate and could not create a project even if asked to.
    """
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "No such user")
    if db.scalar(select(Workspace).where(Workspace.user_id == user.id)):
        raise HTTPException(409, "That user already has a workspace")

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
        "cores": ws.cores, "mem_mib": ws.mem_mib,
        "root_gib": ws.root_gib, "docker_gib": ws.docker_gib,
    })
    if not resp.get("ok"):
        ws.state = WorkspaceState.ERROR
        ws.error = resp.get("error") or resp.get("output", "")[-500:]
        db.commit()
        audit(db, admin.id, "provision_failed", user.email, error=ws.error)
        raise HTTPException(500, "The workspace could not be created. See the audit log.")

    # ws-create leaves the instance running so it can be verified, but a newly
    # approved user has no credit yet - handing them a RUNNING workspace bills
    # them from the first second and puts them straight into debt before they
    # have ever signed in. Provisioning must hand back an OFF workspace; the
    # user turns it on when they choose to, once credit exists.
    import anyio
    client = _incus()
    try:
        anyio.from_thread.run_sync  # noqa: B018 - marker; we are sync here
    except Exception:
        pass
    try:
        asyncio.run(client.stop(ws.instance, ws.incus_project))
    except Exception as exc:  # noqa: BLE001
        audit(db, admin.id, "post_provision_stop_failed", user.email, error=str(exc))
    finally:
        try:
            asyncio.run(client.aclose())
        except Exception:
            pass

    ws.state = WorkspaceState.OFF
    ws.desired_on = False
    ws.period_start = None
    db.commit()
    audit(db, admin.id, "approve", user.email, idx=idx)
    return {"ok": True, "workspace": ws.incus_project, "state": ws.state.value}


@app.post("/api/admin/users/{user_id}/reject")
def admin_reject(user_id: int, admin: User = Depends(require_admin),
                 db: Session = Depends(get_session)) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "No such user")
    user.status = UserStatus.REJECTED
    db.commit()
    audit(db, admin.id, "reject", user.email)
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/credit")
def admin_credit(user_id: int, body: CreditGrant,
                 admin: User = Depends(require_admin),
                 db: Session = Depends(get_session)) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "No such user")
    micro = round(body.credits * MICRO)
    svc.post_transaction(db, user_id=user.id, workspace_id=None,
                         kind=TxKind.GRANT, amount_micro=micro,
                         detail={"note": body.note, "by": admin.email})
    audit(db, admin.id, "grant_credit", user.email, credits=body.credits)
    return {"ok": True, "balance": svc.balance_micro(db, user.id) / MICRO}


@app.post("/api/admin/users/{user_id}/admin")
def admin_set_admin(user_id: int, body: AdminFlag,
                    admin: User = Depends(require_admin),
                    db: Session = Depends(get_session)) -> dict:
    """Promote or demote an administrator."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "No such user")
    if not body.is_admin:
        # Refuse to remove the last administrator - that would lock everyone
        # out of approvals, credit and settings with no way back in.
        remaining = db.scalar(select(func.count(User.id)).where(
            User.is_admin.is_(True), User.id != user_id))
        if not remaining:
            raise HTTPException(409, "This is the only administrator; promote someone else first.")
    user.is_admin = body.is_admin
    if body.is_admin and user.status != UserStatus.APPROVED:
        user.status = UserStatus.APPROVED
    db.commit()
    audit(db, admin.id, "set_admin", user.email, is_admin=body.is_admin)
    return {"ok": True, "email": user.email, "is_admin": user.is_admin}


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, admin: User = Depends(require_admin),
                      db: Session = Depends(get_session)) -> dict:
    """Permanently remove an account and its workspace."""
    if user_id == admin.id:
        raise HTTPException(409, "You cannot delete your own account")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "No such user")
    ws = db.scalar(select(Workspace).where(Workspace.user_id == user_id))
    if ws is not None:
        resp = svc.call_provisioner({"verb": "destroy", "idx": ws.idx})
        if not resp.get("ok"):
            raise HTTPException(500, "Could not remove the workspace; account left in place.")
    email = user.email
    db.delete(user)          # cascades to workspace, account and ledger
    db.commit()
    audit(db, admin.id, "delete_user", email)
    return {"ok": True}


@app.get("/api/admin/settings")
def admin_get_settings(_: User = Depends(require_admin),
                       db: Session = Depends(get_session)) -> dict:
    from .billing.rates import DEFAULT_RATES
    from .scheduler.admission import DEFAULTS as CAP_DEFAULTS
    stored = svc.get_settings(db)
    merged = {**{k: str(v) for k, v in DEFAULT_RATES.items()},
              **{k: str(v) for k, v in CAP_DEFAULTS.items()}, **stored}
    return merged


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
    audit(db, admin.id, "settings_update", None, keys=sorted(body))
    return svc.get_settings(db)


@app.get("/api/admin/capacity")
def admin_capacity(_: User = Depends(require_admin),
                   db: Session = Depends(get_session)) -> dict:
    from .scheduler.admission import host_capacity
    cap = host_capacity(svc.get_settings(db))
    running = svc.running_tiers(db)
    return {
        "total_cores": cap.total_cores, "total_mem_gib": round(cap.total_mem_gib, 2),
        "schedulable_cores": cap.schedulable_cores,
        "schedulable_mem_gib": round(cap.schedulable_mem_gib, 2),
        "used_cores": sum(c for c, _ in running),
        "used_mem_gib": round(sum(m for _, m in running), 2),
        "running": len(running),
    }
