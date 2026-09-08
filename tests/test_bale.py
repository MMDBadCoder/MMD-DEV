"""Linking a Bale chat to a phone number, and what must not be linkable.

Bale replaced SMS because roughly half of everything sent was accepted,
charged, and then filtered by the operator before reaching the handset. The
trade is that a Bale bot cannot message a number unilaterally: someone has to
open the bot and share their contact first. These cover that exchange, which
is now the only way an account becomes reachable at all.
"""
import os

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

import pytest                                              # noqa: E402
from sqlalchemy import create_engine, select               # noqa: E402
from sqlalchemy.orm import Session                         # noqa: E402

from mmd import bale                                       # noqa: E402
from mmd.models import Base, BaleContact                   # noqa: E402


@pytest.fixture
def db():
    s = Session(create_engine("sqlite://"), expire_on_commit=False)
    Base.metadata.create_all(s.bind)
    yield s
    s.close()


@pytest.fixture
def quiet(monkeypatch):
    """Capture what the bot would say instead of calling Bale."""
    said = []
    monkeypatch.setattr(bale, "send", lambda chat, text, **k: said.append((chat, text)) or "1")
    monkeypatch.setattr(bale, "ask_for_contact",
                        lambda chat, text, **k: said.append((chat, text)) or "1")
    return said


# --- the number Bale reports ----------------------------------------------
@pytest.mark.parametrize("given", [
    "989395382065",      # what Bale actually sent, measured
    "+989395382065",
    "00989395382065",
    "09395382065",
    "9395382065",
])
def test_every_way_of_writing_one_number_normalises(given):
    """Bale reports the number the account registered with, not ours.

    The real payload arrived as `989395382065` - country code, no plus. A
    lookup that took it literally would find no account and refuse to link a
    perfectly good customer.
    """
    assert bale.normalise_phone(given) == "09395382065"


# --- linking ---------------------------------------------------------------
def test_sharing_a_contact_links_the_chat(db, quiet):
    ok = bale.link(db, 555, {"phone_number": "989395382065",
                             "first_name": "Mohammad", "user_id": 555},
                   555, "private")
    assert ok is True
    row = db.scalar(select(BaleContact))
    assert (row.phone, row.chat_id) == ("09395382065", 555)
    assert bale.LINKED in [t for _c, t in quiet]


def test_a_forwarded_contact_is_refused(db, quiet):
    """The check that stops one customer receiving another's codes.

    Bale, like Telegram, lets anyone forward a saved contact card. Without
    this, a customer could link somebody else's number to their own chat and
    every verification code for that number would arrive in their bot.
    """
    ok = bale.link(db, 555, {"phone_number": "989121112222",
                             "first_name": "Someone", "user_id": 999}, 555)
    assert ok is False
    assert db.scalar(select(BaleContact)) is None
    assert bale.WRONG_CONTACT in [t for _c, t in quiet]


def test_a_contact_with_no_owner_id_is_refused(db, quiet):
    """The hole the first version left, and the one the tests missed.

    It rejected only when both ids were present AND differed - so a forwarded
    card carrying no `user_id` at all was accepted, linking whatever number it
    named to whoever forwarded it. That is enough to redirect another
    customer's verification and recovery codes to your own chat.
    """
    ok = bale.link(db, 555, {"phone_number": "09121112222"}, 555, "private")
    assert ok is False
    assert db.scalar(select(BaleContact)) is None


def test_linking_is_refused_without_a_known_sender(db, quiet):
    """No sender id is no proof either: the check needs both sides."""
    assert bale.link(db, 555, {"phone_number": "09121112222", "user_id": 555},
                     None, "private") is False
    assert db.scalar(select(BaleContact)) is None


def test_a_group_chat_cannot_be_linked(db, quiet):
    """A bot added to a group reports the GROUP as the chat. Linking one
    delivers every future code to everybody in it."""
    for kind in ("group", "supergroup", "channel"):
        assert bale.link(db, 900, {"phone_number": "09395382065", "user_id": 900},
                         900, kind) is False
    assert db.scalar(select(BaleContact)) is None


def test_a_group_message_never_reaches_link(db, quiet, monkeypatch):
    """The type has to survive the trip from the update to the check."""
    seen = {}
    monkeypatch.setattr(bale, "link",
                        lambda *a, **k: seen.setdefault("chat_type", a[4]))
    bale.handle(db, {"update_id": 1, "message": {
        "chat": {"id": 900, "type": "supergroup"}, "from": {"id": 900},
        "contact": {"phone_number": "09395382065", "user_id": 900}}})
    assert seen["chat_type"] == "supergroup"


def test_a_number_that_is_not_an_iranian_mobile_is_refused(db, quiet):
    assert bale.link(db, 555, {"phone_number": "12125551234", "user_id": 555}, 555) is False
    assert db.scalar(select(BaleContact)) is None


def test_relinking_moves_the_link_rather_than_duplicating(db, quiet):
    """Both directions, because a stale row means messages to a dead chat."""
    bale.link(db, 555, {"phone_number": "09395382065", "user_id": 555}, 555)
    # Same person, new Bale account.
    bale.link(db, 777, {"phone_number": "09395382065", "user_id": 777}, 777)
    rows = list(db.scalars(select(BaleContact)))
    assert len(rows) == 1 and rows[0].chat_id == 777

    # Same Bale account, different number.
    bale.link(db, 777, {"phone_number": "09121112222", "user_id": 777}, 777)
    rows = list(db.scalars(select(BaleContact)))
    assert len(rows) == 1 and rows[0].phone == "09121112222"


# --- the poll --------------------------------------------------------------
def test_start_offers_the_button(db, quiet, monkeypatch):
    monkeypatch.setattr(bale, "configured", lambda: True)
    monkeypatch.setattr(bale, "updates", lambda **k: [
        {"update_id": 7, "message": {"chat": {"id": 42}, "from": {"id": 42},
                                     "text": "/start"}}])
    assert bale.poll(db) == 1
    assert quiet == [(42, bale.WELCOME)]


def test_the_offset_advances_so_updates_are_not_replayed(db, quiet, monkeypatch):
    """Without this the bot answers the same /start on every tick, forever."""
    monkeypatch.setattr(bale, "configured", lambda: True)
    seen = {}

    def fake_updates(offset=0, **k):
        seen["offset"] = offset
        return [{"update_id": 12, "message": {"chat": {"id": 42},
                                              "from": {"id": 42}, "text": "hi"}}]

    monkeypatch.setattr(bale, "updates", fake_updates)
    bale.poll(db)
    assert seen["offset"] == 0
    bale.poll(db)
    assert seen["offset"] == 13, "the next poll must acknowledge update 12"


def test_one_bad_update_does_not_stop_the_batch(db, quiet, monkeypatch):
    """A malformed update must not wedge every later link behind it."""
    monkeypatch.setattr(bale, "configured", lambda: True)
    monkeypatch.setattr(bale, "updates", lambda **k: [
        {"update_id": 1, "message": {}},                       # no chat
        {"update_id": 2, "message": {"chat": {"id": 42}, "from": {"id": 42},
                                     "contact": {"phone_number": "09395382065",
                                                 "user_id": 42}}},
    ])
    assert bale.poll(db) == 2
    assert db.scalar(select(BaleContact)).chat_id == 42


def test_the_bot_token_is_never_logged():
    """httpx logs full request URLs at INFO, and the token is IN the path.

    Left alone this writes a live credential into the journal on every poll -
    every ten seconds. Pinned as a test rather than a comment because it is
    invisible in normal use: nothing fails, the secret just accumulates in a
    log somebody will later paste into a bug report.
    """
    from pathlib import Path
    # Every process that sends through Bale, not just the two that were
    # noticed first. The watchdog was missed and logged the token for days.
    for name in ("worker.py", "app.py", "watchdog.py"):
        src = Path("control/mmd") .joinpath(name).read_text(encoding="utf-8")
        assert 'logging.getLogger("httpx").setLevel(logging.WARNING)' in src, (
            f"{name} lets httpx log request URLs, which contain the bot token")


def test_deleting_an_account_removes_its_bale_link(db, quiet):
    """Both are keyed by phone, so neither is reached by a user_id sweep.

    Left behind, the link keeps delivering to that chat - so somebody who
    later registers the same number inherits a stranger's delivery mapping and
    receives their codes. The message log holds the number and every body ever
    sent to it, which is not erasure either.
    """
    from pathlib import Path
    src = Path("control/mmd/worker.py").read_text(encoding="utf-8")
    at = src.index("def _purge_account")
    block = src[at:at + 4000]
    assert "delete(BaleContact).where(BaleContact.phone" in block, (
        "account deletion leaves the Bale link behind")
    assert "delete(SmsMessage).where(SmsMessage.phone" in block, (
        "account deletion leaves the message log behind")
