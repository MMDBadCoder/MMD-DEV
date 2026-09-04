"""The ticket HTTP surface, exercised through FastAPI itself.

Runs against SQLite with the real routes, real Pydantic models and the real
ownership checks. The session dependency is overridden rather than faked with a
cookie: the point under test is authorisation logic, not the cookie format.
"""
import os

import pytest

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

from fastapi.testclient import TestClient          # noqa: E402
from sqlalchemy import create_engine               # noqa: E402
from sqlalchemy.orm import Session, sessionmaker   # noqa: E402
from sqlalchemy.pool import StaticPool             # noqa: E402

from mmd import app as appmod                      # noqa: E402
from mmd.models import Base, Ticket, TicketStatus, User, UserStatus  # noqa: E402


@pytest.fixture
def env():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    db = Local()
    alice = User(username="alice", phone="09111111111", password_hash="x", status=UserStatus.APPROVED)
    bob = User(username="bob", phone="09222222222", password_hash="x", status=UserStatus.APPROVED)
    ops = User(username="ops", phone="09333333333", password_hash="x", is_admin=True,
               status=UserStatus.APPROVED)
    db.add_all([alice, bob, ops])
    db.commit()

    api = appmod.app
    api.dependency_overrides[appmod.get_session] = lambda: Local()

    def as_user(u):
        api.dependency_overrides[appmod.current_user] = lambda: u
        api.dependency_overrides[appmod.require_admin] = lambda: u

    as_user(alice)
    client = TestClient(api)
    yield client, as_user, alice, bob, ops, db
    api.dependency_overrides.clear()


def _open(client, subject="machine will not start", body="it hangs on boot"):
    r = client.post("/api/tickets", json={"subject": subject, "body": body})
    assert r.status_code == 200, r.text
    return r.json()["ticket"]


# --- the customer's own thread --------------------------------------------
def test_opening_a_ticket_stores_the_first_message(env):
    client, *_ = env
    tk = _open(client)
    assert tk["status"] == "open"
    assert [m["body"] for m in tk["messages"]] == ["it hangs on boot"]
    assert tk["messages"][0]["from_staff"] is False


def test_a_short_subject_is_refused(env):
    client, *_ = env
    r = client.post("/api/tickets", json={"subject": "ab", "body": "hello"})
    assert r.status_code == 422


def test_an_over_long_message_is_refused(env):
    client, *_ = env
    r = client.post("/api/tickets", json={"subject": "hello there", "body": "x" * 4001})
    assert r.status_code == 422


def test_the_list_only_shows_your_own(env):
    client, as_user, alice, bob, *_ = env
    _open(client, "alice's problem")
    as_user(bob)
    _open(client, "bob's problem")
    mine = client.get("/api/tickets").json()["tickets"]
    assert [t["subject"] for t in mine] == ["bob's problem"]


def test_reading_someone_elses_ticket_is_a_404_not_a_403(env):
    """A 403 would confirm the ticket exists, which is enough to enumerate how
    many other customers there are and when they wrote."""
    client, as_user, alice, bob, *_ = env
    tk = _open(client)
    as_user(bob)
    assert client.get(f"/api/tickets/{tk['id']}").status_code == 404
    assert client.post(f"/api/tickets/{tk['id']}/messages",
                       json={"body": "let me see"}).status_code == 404


def test_open_tickets_are_capped(env):
    client, *_ = env
    for i in range(appmod.MAX_OPEN_TICKETS):
        _open(client, f"issue number {i}")
    r = client.post("/api/tickets", json={"subject": "one too many", "body": "hi"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "too_many_tickets"


def test_a_closed_ticket_does_not_count_against_the_cap(env):
    client, as_user, alice, bob, ops, db = env
    ids = [_open(client, f"issue number {i}")["id"]
           for i in range(appmod.MAX_OPEN_TICKETS)]
    as_user(ops)
    client.put(f"/api/admin/tickets/{ids[0]}/status", json={"status": "closed"})
    as_user(alice)
    assert client.post("/api/tickets",
                       json={"subject": "room for one more", "body": "hi"}).status_code == 200


# --- the two automatic transitions ----------------------------------------
def test_a_staff_reply_marks_the_ticket_answered(env):
    client, as_user, alice, bob, ops, _ = env
    tk = _open(client)
    as_user(ops)
    out = client.post(f"/api/admin/tickets/{tk['id']}/messages",
                      json={"body": "try switching it off and on"}).json()["ticket"]
    assert out["status"] == "answered"
    assert out["messages"][-1]["from_staff"] is True


def test_a_customer_reply_reopens_an_answered_ticket(env):
    client, as_user, alice, bob, ops, _ = env
    tk = _open(client)
    as_user(ops)
    client.post(f"/api/admin/tickets/{tk['id']}/messages", json={"body": "done"})
    as_user(alice)
    out = client.post(f"/api/tickets/{tk['id']}/messages",
                      json={"body": "still broken"}).json()["ticket"]
    assert out["status"] == "open"


def test_a_customer_reply_reopens_a_closed_ticket(env):
    """Refusing the message instead would push the customer into opening a
    duplicate that has lost all the context of the original."""
    client, as_user, alice, bob, ops, _ = env
    tk = _open(client)
    as_user(ops)
    client.put(f"/api/admin/tickets/{tk['id']}/status", json={"status": "closed"})
    as_user(alice)
    out = client.post(f"/api/tickets/{tk['id']}/messages",
                      json={"body": "it came back"}).json()["ticket"]
    assert out["status"] == "open"


def test_answering_a_closed_ticket_leaves_it_closed(env):
    """The operator closed it deliberately; a follow-up note must not silently
    put it back in the queue."""
    client, as_user, alice, bob, ops, _ = env
    tk = _open(client)
    as_user(ops)
    client.put(f"/api/admin/tickets/{tk['id']}/status", json={"status": "closed"})
    out = client.post(f"/api/admin/tickets/{tk['id']}/messages",
                      json={"body": "for the record"}).json()["ticket"]
    assert out["status"] == "closed"


# --- unread marks ----------------------------------------------------------
def test_a_new_ticket_is_unread_to_staff_and_read_to_its_author(env):
    client, as_user, alice, bob, ops, _ = env
    tk = _open(client)
    assert client.get("/api/tickets").json()["tickets"][0]["unread"] is False
    as_user(ops)
    assert client.get("/api/admin/tickets").json()["tickets"][0]["unread"] is True


def test_opening_the_thread_clears_the_mark(env):
    client, as_user, alice, bob, ops, _ = env
    tk = _open(client)
    as_user(ops)
    client.get(f"/api/admin/tickets/{tk['id']}")
    assert client.get("/api/admin/tickets").json()["tickets"][0]["unread"] is False


def test_an_answer_shows_as_unread_to_the_customer(env):
    client, as_user, alice, bob, ops, _ = env
    tk = _open(client)
    as_user(ops)
    client.post(f"/api/admin/tickets/{tk['id']}/messages", json={"body": "fixed"})
    as_user(alice)
    assert client.get("/api/tickets").json()["tickets"][0]["unread"] is True
    client.get(f"/api/tickets/{tk['id']}")
    assert client.get("/api/tickets").json()["tickets"][0]["unread"] is False


# --- staff queue -----------------------------------------------------------
def test_the_queue_shows_every_customer(env):
    client, as_user, alice, bob, ops, _ = env
    _open(client, "alice's problem")
    as_user(bob)
    _open(client, "bob's problem")
    as_user(ops)
    rows = client.get("/api/admin/tickets").json()
    assert {t["subject"] for t in rows["tickets"]} == {"alice's problem", "bob's problem"}
    assert {t["user_username"] for t in rows["tickets"]} == {"alice", "bob"}


def test_the_queue_filters_by_status_and_counts(env):
    client, as_user, alice, bob, ops, _ = env
    a = _open(client, "first problem")
    b = _open(client, "second problem")
    as_user(ops)
    client.put(f"/api/admin/tickets/{b['id']}/status", json={"status": "closed"})
    out = client.get("/api/admin/tickets?status=closed").json()
    assert [t["subject"] for t in out["tickets"]] == ["second problem"]
    assert out["counts"]["closed"] == 1
    assert out["counts"]["open"] == 1


def test_an_unknown_status_is_refused(env):
    client, as_user, alice, bob, ops, _ = env
    tk = _open(client)
    as_user(ops)
    assert client.put(f"/api/admin/tickets/{tk['id']}/status",
                      json={"status": "deleted"}).status_code == 422


def test_staff_only_identity_is_never_exposed_to_the_customer_view(env):
    """The staff serialiser adds it; the customer one must not, or one
    customer's ticket JSON becomes a way to read another's address."""
    client, *_ = env
    tk = _open(client)
    out = client.get(f"/api/tickets/{tk['id']}").json()["ticket"]
    assert "user_phone" not in out
