"""The Hermes gateway must run for the webhook listener, not only for Telegram.

The gateway process is what serves Hermes' webhook platform on 8644 and runs
its cron scheduler; the dashboard on 9119 does neither. The provisioner used
to install the unit only when a Telegram bot was connected, and to `rm -f` it
otherwise -- so the ticket webhook could not run at all without Telegram, and
a hand-installed unit vanished at the next reconcile. That is what happened
while the support agent was being set up.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "control"))

import provisioner.provisioner as prov          # noqa: E402


def _fake_run(monkeypatch, output):
    seen = {}

    def fake(cmd, timeout=None, **kw):
        seen["cmd"] = cmd
        return 0, output

    monkeypatch.setattr(prov, "_run", fake)
    return seen


def test_reads_the_machines_own_config(monkeypatch):
    """The truth is config.yaml inside the machine, not state on our side."""
    seen = _fake_run(monkeypatch, "True\n")
    assert prov._hermes_webhook_enabled("ws-1") is True
    joined = " ".join(seen["cmd"])
    assert "ws-1" in joined
    assert "config.yaml" in joined
    assert "webhook" in joined


def test_disabled_reads_false(monkeypatch):
    _fake_run(monkeypatch, "False\n")
    assert prov._hermes_webhook_enabled("ws-1") is False


def test_unreadable_config_is_not_enabled(monkeypatch):
    """An unreadable config must never be the reason a bot keeps polling.

    This decides whether the unit is torn down, so the failure has to fall on
    the side of doing what was asked rather than leaving a gateway behind.
    """
    _fake_run(monkeypatch, "")
    assert prov._hermes_webhook_enabled("ws-1") is False

    _fake_run(monkeypatch, "python3: command not found\n")
    assert prov._hermes_webhook_enabled("ws-1") is False


def test_gateway_is_not_gated_on_telegram_alone():
    """Pins the actual condition, so it cannot silently narrow again."""
    src = Path(prov.__file__).read_text(encoding="utf-8")
    assert "if telegram_enabled or webhook_enabled:" in src
    assert "if telegram_enabled:\n            rc, gout, gerr" not in src
