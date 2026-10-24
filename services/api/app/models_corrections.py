"""A09 schema: challenges, holds, corrections, vendor recoveries, what-if
branches, replays and command idempotency records.

Same conventions as app/models_settlement.py and app/models_finance.py:
integer cents, opaque string ids, nothing financial is ever edited in
place. Where a table carries a workflow `status` (challenges, corrections,
recoveries, holds, branches, replays) the row is mutable, and every state
change is ALSO appended to `business_events` through app/uow.py, so the
history is in the append-only log, not in the mutable row.

Append-only for `encore_app` (migration 0010 REVOKEs UPDATE/DELETE):
  - correction_approvals: one row per human approval of an exact plan.
  - recovery_actions: every decision/receipt/waiver on a vendor recovery.
  - whatif_results: the recomputed figures of a what-if branch.
  - command_records: Idempotency-Key -> original response.

What-if isolation: branch output lives ONLY in `whatif_branches`/
`whatif_results` (branch-tagged tables) plus `business_events` rows whose
`branch_id` is set. Production settlements, payouts, journals and
statements are never written by branch code (see app/whatif.py).
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import _new_id


class Challenge(Base):
    """A human challenge of a decided result (policy version, settlement,
    payout or case), carrying superseding evidence. Creating one pauses the
    affected rule and work; it changes no money by itself."""

    __tablename__ = "challenges"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # OPEN | PREVIEWED | CORRECTION_APPROVED | CORRECTED | WITHDRAWN
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="OPEN")
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    vendor_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    target_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    target_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    target_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    case_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    contract_term_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    superseding_evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # What the evidence says the term should be, e.g. {"commission_bps": 1200},
    # and from which business date. Proposed by the challenger; applied only
    # by an approved correction.
    proposed_effect: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    policy_effect: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    paused_work: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    correction_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    capability: Mapped[str | None] = mapped_column(String(100), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, default=_new_id)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class WorkHold(Base):
    """A hard blocker on releasing money. `record_kind` VENDOR holds every
    payout of that vendor; PAYOUT/SETTLEMENT hold one record. Checked by
    app/payout_dispatch.py at release AND at dispatch time."""

    __tablename__ = "work_holds"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    vendor_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    record_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    record_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(30), nullable=False)  # challenge | recovery
    source_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    release_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class Correction(Base):
    """A server-computed correction plan (preview) and, once approved, its
    execution progress and results. `preview_hash` is the hash of `plan`;
    an approval must quote it, and execution recomputes the plan and
    refuses (STALE) if the facts changed in between."""

    __tablename__ = "corrections"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # PREVIEW_READY | APPROVED | IN_PROGRESS | COMPLETED | FAILED | STALE
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="PREVIEW_READY")
    challenge_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("challenges.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    vendor_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    preview_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    required_capability: Mapped[str] = mapped_column(String(100), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    plan: Mapped[dict] = mapped_column(JSONB, nullable=False)
    progress: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    results: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    approval_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CorrectionApproval(Base):
    """Append-only: the human decision binding an exact plan."""

    __tablename__ = "correction_approvals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    correction_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("corrections.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    actor_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    capability: Mapped[str] = mapped_column(String(100), nullable=False)
    correction_version: Mapped[int] = mapped_column(Integer, nullable=False)
    preview_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    item_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class VendorRecovery(Base):
    """Money a vendor owes back after it was already paid (a correction
    lowered its entitlement, a late refund, or a payout that won a cancel
    race). Never a negative payout. Resolved by an approved offset against
    a later draft payout, a recovery request settled by receipts, or a
    finance-lead waiver. remaining = amount - offset - received - waived."""

    __tablename__ = "vendor_recoveries"
    __table_args__ = (UniqueConstraint("recovery_key", name="uq_vendor_recovery_key"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    recovery_key: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    vendor_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    origin_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    origin_id: Mapped[str] = mapped_column(String(32), nullable=False)
    settlement_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payout_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    business_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    offset_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    received_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    waived_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # OPEN (awaiting a human decision) | REQUESTED (awaiting receipt) | CLOSED
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    @property
    def remaining_cents(self) -> int:
        return self.amount_cents - self.offset_cents - self.received_cents - self.waived_cents


class RecoveryAction(Base):
    """Append-only log of every recovery decision, offset, receipt and waiver."""

    __tablename__ = "recovery_actions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    recovery_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("vendor_recoveries.id", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    payout_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    journal_entry_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    capability: Mapped[str | None] = mapped_column(String(100), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WhatIfBranch(Base):
    __tablename__ = "whatif_branches"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="CREATING")
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    base_knowledge_cutoff: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    business_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    overrides: Mapped[list] = mapped_column(JSONB, nullable=False)
    isolation: Mapped[dict] = mapped_column(JSONB, nullable=False)
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WhatIfResult(Base):
    """Append-only branch output: one recomputed settlement per row."""

    __tablename__ = "whatif_results"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    branch_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("whatif_branches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    production_settlement_id: Mapped[str] = mapped_column(String(32), nullable=False)
    vendor_id: Mapped[str] = mapped_column(String(32), nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    label: Mapped[str] = mapped_column(String(300), nullable=False)
    production_payout_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    branch_payout_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    production_fee_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    branch_fee_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    branch_commission_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    branch_state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Replay(Base):
    __tablename__ = "replays"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="QUEUED")
    mode: Mapped[str] = mapped_column(String(40), nullable=False)
    scope: Mapped[dict] = mapped_column(JSONB, nullable=False)
    from_position: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    to_position: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    matching: Mapped[int | None] = mapped_column(Integer, nullable=True)
    divergences: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    effects: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    checks: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CommandRecord(Base):
    """Append-only Idempotency-Key ledger for A09 commands (C03: same key +
    same canonical payload returns the original result; same key +
    different payload is 409)."""

    __tablename__ = "command_records"
    __table_args__ = (UniqueConstraint("scope_key", name="uq_command_record_scope_key"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    scope_key: Mapped[str] = mapped_column(String(300), nullable=False)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response: Mapped[dict] = mapped_column(JSONB, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
