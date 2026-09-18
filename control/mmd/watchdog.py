"""Notices when the platform's foundations have failed, and tells the operator.

Runs as its OWN unit on a timer. That is the entire point: a check living
inside the worker cannot report the worker being dead, and a heartbeat written
by anything other than the loop it proves would keep ticking through the stall
it exists to detect.

Two things are asked about, independently, because they fail independently:

  the worker - a heartbeat older than its threshold means the loop that bills,
    reconciles and sends everything has stopped.

  the pool - the ZFS pool every machine lives on. Incus exports it when it
    stops and cannot re-import a file vdev on the way back up, so a nightly
    `apt-daily-upgrade` restart took storage down for the whole platform,
    twice. A systemd timer now re-imports it; this is what speaks up when that
    does not work. Both outages were discovered by a customer opening a
    ticket, hours in, which is the gap being closed here.

Deliberately tiny. It reads one timestamp, asks the host one question, and
sends the messages the worker would normally send - which it cannot, because
it is the thing that stopped. So the watchdog sends them directly, and is the
one place outside the worker that holds the provider key.

It reports and never repairs: it runs unprivileged and could not import a pool
if it wanted to. Fixing is the timer's job, and a reporter that also repairs
has no one left to report to it.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from . import sms as smslib
from .config import CONFIG
from .db import SessionLocal
from .models import Setting, User, UserStatus

log = logging.getLogger("mmd.watchdog")

# Not a ZFS health word. ZFS has none for "no such pool", and this is the
# state the platform was actually found in on both outages.
POOL_MISSING = "MISSING"


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


def pool_state(name: str = "") -> str | None:
    """What the host says about the storage pool. None when it cannot say.

    Asked with `zpool list` rather than by looking for a mountpoint: an
    exported pool takes its datasets' mounts with it, but so does a pool whose
    last workspace was deleted, and only one of those is an outage.

    None means the question does not apply here - no `zpool` on this host,
    which is every developer machine and every test run. Silence is the right
    answer there; the alternative is an alarm on every laptop.

    A timeout is deliberately NOT unknown. `zpool list` hangs when the ZFS
    layer is wedged on failing I/O, and a wedged pool is exactly the outage
    this exists to find.
    """
    name = name or CONFIG.zpool_name
    try:
        proc = subprocess.run(["zpool", "list", "-H", "-o", "health", name],
                              capture_output=True, text=True, timeout=15)
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        log.error("zpool list timed out; treating the pool as unusable")
        return "TIMEOUT"
    except OSError as e:
        log.error("could not ask about the pool: %s", e)
        return None
    if proc.returncode != 0:
        # Almost always "no such pool available". If it is ever something else
        # - a tightened /dev/zfs, say - the alert is still the truthful one:
        # the control plane cannot see its storage either way, and the reason
        # is in the journal on the next line.
        log.error("zpool list failed: %s", proc.stderr.strip() or "no output")
        return POOL_MISSING
    return proc.stdout.strip().upper() or POOL_MISSING


def alert(db, kind: str, dedupe: str, now: datetime,
          detail: dict | None = None) -> None:
    """Send one operator alert from this process, immediately.

    Sent rather than queued. The worker drains the outbox, and for a stall it
    is the thing that has stopped - so the alert that matters most would be
    the one that never left. Sending both the same way means the path needed
    under the worst failure is the path exercised every time, rather than one
    that is only ever tried during an outage.

    Deduped by the hour, so a fault lasting all night is a message an hour and
    not one every five minutes.
    """
    for admin in db.scalars(select(User).where(
            User.is_admin.is_(True), User.status == UserStatus.APPROVED)):
        if not smslib.wants(admin, kind):
            continue
        key = f"{dedupe}:{now:%Y%m%d%H}:{admin.id}"
        if db.scalar(select(smslib.SmsMessage).where(
                smslib.SmsMessage.dedupe_key == key)):
            continue
        row = smslib.SmsMessage(
            user_id=admin.id, phone=admin.phone, kind=kind,
            body=smslib.body_for(kind, detail),
            status="queued", dedupe_key=key, next_attempt_at=now)
        db.add(row)
        db.commit()
        smslib.deliver(db, row, now)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # This process sends through the same Bale transport as the worker, and
    # httpx logs every request URL at INFO - with the bot token in the path.
    # Silenced in mmd-api and mmd-worker when that was found; this one was
    # missed, and had been writing a live credential to the journal since.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    now = datetime.now(UTC)
    problems = 0
    with SessionLocal() as db:
        if not check(db, now):
            log.error("worker last ticked at %s", last_heartbeat(db))
            alert(db, "admin_worker_stalled", "stall", now)
            problems += 1

        # Asked even when the worker is healthy, because the pool fails
        # independently of it: through both outages the loop kept ticking and
        # billing quite happily while every machine was unreachable.
        state = pool_state()
        if state is not None and state != "ONLINE":
            log.error("storage pool %s is %s", CONFIG.zpool_name, state)
            alert(db, "admin_pool_down", "pooldown", now, {"state": state})
            problems += 1
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
