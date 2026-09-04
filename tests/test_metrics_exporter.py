"""The Prometheus exporter, and the registry behind it.

Written after shipping two bugs the existing tests could not have caught,
because nothing ever called the endpoint: a NameError that made every scrape a
500, and a worker snapshot that was written to a varchar(255) column and failed
every worker tick. Both are pinned below.
"""
import os

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

import pytest                                       # noqa: E402
from fastapi.testclient import TestClient           # noqa: E402
from sqlalchemy import create_engine                # noqa: E402
from sqlalchemy.orm import Session, sessionmaker    # noqa: E402
from sqlalchemy.pool import StaticPool              # noqa: E402

from mmd import app as appmod                       # noqa: E402
from mmd import metrics as m                        # noqa: E402
from mmd.models import (Base, CreditAccount, User,  # noqa: E402
                        UserStatus, Workspace, WorkspaceState)


@pytest.fixture
def env(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Local()
    u = User(username="ali", phone="09120000000", password_hash="x",
             status=UserStatus.APPROVED)
    db.add(u); db.commit()
    db.add(CreditAccount(user_id=u.id, balance_micro=250_000_000))
    db.add(Workspace(user_id=u.id, idx=3, incus_project="ws-3",
                     state=WorkspaceState.ON, mem_mib=2048))
    db.commit()
    import dataclasses
    monkeypatch.setattr(appmod, "CONFIG",
                        dataclasses.replace(appmod.CONFIG,
                                            prometheus_token="tok"))
    appmod.app.dependency_overrides[appmod.get_session] = lambda: Local()
    yield TestClient(appmod.app), db
    appmod.app.dependency_overrides.clear()


def scrape(client):
    r = client.get("/internal/metrics",
                   headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200, r.text
    return r.text


# --- it renders at all ----------------------------------------------------
def test_the_endpoint_actually_renders(env):
    """The regression that mattered: an un-imported model made every scrape a
    500 while the whole suite stayed green, because nothing called it."""
    client, _db = env
    body = scrape(client)
    assert body.endswith("# EOF\n")
    assert "mmd_build_info" in body


def test_it_is_closed_without_the_token(env):
    client, _db = env
    assert client.get("/internal/metrics").status_code == 401
    assert client.get("/internal/metrics",
                      headers={"Authorization": "Bearer wrong"}).status_code == 401


# --- the two committed catalogue items that were missing ------------------
def test_credit_block_state_and_lifecycle_state_are_exported(env):
    client, _db = env
    body = scrape(client)
    assert "mmd_user_openrouter_blocked" in body
    # Per username, not just the fleet aggregate: "three are in error" does not
    # tell you which customer to look at.
    assert 'mmd_workspace_state{username="ali",state="on"}' in body


# --- cardinality ----------------------------------------------------------
def test_request_metrics_label_the_route_template_not_the_path(env):
    """One series per route, not one per ticket per customer. This is how a
    monitoring stack gets taken down by the thing it was installed to watch."""
    client, _db = env
    client.get("/api/tickets/12345")
    client.get("/api/tickets/67890")
    body = scrape(client)
    assert "12345" not in body and "67890" not in body


def test_unknown_paths_collapse_to_one_series(env):
    """A scanner walking random URLs must not be able to mint series. Unknown
    paths fall through to the SPA catch-all, which is a single template."""
    client, _db = env
    for i in range(3):
        client.get(f"/definitely-not-a-route-{i}")
    body = scrape(client)
    assert "not-a-route" not in body
    series = [l for l in body.splitlines()
              if l.startswith("mmd_http_requests_total{") and "GET" in l
              and "full_path" in l]
    assert len(series) == 1 and series[0].endswith(" 3")


def test_status_is_a_class_not_a_code(env):
    client, _db = env
    client.get("/internal/metrics")          # 401
    body = scrape(client)
    assert 'status="4xx"' in body
    assert 'status="401"' not in body


# --- the registry ---------------------------------------------------------
def test_rendering_a_foreign_snapshot_does_not_mutate_the_registry():
    """The bug this pins: the exporter renders on every scrape, so folding the
    worker's cumulative counters INTO ours would add one worker-lifetime per
    scrape and the graph would climb forever on its own."""
    m.reset()
    m.inc("mmd_test_total", {"a": "1"}, 5)
    snap = {"counters": [["mmd_test_total", [["a", "1"]], 7]]}

    first = [l for l in m.render(snap) if l.startswith("mmd_test_total{")]
    second = [l for l in m.render(snap) if l.startswith("mmd_test_total{")]
    assert first == second == ['mmd_test_total{a="1"} 12']
    # Own value untouched.
    assert [l for l in m.render() if l.startswith("mmd_test_total{")] \
        == ['mmd_test_total{a="1"} 5']
    m.reset()


def test_histograms_are_cumulative_and_carry_an_inf_bucket():
    m.reset()
    m.observe("mmd_test_seconds", 0.3)
    m.observe("mmd_test_seconds", 45.0)
    out = "\n".join(m.render())
    assert 'mmd_test_seconds_bucket{le="0.25"} 0' in out
    assert 'mmd_test_seconds_bucket{le="0.5"} 1' in out
    assert 'mmd_test_seconds_bucket{le="+Inf"} 2' in out
    assert "mmd_test_seconds_count 2" in out
    m.reset()


def test_the_timer_records_even_when_the_body_raises():
    """A call that failed still took time; hiding it makes the p99 of a broken
    dependency look healthy."""
    m.reset()
    with pytest.raises(ValueError):
        with m.timer("mmd_test_seconds"):
            raise ValueError("boom")
    assert "mmd_test_seconds_count 1" in "\n".join(m.render())
    m.reset()


def test_the_worker_snapshot_round_trips_through_a_file(tmp_path):
    """It is a FILE, not a settings row: Setting.value is varchar(255) and a
    snapshot is past that, which failed every worker tick until it was found."""
    m.reset()
    m.inc("mmd_test_total", {"verb": "provision"}, 3)
    m.observe("mmd_test_seconds", 1.5, {"verb": "provision"})
    path = str(tmp_path / "snap.json")
    m.write_snapshot(path)

    m.reset()
    loaded = m.read_snapshot(path)
    out = "\n".join(m.render(loaded))
    assert 'mmd_test_total{verb="provision"} 3' in out
    assert 'mmd_test_seconds_count{verb="provision"} 1' in out
    m.reset()


def test_a_missing_snapshot_is_not_an_error():
    assert m.read_snapshot("/nonexistent/path.json") == {}
