"""Telling an outside agent that a customer wrote something.

The MCP server already lets an agent WAIT for work (`wait_for_new_ticket`).
This is the other direction: the platform pushes, so an agent that cannot hold
a long poll open - one woken by a webhook, like Hermes - still starts within a
second of the customer pressing send.

The two are deliberately both present. A webhook is faster; a long poll cannot
be missed. If the webhook fails, is misconfigured, or the agent was restarting,
the ticket is still sitting in `list_open_tickets` and the next poll finds it.
Nothing is lost by a delivery that does not arrive, which is what makes it safe
to fire and forget.

Stopping someone else from firing it
------------------------------------
The receiving endpoint is on the internet, so anything can POST to it. Three
things together make a forged call useless:

  * **HMAC-SHA256 over the exact body**, with a shared secret. An attacker
    cannot produce the signature without the secret, so an unsigned or
    wrongly-signed request is rejected before the agent reads a word of it.
  * **A timestamp inside the signed material.** Signing the body alone lets a
    captured request be replayed forever; signing `timestamp.body` and
    refusing anything older than a few minutes gives a replay a short life.
  * **No authority in the payload.** The webhook carries a ticket id and a
    nudge, never an instruction. Even a perfectly forged call can only make
    the agent look at a ticket - which it may do at any time anyway.

That last one matters most. The signature keeps strangers out; the payload
design means getting in buys nothing.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Setting

log = logging.getLogger("mmd.webhook")

SETTING_URL = "ticket_webhook_url"
SETTING_SECRET = "ticket_webhook_secret"

# Short, because this is a nudge and not a conversation. An agent that cannot
# accept a nudge in five seconds is not going to answer a ticket quickly.
TIMEOUT = 5.0

# How old a signed request may be. Long enough to survive clock skew between
# two machines, short enough that a captured request is worthless by the time
# anyone finds it.
MAX_AGE_SECONDS = 300


def _get(db: Session, key: str) -> str:
    row = db.scalar(select(Setting).where(Setting.key == key))
    return (row.value if row and row.value else "").strip()


def _set(db: Session, key: str, value: str) -> None:
    row = db.scalar(select(Setting).where(Setting.key == key))
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
    db.flush()


def config(db: Session) -> dict:
    """What the admin page shows. The secret is never returned in full."""
    secret = _get(db, SETTING_SECRET)
    return {
        "url": _get(db, SETTING_URL),
        "secret_set": bool(secret),
        "secret_hint": secret[-4:] if secret else "",
        "enabled": bool(_get(db, SETTING_URL)),
    }


def save(db: Session, url: str, secret: str | None) -> dict:
    """Store the destination. `secret=None` keeps the stored one.

    An empty URL turns the webhook off, which is a supported state: the agent
    falls back to polling and nothing breaks.
    """
    url = (url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        raise ValueError("url must start with http:// or https://")
    if secret is not None:
        _set(db, SETTING_SECRET, secret.strip())
    _set(db, SETTING_URL, url)
    return config(db)


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """The signature the receiver checks.

    Over `timestamp.body`, not the body alone: without the timestamp in the
    signed material a captured request stays valid forever.
    """
    material = timestamp.encode() + b"." + body
    return hmac.new(secret.encode(), material, hashlib.sha256).hexdigest()


def _signature_headers(secret: str, timestamp: str, body: bytes) -> dict:
    """Every header a receiver might check, in the one format that is safe.

    `X-MMD-*` is our own name for it. `X-Webhook-Signature-V2` is the generic
    convention Hermes implements, and it happens to sign exactly what `sign()`
    already produces - `timestamp.body`, refused beyond a five-minute window -
    so the same digest satisfies both under two names.

    GitHub's `X-Hub-Signature-256` is deliberately NOT sent, though it would
    also be accepted. It signs the body ALONE, and a receiver that checks it
    checks it FIRST: Hermes tries GitHub before V2 and returns on the first
    format it recognises. Sending both would therefore not be belt and braces
    - it would hand the receiver the one signature with no timestamp in it and
    silently give back the replay protection V2 exists to provide.
    """
    digest = sign(secret, timestamp, body)
    return {
        "X-MMD-Signature": digest,
        "X-Webhook-Signature-V2": digest,
        "X-Webhook-Timestamp": timestamp,
    }


def verify(secret: str, timestamp: str, body: bytes, signature: str,
           now: float | None = None) -> bool:
    """The receiver's half. Here so the contract has one definition and the
    tests can prove a forged or stale call is refused."""
    try:
        age = abs((now or time.time()) - float(timestamp))
    except (TypeError, ValueError):
        return False
    if age > MAX_AGE_SECONDS:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), signature or "")


def build(event: str, ticket_id: int | None, username: str | None,
          subject: str | None) -> dict:
    """The payload.

    Carries no instruction and no customer text - only enough for the agent to
    find the ticket through the MCP tools, where its own permission checks
    apply. A forged call therefore achieves nothing an agent could not do on
    its own, and ticket text reaches the model through a path that has already
    labelled it untrusted.
    """
    return {
        "event": event,
        "ticket_id": ticket_id,
        "username": username,
        "subject": subject,
        "hint": ("A customer is waiting. Call list_open_tickets for the "
                 "details and answer, or escalate if you cannot."),
    }


def deliver(db: Session, payload: dict) -> dict:
    """Send one nudge. Never raises: a ticket must not fail to be created
    because an agent is down."""
    url = _get(db, SETTING_URL)
    if not url:
        return {"sent": False, "reason": "no webhook configured"}
    secret = _get(db, SETTING_SECRET)

    body = json.dumps(payload, ensure_ascii=False).encode()
    timestamp = str(int(time.time()))
    headers = {"Content-Type": "application/json",
               "X-MMD-Timestamp": timestamp,
               "X-MMD-Event": str(payload.get("event", ""))}
    if secret:
        headers.update(_signature_headers(secret, timestamp, body))

    try:
        r = httpx.post(url, content=body, headers=headers, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        log.warning("ticket webhook to %s failed: %s", url, exc)
        return {"sent": False, "reason": f"unreachable: {exc}"}
    ok = r.status_code < 400
    if not ok:
        log.warning("ticket webhook returned HTTP %s", r.status_code)
    return {"sent": ok, "status": r.status_code,
            "body": r.text[:200] if r.text else ""}


def test(db: Session, url: str, secret: str | None) -> dict:
    """Try a destination WITHOUT saving it.

    An admin should not have to store a wrong address to discover it is wrong,
    and a webhook that is only exercised by a real customer is one whose first
    test is a real customer waiting.
    """
    url = (url or "").strip()
    if not url:
        return {"sent": False, "reason": "no url given"}
    if not url.startswith(("http://", "https://")):
        return {"sent": False, "reason": "url must start with http:// or https://"}
    effective = secret if secret is not None else _get(db, SETTING_SECRET)

    body = json.dumps(build("test", None, None, None), ensure_ascii=False).encode()
    timestamp = str(int(time.time()))
    headers = {"Content-Type": "application/json",
               "X-MMD-Timestamp": timestamp, "X-MMD-Event": "test"}
    if effective:
        headers.update(_signature_headers(effective, timestamp, body))
    try:
        r = httpx.post(url, content=body, headers=headers, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        return {"sent": False, "reason": f"unreachable: {exc}"}
    return {"sent": r.status_code < 400, "status": r.status_code,
            "body": r.text[:300] if r.text else ""}
