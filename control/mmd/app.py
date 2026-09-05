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
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import (Depends, FastAPI, File, HTTPException, Request, Response,
                     UploadFile, WebSocket)
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel, Field
from sqlalchemy import delete, desc, func, or_, select
from sqlalchemy.orm import Session, aliased

from . import hermes
from . import operations as oplib
from . import notifications as notifylib
from . import backup as backuplib
from . import exporter
from . import mcp as mcplib
from . import metrics as m
from . import sms as smslib
from . import smscode
from . import openclaw as oclib
from . import ports as portalloc
from . import sshkeys
from . import service as svc
from .billing import aipricing, pricing
from .billing.pricing import MICRO, InvalidTier, Tier
from .config import CONFIG
from .db import SessionLocal, get_session, init_db
from .incus.client import IncusClient, IncusConfig, IncusError
from .incus.execws import open_exec
from .models import (AiModelPrice, AiUsageMark, AuditLog, CreditAccount,
                     CreditTransaction, ExposedPort, Notification, PortKind,
                     LoginAttempt, Setting, SshKey, Operation, OpenRouterAccount,
                     Ticket, TicketMessage,
                     TicketStatus, TxKind, UsageSample,
                     User, UserStatus, Workspace, WorkspaceState)
from .security import hash_password, verify_password
from . import usernames as unames
from .tickets import is_unread
from .version import APP_VERSION

# The OpenClaw gateway's fixed port inside a workspace. Must agree with
# provisioner.OPENCLAW_PORT and the vhost reconciler.
OPENCLAW_PORT = 18789

log = logging.getLogger("mmd.api")

# Must match worker.TICK_SECONDS. Sent to the interface so the charts refresh in
# step with the data rather than guessing.
SAMPLE_SECONDS = 20


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    logging.basicConfig(level=logging.INFO)
    init_db()
    _backfill_usernames()
    yield


app = FastAPI(title="MMD-DEV", docs_url=None, redoc_url=None,
              lifespan=_lifespan)

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


# --- request metrics ------------------------------------------------------
m.describe("mmd_http_requests_total", "counter",
           "API requests by route template, method and status class.")
m.describe("mmd_http_request_seconds", "histogram",
           "API request latency in seconds, by route template and method.")
m.describe("mmd_http_exceptions_total", "counter",
           "Requests that raised out of the handler.")
m.describe("mmd_auth_failures_total", "counter",
           "Failed sign-in attempts by method and reason.")
m.describe("mmd_registration_conflicts_total", "counter",
           "Rejected sign-ups by the field that conflicted.")

# Seeded at zero so the series EXISTS before the first failure. Otherwise the
# panel reads "No data", which on an operations dashboard is ambiguous in the
# worst way: it looks identical whether nothing is wrong or the exporter is
# broken. A flat zero line says "measured, and fine".
for _method, _reason in (("password", "bad_password"),
                         ("password", "no_such_user"),
                         ("sms", "sms_code_wrong"),
                         ("sms", "sms_code_expired")):
    m.inc("mmd_auth_failures_total", {"method": _method, "reason": _reason}, 0)
for _field in ("username", "phone", "code"):
    m.inc("mmd_registration_conflicts_total", {"field": _field}, 0)
for _verb in ("GET", "POST", "PUT", "DELETE"):
    m.inc("mmd_http_exceptions_total", {"method": _verb}, 0)


@app.middleware("http")
async def _record_request(request: Request, call_next):
    """Count and time every request.

    The label is the ROUTE TEMPLATE - `/api/tickets/{ticket_id}` - never the
    path that was actually requested. One is a handful of series; the other is
    one series per ticket per customer, which is how a monitoring stack gets
    taken down by the thing it was installed to watch. Same reason the status
    is a class (`2xx`) rather than a code.

    Unmatched paths collapse to a single `<unmatched>` label instead of being
    reported individually: a scanner walking random URLs must not be able to
    mint series.
    """
    start = time.monotonic()
    status = "5xx"
    try:
        response = await call_next(request)
        status = f"{response.status_code // 100}xx"
        return response
    except Exception:
        m.inc("mmd_http_exceptions_total", {"method": request.method})
        raise
    finally:
        route = request.scope.get("route")
        template = getattr(route, "path", None) or "<unmatched>"
        labels = {"route": template, "method": request.method}
        m.observe("mmd_http_request_seconds", time.monotonic() - start, labels)
        m.inc("mmd_http_requests_total", {**labels, "status": status})


@app.post("/mcp")
async def mcp_endpoint(request: Request,
                       db: Session = Depends(get_session)):
    """Model Context Protocol, for the AI support agent.

    Its own bearer token, checked before the body is even parsed. Mounted on
    the main application rather than as a separate service because it needs
    the same database session, the same audit log and the same models; a
    second process would duplicate all three to gain nothing.
    """
    supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not mcplib.authorised(supplied):
        fail(401, "mcp_auth", "Authentication failed.")
    try:
        body = await request.json()
    except ValueError:
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "invalid JSON"}}

    # A client may batch. Notifications produce no reply, so a batch of only
    # notifications correctly answers with nothing at all.
    # A fresh session per waiting check, so a long poll does not pin the
    # request's own transaction open for its whole duration.
    if isinstance(body, list):
        replies = []
        for msg in body:
            r = await mcplib.handle(db, msg, SessionLocal)
            if r:
                replies.append(r)
        return Response(status_code=204) if not replies else replies
    reply = await mcplib.handle(db, body, SessionLocal)
    return Response(status_code=204) if reply is None else reply


# --- auth plumbing -------------------------------------------------------
def current_user(request: Request, db: Session = Depends(get_session)) -> User:
    raw = request.cookies.get(COOKIE)
    if not raw:
        fail(401, "not_signed_in", "Not signed in")
    try:
        payload, signed_at = _serializer.loads(
            raw, max_age=CONFIG.session_hours * 3600, return_timestamp=True)
        # Cookies issued before session revocation existed contain only the
        # user id. They remain valid for version-zero accounts and naturally
        # stop working after the first security-sensitive change.
        if isinstance(payload, dict):
            uid = int(payload["user_id"])
            cookie_version = int(payload.get("version", 0))
        else:
            uid = int(payload)
            cookie_version = 0
        request.state.session_expires_at = signed_at + timedelta(
            hours=CONFIG.session_hours)
    except (BadSignature, KeyError, TypeError, ValueError):
        fail(401, "session_expired", "Your session has expired.")
    user = db.get(User, uid)
    if user is None or user.status in (UserStatus.REJECTED, UserStatus.DELETING):
        fail(401, "not_signed_in", "Not signed in")
    if user.status == UserStatus.SUSPENDED:
        fail(403, "suspended", "Your account is suspended.")
    if cookie_version != user.session_version:
        fail(401, "session_expired", "Your session has expired.")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        fail(403, "admin_required", "Administrator access required")
    return user


@app.get("/api/admin/auth-check")
def admin_auth_check(_: User = Depends(require_admin)) -> dict:
    return {"ok": True}


def my_workspace(db: Session, user: User) -> Workspace:
    ws = db.scalar(select(Workspace).where(Workspace.user_id == user.id))
    if ws is None:
        fail(404, "no_workspace", "You do not have a machine yet.")
    return ws


# --- schemas -------------------------------------------------------------
class SignUp(BaseModel):
    password: str = Field(min_length=8, max_length=256)
    # Not optional: it becomes part of a hostname, so it has to be chosen rather
    # than derived from an address the customer may later change.
    username: str = Field(min_length=1, max_length=64)
    full_name: str = Field(min_length=2, max_length=120)
    phone: str = Field(pattern=r"^09[0-9]{9}$")
    # Proves the number is reachable and belongs to whoever is signing up.
    code: str = Field(min_length=4, max_length=8)


class CodeRequest(BaseModel):
    phone: str = Field(pattern=r"^09[0-9]{9}$")
    purpose: str = Field(pattern=r"^(signup|login|profile|recovery)$")


class SmsLogin(BaseModel):
    phone: str = Field(pattern=r"^09[0-9]{9}$")
    code: str = Field(min_length=4, max_length=8)


class SmsPreferences(BaseModel):
    prefs: dict[str, bool] = Field(default_factory=dict)
    credit_step_toman: int | None = Field(
        default=None, ge=smslib.MIN_CREDIT_STEP_TOMAN,
        le=smslib.MAX_CREDIT_STEP_TOMAN, multiple_of=1_000)


class LoginBody(BaseModel):
    identifier: str = Field(min_length=1, max_length=64)
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=10, max_length=200)


class PasswordReset(BaseModel):
    phone: str = Field(pattern=r"^09[0-9]{9}$")
    code: str = Field(min_length=4, max_length=8)
    new_password: str = Field(min_length=10, max_length=200)


class ProfileUpdate(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    phone: str = Field(pattern=r"^09[0-9]{9}$")
    current_password: str = Field(min_length=1, max_length=256)
    code: str | None = Field(default=None, min_length=4, max_length=8)


class TelegramProfileUpdate(BaseModel):
    bot_token: str | None = Field(default=None, max_length=256)
    user_id: str | None = Field(default=None, max_length=15)
    clear: bool = False


class AdminProfileUpdate(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    phone: str = Field(pattern=r"^09[0-9]{9}$")


class PowerRequest(BaseModel):
    on: bool


class TierRequest(BaseModel):
    cpu_milli: int
    mem_mib: int


class PortRequest(BaseModel):
    """No protocol. Every published port forwards TCP and UDP alike.

    The field is still accepted and ignored so an older page, or a script
    somebody wrote against the previous API, does not start failing.
    """
    internal_port: int = Field(ge=1, le=65535)
    protocol: str | None = None
    note: str | None = Field(default=None, max_length=120)


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
    can satisfy. The typed value is the account's own username: unlike a
    fixed phrase, it cannot be copied from documentation, and unlike a checkbox
    it has to be produced rather than dismissed.
    """
    confirm: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=256)


class WorkspaceCreate(BaseModel):
    cpu_milli: int = 1000
    mem_mib: int = 1024


class AiPriceRow(BaseModel):
    """A price row. `service` names which supplier it belongs to; it defaults to
    Claude so an older admin page that never sent the field keeps working."""
    model: str = Field(min_length=1, max_length=96)
    service: str | None = None
    input_usd: float = Field(ge=0, le=10_000)
    cache_write_5m_usd: float = Field(ge=0, le=10_000)
    cache_write_1h_usd: float = Field(ge=0, le=10_000)
    cache_read_usd: float = Field(ge=0, le=10_000)
    output_usd: float = Field(ge=0, le=10_000)


class TicketCreate(BaseModel):
    subject: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=1, max_length=4000)


class TicketReply(BaseModel):
    body: str = Field(min_length=1, max_length=4000)


class TicketStatusChange(BaseModel):
    status: str = Field(pattern="^(open|in_progress|answered|escalated|closed)$")


class AiAction(BaseModel):
    """Claude Code: the CLI is installed and signed in, or the link is removed.
    ("Resync" in the interface is an install onto a machine that already has
    it - the same verb, a different label.)"""
    action: str = Field(pattern="^(install|unlink)$")


class HermesAction(BaseModel):
    """Hermes: a stated intent the worker reconciles, not an act performed here.
    A separate model from AiAction because the two vocabularies are genuinely
    different - sharing one rejected every enable with a schema error.
    """
    action: str = Field(pattern="^(enable|disable)$")
    telegram_enabled: bool | None = None
    telegram_token: str | None = Field(default=None, max_length=256)
    telegram_users: str | None = Field(default=None, max_length=256)


class SshKeyAdd(BaseModel):
    public_key: str = Field(min_length=10, max_length=8192)


class AdminFlag(BaseModel):
    is_admin: bool


class CreditGrant(BaseModel):
    credits: float
    note: str = ""


def _backfill_usernames() -> None:
    """Give every pre-existing account a username.

    The field arrived after these customers signed up. Legacy installations
    backfilled it before the legacy contact field was removed; finding a row without one now is a
    schema invariant violation rather than a reason to invent public identity.
    """
    with SessionLocal() as db:
        rows = list(db.scalars(select(User)))
        missing = [u.id for u in rows if not u.username]
        if missing:
            raise RuntimeError("users_without_username")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "version": APP_VERSION}


def _metric_label(value: object) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")




@app.get("/internal/metrics", response_class=PlainTextResponse)
def prometheus_metrics(request: Request, db: Session = Depends(get_session)) -> str:
    """Low-cardinality platform snapshot, scraped locally every 60 seconds."""
    expected = CONFIG.prometheus_token
    supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
    if not expected or not secrets.compare_digest(supplied, expected):
        fail(401, "metrics_auth", "Metrics authentication failed.")
    lines = [f'mmd_build_info{{version="{_metric_label(APP_VERSION)}"}} 1']
    lines.extend(exporter.customer_metrics(db))
    lines.extend(exporter.usage_metrics(db))
    lines.extend(exporter.operations_metrics(db))
    lines.extend(exporter.process_metrics())
    # The worker's own counters, folded in from the snapshot it persists. Age
    # is exported beside them so a dead/corrupt writer cannot masquerade as a
    # healthy worker that simply recorded zero events.
    worker_snapshot = m.read_snapshot()
    generated_at = worker_snapshot.get("generated_at")
    snapshot_age = (max(0.0, time.time() - float(generated_at))
                    if generated_at is not None else -1)
    lines.append(f"mmd_worker_metrics_snapshot_age_seconds {snapshot_age:g}")
    lines.extend(m.render(worker_snapshot))
    lines.append("# EOF")
    return "\n".join(lines) + "\n"






















# --- auth ----------------------------------------------------------------
def _signup_username(raw: str) -> str:
    name = unames.validate(raw)
    endpoint_label = (CONFIG.endpoint_host or "").split(".", 1)[0].lower()
    if endpoint_label and name == endpoint_label:
        raise unames.UsernameError("username_reserved", "That username is reserved.")
    return name


def _notify_admins(db: Session, kind: str, detail: dict | None = None,
                   dedupe_key: str | None = None) -> None:
    """Text every administrator who has a usable phone.

    Operator alerts fan out to all admins rather than to one configured
    number, because the number that matters is whoever is actually on call,
    and a single hard-coded recipient goes stale the moment they leave.
    """
    for admin in db.scalars(select(User).where(
            User.is_admin.is_(True), User.status == UserStatus.APPROVED)):
        try:
            smslib.queue(db, user_id=admin.id, phone=admin.phone, kind=kind,
                         detail=detail, user=admin,
                         dedupe_key=(f"{dedupe_key}:{admin.id}"
                                     if dedupe_key else None))
        except smslib.SmsError:
            continue
    db.commit()


def _login_throttle(db: Session, identifier: str) -> None:
    """Refuse a sign-in that has failed too often lately.

    Counted against the identifier AS TYPED, matched or not. Throttling only
    real accounts would turn the throttle into the very oracle the login path
    is careful not to be: an attacker would learn which names exist by seeing
    which ones slow down.

    A rolling window rather than a lockout: a person who mistypes their
    password eight times is not locked out for a day, they wait a quarter of
    an hour. An attacker is reduced from unlimited guesses to 32 an hour.
    """
    if CONFIG.login_max_attempts <= 0:
        return
    since = svc.now() - timedelta(minutes=CONFIG.login_window_minutes)
    recent = db.scalar(
        select(func.count(LoginAttempt.id))
        .where(LoginAttempt.identifier == identifier,
               LoginAttempt.created_at >= since)) or 0
    if recent >= CONFIG.login_max_attempts:
        m.inc("mmd_auth_failures_total",
              {"method": "password", "reason": "throttled"})
        fail(429, "too_many_attempts",
             "Too many sign-in attempts. Try again shortly.")


def _record_login_failure(db: Session, identifier: str) -> None:
    db.add(LoginAttempt(identifier=identifier[:64]))
    db.commit()


def _clear_login_failures(db: Session, identifier: str) -> None:
    """A correct password clears the slate, so one bad day does not follow
    someone into their next sign-in."""
    db.execute(delete(LoginAttempt).where(LoginAttempt.identifier == identifier))
    db.commit()


def _issue_session(response: Response, user: User) -> None:
    """One definition, because password and SMS sign-in must produce exactly
    the same session - a second copy is how the two drift in cookie flags."""
    payload = {"user_id": user.id, "version": user.session_version}
    response.set_cookie(COOKIE, _serializer.dumps(payload), httponly=True,
                        samesite="lax", secure=True,
                        max_age=CONFIG.session_hours * 3600)


def _ensure_login_allowed(user: User) -> None:
    """Refuse blocked accounts before a success cookie or login SMS exists."""
    if user.status in (UserStatus.REJECTED, UserStatus.DELETING):
        fail(401, "bad_credentials", "Incorrect username or password")
    if user.status == UserStatus.SUSPENDED:
        fail(403, "suspended", "Your account is suspended.")


def _notify_new_login(db: Session, user: User) -> None:
    """Text the owner about a sign-in they may not have made.

    Once a day at most, keyed by date: a security message that arrives on
    every sign-in is a security message people stop reading, and the point is
    that an unexpected one stands out.
    """
    try:
        smslib.queue(db, user_id=user.id, phone=user.phone, kind="new_login",
                     dedupe_key=f"login:{user.id}:{svc.now():%Y%m%d}")
        db.commit()
    except smslib.SmsError:
        pass


@app.post("/api/auth/register")
def register(body: SignUp, db: Session = Depends(get_session)) -> dict:
    try:
        username = _signup_username(body.username)
    except unames.UsernameError as e:
        fail(400, e.code, str(e))
    if db.scalar(select(User).where(User.username == username)):
        m.inc("mmd_registration_conflicts_total", {"field": "username"})
        fail(409, "username_taken", "That username is already in use.")
    if db.scalar(select(User).where(User.phone == body.phone)):
        m.inc("mmd_registration_conflicts_total", {"field": "phone"})
        fail(409, "phone_taken", "That phone number is already in use.")
    # Before anything is written: an unverified number would make phone - the
    # sole contact identity and now a sign-in credential - self-asserted.
    try:
        smscode.verify(db, body.phone, "signup", body.code)
    except smscode.CodeError as e:
        m.inc("mmd_registration_conflicts_total", {"field": "code"})
        fail(400, e.code, str(e))
    first = db.scalar(select(User).limit(1)) is None
    user = User(
        username=username,
        full_name=body.full_name.strip(), phone=body.phone,
        password_hash=hash_password(body.password),
        is_admin=first,
        status=UserStatus.APPROVED if first else UserStatus.PENDING)
    db.add(user)
    db.commit()
    db.add(CreditAccount(user_id=user.id, balance_micro=0))
    if user.status == UserStatus.APPROVED:
        db.add(OpenRouterAccount(user_id=user.id, credit_blocked=True,
                                 limit_dirty=True))
    db.commit()
    svc.audit(db, user.id, "register", user.username, first_account=first)
    if not first:
        _notify_admins(db, "admin_signup_pending", dedupe_key=f"signup:{user.id}")
    # A CODE, not a sentence. The interface is Persian and translates by code;
    # returning English prose here meant the sign-up page had to compare the
    # server's exact wording to decide what to show - so a reworded string, or a
    # second caller, would silently print English at a customer.
    return {"status": user.status.value,
            "code": "admin_created" if first else "pending_approval"}


@app.post("/api/auth/request-code")
def request_code(body: CodeRequest, db: Session = Depends(get_session)) -> dict:
    """Send a one-time code to a phone, for signing up or signing in.

    Answers identically whether or not the number belongs to an account.
    Saying "no such account" would turn this into a way to ask whether a given
    person is a customer, one number at a time - and the SMS itself already
    tells the real owner what happened.
    """
    phone = body.phone
    exists = db.scalar(select(User).where(User.phone == phone)) is not None
    if body.purpose in ("signup", "profile") and exists:
        # Answered as success, and a different message is sent instead.
        #
        # A 409 here was an enumeration oracle: anyone could learn whether a
        # phone number belongs to a customer, one number at a time, without
        # possessing the number. Refusing silently would leave the real owner
        # waiting for a code that never comes, so they are told what actually
        # happened - which is information only the number's owner receives.
        try:
            smscode.throttle(db, phone, body.purpose)
        except smscode.CodeError as e:
            fail(429, e.code, str(e))
        smslib.queue(db, user_id=None, phone=phone, kind="already_registered")
        db.commit()
        return {"ok": True}
    try:
        # Throttled even when nothing will be sent, so the shape of the
        # response cannot be used to time-probe for existing accounts.
        code = smscode.issue(db, phone, body.purpose)
    except smscode.CodeError as e:
        fail(429, e.code, str(e))
    if body.purpose in ("login", "recovery") and not exists:
        db.commit()
        return {"ok": True}
    smslib.queue(db, user_id=None, phone=phone,
                 kind=("login_code" if body.purpose == "login" else
                       "password_reset_code" if body.purpose == "recovery" else
                       "verification_code"),
                 detail={"code": code})
    db.commit()
    return {"ok": True}


@app.post("/api/auth/login-sms")
def login_sms(body: SmsLogin, response: Response,
              db: Session = Depends(get_session)) -> dict:
    """Sign in with a code instead of a password.

    The code is verified BEFORE the account is looked up, so a wrong code and
    an unknown number take the same path and return the same error.
    """
    try:
        smscode.verify(db, body.phone, "login", body.code)
    except smscode.CodeError as e:
        m.inc("mmd_auth_failures_total", {"method": "sms", "reason": e.code})
        fail(401, e.code, str(e))
    user = db.scalar(select(User).where(User.phone == body.phone))
    if user is None:
        fail(401, "bad_credentials", "Incorrect phone or code")
    _ensure_login_allowed(user)
    _issue_session(response, user)
    svc.audit(db, user.id, "sign_in_sms", user.username)
    _notify_new_login(db, user)
    return {"status": user.status.value, "is_admin": user.is_admin}


@app.post("/api/auth/reset-password")
def reset_password(body: PasswordReset,
                   db: Session = Depends(get_session)) -> dict:
    """Replace a forgotten password after proving ownership of the phone.

    Verification deliberately precedes lookup, matching SMS sign-in: unknown
    numbers and bad codes must not become an account-discovery endpoint.
    Consuming the code and changing the password share one commit, so a failed
    write cannot strand a customer with a spent recovery code.
    """
    try:
        smscode.verify(db, body.phone, "recovery", body.code)
    except smscode.CodeError as e:
        m.inc("mmd_auth_failures_total",
              {"method": "recovery", "reason": e.code})
        fail(401, e.code, str(e))
    user = db.scalar(select(User).where(User.phone == body.phone))
    if user is None:
        fail(401, "bad_credentials", "Incorrect phone or code")
    user.password_hash = hash_password(body.new_password)
    user.session_version += 1
    try:
        smslib.queue(db, user_id=user.id, phone=user.phone,
                     kind="password_changed")
    except smslib.SmsError:
        pass
    db.commit()
    svc.audit(db, user.id, "password_reset", user.username)
    return {"ok": True}


@app.get("/api/auth/username-available")
def username_available(name: str, db: Session = Depends(get_session)) -> dict:
    """Lets the sign-up form say so before the customer submits.

    Deliberately reveals only whether a name is free. That is the same thing
    submitting the form would reveal, so it leaks nothing new - and a username
    is public anyway once it is in a hostname.
    """
    try:
        candidate = _signup_username(name)
    except unames.UsernameError as e:
        return {"available": False, "code": e.code, "reason": str(e)}
    taken = db.scalar(select(User).where(User.username == candidate)) is not None
    return {"available": not taken, "username": candidate,
            "code": "username_taken" if taken else None}


@app.post("/api/auth/login")
def login(body: LoginBody, response: Response,
          db: Session = Depends(get_session)) -> dict:
    identifier = body.identifier.strip().lower()
    # Before the password is even checked, so a throttled caller learns
    # nothing from how long the answer takes.
    _login_throttle(db, identifier)
    user = db.scalar(select(User).where(
        (User.username == identifier) | (User.phone == identifier)))
    if user is None or not verify_password(body.password, user.password_hash):
        # Reason, not identity: "which account" would be an unbounded label and
        # a log of who is being targeted.
        m.inc("mmd_auth_failures_total",
              {"method": "password",
               "reason": "no_such_user" if user is None else "bad_password"})
        _record_login_failure(db, identifier)
        fail(401, "bad_credentials", "Incorrect username or password")
    _ensure_login_allowed(user)
    _clear_login_failures(db, identifier)
    _issue_session(response, user)
    svc.audit(db, user.id, "sign_in", user.username)
    _notify_new_login(db, user)
    return {"status": user.status.value, "is_admin": user.is_admin}


@app.get("/api/account/sms")
def sms_preferences_get(user: User = Depends(current_user)) -> dict:
    """Which optional messages this customer receives, all on by default."""
    return {"prefs": smslib.preferences(user),
            "kinds": smslib.OPTIONAL_KINDS,
            "credit_step_toman": (user.sms_credit_step_toman
                                  or smslib.DEFAULT_CREDIT_STEP_TOMAN),
            "credit_step_min": smslib.MIN_CREDIT_STEP_TOMAN,
            "credit_step_max": smslib.MAX_CREDIT_STEP_TOMAN}


@app.put("/api/account/sms")
def sms_preferences_put(body: SmsPreferences, user: User = Depends(current_user),
                        db: Session = Depends(get_session)) -> dict:
    """Store the customer's choices.

    Only optional kinds are writable. Account, security and code messages are
    rejected rather than silently ignored, because an interface that appears
    to switch off a takeover warning is worse than one that has no switch.
    """
    # Serialize this with the worker's band comparison. Otherwise a balance
    # crossing concurrent with a step edit can be classified against the old
    # denominator and text a change caused only by configuration.
    user = db.scalar(select(User).where(User.id == user.id).with_for_update())
    prefs = dict(user.sms_prefs or {})
    for kind, wanted in body.prefs.items():
        if kind not in smslib.OPTIONAL_KINDS:
            fail(400, "sms_kind_locked", "That message cannot be switched off.")
        prefs[kind] = bool(wanted)
    user.sms_prefs = prefs
    if body.credit_step_toman is not None:
        user.sms_credit_step_toman = body.credit_step_toman
        # Changing n changes floor(balance/n) without changing the balance.
        # Establish the new baseline in the same transaction so a settings
        # edit can never be announced as money entering or leaving the account.
        balance = svc.balance_micro(db, user.id)
        user.sms_credit_band = balance // (body.credit_step_toman * MICRO)
    # The column is JSON; SQLAlchemy needs the reassignment above to see the
    # change, which is why this is not an in-place mutation.
    db.commit()
    svc.audit(db, user.id, "sms_preferences", user.username)
    return sms_preferences_get(user)


@app.post("/api/auth/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.post("/api/auth/password")
def change_password(body: PasswordChange, response: Response,
                    user: User = Depends(current_user),
                    db: Session = Depends(get_session)) -> dict:
    if not verify_password(body.current_password, user.password_hash):
        fail(401, "wrong_password", "Your current password is not correct")
    user.password_hash = hash_password(body.new_password)
    user.session_version += 1
    try:
        smslib.queue(db, user_id=user.id, phone=user.phone,
                     kind="password_changed")
    except smslib.SmsError:
        pass
    db.commit()
    _issue_session(response, user)
    svc.audit(db, user.id, "password_change", user.username)
    # No message: the page says so in Persian. Nothing consumed this, and a
    # sentence sitting in a response is a sentence waiting to be displayed.
    return {"ok": True}


@app.get("/api/me")
def me(user: User = Depends(current_user), db: Session = Depends(get_session)) -> dict:
    acct = db.get(CreditAccount, user.id)
    ws = db.scalar(select(Workspace).where(Workspace.user_id == user.id))
    return {
        "username": user.username,
        "full_name": user.full_name, "phone": user.phone,
        "telegram_user_id": user.telegram_user_id,
        "telegram_configured": bool(user.telegram_bot_token and user.telegram_user_id),
        "version": APP_VERSION,
        "status": user.status.value, "is_admin": user.is_admin,
        "credits": (acct.balance_micro / MICRO) if acct else 0.0,
        "has_workspace": ws is not None,
        "member_since": user.created_at.isoformat() if user.created_at else None,
        # Nav badges. A support system nobody notices a reply in is a support
        # system that looks like it never answers.
        "unread_tickets": _unread_count(db, user, staff=False),
        "unread_staff_tickets": (_unread_count(db, user, staff=True)
                                 if user.is_admin else 0),
    }


def _validate_identity(db: Session, user: User, phone: str) -> None:
    other_phone = db.scalar(select(User).where(User.phone == phone, User.id != user.id))
    if other_phone:
        fail(409, "phone_taken", "That phone number is already in use.")


def _apply_identity(user: User, full_name: str, phone: str) -> None:
    name = full_name.strip()
    if len(name) < 2:
        fail(400, "full_name_invalid", "The full name is too short.")
    user.full_name = name
    user.phone = phone


@app.put("/api/profile")
def update_profile(body: ProfileUpdate, response: Response,
                   user: User = Depends(current_user),
                   db: Session = Depends(get_session)) -> dict:
    if not verify_password(body.current_password, user.password_hash):
        fail(401, "wrong_password", "Your current password is not correct")
    _validate_identity(db, user, body.phone)
    phone_changed = body.phone != user.phone
    if phone_changed:
        if not body.code:
            fail(400, "phone_verification_required",
                 "Verify the new phone number first.")
        try:
            smscode.verify(db, body.phone, "profile", body.code)
        except smscode.CodeError as e:
            fail(400, e.code, str(e))
    _apply_identity(user, body.full_name, body.phone)
    if phone_changed:
        user.session_version += 1
    db.commit()
    if phone_changed:
        _issue_session(response, user)
    svc.audit(db, user.id, "profile_change", user.username)
    return {"ok": True}


@app.put("/api/profile/telegram")
def update_telegram_profile(body: TelegramProfileUpdate,
                            user: User = Depends(current_user),
                            db: Session = Depends(get_session)) -> dict:
    if body.clear:
        user.telegram_bot_token = None
        user.telegram_user_id = None
    else:
        token = (body.bot_token or user.telegram_bot_token or "").strip()
        telegram_user_id = (body.user_id or "").strip()
        if not re.fullmatch(r"[0-9]{6,15}:[A-Za-z0-9_-]{20,}", token):
            fail(400, "telegram_bad_token", "The Telegram bot token is invalid.")
        if not re.fullmatch(r"[1-9][0-9]{4,14}", telegram_user_id):
            fail(400, "telegram_bad_users", "The Telegram user ID is invalid.")
        user.telegram_bot_token = token
        user.telegram_user_id = telegram_user_id
    db.commit()
    svc.audit(db, user.id, "telegram_profile_change", user.username,
              configured=not body.clear)
    return {"ok": True, "configured": bool(user.telegram_bot_token),
            "user_id": user.telegram_user_id}


def _unread_count(db: Session, user: User, *, staff: bool) -> int:
    """How many threads the other side has added to since this side last looked.

    One query. It used to walk the tickets in Python and touch `tk.messages` per
    ticket, which lazy-loads a second query each time - fine when /api/me was
    fetched once per session, wasteful now that it is fetched on every page
    change to keep the badge honest.

    "Last message" is max(id), not max(created_at): ids are monotonic per insert
    and two messages can share a timestamp.
    """
    last = (select(TicketMessage.ticket_id.label("tid"),
                   func.max(TicketMessage.id).label("mid"))
            .group_by(TicketMessage.ticket_id).subquery())
    m = aliased(TicketMessage)

    q = (select(func.count()).select_from(Ticket)
         .join(last, last.c.tid == Ticket.id)
         .join(m, m.id == last.c.mid))

    if staff:
        # A closed thread is not waiting on anyone.
        read_at, from_them = Ticket.staff_read_at, m.from_staff.is_(False)
        q = q.where(Ticket.status != TicketStatus.CLOSED)
    else:
        read_at, from_them = Ticket.user_read_at, m.from_staff.is_(True)
        q = q.where(Ticket.user_id == user.id)

    return db.scalar(q.where(from_them,
                             or_(read_at.is_(None), m.created_at > read_at))) or 0


# --- sizes ---------------------------------------------------------------
@app.get("/api/tiers")
def tiers(user: User = Depends(current_user),
          db: Session = Depends(get_session)) -> dict:
    """The size menu plus what each option costs. One source of truth."""
    r = svc.rates(db)
    ws = db.scalar(select(Workspace).where(Workspace.user_id == user.id))
    disk = ws.disk_gib if ws else (pricing.DEFAULT_ROOT_GIB + pricing.DEFAULT_DOCKER_GIB)
    options = []
    for cm in pricing.CPU_OPTIONS_MILLI:
        for mm in pricing.MEM_OPTIONS_MIB:
            t = Tier(cm, mm, disk)
            q = pricing.quote(t, r)
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
    q = pricing.quote(tier, r)
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
        # Shown to customers in DAYS. At the default tier a funded account has
        # hundreds of hours left, and "۱۶۶۶۶٫۷ ساعت" is a number nobody can act
        # on. Hours are kept in the payload because they are the honest unit the
        # figure is derived in.
        "hours_remaining": (have / MICRO) / q["max_per_hour"] if q["max_per_hour"] else 0,
        "days_remaining": ((have / MICRO) / q["max_per_hour"] / 24
                           if q["max_per_hour"] else 0),
        "published_ports": npub,
        "can_power_on": bool(ws.state == WorkspaceState.OFF and affordable and adm.allowed),
        "blocked": blocked,
        "started_at": ws.started_at.isoformat() if ws.started_at else None,
        "archived_until": ws.purge_after.isoformat() if ws.purge_after else None,
        # The end of THIS run, or null when the customer has asked for it to
        # keep going. `auto_stop_hours` is sent whether or not a deadline is
        # armed, because the interface has to state the rule up front - the
        # customer should learn about the limit from the machine page, not from
        # a machine that stopped.
        "auto_stop_hours": CONFIG.auto_stop_hours,
        "auto_stop_at": (ws.auto_stop_at.isoformat()
                         if ws.auto_stop_at and ws.state == WorkspaceState.ON
                         else None),
    }


@app.post("/api/workspace")
def create_workspace(body: WorkspaceCreate, user: User = Depends(current_user),
                     db: Session = Depends(get_session)) -> dict:
    """Queue compute creation only when an approved customer asks for it."""
    if user.status != UserStatus.APPROVED:
        fail(409, "pending_approval", "The account is not approved yet.")
    if db.scalar(select(Workspace).where(Workspace.user_id == user.id)) is not None:
        fail(409, "already_has_machine", "This account already has a machine.")
    try:
        Tier(cpu_milli=body.cpu_milli, mem_mib=body.mem_mib,
             disk_gib=pricing.DEFAULT_ROOT_GIB + pricing.DEFAULT_DOCKER_GIB)
    except (InvalidTier, ValueError):
        fail(400, "invalid_size", "That machine size is not available.")
    idx = svc.next_free_idx(db)
    ws = Workspace(user_id=user.id, idx=idx, incus_project=f"ws-{idx}",
                   state=WorkspaceState.PROVISIONING,
                   cpu_milli=body.cpu_milli, mem_mib=body.mem_mib)
    db.add(ws)
    db.commit()
    op = oplib.create(db, kind="workspace_create", user_id=user.id,
                      workspace_id=ws.id, actor_id=user.id,
                      detail={"cpu_milli": ws.cpu_milli, "mem_mib": ws.mem_mib})
    svc.audit(db, user.id, "workspace_create_started", ws.incus_project)
    return {"ok": True, "operation": oplib.view(op)}


@app.post("/api/workspace/delete")
def delete_workspace(body: ResetRequest, user: User = Depends(current_user),
                     db: Session = Depends(get_session)) -> dict:
    """Delete compute and public addresses while preserving the account key."""
    ws = my_workspace(db, user)
    if body.confirm.strip().lower() != user.username.strip().lower():
        fail(400, "delete_confirm_mismatch", "The confirmation does not match.")
    if not verify_password(body.password, user.password_hash):
        fail(401, "wrong_password", "The current password is not correct.")
    if ws.state in (WorkspaceState.DELETING, WorkspaceState.RESETTING):
        fail(409, "busy", "The machine is busy.")
    if ws.state == WorkspaceState.ON:
        svc.settle_elapsed(db, ws, powered_on=True)
    ws.desired_on = False
    ws.state = WorkspaceState.DELETING
    db.commit()
    op = oplib.create(db, kind="workspace_delete", user_id=user.id,
                      workspace_id=ws.id, actor_id=user.id)
    svc.audit(db, user.id, "workspace_delete_started", ws.incus_project)
    return {"ok": True, "operation": oplib.view(op)}


@app.post("/api/workspace/keep-running")
def keep_running(user: User = Depends(current_user),
                 db: Session = Depends(get_session)) -> dict:
    """Let this run continue until the customer stops it themselves.

    Scoped to the current run on purpose. The next start arms a new deadline,
    so this cannot become a setting someone ticks once and then forgets while a
    machine bills for a month - which is the cost the automatic stop exists to
    prevent. Making it permanent would give the feature away to exactly the
    customers it is meant to help.
    """
    ws = my_workspace(db, user)
    if ws.state != WorkspaceState.ON:
        fail(409, "machine_off", "The machine must be running to change this.")
    if ws.auto_stop_at is None:
        return {"ok": True, "auto_stop_at": None}     # already opted out
    svc.disarm_auto_stop(ws)
    db.commit()
    svc.audit(db, user.id, "auto_stop_waived", ws.incus_project)
    return {"ok": True, "auto_stop_at": None}


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
            # Every run starts on the clock. The customer can take it off, but
            # only for this run.
            svc.arm_auto_stop(ws)
            db.commit()
            svc.audit(db, user.id, "power_on", ws.incus_project,
                      auto_stop_at=ws.auto_stop_at.isoformat())
            # Both, so the dialog the interface raises here can state the rule
            # without a second request.
            return {"ok": True, "status": "on",
                    "auto_stop_hours": CONFIG.auto_stop_hours,
                    "auto_stop_at": ws.auto_stop_at.isoformat()}

        if ws.state == WorkspaceState.OFF:
            return {"ok": True, "status": "off"}
        ws.state = WorkspaceState.STOPPING
        db.commit()
        await client.stop(ws.instance, ws.incus_project)
        svc.settle_elapsed(db, ws, powered_on=True)
        ws.state = WorkspaceState.OFF
        ws.desired_on = False
        ws.period_start = None
        # A deadline on a stopped machine would be read as "stop it again" by
        # anything that looks at the column without checking state first.
        svc.disarm_auto_stop(ws)
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
        q = pricing.quote(new_tier, svc.rates(db))
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
def _port_view(p: ExposedPort, username: str | None) -> dict:
    """One row, with BOTH of its addresses.

    The numeric one is what it has always been. The second puts the customer's
    own name in front of the same port, and it is a real address rather than a
    label: every name under the domain resolves to this host and the DNAT rule
    keys on the PORT, so `ali.<domain>:24815` reaches exactly what
    `<ip>:24815` reaches. Measured against the live host before it was shown to
    anyone.

    What it is NOT is a way to drop the port number. Nothing in a TCP or UDP
    packet carries the hostname the customer typed - that only exists inside
    HTTP, as the `Host:` header - so the port is what selects the workspace and
    the name is a nicer way to write the address it is attached to.

    Only the customer's own published ports get it. The reserved SSH and RDP
    rows keep exactly the address they have always had.
    """
    # Whether the hostname route can exist at all is decided by the vhost
    # reconciler, not here: it skips any port the host itself listens on,
    # because nginx cannot bind one and a failed bind abandons the whole
    # reload. `web_ready` carries that answer back, so this reports the name
    # and lets the interface gate the LINK on readiness.
    host = (unames.application_host(username, p.internal_port, CONFIG.domain)
            if username and p.kind is PortKind.USER else None)
    protos = portalloc.expand(p.protocol)
    return {
        "id": p.id, "internal_port": p.internal_port,
        "external_port": p.external_port,
        "protocol": p.protocol,
        "protocols": list(protos),
        "kind": p.kind.value,
        # Reserved ports are part of the machine, not something the
        # customer published, so they cannot be handed back.
        "removable": p.kind is PortKind.USER,
        "note": p.note,
        "address": f"{CONFIG.endpoint_host}:{p.external_port}",
        "host_address": f"{host}:{p.internal_port}" if host else None,
        "web_ready": bool(p.web_ready),
        # A ready-to-click URL for the ports a customer publishes, which are
        # almost always HTTP. Not for the reserved SSH and RDP rows - prefixing
        # those with a scheme would be wrong rather than merely unhelpful.
        # Offered on the named form too, since that is the one worth reading.
        "url": (f"http://{CONFIG.endpoint_host}:{p.external_port}"
                if p.kind is PortKind.USER and "tcp" in protos else None),
        "host_url": (f"http://{host}:{p.internal_port}"
                     if host and p.web_ready and "tcp" in protos else None),
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


@app.get("/api/workspace/ports")
def list_ports(request: Request, user: User = Depends(current_user),
               db: Session = Depends(get_session)) -> dict:
    ws = my_workspace(db, user)
    rows = list(db.scalars(select(ExposedPort)
                           .where(ExposedPort.workspace_id == ws.id)
                           .order_by(ExposedPort.kind, ExposedPort.internal_port)))
    return {
        "host": CONFIG.endpoint_host,
        "domain": CONFIG.domain,
        "username": user.username,
        "max_ports": portalloc.MAX_PORTS_PER_WORKSPACE,
        "ports": [_port_view(p, user.username) for p in rows],
        "user_port_count": sum(1 for p in rows if p.kind is PortKind.USER),
    }


@app.post("/api/workspace/ports")
def create_port(body: PortRequest, request: Request,
                user: User = Depends(current_user),
                db: Session = Depends(get_session)) -> dict:
    ws = my_workspace(db, user)
    try:
        # body.protocol is ignored on purpose - see PortRequest.
        row = portalloc.allocate(db, ws.id, body.internal_port,
                                 portalloc.PROTO_BOTH, body.note)
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
    return {"ok": True, **_port_view(row, user.username),
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
    # Remove reachability BEFORE forgetting the reservation. The old order
    # deleted the row first; if nftables then failed, the kernel kept forwarding
    # an address the database no longer knew existed, so no later reconciliation
    # could identify and remove it.
    resp = svc.sync_published_ports(db, exclude_port_id=row.id)
    if not resp.get("ok"):
        fail(500, "port_failed", "The published port could not be removed.")
    svc.audit(db, user.id, "port_unpublish", ws.incus_project,
              internal=row.internal_port, external=row.external_port)
    portalloc.release(db, row)
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
    applications = list(db.scalars(select(ExposedPort).where(
        ExposedPort.workspace_id == ws.id, ExposedPort.kind == PortKind.USER)
        .order_by(ExposedPort.created_at)))

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
        "applications": [_port_view(port, user.username) for port in applications],
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
    if body.confirm.strip().lower() != user.username.lower():
        fail(400, "reset_confirm_mismatch",
             "The typed confirmation does not match your username.")
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

    op = oplib.create(db, kind="factory_reset", user_id=user.id,
                      workspace_id=ws.id, actor_id=user.id,
                      detail={"cores": max(1, round(ws.cpu_milli / 1000)),
                              "mem_mib": ws.mem_mib, "root_gib": ws.root_gib,
                              "docker_gib": ws.docker_gib})
    return {"ok": True, "code": "operation_queued", "state": ws.state.value,
            "operation": oplib.view(op)}


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
                "linked": False, "onboarded": False, "available": True,
                "expires_at": None}
    resp = svc.call_provisioner({"verb": "ai_claude", "idx": ws.idx,
                                 "action": "status"}, timeout=180)
    if not resp.get("ok"):
        log.error("claude status failed for %s: %s", ws.incus_project, resp)
        fail(502, "ai_status_failed", "The AI tool status could not be read.")
    return {"machine_running": True,
            "installed": bool(resp.get("installed")),
            "version": resp.get("version"),
            # Signed in. NOT the same as usable: a machine with credentials but
            # no onboarding key opens the first-run wizard, which customers
            # report as being asked to log in.
            "linked": bool(resp.get("linked")),
            "onboarded": bool(resp.get("onboarded")),
            "available": bool(resp.get("available")),
            "expires_at": resp.get("expires_at")}


def _openrouter_state(user: User, account: OpenRouterAccount | None) -> dict:
    return {"ready": bool(account and account.key_hash),
            "credit_blocked": bool(account.credit_blocked) if account else True,
            "limit_sync_pending": bool(account.limit_dirty) if account else True,
            "limit_usd": account.limit_usd if account else None,
            "limit_synced_at": (account.limit_synced_at.isoformat()
                                if account and account.limit_synced_at else None),
            "key": account.key if account else None,
            "error": bool(account and account.error)}


def _hermes_state(ws: Workspace, account: OpenRouterAccount | None = None) -> dict:
    """What the customer may see about their Hermes service.

    The API deliberately cannot mint or revoke keys - the OpenRouter management
    key is loaded only by mmd-worker. So this reports state and nothing else,
    and `enabled` without `key` means "the worker has not got to it yet" rather
    than an error.

    The key itself IS returned. It spends only this customer's capped credit,
    it is revoked in one call, and the agent reads it from a machine they have
    root on - so withholding it from its owner would protect nothing while
    making the product harder to use.
    """
    return {"enabled": bool(ws.hermes_enabled),
            "ready": bool(ws.hermes_installed and ws.hermes_vhost_ready),
            "credit_blocked": bool(account.credit_blocked) if account else True,
            "dashboard_user": ws.hermes_dash_user,
            "dashboard_password": ws.hermes_dash_password,
            "dashboard_ready": bool(ws.hermes_vhost_ready),
            "host": unames.hermes_host(ws.user.username, CONFIG.domain)
            if ws.user and ws.user.username else None,
            "machine_running": ws.state == WorkspaceState.ON,
            "telegram_enabled": bool(ws.hermes_telegram_enabled),
            "telegram_ready": bool(ws.hermes_telegram_installed),
            "telegram_users": ws.hermes_telegram_users,
            "telegram_profile_configured": bool(
                ws.user and ws.user.telegram_bot_token and ws.user.telegram_user_id),
            "telegram_profile_user_id": ws.user.telegram_user_id if ws.user else None,
            "telegram_error": bool(ws.hermes_telegram_error),
            # A FLAG, not the text. The stored value is whatever OpenRouter or
            # the provisioner said, in English, with HTTP status codes and JSON
            # in it - useful to an operator, meaningless and alarming to a
            # customer reading a Persian page. The interface writes the
            # sentence; the raw text stays for the admin view.
            "error": bool(ws.hermes_error)}


def _codex_state(ws: Workspace) -> dict:
    """Codex, read the same way Claude Code is: probed live, nothing stored.

    Both are host-authenticated CLIs whose only workspace state is a
    credentials file, so there is nothing worth keeping a column for - and a
    stored copy would be a second thing that could disagree with the machine.
    """
    if ws.state != WorkspaceState.ON:
        return {"machine_running": False, "installed": False, "version": None,
                "linked": False, "available": True, "expires_at": None}
    resp = svc.call_provisioner({"verb": "ai_codex", "idx": ws.idx,
                                 "action": "status"}, timeout=180)
    if not resp.get("ok"):
        log.error("codex status failed for %s: %s", ws.incus_project, resp)
        fail(502, "ai_status_failed", "The AI tool status could not be read.")
    return {"machine_running": True,
            "installed": bool(resp.get("installed")),
            "version": resp.get("version"),
            "linked": bool(resp.get("linked")),
            "available": bool(resp.get("available")),
            "expires_at": resp.get("expires_at")}


def _openclaw_state(ws: Workspace, account: OpenRouterAccount | None = None) -> dict:
    """What the customer may see about their OpenClaw gateway.

    Reported from the database rather than probed, because this is a
    worker-reconciled service like Hermes: `enabled` is intent, `installed` is
    what the worker has actually achieved, and the gap between them is the
    "preparing" state the page shows.
    """
    host = (unames.openclaw_host(ws.user.username, CONFIG.domain)
            if ws.user and ws.user.username else None)
    user = ws.user
    return {"enabled": bool(ws.openclaw_enabled),
            "installed": bool(ws.openclaw_installed),
            "ready": bool(ws.openclaw_installed and ws.openclaw_password),
            "telegram_enabled": bool(ws.openclaw_telegram_enabled),
            "telegram_ready": bool(ws.openclaw_telegram_installed),
            "telegram_error": bool(ws.openclaw_telegram_error),
            # Whether the ACCOUNT has a token saved. The token itself is never
            # returned - only whether one exists, so the page can offer the
            # toggle instead of a field the customer has already filled in.
            "telegram_profile_configured": bool(
                user and user.telegram_bot_token and user.telegram_user_id),
            # OpenClaw has no allowlist of its own - it always uses the
            # account's saved id - but the page shows the same two facts for
            # both services, so both have to report them under the same names.
            "telegram_profile_user_id": user.telegram_user_id if user else None,
            "telegram_users": user.telegram_user_id if user else None,
            "password": ws.openclaw_password,
            "host": host,
            "port": OPENCLAW_PORT,
            "machine_running": ws.state == WorkspaceState.ON,
            # Requires the customer's managed OpenRouter key: OpenClaw spends
            # it, which is what keeps this inside the cap and the metering that
            # already exist rather than opening a second way to spend money.
            "needs_openrouter": not bool(account and account.key),
            # A FLAG, not the text. The stored value is whatever npm or systemd
            # said, in English, with paths in it - useful to an operator,
            # meaningless and alarming to a customer reading a Persian page.
            "error": bool(ws.openclaw_error)}


def _managed_web_state(ws: Workspace, account: OpenRouterAccount | None,
                       name: str) -> dict:
    installed = bool(getattr(ws, f"{name}_installed"))
    vhost_ready = bool(getattr(ws, f"{name}_vhost_ready"))
    host_fn = unames.opencode_host if name == "opencode" else unames.openwebui_host
    minimum_memory_mib = 2048 if name == "openwebui" else 0
    enabled = bool(getattr(ws, f"{name}_enabled"))
    has_error = bool(getattr(ws, f"{name}_error"))
    return {"enabled": enabled,
            "installed": installed, "vhost_ready": vhost_ready,
            "ready": bool(enabled and installed and vhost_ready and not has_error),
            "machine_running": ws.state == WorkspaceState.ON,
            "minimum_memory_mib": minimum_memory_mib,
            "needs_memory": bool(minimum_memory_mib and ws.mem_mib < minimum_memory_mib),
            "needs_openrouter": not bool(account and account.key),
            "host": host_fn(ws.user.username, CONFIG.domain),
            "username": ("opencode" if name == "opencode" else
                         f"{ws.user.username}@mmd.local"),
            "password": getattr(ws, f"{name}_password"),
            "error": has_error}


@app.get("/api/workspace/ai")
def ai_status(user: User = Depends(current_user),
              db: Session = Depends(get_session)) -> dict:
    account = db.get(OpenRouterAccount, user.id)
    ws = db.scalar(select(Workspace).where(Workspace.user_id == user.id))
    empty = {"machine_running": False, "installed": False, "version": None,
             "linked": False, "available": True, "expires_at": None,
             "needs_workspace": True}
    return {"has_workspace": ws is not None,
            "openrouter": _openrouter_state(user, account),
            "claude": _ai_state(ws) if ws else dict(empty),
            "hermes": _hermes_state(ws, account) if ws else {"enabled": False,
                "ready": False, "machine_running": False, "needs_workspace": True},
            "codex": _codex_state(ws) if ws else dict(empty),
            "openclaw": _openclaw_state(ws, account) if ws else {"enabled": False,
                "ready": False, "machine_running": False, "needs_workspace": True},
            "opencode": _managed_web_state(ws, account, "opencode") if ws else {
                "enabled": False, "ready": False, "machine_running": False, "needs_workspace": True},
            "openwebui": _managed_web_state(ws, account, "openwebui") if ws else {
                "enabled": False, "ready": False, "machine_running": False, "needs_workspace": True}}


@app.post("/api/workspace/managed-ai/{service}")
def ai_managed_web(service: str, body: HermesAction,
                   user: User = Depends(current_user),
                   db: Session = Depends(get_session)) -> dict:
    if service not in ("opencode", "openwebui"):
        fail(404, "unknown_service", "Unknown service.")
    ws = my_workspace(db, user)
    if ws.state != WorkspaceState.ON:
        fail(409, "machine_off", "The machine must be running to change this.")
    account = db.get(OpenRouterAccount, user.id)
    if body.action == "enable" and not (account and account.key):
        fail(409, "openrouter_not_ready", "The OpenRouter key is not ready.")
    setattr(ws, f"{service}_enabled", body.action == "enable")
    setattr(ws, f"{service}_error", None)
    if body.action == "disable":
        setattr(ws, f"{service}_vhost_ready", False)
        setattr(ws, f"{service}_password", None)
    db.commit()
    svc.audit(db, user.id, f"ai_{service}_{body.action}", ws.incus_project)
    return {"ok": True, service: _managed_web_state(ws, account, service)}


@app.post("/api/workspace/ai/codex")
def ai_codex(body: AiAction, user: User = Depends(current_user),
             db: Session = Depends(get_session)) -> dict:
    """Install and sign in, or remove the link. Mirrors /ai/claude exactly.

    The sign-in itself is the platform's, performed once on the host; this
    copies the allowlisted half of that grant into the customer's machine. The
    customer never authenticates to OpenAI and never sees a token.
    """
    ws = my_workspace(db, user)
    if ws.state != WorkspaceState.ON:
        fail(409, "machine_off", "The machine must be running to change this.")

    resp = svc.call_provisioner({"verb": "ai_codex", "idx": ws.idx,
                                 "action": body.action,
                                 # A starting model, if an operator set one.
                                 # Ignored by the provisioner unless the
                                 # customer's config file is absent.
                                 "model": oclib.agent_model(db, "codex")},
                                timeout=1200)
    if not resp.get("ok"):
        log.error("codex %s failed for %s: %s", body.action, ws.incus_project, resp)
        err = resp.get("error") or ""
        if "not signed in" in err:
            fail(409, "ai_host_unlinked",
                 "The platform account is not signed in on this host.")
        fail(500, "ai_failed", "The AI tool could not be set up.",
             output=(resp.get("output") or err)[-300:])

    svc.audit(db, user.id, f"ai_codex_{body.action}", ws.incus_project)
    return {"ok": True, "codex": _codex_state(ws)}


@app.post("/api/workspace/ai/openclaw")
def ai_openclaw(body: HermesAction, user: User = Depends(current_user),
                db: Session = Depends(get_session)) -> dict:
    """Record the customer's intent. The worker reconciles it.

    Enabling does not install here: it is an npm download and a service start,
    minutes of work that must not be held open on an HTTP request. The customer
    sees "preparing" instead - which is also what makes a failed install
    self-healing rather than a dead toggle.
    """
    ws = my_workspace(db, user)
    if not user.username:
        fail(409, "no_username", "This account has no username yet.")
    account = db.get(OpenRouterAccount, user.id)
    if body.action == "enable" and not (account and account.key):
        # It spends the managed OpenRouter key. Without one there is nothing to
        # configure it with, and a gateway that cannot reach a model is a
        # dashboard that only produces errors.
        fail(409, "needs_openrouter",
             "The managed OpenRouter key must be active first.")

    ws.openclaw_enabled = (body.action == "enable")
    if not ws.openclaw_enabled:
        # Cleared here so the interface stops showing a secret the moment the
        # customer switches it off, rather than until the worker catches up.
        ws.openclaw_password = None
    ws.openclaw_error = None
    db.commit()
    svc.audit(db, user.id, f"ai_openclaw_{body.action}", ws.incus_project)
    return {"ok": True, "openclaw": _openclaw_state(ws, db.get(OpenRouterAccount, user.id))}


@app.post("/api/workspace/ai/openclaw/telegram")
def ai_openclaw_telegram(body: HermesAction, user: User = Depends(current_user),
                         db: Session = Depends(get_session)) -> dict:
    """Turn the Telegram channel on or off for this customer's gateway.

    Intent only, like every other OpenClaw switch: the worker reconciles it,
    because putting the token in place means writing a file inside a workspace
    and restarting a service.

    The token is NOT taken from this request. It is the one the customer saved
    against their account, which Hermes already reuses - so a customer sets a
    bot up once and both services can use it, and no token ever travels through
    a second endpoint that would have to be trusted with it.
    """
    ws = my_workspace(db, user)
    if body.action == "enable":
        if not ws.openclaw_installed:
            fail(409, "openclaw_not_ready", "OpenClaw is not running yet.")
        if not (user.telegram_bot_token and user.telegram_user_id):
            fail(409, "telegram_not_configured",
                 "Save a Telegram bot token on the account first.")
        # ONE BOT, ONE AGENT. Telegram allows a single `getUpdates` poller per
        # bot token, so two services sharing one bot do not both work - the
        # second one connects and is then terminated by the first:
        #
        #   Conflict: terminated by other getUpdates request; make sure that
        #   only one bot instance is running.
        #
        # Observed live with Hermes and OpenClaw both enabled on one token. The
        # channel reported "enabled, configured, running, DISCONNECTED", which
        # is the worst kind of broken - it looks switched on. Refusing here is
        # the honest answer; a customer who wants both makes a second bot.
        if ws.hermes_telegram_enabled:
            fail(409, "telegram_bot_in_use",
                 "That Telegram bot is already connected to Hermes.")
    ws.openclaw_telegram_enabled = (body.action == "enable")
    ws.openclaw_error = None
    # A retry starts clean, exactly as the Hermes toggle does - otherwise the
    # last failure keeps showing while the new attempt is still running.
    ws.openclaw_telegram_error = None
    db.commit()
    svc.audit(db, user.id, f"openclaw_telegram_{body.action}", ws.incus_project)
    return {"ok": True, "openclaw": _openclaw_state(
        ws, db.get(OpenRouterAccount, user.id))}


@app.post("/api/workspace/ai/openclaw/devices")
def ai_openclaw_devices(user: User = Depends(current_user),
                        db: Session = Depends(get_session)) -> dict:
    """Approve whatever is waiting to pair with this customer's gateway.

    OpenClaw treats a browser reaching its Control UI as a device, and one it
    does not consider local must be approved before it may talk - even with the
    right password. The usual path avoids this entirely (`trustedProxies` makes
    connections through our proxy count as local); this exists so the answer to
    a pairing prompt is a button rather than "SSH in and run a command".

    Safe to approve in bulk because of who is asking: this endpoint is reached
    only by a customer signed in to their own dashboard, about their own
    workspace, and a request pending there is one they just caused.
    """
    ws = my_workspace(db, user)
    if ws.state != WorkspaceState.ON:
        fail(409, "machine_off", "The machine must be running to change this.")
    if not ws.openclaw_installed or not ws.openclaw_password:
        fail(409, "openclaw_not_ready", "OpenClaw is not running yet.")

    resp = svc.call_provisioner({"verb": "ai_openclaw", "idx": ws.idx,
                                 "action": "approve_devices",
                                 "password": ws.openclaw_password}, timeout=300)
    if not resp.get("ok"):
        log.error("openclaw device approval failed for %s: %s",
                  ws.incus_project, resp)
        fail(500, "openclaw_approve_failed", "The devices could not be approved.")
    svc.audit(db, user.id, "ai_openclaw_devices_approved", ws.incus_project)
    return {"ok": True}


@app.post("/api/workspace/ai/hermes")
def ai_hermes(body: HermesAction, user: User = Depends(current_user),
              db: Session = Depends(get_session)) -> dict:
    """Record the customer's intent. The worker reconciles it.

    Enabling does not mint the key here, because minting requires the
    management key and this process faces the internet. The customer sees
    "preparing" for a few seconds instead - which is also what makes a failed
    mint self-healing rather than a dead toggle.
    """
    ws = my_workspace(db, user)
    if not user.username:
        fail(409, "no_username", "This account has no username yet.")

    ws.hermes_enabled = (body.action == "enable")
    if ws.hermes_enabled:
        if body.telegram_enabled is True:
            token = (body.telegram_token or user.telegram_bot_token or "").strip()
            users = (body.telegram_users or user.telegram_user_id or "").replace(" ", "")
            if not re.fullmatch(r"[0-9]{6,15}:[A-Za-z0-9_-]{20,}", token):
                fail(400, "telegram_bad_token", "The Telegram bot token is invalid.")
            if not re.fullmatch(r"[1-9][0-9]{4,14}(,[1-9][0-9]{4,14})*", users):
                fail(400, "telegram_bad_users", "The Telegram user allowlist is invalid.")
            if ws.openclaw_telegram_enabled:
                fail(409, "telegram_bot_in_use",
                     "That Telegram bot is already connected to OpenClaw.")
            ws.hermes_telegram_enabled = True
            ws.hermes_telegram_token = token
            ws.hermes_telegram_users = users
            ws.hermes_telegram_error = None
            # Inline credentials from older clients become the account defaults
            # so the next Telegram-capable feature can reuse them too.
            if body.telegram_token:
                user.telegram_bot_token = token
            if body.telegram_users and "," not in users:
                user.telegram_user_id = users
        elif body.telegram_enabled is False and ws.hermes_telegram_enabled:
            ws.hermes_telegram_enabled = False
            ws.hermes_telegram_token = None
            ws.hermes_telegram_users = None
            ws.hermes_telegram_error = None
    if not ws.hermes_enabled:
        # Cleared here so the interface stops showing a secret the moment the
        # customer switches it off, rather than until the worker catches up.
        ws.hermes_vhost_ready = False
        ws.hermes_telegram_enabled = False
        ws.hermes_telegram_token = None
        ws.hermes_telegram_users = None
        ws.hermes_telegram_error = None
    ws.hermes_error = None
    db.commit()
    svc.audit(db, user.id, f"ai_hermes_{body.action}", ws.incus_project)
    return {"ok": True, "hermes": _hermes_state(
        ws, db.get(OpenRouterAccount, user.id))}


@app.post("/api/workspace/ai/claude")
def ai_claude(body: AiAction, user: User = Depends(current_user),
              db: Session = Depends(get_session)) -> dict:
    ws = my_workspace(db, user)
    if ws.state != WorkspaceState.ON:
        fail(409, "machine_off", "The machine must be running to change this.")

    # npm install of the CLI is the slow part on a machine that does not have it
    # yet; the sign-in itself is a single small file.
    resp = svc.call_provisioner({"verb": "ai_claude", "idx": ws.idx,
                                 "action": body.action,
                                 # A starting model, if an operator set one.
                                 # Ignored by the provisioner unless the
                                 # customer's config file is absent.
                                 "model": oclib.agent_model(db, "claude")},
                                timeout=1200)
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


# The windows the overview offers. Five minutes is the default: the question a
# customer actually has in front of a running machine is "what is it doing right
# now", not "what did it do overnight".
METRIC_WINDOWS = (5, 15, 60, 360, 1440)


@app.get("/api/workspace/metrics")
def workspace_metrics(minutes: int = 5, user: User = Depends(current_user),
                      db: Session = Depends(get_session)) -> dict:
    """Recent CPU and memory, in ABSOLUTE units for the overview charts.

    Cores and gigabytes, not percentages. A percentage hides the two things
    worth knowing - how big the machine is, and how much of it is spare - and
    makes 90% of 0.5 vCPU look identical to 90% of 3.
    """
    ws = my_workspace(db, user)
    minutes = max(1, min(minutes, max(METRIC_WINDOWS)))
    since = svc.now() - timedelta(minutes=minutes)
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
        cores = max(0.0, delta) / span
        cpu.append({"ts": cur.ts.isoformat(), "value": round(min(cores, ws.cpu_cores), 3)})
        mem.append({"ts": cur.ts.isoformat(),
                    "value": round(cur.mem_bytes / 1073741824, 3)})

    return {"minutes": minutes, "windows": list(METRIC_WINDOWS),
            "cpu": cpu, "memory": mem,
            # The tier, so the chart can scale against what was bought rather
            # than against the tallest bar it happens to have.
            "cpu_cores": ws.cpu_cores,
            "memory_gb": round(ws.mem_mib / 1024, 3),
            "sample_seconds": SAMPLE_SECONDS,
            "samples": len(rows)}


# --- files ---------------------------------------------------------------
# Routed through the root provisioner because a RESTRICTED Incus certificate is
# denied the file API outright (403). Content moves via a spool directory
# rather than through the socket, so a large file is streamed rather than held
# in memory twice.
# Disk, NOT tmpfs. This was /run/mmd/spool - and /run is a 1.6 GiB tmpfs that
# also holds the provisioner's socket and Incus's config. Staging a customer's
# download there meant a large transfer consumed host RAM and could fill the
# filesystem systemd and sshd depend on, taking every tenant down with it. A
# mistake here should cost disk, which is measurable and recoverable.
SPOOL = "/var/lib/mmd/spool"
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


def _refuse_if_too_large(resp: dict) -> None:
    """Turn the provisioner's size refusal into something the page can explain.

    Without this the customer gets "that file could not be downloaded" for a
    folder that is simply too big, which reads as a fault rather than a limit.
    """
    if resp.get("code") == "too_large":
        fail(413, "download_too_large",
             "That folder is larger than the download limit.",
             size=resp.get("size"), limit=resp.get("limit"))


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
        _refuse_if_too_large(resp)
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
        # Carries the measured size and the limit, so the page can say how far
        # over it is rather than only that it was refused.
        _refuse_if_too_large(resp)
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
    out["has_workspace"] = ws is not None
    if ws is not None:
        q = pricing.quote(svc.tier_of(ws), r)
        out["quote"] = q
        out["hours_remaining"] = ((have / MICRO) / q["max_per_hour"]
                                  if q["max_per_hour"] else 0)
        out["days_remaining"] = out["hours_remaining"] / 24
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
            payload = _serializer.loads(raw, max_age=CONFIG.session_hours * 3600)
            if isinstance(payload, dict):
                uid = int(payload["user_id"])
                cookie_version = int(payload.get("version", 0))
            else:
                uid = int(payload)
                cookie_version = 0
        except (BadSignature, KeyError, TypeError, ValueError):
            await sock.close(code=4401); return
        user = db.get(User, uid)
        if (user is None or user.status != UserStatus.APPROVED
                or user.session_version != cookie_version):
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
        out["user_username"] = tk.user.username if tk.user else None
        out["user_phone"] = tk.user.phone if tk.user else None
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
    _notify_admins(db, "admin_ticket_opened", dedupe_key=f"newticket:{tk.id}")
    svc.audit(db, user.id, "ticket_opened", f"#{tk.id}", subject=tk.subject)
    return {"ok": True, "ticket": _ticket_json(tk, staff=False, messages=True)}


@app.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: int, user: User = Depends(current_user),
               db: Session = Depends(get_session)) -> dict:
    tk = _my_ticket(db, user, ticket_id)
    return {"ticket": _ticket_json(tk, staff=False, messages=True)}


@app.post("/api/tickets/{ticket_id}/read")
def read_ticket(ticket_id: int, user: User = Depends(current_user),
                db: Session = Depends(get_session)) -> dict:
    tk = _my_ticket(db, user, ticket_id)
    _mark_read(db, tk, staff=False)
    db.commit()
    return {"ok": True}


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
    return {"ticket": _ticket_json(tk, staff=True, messages=True)}


@app.post("/api/admin/tickets/{ticket_id}/read")
def admin_read_ticket(ticket_id: int, _: User = Depends(require_admin),
                      db: Session = Depends(get_session)) -> dict:
    tk = db.get(Ticket, ticket_id)
    if tk is None:
        fail(404, "no_such_ticket", "No such ticket")
    _mark_read(db, tk, staff=True)
    db.commit()
    return {"ok": True}


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
    notifylib.emit(db, user_id=tk.user_id, kind="support",
                   code="support_reply", severity="info",
                   detail={"ticket_id": tk.id, "subject": tk.subject},
                   href=f"/console/support/{tk.id}",
                   dedupe_key=f"ticket:{tk.id}:reply:{tk.messages[-1].id}")
    owner = db.get(User, tk.user_id)
    if owner is not None:
        try:
            smslib.queue(db, user_id=owner.id, phone=owner.phone,
                         kind="ticket_replied", user=owner,
                         dedupe_key=f"ticketreply:{tk.messages[-1].id}")
            db.commit()
        except smslib.SmsError:
            pass
    svc.audit(db, admin.id, "ticket_replied", f"#{tk.id}")
    return {"ok": True, "ticket": _ticket_json(tk, staff=True, messages=True)}


@app.put("/api/admin/tickets/{ticket_id}/status")
def admin_ticket_status(ticket_id: int, body: TicketStatusChange,
                        admin: User = Depends(require_admin),
                        db: Session = Depends(get_session)) -> dict:
    tk = db.get(Ticket, ticket_id)
    if tk is None:
        fail(404, "no_such_ticket", "No such ticket")
    became_closed = (TicketStatus(body.status) is TicketStatus.CLOSED
                     and tk.status is not TicketStatus.CLOSED)
    tk.status = TicketStatus(body.status)
    tk.updated_at = svc.now()
    db.commit()
    owner = db.get(User, tk.user_id) if became_closed else None
    if owner is not None:
        try:
            smslib.queue(db, user_id=owner.id, phone=owner.phone,
                         kind="ticket_closed", user=owner,
                         dedupe_key=f"ticketclosed:{tk.id}")
            db.commit()
        except smslib.SmsError:
            pass
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
            "id": u.id, "username": u.username,
            "full_name": u.full_name, "phone": u.phone,
            "status": u.status.value,
            "is_admin": u.is_admin,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "credits": (acct.balance_micro / MICRO) if acct else 0.0,
            "workspace": None if w is None else {
                "id": w.id, "state": w.state.value, "cpu_milli": w.cpu_milli,
                "cpu_cores": w.cpu_cores, "memory_mb": w.mem_mib,
                "disk_gb": w.disk_gib,
                # Observed, not promised. `disk_gb` is the allowance; these are
                # what the machine has actually written, sampled on a timer -
                # the number that matters once allowances are overcommitted.
                "disk_used_mib": w.disk_used_mib,
                "disk_percent": (round(w.disk_used_mib / (w.disk_gib * 1024) * 100)
                                 if w.disk_used_mib and w.disk_gib else None),
                "disk_checked_at": (w.disk_checked_at.isoformat()
                                    if w.disk_checked_at else None)},
        })
    return out


@app.put("/api/admin/users/{user_id}/profile")
def admin_update_profile(user_id: int, body: AdminProfileUpdate,
                         admin: User = Depends(require_admin),
                         db: Session = Depends(get_session)) -> dict:
    user = db.get(User, user_id)
    if user is None:
        fail(404, "no_such_user", "No such user")
    _validate_identity(db, user, body.phone)
    phone_changed = body.phone != user.phone
    _apply_identity(user, body.full_name, body.phone)
    if phone_changed:
        user.session_version += 1
    db.commit()
    svc.audit(db, admin.id, "admin_profile_change", user.username, user_id=user.id)
    return {"ok": True}


@app.post("/api/admin/users/{user_id}/approve")
def admin_approve(user_id: int, admin: User = Depends(require_admin),
                  db: Session = Depends(get_session)) -> dict:
    user = db.get(User, user_id)
    if user is None:
        fail(404, "no_such_user", "No such user")
    if user.status == UserStatus.APPROVED:
        # Old approved rows may predate account-scoped OpenRouter. Approval is
        # intentionally idempotent, but it must also repair that invariant.
        if db.get(OpenRouterAccount, user.id) is None:
            db.add(OpenRouterAccount(user_id=user.id, credit_blocked=True,
                                     limit_dirty=True))
            db.commit()
        return {"ok": True, "status": user.status.value}
    user.status = UserStatus.APPROVED
    user.approved_at = svc.now()
    user.approved_by = admin.id
    if db.get(OpenRouterAccount, user.id) is None:
        db.add(OpenRouterAccount(user_id=user.id, credit_blocked=True,
                                 limit_dirty=True))
    # Queued, not sent: this process has no provider key, approval must not
    # depend on an SMS gateway being up, and a failed send deserves a retry.
    # The dedupe key makes the deliberately idempotent approval idempotent
    # here too - a second click does not send a second message.
    try:
        smslib.queue(db, user_id=user.id, phone=user.phone, kind="approved",
                     dedupe_key=f"approved:{user.id}")
    except smslib.SmsError as e:
        # An unsendable number must not block the approval itself. The account
        # is approved either way; the customer simply is not texted.
        log.warning("approval sms not queued for %s: %s", user.username, e)
    db.commit()
    svc.audit(db, admin.id, "approve", user.username)
    return {"ok": True, "status": user.status.value}


@app.post("/api/admin/users/{user_id}/reject")
def admin_reject(user_id: int, admin: User = Depends(require_admin),
                 db: Session = Depends(get_session)) -> dict:
    user = db.get(User, user_id)
    if user is None:
        fail(404, "no_such_user", "No such user")
    user.status = UserStatus.REJECTED
    user.session_version += 1
    try:
        smslib.queue(db, user_id=user.id, phone=user.phone, kind="rejected",
                     dedupe_key=f"rejected:{user.id}")
    except smslib.SmsError as e:
        log.warning("rejection sms not queued for %s: %s", user.username, e)
    db.commit()
    svc.audit(db, admin.id, "reject", user.username)
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
        user.approved_at = svc.now()
        user.approved_by = admin.id
        if db.get(OpenRouterAccount, user.id) is None:
            db.add(OpenRouterAccount(user_id=user.id, credit_blocked=True,
                                     limit_dirty=True))
    db.commit()
    svc.audit(db, admin.id, "set_admin", user.username, is_admin=body.is_admin)
    return {"ok": True, "username": user.username, "is_admin": user.is_admin}


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, admin: User = Depends(require_admin),
                      db: Session = Depends(get_session)) -> dict:
    if user_id == admin.id:
        fail(409, "cannot_delete_self", "You cannot delete your own account")
    user = db.get(User, user_id)
    if user is None:
        fail(404, "no_such_user", "No such user")
    if user.status == UserStatus.DELETING:
        op = db.scalar(select(Operation).where(
            Operation.user_id == user.id,
            Operation.kind == "account_delete",
            Operation.status.in_(oplib.ACTIVE)))
        return {"ok": True, "code": "delete_queued",
                "operation": oplib.view(op) if op else None}
    ws = db.scalar(select(Workspace).where(Workspace.user_id == user_id))
    user.status = UserStatus.DELETING
    user.session_version += 1
    if ws is not None:
        ws.desired_on = False
    db.commit()
    op = oplib.create(db, kind="account_delete", user_id=user.id,
                      workspace_id=ws.id if ws else None, actor_id=admin.id,
                      detail={"user_id": user.id})
    return {"ok": True, "code": "delete_queued", "operation": oplib.view(op)}


@app.get("/api/operations")
def my_operations(user: User = Depends(current_user),
                  db: Session = Depends(get_session)) -> dict:
    rows = db.scalars(select(Operation).where(Operation.user_id == user.id)
                      .order_by(Operation.id.desc()).limit(20)).all()
    return {"operations": [oplib.view(o) for o in rows]}


def _sync_condition_notifications(db: Session, user: User,
                                  session_expires_at: datetime | None = None) -> None:
    """Materialise changing conditions once, without repeating them per poll."""
    now = svc.now()
    balance = svc.balance_micro(db, user.id)
    if balance < 1_000 * MICRO:
        notifylib.emit(db, user_id=user.id, kind="billing", code="low_balance",
                       severity="warning", detail={"balance": balance / MICRO},
                       href="/console/billing", dedupe_key="condition:low_balance")
    else:
        notifylib.resolve(db, user.id, "condition:low_balance", now)

    ws = db.scalar(select(Workspace).where(Workspace.user_id == user.id))
    if ws and ws.auto_stop_at and ws.auto_stop_at <= now + timedelta(hours=1):
        notifylib.emit(db, user_id=user.id, kind="machine",
                       code="auto_stop_soon", severity="warning",
                       detail={"at": ws.auto_stop_at.isoformat()}, href="/console",
                       dedupe_key="condition:auto_stop")
    else:
        notifylib.resolve(db, user.id, "condition:auto_stop", now)
    if ws and ws.purge_after:
        notifylib.emit(db, user_id=user.id, kind="machine",
                       code="archive_deadline", severity="critical",
                       detail={"at": ws.purge_after.isoformat()},
                       href="/console/billing", dedupe_key="condition:archive")
    else:
        notifylib.resolve(db, user.id, "condition:archive", now)
    if session_expires_at and session_expires_at <= now + timedelta(hours=1):
        notifylib.emit(db, user_id=user.id, kind="security",
                       code="session_expiring", severity="warning",
                       detail={"at": session_expires_at.isoformat()},
                       href="/console/account", dedupe_key="condition:session")
    else:
        notifylib.resolve(db, user.id, "condition:session", now)


@app.get("/api/notifications")
def my_notifications(request: Request, user: User = Depends(current_user),
                     db: Session = Depends(get_session)) -> dict:
    _sync_condition_notifications(
        db, user, getattr(request.state, "session_expires_at", None))
    rows = db.scalars(select(Notification).where(
        Notification.user_id == user.id,
        Notification.resolved_at.is_(None))
        .order_by(Notification.created_at.desc()).limit(100)).all()
    return {"notifications": [notifylib.view(row) for row in rows],
            "unread": sum(row.read_at is None for row in rows)}


@app.post("/api/notifications/{notification_id}/read")
def read_notification(notification_id: int, user: User = Depends(current_user),
                      db: Session = Depends(get_session)) -> dict:
    row = db.get(Notification, notification_id)
    if row is None or row.user_id != user.id:
        fail(404, "no_such_notification", "No such notification")
    row.read_at = svc.now()
    db.commit()
    return {"ok": True}


@app.post("/api/notifications/read-all")
def read_all_notifications(user: User = Depends(current_user),
                           db: Session = Depends(get_session)) -> dict:
    rows = db.scalars(select(Notification).where(
        Notification.user_id == user.id, Notification.read_at.is_(None))).all()
    now = svc.now()
    for row in rows:
        row.read_at = now
    db.commit()
    return {"ok": True}


@app.get("/api/admin/operations")
def admin_operations(_: User = Depends(require_admin),
                     db: Session = Depends(get_session)) -> dict:
    rows = db.scalars(select(Operation).order_by(Operation.id.desc()).limit(100)).all()
    return {"operations": [oplib.view(o) for o in rows]}


@app.post("/api/admin/users/{user_id}/credit")
def admin_credit(user_id: int, body: CreditGrant,
                 admin: User = Depends(require_admin),
                 db: Session = Depends(get_session)) -> dict:
    user = db.get(User, user_id)
    if user is None:
        fail(404, "no_such_user", "No such user")
    svc.post_transaction(db, user_id=user.id, workspace_id=None,
                         kind=TxKind.GRANT, amount_micro=round(body.credits * MICRO),
                         detail={"note": body.note, "by": admin.username})
    account = db.get(OpenRouterAccount, user.id)
    if account is None and user.status == UserStatus.APPROVED:
        account = OpenRouterAccount(user_id=user.id, credit_blocked=True,
                                    limit_dirty=True)
        db.add(account)
    if account is not None:
        account.limit_dirty = True
        db.commit()
    balance = svc.balance_micro(db, user.id)
    svc.audit(db, admin.id, "grant_credit", user.username, credits=body.credits)
    return {"ok": True, "balance": balance / MICRO}


@app.post("/api/admin/workspaces/{workspace_id}/power-off")
async def admin_power_off(workspace_id: int, admin: User = Depends(require_admin),
                          db: Session = Depends(get_session)) -> dict:
    """Stop a customer's machine without impersonating their session."""
    ws = db.get(Workspace, workspace_id)
    if ws is None:
        fail(404, "no_such_workspace", "No such workspace")
    if ws.state == WorkspaceState.OFF:
        return {"ok": True, "state": ws.state.value}
    if ws.state != WorkspaceState.ON:
        fail(409, "power_bad_state", "The machine cannot be stopped in its current state.")
    client = _incus()
    try:
        ws.state = WorkspaceState.STOPPING
        db.commit()
        await client.stop(ws.instance, ws.incus_project)
        svc.settle_elapsed(db, ws, powered_on=True)
        ws.state = WorkspaceState.OFF
        ws.desired_on = False
        ws.period_start = None
        svc.disarm_auto_stop(ws)
        db.commit()
    except IncusError as exc:
        log.error("admin stop failed for %s: %s", ws.incus_project, exc)
        ws.state = WorkspaceState.ERROR
        ws.error = str(exc)
        db.commit()
        fail(500, "power_failed", "The machine could not be changed.")
    except Exception as exc:  # noqa: BLE001
        log.exception("unexpected admin stop failure for %s", ws.incus_project)
        ws.state = WorkspaceState.ERROR
        ws.error = f"{type(exc).__name__}: {exc}"
        db.commit()
        fail(500, "power_failed", "The machine could not be changed.")
    finally:
        await client.aclose()
    svc.audit(db, admin.id, "admin_power_off", ws.incus_project,
              owner_id=ws.user_id)
    return {"ok": True, "state": ws.state.value}


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
    allowed = set(pricing.DEFAULT_RATES) | set(CAP_DEFAULTS)
    return {**{k: str(v) for k, v in pricing.DEFAULT_RATES.items()},
            **{k: str(v) for k, v in CAP_DEFAULTS.items()},
            **{k: str(v) for k, v in stored.items() if k in allowed}}


@app.put("/api/admin/settings")
def admin_put_settings(body: dict[str, str], admin: User = Depends(require_admin),
                       db: Session = Depends(get_session)) -> dict:
    from .scheduler.admission import DEFAULTS as CAP_DEFAULTS
    # Every service's discount key, not just Claude's. A settings panel that
    # silently refuses the key it just rendered is worse than one that never
    # offered it.
    allowed = (set(pricing.DEFAULT_RATES) | set(CAP_DEFAULTS)
               | {aipricing.discount_key(sv) for sv in aipricing.SERVICES})
    unknown = sorted(set(body) - allowed)
    if unknown:
        fail(400, "invalid_setting", "This setting does not belong to this panel.")
    for k, v in body.items():
        row = db.get(Setting, k)
        if row is None:
            db.add(Setting(key=k, value=str(v)))
        else:
            row.value = str(v)
    db.commit()
    svc.audit(db, admin.id, "settings_update", None, keys=sorted(body))
    return svc.get_settings(db)


class OpenClawConfig(BaseModel):
    default_model: str = Field(min_length=1, max_length=128)


@app.get("/api/admin/openclaw")
def admin_openclaw_get(_: User = Depends(require_admin),
                       db: Session = Depends(get_session)) -> dict:
    """OpenClaw's own product setting, and how far it has been adopted.

    Only the model. Everything commercial - the exchange rate and spend cap -
    belongs to OpenRouter, whose key OpenClaw spends, and is
    configured there. Duplicating any of it here would create a second place to
    change a number that has one correct value.
    """
    enabled = db.scalar(select(func.count(Workspace.id))
                        .where(Workspace.openclaw_enabled.is_(True))) or 0
    running = db.scalar(select(func.count(Workspace.id))
                        .where(Workspace.openclaw_installed.is_(True))) or 0
    telegram = db.scalar(select(func.count(Workspace.id))
                         .where(Workspace.openclaw_telegram_installed.is_(True))) or 0
    return {"default_model": oclib.default_model(db),
            "model_prefix": oclib.MODEL_PREFIX,
            "fallback_model": oclib.DEFAULT_MODEL,
            "enabled_count": enabled,
            "running_count": running,
            "telegram_count": telegram}


@app.put("/api/admin/openclaw")
def admin_openclaw_put(body: OpenClawConfig, admin: User = Depends(require_admin),
                       db: Session = Depends(get_session)) -> dict:
    """Set the default model every new gateway is configured with.

    Existing gateways are NOT rewritten. Changing a running agent's model out
    from under a customer mid-conversation is not an admin setting, it is an
    incident; they pick it up on their next install or repair.
    """
    model = body.default_model.strip()
    if not model:
        fail(400, "invalid_model", "The model name cannot be blank.")
    value = oclib.set_default_model(db, model)
    db.commit()
    svc.audit(db, admin.id, "admin_openclaw_config", None, default_model=value)
    return admin_openclaw_get(admin, db)


class BackupConfig(BaseModel):
    enabled: bool = False
    interval_minutes: int = Field(default=backuplib.DEFAULT_INTERVAL, ge=1, le=100000)
    chat_id: str = Field(default="", max_length=32)
    # Absent means "keep the stored token". The panel is never sent the token,
    # so a blank field on an unrelated save must not erase it.
    bot_token: str | None = Field(default=None, max_length=128)


@app.get("/api/admin/backup")
def admin_backup_get(_: User = Depends(require_admin),
                     db: Session = Depends(get_session)) -> dict:
    """The backup schedule, and whether it is actually delivering.

    `last_ok_at` and `last_error` are returned together on purpose: "enabled"
    says what was asked for, and only those two say whether any backup has
    arrived. An operator who cannot tell the difference has no backups.
    """
    return backuplib.config(db)


@app.put("/api/admin/backup")
def admin_backup_put(body: BackupConfig, admin: User = Depends(require_admin),
                     db: Session = Depends(get_session)) -> dict:
    """Configure where the database is sent, and how often.

    A dump is every credential and every customer's ledger in one file, so this
    is admin-only, off until switched on, and audited - without the token,
    which is the one thing an audit row must not carry.
    """
    try:
        state = backuplib.save(db, enabled=body.enabled,
                               interval_minutes=body.interval_minutes,
                               chat_id=body.chat_id, bot_token=body.bot_token)
    except backuplib.BackupError as e:
        fail(400, "backup_invalid", str(e))
    db.commit()
    svc.audit(db, admin.id, "admin_backup_config", None,
              enabled=state["enabled"], interval=state["interval_minutes"],
              chat_id=state["chat_id"])
    return state


@app.post("/api/admin/backup/run")
def admin_backup_run(admin: User = Depends(require_admin),
                     db: Session = Depends(get_session)) -> dict:
    """Send one backup now.

    Configuring a backup and finding out days later that the token was wrong is
    the failure this avoids: the admin presses it once and either the file
    arrives in their Telegram or the page says why it did not.
    """
    svc.audit(db, admin.id, "admin_backup_run", None)
    result = backuplib.run_once(db)
    return {**result, "config": backuplib.config(db)}


# Which dashboard each administration tab embeds. Declared here rather than in
# the page files so that "does a tab have a dashboard" is one list, and a tab
# that gains one does not need its own copy of the embed plumbing.
GRAFANA_DASHBOARDS = {
    "": "mmd-fleet",
    "users": "mmd-customers",
    "storage": "mmd-storage",
    "openrouter": "mmd-ai",
    "policy": "mmd-capacity",
    "tickets": "mmd-support",
    "backup": "mmd-delivery",
}


class AgentModel(BaseModel):
    # Empty is meaningful: it means "write nothing", leaving each CLI on its
    # own built-in default.
    default_model: str = Field(default="", max_length=128)


@app.get("/api/admin/agent-model/{service}")
def admin_agent_model_get(service: str, _: User = Depends(require_admin),
                          db: Session = Depends(get_session)) -> dict:
    if service not in ("claude", "codex"):
        fail(404, "no_such_service", "No such service")
    return {"service": service, "default_model": oclib.agent_model(db, service)}


@app.put("/api/admin/agent-model/{service}")
def admin_agent_model_put(service: str, body: AgentModel,
                          admin: User = Depends(require_admin),
                          db: Session = Depends(get_session)) -> dict:
    """The model a newly installed agent starts on.

    Applied at INSTALL only, and only when the customer has no config file of
    their own yet. Both CLIs read their model from a file in the customer's
    home, which they may edit; an admin default is a starting point, not a
    policy, and rewriting it on every repair would undo their choice silently.
    """
    if service not in ("claude", "codex"):
        fail(404, "no_such_service", "No such service")
    value = oclib.set_agent_model(db, service, body.default_model)
    db.commit()
    svc.audit(db, admin.id, f"admin_{service}_model", None, default_model=value)
    return {"service": service, "default_model": value}


@app.get("/api/admin/grafana")
def admin_grafana(_: User = Depends(require_admin),
                  db: Session = Depends(get_session)) -> dict:
    """Where the dashboards are, and the credential that opens them.

    Grafana has its OWN login now. It used to trust anonymous access behind
    the reverse proxy, which quietly merged two different systems' idea of
    "administrator" into one - anything that reached it was already a Viewer.

    The password is returned in full, deliberately: this endpoint is
    administrator-only, and a credential the operator cannot read is one they
    cannot use. It is a service account for a dashboard, not a customer secret.
    """
    def _get(key: str, default: str = "") -> str:
        row = db.scalar(select(Setting).where(Setting.key == key))
        return row.value if row and row.value else default

    return {"base": "/grafana",
            "dashboards": GRAFANA_DASHBOARDS,
            "username": _get("grafana_admin_user", "admin"),
            "password": _get("grafana_admin_password"),
            "configured": bool(_get("grafana_admin_password"))}


@app.get("/api/admin/users/{user_id}")
def admin_user_detail(user_id: int, minutes: int = 10080,
                      _: User = Depends(require_admin),
                      db: Session = Depends(get_session)) -> dict:
    """One customer: their balance over time, what they were charged, and what
    was done to their account.

    The balance series is reconstructed by walking the ledger BACKWARDS from the
    balance held now, rather than by summing forwards from zero. The ledger is
    the record of movements; the account row is the authority on the total. If
    the two ever disagree, walking back means the chart ends at the number the
    customer actually sees on their dashboard, and the discrepancy shows up as a
    wrong starting point rather than as a chart that contradicts the header.
    """
    u = db.get(User, user_id)
    if u is None:
        fail(404, "no_such_user", "No such user.")

    since = svc.now() - timedelta(minutes=max(60, min(minutes, 525600)))
    txs = list(db.scalars(
        select(CreditTransaction)
        .where(CreditTransaction.user_id == user_id,
               CreditTransaction.created_at >= since)
        .order_by(CreditTransaction.created_at)))

    balance = svc.balance_micro(db, user_id)
    running = balance
    points: list[dict] = []
    for tx in reversed(txs):
        points.append({"ts": tx.created_at.isoformat(), "value": running / MICRO})
        running -= tx.amount_micro
    points.append({"ts": since.isoformat(), "value": running / MICRO})
    points.reverse()

    by_kind: dict[str, int] = {}
    for tx in txs:
        k = tx.kind.value if hasattr(tx.kind, "value") else str(tx.kind)
        by_kind[k] = by_kind.get(k, 0) + tx.amount_micro

    audits = list(db.scalars(
        select(AuditLog).where(AuditLog.actor_id == user_id)
        .order_by(AuditLog.ts.desc()).limit(100)))
    ws = u.workspace
    openrouter = db.get(OpenRouterAccount, u.id)

    return {
        "user": {"id": u.id, "username": u.username,
                 "full_name": u.full_name, "phone": u.phone,
                 "status": u.status.value if hasattr(u.status, "value") else str(u.status),
                 "is_admin": u.is_admin, "created_at": u.created_at.isoformat()},
        "balance": balance / MICRO,
        "minutes": minutes,
        "credit": points,
        "by_kind": {k: v / MICRO for k, v in by_kind.items()},
        "transactions": [{"ts": tx.created_at.isoformat(),
                          "kind": tx.kind.value if hasattr(tx.kind, "value") else str(tx.kind),
                          "amount": tx.amount_micro / MICRO,
                          "detail": tx.detail or {}} for tx in reversed(txs)][:200],
        "audits": [{"ts": a.ts.isoformat(), "action": a.action,
                    "target": a.target} for a in audits],
        "workspace": ({"id": ws.id, "idx": ws.idx,
                       "state": ws.state.value if hasattr(ws.state, "value") else str(ws.state),
                       "hermes_enabled": bool(ws.hermes_enabled),
                       "hermes_ready": bool(ws.hermes_installed)} if ws else None),
        "openrouter": {"ready": bool(openrouter and openrouter.key_hash),
                       "credit_blocked": bool(openrouter and openrouter.credit_blocked)},
    }


# --- Hermes configuration -------------------------------------------------
@app.get("/api/admin/hermes")
def admin_hermes_get(_: User = Depends(require_admin),
                     db: Session = Depends(get_session)) -> dict:
    enabled = db.scalar(select(func.count()).select_from(Workspace)
                        .where(Workspace.hermes_enabled.is_(True))) or 0
    ready = db.scalar(select(func.count()).select_from(Workspace)
                      .where(Workspace.hermes_installed.is_(True))) or 0
    return {"default_model": hermes.default_model(db),
            "workspaces_enabled": int(enabled),
            "workspaces_ready": int(ready),
            # Whether the worker can reach OpenRouter at all. The API cannot
            # check directly - it does not hold the management key, by design -
            # so it reports whether the workspace has ever been discovered.
            "configured": bool(hermes._get(db, hermes.SETTING_WORKSPACE_ID)
                               or hermes._get(db, hermes.LEGACY_WORKSPACE_ID))}


class HermesConfig(BaseModel):
    default_model: str | None = Field(default=None, min_length=1, max_length=128)


@app.put("/api/admin/hermes")
def admin_hermes_put(body: HermesConfig, admin: User = Depends(require_admin),
                     db: Session = Depends(get_session)) -> dict:
    if body.default_model is not None:
        model = body.default_model.strip()
        if not model:
            fail(400, "invalid_model", "The model name cannot be blank.")
        hermes._set(db, hermes.SETTING_DEFAULT_MODEL, model)
    db.commit()
    svc.audit(db, admin.id, "admin_hermes_config", None)
    return admin_hermes_get(admin, db)


class OpenRouterConfig(BaseModel):
    usd_to_toman: float | None = Field(default=None, ge=0)
    # The one model every OpenRouter-backed service starts on.
    default_model: str | None = Field(default=None, min_length=1, max_length=128)


@app.get("/api/admin/openrouter")
def admin_openrouter_get(_: User = Depends(require_admin),
                         db: Session = Depends(get_session)) -> dict:
    usd_rate, _ = svc.ai_settings(db, hermes.SERVICE)
    return {"usd_to_toman": usd_rate,
            "discount_percent": 0.0,
            "default_model": hermes.default_model(db),
            "fallback_model": hermes.DEFAULT_MODEL}


@app.put("/api/admin/openrouter")
def admin_openrouter_put(body: OpenRouterConfig,
                         admin: User = Depends(require_admin),
                         db: Session = Depends(get_session)) -> dict:
    if body.usd_to_toman is not None:
        hermes._set(db, "usd_to_toman", str(max(0.0, float(body.usd_to_toman))))
    if body.default_model is not None:
        model = body.default_model.strip()
        if not model:
            fail(400, "invalid_model", "The model name cannot be blank.")
        hermes.set_default_model(db, model)
    db.commit()
    svc.audit(db, admin.id, "admin_openrouter_config", None)
    return admin_openrouter_get(admin, db)


# --- AI pricing -----------------------------------------------------------
@app.get("/api/workspace/ai/usage")
def workspace_ai_usage(service: str = svc.AI_SERVICE,
                       user: User = Depends(current_user),
                       db: Session = Depends(get_session)) -> dict:
    """What this account has spent on ONE supplier's tokens, and on what.

    Read from the marks rather than recomputed, so it survives the customer
    destroying their workspace - which is the whole reason the totals live here
    and not in the machine.

    Filtered by service, which it was not when Claude was the only one. Summing
    every mark would have added Codex tokens to the Claude tab, priced at the
    wrong supplier's rate, the moment a second service started writing marks.
    """
    if service not in aipricing.SERVICES:
        fail(400, "bad_service", "Unknown AI service.")
    ws = my_workspace(db, user)
    rows = db.scalars(select(AiUsageMark).where(
        AiUsageMark.workspace_id == ws.id,
        AiUsageMark.service == service)).all()

    by_model: dict[str, dict] = {}
    for m in rows:
        acc = by_model.setdefault(m.model, {
            "model": m.model, "toman": 0.0,
            **{c: 0 for c in aipricing.CATEGORIES}})
        acc["toman"] += (m.billed_micro or 0) / MICRO
        acc["input"] += m.input_tokens or 0
        acc["cache_write_5m"] += m.cache_write_5m_tokens or 0
        acc["cache_write_1h"] += m.cache_write_1h_tokens or 0
        acc["cache_read"] += m.cache_read_tokens or 0
        acc["output"] += m.output_tokens or 0

    usd_rate, discount = svc.ai_settings(db, service)
    models = sorted(by_model.values(), key=lambda m: -m["toman"])
    return {
        "service": service,
        "models": models,
        "total_toman": round(sum(m["toman"] for m in models), 2),
        "sessions": len({m.session_id for m in rows}),
        "usd_to_toman": usd_rate,
        "discount_percent": discount,
        # So the page can explain the arithmetic rather than showing a number
        # that arrived from nowhere.
        "period_seconds": svc.AI_PERIOD_SECONDS,
    }


@app.get("/api/admin/ai-pricing")
def admin_ai_pricing(service: str = svc.AI_SERVICE,
                     _: User = Depends(require_admin),
                     db: Session = Depends(get_session)) -> dict:
    """The whole chain a customer's AI bill is computed from.

    Prices in USD per million tokens, the exchange rate, and the discount - all
    editable, because Anthropic changes its rates and this host should not need
    a deploy to keep up.
    """
    if service not in aipricing.SERVICES:
        fail(400, "bad_service", "Unknown AI service.")
    prices = svc.ai_prices(db, service)
    usd_rate, discount = svc.ai_settings(db, service)

    rows = db.scalars(select(AiModelPrice)
                      .where(AiModelPrice.service == service)
                      .order_by(AiModelPrice.model)).all()

    # Models seen in real usage that nothing prices. Their tokens are being held
    # uncounted rather than given away, so this needs to be visible - and it is
    # the ONLY thing standing between an unconfigured supplier and a customer
    # using it for free, so it is scoped to the service being configured rather
    # than showing every supplier's models under each.
    seen = {m.model for m in db.scalars(
        select(AiUsageMark).where(AiUsageMark.service == service))}
    unpriced = sorted(m for m in seen if aipricing.resolve(m, prices) is None)

    return {
        "service": service,
        "services": list(aipricing.SERVICES),
        "usd_to_toman": usd_rate,
        "discount_percent": discount,
        # Every service's rate, so the panel can show them apart. One number for
        # both would lose money on the metered one.
        "discounts": {sv: {"key": aipricing.discount_key(sv),
                           "percent": svc.ai_settings(db, sv)[1]}
                      for sv in aipricing.SERVICES},
        "categories": list(aipricing.CATEGORIES),
        "unpriced_models": unpriced,
        "prices": [{"id": r.id, "model": r.model, "input_usd": r.input_usd,
                    "cache_write_5m_usd": r.cache_write_5m_usd,
                    "cache_write_1h_usd": r.cache_write_1h_usd,
                    "cache_read_usd": r.cache_read_usd,
                    "output_usd": r.output_usd} for r in rows],
    }


@app.put("/api/admin/ai-pricing/{price_id}")
def admin_ai_price_update(price_id: int, body: AiPriceRow,
                          admin: User = Depends(require_admin),
                          db: Session = Depends(get_session)) -> dict:
    row = db.get(AiModelPrice, price_id)
    if row is None:
        fail(404, "no_such_price", "No such model price")
    model = body.model.strip()
    if not model:
        fail(400, "invalid_model", "The model name cannot be blank.")
    for f in ("model", "input_usd", "cache_write_5m_usd", "cache_write_1h_usd",
              "cache_read_usd", "output_usd"):
        setattr(row, f, model if f == "model" else getattr(body, f))
    db.commit()
    svc.audit(db, admin.id, "ai_price_update", row.model)
    return {"ok": True}


@app.post("/api/admin/ai-pricing")
def admin_ai_price_add(body: AiPriceRow, admin: User = Depends(require_admin),
                       db: Session = Depends(get_session)) -> dict:
    """Adding a price is what releases tokens that were held uncounted."""
    service = body.service or svc.AI_SERVICE
    if service not in aipricing.SERVICES:
        fail(400, "bad_service", "Unknown AI service.")
    model = body.model.strip()
    if not model:
        fail(400, "invalid_model", "The model name cannot be blank.")
    row = AiModelPrice(service=service, model=model,
                       input_usd=body.input_usd,
                       cache_write_5m_usd=body.cache_write_5m_usd,
                       cache_write_1h_usd=body.cache_write_1h_usd,
                       cache_read_usd=body.cache_read_usd,
                       output_usd=body.output_usd)
    db.add(row)
    try:
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
        fail(409, "price_exists", "A price for that model already exists.")
    svc.audit(db, admin.id, "ai_price_add", model)
    return {"ok": True, "id": row.id}


@app.delete("/api/admin/ai-pricing/{price_id}")
def admin_ai_price_delete(price_id: int, admin: User = Depends(require_admin),
                          db: Session = Depends(get_session)) -> dict:
    row = db.get(AiModelPrice, price_id)
    if row is None:
        fail(404, "no_such_price", "No such model price")
    name = row.model
    db.delete(row)
    db.commit()
    svc.audit(db, admin.id, "ai_price_delete", name)
    return {"ok": True}


@app.get("/api/admin/activity")
def admin_activity(limit: int = 100, offset: int = 0, q: str = "",
                   _: User = Depends(require_admin),
                   db: Session = Depends(get_session)) -> dict:
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    needle = q.strip()[:128]
    base = (select(AuditLog, User.username)
            .outerjoin(User, User.id == AuditLog.actor_id))
    count = (select(func.count(AuditLog.id)).select_from(AuditLog)
             .outerjoin(User, User.id == AuditLog.actor_id))
    if needle:
        match = (AuditLog.action.ilike(f"%{needle}%") |
                 AuditLog.target.ilike(f"%{needle}%") |
                 User.username.ilike(f"%{needle}%"))
        base = base.where(match)
        count = count.where(match)
    total = db.scalar(count) or 0
    rows = db.execute(base.order_by(desc(AuditLog.ts))
                      .limit(limit).offset(offset)).all()
    return {"total": total, "limit": limit, "offset": offset,
            "events": [{"id": e.id, "ts": e.ts.isoformat() if e.ts else None,
                        "actor_id": e.actor_id, "actor": username,
                        "action": e.action, "target": e.target,
                        "detail": e.detail or {}} for e, username in rows]}


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
