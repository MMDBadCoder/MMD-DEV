"""The Connections page's contract, and the AI endpoints.

The bug this pins down: a newly approved customer opened the SSH and RDP tabs
and both addresses were blank, because reserve_service_ports() was written but
never called from anywhere. The page promises a permanently reserved port from
the moment the machine exists, so the address must be there on the very first
load - before either service has ever been switched on.
"""
import os

import pytest

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

from fastapi.testclient import TestClient          # noqa: E402
from sqlalchemy import create_engine, select       # noqa: E402
from sqlalchemy.orm import sessionmaker            # noqa: E402
from sqlalchemy.pool import StaticPool             # noqa: E402

from mmd import app as appmod                      # noqa: E402
from mmd import ports as PORTS                     # noqa: E402
from mmd import service as svc                     # noqa: E402
from mmd.models import (Base, CreditAccount, ExposedPort, Notification, OpenRouterAccount, PortKind, Setting, User,  # noqa: E402
                        UserStatus, Workspace, WorkspaceState)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(PORTS, "_host_port_free", lambda p: True)
    calls = []
    monkeypatch.setattr(svc, "call_provisioner",
                        lambda payload, timeout=None: calls.append(payload) or {"ok": True})

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    db = Local()
    u = User(password_hash="x", status=UserStatus.APPROVED,
             is_admin=True)
    db.add(u)
    db.commit()
    ws = Workspace(user_id=u.id, idx=3, incus_project="ws-3", state=WorkspaceState.ON,
                   mem_mib=2048)
    db.add(ws)
    db.add(OpenRouterAccount(user_id=u.id, key_hash="supplier-hash",
                             key="secret", limit_dirty=False))
    db.commit()

    api = appmod.app
    api.dependency_overrides[appmod.get_session] = lambda: Local()
    api.dependency_overrides[appmod.current_user] = lambda: u
    yield TestClient(api), db, ws, calls
    api.dependency_overrides.clear()


def test_credit_grant_marks_the_account_openrouter_cap_for_immediate_refresh(env):
    client, db, ws, _ = env
    r = client.post(f"/api/admin/users/{ws.user_id}/credit",
                    json={"credits": 500_000, "note": "paid"})

    assert r.status_code == 200
    account = db.get(OpenRouterAccount, ws.user_id)
    db.refresh(account)
    assert account.limit_dirty is True


def test_openrouter_state_distinguishes_ledger_credit_from_supplier_sync(env):
    client, db, ws, _ = env
    account = db.get(OpenRouterAccount, ws.user_id)
    account.limit_dirty = True
    account.limit_usd = 7.5
    db.commit()

    state = client.get("/api/workspace/ai").json()["openrouter"]

    assert state["limit_sync_pending"] is True
    assert state["limit_usd"] == 7.5
    assert state["limit_synced_at"] is None


def test_admin_can_power_off_a_customer_workspace(env, monkeypatch):
    client, db, ws, _ = env

    class Incus:
        stopped = False

        async def stop(self, instance, project):
            assert (instance, project) == (ws.instance, ws.incus_project)
            self.stopped = True

        async def aclose(self):
            pass

    incus = Incus()
    monkeypatch.setattr(appmod, "_incus", lambda: incus)
    monkeypatch.setattr(svc, "settle_elapsed", lambda *args, **kwargs: None)

    r = client.post(f"/api/admin/workspaces/{ws.id}/power-off")

    assert r.status_code == 200
    assert incus.stopped is True
    db.refresh(ws)
    assert ws.state == WorkspaceState.OFF
    assert ws.desired_on is False


def test_openrouter_admin_settings_have_no_discount_and_use_supplier_names(env):
    client, db, ws, _ = env
    response = client.put("/api/admin/openrouter", json={
        "usd_to_toman": 175000,
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["usd_to_toman"] == 175000
    # The default model joined this endpoint when it became the ONE place a
    # model is chosen for every OpenRouter-backed service - Hermes, OpenClaw,
    # OpenCode and Open WebUI - rather than four settings to keep in step.
    assert set(data) == {"usd_to_toman", "discount_percent",
                         "default_model", "fallback_model"}
    assert data["discount_percent"] == 0


def test_capacity_settings_do_not_repeat_ai_configuration(env):
    client, db, ws, _ = env
    db.add_all([Setting(key="usd_to_toman", value="175000"),
                Setting(key="openrouter_guardrail_id", value="guardrail")])
    db.commit()

    data = client.get("/api/admin/settings").json()

    assert "usd_to_toman" not in data
    assert "openrouter_guardrail_id" not in data
    assert "overcommit_cpu" in data


def test_a_brand_new_machine_already_has_both_addresses(env):
    client, db, ws, _ = env
    d = client.get("/api/workspace/services").json()
    assert d["ssh"]["port"] and d["rdp"]["port"]
    assert d["ssh"]["address"] == f"{d['host']}:{d['ssh']['port']}"
    assert d["rdp"]["address"] == f"{d['host']}:{d['rdp']['port']}"


def test_the_ssh_command_is_filled_in_too(env):
    client, db, ws, _ = env
    d = client.get("/api/workspace/services").json()
    assert d["ssh"]["command"] == f"ssh -p {d['ssh']['port']} dev@{d['host']}"


def test_connection_launcher_receives_published_applications(env):
    client, db, ws, _ = env
    PORTS.allocate(db, ws.id, 8080)
    db.commit()
    apps = client.get("/api/workspace/services").json()["applications"]
    assert len(apps) == 1
    assert apps[0]["internal_port"] == 8080


def test_low_credit_notification_is_deduplicated_and_readable(env):
    client, db, ws, _ = env
    first = client.get("/api/notifications").json()
    second = client.get("/api/notifications").json()
    assert first["unread"] == second["unread"] == 1
    assert db.query(Notification).count() == 1
    notification_id = first["notifications"][0]["id"]
    assert client.post(f"/api/notifications/{notification_id}/read").status_code == 200
    assert client.get("/api/notifications").json()["unread"] == 0


def test_a_resolved_low_balance_warning_becomes_unread_if_it_returns(env):
    client, db, ws, _ = env
    first = client.get("/api/notifications").json()["notifications"][0]
    client.post(f"/api/notifications/{first['id']}/read")
    account = CreditAccount(user_id=ws.user_id, balance_micro=2_000 * 1_000_000)
    db.add(account); db.commit()
    assert client.get("/api/notifications").json()["notifications"] == []
    account.balance_micro = 0
    db.commit()
    returned = client.get("/api/notifications").json()
    assert returned["unread"] == 1
    assert returned["notifications"][0]["id"] == first["id"]


def test_the_addresses_do_not_change_between_loads(env):
    """The whole promise. A port that is re-rolled on each visit is not
    reserved, and a customer's saved SSH config silently stops working."""
    client, *_ = env
    first = client.get("/api/workspace/services").json()
    second = client.get("/api/workspace/services").json()
    assert first["ssh"]["port"] == second["ssh"]["port"]
    assert first["rdp"]["port"] == second["rdp"]["port"]


def test_the_firewall_is_told_once_not_on_every_load(env):
    """Allocating on read is only acceptable if the common path is a no-op;
    otherwise every page view pushes a full ruleset rewrite."""
    client, db, ws, calls = env
    client.get("/api/workspace/services")
    after_first = len(calls)
    client.get("/api/workspace/services")
    client.get("/api/workspace/services")
    assert after_first == 1
    assert len(calls) == 1


def test_a_half_reserved_machine_is_completed(env):
    client, db, ws, _ = env
    PORTS.allocate(db, ws.id, 22, "tcp", note=None, kind=PortKind.SSH)
    db.commit()
    d = client.get("/api/workspace/services").json()
    assert d["ssh"]["port"] and d["rdp"]["port"]
    rows = db.scalars(select(ExposedPort).where(ExposedPort.workspace_id == ws.id)).all()
    assert len(rows) == 2


def test_reserved_ports_are_not_deletable(env):
    client, db, ws, _ = env
    client.get("/api/workspace/services")
    row = db.scalars(select(ExposedPort).where(
        ExposedPort.workspace_id == ws.id, ExposedPort.kind == PortKind.SSH)).first()
    r = client.delete(f"/api/workspace/ports/{row.id}")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "port_reserved"


def test_a_failed_firewall_removal_keeps_the_reservation(env, monkeypatch):
    """Forgetting the row first leaves an unowned DNAT rule nobody can clean."""
    client, db, ws, _ = env
    row = PORTS.allocate(db, ws.id, 8080, "both", kind=PortKind.USER)
    monkeypatch.setattr(svc, "sync_published_ports", lambda db, **kw: {"ok": False})
    r = client.delete(f"/api/workspace/ports/{row.id}")
    assert r.status_code == 500
    assert db.get(ExposedPort, row.id) is not None


# --- RDP password ----------------------------------------------------------
def test_switching_the_desktop_on_without_a_password_is_refused(env):
    client, *_ = env
    r = client.post("/api/workspace/services/rdp", json={"enabled": True})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "rdp_needs_password"


def test_a_short_password_is_refused_with_a_translatable_code(env):
    client, *_ = env
    r = client.post("/api/workspace/services/rdp",
                    json={"enabled": True, "password": "short"})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "rdp_password_short"


def test_the_password_is_still_required_once_the_desktop_is_installed(env):
    """The regression this was written for: the check used to be skipped as
    soon as rdp_installed was true."""
    client, db, ws, _ = env
    ws.rdp_installed = True
    db.commit()
    r = client.post("/api/workspace/services/rdp", json={"enabled": True})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "rdp_needs_password"


def test_switching_the_desktop_off_needs_no_password(env):
    client, *_ = env
    r = client.post("/api/workspace/services/rdp", json={"enabled": False})
    assert r.status_code == 200


# --- AI --------------------------------------------------------------------
def test_ai_status_is_reported_for_a_running_machine(env, monkeypatch):
    monkeypatch.setattr(svc, "call_provisioner", lambda p, timeout=None: {
        "ok": True, "installed": True, "version": "2.1.0", "linked": True,
        "available": True, "expires_at": 123})
    client, *_ = env
    d = client.get("/api/workspace/ai").json()["claude"]
    assert d["installed"] and d["linked"] and d["machine_running"]


def test_ai_reports_honestly_when_the_machine_is_off(env, monkeypatch):
    """Answering "not installed" about a machine nobody can look inside would
    be a guess presented as a fact."""
    client, db, ws, _ = env
    ws.state = WorkspaceState.OFF
    db.commit()

    def boom(*a, **kw):
        raise AssertionError("must not call the provisioner for a stopped machine")

    monkeypatch.setattr(svc, "call_provisioner", boom)
    d = client.get("/api/workspace/ai").json()["claude"]
    assert d["machine_running"] is False


def test_ai_setup_is_refused_while_the_machine_is_off(env):
    client, db, ws, _ = env
    ws.state = WorkspaceState.OFF
    db.commit()
    r = client.post("/api/workspace/ai/claude", json={"action": "install"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "machine_off"


def test_only_the_two_ai_actions_are_accepted(env):
    client, *_ = env
    for bad in ("status", "delete", "install; rm -rf /", ""):
        assert client.post("/api/workspace/ai/claude",
                           json={"action": bad}).status_code == 422


def test_an_unsigned_in_host_is_surfaced_as_its_own_error(env, monkeypatch):
    monkeypatch.setattr(svc, "call_provisioner", lambda p, timeout=None: {
        "ok": False, "error": "the platform account is not signed in on this host"})
    client, *_ = env
    r = client.post("/api/workspace/ai/claude", json={"action": "install"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "ai_host_unlinked"


# --- the message a customer opened a ticket about --------------------------
def test_a_workspace_that_cannot_afford_an_hour_reports_a_code_and_numbers(env):
    """Reported as: "this text was English, it must be Persian". The dashboard
    printed the server's sentence verbatim, so it arrived in English, said
    "credits" where the product charges Toman, and used Western digits."""
    client, db, ws, _ = env
    ws.state = WorkspaceState.OFF
    db.commit()
    d = client.get("/api/workspace").json()

    assert d["can_power_on"] is False
    b = d["blocked"]
    assert b["code"] == "insufficient_credit"
    # The numbers, so the interface can format them as Toman in Persian digits.
    assert b["need"] > 0
    assert b["have"] == 0
    # And no prose anywhere in the payload.
    assert "blocked_reason" not in d
    assert not any(isinstance(v, str) and " " in v and v[:1].isupper()
                   for k, v in d.items() if k not in {"label", "status"})


def test_a_workspace_with_credit_is_not_blocked(env, monkeypatch):
    from mmd.models import CreditAccount
    client, db, ws, _ = env
    db.add(CreditAccount(user_id=ws.user_id, balance_micro=10_000 * 1_000_000))
    db.commit()
    ws.state = WorkspaceState.OFF
    db.commit()
    d = client.get("/api/workspace").json()
    assert d["blocked"] is None


# --- the address the customer is told to connect to ------------------------
# One host for everything a customer connects TO - published ports, SSH, RDP -
# and deliberately not the dashboard's own hostname. The dashboard sends HSTS,
# and HSTS covers a host on EVERY port, not just 443. Measured in a browser:
# with the policy stored, http://<dashboard-host>:28999 is forced to https and
# fails, while the same request to a subdomain loads normally.
def test_connection_addresses_ignore_the_dashboard_hostname(env):
    """Whatever host the dashboard is served on, the addresses handed out must
    come from configuration - otherwise a customer's plain-HTTP app becomes
    unreachable from any browser that has visited the dashboard."""
    from mmd.config import CONFIG
    client, *_ = env
    d = client.get("/api/workspace/services", headers={"Host": "mmd-ai.ir"}).json()

    assert d["host"] == CONFIG.endpoint_host
    assert d["host"] != "mmd-ai.ir"
    assert d["ssh"]["address"] == f"{CONFIG.endpoint_host}:{d['ssh']['port']}"
    assert d["rdp"]["address"] == f"{CONFIG.endpoint_host}:{d['rdp']['port']}"
    assert d["ssh"]["command"] == f"ssh -p {d['ssh']['port']} dev@{CONFIG.endpoint_host}"
    # Never the proxy's own address.
    assert "127.0.0.1" not in str(d)
    assert "localhost" not in str(d)


def test_published_ports_are_not_advertised_on_the_hsts_host(env):
    from mmd.config import CONFIG
    client, db, ws, _ = env
    client.get("/api/workspace/services")          # allocate the reservations

    d = client.get("/api/workspace/ports", headers={"Host": "mmd-ai.ir"}).json()
    assert d["host"] == CONFIG.endpoint_host
    assert d["host"] != "mmd-ai.ir"
    for row in d["ports"]:
        assert not row["address"].startswith("mmd-ai.ir:"), row["address"]
        assert row["address"] == f"{CONFIG.endpoint_host}:{row['external_port']}"


def test_a_published_tcp_port_gets_a_clickable_http_url(env):
    from mmd.models import PortKind
    from mmd.config import CONFIG
    client, db, ws, _ = env
    PORTS.allocate(db, ws.id, 8080, "tcp", note="my app", kind=PortKind.USER)
    db.commit()

    rows = client.get("/api/workspace/ports").json()["ports"]
    user = [r for r in rows if r["kind"] == "user"]
    assert user, "no user port in the listing"
    for r in user:
        assert r["url"] == f"http://{CONFIG.endpoint_host}:{r['external_port']}"


def test_reserved_ports_get_no_url(env):
    """SSH and RDP are not HTTP. A scheme in front of those would be wrong
    rather than merely unhelpful."""
    client, db, ws, _ = env
    client.get("/api/workspace/services")          # allocate the reservations
    rows = client.get("/api/workspace/ports").json()["ports"]
    reserved = [r for r in rows if r["kind"] in ("ssh", "rdp")]
    assert reserved, "no reserved ports in the listing"
    for r in reserved:
        assert r["url"] is None, r


def test_a_udp_port_gets_no_url(env):
    from mmd.models import PortKind
    client, db, ws, _ = env
    PORTS.allocate(db, ws.id, 5353, "udp", note="dns", kind=PortKind.USER)
    db.commit()
    rows = client.get("/api/workspace/ports").json()["ports"]
    udp = [r for r in rows if r["protocol"] == "udp"]
    assert udp and all(r["url"] is None for r in udp)


def test_the_hsts_header_does_not_claim_subdomains():
    """This is what makes a subdomain usable for customer ports at all. With
    includeSubDomains the dashboard's policy would cover ports.<domain> too, and
    every plain-HTTP app behind it would break."""
    import re
    from pathlib import Path
    script = (Path(__file__).resolve().parents[1] / "host" / "70-reverse-proxy.sh").read_text()
    hsts = re.search(r"Strict-Transport-Security[^\\]*", script)
    assert hsts, "no HSTS header configured"
    assert "includeSubDomains" not in hsts.group(0)
    assert "preload" not in hsts.group(0)


# --- the bug: enabling Hermes answered with a schema error -----------------
def test_hermes_accepts_enable_and_disable(env):
    """Reported as: clicking "enable Hermes" printed

        action String should match pattern '^(install|unlink)$'

    The endpoint validated its body with AiAction, whose vocabulary belongs to
    Claude Code. Pydantic rejected "enable" before the handler - which asked
    for exactly that word - ever ran, so the toggle could not be switched on
    at all.
    """
    client, db, ws, _ = env
    ws.user.username = "ali"
    db.commit()

    r = client.post("/api/workspace/ai/hermes", json={"action": "enable"})
    assert r.status_code == 200, r.text
    assert r.json()["hermes"]["enabled"] is True
    db.expire_all()
    assert ws.hermes_enabled is True

    r = client.post("/api/workspace/ai/hermes", json={"action": "disable"})
    assert r.status_code == 200, r.text
    assert r.json()["hermes"]["enabled"] is False


def test_hermes_still_refuses_claudes_verbs(env):
    client, db, ws, _ = env
    ws.user.username = "ali"
    db.commit()
    for bad in ("install", "unlink", "delete", ""):
        assert client.post("/api/workspace/ai/hermes",
                           json={"action": bad}).status_code == 422


def test_telegram_can_be_selected_during_hermes_activation_without_leaking_token(env):
    client, db, ws, _ = env
    ws.user.username = "ali"
    db.commit()
    token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcd"
    r = client.post("/api/workspace/ai/hermes", json={
        "action": "enable", "telegram_enabled": True,
        "telegram_token": token, "telegram_users": "123456789, 987654321",
    })
    assert r.status_code == 200, r.text
    assert token not in r.text
    db.refresh(ws)
    assert ws.hermes_telegram_enabled is True
    assert ws.hermes_telegram_token == token
    assert ws.hermes_telegram_users == "123456789,987654321"


def test_telegram_requires_a_real_bot_token_and_numeric_allowlist(env):
    client, db, ws, _ = env
    ws.user.username = "ali"
    db.commit()
    bad_token = client.post("/api/workspace/ai/hermes", json={
        "action": "enable", "telegram_enabled": True,
        "telegram_token": "not-a-token", "telegram_users": "123456789",
    })
    assert bad_token.status_code == 400
    assert bad_token.json()["detail"]["code"] == "telegram_bad_token"
    bad_users = client.post("/api/workspace/ai/hermes", json={
        "action": "enable", "telegram_enabled": True,
        "telegram_token": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcd",
        "telegram_users": "@username",
    })
    assert bad_users.status_code == 400
    assert bad_users.json()["detail"]["code"] == "telegram_bad_users"


def test_idempotent_hermes_enable_does_not_silently_disable_telegram(env):
    client, db, ws, _ = env
    ws.user.username = "ali"
    ws.hermes_telegram_enabled = True
    ws.hermes_telegram_installed = True
    db.commit()
    assert client.post("/api/workspace/ai/hermes", json={"action": "enable"}).status_code == 200
    db.refresh(ws)
    assert ws.hermes_telegram_enabled is True


def test_hermes_dashboard_is_not_ready_until_its_vhost_is_published(env):
    client, db, ws, _ = env
    ws.user.username = "ali"
    ws.hermes_enabled = True
    ws.hermes_key = "secret"
    ws.hermes_key_hash = "hash"
    ws.hermes_dash_user = "ali"
    ws.hermes_dash_password = "password"
    ws.hermes_vhost_ready = False
    db.commit()

    state = client.get("/api/workspace/ai").json()["hermes"]
    assert state["host"] == "hermes.ali.mmd-ai.ir"
    assert state["dashboard_ready"] is False
