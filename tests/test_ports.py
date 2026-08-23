"""Published-port allocation."""
import pytest

from mmd import ports as PORTS


def test_range_sits_below_the_ephemeral_range():
    # Overlapping the kernel's ephemeral range would let an outbound
    # connection's source port collide with a customer's reserved port.
    assert PORTS.PORT_RANGE_END < 32768
    assert PORTS.PORT_RANGE_START >= 1024


def test_range_is_ordered_and_large_enough_to_be_useful():
    assert PORTS.PORT_RANGE_START < PORTS.PORT_RANGE_END
    assert PORTS.PORT_RANGE_END - PORTS.PORT_RANGE_START > 1000


def test_a_busy_host_port_is_reported_as_unavailable():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("0.0.0.0", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert PORTS._host_port_free(port) is False
    finally:
        s.close()


def test_an_unused_port_is_reported_as_free():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("0.0.0.0", 0))
    port = s.getsockname()[1]
    s.close()
    assert PORTS._host_port_free(port) is True


def test_port_errors_carry_a_translatable_code():
    e = PORTS.PortError("boom", "port_limit")
    assert e.code == "port_limit"


def test_port_error_has_a_default_code():
    assert PORTS.PortError("boom").code == "port_error"


def test_ssh_and_docker_api_are_flagged_as_risky_to_publish():
    assert 22 in PORTS.DISCOURAGED_INTERNAL
    assert 2375 in PORTS.DISCOURAGED_INTERNAL


def test_a_workspace_cannot_publish_unlimited_ports():
    assert 1 <= PORTS.MAX_PORTS_PER_WORKSPACE <= 20
