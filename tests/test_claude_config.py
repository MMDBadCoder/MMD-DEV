"""The Claude sign-in resync, and the config merge it depends on.

Reported as: clicking «تازه‌سازی ورود» reported success, and running `claude`
in the workspace still opened a login/setup flow.

The credentials were never the problem - they were present, valid, and
`claude -p 'reply ok'` answered normally, which is why every non-interactive
check passed. What was missing was ONE key in ~/.claude.json:

    hasCompletedOnboarding: true

Measured with two HOMEs identical but for that key:

    without -> "Welcome to Claude Code v2.1.240 / Let's get started.
                Choose the text style that looks best with your terminal"
    with    -> straight to the normal prompt

And the reason it stayed missing is the whole bug. The old code was:

    [ -e /home/dev/.claude.json ] && exit 0

i.e. write the config only when the file is ABSENT. Claude Code creates that
file itself on its very first run, before the customer has finished anything -
so from the moment they typed `claude` once, resync could never set the key
again, while continuing to report success.

Observed across the live host, which is the shape of the evidence:

    ws-1  11 keys  hasCompletedOnboarding absent  <- the customer who reported it
    ws-8   1 key   hasCompletedOnboarding true    <- file was absent; ours landed
    ws-3  44 keys  hasCompletedOnboarding true    <- onboarded by hand
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "control"))

from provisioner.provisioner import (CLAUDE_CONFIG_MERGE,   # noqa: E402
                                     CLAUDE_MIN_CONFIG)

# The real file from the workspace that reported the bug: Claude Code's own
# first-run config, carrying an account but no onboarding key.
WS1_SHAPE = {
    "changelogLastFetched": 1787788721897,
    "firstStartTime": "2026-08-26T23:58:41.620Z",
    "firstStartVersion": "2.1.240",
    "hasResetAutoModeOptInForDefaultOffer": True,
    "machineID": "536bdec0",
    "migrationVersion": 13,
    "oauthAccount": {"accountUuid": "abc", "emailAddress": "x@example.com"},
    "opusProMigrationComplete": True,
    "seenNotifications": {},
    "sonnet1m45MigrationComplete": True,
    "userID": "42c33e50",
}


def merge(tmp_path, existing, home_name="home"):
    """Run the REAL merge program against a file, and return what it wrote."""
    home = tmp_path / home_name
    home.mkdir(parents=True, exist_ok=True)
    path = home / ".claude.json"
    if existing is not None:
        path.write_text(existing if isinstance(existing, str)
                        else json.dumps(existing))

    prog = (CLAUDE_CONFIG_MERGE
            .replace("/home/dev/.claude.json", str(path))
            # The test user is not `dev`; the chown is exercised in production.
            .replace('shutil.chown(tmp, "dev", "dev")', "pass"))
    r = subprocess.run([sys.executable, "-c", prog],
                       input=json.dumps(CLAUDE_MIN_CONFIG),
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return (json.loads(path.read_text()) if path.exists() else None), r.stdout, path


# --- the reported bug ------------------------------------------------------
def test_the_onboarding_key_is_set_even_when_the_file_already_exists(tmp_path):
    """The whole bug in one assertion. The old code skipped the file entirely
    when it existed, so this key never arrived and resync silently did nothing."""
    out, _, _ = merge(tmp_path, WS1_SHAPE)
    assert out["hasCompletedOnboarding"] is True


def test_everything_the_customer_already_had_survives(tmp_path):
    """The old comment's instinct was right - do not flatten their settings -
    it was the all-or-nothing implementation that was wrong."""
    out, _, _ = merge(tmp_path, WS1_SHAPE)
    for k, v in WS1_SHAPE.items():
        assert out[k] == v, k
    assert out["oauthAccount"] == WS1_SHAPE["oauthAccount"]
    assert len(out) == len(WS1_SHAPE) + 1


def test_it_still_works_when_there_is_no_file_at_all(tmp_path):
    out, _, _ = merge(tmp_path, None)
    assert out == CLAUDE_MIN_CONFIG


def test_running_it_twice_changes_nothing_the_second_time(tmp_path):
    """It runs on every resync and every provision."""
    merge(tmp_path, WS1_SHAPE)
    first = json.loads((tmp_path / "home" / ".claude.json").read_text())
    out, stdout, _ = merge(tmp_path, first)
    assert out == first
    assert "already correct" in stdout


# --- the ways a config can be broken ---------------------------------------
def test_a_corrupt_config_is_moved_aside_not_destroyed(tmp_path):
    """It is still the customer's only copy of whatever they had configured."""
    out, _, path = merge(tmp_path, "{ this is not json")
    assert out["hasCompletedOnboarding"] is True
    saved = path.with_suffix(".json.corrupt")
    assert saved.exists()
    assert saved.read_text() == "{ this is not json"


def test_a_config_that_is_a_list_is_treated_as_corrupt(tmp_path):
    """json.load succeeds on `[]`; .update() on it would explode."""
    out, _, path = merge(tmp_path, "[1, 2, 3]")
    assert out["hasCompletedOnboarding"] is True
    assert path.with_suffix(".json.corrupt").exists()


def test_an_explicit_false_is_corrected(tmp_path):
    """A customer who declined onboarding once must not be stuck with it."""
    out, _, _ = merge(tmp_path, {"hasCompletedOnboarding": False, "keep": 1})
    assert out["hasCompletedOnboarding"] is True
    assert out["keep"] == 1


def test_the_file_is_left_private(tmp_path):
    _, _, path = merge(tmp_path, WS1_SHAPE)
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_no_temporary_file_is_left_behind(tmp_path):
    _, _, path = merge(tmp_path, WS1_SHAPE)
    leftovers = [p.name for p in path.parent.iterdir()
                 if p.name.startswith(".claude.json.") and not p.name.endswith(".corrupt")]
    assert leftovers == [], leftovers


# --- and the reason it was invisible ---------------------------------------
PROVISIONER = (ROOT / "control" / "provisioner" / "provisioner.py").read_text()


def test_the_write_if_absent_shortcut_is_gone():
    """`[ -e ... ] && exit 0` is the bug itself. It must not come back."""
    assert "[ -e /home/dev/.claude.json ] && exit 0" not in PROVISIONER


def test_resync_does_not_report_success_while_the_wizard_still_opens():
    """`linked` alone was the success condition, and `linked` is only "the
    credentials file is non-empty" - so the button reported success on exactly
    the machines where `claude` still opened the wizard."""
    assert 'st["linked"] and st["onboarded"]' in PROVISIONER


def test_status_reports_onboarding_separately_from_being_signed_in():
    assert '"onboarded":' in PROVISIONER
    assert "hasCompletedOnboarding" in PROVISIONER


def test_the_status_probe_and_the_merge_agree_on_the_key():
    """If these drift, the status check starts certifying a key the merge does
    not set, and the button goes back to lying."""
    from provisioner.provisioner import _ONBOARDED_PROBE
    assert "hasCompletedOnboarding" in _ONBOARDED_PROBE
    assert "hasCompletedOnboarding" in CLAUDE_MIN_CONFIG


def test_the_api_passes_onboarding_through_to_the_interface():
    app = (ROOT / "control" / "mmd" / "app.py").read_text()
    assert '"onboarded": bool(resp.get("onboarded"))' in app
    # ...and the stopped-machine branch must carry the same shape, or the page
    # reads `undefined` and renders a machine as ready.
    assert '"onboarded": False' in app


def test_the_page_requires_both_before_it_says_ready():
    ai = (ROOT / "web" / "js" / "pages" / "ai.js").read_text()
    assert "c.linked && c.onboarded" in ai


# --- what a resync is allowed to touch -------------------------------------
# Where Claude Code actually keeps a customer's customisation, measured on live
# workspaces:
#
#   ~/.claude/settings.json   the SELECTED MODEL and its effort level, theme
#   ~/.claude.json            per-project history, model caches, the account
#   ~/.claude/                history.jsonl, sessions/, projects/, plugins/,
#                             shell-snapshots/, file-history/
#
# Resync may replace exactly one file - the credentials - and merge keys into
# ~/.claude.json. Everything else is the customer's and must survive, or the
# button becomes something nobody dares press.
UNTOUCHABLE = (
    "settings.json", "history.jsonl", "sessions", "projects", "plugins",
    "shell-snapshots", "file-history", "session-env", "backups",
)


def _install_commands(monkeypatch, tmp_path):
    """Every command a full install/resync issues, with _run stubbed out."""
    import provisioner.provisioner as prov

    creds = tmp_path / ".claude"
    creds.mkdir()
    (creds / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": "tok", "refreshToken": "ref"}}))
    monkeypatch.setattr(prov, "CLAUDE_HOST_HOME", tmp_path)

    calls = []
    monkeypatch.setattr(prov, "_run",
                        lambda cmd, timeout=None, stdin_text=None, **kw:
                        (calls.append(cmd), (True, ""))[1])
    monkeypatch.setattr(prov, "_run_split",
                        lambda cmd, timeout=None, **kw:
                        (calls.append(cmd),
                         (0, "version=1.0\nlinked=yes\nonboarded=True\n", ""))[1])
    prov._verb_ai_claude("ws-1", {"action": "install"})
    return [" ".join(c) for c in calls]


@pytest.mark.parametrize("path", UNTOUCHABLE)
def test_a_resync_never_writes_to_the_customers_own_state(monkeypatch, tmp_path, path):
    """Verified end to end on a live workspace too: a workspace with
    `model: opus` and `effortLevel: xhigh` came through a real resync
    byte-identical."""
    for cmd in _install_commands(monkeypatch, tmp_path):
        # Reading is fine; writing is not. Anything that could truncate or
        # redirect into one of these paths is the thing being ruled out.
        for verb in (f"> /home/dev/.claude/{path}", f"rm -f /home/dev/.claude/{path}",
                     f"rm -rf /home/dev/.claude/{path}", f"> /home/dev/{path}"):
            assert verb not in cmd, f"{verb!r} in: {cmd[:160]}"


def test_the_only_file_replaced_outright_is_the_credential(monkeypatch, tmp_path):
    joined = " ".join(_install_commands(monkeypatch, tmp_path))
    assert "/home/dev/.claude/.credentials.json" in joined
    # ~/.claude.json is reached through the merge program, never truncated.
    assert "cat > /home/dev/.claude.json" not in joined
    assert "hasCompletedOnboarding" in joined


def test_the_merge_preserves_a_selected_model_living_in_claude_json(tmp_path):
    """settings.json is where the model lives today, but Claude Code has moved
    this kind of state between the two files across versions. If it moves back,
    the merge must still not eat it."""
    existing = {**WS1_SHAPE, "model": "opus",
                "modelSettings": {"claude-opus-5": {"effortLevel": "xhigh"}},
                "projects": {"/home/dev": {"history": ["a", "b"]}}}
    out, _, _ = merge(tmp_path, existing)
    assert out["model"] == "opus"
    assert out["modelSettings"]["claude-opus-5"]["effortLevel"] == "xhigh"
    assert out["projects"]["/home/dev"]["history"] == ["a", "b"]
    assert out["hasCompletedOnboarding"] is True
