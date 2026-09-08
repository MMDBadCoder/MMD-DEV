"""The Hermes reconcile pass must leave a healthy dashboard alone.

Found in production: every customer's dashboard was being restarted every few
seconds. The pass called the provisioner on every tick for every workspace,
and the provisioner's enable path ends in `systemctl restart
hermes-dashboard`. The dashboard mints a fresh session token on each start, so
the page a customer had open was authenticating with a token that no longer
existed - every API call answered 401, and anything landing mid-restart got
502 from the proxy. Nothing crashed and nothing was logged as wrong.
"""
import os
import sys
from pathlib import Path

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "control"))

import pytest                                              # noqa: E402
from sqlalchemy import create_engine                       # noqa: E402
from sqlalchemy.orm import Session                         # noqa: E402

from mmd import worker                                     # noqa: E402
from mmd.models import (Base, OpenRouterAccount, User, UserStatus,  # noqa: E402
                        Workspace, WorkspaceState)
from mmd.security import hash_password                     # noqa: E402


@pytest.fixture
def env(monkeypatch):
    db = Session(create_engine("sqlite://"), expire_on_commit=False)
    Base.metadata.create_all(db.bind)
    user = User(username="c", phone="09120000001",
                password_hash=hash_password("x"), status=UserStatus.APPROVED)
    db.add(user); db.commit()
    ws = Workspace(user_id=user.id, idx=9, incus_project="ws-9",
                   state=WorkspaceState.ON, hermes_enabled=True)
    db.add(ws)
    db.add(OpenRouterAccount(user_id=user.id, key="sk-test"))
    db.commit()

    calls = []
    monkeypatch.setattr(worker.svc, "call_provisioner",
                        lambda payload, **k: calls.append(payload) or {"ok": True})
    return db, ws, calls


def test_a_healthy_workspace_is_still_reconciled(env):
    """The worker must NOT skip an installed workspace.

    It cannot know whether a reconcile is needed: the customer can switch
    Hermes' webhook platform on from its own dashboard, and that state lives
    inside the machine. Skipping here once meant the gateway was never
    installed for a customer who had enabled the webhook, and nothing said so.
    """
    db, ws, calls = env
    ws.hermes_installed = True
    ws.hermes_dash_password = "pw"
    ws.hermes_error = None
    db.commit()

    worker._hermes_install(db, ws)
    assert [c["verb"] for c in calls] == ["service_hermes"]
    assert calls[0]["install"] is False, "no reinstall, but still a reconcile"


def test_the_dashboard_restart_is_conditional():
    """Where the idempotency actually lives.

    The worker reconciles every pass, so an unconditional `systemctl restart`
    in the provisioner bounced every customer's dashboard every pass - and
    Hermes mints a new session token on each start, so the page they had open
    began answering 401 to its own API calls. The script must compare the unit
    file and check whether it is running before restarting anything.
    """
    from pathlib import Path
    src = Path("control/provisioner/provisioner.py").read_text(encoding="utf-8")
    at = src.index("systemctl restart hermes-dashboard")
    block = src[at - 1200:at + 200]
    assert "is-active --quiet hermes-dashboard" in block, (
        "the dashboard is restarted without checking whether it is running")
    assert "changed=1" in block, (
        "the unit file is replaced without comparing it to what is there")


def test_an_uninstalled_dashboard_is_still_installed(env):
    """The behaviour the guard must not break: retry until it exists."""
    db, ws, calls = env
    ws.hermes_installed = False
    db.commit()
    worker._hermes_install(db, ws)
    assert [c["verb"] for c in calls] == ["service_hermes"]
    assert calls[0]["action"] == "enable"


def test_a_recorded_error_is_retried(env):
    """A failed install must not be mistaken for a healthy one."""
    db, ws, calls = env
    ws.hermes_installed = True
    ws.hermes_dash_password = "pw"
    ws.hermes_error = "install failed"
    db.commit()
    worker._hermes_install(db, ws)
    assert len(calls) == 1


def test_a_powered_off_machine_is_left_alone(env):
    """There is nowhere to install to, and the pass runs again on power-on."""
    db, ws, calls = env
    ws.hermes_installed = True
    ws.hermes_dash_password = "pw"
    ws.hermes_error = None
    ws.state = WorkspaceState.OFF
    db.commit()
    worker._hermes_install(db, ws)
    assert calls == []
