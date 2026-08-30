"""Durable customer notifications with idempotent condition updates."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Notification


def emit(db: Session, *, user_id: int, kind: str, code: str,
         severity: str = "info", detail: dict | None = None,
         href: str | None = None, dedupe_key: str | None = None) -> Notification:
    row = None
    if dedupe_key:
        row = db.scalar(select(Notification).where(
            Notification.user_id == user_id,
            Notification.dedupe_key == dedupe_key))
    if row is None:
        row = Notification(user_id=user_id, kind=kind, code=code,
                           severity=severity, detail=detail or {}, href=href,
                           dedupe_key=dedupe_key)
        db.add(row)
    else:
        became_new = row.resolved_at is not None or row.code != code
        row.kind, row.code, row.severity = kind, code, severity
        row.detail, row.href, row.resolved_at = detail or {}, href, None
        if became_new:
            row.read_at = None
            row.created_at = datetime.now(UTC)
    db.commit()
    db.refresh(row)
    return row


def resolve(db: Session, user_id: int, dedupe_key: str, now: datetime) -> None:
    row = db.scalar(select(Notification).where(
        Notification.user_id == user_id,
        Notification.dedupe_key == dedupe_key,
        Notification.resolved_at.is_(None)))
    if row:
        row.resolved_at = now
        db.commit()


def view(row: Notification) -> dict:
    return {"id": row.id, "kind": row.kind, "code": row.code,
            "severity": row.severity, "detail": row.detail or {},
            "href": row.href, "read": row.read_at is not None,
            "resolved": row.resolved_at is not None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None}
