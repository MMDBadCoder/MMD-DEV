"""Database model for the MMD-DEV control plane.

Money is stored as integer MICRO-CREDITS (1 credit = 1_000_000 micro). Billing
accrues per-minute and settles hourly, so floats would drift and eventually
disagree with the ledger. Integers make the ledger the single source of truth.
"""
from __future__ import annotations

import enum
from datetime import datetime

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
    ARCHIVING = "archiving"
    ARCHIVED = "archived"      # instance destroyed, disk archived, restorable
    DELETING = "deleting"
    DELETED = "deleted"
    ERROR = "error"


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
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    # Slot number: determines the Incus project name and the workspace's static
    # IP, so both stay stable for the life of the account.
    idx: Mapped[int] = mapped_column(Integer, unique=True)
    incus_project: Mapped[str] = mapped_column(String(64), unique=True)
    instance: Mapped[str] = mapped_column(String(64), default="ws")

    state: Mapped[WorkspaceState] = mapped_column(
        Enum(WorkspaceState, native_enum=False), default=WorkspaceState.PROVISIONING, index=True)
    desired_on: Mapped[bool] = mapped_column(Boolean, default=False)

    cores: Mapped[int] = mapped_column(Integer, default=1)
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

    user: Mapped[User] = relationship(back_populates="workspace")

    @property
    def mem_gib(self) -> float:
        return self.mem_mib / 1024.0

    @property
    def disk_gib(self) -> int:
        return self.root_gib + self.docker_gib


class CreditAccount(Base):
    __tablename__ = "credit_accounts"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
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
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    workspace_id: Mapped[int | None] = mapped_column(ForeignKey("workspaces.id"))
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
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    cpu_seconds_total: Mapped[float] = mapped_column()   # monotonic counter
    mem_bytes: Mapped[int] = mapped_column(BigInteger)   # gauge


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
