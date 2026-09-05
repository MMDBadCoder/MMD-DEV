"""Two more agents on the AI page, each shaped like the one it resembles.

Codex is Claude Code again: a CLI the PLATFORM signs in on its own host, whose
grant is copied into the customer's machine. Nothing about it is stored - the
state is probed, because a stored copy is a second thing that can disagree with
the machine.

OpenClaw is Hermes again: a service the worker installs INTO the workspace,
with a dashboard of its own on a name the reconciler publishes. It spends the
customer's existing managed OpenRouter key, which is the whole reason it needs
no second supplier integration and cannot become a second way to spend money.

The tests below are mostly about the boundaries between those two shapes, since
that is where a wrong assumption would do damage.
"""
import os

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

import pytest                                             # noqa: E402
from fastapi.testclient import TestClient                 # noqa: E402
from sqlalchemy import create_engine                      # noqa: E402
from sqlalchemy.orm import sessionmaker                    # noqa: E402
from sqlalchemy.pool import StaticPool                     # noqa: E402

from mmd import app as appmod                              # noqa: E402
from mmd import service as svc                             # noqa: E402
from mmd import usernames                                  # noqa: E402
from mmd.models import (Base, OpenRouterAccount, User, UserStatus, Workspace,  # noqa: E402
                        WorkspaceState)


@pytest.fixture
def env(monkeypatch):
    calls = []

    def fake_provisioner(payload, timeout=None):
        calls.append(payload)
        return {"ok": True, "installed": True, "version": "1.0.0",
                "linked": True, "available": True, "expires_at": 1800000000}

    monkeypatch.setattr(svc, "call_provisioner", fake_provisioner)

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Local()
    u = User(password_hash="x",
             status=UserStatus.APPROVED, username="ali")
    db.add(u)
    db.commit()
    ws = Workspace(user_id=u.id, idx=3, incus_project="ws-3",
                   state=WorkspaceState.ON, mem_mib=2048)
    db.add(ws)
    db.add(OpenRouterAccount(user_id=u.id, key="sk-or-test", key_hash="or-hash",
                             credit_blocked=False))
    db.commit()

    api = appmod.app
    api.dependency_overrides[appmod.get_session] = lambda: Local()
    api.dependency_overrides[appmod.current_user] = lambda: u
    yield TestClient(api), db, ws, u, calls
    api.dependency_overrides.clear()


# --- both appear on the page ----------------------------------------------
def test_the_ai_endpoint_reports_account_and_workspace_services(env):
    client, *_ = env
    d = client.get("/api/workspace/ai").json()
    assert set(d) == {"has_workspace", "openrouter", "claude", "hermes", "codex", "openclaw",
                      "opencode", "openwebui"}


@pytest.mark.parametrize("service", ["opencode", "openwebui"])
def test_a_customer_can_enable_each_openrouter_backed_web_service(env, service):
    client, db, ws, *_ = env
    response = client.post(f"/api/workspace/managed-ai/{service}",
                           json={"action": "enable"})
    assert response.status_code == 200
    db.refresh(ws)
    assert getattr(ws, f"{service}_enabled") is True
    state = response.json()[service]
    assert state["needs_openrouter"] is False
    assert state["host"].startswith("opencode." if service == "opencode" else "openweb.")


def test_openrouter_credentials_are_never_returned_in_managed_service_state(env):
    client, *_ = env
    body = client.get("/api/workspace/ai").json()
    assert "sk-or-test" not in str(body["opencode"])
    assert "sk-or-test" not in str(body["openwebui"])


@pytest.mark.parametrize("service", ["opencode", "openwebui"])
def test_managed_web_is_ready_only_after_its_hostname_is_published(env, service):
    client, db, ws, *_ = env
    setattr(ws, f"{service}_enabled", True)
    setattr(ws, f"{service}_installed", True)
    db.commit()
    waiting = client.get("/api/workspace/ai").json()[service]
    assert waiting["installed"] is True
    assert waiting["vhost_ready"] is False
    assert waiting["ready"] is False

    setattr(ws, f"{service}_vhost_ready", True)
    db.commit()
    ready = client.get("/api/workspace/ai").json()[service]
    assert ready["ready"] is True

    setattr(ws, f"{service}_error", "failed health check")
    db.commit()
    assert client.get("/api/workspace/ai").json()[service]["ready"] is False


def test_openwebui_reports_its_memory_requirement(env):
    client, db, ws, *_ = env
    ws.mem_mib = 1024
    db.commit()
    state = client.get("/api/workspace/ai").json()["openwebui"]
    assert state["needs_memory"] is True
    assert state["minimum_memory_mib"] == 2048

    ws.mem_mib = 2048
    db.commit()
    assert client.get("/api/workspace/ai").json()["openwebui"]["needs_memory"] is False


def test_opencode_credentials_are_streamed_into_a_regular_file(monkeypatch):
    """Incus gives exec a pipe for stdin, so reopening `/dev/stdin` as an
    `install` source fails on the real host although a mocked installer passes."""
    import importlib.util
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "managed_web_prov", root / "control" / "provisioner" / "provisioner.py")
    prov = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prov)
    calls = []

    def fake_run(command, timeout=0, stdin_text=None):
        calls.append((command, stdin_text))
        return True, ""

    monkeypatch.setattr(prov, "_run", fake_run)
    result = prov._verb_ai_managed_web("ws-1", {
        "service": "opencode", "action": "install",
        "openrouter_key": "sk-or-secret", "password": "strong-password",
    })

    assert result["ok"] is True
    credential_call = next(c for c in calls if c[1] and "openrouter" in c[1])
    command = credential_call[0][-1]
    assert "cat > /home/dev/.local/share/opencode/auth.json" in command
    assert "/dev/stdin" not in command
    assert json.loads(credential_call[1]) == {
        "openrouter": {"type": "api", "key": "sk-or-secret"}}


# --- Codex: the Claude shape ----------------------------------------------
def test_codex_status_is_probed_not_stored(env):
    client, db, ws, _u, calls = env
    d = client.get("/api/workspace/ai").json()["codex"]
    assert d["linked"] is True and d["version"] == "1.0.0"
    assert any(c.get("verb") == "ai_codex" and c.get("action") == "status"
               for c in calls)


def test_codex_reports_nothing_about_a_stopped_machine(env):
    """Nothing can be inspected inside a stopped machine, and saying so beats
    reporting "not installed" about a machine that may well have it."""
    client, db, ws, _u, calls = env
    ws.state = WorkspaceState.OFF
    db.commit()
    d = client.get("/api/workspace/ai").json()["codex"]
    assert d["machine_running"] is False
    assert not any(c.get("verb") == "ai_codex" for c in calls)


def test_codex_install_and_unlink_reach_the_provisioner(env):
    client, db, ws, _u, calls = env
    for action in ("install", "unlink"):
        r = client.post("/api/workspace/ai/codex", json={"action": action})
        assert r.status_code == 200, r.text
    verbs = [(c.get("verb"), c.get("action")) for c in calls]
    assert ("ai_codex", "install") in verbs
    assert ("ai_codex", "unlink") in verbs


def test_codex_refuses_claudes_own_wrong_verbs(env):
    """Same lesson as the Hermes toggle: two AI features are not one feature.
    Codex takes install/unlink; enable/disable belong to the other shape."""
    client, *_ = env
    for bad in ("enable", "disable", "status", ""):
        assert client.post("/api/workspace/ai/codex",
                           json={"action": bad}).status_code == 422


def test_codex_is_refused_while_the_machine_is_off(env):
    client, db, ws, _u, _c = env
    ws.state = WorkspaceState.OFF
    db.commit()
    r = client.post("/api/workspace/ai/codex", json={"action": "install"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "machine_off"


def test_an_unsigned_in_host_is_surfaced_as_its_own_error(env, monkeypatch):
    monkeypatch.setattr(svc, "call_provisioner", lambda p, timeout=None: {
        "ok": False, "error": "the platform account is not signed in on this host"})
    client, *_ = env
    r = client.post("/api/workspace/ai/codex", json={"action": "install"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "ai_host_unlinked"


# --- OpenClaw: the Hermes shape -------------------------------------------
def test_openclaw_cannot_be_enabled_without_the_managed_key(env):
    """It spends the OpenRouter key. Without one there is nothing to configure
    it with, and a gateway that cannot reach a model is a dashboard that only
    produces errors."""
    client, db, ws, u, _c = env
    account = db.get(OpenRouterAccount, u.id)
    account.key = None
    account.key_hash = None
    db.commit()
    r = client.post("/api/workspace/ai/openclaw", json={"action": "enable"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "needs_openrouter"


def test_openclaw_enable_records_intent_and_does_not_install(env):
    """Installing is an npm download and a service start - minutes of work that
    must not be held open on a customer's HTTP request."""
    client, db, ws, _u, calls = env
    r = client.post("/api/workspace/ai/openclaw", json={"action": "enable"})
    assert r.status_code == 200, r.text
    d = r.json()["openclaw"]
    assert d["enabled"] is True and d["ready"] is False
    db.expire_all()
    assert ws.openclaw_enabled is True
    assert not any(c.get("verb") == "ai_openclaw" for c in calls)


def test_disabling_clears_the_password_immediately(env):
    """So the interface stops showing a secret the moment the customer switches
    it off, rather than when the worker next runs."""
    client, db, ws, _u, _c = env
    ws.openclaw_enabled = True
    ws.openclaw_installed = True
    ws.openclaw_password = "sekrit"
    db.commit()

    r = client.post("/api/workspace/ai/openclaw", json={"action": "disable"})
    assert r.status_code == 200
    assert r.json()["openclaw"]["password"] is None
    db.expire_all()
    assert ws.openclaw_password is None


def test_openclaw_refuses_the_cli_verbs(env):
    client, db, ws, _u, _c = env
    ws.hermes_key = "sk-or-test"
    db.commit()
    for bad in ("install", "unlink", ""):
        assert client.post("/api/workspace/ai/openclaw",
                           json={"action": bad}).status_code == 422


def test_the_dashboard_address_is_only_offered_once_it_exists(env):
    """`enabled` is intent; `ready` is the worker having actually started the
    gateway. Publishing the address on intent alone would hand the customer a
    link that 502s."""
    client, db, ws, _u, _c = env
    ws.hermes_key = "sk-or-test"
    ws.openclaw_enabled = True
    db.commit()
    d = client.get("/api/workspace/ai").json()["openclaw"]
    assert d["ready"] is False

    ws.openclaw_installed = True
    ws.openclaw_password = "sekrit"
    db.commit()
    d = client.get("/api/workspace/ai").json()["openclaw"]
    assert d["ready"] is True
    assert d["host"] == f"openclaw.ali.{appmod.CONFIG.domain}"


def test_an_account_with_no_username_has_nowhere_to_publish(env):
    client, db, ws, u, _c = env
    u.username = None
    db.commit()
    assert client.get("/api/workspace/ai").json()["openclaw"]["host"] is None
    r = client.post("/api/workspace/ai/openclaw", json={"action": "enable"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "no_username"


def test_the_error_is_a_flag_not_the_english_text(env):
    """The stored value is whatever npm or systemd said - useful to an
    operator, meaningless and alarming to a customer reading a Persian page."""
    client, db, ws, _u, _c = env
    ws.openclaw_error = "npm ERR! code E404 at /usr/lib/node_modules"
    db.commit()
    d = client.get("/api/workspace/ai").json()["openclaw"]
    assert d["error"] is True
    assert "npm" not in str(d)


# --- the names are not available to customers -----------------------------
def test_the_service_names_cannot_be_taken_as_usernames():
    """`openclaw.<username>.<domain>` is the platform's; a customer called
    `openclaw` would read as one of ours."""
    for name in ("codex", "openclaw", "hermes", "claude"):
        with pytest.raises(usernames.UsernameError) as e:
            usernames.validate(name)
        assert e.value.code == "username_reserved"


# --- the port agrees everywhere -------------------------------------------
def test_the_gateway_port_is_the_same_number_in_every_component():
    """Three components have to agree about where the gateway listens: the
    provisioner writes the config, the API reports it, and the reconciler
    proxies to it. A disagreement is a 502 nobody can explain."""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    vhosts = load("mmd_vhosts", root / "host" / "mmd-vhosts.py")
    prov_src = (root / "control" / "provisioner" / "provisioner.py").read_text()

    assert vhosts.OPENCLAW_PORT == appmod.OPENCLAW_PORT
    assert f"OPENCLAW_PORT = {appmod.OPENCLAW_PORT}" in prov_src


# --- the sandbox that hid the credentials ---------------------------------
def test_the_provisioner_can_actually_see_the_codex_credentials():
    """The unit runs with ProtectHome=yes, which hides /root ENTIRELY - so the
    provisioner found no auth.json and the page told customers the platform was
    not signed in, while `codex` worked perfectly for the operator one shell
    away. Claude solves this with a read-only bind mount; Codex needs its own,
    and the environment variable that points at it.

    Both lines, because either alone is silently useless.
    """
    from pathlib import Path
    unit = (Path(__file__).resolve().parents[1]
            / "deploy" / "mmd-provisioner.service").read_text()
    assert "BindReadOnlyPaths=/root/.codex:/var/lib/mmd/host-codex/.codex" in unit
    assert "MMD_CODEX_HOST_HOME=/var/lib/mmd/host-codex" in unit


def test_the_mount_point_is_created_by_the_installer():
    """systemd will not create a missing bind-mount destination, and the unit
    fails to start without it."""
    from pathlib import Path
    script = (Path(__file__).resolve().parents[1]
              / "host" / "60-control-plane.sh").read_text()
    assert "/var/lib/mmd/host-codex/.codex" in script


def test_the_credential_reader_honours_the_relocated_home(tmp_path, monkeypatch):
    """It must read the BOUND path, not /root - otherwise the mount achieves
    nothing."""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("MMD_CODEX_HOST_HOME", str(tmp_path))
    home = tmp_path / ".codex"
    home.mkdir()
    (home / "auth.json").write_text(
        '{"auth_mode":"chatgpt","tokens":{"access_token":"a"},'
        '"last_refresh":"x","history":"SECRET"}')

    spec = importlib.util.spec_from_file_location(
        "prov", root / "control" / "provisioner" / "provisioner.py")
    prov = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prov)

    creds, why = prov._codex_credentials()
    assert creds is not None, why
    assert creds["auth_mode"] == "chatgpt"
    # Rebuilt from an allowlist, so anything alongside is dropped by
    # construction rather than by remembering to blocklist it.
    assert "history" not in creds


def test_a_row_with_no_password_is_not_treated_as_installed(env, monkeypatch):
    """Reported as: the tab says "installing" forever.

    `openclaw_installed` and `openclaw_password` can disagree. Disabling clears
    the password at once so the interface stops showing a secret, while
    `installed` is only cleared after the worker has actually removed the
    service - so a customer who switches off and straight back on lands with
    installed=True and no password. The reconcile guard checked `installed`
    alone, skipped that row forever, and nothing ever regenerated the password.
    """
    from mmd import worker
    client, db, ws, _u, _c = env
    ws.hermes_key = "sk-or-test"
    ws.openclaw_enabled = True
    ws.openclaw_installed = True
    ws.openclaw_password = None          # the state the bug produced
    db.commit()

    monkeypatch.setattr(worker, "SessionLocal", lambda: db)
    calls = []
    monkeypatch.setattr(worker.svc, "call_provisioner",
                        lambda p, timeout=None: calls.append(p) or {"ok": True})
    # The session is the fixture's; committing must not close it out from under.
    monkeypatch.setattr(db, "close", lambda: None)

    worker.openclaw_once()

    assert ws.openclaw_password, "no password was generated"
    assert any(c.get("action") == "install" for c in calls), \
        "the incomplete row was skipped instead of being reinstalled"


def test_a_complete_row_is_left_alone(env, monkeypatch):
    """The guard must still be a guard: a finished install is not reinstalled
    on every pass."""
    from mmd import worker
    client, db, ws, _u, _c = env
    ws.hermes_key = "sk-or-test"
    ws.openclaw_enabled = True
    ws.openclaw_installed = True
    ws.openclaw_password = "already-set"
    db.commit()

    monkeypatch.setattr(worker, "SessionLocal", lambda: db)
    calls = []
    monkeypatch.setattr(worker.svc, "call_provisioner",
                        lambda p, timeout=None: calls.append(p) or {"ok": True})
    monkeypatch.setattr(db, "close", lambda: None)

    worker.openclaw_once()
    assert calls == []


def test_the_gateway_unit_restarts_on_a_clean_exit():
    """OpenClaw restarts itself by exiting CLEANLY and expecting its supervisor
    to bring it back - "restart mode: full process restart (supervisor
    restart)". Under Restart=on-failure that exit code 0 reads as "finished
    successfully" and systemd leaves it stopped, so any configuration the
    gateway applies to itself takes the dashboard down until something else
    notices. Observed live: the unit sat `inactive` with nothing listening.
    """
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "prov", root / "control" / "provisioner" / "provisioner.py")
    prov = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prov)

    unit = prov.OPENCLAW_SERVICE
    assert "Restart=always" in unit
    assert "Restart=on-failure" not in unit
    # ...but still bounded, or a config it will never accept loops forever.
    assert "StartLimitBurst=" in unit
    # In [Unit]: systemd moved these in v230 and ignores them in [Service].
    # Split on the section HEADER, not the first mention - the [Unit] comment
    # explaining this names "[Service]" too.
    head = unit.split("\n[Service]")[0]
    assert "StartLimitBurst=" in head and "StartLimitIntervalSec=" in head


# --- the default model, and the shape OpenClaw actually needs -------------
def test_the_model_id_is_provider_prefixed():
    """OpenClaw addresses a model by PROVIDER-prefixed id: OpenRouter's
    `z-ai/glm-5.2` is `openrouter/z-ai/glm-5.2` here. Getting it wrong does not
    fail loudly - the gateway starts, answers, and quietly uses its own default,
    which may not be reachable with the customer's key."""
    from mmd import openclaw as oc
    # An operator will paste the id OpenRouter's own site shows.
    assert oc.normalise_model("z-ai/glm-5.2") == "openrouter/z-ai/glm-5.2"
    # ...and pasting the already-prefixed form must not double it.
    assert oc.normalise_model("openrouter/z-ai/glm-5.2") == "openrouter/z-ai/glm-5.2"
    assert oc.normalise_model("") == oc.DEFAULT_MODEL
    assert oc.DEFAULT_MODEL.startswith(oc.MODEL_PREFIX)


def test_install_refuses_without_a_model():
    """A gateway with no default model is the silent failure this guards: it
    runs, it answers, and it is not using the customer's supplier."""
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "prov", root / "control" / "provisioner" / "provisioner.py")
    prov = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prov)
    r = prov._verb_ai_openclaw("ws-1", {"action": "install",
                                        "openrouter_key": "k", "password": "p"})
    assert r["ok"] is False and "model" in r["error"]


def test_the_written_config_carries_what_the_gateway_needs():
    """Every key here was learned from a working installation, not guessed."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1]
           / "control" / "provisioner" / "provisioner.py").read_text()
    for key in ['"primary": model',                    # the default model
                '"openrouter:default"',                # the auth profile
                '"baseUrl": "https://openrouter.ai/api/v1"',
                '"mode": "password"',                  # gateway auth scheme
                '"tokenFile": OPENCLAW_TG_TOKEN']:     # telegram, by file
        assert key in src, f"the install config no longer writes {key}"


def test_the_telegram_token_is_a_file_not_a_config_value():
    """OpenClaw supports `tokenFile` natively, so the bot token never sits in a
    config a customer might paste into a support ticket."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1]
           / "control" / "provisioner" / "provisioner.py").read_text()
    assert "OPENCLAW_TG_TOKEN" in src
    assert 'stdin_text=telegram_token' in src, "the token reaches a command line"
    # Removed when switched off: a disabled channel must not leave a live token.
    assert f"rm -f {{OPENCLAW_TG_TOKEN}}" in src or "rm -f {OPENCLAW_TG_TOKEN}" in src


def test_telegram_uses_the_account_token_not_a_request_body(env):
    """One bot token, saved once under Account, reused by Hermes and OpenClaw.
    A second endpoint accepting tokens would be a second thing to trust."""
    client, db, ws, u, _c = env
    ws.openclaw_installed = True
    db.commit()
    r = client.post("/api/workspace/ai/openclaw/telegram", json={"action": "enable"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "telegram_not_configured"

    u.telegram_bot_token, u.telegram_user_id = "123:AA", "42"
    db.commit()
    r = client.post("/api/workspace/ai/openclaw/telegram", json={"action": "enable"})
    assert r.status_code == 200, r.text
    db.expire_all()
    assert ws.openclaw_telegram_enabled is True


def test_telegram_cannot_be_enabled_before_the_gateway_exists(env):
    client, db, ws, u, _c = env
    u.telegram_bot_token, u.telegram_user_id = "123:AA", "42"
    ws.openclaw_installed = False
    db.commit()
    r = client.post("/api/workspace/ai/openclaw/telegram", json={"action": "enable"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "openclaw_not_ready"


def test_one_bot_cannot_serve_two_agents(env):
    """Telegram allows a single `getUpdates` poller per bot token. With Hermes
    and OpenClaw both enabled on one token the channel reported

        enabled, configured, running, DISCONNECTED
        Conflict: terminated by other getUpdates request

    which is the worst kind of broken, because it looks switched on. Observed
    live before this guard existed."""
    client, db, ws, u, _c = env
    u.telegram_bot_token = "123456:AAbbccddeeffgghhiijjkkllmmnnoo"
    u.telegram_user_id = "375210989"      # the allowlist wants 5+ digits
    ws.openclaw_installed = True
    ws.hermes_telegram_enabled = True
    db.commit()

    r = client.post("/api/workspace/ai/openclaw/telegram", json={"action": "enable"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "telegram_bot_in_use"

    # ...and the other direction, or the race just runs the other way.
    ws.hermes_telegram_enabled = False
    ws.openclaw_telegram_enabled = True
    db.commit()
    r = client.post("/api/workspace/ai/hermes", json={
        "action": "enable", "telegram_enabled": True,
        "telegram_token": "123456:AAbbccddeeffgghhiijjkkllmmnnoo",
        "telegram_users": "375210989"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "telegram_bot_in_use"


def test_disabling_is_never_blocked_by_the_guard(env):
    """A customer must always be able to switch a channel OFF - otherwise the
    guard becomes a trap that leaves both stuck on."""
    client, db, ws, u, _c = env
    ws.hermes_telegram_enabled = True
    ws.openclaw_telegram_enabled = True
    ws.openclaw_installed = True
    db.commit()
    r = client.post("/api/workspace/ai/openclaw/telegram", json={"action": "disable"})
    assert r.status_code == 200


def test_telegram_is_never_configured_without_an_allowlist():
    """A bot with no policy answers ANY Telegram user who finds it, and this one
    is wired to an agent with a shell in the customer's workspace. The channel
    is refused rather than opened."""
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "prov", root / "control" / "provisioner" / "provisioner.py")
    prov = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prov)
    src = (root / "control" / "provisioner" / "provisioner.py").read_text()

    assert '"dmPolicy": "allowlist"' in src
    # BOTH lists. `allowFrom` decides who may DM; `ownerAllowFrom` separately
    # decides who may use owner-scoped slash commands, and it is namespaced by
    # channel. Setting only the first gives a customer a bot that talks to them
    # and then answers "You are not authorized to use this command".
    assert '"allowFrom": telegram_users' in src
    assert 'f"telegram:{u}"' in src
    assert '"ownerAllowFrom"' in src
    # An empty allowlist must disable the channel, not open it.
    assert "and bool(telegram_users)" in src


# The Telegram section is rendered once and shown on two tabs, so anything one
# service can report the other must be able to report too. OpenClaw used to
# pass its whole-service error into that section - a value that is never set
# while the section is visible - so a Telegram channel that failed to start sat
# behind a "preparing" pill indefinitely with nothing said.
def test_openclaw_reports_telegram_failures_separately_like_hermes():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert "openclaw_telegram_error" in (
        root / "control" / "mmd" / "models" / "__init__.py").read_text()
    assert "openclaw_telegram_error TEXT" in (
        root / "control" / "mmd" / "db.py").read_text()

    app = (root / "control" / "mmd" / "app.py").read_text()
    assert '"telegram_error": bool(ws.openclaw_telegram_error)' in app
    # Cleared when the customer retries, as the Hermes toggle does.
    assert "ws.openclaw_telegram_error = None" in app

    worker = (root / "control" / "mmd" / "worker.py").read_text()
    assert "ws.openclaw_telegram_error = (" in worker


def test_the_openclaw_tab_shows_the_telegram_error_not_the_service_error():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    ai = (root / "web" / "js" / "pages" / "ai.js").read_text()
    # The section is only rendered when the gateway is ready, so the service
    # error can never be true there - passing it said nothing, ever.
    assert "error: o.telegram_error," in ai
    assert "enabled: o.telegram_enabled, error: o.error," not in ai
