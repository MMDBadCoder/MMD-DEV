"""What a failed machine creation must leave behind: nothing.

A customer tried to create a machine while the ZFS pool was unmounted. The
provision failed in under a second, the workspace row was left in `error`, and
create_workspace refuses when ANY row exists - so the panel answered "this
account already has a machine" about a machine that had never been built, and
only an operator could clear it.
"""
import os
from pathlib import Path

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

ROOT = Path(__file__).resolve().parents[1]


def test_a_failed_creation_removes_the_empty_row():
    """Creation is the one failure where nothing exists yet."""
    src = (ROOT / "control" / "mmd" / "worker.py").read_text(encoding="utf-8")
    at = src.index('oplib.fail(db, op, "provision_failed"')
    block = src[at - 900:at + 400]
    assert "db.delete(ws)" in block, (
        "a failed provision leaves a workspace row, which blocks the customer "
        "from ever creating one - create_workspace refuses if any row exists")


def test_a_failed_power_operation_still_keeps_the_row():
    """The opposite case: there the machine and its disk are real, and
    forgetting them would orphan a customer's data."""
    src = (ROOT / "control" / "mmd" / "worker.py").read_text(encoding="utf-8")
    assert "WorkspaceState.ERROR" in src, (
        "nothing records an error state any more, so a failed start or stop "
        "has nowhere to be reported")


def test_the_create_endpoint_still_refuses_a_second_machine():
    """The guard that made this matter must stay: one machine per account."""
    src = (ROOT / "control" / "mmd" / "app.py").read_text(encoding="utf-8")
    assert 'fail(409, "already_has_machine"' in src
