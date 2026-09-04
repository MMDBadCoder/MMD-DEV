"""Disk is overcommitted on purpose, and this is what makes that safe.

Reservations are gone: ten workspaces were holding 60 GiB of a 67.5 GiB pool
while writing 9.4 GiB between them, and no eleventh customer could be created.
Dropping them reclaimed 44.8 GiB and let the allowances sum to more than the
pool holds - which is the normal trade, and is only defensible with two things
in place:

  * `use_refquota` stays TRUE, so no single workspace can overrun its own
    allowance. A customer who fills up gets write errors inside their own
    machine and nobody else notices.
  * a guard that watches the POOL, because the caps summing past the pool is
    exactly the case a per-workspace cap cannot see.

A full ZFS pool does not fail politely for the tenant who caused it: every
workspace loses writes at once, and on this host PostgreSQL is on the same disk.
"""
import asyncio
import os

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

import pytest                                           # noqa: E402
from sqlalchemy import create_engine, select            # noqa: E402
from sqlalchemy.orm import sessionmaker                  # noqa: E402
from sqlalchemy.pool import StaticPool                   # noqa: E402

from mmd import worker                                   # noqa: E402
from mmd.config import CONFIG                            # noqa: E402
from mmd.models import (Base, Notification, User,        # noqa: E402
                        UserStatus, Workspace, WorkspaceState)

GIB = 1024 ** 3


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    for i in (1, 2, 3):
        u = User(password_hash="x",
                 status=UserStatus.APPROVED, username=f"u{i}")
        db.add(u)
        db.commit()
        db.add(Workspace(user_id=u.id, idx=i, incus_project=f"ws-{i}",
                         state=WorkspaceState.ON, root_gib=6, docker_gib=4))
    db.commit()
    monkeypatch.setattr(worker, "SessionLocal", lambda: db)
    monkeypatch.setattr(db, "close", lambda: None)

    stopped = []

    class FakeIncus:
        async def stop(self, instance, project):
            stopped.append(project)

        async def aclose(self):
            pass

    monkeypatch.setattr(worker, "_incus", lambda: FakeIncus())
    monkeypatch.setattr(worker.svc, "settle_elapsed", lambda *a, **k: 0)
    monkeypatch.setattr(worker.svc, "audit", lambda *a, **k: None)
    yield db, stopped


def _report(free_gib, usage):
    """usage: {idx: root_bytes}. Docker is zero unless a test says otherwise."""
    return {"ok": True,
            "pool": {"available": int(free_gib * GIB), "used": 0},
            "workspaces": {str(i): {"root_used": b, "docker_used": 0,
                                    "docker_size": 4 * GIB}
                           for i, b in usage.items()}}


def _provisioner(monkeypatch, report):
    monkeypatch.setattr(worker.svc, "call_provisioner",
                        lambda payload, timeout=None: report)


# --- measurement ----------------------------------------------------------
def test_usage_is_recorded_per_workspace(env, monkeypatch):
    db, _stopped = env
    _provisioner(monkeypatch, _report(50, {1: 2 * GIB, 2: 1 * GIB, 3: 0}))
    asyncio.run(worker.disk_once())
    used = {w.idx: w.disk_used_mib for w in db.scalars(select(Workspace))}
    assert used[1] == 2048 and used[2] == 1024 and used[3] == 0
    assert all(w.disk_checked_at for w in db.scalars(select(Workspace)))


def test_a_full_workspace_warns_its_owner(env, monkeypatch):
    db, _stopped = env
    # 9 GiB of a 10 GiB allowance is 90%, past the 85% warning line.
    _provisioner(monkeypatch, _report(50, {1: 9 * GIB, 2: 0, 3: 0}))
    asyncio.run(worker.disk_once())
    notes = db.scalars(select(Notification)).all()
    assert [n.code for n in notes] == ["disk_nearly_full"]
    assert notes[0].detail["percent"] == 90


def test_the_warning_clears_when_space_is_freed(env, monkeypatch):
    db, _stopped = env
    _provisioner(monkeypatch, _report(50, {1: 9 * GIB, 2: 0, 3: 0}))
    asyncio.run(worker.disk_once())
    _provisioner(monkeypatch, _report(50, {1: 1 * GIB, 2: 0, 3: 0}))
    asyncio.run(worker.disk_once())
    note = db.scalars(select(Notification)).all()[0]
    assert note.resolved_at is not None, "the warning outlived the condition"


# --- the pool guard -------------------------------------------------------
def test_a_healthy_pool_stops_nothing(env, monkeypatch):
    db, stopped = env
    _provisioner(monkeypatch, _report(CONFIG.pool_floor_gib + 5,
                                      {1: 5 * GIB, 2: 5 * GIB, 3: 5 * GIB}))
    asyncio.run(worker.disk_once())
    assert stopped == []


def test_a_low_pool_stops_the_largest_consumer_first(env, monkeypatch):
    """Ordered by consumption, not by who grew last: the point is to free the
    most space with the fewest machines stopped, and "most recently grown"
    would punish activity rather than size."""
    db, stopped = env
    _provisioner(monkeypatch, _report(1, {1: 1 * GIB, 2: 5 * GIB, 3: 3 * GIB}))
    asyncio.run(worker.disk_once())
    assert stopped, "the pool guard did not fire below the floor"
    assert stopped[0] == "ws-2", f"stopped {stopped[0]} before the biggest"


def test_a_stopped_workspace_is_told_why(env, monkeypatch):
    db, _stopped = env
    _provisioner(monkeypatch, _report(1, {1: 5 * GIB, 2: 0, 3: 0}))
    asyncio.run(worker.disk_once())
    codes = {n.code for n in db.scalars(select(Notification))}
    assert "stopped_pool_full" in codes


def test_the_guard_leaves_stopped_machines_alone(env, monkeypatch):
    """It can only stop what is running. A machine already off is not writing."""
    db, stopped = env
    for w in db.scalars(select(Workspace)):
        w.state = WorkspaceState.OFF
    db.commit()
    _provisioner(monkeypatch, _report(0.5, {1: 5 * GIB, 2: 5 * GIB, 3: 5 * GIB}))
    asyncio.run(worker.disk_once())
    assert stopped == []


def test_a_failed_scan_changes_nothing(env, monkeypatch):
    db, stopped = env
    _provisioner(monkeypatch, {"ok": False, "error": "zfs list failed"})
    asyncio.run(worker.disk_once())
    assert stopped == []
    assert all(w.disk_used_mib is None for w in db.scalars(select(Workspace)))


# --- the settings the two halves share ------------------------------------
def test_the_pool_floor_is_above_zero():
    """A floor of zero is not a guard - by the time the pool is empty, ZFS has
    already begun failing writes for every tenant."""
    assert CONFIG.pool_floor_gib > 0
    assert 0 < CONFIG.disk_warn_percent < 100


def test_the_storage_pool_still_caps_each_workspace():
    """Thin provisioning is only safe because the per-workspace cap remains.
    `use_refquota` going false would let one tenant fill the pool alone."""
    from pathlib import Path
    cfg = (Path(__file__).resolve().parents[1] / "host" / "20-storage.sh").read_text()
    assert 'volume.zfs.use_refquota: "true"' in cfg
    assert 'volume.zfs.reserve_space: "false"' in cfg
