"""A machine in `error` must still be startable by the person who owns it.

Two customers in two weeks were locked out this way. A storage outage put the
workspace in `error`; the console then labelled it "needs review" - naming
nobody - and every press of the power button answered 409. There was no way
forward from the interface, and the worker only adopted reality seven hours
later.

`error` records that something failed once, not that the machine is unusable.
"""
import os
from pathlib import Path

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "control" / "mmd" / "app.py").read_text(encoding="utf-8")


def _power_on_branch() -> str:
    at = APP.index("        if body.on:")
    return APP[at:at + 2200]


BRANCH = _power_on_branch()


def test_error_is_a_startable_state():
    assert "WorkspaceState.OFF, WorkspaceState.ERROR" in BRANCH, (
        "a machine in error still refuses to start, so its owner is locked "
        "out of the only control the interface offers them")


def test_archived_is_still_refused():
    """Archived is genuinely not startable - the disk has been put away, and
    restoring it is a different operation with a different cost."""
    assert 'fail(409, "archived"' in BRANCH


def test_a_busy_machine_is_still_refused():
    """starting/stopping/provisioning are transitions in flight. Starting one
    again would race the operation already running."""
    assert 'fail(409, "busy"' in BRANCH


def test_a_recovered_machine_does_not_keep_its_old_error():
    """Otherwise the console shows a fault on a machine that just started."""
    assert "recovering" in BRANCH and "ws.error = None" in BRANCH


def test_the_credit_and_capacity_checks_still_run_first():
    """Recovery must not become a way around the two things that protect the
    platform from a machine it cannot pay for or fit."""
    assert BRANCH.index("WorkspaceState.ERROR") < BRANCH.index("can_afford_next_hour")
    assert BRANCH.index("WorkspaceState.ERROR") < BRANCH.index("check_admission")
