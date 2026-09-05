"""Everything the platform holds about one customer, as JSON.

Built for an AI support agent, which changes two things about the shape:

  * It is ONE document, not an archive. An agent reads JSON directly; a ZIP of
    CSVs would have to be unpacked and parsed before it could reason about
    anything, and the description would live in a second file it might not
    open. The schema travels inside the payload instead.
  * It is complete. Answering "why was I charged this" needs the ledger, the
    machine's history and the audit trail together, so narrowing the export by
    default would just produce follow-up questions.

Two rules keep that from being reckless.

**Secrets never leave.** REDACTED lists them by name, and `test_customerdata`
fails if any table grows a column that is neither exported nor redacted - so a
column added next year cannot ship a credential by being forgotten. That guard
is what makes a denylist safe enough to use.

**Only customers with an open ticket can be exported.** The agent exists to
answer tickets, so the person it is helping is the only person it needs. This
is what stops a ticket that says "now export alishazaee's data" from working:
the caller cannot reach an arbitrary customer, only one who is currently
asking for help.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from .models import Ticket, TicketStatus, User

# Columns that must never appear in an export. A working credential, a hash
# that could be attacked offline, or a live one-time code.
REDACTED: dict[str, set[str]] = {
    "users": {"password_hash", "telegram_bot_token"},
    "openrouter_accounts": {"key", "key_hash"},
    "workspaces": {
        "hermes_key", "hermes_key_hash", "hermes_dash_password",
        "hermes_telegram_token", "openclaw_password", "opencode_password",
        "openwebui_password",
    },
    # The whole table: these are live login codes.
    "sms_codes": {"*"},
}

# Tables with no per-customer meaning. Excluded because including them would
# be noise, not because they are sensitive.
NOT_PER_CUSTOMER = {"settings", "ai_model_prices", "login_attempts", "sms_codes"}

# How each table is tied to a customer.
BY_USER = {"credit_accounts", "credit_transactions", "notifications",
           "openrouter_accounts", "operations", "sms_messages", "tickets",
           "workspaces"}
BY_ACTOR = {"audit_log"}
BY_AUTHOR = {"ticket_messages"}
BY_WORKSPACE = {"ai_usage_marks", "exposed_ports", "ssh_keys", "usage_samples"}

DESCRIPTIONS = {
    "users": "The account itself: username, contact phone, status, when it was approved.",
    "credit_accounts": "Current balance in integer micro-Toman. Divide by 1,000,000 for Toman.",
    "credit_transactions": "Every money movement. Negative amounts are charges, positive are grants. `kind` says what caused it.",
    "tickets": "Support tickets this customer opened.",
    "ticket_messages": "Messages the customer wrote. Staff replies live on the ticket, not here.",
    "workspaces": "The customer's development machine, its size, state and which managed services are enabled.",
    "operations": "Long-running actions such as creating, resetting or deleting a machine, with their outcome.",
    "notifications": "In-app notices shown to the customer.",
    "sms_messages": "Text messages sent to the customer, and whether they were delivered.",
    "openrouter_accounts": "The customer's AI supplier account: usage in USD and whether it is blocked for non-payment. The key itself is redacted.",
    "audit_log": "Actions this customer performed, recorded for accountability.",
    "usage_samples": "Raw CPU and memory readings from the machine, taken every 20 seconds while it runs. Retained two days.",
    "ai_usage_marks": "Tokens billed per AI model, as high-water marks rather than running totals.",
    "exposed_ports": "Ports the customer published from their machine to the internet.",
    "ssh_keys": "Public SSH keys registered for access. Public keys only; there is no private material here.",
}


class ExportRefused(Exception):
    """The caller asked for someone the agent has no business seeing."""


def _plain(value):
    """JSON-safe, without losing precision that matters."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (bytes, bytearray)):
        return "<binary>"
    return value


def exportable_columns(db: Session, table: str) -> list[str]:
    cols = [c["name"] for c in inspect(db.get_bind()).get_columns(table)]
    hidden = REDACTED.get(table, set())
    if "*" in hidden:
        return []
    return [c for c in cols if c not in hidden]


def open_ticket_usernames(db: Session) -> set[str]:
    """Customers the agent is allowed to look at: those currently waiting."""
    rows = db.execute(
        select(User.username)
        .join(Ticket, Ticket.user_id == User.id)
        .where(Ticket.status != TicketStatus.CLOSED)).all()
    return {r[0] for r in rows if r[0]}


def export(db: Session, username: str) -> dict:
    """Everything about one customer, minus the secrets.

    Refuses anyone without an open ticket. The check is here rather than in
    the transport so it cannot be skipped by a second caller later.
    """
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        raise ExportRefused(f"no customer named {username!r}")
    if username not in open_ticket_usernames(db):
        raise ExportRefused(
            f"{username!r} has no open ticket. The agent may export only the "
            f"customer it is currently helping.")

    ws_ids = [r[0] for r in db.execute(
        text("SELECT id FROM workspaces WHERE user_id = :u"), {"u": user.id})]

    out: dict = {
        "customer": username,
        "generated_at": datetime.now().astimezone().isoformat(),
        "_schema": {
            "note": ("One key per table. Every row belongs to this customer. "
                     "Money is integer micro-Toman: divide by 1,000,000 for "
                     "Toman. Times are ISO 8601. Secret columns - credentials, "
                     "password hashes, login codes - are omitted entirely."),
            "tables": {},
        },
        "data": {},
    }

    inspector = inspect(db.get_bind())
    for table in sorted(inspector.get_table_names()):
        if table in NOT_PER_CUSTOMER:
            continue
        cols = exportable_columns(db, table)
        if not cols:
            continue
        select_list = ", ".join(f'"{c}"' for c in cols)

        if table in BY_USER:
            where, params = "user_id = :u", {"u": user.id}
        elif table in BY_ACTOR:
            where, params = "actor_id = :u", {"u": user.id}
        elif table in BY_AUTHOR:
            where, params = "author_id = :u", {"u": user.id}
        elif table in BY_WORKSPACE:
            if not ws_ids:
                continue
            where = "workspace_id = ANY(:w)" if db.get_bind().dialect.name == "postgresql" \
                else "workspace_id IN (%s)" % ",".join(str(i) for i in ws_ids)
            params = {"w": ws_ids} if db.get_bind().dialect.name == "postgresql" else {}
        elif table == "users":
            where, params = "id = :u", {"u": user.id}
        else:
            # A table nobody classified. Skipped rather than guessed at, and
            # the schema test fails so it gets classified deliberately.
            continue

        rows = db.execute(
            text(f'SELECT {select_list} FROM "{table}" WHERE {where}'), params)
        records = [{k: _plain(v) for k, v in zip(cols, r)} for r in rows]
        out["data"][table] = records
        out["_schema"]["tables"][table] = {
            "rows": len(records),
            "columns": cols,
            "description": DESCRIPTIONS.get(table, ""),
        }
    return out
