"""The supplier key follows credit in both directions."""
import os

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session    # noqa: E402

from mmd import hermes, worker        # noqa: E402
from mmd.models import (Base, CreditAccount, User, UserStatus, Workspace,
                        WorkspaceState)  # noqa: E402
from mmd.openrouter import KeyInfo    # noqa: E402

MICRO = 1_000_000


class Supplier:
    def __init__(self, *, disabled: bool, usage: float, limit: float):
        self.info = KeyInfo("hash", "name", "", disabled, usage, limit,
                            max(0.0, limit - usage))
        self.updates = []

    def get_key(self, _key_hash):
        return self.info

    def update_key(self, _key_hash, **changes):
        self.updates.append(changes)


def workspace(balance: int, *, blocked: bool):
    db = Session(create_engine("sqlite://"), expire_on_commit=False)
    Base.metadata.create_all(db.bind)
    user = User(email="credit@example.com", username="credit-user",
                password_hash="x", status=UserStatus.APPROVED)
    db.add(user); db.commit()
    db.add(CreditAccount(user_id=user.id, balance_micro=balance))
    ws = Workspace(user_id=user.id, idx=1, incus_project="ws-1",
                   state=WorkspaceState.ON, hermes_enabled=True,
                   hermes_installed=True, hermes_key="secret",
                   hermes_key_hash="hash", hermes_credit_blocked=blocked)
    db.add(ws); db.commit()
    return db, ws


def test_zero_credit_disables_an_existing_key_immediately(monkeypatch):
    db, ws = workspace(0, blocked=False)
    supplier = Supplier(disabled=False, usage=2.0, limit=5.0)
    monkeypatch.setattr(hermes, "meter", lambda *args: 0)

    worker._hermes_workspace(db, ws, supplier, object(), 200_000.0, 0.0,
                             meter_usage=False)

    assert supplier.updates[-1]["disabled"] is True
    assert ws.hermes_credit_blocked is True


def test_adding_credit_reenables_the_same_key_with_new_headroom(monkeypatch):
    db, ws = workspace(200_000 * MICRO, blocked=True)
    supplier = Supplier(disabled=True, usage=2.0, limit=2.01)
    monkeypatch.setattr(hermes, "meter", lambda *args: 0)

    worker._hermes_workspace(db, ws, supplier, object(), 200_000.0, 0.0,
                             meter_usage=False)

    assert supplier.updates[-1] == {"limit_usd": 3.0, "disabled": False}
    assert ws.hermes_credit_blocked is False


def test_a_positive_balance_top_up_refreshes_the_cap_on_the_fast_pass(monkeypatch):
    db, ws = workspace(400_000 * MICRO, blocked=False)
    ws.hermes_limit_dirty = True
    db.commit()
    supplier = Supplier(disabled=False, usage=2.0, limit=3.0)
    monkeypatch.setattr(hermes, "meter", lambda *args: 0)

    worker._hermes_workspace(db, ws, supplier, object(), 200_000.0, 0.0,
                             meter_usage=False)

    assert supplier.updates[-1] == {"limit_usd": 4.0, "disabled": False}
    assert ws.hermes_limit_dirty is False


def test_zero_credit_does_not_mint_a_new_usable_key(monkeypatch):
    db, ws = workspace(0, blocked=False)
    ws.hermes_key = None
    ws.hermes_key_hash = None
    db.commit()
    monkeypatch.setattr(hermes, "ensure_key",
                        lambda *args: (_ for _ in ()).throw(AssertionError()))

    worker._hermes_workspace(db, ws, Supplier(disabled=False, usage=0, limit=1),
                             object(), 200_000.0, 0.0)

    assert ws.hermes_key_hash is None
    assert ws.hermes_credit_blocked is True


def test_successful_gateway_delivery_erases_the_control_plane_token(monkeypatch):
    db, ws = workspace(200_000 * MICRO, blocked=False)
    ws.hermes_telegram_enabled = True
    ws.hermes_telegram_token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcd"
    ws.hermes_telegram_users = "123456789"
    ws.hermes_dash_user = "credit-user"
    ws.hermes_dash_password = "dashboard-password"
    db.commit()
    sent = {}
    monkeypatch.setattr(worker.hermes, "default_model", lambda db: "test/model")
    monkeypatch.setattr(worker.svc, "workspace_ip", lambda ws: "10.0.0.2")
    monkeypatch.setattr(worker.svc, "call_provisioner",
                        lambda payload, timeout=None: sent.update(payload) or {"ok": True})

    worker._hermes_install(db, ws)

    assert sent["telegram_enabled"] is True
    assert sent["telegram_token"].startswith("123456789:")
    assert sent["telegram_users"] == "123456789"
    assert ws.hermes_telegram_installed is True
    assert ws.hermes_telegram_token is None
