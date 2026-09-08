"""The additive-migration mechanism.

The bug this pins down: every patch shared ONE transaction. Postgres aborts a
whole transaction after any failed command and refuses everything after it, so
one unsupported statement silently skipped every statement that followed - and
it logged at debug, so nothing said so.

It surfaced as `ALTER TABLE exposed_ports ALTER COLUMN external_port DROP NOT
NULL` never applying on a live host, while the identical statement ran fine by
hand. Publishing a named application would then have failed with a NOT NULL
violation at runtime.
"""
import logging
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from mmd import db as dbmod


def _fresh(monkeypatch, patches):
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE t (a INTEGER)"))
    monkeypatch.setattr(dbmod, "engine", engine)
    monkeypatch.setattr(dbmod, "SCHEMA_PATCHES", patches)
    return engine


def test_a_failing_patch_does_not_skip_the_ones_after_it(monkeypatch):
    engine = _fresh(monkeypatch, (
        "ALTER TABLE t ADD COLUMN first INTEGER",
        "THIS IS NOT SQL AT ALL",
        "ALTER TABLE t ADD COLUMN after_the_failure INTEGER",
    ))
    dbmod._patch_schema()

    cols = {c["name"] for c in inspect(engine).get_columns("t")}
    assert "first" in cols
    # The whole point: this one comes AFTER the failure.
    assert "after_the_failure" in cols


def test_a_skipped_patch_is_logged_loudly_enough_to_notice(monkeypatch, caplog):
    _fresh(monkeypatch, ("THIS IS NOT SQL AT ALL",))
    with caplog.at_level(logging.WARNING, logger="mmd.db"):
        dbmod._patch_schema()
    assert any(r.levelno >= logging.WARNING for r in caplog.records), caplog.records


def test_a_patch_that_cannot_apply_never_stops_the_process_booting(monkeypatch):
    _fresh(monkeypatch, ("THIS IS NOT SQL AT ALL",))
    dbmod._patch_schema()          # must not raise


def test_every_shipped_patch_is_idempotent_in_shape(monkeypatch):
    """Each runs on every boot, so each must be safe to re-run. Postgres has no
    `ALTER COLUMN ... DROP NOT NULL IF ...`, but that statement is naturally
    idempotent; the ADD COLUMN and CREATE INDEX ones need the guard."""
    for stmt in dbmod.SCHEMA_PATCHES:
        if stmt.startswith("ALTER TABLE") and " ADD COLUMN" in stmt:
            assert "IF NOT EXISTS" in stmt, stmt
        if stmt.startswith("CREATE"):
            assert "IF NOT EXISTS" in stmt, stmt


def test_workspace_schema_drops_only_the_superseded_openrouter_columns():
    model_source = Path(__file__).resolve().parents[1].joinpath(
        "control/mmd/models/__init__.py").read_text()
    for name in ("hermes_key_hash", "hermes_key", "hermes_usage_usd",
                 "hermes_credit_blocked", "hermes_limit_dirty"):
        assert f"{name}: Mapped" not in model_source
        assert any(f"DROP COLUMN IF EXISTS {name}" in stmt
                   for stmt in dbmod.SCHEMA_PATCHES)
    # Hermes itself still belongs to the workspace; its state and dashboard
    # credentials must not be mistaken for the retired supplier account.
    for name in ("hermes_enabled", "hermes_installed", "hermes_dash_password"):
        assert f"{name}: Mapped" in model_source


def test_new_ticket_statuses_reach_the_postgresql_enum():
    """PostgreSQL enforces enum types and SQLite does not, so a status added
    to the Python enum passes every test and is rejected by production. That
    is not hypothetical: ESCALATED shipped, every test was green, and the
    first real call returned 500 with `invalid input value for enum`.

    Each member therefore needs an ALTER TYPE patch. The two lists are
    compared rather than eyeballed, because the failure only appears in
    production.
    """
    from pathlib import Path
    from mmd.models import TicketStatus

    patches = (Path(__file__).resolve().parents[1]
               / "control" / "mmd" / "db.py").read_text()
    # OPEN, ANSWERED and CLOSED predate the patch list: they were created with
    # the type itself. Anything added later needs a patch - including
    # WAITING_FOR_USER, which replaced the original IN_PROGRESS and therefore
    # is NOT one of these however much it looks like a rename.
    original = {"OPEN", "ANSWERED", "CLOSED"}
    for member in TicketStatus:
        if member.name in original:
            continue
        assert f"'{member.name}'" in patches, (
            f"TicketStatus.{member.name} has no ALTER TYPE patch, so it will "
            f"be rejected by PostgreSQL in production while passing on SQLite")
