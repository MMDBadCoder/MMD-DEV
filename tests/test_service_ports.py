"""The two permanent addresses every workspace is promised.

The Connections page tells the customer their SSH and RDP ports are reserved
from the moment the machine exists and never change. That was only true for
workspaces someone had reserved by hand: reserve_service_ports() existed but
nothing called it, so a newly approved customer opened both tabs to an empty
address. These tests pin the promise down.
"""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from mmd import ports as PORTS
from mmd.models import (Base, ExposedPort, PortKind, User, UserStatus,
                        Workspace, WorkspaceState)


@pytest.fixture
def db(monkeypatch):
    # Allocation checks that the host port is free; in a test there is no host
    # to ask, and asking would make the result depend on what else is running.
    monkeypatch.setattr(PORTS, "_host_port_free", lambda p: True)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def ws(db):
    u = User(email="dev@example.com", password_hash="x", status=UserStatus.APPROVED)
    db.add(u)
    db.commit()
    w = Workspace(user_id=u.id, idx=1, incus_project="ws-1", state=WorkspaceState.OFF)
    db.add(w)
    db.commit()
    return w


def _reserved(db, ws):
    return {p.kind: p.external_port for p in db.scalars(select(ExposedPort).where(
        ExposedPort.workspace_id == ws.id,
        ExposedPort.kind.in_([PortKind.SSH, PortKind.RDP])))}


def test_a_fresh_workspace_gets_both_reservations(db, ws):
    PORTS.reserve_service_ports(db, ws.id)
    db.commit()
    got = _reserved(db, ws)
    assert set(got) == {PortKind.SSH, PortKind.RDP}
    assert all(PORTS.PORT_RANGE_START <= p <= PORTS.PORT_RANGE_END for p in got.values())


def test_the_two_ports_are_different(db, ws):
    PORTS.reserve_service_ports(db, ws.id)
    db.commit()
    got = _reserved(db, ws)
    assert got[PortKind.SSH] != got[PortKind.RDP]


def test_reserving_twice_never_moves_an_address(db, ws):
    """A customer may already have saved the address in an SSH config. Calling
    this again - which now happens on every Connections page load - must be a
    no-op, not a re-roll."""
    first = PORTS.reserve_service_ports(db, ws.id)
    db.commit()
    second = PORTS.reserve_service_ports(db, ws.id)
    db.commit()
    assert first == second
    assert len(_reserved(db, ws)) == 2


def test_a_half_reserved_workspace_is_completed_not_duplicated(db, ws):
    """The state a partially-migrated machine can be left in: SSH reserved by
    an older code path, RDP missing."""
    PORTS.allocate(db, ws.id, 22, "tcp", note=None, kind=PortKind.SSH)
    db.commit()
    ssh_before = _reserved(db, ws)[PortKind.SSH]

    PORTS.reserve_service_ports(db, ws.id)
    db.commit()

    got = _reserved(db, ws)
    assert len(got) == 2
    assert got[PortKind.SSH] == ssh_before


def test_two_workspaces_never_share_an_external_port(db, ws):
    other_user = User(email="two@example.com", password_hash="x",
                      status=UserStatus.APPROVED)
    db.add(other_user)
    db.commit()
    w2 = Workspace(user_id=other_user.id, idx=2, incus_project="ws-2",
                   state=WorkspaceState.OFF)
    db.add(w2)
    db.commit()

    a = PORTS.reserve_service_ports(db, ws.id)
    db.commit()
    b = PORTS.reserve_service_ports(db, w2.id)
    db.commit()
    assert set(a.values()).isdisjoint(b.values())


def test_reserved_ports_map_to_the_right_services(db, ws):
    PORTS.reserve_service_ports(db, ws.id)
    db.commit()
    rows = {p.kind: p.internal_port for p in db.scalars(select(ExposedPort))}
    assert rows[PortKind.SSH] == 22
    assert rows[PortKind.RDP] == 3389
