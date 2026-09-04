"""Database backups delivered to an administrator's Telegram.

This is the one feature that deliberately moves every secret the platform holds
off the host in a single file, so the tests are mostly about the boundaries:
who may configure it, what the panel is allowed to see, and what happens when
delivery fails. A backup feature that fails quietly is worse than none, because
it replaces "we have no backups" with "we believe we have backups".
"""
import os

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

from datetime import datetime, timedelta, timezone      # noqa: E402
from pathlib import Path                                # noqa: E402

import pytest                                           # noqa: E402
from fastapi.testclient import TestClient               # noqa: E402
from sqlalchemy import create_engine                    # noqa: E402
from sqlalchemy.orm import sessionmaker                 # noqa: E402
from sqlalchemy.pool import StaticPool                  # noqa: E402

from mmd import app as appmod                           # noqa: E402
from mmd import backup as bk                            # noqa: E402
from mmd.models import Base, User, UserStatus           # noqa: E402

TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
CHAT = "375210989"


@pytest.fixture
def env():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Local()
    admin = User(password_hash="x",
                 status=UserStatus.APPROVED, username="ali", is_admin=True)
    plain = User(password_hash="x",
                 status=UserStatus.APPROVED, username="reza", is_admin=False)
    db.add_all([admin, plain])
    db.commit()

    api = appmod.app
    api.dependency_overrides[appmod.get_session] = lambda: Local()
    api.dependency_overrides[appmod.current_user] = lambda: admin
    yield TestClient(api), db, api, admin, plain
    api.dependency_overrides.clear()


# --- who may touch it -----------------------------------------------------
def test_backups_are_an_admin_feature_not_a_customer_one(env):
    client, _db, api, _admin, plain = env
    api.dependency_overrides[appmod.current_user] = lambda: plain
    for call in (lambda: client.get("/api/admin/backup"),
                 lambda: client.put("/api/admin/backup", json={"enabled": False}),
                 lambda: client.post("/api/admin/backup/run")):
        assert call().status_code == 403


# --- the token never comes back -------------------------------------------
def test_the_panel_is_never_sent_the_bot_token(env):
    client, db, *_ = env
    client.put("/api/admin/backup",
               json={"enabled": True, "interval_minutes": 60,
                     "chat_id": CHAT, "bot_token": TOKEN})
    body = client.get("/api/admin/backup").text
    assert TOKEN not in body
    # Enough to recognise WHICH bot, without the panel ever holding the secret.
    d = client.get("/api/admin/backup").json()
    assert d["bot_token_set"] is True
    assert d["bot_token_hint"] == TOKEN[-4:]


def test_an_omitted_token_keeps_the_stored_one(env):
    """A page that cannot display the token must not erase it on every save.
    Otherwise an admin changing the interval silently turns delivery off."""
    client, db, *_ = env
    client.put("/api/admin/backup",
               json={"enabled": True, "interval_minutes": 60,
                     "chat_id": CHAT, "bot_token": TOKEN})
    client.put("/api/admin/backup",
               json={"enabled": True, "interval_minutes": 30, "chat_id": CHAT})
    assert bk._get(db, bk.SETTING_TOKEN) == TOKEN
    assert bk.config(db)["interval_minutes"] == 30


# --- refusing to look armed when it is not --------------------------------
def test_enabling_without_a_destination_is_refused(env):
    client, *_ = env
    r = client.put("/api/admin/backup",
                   json={"enabled": True, "interval_minutes": 60, "chat_id": ""})
    assert r.status_code == 400
    assert client.get("/api/admin/backup").json()["enabled"] is False


def test_a_malformed_token_or_chat_id_is_refused(env):
    client, *_ = env
    assert client.put("/api/admin/backup",
                      json={"enabled": False, "interval_minutes": 60,
                            "chat_id": CHAT, "bot_token": "nope"}).status_code == 400
    assert client.put("/api/admin/backup",
                      json={"enabled": False, "interval_minutes": 60,
                            "chat_id": "abc"}).status_code == 400


# --- scheduling -----------------------------------------------------------
def test_the_interval_is_clamped_to_something_deliverable():
    # Shorter than a run takes would queue backups behind each other forever.
    assert bk.clamp_interval(1) == bk.MIN_INTERVAL
    assert bk.clamp_interval(999999) == bk.MAX_INTERVAL
    assert bk.clamp_interval("nonsense") == bk.DEFAULT_INTERVAL
    assert bk.clamp_interval(60) == 60


def test_nothing_is_due_while_backups_are_off(env):
    _client, db, *_ = env
    bk.save(db, enabled=False, interval_minutes=60, chat_id=CHAT, bot_token=TOKEN)
    db.commit()
    assert bk.due(db) is False


def test_the_interval_is_measured_from_the_last_SUCCESS(env):
    """From the last success, not the last attempt: a failing backup should be
    retried on the normal cadence, not skipped until the failure ages out."""
    _client, db, *_ = env
    bk.save(db, enabled=True, interval_minutes=60, chat_id=CHAT, bot_token=TOKEN)
    db.commit()
    # Never run: owed immediately.
    assert bk.due(db) is True

    now = datetime.now(timezone.utc)
    bk._set(db, bk.SETTING_LAST_OK, (now - timedelta(minutes=30)).isoformat())
    db.commit()
    assert bk.due(db) is False

    bk._set(db, bk.SETTING_LAST_OK, (now - timedelta(minutes=61)).isoformat())
    db.commit()
    assert bk.due(db) is True


# --- the dump -------------------------------------------------------------
def test_the_driver_is_stripped_from_the_url_pg_dump_is_given():
    """SQLAlchemy writes postgresql+psycopg://; libpq rejects that form."""
    assert bk.dump_url("postgresql+psycopg://u:p@h/db") == "postgresql://u:p@h/db"
    assert bk.dump_url("postgresql+psycopg2://u:p@h/db") == "postgresql://u:p@h/db"
    assert bk.dump_url("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


def test_a_dump_too_large_for_telegram_is_reported_not_attempted(env, monkeypatch):
    _client, db, *_ = env
    bk.save(db, enabled=True, interval_minutes=60, chat_id=CHAT, bot_token=TOKEN)
    db.commit()

    monkeypatch.setattr(bk, "dump_to", lambda p, url=None: bk.TELEGRAM_MAX_BYTES + 1)
    sent = []
    monkeypatch.setattr(bk, "send_document",
                        lambda *a, **k: sent.append(a))

    r = bk.run_once(db)
    assert r["ok"] is False and not sent
    assert "limit" in bk.config(db)["last_error"]


def test_a_failed_send_is_recorded_where_an_admin_will_see_it(env, monkeypatch):
    """Logged-only would let an operator believe they had backups for a month."""
    _client, db, *_ = env
    bk.save(db, enabled=True, interval_minutes=60, chat_id=CHAT, bot_token=TOKEN)
    db.commit()

    monkeypatch.setattr(bk, "dump_to", lambda p, url=None: 1024)

    def boom(*_a, **_k):
        raise bk.BackupError("telegram rejected the upload: chat not found")

    monkeypatch.setattr(bk, "send_document", boom)
    r = bk.run_once(db)
    assert r["ok"] is False
    assert "chat not found" in bk.config(db)["last_error"]
    # A failure must not advance the clock, or the retry is skipped.
    assert bk.config(db)["last_ok_at"] is None
    assert bk.due(db) is True


def test_a_successful_run_records_when_and_how_big(env, monkeypatch):
    _client, db, *_ = env
    bk.save(db, enabled=True, interval_minutes=60, chat_id=CHAT, bot_token=TOKEN)
    db.commit()
    monkeypatch.setattr(bk, "dump_to", lambda p, url=None: 4096)
    monkeypatch.setattr(bk, "send_document", lambda *a, **k: None)

    assert bk.run_once(db)["ok"] is True
    d = bk.config(db)
    assert d["last_size"] == 4096 and d["last_ok_at"] and not d["last_error"]
    assert bk.due(db) is False


def test_run_once_never_raises_into_the_worker_loop(env, monkeypatch):
    _client, db, *_ = env
    bk.save(db, enabled=True, interval_minutes=60, chat_id=CHAT, bot_token=TOKEN)
    db.commit()

    def explode(*_a, **_k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(bk, "dump_to", explode)
    assert bk.run_once(db)["ok"] is False
    assert "disk gone" in bk.config(db)["last_error"]


# --- wiring ---------------------------------------------------------------
def test_the_worker_runs_backups_off_the_event_loop():
    """A dump plus an upload is seconds to minutes of blocking work. Inline, it
    would stall metering, auto-stop and every reconciler behind it."""
    src = (Path(__file__).resolve().parents[1]
           / "control" / "mmd" / "worker.py").read_text()
    assert "async def backup_once()" in src
    assert "asyncio.to_thread" in src
    assert "await backup_once()" in src


def test_configuring_backups_is_audited_without_the_token():
    src = (Path(__file__).resolve().parents[1]
           / "control" / "mmd" / "app.py").read_text()
    block = src.split('def admin_backup_put', 1)[1].split("@app.post", 1)[0]
    assert 'svc.audit' in block
    assert "bot_token" not in block.split("svc.audit", 1)[1]
