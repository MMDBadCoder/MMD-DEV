"""Every published port forwards TCP and UDP, and carries two addresses.

Two changes, asked for together:

  * The customer no longer picks a protocol. They know what their service
    speaks and we do not, and asking produced "my UDP service does not answer"
    against a rule that had only ever been written for TCP.
  * Each of their ports is shown twice - once on the bare host, once with their
    own name in front of the SAME port number.

The second is worth being precise about, because it looks like the start of a
slippery slope back to name-based routing and is not. `ali.<domain>:24815`
works because every name under the domain resolves to this host and the DNAT
rule keys on the PORT. The name carries no routing information at all: nothing
in a TCP or UDP packet transmits the hostname the customer typed. It is a
readable way to write the same address, and the port is doing the work.
"""
import os

os.environ.setdefault("MMD_DATABASE_URL", "sqlite://")
os.environ.setdefault("MMD_SECRET_KEY", "test-only")

import pytest                                              # noqa: E402
from fastapi.testclient import TestClient                  # noqa: E402
from sqlalchemy import create_engine, select               # noqa: E402
from sqlalchemy.orm import sessionmaker                     # noqa: E402
from sqlalchemy.pool import StaticPool                      # noqa: E402

from mmd import app as appmod                               # noqa: E402
from mmd import ports as PORTS                              # noqa: E402
from mmd import service as svc                              # noqa: E402
from mmd.service import sync_published_ports as REAL_SYNC   # noqa: E402
from mmd.models import (Base, ExposedPort, PortKind, User,  # noqa: E402
                        UserStatus, Workspace, WorkspaceState)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(PORTS, "_host_port_free", lambda p: True)
    monkeypatch.setattr(svc, "sync_published_ports", lambda db: {"ok": True})

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    db = Local()
    u = User(email="a@example.com", password_hash="x",
             status=UserStatus.APPROVED, username="ali")
    db.add(u)
    db.commit()
    ws = Workspace(user_id=u.id, idx=3, incus_project="ws-3",
                   state=WorkspaceState.ON, mem_mib=2048)
    db.add(ws)
    db.commit()

    api = appmod.app
    api.dependency_overrides[appmod.get_session] = lambda: Local()
    api.dependency_overrides[appmod.current_user] = lambda: u
    yield TestClient(api), db, ws, u
    api.dependency_overrides.clear()


# --- both protocols --------------------------------------------------------
def test_publishing_a_port_forwards_both_protocols(env):
    client, *_ = env
    d = client.post("/api/workspace/ports", json={"internal_port": 8080}).json()
    assert d["protocol"] == PORTS.PROTO_BOTH
    assert d["protocols"] == ["tcp", "udp"]


def test_a_protocol_sent_by_an_old_client_is_ignored_not_rejected(env):
    """The field was removed from the page, but a script somebody wrote against
    the previous API must not start failing."""
    client, *_ = env
    d = client.post("/api/workspace/ports",
                    json={"internal_port": 9000, "protocol": "udp"}).json()
    assert d["protocols"] == ["tcp", "udp"]


def test_an_old_single_protocol_row_blocks_a_second_publication(env):
    """The migration may not have run yet when the API first serves traffic.
    An old TCP row must already count as this internal port being published."""
    client, db, ws, _ = env
    PORTS.allocate(db, ws.id, 8080, "tcp")
    r = client.post("/api/workspace/ports", json={"internal_port": 8080})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "port_duplicate"


def test_one_row_becomes_two_firewall_rules(env):
    client, db, ws, _ = env
    PORTS.allocate(db, ws.id, 8080)

    sent = {}
    svc.call_provisioner = lambda payload, timeout=None: sent.update(payload) or {"ok": True}
    REAL_SYNC(db)

    protos = sorted(m["protocol"] for m in sent["mappings"])
    assert protos == ["tcp", "udp"]
    # Same external port for both halves - it is one reservation.
    assert len({m["external_port"] for m in sent["mappings"]}) == 1


def test_the_reserved_rows_stay_tcp_only(env):
    """SSH and RDP are TCP services. A UDP rule there would forward to a port
    that never answers."""
    client, db, ws, _ = env
    PORTS.reserve_service_ports(db, ws.id)
    for kind in (PortKind.SSH, PortKind.RDP):
        row = db.scalar(select(ExposedPort).where(
            ExposedPort.workspace_id == ws.id, ExposedPort.kind == kind))
        assert row.protocol == "tcp"
        assert PORTS.expand(row.protocol) == ("tcp",)


def test_the_provisioner_never_receives_the_shorthand(env):
    """It re-validates everything and accepts only tcp or udp; `both` is ours."""
    client, db, ws, _ = env
    PORTS.allocate(db, ws.id, 8080)
    PORTS.reserve_service_ports(db, ws.id)

    sent = {}
    svc.call_provisioner = lambda payload, timeout=None: sent.update(payload) or {"ok": True}
    REAL_SYNC(db)
    assert all(m["protocol"] in ("tcp", "udp") for m in sent["mappings"]), sent


# --- the two addresses -----------------------------------------------------
def test_a_published_port_carries_both_addresses(env):
    client, *_ = env
    d = client.post("/api/workspace/ports", json={"internal_port": 8080}).json()
    port = d["external_port"]
    assert d["address"] == f"{appmod.CONFIG.endpoint_host}:{port}"
    assert d["host_address"] == f"ali.{appmod.CONFIG.domain}:8080"
    assert d["web_ready"] is False
    assert d["host_url"] is None


def test_https_uses_the_internal_port_name_while_raw_traffic_uses_external(env):
    client, *_ = env
    d = client.post("/api/workspace/ports", json={"internal_port": 8080}).json()
    assert d["host_address"] == f"ali.{appmod.CONFIG.domain}:8080"
    assert d["address"].endswith(f":{d['external_port']}")


def test_https_url_appears_only_after_vhost_reconciliation(env):
    client, db, *_ = env
    created = client.post("/api/workspace/ports", json={"internal_port": 8080}).json()
    row = db.get(ExposedPort, created["id"])
    row.web_ready = True
    db.commit()

    ready = [p for p in client.get("/api/workspace/ports").json()["ports"]
             if p["id"] == row.id][0]
    assert ready["host_url"] == f"https://ali.{appmod.CONFIG.domain}:8080"


def test_the_reserved_rows_keep_the_single_address_they_always_had(env):
    """Explicitly asked for: SSH and RDP are not to be touched."""
    client, db, ws, _ = env
    PORTS.reserve_service_ports(db, ws.id)
    rows = {p["kind"]: p for p in client.get("/api/workspace/ports").json()["ports"]}
    for kind in ("ssh", "rdp"):
        assert rows[kind]["host_address"] is None
        assert rows[kind]["address"].startswith(appmod.CONFIG.endpoint_host)


def test_an_account_with_no_username_gets_only_the_numeric_address(env):
    client, db, ws, u = env
    PORTS.allocate(db, ws.id, 8080)
    u.username = None
    db.commit()
    row = [p for p in client.get("/api/workspace/ports").json()["ports"]
           if p["kind"] == "user"][0]
    assert row["host_address"] is None
    assert row["address"] is not None
