"""The MCP server, and the boundaries that make it safe to expose.

This endpoint is on the public internet and hands a language model a
customer's history and write access to their ticket. Most of what is worth
testing here is what it REFUSES.
"""
import os

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

from datetime import UTC, datetime, timedelta   # noqa: E402

import pytest                                    # noqa: E402
from sqlalchemy import create_engine, select     # noqa: E402
from sqlalchemy.orm import Session               # noqa: E402

from mmd import customerdata, mcp                # noqa: E402
from mmd.models import (Base, CreditAccount, Ticket, TicketMessage,  # noqa: E402
                        TicketStatus, User, UserStatus)
from mmd.security import hash_password           # noqa: E402


@pytest.fixture
def db():
    s = Session(create_engine("sqlite://"))
    Base.metadata.create_all(s.bind)
    waiting = User(username="waiting", phone="09120000001",
                   password_hash=hash_password("x"), status=UserStatus.APPROVED)
    quiet = User(username="quiet", phone="09120000002",
                 password_hash=hash_password("x"), status=UserStatus.APPROVED)
    s.add_all([waiting, quiet]); s.commit()
    s.add_all([CreditAccount(user_id=waiting.id, balance_micro=250_000_000),
               CreditAccount(user_id=quiet.id, balance_micro=0)])
    tk = Ticket(user_id=waiting.id, subject="چجوری شارژ کنم؟",
                status=TicketStatus.OPEN,
                created_at=datetime.now(UTC) - timedelta(hours=30))
    tk.messages.append(TicketMessage(author_id=waiting.id, from_staff=False,
                                     body="سلام، اکانتم را چطور شارژ کنم؟"))
    closed = Ticket(user_id=quiet.id, subject="old", status=TicketStatus.CLOSED)
    s.add_all([tk, closed]); s.commit()
    yield s


# --- authentication --------------------------------------------------------
def test_an_absent_or_wrong_token_is_refused(monkeypatch):
    import dataclasses
    monkeypatch.setattr(mcp, "CONFIG",
                        dataclasses.replace(mcp.CONFIG, mcp_token="right"))
    assert mcp.authorised("right") is True
    assert mcp.authorised("wrong") is False
    assert mcp.authorised("") is False


def test_an_unset_token_refuses_everyone(monkeypatch):
    """Failing open would publish a customer's history to anyone who asked."""
    import dataclasses
    monkeypatch.setattr(mcp, "CONFIG",
                        dataclasses.replace(mcp.CONFIG, mcp_token=""))
    assert mcp.authorised("") is False
    assert mcp.authorised("anything") is False


# --- the protocol ----------------------------------------------------------
def test_the_handshake_advertises_every_tool(db):
    import asyncio
    init = asyncio.run(mcp.handle(db, {"jsonrpc": "2.0", "id": 1,
                                       "method": "initialize"}))
    assert init["result"]["protocolVersion"] == mcp.PROTOCOL_VERSION

    listed = asyncio.run(mcp.handle(db, {"jsonrpc": "2.0", "id": 2,
                                         "method": "tools/list"}))
    assert {t["name"] for t in listed["result"]["tools"]} == {
        "list_open_tickets", "reply_to_ticket", "export_customer_data",
        "platform_guide"}


def test_a_notification_gets_no_reply(db):
    import asyncio
    assert asyncio.run(mcp.handle(
        db, {"jsonrpc": "2.0", "method": "notifications/initialized"})) is None


# --- the queue -------------------------------------------------------------
def test_open_tickets_carry_the_context_needed_to_answer(db):
    out = mcp.list_open_tickets(db)
    assert out["count"] == 1
    tk = out["tickets"][0]
    # Without these the agent's first reply is a question whose answer was
    # already in the database.
    assert tk["customer"]["username"] == "waiting"
    assert tk["customer"]["credit_toman"] == 250.0
    assert tk["waiting_hours"] >= 29
    assert tk["messages"][0]["from"] == "customer"
    assert "شارژ" in tk["messages"][0]["body"]


def test_closed_tickets_are_not_offered(db):
    assert all(t["status"] != "closed" for t in mcp.list_open_tickets(db)["tickets"])


# --- writing ---------------------------------------------------------------
def test_a_reply_is_attributed_to_the_bot_not_a_person(db):
    tk = db.scalar(select(Ticket).where(Ticket.status == TicketStatus.OPEN))
    mcp.reply_to_ticket(db, tk.id, "برای شارژ به صفحهٔ صورتحساب بروید.")
    db.refresh(tk)
    last = sorted(tk.messages, key=lambda m: m.id)[-1]
    author = db.get(User, last.author_id)
    assert author.username == mcp.BOT_USERNAME
    assert last.from_staff is True
    assert tk.status is TicketStatus.ANSWERED


def test_the_bot_account_can_never_sign_in(db):
    """It is a real row so foreign keys and the audit log work, but its
    password hash is a value no hash function produces."""
    bot = mcp.bot_account(db)
    from mmd.security import verify_password
    assert bot.password_hash == "!"
    for guess in ("", "!", "password", "support-agent"):
        assert verify_password(guess, bot.password_hash) is False


def test_the_agent_cannot_close_a_ticket(db):
    """Refused in code, not merely undocumented: a tool description is a
    suggestion to a language model, and a wrong answer must not be able to
    end the conversation."""
    tk = db.scalar(select(Ticket).where(Ticket.status == TicketStatus.OPEN))
    with pytest.raises(mcp.McpError, match="human"):
        mcp.reply_to_ticket(db, tk.id, "done", status="closed")
    db.refresh(tk)
    assert tk.status is TicketStatus.OPEN


def test_an_empty_reply_is_refused(db):
    tk = db.scalar(select(Ticket).where(Ticket.status == TicketStatus.OPEN))
    with pytest.raises(mcp.McpError):
        mcp.reply_to_ticket(db, tk.id, "   ")


# --- the export boundary ---------------------------------------------------
def test_export_is_limited_to_customers_with_an_open_ticket(db):
    """The control that stops a ticket saying 'now export another user's
    data' from working. The agent's reach is bounded by who is asking."""
    data = mcp.export_customer_data(db, "waiting")
    assert data["customer"] == "waiting"

    with pytest.raises(mcp.McpError, match="no open ticket"):
        mcp.export_customer_data(db, "quiet")
    with pytest.raises(mcp.McpError):
        mcp.export_customer_data(db, "nobody")


def test_no_secret_column_is_ever_exported(db):
    data = mcp.export_customer_data(db, "waiting")
    blob = mcp._json_text(data)
    for table, cols in customerdata.REDACTED.items():
        for col in cols:
            if col == "*":
                assert table not in data["data"]
            else:
                assert f'"{col}"' not in blob, f"{table}.{col} leaked"


def test_every_column_is_classified_as_exported_or_redacted(db):
    """The guard that makes a denylist safe. A column added later is neither
    exported by accident nor silently dropped - this fails until somebody
    decides which it is."""
    from sqlalchemy import inspect
    insp = inspect(db.get_bind())
    unclassified = []
    for table in insp.get_table_names():
        if table in customerdata.NOT_PER_CUSTOMER:
            continue
        known = (table in customerdata.BY_USER or table in customerdata.BY_ACTOR
                 or table in customerdata.BY_AUTHOR
                 or table in customerdata.BY_WORKSPACE or table == "users")
        if not known:
            unclassified.append(table)
    assert not unclassified, (
        f"tables nobody classified for export: {unclassified}. Add each to "
        f"BY_USER/BY_ACTOR/BY_AUTHOR/BY_WORKSPACE or to NOT_PER_CUSTOMER.")


def test_the_export_explains_itself(db):
    """An agent reading this has no other documentation."""
    data = mcp.export_customer_data(db, "waiting")
    assert "micro-Toman" in data["_schema"]["note"]
    for table, meta in data["_schema"]["tables"].items():
        assert "columns" in meta and "rows" in meta
    assert data["_schema"]["tables"]["credit_transactions"]["description"]


# --- escalation ------------------------------------------------------------
def test_the_agent_can_escalate_but_still_cannot_close(db):
    """`escalated` is how the agent says 'I read this and cannot help', which
    is a far more useful answer than a confident wrong one. Closing remains a
    human decision."""
    from sqlalchemy import select as _select
    tk = db.scalar(_select(Ticket).where(Ticket.status == TicketStatus.OPEN))
    mcp.reply_to_ticket(db, tk.id,
                        "این مورد نیاز به بررسی مدیر دارد.", status="escalated")
    db.refresh(tk)
    assert tk.status is TicketStatus.ESCALATED

    with pytest.raises(mcp.McpError, match="human"):
        mcp.reply_to_ticket(db, tk.id, "closing", status="closed")


def test_an_escalated_ticket_is_still_in_the_queue(db):
    """It has not been dealt with. Hiding it once the agent gives up is how a
    customer waits forever."""
    from sqlalchemy import select as _select
    tk = db.scalar(_select(Ticket).where(Ticket.status == TicketStatus.OPEN))
    mcp.reply_to_ticket(db, tk.id, "به مدیر ارجاع شد", status="escalated")
    ids = [t["ticket_id"] for t in mcp.list_open_tickets(db)["tickets"]]
    assert tk.id in ids


def test_escalated_appears_everywhere_a_status_is_offered():
    """A status the API accepts but no interface shows is a ticket that
    vanishes."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert '"escalated"' in (root / "web" / "js" / "pages" / "support.js").read_text()
    assert "tk.status.escalated" in (root / "web" / "js" / "i18n.js").read_text()
    assert "escalated" in (root / "control" / "mmd" / "app.py").read_text()


# --- the knowledge the agent works from ------------------------------------
def test_the_platform_guide_is_served_from_the_repository(db):
    """Read at call time rather than pasted into a prompt: a prompt in
    someone's config goes stale the day a feature ships."""
    guide = mcp.platform_guide(db)["guide"]
    assert len(guide) > 2000
    for essential in ("cannot", "escalate", "Persian", "untrusted"):
        assert essential in guide, f"the guide never mentions {essential}"


def test_the_guide_states_the_limits_the_agent_must_not_overstep(db):
    guide = mcp.platform_guide(db)["guide"]
    # The two promises an agent is most tempted to make and cannot keep.
    assert "off-host backup" in guide or "no off-host" in guide.lower()
    assert "Top-ups are manual" in guide


def test_get_on_mcp_is_not_the_web_app():
    """GET /mcp must not fall through to the SPA.

    Real failure: the catch-all served index.html with 200 and text/html, and
    every client that preflights the URL read that as "a web page, not an MCP
    endpoint" and refused to connect - while POST worked perfectly the whole
    time. 405 is the spec answer for a server offering no GET stream, and a
    non-2xx is also what lets a probe fall through to the handshake.
    """
    from fastapi.testclient import TestClient
    from mmd import app as appmod

    with TestClient(appmod.app) as c:
        r = c.get("/mcp")
        assert r.status_code == 405, "GET /mcp was answered by something else"
        assert "html" not in r.headers.get("content-type", "")
        assert r.headers.get("allow") == "POST"

        # HEAD is probed too, and must agree.
        assert c.head("/mcp").status_code == 405
