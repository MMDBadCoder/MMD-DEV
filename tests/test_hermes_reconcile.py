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


def test_the_gateway_restart_is_conditional_too():
    """The half that was missed when the dashboard was fixed.

    Making the worker reconcile every pass - which is what lets a customer
    switching the webhook on be noticed - also made this run every pass. With
    an unconditional restart the gateway was bounced every ~19 seconds: it
    bound 8644, was killed, bound again. `ss` showed nothing most of the time
    and no webhook could be delivered.
    """
    from pathlib import Path
    src = Path("control/provisioner/provisioner.py").read_text(encoding="utf-8")
    at = src.index("systemctl restart hermes-gateway")
    block = src[at - 1400:at + 200]
    assert "is-active --quiet hermes-gateway" in block, (
        "the gateway is restarted without checking whether it is running")
    assert "changed=1" in block, (
        "the unit file is replaced without comparing it to what is there")


def test_the_env_file_is_merged_not_overwritten():
    """It is shared. The platform owns the OpenRouter and Telegram variables;
    WEBHOOK_ENABLED, WEBHOOK_PORT and WEBHOOK_SECRET belong to the operator.

    Truncating it meant the webhook settings someone had just added were gone
    again within seconds, and nothing said why.
    """
    from pathlib import Path
    src = Path("control/provisioner/provisioner.py").read_text(encoding="utf-8")
    # Anchored on the temp file the merge writes: the first mention of the
    # env path is the gateway unit's EnvironmentFile, which is not this.
    assert "cat > /home/dev/.hermes/.env " not in src, (
        "the env file is still truncated on every pass")
    at = src.index("/home/dev/.hermes/.env.new")
    block = src[at - 1200:at + 600]
    assert "grep -vE " in block, "nothing preserves the variables we do not own"
    assert "OPENROUTER_API_KEY|TELEGRAM_BOT_TOKEN" in block, (
        "the replace-list must name exactly the variables this platform owns")


def test_a_changed_env_restarts_the_gateway():
    """The gap the conditional restart opened.

    The gateway reads .env once, at start, through EnvironmentFile. Restarting
    only on a unit-file change meant a new Telegram token or allowlist was
    written to disk and never loaded - the process kept the old credentials
    while the panel reported success.
    """
    from pathlib import Path
    src = Path("control/provisioner/provisioner.py").read_text(encoding="utf-8")
    assert "env_changed=1" in src, "the env write never reports a change"
    assert 'env_changed = "env_changed=1" in (out + err)' in src, (
        "the reported change is never read back")
    assert '("changed=1; " if env_changed else "")' in src, (
        "a changed environment does not reach the restart decision")
