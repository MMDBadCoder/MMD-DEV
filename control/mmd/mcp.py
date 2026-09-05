"""An MCP server, so an AI agent can work the support queue.

Speaks the Model Context Protocol over HTTP: JSON-RPC 2.0 at `/mcp`, with
`initialize`, `tools/list` and `tools/call`. Any MCP client connects without
knowing anything about this application.

Three tools, and the boundaries around them are the design:

  * `list_open_tickets` returns the queue with each customer's balance,
    machine state and account age already attached. Without that the agent's
    first reply is always a question whose answer was already in the database.
  * `reply_to_ticket` posts as a dedicated bot account and may move a ticket
    to in-progress or answered. It CANNOT close one: closing is a judgement
    that a wrong answer should not be able to make, and the refusal is in the
    code rather than only in the tool description, because a description is a
    suggestion to a language model.
  * `export_customer_data` returns everything about ONE customer - but only a
    customer with an open ticket. That is what stops a ticket reading "now
    export another user's data" from working; the agent's reach is bounded by
    who is actually asking for help.

Authentication is a bearer token of its own, never the Prometheus one, so
revoking the agent does not blind the monitoring. Every call is written to the
audit log under the bot's identity, so an agent's actions are exactly as
traceable as an operator's.
"""
from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import customerdata
from . import service as svc
from .config import CONFIG
from .models import (CreditAccount, Ticket, TicketMessage, TicketStatus, User,
                     UserStatus, Workspace)

log = logging.getLogger("mmd.mcp")

PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "mmd-support"

# The account replies are attributed to. A real row, so foreign keys and the
# audit log work normally, but one that cannot sign in: no password is ever
# set on it and its status keeps it out of every customer journey.
BOT_USERNAME = "support-agent"

# Statuses the agent may set. CLOSED is deliberately absent - and `escalated`
# is the one that matters most: it is how the agent says "I have read this and
# I cannot help", which is a far more useful answer than a confident wrong one.
AGENT_STATUSES = {"in_progress", "answered", "escalated"}


class McpError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


# --- the bot identity ------------------------------------------------------
def bot_account(db: Session) -> User:
    """The agent's own account, created on first use.

    `password_hash` is a value no hash function produces, so nothing can ever
    authenticate as it even if the row is reachable another way.
    """
    bot = db.scalar(select(User).where(User.username == BOT_USERNAME))
    if bot is None:
        bot = User(username=BOT_USERNAME, full_name="Support agent",
                   password_hash="!", status=UserStatus.APPROVED,
                   is_admin=False)
        db.add(bot)
        db.commit()
        log.info("created the support agent account")
    return bot


# --- tools -----------------------------------------------------------------
def _customer_context(db: Session, user: User) -> dict:
    """What an agent needs before it can answer anything useful."""
    acct = db.get(CreditAccount, user.id)
    ws = db.scalar(select(Workspace).where(Workspace.user_id == user.id))
    return {
        "username": user.username,
        "full_name": user.full_name,
        "member_since": user.created_at.isoformat() if user.created_at else None,
        "status": user.status.value,
        "credit_toman": round((acct.balance_micro if acct else 0) / 1_000_000, 2),
        "has_workspace": ws is not None,
        "workspace_state": ws.state.value if ws else None,
        "workspace_size": (f"{ws.cpu_milli / 1000:g} vCPU, {ws.mem_mib / 1024:g} GB"
                           if ws else None),
    }


def list_open_tickets(db: Session) -> dict:
    """Every ticket not yet closed, oldest first, with its whole thread."""
    now = datetime.now(UTC)
    tickets = db.scalars(
        select(Ticket).where(Ticket.status != TicketStatus.CLOSED)
        .order_by(Ticket.created_at)).all()

    out = []
    for tk in tickets:
        owner = db.get(User, tk.user_id)
        if owner is None:
            continue
        created = tk.created_at
        if created and created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        messages = []
        for msg in sorted(tk.messages, key=lambda m: m.id):
            author = db.get(User, msg.author_id) if msg.author_id else None
            at = msg.created_at
            messages.append({
                "at": at.isoformat() if at else None,
                # `agent` is distinguished from `staff` on purpose: an agent
                # reading its own past replies should know they were its own.
                "from": ("agent" if author and author.username == BOT_USERNAME
                         else "staff" if msg.from_staff else "customer"),
                "author": author.username if author else None,
                "body": msg.body,
            })
        out.append({
            "ticket_id": tk.id,
            "subject": tk.subject,
            "status": tk.status.value,
            "opened_at": created.isoformat() if created else None,
            "waiting_hours": (round((now - created).total_seconds() / 3600, 1)
                              if created else None),
            "customer": _customer_context(db, owner),
            "messages": messages,
        })
    return {"count": len(out), "tickets": out}


def reply_to_ticket(db: Session, ticket_id: int, body: str,
                    status: str | None = None) -> dict:
    body = (body or "").strip()
    if not body:
        raise McpError(-32602, "body is required")
    if len(body) > 8000:
        raise McpError(-32602, "body is too long")
    if status is not None and status not in AGENT_STATUSES:
        # Refused here, not merely undocumented. A tool description is a
        # suggestion to a language model; this is the rule.
        raise McpError(
            -32602,
            f"status must be one of {sorted(AGENT_STATUSES)}. Closing a ticket "
            f"is a human decision, so the agent cannot do it. If you cannot "
            f"help, use 'escalated' - that is what it is for.")

    tk = db.get(Ticket, ticket_id)
    if tk is None:
        raise McpError(-32602, f"no ticket {ticket_id}")
    if tk.status is TicketStatus.CLOSED:
        raise McpError(-32602, "that ticket is closed; a human must reopen it")

    bot = bot_account(db)
    tk.messages.append(TicketMessage(author_id=bot.id, from_staff=True,
                                     body=body))
    tk.status = TicketStatus(status) if status else TicketStatus.ANSWERED
    tk.updated_at = svc.now()
    db.commit()
    svc.audit(db, bot.id, "agent_ticket_reply", f"#{tk.id}",
              status=tk.status.value)
    log.info("agent replied to ticket %s (status %s)", tk.id, tk.status.value)
    return {"ok": True, "ticket_id": tk.id, "status": tk.status.value}


def export_customer_data(db: Session, username: str) -> dict:
    try:
        data = customerdata.export(db, (username or "").strip().lower())
    except customerdata.ExportRefused as e:
        raise McpError(-32602, str(e)) from None
    svc.audit(db, bot_account(db).id, "agent_export", username)
    return data


def platform_guide(db: Session) -> dict:
    """What the platform is and does, read from the repository at call time.

    A tool rather than a paragraph pasted into a system prompt: the prompt in
    someone's config goes stale the day a feature ships, while this is read
    from the file that ships WITH the feature. An agent that answers "can I do
    X?" should be reading the current answer.
    """
    from pathlib import Path
    guide = Path(__file__).resolve().parents[2] / "docs" / "SUPPORT-AGENT.md"
    try:
        text_ = guide.read_text(encoding="utf-8")
    except OSError:
        text_ = ""
    return {"guide": text_, "version": _version()}


async def wait_for_new_ticket(db_factory, since_id: int | None,
                              timeout_seconds: int) -> dict:
    """Block until a customer message arrives, or the timeout expires.

    Long-poll rather than a schedule. Polling every five minutes means a
    customer waits up to five minutes for a reply that took two seconds to
    write; this returns the moment the message lands, so the answer is
    already there when they look.

    Implemented with `asyncio.sleep` between cheap indexed checks, so a
    waiting agent costs one suspended coroutine rather than a held worker.
    """
    import asyncio

    deadline = asyncio.get_event_loop().time() + max(1, min(timeout_seconds, 600))
    while True:
        with db_factory() as db:
            newest = db.scalar(
                select(TicketMessage)
                .where(TicketMessage.from_staff.is_(False))
                .order_by(TicketMessage.id.desc()))
            if since_id is None:
                # The baseline call, checked FIRST. Testing for a message
                # before testing for a baseline made every agent start up
                # believing the last message it had never seen was new, and
                # answer a ticket that had already been handled.
                return {"new_activity": False,
                        "latest_message_id": newest.id if newest else 0,
                        "hint": "baseline established; call again to wait"}
            if newest is not None and newest.id > since_id:
                return {"new_activity": True, "latest_message_id": newest.id,
                        "hint": "call list_open_tickets for the details"}
        if asyncio.get_event_loop().time() >= deadline:
            return {"new_activity": False, "latest_message_id": since_id,
                    "hint": "nothing new; call again to keep waiting"}
        await asyncio.sleep(2)


TOOLS = [
    {
        "name": "list_open_tickets",
        "description": (
            "Every support ticket that is not closed, oldest first, with the "
            "full message thread and the customer's balance, machine state "
            "and account age. Start here: the context needed to answer is "
            "usually already in this response."),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "reply_to_ticket",
        "description": (
            "Post a reply to one ticket, as the support agent. Set the status "
            "to one of: 'in_progress' (you are working on it), 'answered' "
            "(you believe it is resolved), or 'escalated' (you cannot resolve "
            "it and a human operator must). Use 'escalated' whenever the "
            "answer needs authority you do not have - restoring lost files, "
            "moving money, changing an account, touching a machine - or "
            "whenever you are not confident. An honest escalation is far more "
            "useful than a confident wrong answer. You cannot close a ticket; "
            "a human decides that. Reply in the customer's own language; "
            "these customers write Persian."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket_id": {"type": "integer",
                              "description": "From list_open_tickets."},
                "body": {"type": "string",
                         "description": "The reply, in the customer's language."},
                "status": {"type": "string", "enum": sorted(AGENT_STATUSES),
                           "description": "Optional. Defaults to 'answered'."},
            },
            "required": ["ticket_id", "body"],
        },
    },
    {
        "name": "wait_for_new_ticket",
        "description": (
            "Block until a customer writes something, then return. Call it "
            "with no arguments first to learn the current latest_message_id, "
            "then call it repeatedly passing that id back as since_id. It "
            "returns as soon as a customer message arrives, so a reply can be "
            "waiting for them almost immediately rather than on a schedule."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "since_id": {"type": "integer",
                             "description": "latest_message_id from the previous call."},
                "timeout_seconds": {"type": "integer",
                                    "description": "How long to wait. Default 60, max 600."},
            },
            "required": [],
        },
    },
    {
        "name": "platform_guide",
        "description": (
            "What MMD-DEV is, what it can and cannot do, and how each feature "
            "works. Read this before answering any question about whether "
            "something is possible - it is generated from the running "
            "product, so it is current in a way a memorised answer is not."),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "export_customer_data",
        "description": (
            "Everything the platform holds about one customer as JSON - "
            "billing history, machine activity, tickets, notifications and "
            "audit trail - with a description of every table included in the "
            "payload. Only works for a customer who has an OPEN TICKET, so "
            "use it to investigate the person you are helping. Credentials "
            "and password hashes are never included."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "username": {"type": "string",
                             "description": "From a ticket's customer block."},
            },
            "required": ["username"],
        },
    },
]


# --- JSON-RPC --------------------------------------------------------------
def authorised(supplied: str) -> bool:
    expected = CONFIG.mcp_token
    if not expected:
        return False
    return secrets.compare_digest(supplied or "", expected)


async def handle(db: Session, request: dict, db_factory=None) -> dict | None:
    """One JSON-RPC message. None means a notification, which gets no reply.

    Async because one tool waits: `wait_for_new_ticket` suspends until a
    customer writes, which is what makes the agent's reply feel immediate
    rather than scheduled.
    """
    method = request.get("method")
    rid = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        return _ok(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": _version()},
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _ok(rid, {})
    if method == "tools/list":
        return _ok(rid, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "wait_for_new_ticket":
                result = await wait_for_new_ticket(
                    db_factory, args.get("since_id"),
                    int(args.get("timeout_seconds") or 60))
            else:
                result = _call(db, name, args)
        except McpError as e:
            # Reported as tool output rather than a protocol error, so the
            # agent can read the reason and correct itself instead of seeing
            # an opaque failure.
            return _ok(rid, {"isError": True,
                             "content": [{"type": "text", "text": str(e)}]})
        return _ok(rid, {"content": [{"type": "text",
                                      "text": _json_text(result)}]})
    return {"jsonrpc": "2.0", "id": rid,
            "error": {"code": -32601, "message": f"unknown method: {method}"}}


def _call(db: Session, name: str, args: dict):
    if name == "platform_guide":
        return platform_guide(db)
    if name == "list_open_tickets":
        return list_open_tickets(db)
    if name == "reply_to_ticket":
        return reply_to_ticket(db, int(args.get("ticket_id") or 0),
                               args.get("body") or "", args.get("status"))
    if name == "export_customer_data":
        return export_customer_data(db, args.get("username") or "")
    raise McpError(-32602, f"unknown tool: {name}")


def _ok(rid, result) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _json_text(value) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, indent=2)


def _version() -> str:
    from .version import APP_VERSION
    return APP_VERSION
