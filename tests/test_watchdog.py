"""What the watchdog must notice, and what it must stay quiet about.

It exists because the two worst failures this platform has are both silent.
A stalled worker keeps every page loading while nothing is billed, reconciled
or sent. A missing storage pool keeps the panel up, the worker ticking and the
database answering while not one machine can start - which is exactly how it
went undetected twice, until a customer wrote in hours later.

So the properties worth pinning are about noticing and not crying wolf, not
about wording.
"""
import os
import subprocess
from datetime import UTC, datetime, timedelta

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

import pytest                                      # noqa: E402
from sqlalchemy import create_engine, select       # noqa: E402
from sqlalchemy.orm import Session                 # noqa: E402

from mmd import sms as smslib                      # noqa: E402
from mmd import watchdog                           # noqa: E402
from mmd.models import (BaleContact, Base, Setting, SmsMessage, User,  # noqa: E402
                        UserStatus)
from mmd.security import hash_password             # noqa: E402


def database():
    db = Session(create_engine("sqlite://"), expire_on_commit=False)
    Base.metadata.create_all(db.bind)
    admin = User(username="admin-user", phone="09120000000",
                 password_hash=hash_password("x"), is_admin=True,
                 status=UserStatus.APPROVED)
    db.add(admin); db.commit()
    # Linked, or every send would park as `unlinked` and the tests below would
    # pass while the operator heard nothing.
    db.add(BaleContact(phone=admin.phone, chat_id=1000000001)); db.commit()
    return db, admin


class _Keep:
    """`with SessionLocal() as db` without closing the test's session."""

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *_exc):
        return False


@pytest.fixture
def sent(monkeypatch):
    """Everything that actually reached Bale during a test."""
    out = []
    monkeypatch.setattr(smslib.balelib, "send",
                        lambda chat_id, body, *_a, **_k: out.append(body) or "1")
    return out


def run(monkeypatch, db, *, health=None, rc=0, stderr="", missing_binary=False,
        hangs=False):
    """Run the whole watchdog against a host that answers `health`."""
    def fake(cmd, *_a, **_kw):
        assert cmd[0] == "zpool"
        if missing_binary:
            raise FileNotFoundError(2, "No such file or directory", "zpool")
        if hangs:
            raise subprocess.TimeoutExpired(cmd, 15)
        return subprocess.CompletedProcess(cmd, rc, f"{health}\n", stderr)

    monkeypatch.setattr(watchdog.subprocess, "run", fake)
    monkeypatch.setattr(watchdog, "SessionLocal", lambda: _Keep(db))
    return watchdog.main()


def beat(db, age_minutes: float):
    db.add(Setting(key="worker_heartbeat",
                   value=(datetime.now(UTC)
                          - timedelta(minutes=age_minutes)).isoformat()))
    db.commit()


# --- the pool -------------------------------------------------------------
def test_a_healthy_pool_says_nothing(monkeypatch, sent):
    db, _ = database()
    beat(db, 0)
    assert run(monkeypatch, db, health="ONLINE") == 0
    assert sent == []


def test_a_pool_that_is_not_imported_is_reported(monkeypatch, sent):
    """The actual outage: `zpool list` exits non-zero with "no such pool".
    Storage is gone, the panel is up, and nothing else notices."""
    db, _ = database()
    beat(db, 0)
    assert run(monkeypatch, db, rc=1, stderr="cannot open 'mmdpool'") == 1
    assert len(sent) == 1 and "MISSING" in sent[0]


def test_an_unhealthy_pool_is_reported_too(monkeypatch, sent):
    """A single file vdev cannot be DEGRADED in the redundancy sense, but it
    can be SUSPENDED on failing I/O - which is the same outage for a customer,
    so anything that is not ONLINE counts."""
    db, _ = database()
    beat(db, 0)
    assert run(monkeypatch, db, health="SUSPENDED") == 1
    assert len(sent) == 1 and "SUSPENDED" in sent[0]


def test_a_hung_zpool_counts_as_down(monkeypatch, sent):
    """`zpool list` hangs when the ZFS layer is wedged. Treating that as
    "cannot tell" would stay silent through the very failure it looks for."""
    db, _ = database()
    beat(db, 0)
    assert run(monkeypatch, db, hangs=True) == 1
    assert len(sent) == 1


def test_a_host_without_zfs_is_not_an_alarm(monkeypatch, sent):
    """Every developer machine and every CI run. An alert here would train the
    operator to ignore the one that matters."""
    db, _ = database()
    beat(db, 0)
    assert run(monkeypatch, db, missing_binary=True) == 0
    assert sent == []


def test_the_operator_is_told_which_failure_it_is(monkeypatch, sent):
    """MISSING is fixed by an import; SUSPENDED is a disk going bad. The same
    message for both would cost the operator the first ten minutes."""
    db, _ = database()
    beat(db, 0)
    run(monkeypatch, db, rc=1)
    db.query(SmsMessage).delete(); db.commit()
    run(monkeypatch, db, health="FAULTED")
    assert sent[0] != sent[1]


# --- the worker, and the two together -------------------------------------
def test_a_stalled_worker_still_alerts(monkeypatch, sent):
    db, _ = database()
    beat(db, watchdog.CONFIG.worker_stall_minutes + 1)
    assert run(monkeypatch, db, health="ONLINE") == 1
    assert len(sent) == 1


def test_the_pool_is_checked_even_when_the_worker_is_healthy(monkeypatch, sent):
    """The check used to return the moment the worker looked alive. Through
    both outages the worker WAS alive - ticking and billing quite happily -
    while no machine on the host could start."""
    db, _ = database()
    beat(db, 0)
    assert watchdog.check(db) is True
    assert run(monkeypatch, db, rc=1) == 1
    assert len(sent) == 1


def test_both_failures_are_reported_separately(monkeypatch, sent):
    """They have different first moves, so one merged alarm would hide one."""
    db, _ = database()
    beat(db, watchdog.CONFIG.worker_stall_minutes + 1)
    assert run(monkeypatch, db, rc=1) == 1
    assert len(sent) == 2
    kinds = {row.kind for row in db.scalars(select(SmsMessage))}
    assert kinds == {"admin_worker_stalled", "admin_pool_down"}


def test_a_missing_heartbeat_is_not_an_alarm(monkeypatch, sent):
    """A fresh install, before the worker's first tick."""
    db, _ = database()
    assert run(monkeypatch, db, health="ONLINE") == 0
    assert sent == []


# --- how often, and by what route -----------------------------------------
def test_an_outage_alerts_once_an_hour_not_every_five_minutes(monkeypatch, sent):
    """The timer fires every five minutes and an outage lasts hours."""
    db, _ = database()
    beat(db, 0)
    for _ in range(5):
        assert run(monkeypatch, db, rc=1) == 1
    assert len(sent) == 1

    # ...but it must come back, or a night-long outage is announced once and
    # then never mentioned again.
    row = db.scalar(select(SmsMessage))
    row.dedupe_key = row.dedupe_key.replace(f"{datetime.now(UTC):%Y%m%d%H}",
                                            "1999010100")
    db.commit()
    assert run(monkeypatch, db, rc=1) == 1
    assert len(sent) == 2


def test_the_alert_is_sent_not_left_in_the_outbox(monkeypatch, sent):
    """The worker drains the outbox, and the worker is one of the two things
    this reports on. Queueing would file the alarm with the process that is
    on fire."""
    db, _ = database()
    beat(db, watchdog.CONFIG.worker_stall_minutes + 1)
    run(monkeypatch, db, rc=1)
    assert [row.status for row in db.scalars(select(SmsMessage))] == ["sent"] * 2


def test_an_operator_who_switched_the_alert_off_is_respected(monkeypatch, sent):
    db, admin = database()
    beat(db, 0)
    admin.sms_prefs = {"admin_pool_down": False}
    db.commit()
    assert run(monkeypatch, db, rc=1) == 1        # still a non-zero exit
    assert sent == []                             # but no message


# --- the plumbing the above depends on ------------------------------------
def test_the_pool_it_asks_about_is_the_one_incus_uses(monkeypatch):
    """Both read MMD_ZPOOL, so a renamed pool cannot leave the watchdog
    guarding a pool that no longer exists."""
    seen = {}

    def fake(cmd, *_a, **_kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "ONLINE\n", "")

    monkeypatch.setattr(watchdog.subprocess, "run", fake)
    assert watchdog.pool_state() == "ONLINE"
    assert seen["cmd"][-1] == watchdog.CONFIG.zpool_name

    from pathlib import Path
    prov = (Path(__file__).resolve().parents[1]
            / "control" / "provisioner" / "provisioner.py").read_text()
    assert 'os.environ.get("MMD_ZPOOL"' in prov
