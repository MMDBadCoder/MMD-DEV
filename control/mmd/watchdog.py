"""Notices when the worker has stopped, and texts the operator.

Runs as its OWN unit on a timer. That is the entire point: a check living
inside the worker cannot report the worker being dead, and a heartbeat written
by anything other than the loop it proves would keep ticking through the stall
it exists to detect.

Deliberately tiny. It reads one setting, compares one timestamp, and queues a
message that the worker would normally send - which it cannot, because it is
stopped. So the watchdog sends it directly, and is the one place outside the
worker that holds the provider key.
"""
from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from . import sms as smslib
from .config import CONFIG
from .db import SessionLocal
from .models import Setting, User, UserStatus

log = logging.getLogger("mmd.watchdog")


def last_heartbeat(db) -> datetime | None:
    row = db.scalar(select(Setting).where(Setting.key == "worker_heartbeat"))
    if row is None or not row.value:
        return None
    try:
        when = datetime.fromisoformat(row.value)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def check(db, now: datetime | None = None) -> bool:
    """True when the worker looks alive."""
    now = now or datetime.now(UTC)
    beat = last_heartbeat(db)
    if beat is None:
        # Never seen one. Nothing to compare against, and alerting here would
        # fire once on every fresh install before the worker's first tick.
        return True
    return now - beat <= timedelta(minutes=CONFIG.worker_stall_minutes)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    now = datetime.now(UTC)
    with SessionLocal() as db:
        if check(db, now):
            return 0
        beat = last_heartbeat(db)
        log.error("worker last ticked at %s", beat)
        # Sent here and now, not queued: the worker drains the outbox, and it
        # is the thing that has stopped.
        for admin in db.scalars(select(User).where(
                User.is_admin.is_(True), User.status == UserStatus.APPROVED)):
            if not smslib.wants(admin, "admin_worker_stalled"):
                continue
            key = f"stall:{now:%Y%m%d%H}:{admin.id}"
            if db.scalar(select(smslib.SmsMessage).where(
                    smslib.SmsMessage.dedupe_key == key)):
                continue
            row = smslib.SmsMessage(
                user_id=admin.id, phone=admin.phone,
                kind="admin_worker_stalled",
                body=smslib.body_for("admin_worker_stalled"),
                status="queued", dedupe_key=key, next_attempt_at=now)
            db.add(row)
            db.commit()
            smslib.deliver(db, row, now)
    return 1


if __name__ == "__main__":
    sys.exit(main())
