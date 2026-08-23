"""Reserved service ports."""
from mmd.models import PortKind
from mmd import ports as P


def test_ssh_and_rdp_both_have_a_reserved_kind():
    assert PortKind.SSH.value == "ssh"
    assert PortKind.RDP.value == "rdp"


def test_the_service_map_points_at_the_right_internal_ports():
    assert P.SERVICE_INTERNAL[PortKind.SSH] == 22
    assert P.SERVICE_INTERNAL[PortKind.RDP] == 3389


def test_every_service_kind_has_an_internal_port():
    for kind in (PortKind.SSH, PortKind.RDP):
        assert kind in P.SERVICE_INTERNAL


def test_user_ports_are_not_in_the_service_map():
    assert PortKind.USER not in P.SERVICE_INTERNAL


def test_releasing_a_reserved_port_is_refused():
    # The address a customer wrote into their SSH config must not be
    # removable, by them or by an accidental call.
    class FakeRow:
        kind = PortKind.SSH
    try:
        P.release(None, FakeRow())
    except P.PortError as e:
        assert e.code == "port_reserved"
    else:
        raise AssertionError("a reserved port was released")
