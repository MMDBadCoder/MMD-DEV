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
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS hermes_enabled BOOLEAN DEFAULT FALSE",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS hermes_installed BOOLEAN DEFAULT FALSE",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS hermes_key_hash VARCHAR(128)",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS hermes_key VARCHAR(256)",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS hermes_usage_usd DOUBLE PRECISION DEFAULT 0",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS hermes_dash_user VARCHAR(64)",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS hermes_dash_password VARCHAR(64)",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS hermes_error TEXT",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS hermes_credit_blocked BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS auto_stop_at TIMESTAMPTZ",
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
    for stmt in SCHEMA_PATCHES:
        try:
            with engine.begin() as conn:
                conn.execute(text(stmt))
        except Exception as exc:  # noqa: BLE001
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
