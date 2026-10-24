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
# A03: required to call POST /api/v1/reconciliation/runs (triggers the
# deterministic matching engine). Separate from CAPABILITY_ADMIN for the
# same reason as CAPABILITY_IMPORT_SOURCE above.
CAPABILITY_RUN_RECONCILIATION = "run_reconciliation"
# A04: required to call POST /api/v1/settlements/calculate (triggers the
# deterministic settlement engine). Separate from CAPABILITY_ADMIN for the
# same reason as CAPABILITY_IMPORT_SOURCE/CAPABILITY_RUN_RECONCILIATION above.
CAPABILITY_CALCULATE_SETTLEMENT = "calculate_settlement"
# A08: record POs/invoices/credit notes and approve simulated supplier
# payments. Separate from CAPABILITY_APPROVE_PAYOUT (vendor payouts) so
# payables and vendor-payout authority can be split later.
CAPABILITY_MANAGE_PAYABLES = "manage_payables"
# A08: post journals, change finance (tax/schedule) configuration, issue
# vendor statements and run the daily close.
CAPABILITY_CLOSE_BOOKS = "close_books"
# A06: start agent work (document extraction / investigation) that spends
# Anthropic API credit. Separate so model spend can be granted on its own.
CAPABILITY_RUN_AGENTS = "run_agents"

# X0.1 (prevention extension): the five new capabilities TICKET_SPEC.md's
# X0.1 section lists by name, gating the new generic Approval mechanism's
# `action`s (see packages/domain/domain/approvals.py). Each is separate
# from CAPABILITY_ADMIN/CAPABILITY_FINANCE_LEAD for the same reason every
# earlier capability above is split out -- so a later deployment can grant
# one narrow approval authority without granting the others.
# R2: approve a scoped "internal interpretation" rule for a readiness
# conflict. Distinct from CAPABILITY_RECORD_AMENDMENT -- "signed amendment
# != approved interpretation" (TICKET_SPEC.md rule) extends to who may
# grant each, not just how they're stored/displayed.
CAPABILITY_APPROVE_INTERPRETATION = "approve_interpretation"
# R2: record that a signed amendment document resolves a readiness
# conflict (requires a document upload per R2's "Done when").
CAPABILITY_RECORD_AMENDMENT = "record_amendment"
# S4: accept/dispute/credit a supplier-invoice matching exception.
# Distinct from CAPABILITY_MANAGE_PAYABLES (A08) so invoice-exception
# authority can be split from ordinary PO/invoice recording later.
CAPABILITY_APPROVE_INVOICE_EXCEPTION = "approve_invoice_exception"
# S4: the separate payment-release step after an exception is accepted.
# Every supplier payment release needs this -- TICKET_SPEC.md's default:
# "there is no automatic limit."
CAPABILITY_RELEASE_SUPPLIER_PAYMENT = "release_supplier_payment"
# F5: acknowledge a cash-forecast shortfall case.
CAPABILITY_ACKNOWLEDGE_FORECAST = "acknowledge_forecast"

# R1 (prevention extension): trigger the deterministic readiness-check
# engine (POST /api/v1/readiness/recompute). Not one of X0.1's five named
# capabilities (those gate the generic Approval mechanism's decision
# actions) -- this follows the SAME precedent as every other "trigger a
# deterministic engine" endpoint already has its own dedicated capability
# (CAPABILITY_IMPORT_SOURCE, CAPABILITY_RUN_RECONCILIATION,
# CAPABILITY_CALCULATE_SETTLEMENT): a judgment call, documented here and in
# docs/progress/R1-handoff.md/PROGRESS.md's Deviations, not silently added.
CAPABILITY_RUN_READINESS = "run_readiness"

ALL_CAPABILITY_CODES = (
    CAPABILITY_ADMIN,
    CAPABILITY_RESOLVE_CASE,
    CAPABILITY_APPROVE_POLICY,
    CAPABILITY_APPROVE_PAYOUT,
    CAPABILITY_FINANCE_LEAD,
    CAPABILITY_IMPORT_SOURCE,
    CAPABILITY_RUN_RECONCILIATION,
    CAPABILITY_CALCULATE_SETTLEMENT,
    CAPABILITY_MANAGE_PAYABLES,
    CAPABILITY_CLOSE_BOOKS,
    CAPABILITY_RUN_AGENTS,
    CAPABILITY_APPROVE_INTERPRETATION,
    CAPABILITY_RECORD_AMENDMENT,
    CAPABILITY_APPROVE_INVOICE_EXCEPTION,
    CAPABILITY_RELEASE_SUPPLIER_PAYMENT,
    CAPABILITY_ACKNOWLEDGE_FORECAST,
    CAPABILITY_RUN_READINESS,
)
