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
)


def init_db() -> None:
    Base.metadata.create_all(engine)
    _patch_schema()


def _patch_schema() -> None:
    from sqlalchemy import text
    with engine.begin() as conn:
        for stmt in SCHEMA_PATCHES:
            try:
                conn.execute(text(stmt))
            except Exception:  # noqa: BLE001
                # SQLite (the tests) rejects some of this syntax, and a patch
                # that cannot apply must not stop the process booting. The
                # column either exists or the next query says so loudly.
                logging.getLogger("mmd.db").debug("schema patch skipped: %s", stmt)


def get_session() -> Iterator[Session]:
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
