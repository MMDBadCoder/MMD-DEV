"""Durable long-running operations shared by the API and worker."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Operation

ACTIVE = ("queued", "running")


def create(db: Session, *, kind: str, user_id: int | None,
           workspace_id: int | None, actor_id: int | None,
           detail: dict | None = None) -> Operation:
    existing = db.scalar(select(Operation).where(
        Operation.workspace_id == workspace_id,
        Operation.status.in_(ACTIVE))) if workspace_id is not None else None
    if existing is not None:
        return existing
    op = Operation(kind=kind, user_id=user_id, workspace_id=workspace_id,
                   actor_id=actor_id, detail=detail or {}, status="queued",
                   progress_code="queued")
    db.add(op)
    db.commit()
    db.refresh(op)
    return op


def view(op: Operation) -> dict:
    return {
        "id": op.id, "kind": op.kind, "status": op.status,
        "progress_code": op.progress_code, "error_code": op.error_code,
        "created_at": op.created_at.isoformat() if op.created_at else None,
        "updated_at": op.updated_at.isoformat() if op.updated_at else None,
        "finished_at": op.finished_at.isoformat() if op.finished_at else None,
    }


def progress(db: Session, op: Operation, code: str) -> None:
    op.status = "running"
    op.progress_code = code
    op.error_code = None
    db.commit()


def finish(db: Session, op: Operation, now: datetime) -> None:
    op.status = "succeeded"
    op.progress_code = "done"
    op.finished_at = now
    db.commit()


def fail(db: Session, op: Operation, code: str, now: datetime) -> None:
    op.status = "failed"
    op.progress_code = "failed"
    op.error_code = code
    op.finished_at = now
    db.commit()
