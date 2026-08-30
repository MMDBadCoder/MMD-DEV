"""Destructive journeys leave no unreachable external or database resources."""
import os
import asyncio
from types import SimpleNamespace
from datetime import UTC, datetime

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session            # noqa: E402

from mmd import worker                        # noqa: E402
from mmd.models import (AiUsageMark, AuditLog, Base, CreditAccount,
                        CreditTransaction, ExposedPort, Operation, PortKind,
                        SshKey, Ticket, TicketMessage, TxKind, User,
                        UserStatus, UsageSample, Workspace, WorkspaceState)


def populated():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = Session(engine, expire_on_commit=False)
    admin = User(email="admin@example.com", username="admin", password_hash="x",
                 status=UserStatus.APPROVED, is_admin=True)
    victim = User(email="gone@example.com", username="gone", password_hash="x",
                  status=UserStatus.DELETING)
    db.add_all([admin, victim]); db.commit()
    ws = Workspace(user_id=victim.id, idx=7, incus_project="ws-7",
                   state=WorkspaceState.OFF, hermes_key_hash="hash",
                   hermes_key="secret")
    db.add(ws); db.commit()
    db.add_all([
        CreditAccount(user_id=victim.id, balance_micro=1),
        CreditTransaction(user_id=victim.id, workspace_id=ws.id,
                          kind=TxKind.GRANT, amount_micro=1),
        UsageSample(workspace_id=ws.id, ts=datetime.now(UTC),
                    cpu_seconds_total=1, mem_bytes=1),
        ExposedPort(workspace_id=ws.id, internal_port=8080, external_port=22000,
                    protocol="both", kind=PortKind.USER, device="legacy"),
        SshKey(workspace_id=ws.id, key_type="ssh-ed25519", body="AAAA",
               fingerprint="SHA256:x"),
        AiUsageMark(workspace_id=ws.id, service="claude", session_id="s",
                    model="m"),
        AuditLog(actor_id=victim.id, action="x", target="ws-7"),
    ])
    ticket = Ticket(user_id=victim.id, subject="private")
    db.add(ticket); db.commit()
    db.add(TicketMessage(ticket_id=ticket.id, author_id=victim.id,
                         from_staff=False, body="private"))
    op = Operation(user_id=victim.id, workspace_id=ws.id, actor_id=admin.id,
                   kind="account_delete", status="queued", progress_code="queued")
    db.add(op); db.commit()
    return db, admin, victim, ws, op


def test_account_deletion_revokes_unpublishes_destroys_then_erases(monkeypatch):
    db, admin, victim, ws, op = populated()
    events = []

    class Router:
        def __init__(self, key): events.append("router")
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def delete_key(self, key): events.append("revoke")

    monkeypatch.setattr(worker, "CONFIG", SimpleNamespace(openrouter_key="management"))
    monkeypatch.setattr(worker, "OpenRouter", Router)
    monkeypatch.setattr(worker.svc, "sync_published_ports",
                        lambda db, **kw: events.append("firewall") or {"ok": True})
    monkeypatch.setattr(worker.svc, "call_provisioner",
                        lambda p, **kw: events.append("destroy") or {"ok": True})

    worker._purge_account(db, op)

    assert events.index("revoke") < events.index("firewall") < events.index("destroy")
    assert db.get(User, victim.id) is None
    assert db.get(User, admin.id) is not None
    for model in (Workspace, CreditAccount, CreditTransaction, UsageSample,
                  ExposedPort, SshKey, AiUsageMark, Ticket, TicketMessage,
                  Operation, AuditLog):
        assert db.scalar(select(model).limit(1)) is None, model.__name__


def test_cleanup_failure_keeps_identity_needed_for_retry(monkeypatch):
    db, _, victim, ws, op = populated()
    ws.hermes_key_hash = None
    db.commit()
    monkeypatch.setattr(worker.svc, "sync_published_ports",
                        lambda db, **kw: {"ok": False})
    try:
        worker._purge_account(db, op)
    except RuntimeError:
        db.rollback()
    assert db.get(User, victim.id) is not None
    assert db.get(Workspace, ws.id) is not None
    assert db.get(Operation, op.id) is not None


def test_a_failed_cleanup_does_not_block_customer_operations(monkeypatch):
    db, _, victim, ws, cleanup = populated()
    cleanup.status = "failed"
    customer_op = Operation(user_id=victim.id, workspace_id=ws.id,
                            actor_id=victim.id, kind="install_packages",
                            status="queued", progress_code="queued")
    db.add(customer_op); db.commit()
    selected = []

    monkeypatch.setattr(worker, "SessionLocal", lambda: db)
    monkeypatch.setattr(worker, "_install_packages",
                        lambda _db, op, _ws: selected.append(op.id))
    asyncio.run(worker.operations_once())

    assert selected == [customer_op.id]
