"""Baseline auth schema: users, capabilities, user_capabilities.

Deliberately minimal for A01 -- this is the baseline migration's target.
Full domain schema (vendors, orders, settlements, policies, etc. per C01/C03)
lands in A02 and beyond.

Design notes (see docs/adr/0001-runtime.md for the full rationale):
  - IDs are opaque strings (uuid4 hex), per C01 "stable opaque string IDs,
    independent of display names".
  - `version` is an integer aggregate version per C01, incremented whenever
    the row changes meaningfully. A01 does not yet implement a general
    optimistic-concurrency command envelope (that is C03/C06 territory for
    domain records) -- here it is just tracked on `users` as a convention
    seed for later tasks to build on.
  - Capabilities model backend-enforced authorization: a user's JWT carries
    only identity (sub=user_id), never a capability list. Every request that
    needs a capability re-reads user_capabilities from the database, so a
    client cannot forge or role-switch its own permissions by editing a
    token or local state.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _new_id() -> str:
    return uuid.uuid4().hex


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    capability_grants: Mapped[list[UserCapability]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="UserCapability.user_id",
    )


class Capability(Base):
    __tablename__ = "capabilities"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    code: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UserCapability(Base):
    __tablename__ = "user_capabilities"
    __table_args__ = (UniqueConstraint("user_id", "capability_id", name="uq_user_capability"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    user_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    capability_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("capabilities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Nullable: bootstrap seeding of the first admin grants capabilities
    # before any human actor exists to be the granter. AGENT_HANDOFF.md:
    # "Record the actual person/capability, never fabricate distinct
    # approvers" -- a null granted_by on the bootstrap row is honest about
    # that, not a fabricated approver.
    granted_by: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(foreign_keys=[user_id], back_populates="capability_grants")


# Known capability codes for v1. One initial user may hold all of them
# (AGENT_HANDOFF.md); they remain separately assignable so roles can split
# later without a schema change.
CAPABILITY_ADMIN = "admin"
CAPABILITY_RESOLVE_CASE = "resolve_case"
CAPABILITY_APPROVE_POLICY = "approve_policy"
CAPABILITY_APPROVE_PAYOUT = "approve_payout"
CAPABILITY_FINANCE_LEAD = "finance_lead"
# A02: required to call POST /api/v1/imports. Separate from CAPABILITY_ADMIN
# so ingestion access can later be granted without granting full admin.
CAPABILITY_IMPORT_SOURCE = "import_source"

ALL_CAPABILITY_CODES = (
    CAPABILITY_ADMIN,
    CAPABILITY_RESOLVE_CASE,
    CAPABILITY_APPROVE_POLICY,
    CAPABILITY_APPROVE_PAYOUT,
    CAPABILITY_FINANCE_LEAD,
    CAPABILITY_IMPORT_SOURCE,
)
