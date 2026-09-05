"""Support tickets: ownership, the automatic status transitions, unread marks.

These run against a real SQLite database through the same SQLAlchemy models the
control plane uses, so the constraints and defaults being exercised are the ones
that ship - not a re-description of them.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from mmd.models import Base, Ticket, TicketMessage, TicketStatus, User, UserStatus
from mmd.tickets import as_utc, is_unread


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def people(db):
    customer = User(password_hash="x",
                    status=UserStatus.APPROVED)
    staff = User(password_hash="x", is_admin=True,
                 status=UserStatus.APPROVED)
    db.add_all([customer, staff])
    db.commit()
    return customer, staff


def _open(db, user, subject="cannot start my machine", body="it just spins"):
    tk = Ticket(user_id=user.id, subject=subject, status=TicketStatus.OPEN)
    tk.messages.append(TicketMessage(author_id=user.id, from_staff=False, body=body))
    db.add(tk)
    db.commit()
    return tk


def test_a_new_ticket_starts_open_with_its_first_message(db, people):
    customer, _ = people
    tk = _open(db, customer)
    assert tk.status is TicketStatus.OPEN
    assert len(tk.messages) == 1
    assert tk.messages[0].from_staff is False


def test_messages_keep_their_order(db, people):
    customer, staff = people
    tk = _open(db, customer)
    tk.messages.append(TicketMessage(author_id=staff.id, from_staff=True, body="two"))
    tk.messages.append(TicketMessage(author_id=customer.id, from_staff=False, body="three"))
    db.commit()
    db.expire(tk)
    assert [m.body for m in tk.messages] == ["it just spins", "two", "three"]


def test_authorship_is_recorded_not_derived(db, people):
    """A customer later promoted to admin must not have their old questions
    turn into staff answers."""
    customer, _ = people
    tk = _open(db, customer)
    customer.is_admin = True
    db.commit()
    db.expire(tk)
    assert tk.messages[0].from_staff is False


def test_deleting_the_operator_keeps_their_answers(db, people):
    """ON DELETE SET NULL, like the audit log: a thread that loses half its
    content when someone leaves the company is not a record of anything."""
    customer, staff = people
    tk = _open(db, customer)
    tk.messages.append(TicketMessage(author_id=staff.id, from_staff=True, body="have you tried"))
    db.commit()
    # SQLite needs foreign keys switched on explicitly for ON DELETE to fire.
    db.execute(__import__("sqlalchemy").text("PRAGMA foreign_keys=ON"))
    db.delete(staff)
    db.commit()
    db.expire(tk)
    bodies = [m.body for m in tk.messages]
    assert "have you tried" in bodies
    assert tk.messages[1].author_id is None


def test_deleting_the_customer_removes_the_ticket(db, people):
    """The opposite direction: a ticket has no meaning without its owner, and
    leaving orphans behind would strand them in the staff queue forever."""
    import sqlalchemy
    customer, _ = people
    _open(db, customer)
    db.execute(sqlalchemy.text("PRAGMA foreign_keys=ON"))
    db.delete(customer)
    db.commit()
    assert db.scalars(select(Ticket)).all() == []
    assert db.scalars(select(TicketMessage)).all() == []


def test_every_status_has_a_stable_wire_value(db):
    """The web filter chips and the API both send these strings.

    `escalated` sits between answered and closed: the queue reads left to
    right as "waiting on us, being worked, answered, needs a person, done",
    and the one that needs a person must not hide at the end.
    """
    assert [s.value for s in TicketStatus] == [
        "open", "in_progress", "answered", "escalated", "closed"]


# --- the unread rule, which is the part that is easy to get backwards -------
def _unread(tk, *, staff):
    """The shipped rule, called directly - not a re-description of it."""
    last = tk.messages[-1] if tk.messages else None
    return is_unread(last.from_staff if last else None,
                     last.created_at if last else None,
                     tk.staff_read_at if staff else tk.user_read_at, staff=staff)


def test_your_own_message_is_never_unread_to_you(db, people):
    customer, _ = people
    tk = _open(db, customer)
    tk.user_read_at = None
    db.commit()
    assert _unread(tk, staff=False) is False
    # ...but it IS unread to the other side.
    assert _unread(tk, staff=True) is True


def test_an_answer_is_unread_until_the_customer_looks(db, people):
    customer, staff = people
    tk = _open(db, customer)
    now = datetime.now(timezone.utc)
    tk.user_read_at = now
    tk.messages.append(TicketMessage(author_id=staff.id, from_staff=True,
                                     body="fixed", created_at=now + timedelta(minutes=1)))
    db.commit()
    assert _unread(tk, staff=False) is True
    tk.user_read_at = now + timedelta(minutes=2)
    assert _unread(tk, staff=False) is False


def test_mixed_timezone_awareness_does_not_raise():
    """Postgres hands back aware timestamps, SQLite naive ones. A comparison
    across the two raises TypeError, which would surface as a 500 on the
    support page rather than as anything diagnosable."""
    later = datetime(2026, 1, 1, 12, 0)                          # naive
    earlier = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)    # aware
    # Written after it was read -> unread, whichever side carries the tzinfo.
    assert is_unread(True, later, earlier, staff=False) is True
    assert is_unread(True, earlier, later, staff=False) is False
    assert as_utc(later).tzinfo is not None


def test_a_ticket_with_no_messages_is_not_unread():
    assert is_unread(None, None, None, staff=False) is False
    assert is_unread(None, None, None, staff=True) is False
