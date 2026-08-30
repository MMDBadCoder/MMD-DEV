"""Usage charts: units, windows, and the cadence that feeds them.

The charts used to be drawn in PERCENT over a fixed six-hour window. Percent
hides the two things worth knowing - how big the machine is and how much is
spare - and "90%" reads identically on half a core and on three. They are now
absolute (cores, gigabytes) over a window the customer picks, defaulting to the
last five minutes.
"""
import os
from datetime import timedelta

import pytest

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

from fastapi.testclient import TestClient          # noqa: E402
from sqlalchemy import create_engine               # noqa: E402
from sqlalchemy.orm import sessionmaker            # noqa: E402
from sqlalchemy.pool import StaticPool             # noqa: E402

from mmd import app as appmod                      # noqa: E402
from mmd import service as svc                     # noqa: E402
from mmd.models import (Base, UsageSample, User, UserStatus,  # noqa: E402
                        Workspace, WorkspaceState)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(svc, "call_provisioner", lambda p, timeout=None: {"ok": True})
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    db = Local()
    u = User(email="dev@example.com", password_hash="x", status=UserStatus.APPROVED,
             is_admin=True)
    db.add(u)
    db.commit()
    # 2 cores / 4 GiB, so a percentage and an absolute value cannot coincide.
    ws = Workspace(user_id=u.id, idx=7, incus_project="ws-7", state=WorkspaceState.ON,
                   cpu_milli=2000, mem_mib=4096)
    db.add(ws)
    db.commit()

    # Six samples, 20s apart. CPU counter advances 10s per 20s wall = 0.5 cores.
    now = svc.now()
    for i in range(6):
        db.add(UsageSample(workspace_id=ws.id, ts=now - timedelta(seconds=20 * (5 - i)),
                           cpu_seconds_total=100.0 + 10.0 * i,
                           mem_bytes=1073741824))       # exactly 1 GiB
    db.commit()

    api = appmod.app
    api.dependency_overrides[appmod.get_session] = lambda: Local()
    api.dependency_overrides[appmod.current_user] = lambda: u
    api.dependency_overrides[appmod.require_admin] = lambda: u
    yield TestClient(api), db, ws
    api.dependency_overrides.clear()


# --- units -----------------------------------------------------------------
def test_cpu_is_reported_in_cores_not_percent(env):
    client, *_ = env
    d = client.get("/api/workspace/metrics").json()
    assert d["cpu"], "no cpu samples"
    # 10 counter-seconds per 20 wall-seconds = 0.5 cores. As a percentage of a
    # 2-core tier this would have been 25.0 - the old behaviour.
    assert all(p["value"] == pytest.approx(0.5, abs=0.01) for p in d["cpu"]), d["cpu"]


def test_memory_is_reported_in_gigabytes_not_percent(env):
    client, *_ = env
    d = client.get("/api/workspace/metrics").json()
    # 1 GiB of a 4 GiB tier: 1.0 in absolute terms, 25.0 as a percentage.
    assert all(p["value"] == pytest.approx(1.0, abs=0.01) for p in d["memory"])


def test_the_tier_is_sent_so_the_chart_can_show_headroom(env):
    """Scaling to the tallest sample would make a quiet machine look busy."""
    client, *_ = env
    d = client.get("/api/workspace/metrics").json()
    assert d["cpu_cores"] == 2.0
    assert d["memory_gb"] == 4.0


def test_cpu_never_exceeds_the_tier(env, monkeypatch):
    """A counter that jumps - a restart, a clock step - must not draw a spike
    taller than the machine can physically be."""
    client, db, ws = env
    now = svc.now()
    db.add(UsageSample(workspace_id=ws.id, ts=now + timedelta(seconds=20),
                       cpu_seconds_total=100000.0, mem_bytes=1073741824))
    db.commit()
    d = client.get("/api/workspace/metrics?minutes=15").json()
    assert all(p["value"] <= ws.cpu_cores for p in d["cpu"])


# --- windows ---------------------------------------------------------------
def test_the_default_window_is_five_minutes(env):
    client, *_ = env
    assert client.get("/api/workspace/metrics").json()["minutes"] == 5


def test_the_offered_windows_are_advertised(env):
    """The picker is built from this, so the interface and the server cannot
    disagree about what is selectable."""
    client, *_ = env
    d = client.get("/api/workspace/metrics").json()
    assert d["windows"] == [5, 15, 60, 360, 1440]


def test_a_window_outside_the_range_is_clamped_not_refused(env):
    client, *_ = env
    assert client.get("/api/workspace/metrics?minutes=0").json()["minutes"] == 1
    assert client.get("/api/workspace/metrics?minutes=99999").json()["minutes"] == 1440


def test_a_short_window_excludes_older_samples(env):
    client, db, ws = env
    db.add(UsageSample(workspace_id=ws.id, ts=svc.now() - timedelta(hours=3),
                       cpu_seconds_total=1.0, mem_bytes=1))
    db.commit()
    assert client.get("/api/workspace/metrics?minutes=5").json()["samples"] == 6
    assert client.get("/api/workspace/metrics?minutes=360").json()["samples"] == 7


def test_the_refresh_cadence_is_sent_to_the_interface(env):
    """So the page redraws in step with the data instead of guessing."""
    client, *_ = env
    assert client.get("/api/workspace/metrics").json()["sample_seconds"] == 20


# --- the host-wide view ----------------------------------------------------
def test_admin_metrics_sums_every_workspace(env):
    client, db, ws = env
    other = User(email="two@example.com", password_hash="x", status=UserStatus.APPROVED)
    db.add(other)
    db.commit()
    ws2 = Workspace(user_id=other.id, idx=8, incus_project="ws-8",
                    state=WorkspaceState.ON, cpu_milli=1000, mem_mib=1024)
    db.add(ws2)
    db.commit()
    now = svc.now()
    for i in range(6):
        db.add(UsageSample(workspace_id=ws2.id, ts=now - timedelta(seconds=20 * (5 - i)),
                           cpu_seconds_total=50.0 + 10.0 * i, mem_bytes=1073741824))
    db.commit()

    d = client.get("/api/admin/metrics").json()
    assert d["workspaces"] == 2
    # Both machines burn 0.5 cores and hold 1 GiB, so the host total is double.
    assert max(p["value"] for p in d["cpu"]) == pytest.approx(1.0, abs=0.05)
    assert max(p["value"] for p in d["memory"]) == pytest.approx(2.0, abs=0.05)


def test_admin_metrics_scales_against_sellable_capacity(env):
    """Not the raw host total - the host reserve is not for sale."""
    client, *_ = env
    d = client.get("/api/admin/metrics").json()
    cap = client.get("/api/admin/capacity").json()
    assert d["cpu_cores"] == cap["schedulable_cores"]
    assert d["memory_gb"] == pytest.approx(cap["schedulable_mem_gib"], abs=0.01)


def test_admin_metrics_is_admin_only(env):
    """The route must not be reachable by an ordinary customer."""
    import inspect
    src = inspect.getsource(appmod.admin_metrics)
    assert "require_admin" in src


# --- the cadence behind it -------------------------------------------------
def test_faster_sampling_did_not_change_settlement_timing():
    """Sampling went from 60s to 20s so a five-minute chart has enough points.
    The multipliers must keep settlement at 5 minutes and reconciliation at 15,
    or billing and recovery silently start running three times as often.
    """
    from mmd import worker
    assert worker.TICK_SECONDS == 20
    assert worker.LOOP_SECONDS * worker.METRICS_EVERY == worker.TICK_SECONDS
    assert worker.LOOP_SECONDS * worker.SETTLE_EVERY == 300
    assert worker.LOOP_SECONDS * worker.RECONCILE_EVERY == 900
    # The interface is told the real cadence.
    assert appmod.SAMPLE_SECONDS == worker.TICK_SECONDS


def test_samples_are_pruned():
    """There was no pruning at all - the table grew forever, and tripling the
    sample rate would have tripled the rate it grew at."""
    from mmd import worker
    assert worker.SAMPLE_RETENTION_DAYS > 0
    assert hasattr(worker, "prune_samples_once")


# --- remaining time, in days -----------------------------------------------
def test_remaining_time_is_offered_in_days(env):
    """Shown to customers in days. At the default tier a funded account has
    hundreds of hours left, and a four-digit hour count is not a number anyone
    can act on."""
    from mmd.models import CreditAccount
    client, db, ws = env
    db.add(CreditAccount(user_id=ws.user_id, balance_micro=500_000 * 1_000_000))
    db.commit()
    d = client.get("/api/workspace").json()
    assert d["days_remaining"] == pytest.approx(d["hours_remaining"] / 24, rel=1e-9)
    assert d["days_remaining"] > 1


def test_hours_are_still_reported(env):
    """Kept in the payload: it is the honest unit the figure is derived in, and
    the days value is only a presentation choice on top of it."""
    client, *_ = env
    assert "hours_remaining" in client.get("/api/workspace").json()


def test_a_free_tier_does_not_divide_by_zero(env, monkeypatch):
    from mmd.billing import pricing
    monkeypatch.setattr(pricing, "max_hour_micro", lambda *a, **k: 0)
    client, *_ = env
    d = client.get("/api/workspace").json()
    assert d["days_remaining"] == 0


def test_the_firewall_is_resynced_on_every_pass():
    """Deleting a workspace cascades its port rows away, but nothing re-synced
    the host firewall - so DNAT entries for a machine that no longer existed sat
    there until some unrelated port change. The provisioner rewrites the whole
    rule set from what it is handed, so pushing every pass is idempotent and is
    what makes the mapping self-healing."""
    import inspect
    from mmd import worker
    src = inspect.getsource(worker.reserve_service_ports_once)
    body = src[src.index("with SessionLocal"):]
    assert "svc.sync_published_ports(db)" in body
    # Not nested under the "did we change anything" branch.
    for line in body.splitlines():
        if "sync_published_ports" in line:
            assert line.startswith("        svc."), f"still conditional: {line!r}"
