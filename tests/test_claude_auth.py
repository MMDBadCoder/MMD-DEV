"""What may cross from the host into a customer's machine.

Workspaces ship with Claude Code already signed in, which means the provisioner
reaches into the operator's own home directory. That directory also holds every
repository they have opened, every conversation and every plan. These tests
exist to make the boundary a property of the code rather than a promise: the
copy is an allowlist, it is a re-serialisation rather than a file transfer, and
nothing outside the single credential key can travel even if a future Claude
Code release puts it in the same file.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# The provisioner is a standalone root daemon, not part of the mmd package, so
# it is loaded by path rather than imported.
spec = importlib.util.spec_from_file_location(
    "mmd_provisioner", ROOT / "control" / "provisioner" / "provisioner.py")
prov = importlib.util.module_from_spec(spec)
sys.modules["mmd_provisioner"] = prov
spec.loader.exec_module(prov)


# --- the allowlist itself --------------------------------------------------
def test_exactly_one_file_is_ever_read():
    assert prov.CLAUDE_AUTH_FILE == ".credentials.json"


def test_exactly_one_key_is_ever_carried():
    assert prov.CLAUDE_AUTH_KEYS == ("claudeAiOauth",)


def test_the_things_the_customer_was_promised_are_not_copied():
    """The customer-facing page names four categories. Each must be refused."""
    for name in ("sessions", "cache", "projects", "history.jsonl",
                 "settings.json", "shell-snapshots", "todos", ".claude.json"):
        assert name in prov.CLAUDE_NEVER_COPY
        assert name != prov.CLAUDE_AUTH_FILE


def test_the_synthesised_config_carries_no_identity():
    """The workspace's ~/.claude.json is generated, never copied.

    The host's own is 59 kB of account record, feature flags and repository
    history. Anything resembling an identifier appearing here would mean the
    generated file had started to become a copy.
    """
    cfg = prov.CLAUDE_MIN_CONFIG
    assert cfg == {"hasCompletedOnboarding": True}
    blob = json.dumps(cfg).lower()
    for leak in ("userid", "oauthaccount", "machineid", "email", "projects",
                 "account", "organization"):
        assert leak not in blob


# --- the extraction --------------------------------------------------------
@pytest.fixture
def fake_host(tmp_path, monkeypatch):
    """A host home that looks like a real one: credentials plus everything else."""
    home = tmp_path / "home"
    cdir = home / ".claude"
    cdir.mkdir(parents=True)
    (cdir / ".credentials.json").write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "tok-abc", "refreshToken": "ref-xyz",
                          "expiresAt": 111, "refreshTokenExpiresAt": 222,
                          "scopes": ["user:inference"], "subscriptionType": "pro"},
        # Plausible future additions that are NOT auth. Neither is allowlisted,
        # so both must be dropped without anyone having to notice them.
        "telemetryUserId": "should-not-travel",
        "lastSessionId": "should-not-travel",
    }))
    (cdir / "history.jsonl").write_text('{"secret":"private"}\n')
    (cdir / "settings.json").write_text('{"theme":"dark"}')
    (cdir / "projects").mkdir()
    (cdir / "projects" / "acme.jsonl").write_text("private client work")
    (cdir / "sessions").mkdir()
    (home / ".claude.json").write_text(json.dumps({"userID": "u-1", "projects": {"/srv": {}}}))
    monkeypatch.setattr(prov, "CLAUDE_HOST_HOME", home)
    return home


def test_only_the_oauth_grant_survives(fake_host):
    creds, why = prov._claude_credentials()
    assert why == ""
    assert set(creds) == {"claudeAiOauth"}
    assert creds["claudeAiOauth"]["accessToken"] == "tok-abc"


def test_nothing_else_in_the_file_travels(fake_host):
    creds, _ = prov._claude_credentials()
    blob = json.dumps(creds)
    assert "should-not-travel" not in blob
    assert "telemetryUserId" not in blob
    assert "lastSessionId" not in blob


def test_no_other_file_is_opened(fake_host, monkeypatch):
    """Directly assert the file access, not just the output.

    An implementation could produce a correct-looking document while still
    reading - and therefore risking logging or erroring with - the operator's
    history. Only one path may be opened.
    """
    opened = []
    real = Path.read_text

    def spy(self, *a, **kw):
        opened.append(self.name)
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", spy)
    prov._claude_credentials()
    assert opened == [".credentials.json"]


def test_a_host_that_is_not_signed_in_is_reported_not_guessed(tmp_path, monkeypatch):
    monkeypatch.setattr(prov, "CLAUDE_HOST_HOME", tmp_path)
    creds, why = prov._claude_credentials()
    assert creds is None
    assert "not signed in" in why


def test_a_credential_without_a_token_is_refused(tmp_path, monkeypatch):
    cdir = tmp_path / ".claude"
    cdir.mkdir(parents=True)
    (cdir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {}}))
    monkeypatch.setattr(prov, "CLAUDE_HOST_HOME", tmp_path)
    creds, why = prov._claude_credentials()
    assert creds is None
    assert "not signed in" in why


def test_a_corrupt_credential_file_does_not_raise(tmp_path, monkeypatch):
    cdir = tmp_path / ".claude"
    cdir.mkdir(parents=True)
    (cdir / ".credentials.json").write_text("{not json")
    monkeypatch.setattr(prov, "CLAUDE_HOST_HOME", tmp_path)
    creds, why = prov._claude_credentials()
    assert creds is None and why


# --- the verb --------------------------------------------------------------
def test_the_verb_is_allowlisted():
    assert "ai_claude" in prov.VERBS


def test_unknown_actions_are_refused(fake_host):
    for bad in ("", "status; rm -rf /", "read", None, "provision"):
        out = prov._verb_ai_claude("ws-1", {"action": bad})
        assert out["ok"] is False


def test_install_refuses_when_the_host_is_not_signed_in(tmp_path, monkeypatch):
    monkeypatch.setattr(prov, "CLAUDE_HOST_HOME", tmp_path)
    out = prov._verb_ai_claude("ws-1", {"action": "install"})
    assert out["ok"] is False
    assert "not signed in" in out["error"]


def test_the_token_never_reaches_a_command_line(fake_host, monkeypatch):
    """The credential is piped on stdin.

    Anything placed in argv is visible in `ps` to every process on the host,
    including - through no fault of the design - other tenants' operators.
    """
    calls = []

    def fake_run(cmd, timeout=None, stdin_text=None, **kw):
        calls.append((cmd, stdin_text))
        return True, "version=1.0.0\nlinked=yes\n"

    def fake_run_split(cmd, timeout=None, **kw):
        calls.append((cmd, None))
        return 0, "version=1.0.0\nlinked=yes\n", ""

    monkeypatch.setattr(prov, "_run", fake_run)
    monkeypatch.setattr(prov, "_run_split", fake_run_split)
    out = prov._verb_ai_claude("ws-1", {"action": "install"})
    assert out["ok"] is True

    for cmd, _ in calls:
        assert "tok-abc" not in " ".join(cmd)
        assert "ref-xyz" not in " ".join(cmd)
    # ...and it did travel, on stdin, exactly once.
    piped = [s for _, s in calls if s and "tok-abc" in s]
    assert len(piped) == 1
    assert set(json.loads(piped[0])) == {"claudeAiOauth"}
