"""Outbound SMS through Kavenegar: catalogue, preferences and delivery.

Why an outbox and not a call at the event
-----------------------------------------
The provider key spends real money and can message any number, so it is a
worker-only credential delivered by `LoadCredential`, exactly like the
OpenRouter management key - the internet-facing API process cannot read it.
That alone settles the design: whoever causes a message writes a row, and the
worker sends it. Two other properties fall out of the same choice: approving a
customer no longer depends on an SMS gateway being up or quick, and a failed
send is retried rather than lost.

Length is a cost decision, measured in UTF-16
---------------------------------------------
Persian and emoji are both non-GSM, so every message is UCS-2 and a segment
holds **70 UTF-16 code units** - not 70 Python characters. Those differ: `🔴`
is one Python character and two units, `🖥️` is two characters and three. A
naive `len()` under-counts and silently doubles the bill, so `segments()`
encodes before measuring and a test holds every template to its budget.

One script per line
-------------------
No line mixes Persian letters with Latin letters. It reads badly in an RTL
client, where a bare `mmd-ai.ir` inside a Persian sentence jumps to the wrong
end of the line. That constraint shapes the wording as much as the layout:
product names like OpenRouter are written in Persian rather than dropped into
a Persian sentence in Latin script.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable

import httpx
from sqlalchemy import select

from . import bale as balelib
from .config import CONFIG
from .models import BaleContact, SmsMessage

log = logging.getLogger("mmd.sms")

BASE = "https://api.kavenegar.com/v1"
TIMEOUT = 30.0

# One UCS-2 segment. Persian is never GSM-7, so this is the only limit here.
SEGMENT_CHARS = 70
# Concatenated parts carry a header, leaving 67 units each.
MULTIPART_CHARS = 67

MAX_ATTEMPTS = 5
BACKOFF = (60, 300, 900, 1800)

DEFAULT_CREDIT_STEP_TOMAN = 50_000
MIN_CREDIT_STEP_TOMAN = 1_000
MAX_CREDIT_STEP_TOMAN = 1_000_000_000

PHONE_RE = re.compile(r"^09[0-9]{9}$")

URL = "mmd-ai.ir"


class SmsError(RuntimeError):
    pass


def segments(body: str) -> int:
    """How many SMS this body bills as. Measured in UTF-16 code units."""
    units = len(body.encode("utf-16-le")) // 2
    if units <= SEGMENT_CHARS:
        return 1
    return -(-units // MULTIPART_CHARS)


def units(body: str) -> int:
    return len(body.encode("utf-16-le")) // 2


# --- the catalogue --------------------------------------------------------
@dataclass(frozen=True)
class Template:
    """One kind of message, and who decides whether it is sent.

    `optional` is the whole preference system in one flag. A customer may
    silence anything that is merely useful; they may not silence the messages
    that exist to protect the account, because a takeover would simply turn
    them off first. Auth codes are not silenceable for the same reason - they
    are the mechanism, not a notification about it.
    """
    kind: str
    category: str          # money | machine | support | security | auth | admin
    audience: str          # customer | admin
    optional: bool
    build: Callable[[dict], str]


def _t(kind, category, audience, optional, build):
    return Template(kind, category, audience, optional, build)


# Written so that no line mixes Persian and Latin letters, and so that each
# fits the segment budget the tests enforce.
CATALOGUE: dict[str, Template] = {t.kind: t for t in [
    # --- account lifecycle ---
    _t("approved", "account", "customer", False,
       lambda d: f"✅ حساب شما تأیید شد\nاکنون می‌توانید وارد شوید\n{URL}"),
    _t("rejected", "account", "customer", False,
       lambda d: f"⛔ درخواست عضویت شما تأیید نشد\n{URL}"),

    # --- money ---
    _t("low_credit", "money", "customer", True,
       lambda d: f"⚠️ اعتبار شما رو به پایان است\nلطفاً شارژ کنید\n{URL}"),
    _t("stopped_no_credit", "money", "customer", True,
       lambda d: f"🔴 ماشین شما به دلیل پایان اعتبار خاموش شد\n{URL}"),
    _t("credit_step", "money", "customer", True,
       lambda d: ("📈 اعتبار شما افزایش یافت\n" if d.get("increased") else
                  "📉 اعتبار شما کاهش یافت\n")
                 + f"موجودی جدید: {d.get('balance', '')} تومان"),
    _t("key_blocked", "money", "customer", True,
       lambda d: "🔑 کلید هوش مصنوعی شما غیرفعال شد\nبه دلیل پایان اعتبار"),

    # --- the machine ---
    _t("auto_stopped", "machine", "customer", True,
       lambda d: "⏰ ماشین شما پس از ۱۲ ساعت خاموش شد"),
    _t("pool_stopped", "machine", "customer", True,
       lambda d: "🔴 ماشین شما موقتاً متوقف شد\nبه دلیل کمبود فضای سرور"),
    _t("disk_high", "machine", "customer", True,
       lambda d: f"⚠️ فضای دیسک ماشین شما رو به پایان است\n{URL}"),
    _t("workspace_ready", "machine", "customer", True,
       lambda d: f"✅ ماشین شما آماده است\n{URL}"),
    _t("operation_failed", "machine", "customer", True,
       lambda d: f"❌ عملیات شما ناموفق بود\nلطفاً دوباره تلاش کنید\n{URL}"),

    # --- support ---
    _t("ticket_replied", "support", "customer", True,
       lambda d: f"🎫 به تیکت شما پاسخ داده شد\n{URL}"),
    _t("ticket_closed", "support", "customer", True,
       lambda d: f"🎫 تیکت شما بسته شد\n{URL}"),

    # --- security: never optional ---
    _t("password_changed", "security", "customer", False,
       lambda d: "🔐 گذرواژه حساب شما تغییر کرد\nاگر شما نبودید تماس بگیرید"),
    _t("new_login", "security", "customer", False,
       lambda d: "🔐 ورود تازه‌ای به حساب شما ثبت شد\nاگر شما نبودید گذرواژه را عوض کنید"),

    # --- auth codes: the mechanism itself ---
    _t("signup_code", "auth", "customer", False,
       lambda d: f"کد تأیید شما\n{d.get('code', '')}"),
    _t("verification_code", "auth", "customer", False,
       lambda d: f"کد تأیید شما\n{d.get('code', '')}"),
    # Sent when someone asks to SIGN UP with a number that already has an
    # account. The request is answered identically either way, so an attacker
    # learns nothing; the real owner - the only person who receives this -
    # learns the useful thing, which is that they should sign in instead.
    _t("already_registered", "auth", "customer", False,
       lambda d: f"👤 برای این شماره از قبل حساب دارید\nبا همین شماره وارد شوید\n{URL}"),
    _t("login_code", "auth", "customer", False,
       lambda d: f"کد ورود شما\n{d.get('code', '')}"),
    _t("password_reset_code", "auth", "customer", False,
       lambda d: f"کد بازیابی گذرواژه\n{d.get('code', '')}"),

    # --- to the operator ---
    _t("admin_signup_pending", "admin", "admin", True,
       lambda d: f"👤 درخواست عضویت تازه ثبت شد\n{URL}"),
    _t("admin_ticket_opened", "admin", "admin", True,
       lambda d: f"🎫 تیکت تازه‌ای ثبت شد\n{URL}"),
    _t("admin_backup_failed", "admin", "admin", True,
       lambda d: "📦 پشتیبان‌گیری ناموفق بود"),
    _t("admin_pool_low", "admin", "admin", True,
       lambda d: f"⚠️ فضای دیسک سرور بحرانی است\n{d.get('free', '')} گیگابایت"),
    # The ticket text carries no hours: the threshold is configurable, and a
    # number baked into the sentence would go stale the moment it changed.
    _t("admin_ticket_overdue", "admin", "admin", True,
       lambda d: f"🎫 تیکتی بیش از حد انتظار بی‌پاسخ مانده است\n{URL}"),
    _t("admin_error_rate", "admin", "admin", True,
       lambda d: f"🔴 نرخ خطای سرور بالا رفته است\n{URL}"),
    _t("admin_worker_stalled", "admin", "admin", True,
       lambda d: "🔴 پردازشگر پس‌زمینه متوقف شده است"),
]}

# What a customer may switch off, in the order the settings page shows them.
OPTIONAL_KINDS = [t.kind for t in CATALOGUE.values()
                  if t.optional and t.audience == "customer"]


def body_for(kind: str, detail: dict | None = None) -> str:
    try:
        return CATALOGUE[kind].build(detail or {})
    except KeyError:
        raise SmsError(f"no sms template named {kind!r}") from None


# --- preferences ----------------------------------------------------------
def wants(user, kind: str) -> bool:
    """Whether this customer should receive this kind.

    Absent means yes: preferences default to everything on, so a new template
    reaches existing customers without a migration, and an empty preference
    map is the normal state rather than something to backfill.
    """
    tpl = CATALOGUE.get(kind)
    if tpl is None or not tpl.optional:
        return True
    prefs = getattr(user, "sms_prefs", None) or {}
    return bool(prefs.get(kind, True))


def preferences(user) -> dict:
    prefs = getattr(user, "sms_prefs", None) or {}
    return {kind: bool(prefs.get(kind, True)) for kind in OPTIONAL_KINDS}


# --- queueing -------------------------------------------------------------
def queue(db, *, user_id: int | None, phone: str, kind: str,
          detail: dict | None = None, dedupe_key: str | None = None,
          user=None) -> SmsMessage | None:
    """Record one message to send. Returns None if suppressed or duplicate.

    Callers are endpoints and worker passes; neither holds the provider key.
    Nothing here talks to Kavenegar.
    """
    phone = (phone or "").strip()
    if not PHONE_RE.fullmatch(phone):
        raise SmsError("invalid phone number")
    if user is not None and not wants(user, kind):
        return None
    if dedupe_key:
        existing = db.scalar(select(SmsMessage)
                             .where(SmsMessage.dedupe_key == dedupe_key))
        if existing is not None:
            return None
    row = SmsMessage(user_id=user_id, phone=phone, kind=kind,
                     body=body_for(kind, detail), status="queued",
                     dedupe_key=dedupe_key, next_attempt_at=datetime.now(UTC))
    db.add(row)
    return row


# --- sending --------------------------------------------------------------
def send(phone: str, body: str, *, key: str = "", sender: str = "") -> str:
    """Hand one message to Kavenegar. Returns the provider's message id."""
    key = key or CONFIG.kavenegar_key
    if not key:
        raise SmsError("no kavenegar key configured")
    data = {"receptor": phone, "message": body}
    if sender or CONFIG.sms_sender:
        data["sender"] = sender or CONFIG.sms_sender
    try:
        r = httpx.post(f"{BASE}/{key}/sms/send.json", data=data, timeout=TIMEOUT)
    except httpx.HTTPError as e:
        raise SmsError(f"kavenegar unreachable: {e}") from e
    try:
        payload = r.json()
    except ValueError:
        raise SmsError(f"kavenegar returned HTTP {r.status_code}") from None
    # Kavenegar reports refusal inside a 200 body as often as by status code,
    # so the envelope decides. The key is in the URL, so nothing is logged.
    ret = payload.get("return") or {}
    if int(ret.get("status") or 0) != 200:
        raise SmsError(f"kavenegar refused: {ret.get('status')} "
                       f"{ret.get('message') or ''}".strip())
    entries = payload.get("entries") or []
    return str((entries[0] or {}).get("messageid") or "") if entries else ""


# How far back a parked message is still worth sending once the number links.
# A day is generous for an alert and far too long for a code, which is why the
# codes are excluded outright rather than given a shorter window.
RELINK_WINDOW = timedelta(hours=24)


def requeue_after_linking(db, now: datetime | None = None) -> int:
    """Return still-useful `unlinked` messages to the queue. Count requeued.

    Delivery parks a message when the number has never opened the bot, which
    is permanent until they do - so nothing retried it, and linking afterwards
    did not reconsider it. A customer who linked five minutes after signing up
    never received the alert that was waiting for them.

    Auth codes are deliberately NOT requeued. A verification code is valid for
    three minutes; sending a stale one invites the customer to type something
    that will be refused, which is worse than sending nothing. Those need an
    explicit resend, which the sign-in page already offers.
    """
    now = now or datetime.now(UTC)
    stale = [k for k, t in CATALOGUE.items() if t.category == "auth"]
    rows = list(db.scalars(
        select(SmsMessage)
        .where(SmsMessage.status == "unlinked",
               SmsMessage.created_at >= now - RELINK_WINDOW,
               SmsMessage.kind.notin_(stale))))
    requeued = 0
    for row in rows:
        if chat_for(db, row.phone) is None:
            continue                      # still unreachable; leave it parked
        row.status, row.error, row.next_attempt_at = "queued", None, now
        requeued += 1
    if requeued:
        db.commit()
        log.info("bale: requeued %s message(s) after linking", requeued)
    return requeued


def due(db, now: datetime | None = None, limit: int = 20) -> list[SmsMessage]:
    now = now or datetime.now(UTC)
    return list(db.scalars(
        select(SmsMessage)
        .where(SmsMessage.status == "queued",
               SmsMessage.attempts < MAX_ATTEMPTS,
               (SmsMessage.next_attempt_at.is_(None))
               | (SmsMessage.next_attempt_at <= now))
        .order_by(SmsMessage.created_at)
        .limit(limit)))


def chat_for(db, phone: str) -> int | None:
    """The Bale chat that reaches this number, or None if it has never linked."""
    row = db.scalar(select(BaleContact).where(BaleContact.phone == phone))
    return row.chat_id if row else None


def deliver(db, row: SmsMessage, now: datetime | None = None) -> bool:
    """Send one queued row and record what happened. Never raises.

    Delivery goes through Bale. A number that has never opened the bot has no
    chat to send to, and that is a permanent condition rather than a transient
    one: retrying cannot conjure a chat, and burning the attempt budget on it
    would only bury the real reason under "failed". It is parked as
    `unlinked`, where the operator can see exactly who is unreachable and why.
    """
    now = now or datetime.now(UTC)
    chat_id = chat_for(db, row.phone)
    if chat_id is None:
        row.status = "unlinked"
        row.error = "the number has not opened the bot"
        db.commit()
        log.info("bale %s not sent: %s has no linked chat", row.id, row.phone)
        return False
    row.attempts += 1
    try:
        row.provider_message_id = balelib.send(chat_id, row.body)
    except Exception as e:  # noqa: BLE001
        row.error = str(e)[:255]
        if row.attempts >= MAX_ATTEMPTS:
            row.status = "failed"
            log.warning("bale %s permanently failed: %s", row.id, row.error)
        else:
            delay = BACKOFF[min(row.attempts - 1, len(BACKOFF) - 1)]
            row.next_attempt_at = now + timedelta(seconds=delay)
        db.commit()
        return False
    row.status, row.sent_at, row.error = "sent", now, None
    db.commit()
    log.info("bale %s sent to %s (%s)", row.id, row.phone, row.kind)
    return True
