"""Outbound messages through Bale, and the link between a chat and an account.

Bale replaces SMS as the way this platform reaches a customer. The catalogue,
the preferences and the queue in `sms.py` are unchanged and transport-agnostic;
only delivery moved here.

The difference that shapes everything
-------------------------------------
An SMS is addressed to a phone number, so the platform could always reach any
customer unilaterally. A Bale bot is addressed to a **chat id**, and a chat
only exists once the person has opened the bot themselves. There is no way to
message a number that has never written to the bot - measured, not assumed:
`sendMessage` with a phone number as `chat_id` answers
`404 Bad Request: no such group or user`.

So every account needs a linking step, once. The bot asks for the customer's
contact with a `request_contact` button; Bale then sends us the phone number
*and* the chat id together, and the phone number comes from Bale rather than
from whoever typed it. That is stronger evidence than an SMS code: a code
proves someone could read a message, while this proves Bale itself believes
the account owns the number.

What this buys, beyond cost: on the line this platform used, 48% of messages
were accepted, charged, and then filtered by the operator before reaching the
handset - including every verification code measured on the day it was
replaced. Bale delivers or reports an error; there is no silent third outcome.
"""
from __future__ import annotations

import logging

import httpx
from sqlalchemy import select

from .config import CONFIG

log = logging.getLogger("mmd.bale")

BASE = "https://tapi.bale.ai/bot"
TIMEOUT = 15.0

# The button that produces a linked account. Text is Persian because every
# customer-facing string on this platform is.
CONTACT_BUTTON = "📱 ارسال شمارهٔ من"
CONTACT_KEYBOARD = {
    "keyboard": [[{"text": CONTACT_BUTTON, "request_contact": True}]],
    "resize_keyboard": True,
    "one_time_keyboard": True,
}


class BaleError(RuntimeError):
    pass


def configured() -> bool:
    return bool(CONFIG.bale_token)


def api(method: str, token: str = "", **params) -> dict:
    """One Bale API call. Raises BaleError on anything that is not `ok`."""
    token = token or CONFIG.bale_token
    if not token:
        raise BaleError("no bale token configured")
    try:
        r = httpx.post(f"{BASE}{token}/{method}", data=params, timeout=TIMEOUT)
    except httpx.HTTPError as e:
        raise BaleError(f"bale unreachable: {e}") from e
    try:
        payload = r.json()
    except ValueError:
        raise BaleError(f"bale returned HTTP {r.status_code}") from None
    if not payload.get("ok"):
        # The token is in the URL, never in a logged message.
        raise BaleError(f"bale refused: {payload.get('error_code')} "
                        f"{payload.get('description') or ''}".strip())
    return payload.get("result") or {}


def send(chat_id: int | str, text: str, token: str = "") -> str:
    """Deliver one message. Returns Bale's message id."""
    result = api("sendMessage", token=token, chat_id=chat_id, text=text)
    return str(result.get("message_id") or "")


def ask_for_contact(chat_id: int | str, text: str, token: str = "") -> str:
    """Show the share-my-number button.

    Sent as a keyboard rather than asking the customer to type their number:
    a typed number is a claim, while this one is reported by Bale for the
    account actually pressing the button.
    """
    import json
    result = api("sendMessage", token=token, chat_id=chat_id, text=text,
                 reply_markup=json.dumps(CONTACT_KEYBOARD, ensure_ascii=False))
    return str(result.get("message_id") or "")


def updates(offset: int = 0, timeout: int = 0, token: str = "") -> list[dict]:
    """Pending updates. `offset` acknowledges everything below it.

    Polling rather than a webhook: a webhook needs Bale to reach this host,
    which makes delivery depend on an inbound path staying correct, and it
    fails silently when it does not. A poll is one call on a timer that either
    works or shows up in the worker's own logs.
    """
    result = api("getUpdates", token=token, offset=offset, timeout=timeout)
    return list(result) if isinstance(result, list) else []


def normalise_phone(raw: str) -> str:
    """Bale's phone number, in the form the accounts table stores.

    Bale reports what the account holder registered with, which may carry a
    country code and may or may not carry a `+`. All of `+989123456789`,
    `989123456789` and `09123456789` are the same subscriber, and an account
    lookup that missed that would refuse to link a perfectly good number.
    """
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    # `00` is the other way of writing `+`, and a number carrying it would
    # otherwise survive to the length check and be refused as malformed.
    if digits.startswith("00"):
        digits = digits[2:]
    if digits.startswith("98"):
        digits = "0" + digits[2:]
    elif digits.startswith("9") and len(digits) == 10:
        digits = "0" + digits
    return digits


# --- inbound: what the bot receives ---------------------------------------
OFFSET_SETTING = "bale_update_offset"

WELCOME = ("سلام 👋\n"
           "برای دریافت پیام‌های حساب خود، شمارهٔ تلفنتان را با دکمهٔ زیر بفرستید.\n"
           "همان شماره‌ای که با آن ثبت‌نام کرده‌اید یا می‌خواهید ثبت‌نام کنید.")

LINKED = ("✅ شمارهٔ شما ثبت شد\n"
          "از این پس پیام‌های حساب شما همین‌جا فرستاده می‌شود.")

WRONG_CONTACT = ("⛔ فقط می‌توانید شمارهٔ خودتان را بفرستید\n"
                 "لطفاً از دکمهٔ زیر استفاده کنید.")

BAD_NUMBER = ("⛔ این شماره پذیرفته نشد\n"
              "شماره باید یک شمارهٔ همراه ایران باشد.")


def _setting(db, key: str, value: str | None = None) -> str:
    """Read or write one Setting row. Kept local so this module owns its state."""
    from .models import Setting
    row = db.scalar(select(Setting).where(Setting.key == key))
    if value is None:
        return (row.value if row and row.value else "").strip()
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
    return value


def link(db, chat_id: int, contact: dict, sender_id: int | None,
         chat_type: str = "") -> bool:
    """Record that `chat_id` owns the contact's number. Returns True on success.

    Linking decides where this number's verification and recovery codes are
    delivered, so it demands positive proof rather than the absence of a
    contradiction.

    `request_contact` sets `user_id` to the person pressing the button. A
    forwarded contact card carries someone else's id - or none at all, which
    is the case an earlier version of this let through: it rejected only when
    both ids were present AND differed, so a card with no `user_id` linked
    whatever number it named to whoever forwarded it. Anyone could have
    redirected another customer's codes to their own chat.

    The chat must also be private. A bot added to a group reports that group's
    id as the chat, and linking one would deliver every code to everybody in
    it.
    """
    from .models import BaleContact

    owner = contact.get("user_id")
    if owner is None or sender_id is None or int(owner) != int(sender_id):
        send(chat_id, WRONG_CONTACT)
        log.warning("bale: chat %s shared a contact it does not own (%r)",
                    chat_id, owner)
        return False
    if chat_type and chat_type != "private":
        send(chat_id, WRONG_CONTACT)
        log.warning("bale: refused to link in a %s chat", chat_type)
        return False

    phone = normalise_phone(contact.get("phone_number", ""))
    if len(phone) != 11 or not phone.startswith("09"):
        send(chat_id, BAD_NUMBER)
        return False

    name = " ".join(x for x in (contact.get("first_name"),
                                contact.get("last_name")) if x).strip() or None

    # One number, one chat, in both directions: re-linking moves the link
    # rather than leaving a stale row that would send to a chat nobody reads.
    for stale in db.scalars(select(BaleContact).where(
            (BaleContact.phone == phone) | (BaleContact.chat_id == chat_id))):
        db.delete(stale)
    db.flush()
    db.add(BaleContact(phone=phone, chat_id=chat_id, display_name=name))
    db.commit()
    send(chat_id, LINKED)
    log.info("bale: linked %s to chat %s", phone, chat_id)
    return True


def handle(db, update: dict) -> None:
    """One inbound update. Never raises: a bad update must not stop the poll."""
    try:
        msg = update.get("message") or update.get("edited_message") or {}
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if not chat_id:
            return
        sender_id = (msg.get("from") or {}).get("id")
        contact = msg.get("contact")
        if contact:
            link(db, chat_id, contact, sender_id, str(chat.get("type") or ""))
            return
        # Anything else - /start, a greeting, a stray message - gets the same
        # answer, because there is only one thing to do here and a customer
        # who typed something unexpected still needs the button.
        ask_for_contact(chat_id, WELCOME)
    except Exception as e:                                   # noqa: BLE001
        log.warning("bale update failed: %s", e)


def poll(db) -> int:
    """Fetch and handle pending updates. Returns how many were processed.

    The offset is committed only after the batch is handled, so a crash
    mid-batch replays rather than silently dropping somebody's link.
    """
    if not configured():
        return 0
    offset = int(_setting(db, OFFSET_SETTING) or 0)
    try:
        batch = updates(offset=offset)
    except BaleError as e:
        log.warning("bale poll failed: %s", e)
        return 0
    for update in batch:
        handle(db, update)
        offset = max(offset, int(update.get("update_id", 0)) + 1)
    if batch:
        _setting(db, OFFSET_SETTING, str(offset))
        db.commit()
    return len(batch)
