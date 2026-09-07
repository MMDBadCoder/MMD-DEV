"""Rotating the MCP bearer token.

The key file is root-owned mode 400 so the internet-facing process cannot read
it; that same fact means mmd-api cannot rewrite it, so the work belongs to the
root provisioner. These pin the parts that would be silently wrong: the file
must never be left half-written, the new value must actually reach the caller
(it is shown once and pasted into an agent), and a failure to schedule the
restart must not be reported as a failed rotation when the key on disk has
already changed.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "control"))

import provisioner.provisioner as prov          # noqa: E402


def _redirect(monkeypatch, tmp_path):
    key = tmp_path / "mcp.key"
    key.write_text("old-token\n")
    os.chmod(key, 0o400)
    monkeypatch.setattr(prov, "MCP_KEY_PATH", str(key))
    return key


def test_replaces_the_key_and_returns_it(monkeypatch, tmp_path):
    key = _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(prov, "_run", lambda *a, **k: (0, ""))

    out = prov._rotate_mcp_key()

    assert out["ok"] is True
    assert out["restart_scheduled"] is True
    on_disk = key.read_text().strip()
    assert on_disk != "old-token"
    # Returned in full: it is displayed once and typed into an agent's config.
    assert out["token"] == on_disk
    assert len(on_disk) == 64            # token_hex(32)


def test_key_stays_unreadable(monkeypatch, tmp_path):
    key = _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(prov, "_run", lambda *a, **k: (0, ""))
    prov._rotate_mcp_key()
    assert oct(key.stat().st_mode)[-3:] == "400"


def test_no_temporary_file_is_left_behind(monkeypatch, tmp_path):
    """The write is staged then renamed, so a reader never sees a partial key."""
    key = _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(prov, "_run", lambda *a, **k: (0, ""))
    prov._rotate_mcp_key()
    assert sorted(p.name for p in tmp_path.iterdir()) == [key.name]


def test_restart_failure_still_reports_the_new_key(monkeypatch, tmp_path):
    """The key on disk already changed, so "failed" would be a lie.

    Reporting failure here would leave the operator believing the old token
    still works, when nothing accepts it after the next restart.
    """
    key = _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(prov, "_run", lambda *a, **k: (1, "boom"))

    out = prov._rotate_mcp_key()

    assert out["ok"] is True
    assert out["restart_scheduled"] is False
    assert "systemctl restart mmd-api" in out["warning"]
    assert out["token"] == key.read_text().strip()


def test_unwritable_path_is_a_clean_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(prov, "MCP_KEY_PATH", str(tmp_path / "nope" / "mcp.key"))
    monkeypatch.setattr(prov, "_run", lambda *a, **k: (0, ""))
    out = prov._rotate_mcp_key()
    assert out["ok"] is False
    assert "token" not in out


def test_verb_is_allowed_and_needs_no_workspace():
    """It is a host-wide action: requiring `idx` would make it uncallable."""
    assert "rotate_mcp_key" in prov.VERBS
    src = Path(prov.__file__).read_text(encoding="utf-8")
    assert src.index('if verb == "rotate_mcp_key"') < src.index('req["idx"]')
