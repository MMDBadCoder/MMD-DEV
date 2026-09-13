"""What a failed machine creation must leave behind, and in what order.

Two customers were stranded by the same sequence. A ZFS pool outage broke the
first attempt; then every retry failed with "project ws-N already exists",
because the failure had left an Incus project behind while the database row was
removed - which freed the index for the next attempt to collide with.

These pin the order of the cleanup rather than its wording, because the order
is the part that was wrong each time.
"""
import os
from pathlib import Path

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

ROOT = Path(__file__).resolve().parents[1]
WORKER = (ROOT / "control" / "mmd" / "worker.py").read_text(encoding="utf-8")


def _failure_branch() -> str:
    """The provision-failure branch, from its `fail()` call to its `return`."""
    at = WORKER.index('oplib.fail(db, op, "provision_failed"')
    end = WORKER.index("\n        return", at)
    return WORKER[at:end]


BRANCH = _failure_branch()


def test_the_empty_row_is_removed():
    """create_workspace refuses when ANY row exists, so a row left in `error`
    means the customer can never create one - the panel answers "this account
    already has a machine" about a machine that was never built."""
    assert "db.delete(ws)" in BRANCH


def test_the_partial_workspace_is_destroyed_before_the_row():
    """ws-create.sh creates the Incus project first, so a failure leaves it
    behind. Removing only the row frees the index, so the next attempt is
    handed the SAME index, collides with the orphan, and fails forever with a
    message about a project the customer has never heard of."""
    assert BRANCH.index('"verb": "destroy"') < BRANCH.index("db.delete(ws)"), (
        "the row is deleted before the partial workspace is destroyed")


def test_the_failure_reason_outlives_the_row():
    """operations.workspace_id is ON DELETE CASCADE, so dropping the row took
    the failed operation with it - and that operation is the only place the
    activity page can read why the machine was never built."""
    assert BRANCH.index("Operation.workspace_id == ws.id") < BRANCH.index("db.delete(ws)"), (
        "the cascade destroys the only record of the failure")


def test_a_failed_cleanup_still_releases_the_customer():
    """If the destroy itself fails, the row must still go. Otherwise the
    customer is stuck behind a row they cannot use either way."""
    assert "try:" in BRANCH, "the cleanup is not guarded"
    assert BRANCH.index("try:") < BRANCH.index("db.delete(ws)")
    assert "except Exception" in BRANCH


def test_the_cascade_this_depends_on_is_real():
    """If it ever stops cascading the detach is harmless, but the test above
    would pass for the wrong reason - so the premise is pinned."""
    models = (ROOT / "control" / "mmd" / "models" / "__init__.py").read_text(encoding="utf-8")
    at = models.index('__tablename__ = "operations"')
    assert 'ForeignKey("workspaces.id", ondelete="CASCADE")' in models[at:at + 600]


def test_a_failed_power_operation_still_keeps_its_row():
    """The opposite case: there the machine and its disk are real, and
    forgetting them would orphan a customer's data."""
    assert "WorkspaceState.ERROR" in WORKER


def test_one_machine_per_account_still_holds():
    """The guard that made all of this matter must stay."""
    app = (ROOT / "control" / "mmd" / "app.py").read_text(encoding="utf-8")
    assert 'fail(409, "already_has_machine"' in app
