"""Database model for the MMD-DEV control plane.

Money is stored as integer MICRO-CREDITS (1 credit = 1_000_000 micro). Billing
accrues per-minute and settles hourly, so floats would drift and eventually
disagree with the ledger. Integers make the ledger the single source of truth.
"""
from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, ForeignKey, Index, Integer,
    JSON, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

MICRO = 1_000_000


class Base(DeclarativeBase):
    pass


class UserStatus(str, enum.Enum):
    PENDING = "pending"        # signed up, waiting for an admin
    APPROVED = "approved"      # workspace provisioned
    REJECTED = "rejected"
    SUSPENDED = "suspended"


class WorkspaceState(str, enum.Enum):
    """Lifecycle. `desired_state` is what the user asked for; `state` is what
    Incus actually reports. The worker reconciles one toward the other - which
    is also how a host reboot restores the right set of workspaces, with a
    fresh credit and capacity check rather than blindly autostarting."""
    PROVISIONING = "provisioning"
    OFF = "off"
    STARTING = "starting"
    ON = "on"
    STOPPING = "stopping"
    RESETTING = "resetting"    # being rebuilt from the golden image
    ARCHIVING = "archiving"
    ARCHIVED = "archived"      # instance destroyed, disk archived, restorable
    DELETING = "deleting"
    DELETED = "deleted"
    ERROR = "error"


class TicketStatus(str, enum.Enum):
    """Who the ticket is currently waiting on.

    Deliberately about *whose turn it is* rather than about how the operator
    feels: a customer opening the list wants to know whether they are waiting
    for an answer or the answer is waiting for them.
    """
    OPEN = "open"                # customer wrote last; waiting on support
    IN_PROGRESS = "in_progress"  # support is working on it
    ANSWERED = "answered"        # support wrote last; waiting on the customer
    CLOSED = "closed"


class TxKind(str, enum.Enum):
    GRANT = "grant"                    # admin adds credit
    CHARGE_HOUR = "charge_hour"        # hourly settlement, in arrears
    CHARGE_PARTIAL = "charge_partial"  # pro-rata on power-off mid-hour
    ADJUSTMENT = "adjustment"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, native_enum=False), default=UserStatus.PENDING, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))

    workspace: Mapped["Workspace"] = relationship(back_populates="user", uselist=False)
    account: Mapped["CreditAccount"] = relationship(back_populates="user", uselist=False)


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True)
    # Slot number: determines the Incus project name and the workspace's static
    # IP, so both stay stable for the life of the account.
    idx: Mapped[int] = mapped_column(Integer, unique=True)
    incus_project: Mapped[str] = mapped_column(String(64), unique=True)
    instance: Mapped[str] = mapped_column(String(64), default="ws")

    state: Mapped[WorkspaceState] = mapped_column(
        Enum(WorkspaceState, native_enum=False), default=WorkspaceState.PROVISIONING, index=True)
    desired_on: Mapped[bool] = mapped_column(Boolean, default=False)

    # MILLICORES, not cores: 0.5 vCPU is 500, so fractional sizes stay exact
    # integers and no float ever reaches a price calculation.
    cpu_milli: Mapped[int] = mapped_column(Integer, default=1000)
    mem_mib: Mapped[int] = mapped_column(Integer, default=1024)
    root_gib: Mapped[int] = mapped_column(Integer, default=6)
    docker_gib: Mapped[int] = mapped_column(Integer, default=4)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Start of the hour currently being accrued; settlement is in arrears.
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)

    # --- optional services -------------------------------------------------
    # Each is off until the customer asks for it. sshd costs ~5 MB idle and
    # xrdp ~4 MB, so the toggle is about exposure and intent rather than
    # resources - but a listener nobody wants should not be on the internet.
    ssh_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    ssh_keys: Mapped[str | None] = mapped_column(Text)     # authorized_keys body
    rdp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    rdp_installed: Mapped[bool] = mapped_column(Boolean, default=False)

    user: Mapped[User] = relationship(back_populates="workspace")

    @property
    def mem_gib(self) -> float:
        return self.mem_mib / 1024.0

    @property
    def cpu_cores(self) -> float:
        return self.cpu_milli / 1000.0

    @property
    def disk_gib(self) -> int:
        return self.root_gib + self.docker_gib


class CreditAccount(Base):
    __tablename__ = "credit_accounts"
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    # Signed: goes negative while an archived workspace still accrues storage.
    balance_micro: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    user: Mapped[User] = relationship(back_populates="account")

    @property
    def balance(self) -> float:
        return self.balance_micro / MICRO


class CreditTransaction(Base):
    __tablename__ = "credit_transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # SET NULL, not CASCADE: a charge is a financial record and must outlive
    # the workspace it was raised against.
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL"))
    kind: Mapped[TxKind] = mapped_column(Enum(TxKind, native_enum=False))
    amount_micro: Mapped[int] = mapped_column(BigInteger)   # negative = charge
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # The idempotency guarantee. A worker that crashes mid-settlement and
        # restarts will retry the same hour; this constraint makes the retry a
        # no-op instead of a double charge. Without it, every restart silently
        # bills users twice.
        UniqueConstraint("workspace_id", "period_start", "kind",
                         name="uq_charge_once_per_period"),
        Index("ix_tx_user_created", "user_id", "created_at"),
    )


class UsageSample(Base):
    """Raw scrape of Incus /1.0/metrics. Retained ~7 days; the authoritative
    record is the settled transaction, not these."""
    __tablename__ = "usage_samples"
    id: Mapped[int] = mapped_column(primary_key=True)
    # Ephemeral metering data - CASCADE, it dies with what it measured.
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    cpu_seconds_total: Mapped[float] = mapped_column()   # monotonic counter
    mem_bytes: Mapped[int] = mapped_column(BigInteger)   # gauge


class PortKind(str, enum.Enum):
    """Why a port exists.

    USER  - published by the customer, removable by them, counts toward the
            per-workspace limit.
    SSH   - reserved for the workspace's SSH service at provisioning time.
    RDP   - reserved for the workspace's remote desktop.

    Reserved ports are allocated once and never move, so the address a customer
    puts in their SSH config keeps working across power cycles, resizes and
    service restarts. They are not deletable and do not count toward the limit.
    """
    USER = "user"
    SSH = "ssh"
    RDP = "rdp"


class ExposedPort(Base):
    """A port inside a workspace published on the host's public address.

    The external port is allocated by the host from a fixed range and RESERVED
    for this workspace, so a developer's endpoint keeps working across power
    cycles instead of moving every time they switch on. It is released only
    when the developer removes it or the workspace is destroyed.
    """
    __tablename__ = "exposed_ports"
    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    internal_port: Mapped[int] = mapped_column(Integer)
    external_port: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    protocol: Mapped[str] = mapped_column(String(8), default="tcp")
    kind: Mapped[PortKind] = mapped_column(
        Enum(PortKind, native_enum=False), default=PortKind.USER, index=True)
    device: Mapped[str] = mapped_column(String(64))     # legacy; DNAT is by rule
    note: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("workspace_id", "internal_port", "protocol",
                         name="uq_one_mapping_per_internal_port"),
    )


class SshKey(Base):
    """One authorised public key.

    A table rather than a text blob on the workspace: customers add and remove
    keys individually (a new laptop, a colleague leaving), and each needs its
    own identity to be removable and its own fingerprint to be recognisable.
    """
    __tablename__ = "ssh_keys"
    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    key_type: Mapped[str] = mapped_column(String(48))
    body: Mapped[str] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(String(200))
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # The same key twice would be confusing to look at and pointless to
        # store; the second add is treated as already-present.
        UniqueConstraint("workspace_id", "fingerprint", name="uq_key_per_workspace"),
    )

    @property
    def line(self) -> str:
        return f"{self.key_type} {self.body}" + (f" {self.comment}" if self.comment else "")


class Setting(Base):
    """Runtime configuration: the rate card, overcommit ratios and host reserve.
    Kept in the DB so the admin panel can change them without a redeploy."""
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    # ON DELETE SET NULL: an audit record must outlive the account it refers
    # to. A plain FK makes the audit log *block* user deletion, which is
    # backwards - the trail should survive, not act as a lock.
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str | None] = mapped_column(String(128))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class Ticket(Base):
    """One support conversation.

    Status is the operator's to set, with two exceptions that are automatic
    because leaving them manual would silently lose messages: a customer reply
    always moves a ticket back to OPEN (including reopening a closed one), and
    a staff reply moves it to ANSWERED. Support that requires an operator to
    remember to flip a flag before the customer's message is visible as
    outstanding is support that drops tickets.
    """
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True)
    subject: Mapped[str] = mapped_column(String(200))
    status: Mapped[TicketStatus] = mapped_column(
        Enum(TicketStatus, name="ticket_status"),
        default=TicketStatus.OPEN, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    # Read marks, one per side. These drive the "new reply" badge; without them
    # a customer has no way to tell an answered ticket from one they have
    # already read, and the list becomes noise.
    user_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    staff_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship()
    messages: Mapped[list[TicketMessage]] = relationship(
        back_populates="ticket", cascade="all, delete-orphan",
        order_by="TicketMessage.id")

    __table_args__ = (Index("ix_tickets_status_updated", "status", "updated_at"),)


class TicketMessage(Base):
    __tablename__ = "ticket_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(
        ForeignKey("tickets.id", ondelete="CASCADE"), index=True)
    # SET NULL, like the audit log: deleting an operator account must not erase
    # the answers they gave, or a customer's own thread loses half its content.
    author_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"))
    # Recorded at write time rather than derived from the author's current role.
    # A customer later promoted to admin must not retroactively turn their old
    # questions into staff answers.
    from_staff: Mapped[bool] = mapped_column(Boolean, default=False)
    body: Mapped[str] = mapped_column(Text)
    # Application clock, not server_default=func.now(). Read marks are written
    # from Python, and comparing the two decides whether a reply shows as
    # unread; SQL's now() has different precision and, on some backends, is the
    # transaction start rather than the insert. One clock removes the question.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC),
        server_default=func.now(), index=True)

    ticket: Mapped[Ticket] = relationship(back_populates="messages")
    author: Mapped[User | None] = relationship()
