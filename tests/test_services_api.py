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
from mmd.models import (Base, ExposedPort, PortKind, User,  # noqa: E402
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
    u = User(email="new@example.com", password_hash="x", status=UserStatus.APPROVED)
    db.add(u)
    db.commit()
    ws = Workspace(user_id=u.id, idx=3, incus_project="ws-3", state=WorkspaceState.ON,
                   mem_mib=2048)
    db.add(ws)
    db.commit()

    api = appmod.app
    api.dependency_overrides[appmod.get_session] = lambda: Local()
    api.dependency_overrides[appmod.current_user] = lambda: u
    yield TestClient(api), db, ws, calls
    api.dependency_overrides.clear()


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
def test_connection_addresses_follow_the_host_the_request_arrived_on(env):
    """The dashboard moved from a bare IP to a domain, and the SSH and RDP
    addresses have to move with it.

    They are built from `request.url.hostname`, which Starlette takes from the
    Host header - so this holds only as long as the reverse proxy forwards it
    (`proxy_set_header Host $host`). If someone "simplifies" that away, the
    customer is handed an address pointing at the proxy's own loopback.
    """
    client, *_ = env
    d = client.get("/api/workspace/services", headers={"Host": "mmd-ai.ir"}).json()

    assert d["host"] == "mmd-ai.ir"
    assert d["ssh"]["address"] == f"mmd-ai.ir:{d['ssh']['port']}"
    assert d["rdp"]["address"] == f"mmd-ai.ir:{d['rdp']['port']}"
    assert d["ssh"]["command"] == f"ssh -p {d['ssh']['port']} dev@mmd-ai.ir"
    # Never the proxy's own address.
    assert "127.0.0.1" not in str(d)
    assert "localhost" not in str(d)




def test_published_ports_are_not_advertised_on_the_hsts_host(env):
    """The dashboard sends Strict-Transport-Security, and HSTS covers a host on
    EVERY port - not just 443.

    So advertising a customer's published port as `dashboard-domain:29562` makes
    their plain-HTTP app unreachable from any browser that has ever visited the
    dashboard: it silently rewrites the URL to https:// and the connection
    fails. The address must use a host no HSTS policy applies to.
    """
    from mmd.config import CONFIG
    client, db, ws, _ = env
    client.get("/api/workspace/services")          # allocate the reservations

    d = client.get("/api/workspace/ports", headers={"Host": "mmd-ai.ir"}).json()
    assert d["host"] == CONFIG.port_host
    assert d["host"] != "mmd-ai.ir"
    for row in d["ports"]:
        assert not row["address"].startswith("mmd-ai.ir:"), row["address"]
        assert row["address"] == f"{CONFIG.port_host}:{row['external_port']}"


def test_the_hsts_header_does_not_claim_subdomains():
    """includeSubDomains would extend the policy to any name a customer's app
    might later be served from, which is the same trap one level up."""
    import re
    from pathlib import Path
    script = (Path(__file__).resolve().parents[1] / "host" / "70-reverse-proxy.sh").read_text()
    hsts = re.search(r"Strict-Transport-Security[^\\]*", script)
    assert hsts, "no HSTS header configured"
    assert "includeSubDomains" not in hsts.group(0)
    assert "preload" not in hsts.group(0)
