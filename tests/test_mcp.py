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
def test_the_handshake_advertises_the_three_tools(db):
    init = mcp.handle(db, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["protocolVersion"] == mcp.PROTOCOL_VERSION

    listed = mcp.handle(db, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert {t["name"] for t in listed["result"]["tools"]} == {
        "list_open_tickets", "reply_to_ticket", "export_customer_data"}


def test_a_notification_gets_no_reply(db):
    assert mcp.handle(db, {"jsonrpc": "2.0",
                           "method": "notifications/initialized"}) is None


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
