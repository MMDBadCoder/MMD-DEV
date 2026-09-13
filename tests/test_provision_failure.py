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
    block = src[at - 900:at + 1400]
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


def test_the_failure_reason_outlives_the_deleted_row():
    """operations.workspace_id is ON DELETE CASCADE.

    So deleting the workspace took the failed operation with it - and that
    operation is the only place the customer's activity page can read why their
    machine was never built. The first version of this fix claimed the reason
    was kept and then deleted it; the record survived exactly as long as nobody
    looked at it.
    """
    src = (ROOT / "control" / "mmd" / "worker.py").read_text(encoding="utf-8")
    at = src.index('oplib.fail(db, op, "provision_failed"')
    block = src[at:at + 1200]
    detach = block.index("Operation.workspace_id == ws.id")
    drop = block.index("db.delete(ws)")
    assert detach < drop, (
        "the workspace is deleted before the operation is detached, so the "
        "cascade destroys the only record of why it failed")


def test_the_cascade_that_makes_this_necessary_is_real():
    """If this ever stops cascading, the detach above is harmless - but the
    test above would pass for the wrong reason, so the premise is pinned."""
    src = (ROOT / "control" / "mmd" / "models" / "__init__.py").read_text(encoding="utf-8")
    at = src.index('__tablename__ = "operations"')
    block = src[at:at + 600]
    assert 'ForeignKey("workspaces.id", ondelete="CASCADE")' in block
