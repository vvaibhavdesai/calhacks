"""A06 agent records: document extractions and the claims they produced.

AGENT_HANDOFF.md: "AI interprets documents ... Deterministic code computes
money ... Never trust the model's confidence as authority." These tables
hold what the model PROPOSED and what code VERIFIED -- never a decision.
Turning verified claims into contract terms or cases is deterministic code
(app/terms_resolution.py).

`document_extractions` is the recorded model call. It is keyed by
(source_version_id, model, prompt_version) for SUCCEEDED rows: re-running,
replaying or resuming reuses the stored result and never calls the model
again for the same input. Failed attempts are kept as their own rows
(retryable, visible), never overwritten.

Both tables are append-only for encore_app (migration 0009).
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import _new_id


class DocumentExtraction(Base):
    __tablename__ = "document_extractions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    source_document_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("source_documents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_version_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("source_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parser: Mapped[str] = mapped_column(String(60), nullable=False)  # e.g. "pypdf 6.21.2"
    text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    model_requested: Mapped[str] = mapped_column(String(60), nullable=False)
    model_served: Mapped[str | None] = mapped_column(String(60), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # SUCCEEDED | INVALID_OUTPUT | REFUSED | API_ERROR | TRUNCATED
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # The model's short stated summary and flags -- never hidden reasoning.
    document_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedded_instructions_detected: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DocumentClaim(Base):
    __tablename__ = "document_claims"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    extraction_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("document_extractions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_document_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    vendor_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    vendor_name_claimed: Mapped[str] = mapped_column(String(300), nullable=False)
    # COMMISSION_RATE | MINIMUM_FEE | MINIMUM_PERIOD | PROCESSING_FEE_DEDUCTION
    # | BENEFICIARY_CHANGE | OTHER
    claim_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    value_raw: Mapped[str] = mapped_column(String(100), nullable=False)
    # Code-normalized value; null when the raw value did not validate.
    commission_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minimum_fee_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    minimum_period: Mapped[str | None] = mapped_column(String(20), nullable=True)
    processing_fee_deductible: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    speaker_role: Mapped[str] = mapped_column(String(30), nullable=False)
    restates_existing_terms: Mapped[bool] = mapped_column(Boolean, nullable=False)
    available_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    quoted_excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Code verification of the model's citation and value. Unverified claims
    # are kept (visible) but never used by terms resolution.
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    verification_notes: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    source_ref: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
