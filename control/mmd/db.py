from __future__ import annotations

import logging
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import CONFIG
from .models import Base

engine = create_engine(CONFIG.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


# Columns added to tables that already exist in the wild.
#
# `create_all` CREATES missing tables and does nothing else - it will not add a
# column to a table it already sees. Adding `users.username` without this took
# the API down on restart: the model knew about a column the database did not
# have, and every query against it failed.
#
# Each entry is idempotent (`IF NOT EXISTS`), so this runs on every boot and is
# a no-op once applied. Append here whenever a column is added to a live table;
# a full migration tool is more machinery than a single-host product needs, but
# "the schema silently disagrees with the code" is not an acceptable trade.
SCHEMA_PATCHES: tuple[str, ...] = (
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR(32)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users (username)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name VARCHAR(120)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone VARCHAR(11)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_phone ON users (phone)",
    # Contact identity is phone-only. This intentionally follows the username
    # and phone backfills from older releases; the ORM no longer references the
    # column, and a clean database never creates it.
    "ALTER TABLE users DROP COLUMN IF EXISTS email",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_bot_token VARCHAR(256)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS telegram_user_id VARCHAR(15)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS session_version INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE credit_transactions ADD COLUMN IF NOT EXISTS scope_key VARCHAR(64)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_scoped_charge_once_per_period "
    "ON credit_transactions (scope_key, period_start, kind)",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS hermes_enabled BOOLEAN DEFAULT FALSE",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS hermes_installed BOOLEAN DEFAULT FALSE",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS hermes_dash_user VARCHAR(64)",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS hermes_dash_password VARCHAR(64)",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS hermes_vhost_ready BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS hermes_error TEXT",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS hermes_telegram_enabled BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS hermes_telegram_installed BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS hermes_telegram_token VARCHAR(256)",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS hermes_telegram_users VARCHAR(256)",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS hermes_telegram_error TEXT",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS auto_stop_at TIMESTAMPTZ",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS disk_used_mib INTEGER",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS disk_checked_at TIMESTAMPTZ",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS openclaw_enabled BOOLEAN DEFAULT FALSE",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS openclaw_installed BOOLEAN DEFAULT FALSE",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS openclaw_password VARCHAR(64)",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS openclaw_error TEXT",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS openclaw_telegram_enabled BOOLEAN DEFAULT FALSE",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS openclaw_telegram_installed BOOLEAN DEFAULT FALSE",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS openclaw_telegram_error TEXT",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS opencode_enabled BOOLEAN DEFAULT FALSE",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS opencode_installed BOOLEAN DEFAULT FALSE",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS opencode_password VARCHAR(64)",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS opencode_error TEXT",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS opencode_vhost_ready BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS openwebui_enabled BOOLEAN DEFAULT FALSE",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS openwebui_installed BOOLEAN DEFAULT FALSE",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS openwebui_password VARCHAR(64)",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS openwebui_error TEXT",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS openwebui_vhost_ready BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE exposed_ports ADD COLUMN IF NOT EXISTS web_ready BOOLEAN NOT NULL DEFAULT FALSE",
    # The SMS outbox. create_all makes the table on a fresh database; this is
    # what gives it to one that already exists.
    "CREATE TABLE IF NOT EXISTS sms_messages ("
    "id SERIAL PRIMARY KEY, "
    "user_id INTEGER REFERENCES users(id) ON DELETE SET NULL, "
    "phone VARCHAR(11) NOT NULL, kind VARCHAR(32) NOT NULL, "
    "body VARCHAR(320) NOT NULL, status VARCHAR(16) NOT NULL DEFAULT 'queued', "
    "attempts INTEGER NOT NULL DEFAULT 0, error VARCHAR(255), "
    "provider_message_id VARCHAR(32), dedupe_key VARCHAR(128) UNIQUE, "
    "next_attempt_at TIMESTAMPTZ, "
    "created_at TIMESTAMPTZ NOT NULL DEFAULT now(), sent_at TIMESTAMPTZ)",
    "CREATE INDEX IF NOT EXISTS ix_sms_pending ON sms_messages (status, next_attempt_at)",
    # Everything on by default, so an empty map is the normal state.
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS sms_prefs JSONB NOT NULL DEFAULT '{}'::jsonb",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS sms_credit_step_toman INTEGER NOT NULL DEFAULT 50000",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS sms_credit_band BIGINT",
    "CREATE TABLE IF NOT EXISTS sms_codes ("
    "id SERIAL PRIMARY KEY, phone VARCHAR(11) NOT NULL, "
    "purpose VARCHAR(16) NOT NULL, code_hash VARCHAR(128) NOT NULL, "
    "expires_at TIMESTAMPTZ NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
    "consumed_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "CREATE INDEX IF NOT EXISTS ix_sms_code_lookup ON sms_codes (phone, purpose)",
    "CREATE TABLE IF NOT EXISTS login_attempts ("
    "id SERIAL PRIMARY KEY, identifier VARCHAR(64) NOT NULL, "
    "created_at TIMESTAMPTZ NOT NULL DEFAULT now())",
    "CREATE INDEX IF NOT EXISTS ix_login_attempts_identifier "
    "ON login_attempts (identifier, created_at)",
    # PostgreSQL enforces enum types; SQLite does not. A status added to the
    # Python enum is therefore accepted by every test and rejected by
    # production, which is precisely what happened when ESCALATED shipped.
    "ALTER TYPE ticket_status ADD VALUE IF NOT EXISTS 'ESCALATED'",
    # `in_progress` said only that somebody was looking, which told the
    # customer nothing about whose turn it was. It is replaced by
    # `waiting_for_user`, which does. Adding the value and consuming it are
    # separate patches on purpose: Postgres refuses to use a new enum value
    # inside the transaction that added it, and each patch here gets its own.
    "ALTER TYPE ticket_status ADD VALUE IF NOT EXISTS 'WAITING_FOR_USER'",
    "UPDATE tickets SET status = 'WAITING_FOR_USER' WHERE status = 'IN_PROGRESS'",
    "CREATE INDEX IF NOT EXISTS ix_sms_codes_created_at ON sms_codes (created_at)",
    "ALTER TABLE openrouter_accounts ADD COLUMN IF NOT EXISTS limit_usd DOUBLE PRECISION",
    "ALTER TABLE openrouter_accounts ADD COLUMN IF NOT EXISTS limit_synced_at TIMESTAMPTZ",
    # Complete the 1.7 credential move before removing its five dead workspace
    # columns. Dynamic SQL matters here: after the columns are dropped, a later
    # boot must be able to parse this patch and take the false branch.
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM information_schema.columns "
    "WHERE table_schema = current_schema() AND table_name = 'workspaces' "
    "AND column_name = 'hermes_key_hash') THEN "
    "EXECUTE 'INSERT INTO openrouter_accounts "
    "(user_id, key_hash, key, usage_usd, credit_blocked, limit_dirty) "
    "SELECT user_id, hermes_key_hash, hermes_key, COALESCE(hermes_usage_usd, 0), "
    "COALESCE(hermes_credit_blocked, FALSE), COALESCE(hermes_limit_dirty, TRUE) "
    "FROM workspaces WHERE hermes_key_hash IS NOT NULL "
    "ON CONFLICT (user_id) DO NOTHING'; END IF; END $$",
    "ALTER TABLE workspaces DROP COLUMN IF EXISTS hermes_key_hash",
    "ALTER TABLE workspaces DROP COLUMN IF EXISTS hermes_key",
    "ALTER TABLE workspaces DROP COLUMN IF EXISTS hermes_usage_usd",
    "ALTER TABLE workspaces DROP COLUMN IF EXISTS hermes_credit_blocked",
    "ALTER TABLE workspaces DROP COLUMN IF EXISTS hermes_limit_dirty",
    # Published ports now carry both protocols. Customer-published rows written
    # before that are widened; the reserved SSH and RDP rows are left alone,
    # because both are TCP services and a UDP rule there would forward to a port
    # that never answers. The old UI allowed the same internal port to be
    # published once per protocol, so collapse that rare pair first; otherwise
    # widening both rows would violate uq_one_mapping_per_internal_port and
    # leave the entire migration unapplied.
    "DELETE FROM exposed_ports newer USING exposed_ports older "
    "WHERE newer.kind = 'USER' AND older.kind = 'USER' "
    "AND newer.workspace_id = older.workspace_id "
    "AND newer.internal_port = older.internal_port AND newer.id > older.id",
    # Idempotent - the second run matches nothing.
    "UPDATE exposed_ports SET protocol = 'both' "
    "WHERE kind = 'USER' AND protocol IN ('tcp', 'udp')",
)


# Patches that could not be applied on this boot. A skipped patch is a silent
# failure by design - the process must still start - so it is counted here and
# exported, rather than living only in a log line nobody reads.
SCHEMA_PATCH_FAILURES: list[str] = []


def init_db() -> None:
    Base.metadata.create_all(engine)
    _patch_schema()


def _patch_schema() -> None:
    """Apply each patch in its OWN transaction.

    This used to share one transaction across every statement, and that is a
    trap specific to Postgres: once any command in a transaction fails, the
    server aborts the whole thing and refuses every later command with
    "current transaction is aborted". The except clause swallowed that, so a
    single unsupported patch silently skipped EVERY patch after it - and logged
    at debug, so nothing said so.

    Found when `external_port DROP NOT NULL` never applied on a live host while
    the same statement ran fine by hand. Per-statement transactions mean one
    patch that cannot apply costs only itself.
    """
    from sqlalchemy import text
    log = logging.getLogger("mmd.db")
    SCHEMA_PATCH_FAILURES.clear()
    for stmt in SCHEMA_PATCHES:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception as exc:  # noqa: BLE001
            SCHEMA_PATCH_FAILURES.append(stmt[:120])
            # SQLite (the tests) rejects some of this syntax, and a patch that
            # cannot apply must not stop the process booting. Logged at WARNING
            # with the reason, because "silently skipped" is how the above went
            # unnoticed.
            log.warning("schema patch skipped: %s (%s)",
                        stmt, str(exc).splitlines()[0][:200])


def get_session() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
