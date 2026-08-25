"""A funded workspace is never switched off by us.

There used to be an idle auto-stop: 60 minutes without browser-terminal
activity and the machine was powered off "to protect the customer's credit". On
this host it fired seven times in a week, on a customer's machine and on the
operator's own.

It deserved to go regardless of the policy argument, because of what it measured:
`last_activity` is written on power-on and when the browser terminal opens, and
nowhere else. A customer running a long build, working in the file manager, or
serving traffic on a published port registered as idle. Billing is hourly, so a
machine left running is simply paid for - that is the customer's call.

Running OUT OF CREDIT still stops a machine. That is a different thing and stays.
"""
import inspect
from pathlib import Path

from mmd import config, worker

ROOT = Path(__file__).resolve().parents[1]


def test_there_is_no_idle_timer():
    assert not hasattr(config.CONFIG, "idle_stop_minutes")
    src = (ROOT / "control" / "mmd").rglob("*.py")
    for f in src:
        text = f.read_text()
        assert "idle_stop" not in text, f
        assert "MMD_IDLE_STOP_MINUTES" not in text, f


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


def test_the_reconciler_only_stops_what_is_already_marked_off():
    """Not a policy - a consistency repair. It stops an instance that Incus
    reports running while the ledger says off, because that is compute being
    given away."""
    src = inspect.getsource(worker.reconcile_once)
    assert "actually_on and ws.state == WorkspaceState.OFF" in src


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
