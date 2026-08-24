"""Factory reset: the confirmation, and what survives it.

A reset destroys everything on a customer's machine and cannot be undone, so the
tests that matter are the ones proving it cannot happen by accident - and that
the things a customer would be upset to lose alongside it are kept.
"""
import os
from pathlib import Path

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
from mmd.models import (Base, ExposedPort, PortKind, SshKey, User,  # noqa: E402
                        UserStatus, Workspace, WorkspaceState)
from mmd.security import hash_password             # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "correct-horse-battery"
EMAIL = "owner@example.com"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(PORTS, "_host_port_free", lambda p: True)
    sent = []

    def fake_provisioner(payload, timeout=None):
        sent.append(payload)
        return {"ok": True, "output": "workspace reset"}

    monkeypatch.setattr(svc, "call_provisioner", fake_provisioner)

    # The endpoint stops the machine afterwards; there is no Incus here.
    class FakeIncus:
        async def stop(self, *a, **kw):
            sent.append({"verb": "stop"})
        async def aclose(self):
            pass

    monkeypatch.setattr(appmod, "_incus", lambda: FakeIncus())

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    db = Local()
    u = User(email=EMAIL, password_hash=hash_password(PASSWORD),
             status=UserStatus.APPROVED)
    db.add(u)
    db.commit()
    ws = Workspace(user_id=u.id, idx=4, incus_project="ws-4",
                   state=WorkspaceState.OFF, mem_mib=2048,
                   ssh_enabled=True, ssh_keys="ssh-ed25519 AAAA... dev@laptop",
                   rdp_enabled=True, rdp_installed=True)
    db.add(ws)
    db.commit()
    PORTS.reserve_service_ports(db, ws.id)
    db.add(SshKey(workspace_id=ws.id, key_type="ssh-ed25519", body="AAAA",
                  comment="dev@laptop", fingerprint="SHA256:abc"))
    db.commit()

    api = appmod.app
    api.dependency_overrides[appmod.get_session] = lambda: Local()
    api.dependency_overrides[appmod.current_user] = lambda: u
    yield TestClient(api), db, ws, u, sent
    api.dependency_overrides.clear()


def _ok_body():
    return {"confirm": EMAIL, "password": PASSWORD}


# --- the confirmation ------------------------------------------------------
def test_the_wrong_password_is_refused(env):
    client, db, ws, *_ = env
    r = client.post("/api/workspace/reset",
                    json={"confirm": EMAIL, "password": "not-it"})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "bad_password"
    db.refresh(ws)
    assert ws.state is WorkspaceState.OFF


def test_the_wrong_typed_confirmation_is_refused(env):
    client, db, ws, *_ = env
    r = client.post("/api/workspace/reset",
                    json={"confirm": "someone@else.com", "password": PASSWORD})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "reset_confirm_mismatch"


def test_neither_field_may_be_omitted(env):
    client, *_ = env
    assert client.post("/api/workspace/reset",
                       json={"confirm": EMAIL}).status_code == 422
    assert client.post("/api/workspace/reset",
                       json={"password": PASSWORD}).status_code == 422
    assert client.post("/api/workspace/reset", json={}).status_code == 422


def test_an_empty_password_cannot_satisfy_it(env):
    """Guards against a client that sends "" for a field it did not collect."""
    client, *_ = env
    r = client.post("/api/workspace/reset", json={"confirm": EMAIL, "password": ""})
    assert r.status_code == 422


def test_the_typed_confirmation_ignores_case_and_padding_only(env):
    """A customer retyping their own address should not be defeated by a capital
    letter - but nothing else is accepted."""
    client, db, ws, *_ = env
    r = client.post("/api/workspace/reset",
                    json={"confirm": f"  {EMAIL.upper()}  ", "password": PASSWORD})
    assert r.status_code == 200


def test_a_refused_attempt_is_recorded(env):
    """Someone probing an unlocked browser should leave a trace in the account's
    own activity log."""
    from mmd.models import AuditLog
    client, db, ws, *_ = env
    client.post("/api/workspace/reset", json={"confirm": EMAIL, "password": "no"})
    actions = [a.action for a in db.scalars(select(AuditLog))]
    assert "reset_refused" in actions
    assert "reset_started" not in actions


# --- what it does ----------------------------------------------------------
def test_a_successful_reset_hands_the_machine_back_off(env):
    client, db, ws, u, sent = env
    r = client.post("/api/workspace/reset", json=_ok_body())
    assert r.status_code == 200, r.text
    db.refresh(ws)
    assert ws.state is WorkspaceState.OFF
    assert ws.desired_on is False
    assert ws.period_start is None


def test_it_asks_the_provisioner_for_the_same_size(env):
    client, db, ws, u, sent = env
    client.post("/api/workspace/reset", json=_ok_body())
    reset = next(p for p in sent if p.get("verb") == "reset")
    assert reset["idx"] == ws.idx
    assert reset["mem_mib"] == 2048
    assert reset["root_gib"] == ws.root_gib
    assert reset["docker_gib"] == ws.docker_gib


def test_the_reserved_addresses_survive(env):
    """They are what the customer saved in their SSH config. Losing them would
    make a reset indistinguishable from being handed a different machine."""
    client, db, ws, *_ = env
    before = {p.kind: p.external_port for p in db.scalars(select(ExposedPort))}
    client.post("/api/workspace/reset", json=_ok_body())
    after = {p.kind: p.external_port for p in db.scalars(select(ExposedPort))}
    assert before == after
    assert set(after) == {PortKind.SSH, PortKind.RDP}


def test_the_saved_public_keys_survive(env):
    """The keys are the customer's, not the machine's."""
    client, db, ws, *_ = env
    client.post("/api/workspace/reset", json=_ok_body())
    keys = db.scalars(select(SshKey)).all()
    assert [k.fingerprint for k in keys] == ["SHA256:abc"]


def test_the_services_are_switched_back_off(env):
    """They were installed on a filesystem that no longer exists. Leaving the
    flags set would show a desktop switch for software that is not there."""
    client, db, ws, *_ = env
    client.post("/api/workspace/reset", json=_ok_body())
    db.refresh(ws)
    assert ws.ssh_enabled is False
    assert ws.rdp_enabled is False
    assert ws.rdp_installed is False
    assert ws.ssh_keys is None


def test_a_running_machine_is_billed_for_the_time_it_ran(env, monkeypatch):
    """Otherwise a reset is the cheapest way to avoid a bill."""
    settled = []
    monkeypatch.setattr(svc, "settle_elapsed",
                        lambda db, ws, **kw: settled.append(kw))
    client, db, ws, *_ = env
    ws.state = WorkspaceState.ON
    db.commit()
    client.post("/api/workspace/reset", json=_ok_body())
    assert settled == [{"powered_on": True}]


def test_a_stopped_machine_is_not_billed(env, monkeypatch):
    settled = []
    monkeypatch.setattr(svc, "settle_elapsed",
                        lambda db, ws, **kw: settled.append(kw))
    client, *_ = env
    client.post("/api/workspace/reset", json=_ok_body())
    assert settled == []


def test_a_wedged_machine_can_be_reset(env):
    """ERROR is the state where starting over is most useful. Refusing there
    would leave the one situation with no self-service way out."""
    client, db, ws, *_ = env
    ws.state = WorkspaceState.ERROR
    ws.error = "ReadTimeout"
    db.commit()
    assert client.post("/api/workspace/reset", json=_ok_body()).status_code == 200
    db.refresh(ws)
    assert ws.state is WorkspaceState.OFF
    assert ws.error is None


def test_a_machine_mid_operation_cannot_be_reset(env):
    client, db, ws, *_ = env
    for bad in (WorkspaceState.PROVISIONING, WorkspaceState.ARCHIVED,
                WorkspaceState.RESETTING, WorkspaceState.STOPPING):
        ws.state = bad
        db.commit()
        r = client.post("/api/workspace/reset", json=_ok_body())
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "reset_bad_state"


def test_a_failed_reset_leaves_a_retryable_error(env, monkeypatch):
    monkeypatch.setattr(svc, "call_provisioner",
                        lambda p, timeout=None: {"ok": False, "output": "boom"})
    client, db, ws, *_ = env
    r = client.post("/api/workspace/reset", json=_ok_body())
    assert r.status_code == 500
    db.refresh(ws)
    assert ws.state is WorkspaceState.ERROR
    assert ws.error


# --- the script it drives --------------------------------------------------
RESET_SH = (ROOT / "workspace" / "ws-reset.sh").read_text()
LIB_SH = (ROOT / "workspace" / "ws-lib.sh").read_text()
CREATE_SH = (ROOT / "workspace" / "ws-create.sh").read_text()


def test_reset_refuses_to_become_provision():
    """Creating the project here would let a reset silently re-provision a
    workspace that had been deleted, outside admission and billing."""
    assert "reset is not provision" in RESET_SH
    assert "ws_create_project" not in RESET_SH


def test_the_docker_volume_is_destroyed_too():
    """Deleting the instance releases the device but not the volume, so images
    and containers would survive a reset the customer was told is clean."""
    assert "storage volume delete" in RESET_SH


def test_the_instance_definition_is_not_duplicated():
    """Two copies would drift, and the drift would show up as a reset machine
    that is subtly not what the customer bought."""
    assert "ws_create_instance" in LIB_SH
    assert "ws_create_instance" in RESET_SH
    assert "ws_create_instance" in CREATE_SH
    # The literal config block lives in exactly one place.
    assert CREATE_SH.count("security.idmap.isolated") == 0
    assert RESET_SH.count("security.idmap.isolated") == 0
    assert LIB_SH.count("security.idmap.isolated") == 1


def test_the_script_tolerates_a_half_destroyed_workspace():
    """Retrying is the recovery path when a reset dies partway, so every removal
    has to be conditional."""
    assert RESET_SH.count("incus info") >= 1
    assert "storage volume info" in RESET_SH


def test_the_verb_is_allowlisted():
    prov = (ROOT / "control" / "provisioner" / "provisioner.py").read_text()
    assert '"reset"' in prov
    assert 'WS_RESET = REPO / "workspace" / "ws-reset.sh"' in prov
