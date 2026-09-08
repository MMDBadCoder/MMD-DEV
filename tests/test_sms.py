"""Approval SMS, and the outbox that carries it.

The interesting properties are not "does it send" - they are the ones that keep
a message service from becoming a liability: the internet-facing process never
holds the provider key, an approval that cannot be texted still approves, a
re-approval does not text twice, and a template that grows past one segment
starts costing double without anyone noticing.
"""
import os

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

from datetime import UTC, datetime, timedelta      # noqa: E402
from pathlib import Path                           # noqa: E402

import pytest                                      # noqa: E402
from sqlalchemy import create_engine, select       # noqa: E402
from sqlalchemy.orm import Session                 # noqa: E402

from mmd import app as appmod                      # noqa: E402
from mmd import sms as smslib                      # noqa: E402
from mmd.models import (BaleContact, Base, CreditAccount, SmsMessage, User,  # noqa: E402
                        UserStatus)
from mmd.security import hash_password             # noqa: E402


def database():
    db = Session(create_engine("sqlite://"), expire_on_commit=False)
    Base.metadata.create_all(db.bind)
    admin = User(username="admin-user", phone="09120000000",
                 password_hash=hash_password("x"), is_admin=True,
                 status=UserStatus.APPROVED)
    user = User(username="customer", phone="09395382065",
                password_hash=hash_password("x"), status=UserStatus.PENDING)
    db.add_all([admin, user]); db.commit()
    db.add_all([CreditAccount(user_id=admin.id), CreditAccount(user_id=user.id)])
    # Both have opened the bot: delivery is only possible for a linked number,
    # so a fixture without this would be exercising the unlinked path by
    # accident and every send test would pass for the wrong reason.
    db.add_all([BaleContact(phone=admin.phone, chat_id=1000000001),
                BaleContact(phone=user.phone, chat_id=1000000002)])
    db.commit()
    return db, admin, user


# --- cost -----------------------------------------------------------------
SAMPLE = {"code": "12345", "balance": "250,000", "increased": True,
          "free": "6.2"}


def test_every_template_bills_as_one_segment():
    """Persian and emoji are both UCS-2, so a segment is 70 UTF-16 code units.
    Measured in the wrong unit this passes and the bill silently doubles: a
    73-unit draft was billed 3808 where 70 would have been half."""
    for kind, tpl in smslib.CATALOGUE.items():
        body = tpl.build(SAMPLE)
        assert smslib.segments(body) == 1, (
            f"{kind} is {smslib.units(body)} units")


def test_segments_are_counted_in_utf16_not_python_characters():
    """A single emoji is one Python character and two UTF-16 units. Counting
    characters under-counts every message that carries one."""
    assert smslib.units("🔴") == 2 and len("🔴") == 1
    assert smslib.segments("🔴" * 35) == 1
    assert smslib.segments("🔴" * 36) == 2


def test_no_line_mixes_persian_and_latin_letters():
    """A bare `mmd-ai.ir` inside a Persian sentence jumps to the wrong end of
    the line in an RTL client, so scripts get their own lines."""
    import re
    fa, en = re.compile(r"[؀-ۿ]"), re.compile(r"[A-Za-z]")
    for kind, tpl in smslib.CATALOGUE.items():
        for line in tpl.build(SAMPLE).split("\n"):
            assert not (fa.search(line) and en.search(line)), f"{kind}: {line}"


def test_the_approval_message_is_persian_and_says_what_happened():
    body = smslib.body_for("approved")
    assert "تأیید" in body
    assert "mmd-ai.ir" in body


# --- the outbox -----------------------------------------------------------
def test_approval_queues_a_message_without_sending_it(monkeypatch):
    """The API process has no provider key by design, so approving must not
    reach the network at all."""
    db, admin, user = database()

    def explode(*_a, **_k):
        raise AssertionError("the API process must not send SMS itself")

    monkeypatch.setattr(smslib.balelib, "send", explode)
    assert appmod.admin_approve(user.id, admin, db)["status"] == "approved"

    row = db.scalar(select(SmsMessage))
    assert row.status == "queued" and row.kind == "approved"
    assert row.phone == "09395382065" and row.user_id == user.id


def test_re_approving_does_not_text_the_customer_twice():
    """Approval is deliberately idempotent so it can repair old rows; the
    message must not be."""
    db, admin, user = database()
    appmod.admin_approve(user.id, admin, db)
    appmod.admin_approve(user.id, admin, db)
    assert len(list(db.scalars(select(SmsMessage)))) == 1


def test_an_unsendable_number_still_approves_the_account():
    db, admin, user = database()
    user.phone = "not-a-phone"
    db.commit()
    assert appmod.admin_approve(user.id, admin, db)["status"] == "approved"
    assert db.get(User, user.id).status is UserStatus.APPROVED
    assert db.scalar(select(SmsMessage)) is None


# --- delivery -------------------------------------------------------------
def test_a_sent_message_records_the_provider_id(monkeypatch):
    db, admin, user = database()
    appmod.admin_approve(user.id, admin, db)
    monkeypatch.setattr(smslib.balelib, "send", lambda *_a, **_k: "564905598")
    row = smslib.due(db)[0]
    assert smslib.deliver(db, row) is True
    assert row.status == "sent" and row.provider_message_id == "564905598"
    assert row.sent_at is not None
    assert smslib.due(db) == []


def test_a_failed_send_is_retried_with_backoff_then_given_up(monkeypatch):
    db, admin, user = database()
    appmod.admin_approve(user.id, admin, db)

    def refuse(*_a, **_k):
        raise smslib.SmsError("kavenegar refused: 411 receptor is invalid")

    monkeypatch.setattr(smslib.balelib, "send", refuse)
    row = smslib.due(db)[0]

    smslib.deliver(db, row)
    assert row.status == "queued" and row.attempts == 1
    # Backed off, so the next tick does not immediately hammer the provider.
    assert smslib.due(db) == []
    assert row.next_attempt_at > datetime.now(UTC)
    assert "411" in row.error

    for _ in range(smslib.MAX_ATTEMPTS - 1):
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        smslib.deliver(db, smslib.due(db)[0])
    assert row.status == "failed" and row.attempts == smslib.MAX_ATTEMPTS
    # Given up: a permanently bad number stops spending credit.
    row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    assert smslib.due(db) == []


def test_a_200_body_that_reports_failure_is_a_failure(monkeypatch):
    """Kavenegar reports a refusal inside a 200 body as often as by status
    code, so the envelope decides - not r.status_code."""
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"return": {"status": 411, "message": "invalid receptor"}}

    monkeypatch.setattr(smslib.httpx, "post", lambda *_a, **_k: Response())
    with pytest.raises(smslib.SmsError, match="411"):
        smslib.send("09395382065", "x", key="k")


# --- the credential -------------------------------------------------------
def test_the_provider_key_is_delivered_to_the_worker_only():
    root = Path(__file__).resolve().parents[1]
    worker = (root / "deploy" / "mmd-worker.service").read_text()
    api = (root / "deploy" / "mmd-api.service").read_text()
    assert "LoadCredential=kavenegar:/etc/mmd/kavenegar.key" in worker
    # Both units run as the same user, so anything mmd-api can read it holds.
    assert "kavenegar" not in api


def test_only_the_worker_ever_calls_send():
    root = Path(__file__).resolve().parents[1]
    assert "smslib.send" not in (root / "control" / "mmd" / "app.py").read_text()
    assert "sms_once" in (root / "control" / "mmd" / "worker.py").read_text()


# --- rejection ------------------------------------------------------------
def test_rejection_texts_the_applicant_once():
    db, admin, user = database()
    appmod.admin_reject(user.id, admin, db)
    rows = list(db.scalars(select(SmsMessage)))
    assert len(rows) == 1 and rows[0].kind == "rejected"
    appmod.admin_reject(user.id, admin, db)
    assert len(list(db.scalars(select(SmsMessage)))) == 1


# --- low credit -----------------------------------------------------------
def _low(db, user_id):
    return [r for r in db.scalars(select(SmsMessage))
            if r.kind == "low_credit" and r.user_id == user_id]


def _balance(monkeypatch, worker, toman):
    from mmd.billing.pricing import MICRO
    monkeypatch.setattr(worker.svc, "balance_micro",
                        lambda _db, _uid: int(toman * MICRO))


def test_low_credit_texts_once_per_crossing_not_once_per_pass(monkeypatch):
    """A customer sitting below the threshold must be told once, not on every
    pass for as long as they stay there."""
    from mmd import worker
    db, _admin, user = database()
    user.status = UserStatus.APPROVED
    db.commit()

    monkeypatch.setattr(worker, "SessionLocal", lambda: _NoClose(db))
    _balance(monkeypatch, worker, 40_000)

    worker.low_credit_once()
    worker.low_credit_once()
    worker.low_credit_once()
    rows = _low(db, user.id)
    assert len(rows) == 1


def test_topping_up_re_arms_the_warning(monkeypatch):
    from mmd import worker
    db, _admin, user = database()
    user.status = UserStatus.APPROVED
    db.commit()
    monkeypatch.setattr(worker, "SessionLocal", lambda: _NoClose(db))

    _balance(monkeypatch, worker, 40_000)
    worker.low_credit_once()
    _balance(monkeypatch, worker, 500_000)      # topped up: resolves
    worker.low_credit_once()
    _balance(monkeypatch, worker, 40_000)       # low again: a NEW crossing
    worker.low_credit_once()

    rows = _low(db, user.id)
    assert len(rows) == 2


def test_a_zero_balance_is_not_a_low_credit_warning(monkeypatch):
    """At zero the machine is already stopped and the key already blocked; a
    warning arriving then is a receipt, not a warning."""
    from mmd import worker
    db, _admin, user = database()
    user.status = UserStatus.APPROVED
    db.commit()
    monkeypatch.setattr(worker, "SessionLocal", lambda: _NoClose(db))
    _balance(monkeypatch, worker, 0)
    worker.low_credit_once()
    assert _low(db, user.id) == []


class _NoClose:
    """The worker opens `with SessionLocal() as db`, which would close the
    test's session on exit."""

    def __init__(self, db): self._db = db
    def __enter__(self): return self._db
    def __exit__(self, *_exc): return False


def test_credit_step_baselines_silently_then_reports_both_directions(monkeypatch):
    """Deployment history is silent; later band crossings state the direction."""
    from mmd import worker

    db, _admin, user = database()
    user.status = UserStatus.APPROVED
    user.sms_credit_step_toman = 50_000
    db.commit()
    monkeypatch.setattr(worker, "SessionLocal", lambda: _NoClose(db))

    _balance(monkeypatch, worker, 126_000)
    worker.credit_step_once()
    assert [r for r in db.scalars(select(SmsMessage))
            if r.kind == "credit_step"] == []
    assert user.sms_credit_band == 2

    _balance(monkeypatch, worker, 124_000)  # same band: no message
    worker.credit_step_once()
    _balance(monkeypatch, worker, 99_000)   # band 2 -> 1
    worker.credit_step_once()
    _balance(monkeypatch, worker, 151_000)  # band 1 -> 3
    worker.credit_step_once()

    rows = [r for r in db.scalars(select(SmsMessage))
            if r.kind == "credit_step" and r.user_id == user.id]
    assert len(rows) == 2
    assert "کاهش" in rows[0].body and "99,000" in rows[0].body
    assert "افزایش" in rows[1].body and "151,000" in rows[1].body


def test_credit_step_is_configured_independently_per_customer(monkeypatch):
    from mmd import worker

    db, _admin, first = database()
    first.status = UserStatus.APPROVED
    first.sms_credit_step_toman = 50_000
    first.sms_credit_band = 1
    second = User(username="second", phone="09121112222",
                  password_hash=hash_password("x"), status=UserStatus.APPROVED,
                  sms_credit_step_toman=100_000, sms_credit_band=0)
    db.add(second); db.commit()
    db.add(CreditAccount(user_id=second.id)); db.commit()
    monkeypatch.setattr(worker, "SessionLocal", lambda: _NoClose(db))
    monkeypatch.setattr(worker.svc, "balance_micro",
                        lambda _db, uid: (110_000 if uid == first.id else 60_000)
                        * 1_000_000)

    worker.credit_step_once()
    rows = [r for r in db.scalars(select(SmsMessage)) if r.kind == "credit_step"]
    assert [r.user_id for r in rows] == [first.id]


# --- unrestricted delivery ------------------------------------------------
def test_any_linked_customer_number_is_sent(monkeypatch):
    """No allowlist: whichever number a customer has, a linked one is sent to.

    Delivery addresses the CHAT, not the phone - the phone only selects which
    chat. Asserting on the chat id is what would catch a regression that sent
    every message to the same person.
    """
    db, admin, user = database()
    sent = []
    monkeypatch.setattr(smslib.balelib, "send",
                        lambda chat_id, *_a, **_k: sent.append(chat_id) or "1")

    user.phone = "09121112222"
    db.add(BaleContact(phone="09121112222", chat_id=1000000003))
    db.commit()
    appmod.admin_approve(user.id, admin, db)
    row = smslib.due(db)[0]
    assert smslib.deliver(db, smslib.due(db)[0]) is True
    assert sent == [1000000003]
    assert row.status == "sent" and row.attempts == 1


def test_a_number_that_never_opened_the_bot_is_parked_not_retried(monkeypatch):
    """The state SMS never had, and the one that will surprise an operator.

    Bale cannot message a number that has not written to the bot, so this is
    permanent, not transient. Retrying would spend the attempt budget to reach
    `failed`, burying "they never linked" under a generic error - and it must
    not be reported as sent, because nobody received anything.
    """
    db, admin, user = database()
    called = []
    monkeypatch.setattr(smslib.balelib, "send",
                        lambda *a, **k: called.append(a) or "1")

    user.phone = "09121113333"          # linked to nothing
    db.commit()
    appmod.admin_approve(user.id, admin, db)
    row = smslib.due(db)[0]

    assert smslib.deliver(db, row) is False
    assert row.status == "unlinked"
    assert row.attempts == 0, "an unlinked number must not spend a retry"
    assert called == [], "nothing may be sent for an unlinked number"
    assert smslib.due(db) == [], "and it must not come back round the queue"


# --- operator alerts --------------------------------------------------------
def test_an_overdue_ticket_texts_the_operator_once_a_day(monkeypatch):
    """A ticket still late tomorrow must not send a message every pass; an
    hourly reminder is how an operator learns to ignore the alert."""
    from datetime import timedelta
    from mmd import worker
    from mmd.models import Ticket, TicketStatus

    db, admin, _user = database()
    old = datetime.now(UTC) - timedelta(hours=30)
    db.add(Ticket(user_id=admin.id, subject="s", status=TicketStatus.OPEN,
                  created_at=old, updated_at=old))
    db.commit()
    monkeypatch.setattr(worker, "SessionLocal", lambda: _NoClose(db))
    monkeypatch.setattr(worker, "_prometheus_scalar", lambda _q: 0.0)

    worker.alerts_once()
    worker.alerts_once()
    sent = [r for r in db.scalars(select(SmsMessage))
            if r.kind == "admin_ticket_overdue"]
    assert len(sent) == 1


def test_a_ticket_inside_the_target_is_not_alerted(monkeypatch):
    from datetime import timedelta
    from mmd import worker
    from mmd.models import Ticket, TicketStatus

    db, admin, _user = database()
    recent = datetime.now(UTC) - timedelta(hours=2)
    db.add(Ticket(user_id=admin.id, subject="s", status=TicketStatus.OPEN,
                  created_at=recent, updated_at=recent))
    db.commit()
    monkeypatch.setattr(worker, "SessionLocal", lambda: _NoClose(db))
    monkeypatch.setattr(worker, "_prometheus_scalar", lambda _q: 0.0)
    worker.alerts_once()
    assert not [r for r in db.scalars(select(SmsMessage))
                if r.kind == "admin_ticket_overdue"]


def test_a_server_error_spike_texts_the_operator(monkeypatch):
    from mmd import worker
    db, _admin, _user = database()
    monkeypatch.setattr(worker, "SessionLocal", lambda: _NoClose(db))
    monkeypatch.setattr(worker, "_prometheus_scalar", lambda _q: 5.0)
    worker.alerts_once()
    assert [r for r in db.scalars(select(SmsMessage))
            if r.kind == "admin_error_rate"]


def test_prometheus_being_unreachable_is_not_itself_an_alert(monkeypatch):
    """Inventing an alarm from a failed query is how a monitoring outage
    becomes a paging storm."""
    from mmd import worker
    db, _admin, _user = database()
    monkeypatch.setattr(worker, "SessionLocal", lambda: _NoClose(db))
    monkeypatch.setattr(worker, "_prometheus_scalar", lambda _q: None)
    worker.alerts_once()
    assert not [r for r in db.scalars(select(SmsMessage))
                if r.kind == "admin_error_rate"]


# --- linking recovers what was parked ---------------------------------------
def test_linking_later_recovers_a_parked_message(monkeypatch):
    """Delivery parks a message when the number has never opened the bot.

    That is permanent until they do, so nothing retries it - and linking
    afterwards did not reconsider it. A customer who linked five minutes after
    signing up never received the alert that had been waiting for them.
    """
    db, admin, user = database()
    user.phone = "09121114444"                   # linked to nothing yet
    db.commit()
    appmod.admin_approve(user.id, admin, db)
    row = smslib.due(db)[0]
    assert smslib.deliver(db, row) is False and row.status == "unlinked"

    db.add(BaleContact(phone="09121114444", chat_id=1000000009))
    db.commit()
    assert smslib.requeue_after_linking(db) == 1
    assert row.status == "queued"
    assert smslib.due(db), "and it must be eligible for the next pass"


def test_a_stale_auth_code_is_not_resent(monkeypatch):
    """A code is valid for three minutes. Sending a stale one invites the
    customer to type something that will be refused, which is worse than
    sending nothing - the sign-in page already offers a resend."""
    db, admin, user = database()
    smslib.queue(db, user_id=user.id, phone=user.phone, kind="login_code",
                 detail={"code": "12345"})
    db.commit()
    row = smslib.due(db)[-1]
    row.status = "unlinked"
    db.commit()

    assert smslib.requeue_after_linking(db) == 0
    assert row.status == "unlinked"


def test_a_number_still_unlinked_stays_parked():
    db, admin, user = database()
    user.phone = "09121115555"
    db.commit()
    appmod.admin_approve(user.id, admin, db)
    smslib.deliver(db, smslib.due(db)[0])
    assert smslib.requeue_after_linking(db) == 0
