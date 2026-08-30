"""The privileged Hermes gateway boundary validates and protects credentials."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVISIONER = (ROOT / "control" / "provisioner" / "provisioner.py").read_text()


def test_gateway_runs_as_the_workspace_user_and_restarts_with_the_machine():
    unit = PROVISIONER.split('HERMES_GATEWAY_UNIT = """', 1)[1].split('"""', 1)[0]
    assert "User=dev" in unit
    assert "ExecStart=/home/dev/.local/bin/hermes gateway" in unit
    assert "WantedBy=multi-user.target" in unit
    assert "Restart=on-failure" in unit
    assert "EnvironmentFile=/home/dev/.hermes/.env" in unit


def test_provisioner_revalidates_both_telegram_credentials():
    assert PROVISIONER.count('re.fullmatch(r"[0-9]{6,15}:[A-Za-z0-9_-]{20,}"') == 1
    assert PROVISIONER.count('re.fullmatch(r"[1-9][0-9]{4,14}(,[1-9][0-9]{4,14})*"') == 1


def test_gateway_credentials_share_the_existing_protected_environment():
    assert "TELEGRAM_BOT_TOKEN={telegram_token}" in PROVISIONER
    assert "TELEGRAM_ALLOWED_USERS={telegram_users}" in PROVISIONER
    assert "chmod 0600 /home/dev/.hermes/.env" in PROVISIONER


def test_disabling_hermes_also_stops_the_gateway_and_removes_its_environment():
    disable = PROVISIONER.split('if action == "disable":', 1)[1].split("key = req.get", 1)[0]
    assert "hermes-gateway" in disable
    assert "rm -f /home/dev/.hermes/.env" in disable


def test_gateway_startup_rejects_a_process_with_no_messaging_platform():
    assert "No messaging platforms enabled" in PROVISIONER
    assert "journalctl -u hermes-gateway" in PROVISIONER
