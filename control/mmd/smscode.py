"""One-time SMS codes, for verifying a phone at signup and for signing in.

A code is a credential for the ninety seconds it lives, so it is treated as
one: hashed at rest, single-use, expiring, attempt-limited, and rate-limited
per phone. The table is read by the internet-facing process, and a dump of
live plaintext codes would be a dump of working logins.

Rate limiting lives in the table, not in memory, because it has to survive a
restart - an in-process counter resets on every deploy, which is precisely
when someone watching would try.

Enumeration: requesting a code answers the same way whether or not the phone
belongs to an account. Otherwise this endpoint becomes a way to ask "is this
person a customer?" one number at a time.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from .config import CONFIG
from .models import SmsCode

CODE_DIGITS = 5
TTL_SECONDS = 180
MAX_ATTEMPTS = 5

# Per phone, per window. Generous enough for a lost message and a retry;
# tight enough that the endpoint cannot be used to bill the operator.
MAX_PER_HOUR = 5
RESEND_SECONDS = 60


class CodeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _hash(phone: str, purpose: str, code: str) -> str:
    """Salted by the session secret, so codes are not comparable across
    installs and a stolen table is not a rainbow table."""
    key = (CONFIG.secret_key or "dev-only-insecure-key").encode()
    return hmac.new(key, f"{purpose}:{phone}:{code}".encode(),
                    hashlib.sha256).hexdigest()


def generate() -> str:
    # Uniform over the full range, leading zeros allowed - `randbelow` rather
    # than a digit loop so every code is equally likely.
    return f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"


def throttle(db, phone: str, purpose: str, now: datetime | None = None) -> None:
    """Raise if this phone has asked too often or too recently."""
    now = now or datetime.now(UTC)
    recent = list(db.scalars(
        select(SmsCode)
        .where(SmsCode.phone == phone, SmsCode.purpose == purpose,
               SmsCode.created_at >= now - timedelta(hours=1))
        .order_by(SmsCode.created_at.desc())))
    if len(recent) >= MAX_PER_HOUR:
        raise CodeError("sms_code_rate", "Too many codes requested.")
    if recent:
        last = recent[0].created_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if (now - last).total_seconds() < RESEND_SECONDS:
            raise CodeError("sms_code_wait", "A code was just sent.")


def issue(db, phone: str, purpose: str, now: datetime | None = None) -> str:
    """Create a code and return the PLAINTEXT for delivery. Never stored."""
    now = now or datetime.now(UTC)
    throttle(db, phone, purpose, now)
    # Any earlier code for this phone stops working the moment a new one is
    # sent, so two live codes never exist at once.
    for row in db.scalars(select(SmsCode).where(
            SmsCode.phone == phone, SmsCode.purpose == purpose,
            SmsCode.consumed_at.is_(None))):
        row.consumed_at = now
    code = generate()
    db.add(SmsCode(phone=phone, purpose=purpose,
                   code_hash=_hash(phone, purpose, code),
                   expires_at=now + timedelta(seconds=TTL_SECONDS)))
    return code


def verify(db, phone: str, purpose: str, code: str,
           now: datetime | None = None) -> None:
    """Consume the code, or raise. Success is silent.

    The row is LOCKED before it is read. Without that, two requests carrying
    the same code both find it unconsumed, both pass every check, and both
    mark it used - so one code admits two sessions. The window is small and
    entirely reachable: a double-submitted form, a retried request, or someone
    replaying a code they watched go past.

    `with_for_update()` makes the second caller wait until the first has
    committed, at which point `consumed_at` is set and it correctly fails.
    """
    now = now or datetime.now(UTC)
    row = db.scalar(
        select(SmsCode)
        .where(SmsCode.phone == phone, SmsCode.purpose == purpose,
               SmsCode.consumed_at.is_(None))
        .order_by(SmsCode.created_at.desc())
        .with_for_update())
    if row is None:
        raise CodeError("sms_code_missing", "Request a code first.")
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires <= now:
        raise CodeError("sms_code_expired", "That code has expired.")
    if row.attempts >= MAX_ATTEMPTS:
        raise CodeError("sms_code_attempts", "Too many wrong attempts.")
    # compare_digest: a timing-comparable check on a five-digit secret is a
    # narrow window, but it is free to close.
    if not hmac.compare_digest(row.code_hash, _hash(phone, purpose, code)):
        row.attempts += 1
        db.commit()
        raise CodeError("sms_code_wrong", "That code is not correct.")
    row.consumed_at = now
    db.commit()
