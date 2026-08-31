"""Allocation of externally reachable ports for workspaces.

A developer picks a port inside their workspace; the host picks a free port on
its public address, RESERVES it for that workspace, and forwards it. The
reservation persists across power cycles - an endpoint that moved every time
the machine restarted would be useless for a webhook or a demo link.

Allocation has to survive three separate races:
  * two users allocating at the same moment  -> the unique constraint on
    external_port is the real arbiter; we retry on conflict
  * a port already reserved but idle         -> excluded via the database
  * a port some host process is listening on -> excluded by probing the kernel

The range deliberately sits below the ephemeral range (32768-60999 on Linux)
so an allocation can never collide with an outbound connection's source port.
"""
from __future__ import annotations

import secrets
import socket

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import ExposedPort, PortKind

PORT_RANGE_START = 20000
PORT_RANGE_END = 29999
MAX_PORTS_PER_WORKSPACE = 5
ALLOC_ATTEMPTS = 40

# Ports inside the workspace that must not be published. Not a security
# boundary (the workspace is the developer's own machine) but a guard against
# accidentally putting a container's ssh or docker API on the public internet.
DISCOURAGED_INTERNAL = {22, 2375, 2376}


class PortError(RuntimeError):
    """Carries a stable code so the interface can translate the reason."""

    def __init__(self, message: str, code: str = "port_error"):
        super().__init__(message)
        self.code = code


# What a customer's port forwards. Every published port now carries BOTH
# protocols: the customer knows which one their service speaks, we do not, and
# asking them to pick produced a steady trickle of "my UDP service does not
# answer" against a rule that had only ever been written for TCP.
#
# The reserved SSH and RDP rows stay TCP-only - both protocols are defined as
# TCP services, and a UDP rule there would forward to a port nothing listens on.
PROTO_BOTH = "both"
PROTOCOLS = ("tcp", "udp", PROTO_BOTH)


def expand(protocol: str) -> tuple[str, ...]:
    """The wire protocols one row stands for."""
    return ("tcp", "udp") if protocol == PROTO_BOTH else (protocol,)


def _host_port_free(port: int) -> bool:
    """Is anything on the host already bound to this port?

    Checked by attempting the bind ourselves. Reading /proc/net/tcp would race
    and would miss IPv6-only or SO_REUSEPORT listeners.

    Both protocols are probed, because a reservation now forwards both. A port
    free for TCP but taken for UDP would produce a rule that silently loses the
    UDP half.
    """
    for family, addr in ((socket.AF_INET, ("0.0.0.0", port)),
                         (socket.AF_INET6, ("::", port))):
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            s = socket.socket(family, kind)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(addr)
            except OSError:
                return False
            finally:
                s.close()
    return True


def host_binds_tcp(port: int) -> bool:
    """Does something on this host already listen on this TCP port?

    Asked about a customer's INTERNAL port, and only to decide whether the
    hostname route can exist. `mmd-vhosts` serves that route with
    `listen <port>;`, so if the host already holds the port nginx cannot bind
    it - and an nginx that cannot bind ABANDONS THE WHOLE RELOAD, keeping the
    previous configuration. That does not just lose one customer's address: it
    silently freezes every later configuration change, including certbot's
    renewal hook.

    Measured on the live host: a customer published internal port 8000, which
    is uvicorn's, and every reload from that moment failed with
    `bind() to 0.0.0.0:8000 failed (98: Address already in use)`.

    TCP only, deliberately. `_host_port_free` also probes UDP because a
    reservation forwards both, but nginx only ever needs the TCP half, and
    refusing a hostname over a busy UDP port would be a false negative.
    """
    for family, addr in ((socket.AF_INET, ("0.0.0.0", port)),
                         (socket.AF_INET6, ("::", port))):
        s = socket.socket(family, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(addr)
        except OSError:
            return True
        finally:
            s.close()
    return False


def reserved_external(db: Session) -> set[int]:
    return set(db.scalars(select(ExposedPort.external_port)))


# The ports the built-in services listen on inside the workspace.
SERVICE_INTERNAL = {PortKind.SSH: 22, PortKind.RDP: 3389}


def allocate(db: Session, workspace_id: int, internal_port: int,
             protocol: str = PROTO_BOTH, note: str | None = None,
             kind: PortKind = PortKind.USER) -> ExposedPort:
    """Reserve a free external port and record the mapping.

    The row is committed BEFORE the proxy device is created, so a crash leaves
    a reservation with no forwarder (harmless, and reconciled) rather than a
    forwarder with no reservation (which would leak the port).
    """
    if not (1 <= internal_port <= 65535):
        raise PortError("Port must be between 1 and 65535.", "port_out_of_range")
    if protocol not in PROTOCOLS:
        raise PortError("Protocol must be tcp, udp or both.", "bad_protocol")

    # A customer publishes an INTERNAL port once. Before dual-protocol
    # mappings existed, TCP and UDP were separate rows and the protocol was
    # part of the duplicate check. Keeping that check would now let an old TCP
    # row and a new BOTH row expose the same service on two public addresses.
    # Reserved SSH/RDP rows are separate product-owned addresses, so they do
    # not prevent the customer deliberately publishing the same internal port.
    existing = db.scalar(select(ExposedPort).where(
        ExposedPort.workspace_id == workspace_id,
        ExposedPort.internal_port == internal_port,
        ExposedPort.kind == kind))
    if existing is not None:
        raise PortError(f"Port {internal_port} is already published.", "port_duplicate")

    # Only customer-published ports count toward the limit. The SSH and RDP
    # reservations are part of the machine, not something the customer chose to
    # spend an allowance on.
    if kind is PortKind.USER:
        count = len(list(db.scalars(select(ExposedPort).where(
            ExposedPort.workspace_id == workspace_id,
            ExposedPort.kind == PortKind.USER))))
        if count >= MAX_PORTS_PER_WORKSPACE:
            raise PortError(
                f"At most {MAX_PORTS_PER_WORKSPACE} ports.", "port_limit")

    taken = reserved_external(db)
    for _ in range(ALLOC_ATTEMPTS):
        candidate = secrets.randbelow(PORT_RANGE_END - PORT_RANGE_START + 1) + PORT_RANGE_START
        if candidate in taken or not _host_port_free(candidate):
            continue
        row = ExposedPort(
            workspace_id=workspace_id, internal_port=internal_port,
            external_port=candidate, protocol=protocol, kind=kind,
            device=f"pub-{protocol}-{internal_port}", note=note)
        db.add(row)
        try:
            db.commit()
            return row
        except IntegrityError:
            # Someone else won the same number between our check and commit.
            db.rollback()
            taken.add(candidate)
            continue
    raise PortError("No free external port available.", "port_exhausted")


def release(db: Session, row: ExposedPort) -> None:
    """Give a published port back. Reserved service ports are not releasable -
    the whole point is that a customer's SSH address never changes."""
    if row.kind is not PortKind.USER:
        raise PortError("Reserved ports cannot be removed.", "port_reserved")
    db.delete(row)
    db.commit()


def reserve_service_ports(db: Session, workspace_id: int) -> dict[str, int]:
    """Allocate the SSH and RDP reservations for a new workspace.

    Called once at provisioning. Idempotent: an existing reservation is
    returned rather than replaced, so re-running never moves an address a
    customer has already written down.
    """
    out: dict[str, int] = {}
    for kind, internal in SERVICE_INTERNAL.items():
        existing = db.scalar(select(ExposedPort).where(
            ExposedPort.workspace_id == workspace_id,
            ExposedPort.kind == kind))
        if existing is None:
            # Explicitly TCP: SSH and RDP are TCP services, and the default
            # is now "both", which would add a UDP rule to a port that will
            # never answer on it.
            existing = allocate(db, workspace_id, internal, "tcp",
                                note=None, kind=kind)
        out[kind.value] = existing.external_port
    return out
