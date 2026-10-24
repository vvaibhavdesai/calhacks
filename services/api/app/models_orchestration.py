"""A06 stage 3: nightly close orchestration records.

What lives where (AGENT_HANDOFF.md "Maintain separate source evidence,
approved policies, workflow/checkpoint state and execution history"):

  - LangGraph checkpoints (`checkpoints`, `checkpoint_blobs`,
    `checkpoint_writes`, created by migration 0010) are WORKFLOW state only:
    where each vendor's graph is, so it can resume. They are NOT the audit
    record and never authorize a financial effect.
  - `business_events` (UnitOfWork.append_event) is the flight recorder:
    node starts/ends, holds, resumes, failures, retries, model versions.
  - The tables below are the run's operational projection plus the
    effect ledger that makes resume safe.

`orchestration_runs` is one-to-one with the shared `runs` row (C03 Run):
`runs.id` is what cases/settlements/match groups already reference as
`run_id`, so everything the nightly close produces links to one parent.

`orchestration_steps` is the effect ledger. A node that performs a
domain effect writes its step row IN THE SAME TRANSACTION as the effect
(UNIQUE(run_id, vendor_id, step)). Re-executing the node after a crash --
whether the crash was before or after LangGraph saved its checkpoint --
finds the row and returns the recorded result instead of repeating the
effect. Append-only for encore_app (migration 0010).

`orchestration_commands` are the durable, idempotent human/API commands
(start, resume, retry) the worker applies; `command_id` is the C03
Idempotency-Key.

`agent_case_explanations` records the optional Claude explanation node:
a PROPOSAL shown next to a held vendor's case, never a decision.
Append-only for encore_app.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import _new_id


class OrchestrationRun(Base):
    __tablename__ = "orchestration_runs"
    __table_args__ = (
        UniqueConstraint("festival_id", "business_date", name="uq_orchestration_festival_date"),
    )

    # Same id as the shared `runs` row.
    id: Mapped[str] = mapped_column(
        String(32), ForeignKey("runs.id", ondelete="RESTRICT"), primary_key=True
    )
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_final_period: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # QUEUED | RUNNING | WAITING_REVIEW | PARTIALLY_COMPLETE | COMPLETED |
    # FAILED | CANCELLED (C04 run projection; mirrored onto runs.status)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="QUEUED")
    requested_by: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    requested_capability: Mapped[str] = mapped_column(String(100), nullable=False)
    start_command_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    graph_version: Mapped[str] = mapped_column(String(40), nullable=False)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    # Claude spend budget for the run (USD). Reserved before a call at
    # worst-case cost, settled to the real cost after.
    budget_usd: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False, default=0)
    spent_usd: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False, default=0)
    reserved_usd: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False, default=0)
    explain_cases: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Worker claim (C06 "lease/claim ownership"). A lease alone never
    # prevents duplicate effects -- orchestration_steps + domain
    # idempotency keys do; the lease only keeps two healthy workers from
    # doing the same work at once.
    needs_work: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    wake_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OrchestrationVendorRun(Base):
    __tablename__ = "orchestration_vendor_runs"
    __table_args__ = (UniqueConstraint("run_id", "vendor_id", name="uq_orch_vendor_run"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("orchestration_runs.id", ondelete="RESTRICT"), index=True
    )
    vendor_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    thread_id: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    # Last milestone reached (C04): PENDING, INGESTED, RECONCILED,
    # TERMS_RESOLVED, CALCULATED, CONTROLS_CHECKED, APPROVED, PAYING,
    # CONFIRMED, POSTED, REPORTED
    milestone: Mapped[str] = mapped_column(String(30), nullable=False, default="PENDING")
    # PENDING | RUNNING | HELD | FAILED | COMPLETED
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    held_at: Mapped[str | None] = mapped_column(String(30), nullable=True)
    hold_reasons: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    settlement_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payout_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    statement_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class OrchestrationStep(Base):
    """Effect ledger. vendor_id is '' for festival-level steps."""

    __tablename__ = "orchestration_steps"
    __table_args__ = (
        UniqueConstraint("run_id", "vendor_id", "step", name="uq_orchestration_step"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("orchestration_runs.id", ondelete="RESTRICT"), index=True
    )
    vendor_id: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    step: Mapped[str] = mapped_column(String(80), nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OrchestrationCommand(Base):
    __tablename__ = "orchestration_commands"

    # The C03 command_id / Idempotency-Key.
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("orchestration_runs.id", ondelete="RESTRICT"), index=True
    )
    vendor_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # start | resume | retry
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    actor_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    actor_capability: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AgentCaseExplanation(Base):
    __tablename__ = "agent_case_explanations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    case_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("cases.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    case_version: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    vendor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model_requested: Mapped[str] = mapped_column(String(60), nullable=False)
    model_served: Mapped[str | None] = mapped_column(String(60), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # SUCCEEDED | INVALID_OUTPUT | REFUSED | API_ERROR | TIMEOUT | TRUNCATED
    # | UNVERIFIED_CITATIONS
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    citations: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
