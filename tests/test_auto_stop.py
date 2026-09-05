"""A machine stops on a schedule the customer agreed to - and never otherwise.

There used to be an idle auto-stop: 60 minutes without browser-terminal
activity and the machine was powered off "to protect the customer's credit". On
this host it fired seven times in a week, on a customer's machine and on the
operator's own. It was removed, and it is not coming back, because of what it
MEASURED: `last_activity` is written on power-on and when the browser terminal
opens, and nowhere else. A customer running a long build, working in the file
manager, or serving traffic on a published port registered as idle.

What replaced it, at a customer's request (ticket #17), is a different thing:

  * the deadline is measured from POWER-ON, not from activity, so it cannot
    mistake a busy machine for an abandoned one - it does not try to tell them
    apart at all;
  * it is stated up front rather than discovered, and
  * the customer can waive it for the run with one click.

The waiver is per RUN, not a stored preference. That is the load-bearing part:
a permanent "never stop this" would hand the cost straight back to the customer
who ticked it once and forgot, which is precisely what the feature exists to
prevent.

Running OUT OF CREDIT still stops a machine, on a separate path. These tests
keep the two apart, because a credit path that quietly grew a second reason to
stop a funded workspace is how the first version of this went wrong.
"""
import inspect
from pathlib import Path

from mmd import config, worker

ROOT = Path(__file__).resolve().parents[1]


def test_there_is_still_no_idle_timer():
    """The scheduled stop is NOT the idle stop returning. Nothing may read
    activity to decide whether to switch a machine off."""
    assert not hasattr(config.CONFIG, "idle_stop_minutes")
    src = (ROOT / "control" / "mmd").rglob("*.py")
    for f in src:
        text = f.read_text()
        assert "idle_stop" not in text, f
        assert "MMD_IDLE_STOP_MINUTES" not in text, f


def test_the_scheduled_stop_never_consults_activity():
    """`last_activity` is the signal that made the old timer wrong. The new one
    measures elapsed time from power-on, so it must not read it at all.

    Checked against the CODE, not the docstring - which mentions the old signal
    on purpose, to explain what this pass deliberately does not do."""
    body = inspect.getsource(worker.auto_stop_once).split('"""')[-1]
    assert "last_activity" not in body
    assert "auto_stop_at" in body


def test_the_scheduled_stop_is_its_own_pass():
    """Kept out of the credit path so the credit path stays checkable."""
    src = inspect.getsource(worker.lifecycle_once)
    assert "auto_stop" not in src, "the scheduled stop leaked into the credit pass"


def test_the_lifecycle_pass_only_stops_for_credit():
    """Every stop or destroy in the lifecycle loop must be reached only through
    a balance check."""
    src = inspect.getsource(worker.lifecycle_once)
    assert "balance_micro" in src
    # The only destructive actions left are the credit ones.
    assert "client.stop(" not in src, "a direct stop is back in the lifecycle pass"
    for marker in ("credit exhausted", "retention expired"):
        assert marker in src


def test_the_only_scheduled_stop_is_the_affordability_gate():
    """settle_once may stop a machine at the hour boundary - but only when the
    balance cannot cover the coming hour."""
    src = inspect.getsource(worker.settle_once)
    assert "can_afford_next_hour" in src
    stop_at = src.index("client.stop(")
    gate_at = src.index("can_afford_next_hour")
    assert gate_at < stop_at, "the stop is not guarded by the affordability check"


def test_the_reconciler_stops_reality_when_the_customer_wants_off():
    """A failed stop/reset may leave Incus running while desired_on is false.
    Reconciliation must honor intent rather than preserve leaked compute."""
    src = inspect.getsource(worker.reconcile_once)
    assert "or not ws.desired_on" in src


def test_recovered_transitional_state_is_observable():
    """Self-healing must leave evidence; otherwise an intermittent host fault
    disappears before an operator can understand or count it."""
    src = inspect.getsource(worker.reconcile_once)
    assert '"workspace_state_adopted"' in src
    assert '"mmd_workspace_state_adoptions_total"' in src


def test_the_session_probe_is_gone_too():
    """It existed only so the idle timer would not kill a live SSH session.
    With no timer there is nothing for it to serve, and a provisioner verb that
    serves nothing is surface for no benefit."""
    prov = (ROOT / "control" / "provisioner" / "provisioner.py").read_text()
    assert "probe_sessions" not in prov


def test_nothing_in_the_control_plane_stops_a_workspace_unprompted():
    """The API may stop a machine, but only from an endpoint a customer or an
    admin called."""
    app = (ROOT / "control" / "mmd" / "app.py").read_text()
    for line_no, line in enumerate(app.splitlines(), 1):
        if "client.stop(" in line:
            # Walk back to the enclosing def and check it is a request handler.
            head = "\n".join(app.splitlines()[:line_no])
            enclosing = head.rsplit("\ndef ", 1)[-1].rsplit("\nasync def ", 1)[-1]
            assert "Depends(" in enclosing.split("\n")[0] or "Depends(" in enclosing[:400], \
                f"app.py:{line_no} stops a workspace outside a request handler"


# --- the behaviour itself --------------------------------------------------
import os                                                     # noqa: E402

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

import pytest                                                 # noqa: E402
from datetime import timedelta                                # noqa: E402
from fastapi.testclient import TestClient                     # noqa: E402
from sqlalchemy import create_engine                          # noqa: E402
from sqlalchemy.orm import sessionmaker                        # noqa: E402
from sqlalchemy.pool import StaticPool                         # noqa: E402

from mmd import app as appmod                                  # noqa: E402
from mmd import service as svc                                 # noqa: E402
from mmd.config import CONFIG                                  # noqa: E402
from mmd.models import (Base, User, UserStatus, Workspace,     # noqa: E402
                        WorkspaceState)


@pytest.fixture
def env():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Local()
    u = User(password_hash="x", status=UserStatus.APPROVED,
             username="ali")
    db.add(u)
    db.commit()
    ws = Workspace(user_id=u.id, idx=3, incus_project="ws-3",
                   state=WorkspaceState.ON, mem_mib=2048)
    db.add(ws)
    db.commit()

    api = appmod.app
    api.dependency_overrides[appmod.get_session] = lambda: Local()
    api.dependency_overrides[appmod.current_user] = lambda: u
    yield TestClient(api), db, ws
    api.dependency_overrides.clear()


def test_arming_sets_a_deadline_the_configured_number_of_hours_out():
    ws = Workspace(state=WorkspaceState.ON)
    before = svc.now()
    svc.arm_auto_stop(ws)
    delta = ws.auto_stop_at - before
    assert timedelta(hours=CONFIG.auto_stop_hours) - timedelta(seconds=5) <= delta
    assert delta <= timedelta(hours=CONFIG.auto_stop_hours) + timedelta(seconds=5)


def test_a_machine_is_due_only_once_the_deadline_has_passed():
    ws = Workspace(state=WorkspaceState.ON)
    svc.arm_auto_stop(ws)
    assert svc.due_for_auto_stop(ws) is False
    assert svc.due_for_auto_stop(ws, at=ws.auto_stop_at + timedelta(seconds=1)) is True


def test_a_waived_run_is_never_due():
    """NULL means "the customer asked for this to keep going", not "stop now".
    Reading it the other way round would stop every opted-out machine at once."""
    ws = Workspace(state=WorkspaceState.ON)
    svc.disarm_auto_stop(ws)
    assert svc.due_for_auto_stop(ws, at=svc.now() + timedelta(days=365)) is False


def test_a_stopped_machine_is_never_due():
    ws = Workspace(state=WorkspaceState.OFF)
    svc.arm_auto_stop(ws)
    assert svc.due_for_auto_stop(ws, at=ws.auto_stop_at + timedelta(days=1)) is False


def test_the_customer_can_waive_the_deadline_for_this_run(env):
    client, db, ws = env
    svc.arm_auto_stop(ws)
    db.commit()

    assert client.get("/api/workspace").json()["auto_stop_at"] is not None
    r = client.post("/api/workspace/keep-running")
    assert r.status_code == 200, r.text
    assert r.json()["auto_stop_at"] is None

    db.expire_all()
    assert ws.auto_stop_at is None
    assert client.get("/api/workspace").json()["auto_stop_at"] is None


def test_waiving_is_idempotent(env):
    client, db, ws = env
    svc.disarm_auto_stop(ws)
    db.commit()
    assert client.post("/api/workspace/keep-running").status_code == 200


def test_the_waiver_does_not_survive_the_next_start(env):
    """The whole design rests on this. A stored "never stop this machine" would
    hand the cost back to the customer who ticked it once and forgot."""
    client, db, ws = env
    client.post("/api/workspace/keep-running")
    db.expire_all()
    assert ws.auto_stop_at is None

    # What the power endpoint does on the way into ON.
    svc.arm_auto_stop(ws)
    db.commit()
    assert ws.auto_stop_at is not None


def test_a_stopped_machine_cannot_waive_a_deadline_it_does_not_have(env):
    client, db, ws = env
    ws.state = WorkspaceState.OFF
    db.commit()
    r = client.post("/api/workspace/keep-running")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "machine_off"


def test_the_limit_is_advertised_even_when_no_deadline_is_armed(env):
    """The customer must learn the rule from the machine page, not from a
    machine that stopped."""
    client, db, ws = env
    svc.disarm_auto_stop(ws)
    db.commit()
    d = client.get("/api/workspace").json()
    assert d["auto_stop_hours"] == CONFIG.auto_stop_hours
    assert d["auto_stop_at"] is None
