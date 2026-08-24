"""Pure support-ticket rules.

Kept out of app.py so they can be tested without a database, a config file or a
running control plane - and so the one rule that is easy to get backwards (what
counts as unread) has somewhere to be stated once.
"""
from __future__ import annotations

from datetime import UTC, datetime


def as_utc(dt: datetime | None) -> datetime | None:
    """Normalise a timestamp before comparing it.

    PostgreSQL returns timezone-aware values for `timestamptz`; SQLite returns
    naive ones, and comparing the two raises TypeError. Rather than depend on
    which backend happens to be underneath - and turn a support page into a 500
    the day that changes - both sides are pinned to UTC here.
    """
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def is_unread(last_from_staff: bool | None, last_at: datetime | None,
              read_at: datetime | None, *, staff: bool) -> bool:
    """Has the OTHER side written since this side last looked?

    Two things this deliberately is not: it is not "has anyone written since"
    (your own message would mark your own ticket unread), and it is not "is the
    status open" (a ticket an operator has read and is working on should stop
    shouting at them).
    """
    if last_from_staff is None or last_at is None:
        return False
    if last_from_staff == staff:
        return False
    read_at = as_utc(read_at)
    return read_at is None or as_utc(last_at) > read_at
