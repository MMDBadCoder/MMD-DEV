"""An approved account is useful before, between and without workspaces."""
import asyncio
import os

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from mmd import app as appmod, worker
from mmd.models import (Base, CreditAccount, OpenRouterAccount, Operation, User,
                        UserStatus, Workspace, WorkspaceState)
from mmd.security import hash_password


PASSWORD = "correct-horse-battery"


def database():
    db = Session(create_engine("sqlite://"), expire_on_commit=False)
    Base.metadata.create_all(db.bind)
    admin = User(username="admin-user",
                 password_hash=hash_password(PASSWORD), is_admin=True,
                 status=UserStatus.APPROVED)
    user = User(username="customer",
                password_hash=hash_password(PASSWORD), status=UserStatus.PENDING)
    db.add_all([admin, user]); db.commit()
    db.add_all([CreditAccount(user_id=admin.id), CreditAccount(user_id=user.id)])
    db.commit()
    return db, admin, user


def test_approval_activates_openrouter_without_creating_compute():
    db, admin, user = database()
    result = appmod.admin_approve(user.id, admin, db)
    assert result == {"ok": True, "status": "approved"}
    assert db.scalar(select(Workspace).where(Workspace.user_id == user.id)) is None
    account = db.get(OpenRouterAccount, user.id)
    assert account is not None and account.limit_dirty is True


def test_an_approved_customer_can_request_their_first_workspace():
    db, _admin, user = database()
    user.status = UserStatus.APPROVED
    db.commit()
    result = appmod.create_workspace(appmod.WorkspaceCreate(), user, db)
    ws = db.scalar(select(Workspace).where(Workspace.user_id == user.id))
    assert ws is not None and ws.state is WorkspaceState.PROVISIONING
    assert result["operation"]["kind"] == "workspace_create"


def test_workspace_deletion_keeps_the_account_and_openrouter(monkeypatch):
    db, _admin, user = database()
    user.status = UserStatus.APPROVED
    account = OpenRouterAccount(user_id=user.id, key="sk-or", key_hash="hash",
                                credit_blocked=False)
    ws = Workspace(user_id=user.id, idx=4, incus_project="ws-4",
                   state=WorkspaceState.DELETING)
    db.add_all([account, ws]); db.commit()
    op = Operation(user_id=user.id, workspace_id=ws.id, actor_id=user.id,
                   kind="workspace_delete", status="queued", progress_code="queued")
    db.add(op); db.commit()
    monkeypatch.setattr(worker.svc, "sync_published_ports",
                        lambda *_a, **_kw: {"ok": True})
    monkeypatch.setattr(worker.svc, "call_provisioner",
                        lambda *_a, **_kw: {"ok": True})
    worker._workspace_delete(db, op, ws)
    assert db.get(User, user.id) is not None
    assert db.get(OpenRouterAccount, user.id).key == "sk-or"
    assert db.scalar(select(Workspace).where(Workspace.user_id == user.id)) is None


def test_factory_reset_keeps_the_account_openrouter(monkeypatch):
    db, _admin, user = database()
    user.status = UserStatus.APPROVED
    account = OpenRouterAccount(user_id=user.id, key="sk-or", key_hash="hash",
                                credit_blocked=False)
    ws = Workspace(user_id=user.id, idx=5, incus_project="ws-5",
                   state=WorkspaceState.RESETTING, hermes_enabled=True)
    db.add_all([account, ws]); db.commit()
    op = Operation(user_id=user.id, workspace_id=ws.id, actor_id=user.id,
                   kind="factory_reset", status="queued", progress_code="queued")
    db.add(op); db.commit()
    monkeypatch.setattr(worker.svc, "call_provisioner",
                        lambda *_a, **_kw: {"ok": True})

    class Incus:
        async def stop(self, *_a): pass
        async def aclose(self): pass
    monkeypatch.setattr(worker, "_incus", lambda: Incus())
    asyncio.run(worker._factory_reset(db, op, ws))
    assert db.get(OpenRouterAccount, user.id).key == "sk-or"
    assert ws.hermes_enabled is False

