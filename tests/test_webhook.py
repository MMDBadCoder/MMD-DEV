"""The ticket webhook, and what stops a stranger firing it.

The receiving endpoint is on the internet, so anything can POST to it. Three
things together make a forged call useless, and each is tested here because
"we sign it" is a claim, not a guarantee.
"""
import os

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

import json                                       # noqa: E402
import time                                       # noqa: E402

import pytest                                     # noqa: E402
from sqlalchemy import create_engine              # noqa: E402
from sqlalchemy.orm import Session                # noqa: E402

from mmd import webhook                           # noqa: E402
from mmd.models import Base                       # noqa: E402

SECRET = "a-long-shared-secret"


@pytest.fixture
def db():
    s = Session(create_engine("sqlite://"))
    Base.metadata.create_all(s.bind)
    yield s


# --- the signature ---------------------------------------------------------
def test_a_correctly_signed_call_verifies():
    body = b'{"event":"ticket_opened","ticket_id":7}'
    ts = str(int(time.time()))
    assert webhook.verify(SECRET, ts, body, webhook.sign(SECRET, ts, body))


def test_a_forged_call_is_refused():
    """Without the secret an attacker cannot produce the signature."""
    body = b'{"event":"ticket_opened","ticket_id":7}'
    ts = str(int(time.time()))
    assert not webhook.verify(SECRET, ts, body, "deadbeef")
    assert not webhook.verify(SECRET, ts, body, "")
    assert not webhook.verify(SECRET, ts, body,
                              webhook.sign("wrong-secret", ts, body))


def test_a_tampered_body_is_refused():
    """The signature covers the body, so changing the ticket id breaks it."""
    ts = str(int(time.time()))
    signature = webhook.sign(SECRET, ts, b'{"ticket_id":7}')
    assert not webhook.verify(SECRET, ts, b'{"ticket_id":8}', signature)


def test_a_captured_call_cannot_be_replayed_forever():
    """The timestamp is INSIDE the signed material. Signing the body alone
    would leave a captured request valid for all time."""
    body = b'{"event":"ticket_opened"}'
    old = str(int(time.time()) - webhook.MAX_AGE_SECONDS - 60)
    signature = webhook.sign(SECRET, old, body)
    # Genuinely signed, and still refused because it is stale.
    assert webhook.sign(SECRET, old, body) == signature
    assert not webhook.verify(SECRET, old, body, signature)


def test_the_timestamp_cannot_be_moved_without_breaking_the_signature():
    body = b'{"event":"ticket_opened"}'
    old = str(int(time.time()) - 1000)
    signature = webhook.sign(SECRET, old, body)
    fresh = str(int(time.time()))
    assert not webhook.verify(SECRET, fresh, body, signature)


def test_a_malformed_timestamp_is_refused_not_crashed():
    body = b"{}"
    for bad in ("", "soon", None, "NaN"):
        assert webhook.verify(SECRET, bad, body, "x") is False


# --- the payload -----------------------------------------------------------
def test_the_payload_carries_no_instruction_and_no_customer_text():
    """The signature keeps strangers out; this makes getting in worthless. A
    forged call can only make the agent look at a ticket - which it may do at
    any time anyway - and ticket text reaches the model through the MCP tools,
    where it has already been labelled untrusted."""
    payload = webhook.build("ticket_opened", 7, "ali", "کمک می‌خواهم")
    blob = json.dumps(payload, ensure_ascii=False)
    assert payload["ticket_id"] == 7
    # The subject is a label for a human reading logs, never the ticket body.
    assert "hint" in payload
    assert len(blob) < 500, "the payload is carrying more than a nudge"


# --- configuration ---------------------------------------------------------
def test_an_empty_url_turns_it_off_rather_than_erroring(db):
    """A supported state: the agent falls back to polling and nothing breaks."""
    webhook.save(db, "", "s")
    db.commit()
    assert webhook.config(db)["enabled"] is False
    assert webhook.deliver(db, {"event": "x"})["sent"] is False


def test_a_url_without_a_scheme_is_refused(db):
    with pytest.raises(ValueError):
        webhook.save(db, "127.0.0.1:8644/webhooks/x", "s")


def test_the_secret_is_never_returned_in_full(db):
    webhook.save(db, "http://127.0.0.1:8644/webhooks/t", SECRET)
    db.commit()
    cfg = webhook.config(db)
    assert cfg["secret_set"] is True
    assert SECRET not in json.dumps(cfg)
    assert cfg["secret_hint"] == SECRET[-4:]


def test_saving_without_a_secret_keeps_the_stored_one(db):
    """Otherwise changing only the URL silently unsigns every future call."""
    webhook.save(db, "http://127.0.0.1:8644/webhooks/t", SECRET)
    db.commit()
    webhook.save(db, "http://127.0.0.1:8644/webhooks/other", None)
    db.commit()
    assert webhook._get(db, webhook.SETTING_SECRET) == SECRET


def test_delivery_failure_never_raises(db, monkeypatch):
    """A ticket must not fail to be created because an agent is down."""
    import httpx
    webhook.save(db, "http://127.0.0.1:9/webhooks/nothing", SECRET)
    db.commit()

    def boom(*_a, **_kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(webhook.httpx, "post", boom)
    out = webhook.deliver(db, webhook.build("ticket_opened", 1, "ali", "s"))
    assert out["sent"] is False and "unreachable" in out["reason"]


def test_a_test_call_does_not_save_anything(db, monkeypatch):
    """An operator should not have to store a wrong address to find out it is
    wrong."""
    class R:
        status_code = 200
        text = "ok"
    monkeypatch.setattr(webhook.httpx, "post", lambda *_a, **_kw: R())
    out = webhook.test(db, "http://127.0.0.1:8644/webhooks/probe", SECRET)
    assert out["sent"] is True
    assert webhook.config(db)["url"] == ""


def test_the_signature_headers_a_receiver_can_check(db, monkeypatch):
    """Two header formats are sent: our own, and GitHub's, because several
    agents - Hermes among them - already verify that one."""
    seen = {}

    class R:
        status_code = 200
        text = ""

    def capture(url, content=None, headers=None, timeout=None):
        seen.update(headers or {})
        seen["_body"] = content
        return R()

    monkeypatch.setattr(webhook.httpx, "post", capture)
    webhook.save(db, "http://127.0.0.1:8644/webhooks/t", SECRET)
    db.commit()
    webhook.deliver(db, webhook.build("ticket_opened", 3, "ali", "s"))

    expected = webhook.sign(SECRET, seen["X-MMD-Timestamp"], seen["_body"])
    assert seen["X-MMD-Signature"] == expected
    assert seen["X-MMD-Event"] == "ticket_opened"

    # The same digest under the generic name Hermes verifies, with the
    # timestamp it needs to enforce its own replay window.
    assert seen["X-Webhook-Signature-V2"] == expected
    assert seen["X-Webhook-Timestamp"] == seen["X-MMD-Timestamp"]

    # GitHub's header must NOT be sent. It signs the body alone, and a
    # receiver that understands it checks it before V2 and stops there -
    # so including it would quietly replace a replay-protected signature
    # with one that replays forever.
    assert "X-Hub-Signature-256" not in seen
