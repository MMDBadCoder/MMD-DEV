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
                 or table in customerdata.BY_WORKSPACE
                 or table in customerdata.BY_PHONE or table == "users")
        if not known:
            unclassified.append(table)
    assert not unclassified, (
        f"tables nobody classified for export: {unclassified}. Add each to "
        f"BY_USER/BY_ACTOR/BY_AUTHOR/BY_WORKSPACE/BY_PHONE or to "
        f"NOT_PER_CUSTOMER.")


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


def test_the_agent_is_handed_only_what_is_waiting_on_it(db):
    """`open` and nothing else.

    Every other status is somebody else's turn: `waiting_for_user` is the
    customer's, `answered` is a human's to confirm, `escalated` is a human's
    to handle. Handing those back invited the agent to reply over a
    colleague's escalation - which buries their queue and tells the customer
    they were helped when nobody had looked - or to nag a customer who owed
    the answer.
    """
    from sqlalchemy import select as _select
    ids = lambda: [t["ticket_id"] for t in mcp.list_open_tickets(db)["tickets"]]
    tk = db.scalar(_select(Ticket).where(Ticket.status == TicketStatus.OPEN))
    assert tk.id in ids(), "an open ticket must be handed to the agent"

    for status in ("waiting_for_user", "answered", "escalated"):
        tk.status = TicketStatus(status)
        db.commit()
        assert tk.id not in ids(), f"a {status} ticket must not come back"

    # But it is not lost: a human still sees it, and the customer writing
    # again puts it back in the agent's queue.
    tk.status = TicketStatus.OPEN
    db.commit()
    assert tk.id in ids()


def test_escalated_appears_everywhere_a_status_is_offered():
    """A status the API accepts but no interface shows is a ticket that
    vanishes."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert '"escalated"' in (root / "web" / "js" / "pages" / "support.js").read_text()
    assert "tk.status.escalated" in (root / "web" / "js" / "i18n.js").read_text()
    assert "escalated" in (root / "control" / "mmd" / "app.py").read_text()


# --- the knowledge the agent works from ------------------------------------
def test_the_guide_lists_the_real_project_documentation(db):
    """Served from the docs the team already keeps current.

    It used to be one curated file written for the agent. That has to be
    maintained beside the documentation it duplicates, and the day someone
    updates one and not the other the agent answers from the stale copy -
    confidently, because it cannot tell which it is holding.
    """
    listed = mcp.platform_guide(db)
    names = {d["name"] for d in listed["documents"]}
    assert {"ARCHITECTURE.md", "BILLING.md", "API.md"} <= names
    assert "document" not in listed, "the bare call must not return a whole file"
    # Each entry explains itself, so one call is enough to choose.
    assert all(d["first_line"] for d in listed["documents"])


def test_one_document_is_returned_by_name(db):
    got = mcp.platform_guide(db, "ARCHITECTURE.md")
    assert got["document"] == "ARCHITECTURE.md"
    assert len(got["text"]) > 2000
    assert got["truncated"] is False


def test_the_name_is_not_case_sensitive(db):
    """An agent that types the name from memory should still get the file."""
    assert mcp.platform_guide(db, "architecture.md")["document"] == "ARCHITECTURE.md"


def test_an_unknown_document_says_how_to_find_the_right_one(db):
    with pytest.raises(mcp.McpError) as e:
        mcp.platform_guide(db, "HANDBOOK.md")
    assert "platform_guide" in str(e.value), "the error must name the way out"


def test_internal_documents_are_not_offered(db):
    """DECISIONS.md is the one that matters here.

    It is 135 KB of design rationale, including how each security boundary
    works and why. Useful to an engineer; exactly the thing that must not be
    paraphrased into a reply to a stranger. The agent cannot judge which half
    of a sentence is safe to repeat, so it is not given the chance - and the
    same goes for the development, metrics and test documents, which answer no
    question a customer has ever asked.
    """
    names = {d["name"] for d in mcp.platform_guide(db)["documents"]}
    for internal in ("DECISIONS.md", "DEVELOPMENT.md", "METRICS.md",
                     "TEST-INVARIANTS.md", "DESIGN-SYSTEM.md"):
        assert internal not in names, f"{internal} is offered to the agent"

    # And not reachable by asking for it directly either.
    with pytest.raises(mcp.McpError):
        mcp.platform_guide(db, "DECISIONS.md")


def test_a_large_document_is_truncated_rather_than_failing(db, monkeypatch):
    """An oversized tool result is worse than a truncated one: it can fail the
    call outright and leave the agent with nothing."""
    monkeypatch.setattr(mcp, "AGENT_DOCS", ("docs/DECISIONS.md",))
    got = mcp.platform_guide(db, "DECISIONS.md")
    assert got["truncated"] is True
    assert 0 < len(got["text"]) <= 80_000


def test_the_answering_policy_lives_in_the_skill():
    """The rules an agent must not overstep are instructions, not product
    documentation - so they travel with the agent, not in the docs the
    platform_guide serves."""
    from pathlib import Path
    skill = Path("support-agent/SKILL.md").read_text(encoding="utf-8")
    for essential in ("Persian", "escalate", "untrusted", "off-host backup",
                      "Top-ups are manual"):
        assert essential.lower() in skill.lower(), (
            f"the skill never tells the agent about {essential}")


def test_the_skill_is_loadable_by_hermes():
    """Hermes probes each subdirectory of the skills path for SKILL.md, and
    reads `name` and `description` from its frontmatter. A skill missing
    either is discovered but never surfaced."""
    from pathlib import Path
    text = Path("support-agent/SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\n"), "no YAML frontmatter"
    front = text.split("---", 2)[1]
    assert "name: mmd-support" in front
    assert "description:" in front


def test_the_prompt_covers_running_unattended():
    """The one thing the skill cannot know: nobody is reading the transcript.

    Without this an agent asks a clarifying question, or narrates, or waits
    for more work - each of which burns a scheduled run and answers nobody.
    """
    from pathlib import Path
    prompt = Path("support-agent/system-prompt.md").read_text(encoding="utf-8")
    assert "mmd-support" in prompt, "the prompt never points at the skill"
    for essential in ("unattended", "stop", "nothing in the queue"):
        assert essential.lower() in prompt.lower(), (
            f"the prompt never mentions {essential}")


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


def test_the_agent_can_learn_where_to_click(db):
    """The commonest kind of ticket is "how do I do X in the panel".

    Every other document explains how the platform works; none described the
    screens, so an agent could say what was possible but never where the
    button was.
    """
    names = {d["name"] for d in mcp.platform_guide(db)["documents"]}
    assert "USER-GUIDE.md" in names

    guide = mcp.platform_guide(db, "USER-GUIDE.md")["text"]
    # The pages a customer actually asks about, by their real labels.
    for page in ("اتصال‌ها", "پورت‌ها", "صورتحساب", "منابع", "پیام‌ها"):
        assert page in guide, f"the guide never covers {page}"
    # And the three answers that account for most tickets.
    assert "0.0.0.0" in guide, "the commonest published-port mistake is missing"
    assert "off-host backup" in guide.lower() or "no off-host" in guide.lower()


def test_the_operator_runbook_is_not_given_to_the_agent(db):
    """OPERATIONS.md is host layout, deploys and provisioner commands. It
    invites the agent to describe internals, or to offer a customer an action
    only an operator can take."""
    names = {d["name"] for d in mcp.platform_guide(db)["documents"]}
    assert "OPERATIONS.md" not in names


def test_a_customer_reply_returns_the_ticket_to_the_agent(db):
    """The loop that makes `waiting_for_user` safe to set.

    The agent asks a question and steps back. Nothing else moves the ticket -
    so if a customer's reply did not return it to `open`, the conversation
    would stop there and the customer would wait forever for an agent that is
    no longer being handed the ticket.
    """
    from sqlalchemy import select as _select
    from mmd.models import TicketMessage

    tk = db.scalar(_select(Ticket).where(Ticket.status == TicketStatus.OPEN))
    mcp.reply_to_ticket(db, tk.id, "کدام نسخه را نصب کرده‌اید؟",
                        status="waiting_for_user")
    ids = lambda: [t["ticket_id"] for t in mcp.list_open_tickets(db)["tickets"]]
    assert tk.id not in ids(), "the agent must step back after asking"

    # What the customer's reply endpoint does, which is the only thing that
    # sets OPEN.
    tk.messages.append(TicketMessage(author_id=tk.user_id, from_staff=False,
                                     body="نسخهٔ ۲۴"))
    tk.status = TicketStatus.OPEN
    db.commit()
    assert tk.id in ids(), "their answer must hand it back"


def test_the_agent_cannot_open_or_close_a_ticket(db):
    """Closing would let it empty its own queue; `open` is the customer's word,
    set when they write. Both refused in code, not merely undocumented."""
    from sqlalchemy import select as _select
    tk = db.scalar(_select(Ticket).where(Ticket.status == TicketStatus.OPEN))
    for forbidden in ("closed", "open", "in_progress"):
        with pytest.raises(mcp.McpError):
            mcp.reply_to_ticket(db, tk.id, "x", status=forbidden)


def test_a_malformed_request_is_a_protocol_error_not_a_crash(db):
    """These arrive from a language model and from the open internet.

    `null`, a list of numbers, a non-object `params` - none has `.get`, so an
    AttributeError became a 500: the server looking broken for a request that
    was merely malformed, and telling the caller nothing it could act on.
    """
    import asyncio
    for bad in (None, [1, 2], "hello", 7):
        out = asyncio.run(mcp.handle(db, bad))
        assert out and "error" in out, f"{bad!r} did not produce a protocol error"
        assert out["error"]["code"] == -32600

    for bad_params in ("nope", [1], 3):
        out = asyncio.run(mcp.handle(db, {"jsonrpc": "2.0", "id": 1,
                                          "method": "tools/list",
                                          "params": bad_params}))
        assert out["error"]["code"] == -32602


def test_a_non_numeric_ticket_id_is_a_tool_error(db):
    """A model writes these arguments, so it will eventually write prose."""
    import asyncio
    out = asyncio.run(mcp.handle(db, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "reply_to_ticket",
                   "arguments": {"ticket_id": "the first one", "body": "x"}}}))
    text = out["result"]["content"][0]["text"]
    assert "ticket_id" in text, "the agent is not told which argument was wrong"


def test_the_bot_never_adopts_a_customer_account(db):
    """Reserved now, but an account registered before it was could still be
    here - and adopting it hands a customer the agent's identity."""
    from mmd.models import User, UserStatus
    from mmd.security import hash_password

    db.add(User(username=mcp.BOT_USERNAME, full_name="impostor",
                phone="09120009999", password_hash=hash_password("known"),
                status=UserStatus.APPROVED))
    db.commit()
    with pytest.raises(mcp.McpError):
        mcp.bot_account(db)


def test_the_bot_name_cannot_be_registered():
    from mmd import usernames
    for name in ("support-agent", "supportagent"):
        with pytest.raises(usernames.UsernameError):
            usernames.validate(name)


def test_an_agent_reply_tells_the_customer(db):
    """The path that answers fastest was the one that told them least.

    A human reply emitted an in-app notification and queued a message; the
    agent posted the reply and stopped. A customer who was not looking at the
    panel never learned they had been answered at all.
    """
    from sqlalchemy import select as _select
    from mmd.models import BaleContact, Notification, SmsMessage, User

    tk = db.scalar(_select(Ticket).where(Ticket.status == TicketStatus.OPEN))
    owner = db.get(User, tk.user_id)
    db.add(BaleContact(phone=owner.phone, chat_id=1000000123))
    db.commit()

    before_n = len(list(db.scalars(_select(Notification))))
    before_m = len(list(db.scalars(_select(SmsMessage))))
    mcp.reply_to_ticket(db, tk.id, "پاسخ داده شد", status="answered")

    notes = list(db.scalars(_select(Notification)))
    msgs = list(db.scalars(_select(SmsMessage)))
    assert len(notes) == before_n + 1, "no in-app notification for the customer"
    assert len(msgs) == before_m + 1, "nothing queued to reach them outside"
    assert msgs[-1].kind == "ticket_replied"
    assert notes[-1].user_id == tk.user_id


def test_announcing_the_same_reply_twice_is_one_announcement(db):
    """Keyed off the message id, so a retry is the same announcement."""
    from sqlalchemy import select as _select
    from mmd.models import Notification
    from mmd import service as svc

    tk = db.scalar(_select(Ticket).where(Ticket.status == TicketStatus.OPEN))
    mcp.reply_to_ticket(db, tk.id, "یک بار", status="answered")
    first = len(list(db.scalars(_select(Notification))))
    svc.announce_ticket_reply(db, tk)          # same last message
    assert len(list(db.scalars(_select(Notification)))) == first


def test_a_failed_announcement_does_not_undo_the_reply(db, monkeypatch):
    """A notification nobody received beats an answer that was rolled back."""
    from sqlalchemy import select as _select
    from mmd import notifications as notifylib

    monkeypatch.setattr(notifylib, "emit",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    tk = db.scalar(_select(Ticket).where(Ticket.status == TicketStatus.OPEN))
    before = len(tk.messages)
    out = mcp.reply_to_ticket(db, tk.id, "still posted", status="answered")
    assert out["ok"] is True
    db.refresh(tk)
    assert len(tk.messages) == before + 1, "the reply was rolled back"
