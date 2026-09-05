"""The global audit trail is usable, searchable and bounded."""
import os

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from mmd import app as appmod  # noqa: E402
from mmd.models import AuditLog, Base, User, UserStatus  # noqa: E402


def database():
    db = Session(create_engine("sqlite://"), expire_on_commit=False)
    Base.metadata.create_all(db.bind)
    return db


def seed(db):
    admin = User(username="operator", password_hash="x", is_admin=True,
                 status=UserStatus.APPROVED)
    customer = User(username="ali", password_hash="x",
                    status=UserStatus.APPROVED)
    db.add_all([admin, customer]); db.commit()
    db.add_all([
        AuditLog(actor_id=customer.id, action="power_on", target="ws-2",
                 detail={"source": "console"}),
        AuditLog(actor_id=admin.id, action="grant_credit", target="ali",
                 detail={"credits": 50_000}),
        AuditLog(actor_id=None, action="auto_stop", target="ws-2", detail={}),
    ])
    db.commit()
    return admin


def test_admin_activity_is_paginated_and_names_actors():
    db = database()
    admin = seed(db)
    out = appmod.admin_activity(limit=2, offset=0, q="", _=admin, db=db)
    assert out["total"] == 3
    assert out["limit"] == 2
    assert len(out["events"]) == 2
    assert {event["actor"] for event in out["events"]} <= {None, "operator", "ali"}


def test_admin_activity_searches_actor_action_and_target():
    db = database()
    admin = seed(db)
    by_actor = appmod.admin_activity(q="operator", _=admin, db=db)
    by_action = appmod.admin_activity(q="power_on", _=admin, db=db)
    by_target = appmod.admin_activity(q="ws-2", _=admin, db=db)
    assert [event["action"] for event in by_actor["events"]] == ["grant_credit"]
    assert [event["action"] for event in by_action["events"]] == ["power_on"]
    assert {event["action"] for event in by_target["events"]} == {"power_on", "auto_stop"}
