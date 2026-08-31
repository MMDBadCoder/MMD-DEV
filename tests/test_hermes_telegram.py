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


# Hermes ships `hermes gateway install`, which writes a *user* unit named
# hermes-gateway into ~/.config/systemd/user and enables lingering. Ours is a
# system unit of the same name, so root's `systemctl disable --now
# hermes-gateway` reported success having touched only its own copy while the
# customer's kept polling. Telegram serves one getUpdates poller per token, so
# the second got HTTP 409 and the bot answered nobody - which is what a
# customer saw after turning OpenClaw off and Hermes back on.
def test_every_hermes_path_purges_the_vendor_user_gateway():
    purge = "HERMES_USER_GATEWAY_PURGE"
    # Defined once, used on disable, on telegram-enable and on telegram-off:
    # a path that skipped it would leave the duplicate running.
    assert PROVISIONER.count(f"{purge} = (") == 1
    assert PROVISIONER.count(f"{purge} +") == 3


def test_the_purge_reaches_the_user_manager_and_leaves_nothing_behind():
    block = PROVISIONER.split("HERMES_USER_GATEWAY_PURGE = (", 1)[1].split("\n)\n", 1)[0]
    # Without XDG_RUNTIME_DIR `systemctl --user` cannot find the bus and exits
    # non-zero having done nothing - silently, since the call tolerates failure.
    assert "XDG_RUNTIME_DIR=/run/user/$U" in block
    assert "systemctl --user disable --now hermes-gateway" in block
    assert "rm -f /home/dev/.config/systemd/user/hermes-gateway.service" in block
    # Restart=always means the unit can outlive its own removal.
    assert "pkill -u dev -f 'hermes_cli.main gateway'" in block
    # It must not reap the system gateway we are about to start, whose argv is
    # the console script rather than the module.
    assert "hermes_cli.main" not in PROVISIONER.split(
        'HERMES_GATEWAY_UNIT = """', 1)[1].split('"""', 1)[0]


def test_a_duplicate_poller_is_reported_as_a_failed_enable():
    # Both failures look the same to the customer - a silent bot - so both
    # have to fail the enable rather than being reported as success.
    assert "terminated by other getUpdates" in PROVISIONER
