"""A02 canonical-records / flight-recorder schema.

Scope note: this is the canonical-records/audit/outbox FOUNDATION that A02's
task description asks for -- not every field every later task will want.
A03+ add to these tables; they must not need to fight this schema (e.g.
reconciliation match-groups, settlement line items). Where a later task's
exact shape is still unclear (C02/C03/C05 remain DRAFT per CONTRACTS.md),
fields are kept intentionally minimal and additive-friendly (JSONB payload
columns) rather than guessed in detail.

Tables are grouped to match TASK_IMPLEMENTATION.md's A02 bullet list:
  1. source documents/versions           -> SourceDocument, SourceVersion
  2. vendors/terms                       -> Vendor, VendorContractTerm
  3. orders/payments                     -> Order, OrderLine, Payment
  4. runs/cases/policies                 -> Run, Case, PolicyVersion
  5. payouts                             -> Payout
  6. invoices                            -> Supplier, SupplierInvoice
  7. journals                            -> JournalEntry, JournalLine
  8. events/outbox/lineage/snapshots     -> StreamHead, BusinessEvent,
                                            OutboxIntent, EventLineage,
                                            AggregateProjection,
                                            ProjectionSnapshot

Money columns are integer cents (bigint), per C01. SourceRef-shaped columns
(source_document_id/source_version/content_hash/locator_kind/locator/
quoted_excerpt) are stored inline as plain columns rather than nesting a
JSON blob, specifically so "source drill-down" (acceptance criterion) is a
simple indexed join, not a JSON reach-in.

`BusinessEvent`, `SourceVersion`, `JournalEntry` and `JournalLine` are the
tables the A02 migration (0003) revokes UPDATE/DELETE on for the ordinary
application role -- see that migration and
docs/notes/audit-trust-boundary.md.
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
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models import _new_id

# ---------------------------------------------------------------------------
# 1. Source documents / versions (immutable raw source storage)
# ---------------------------------------------------------------------------


class SourceDocument(Base):
    """Logical identity of one externally-sourced document/feed record.

    One SourceDocument groups all the raw bytes ever imported under a given
    (source_system, source_native_id) pair across however many versions it
    has had (re-imports, corrections, re-sends).
    """

    __tablename__ = "source_documents"
    __table_args__ = (
        UniqueConstraint("source_system", "source_native_id", name="uq_source_document_native"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    source_system: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    source_native_id: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    versions: Mapped[list[SourceVersion]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class SourceVersion(Base):
    """One immutable, hash-identified byte payload for a SourceDocument.

    Idempotency for ingestion lives here: `uq_source_version_hash` means
    importing byte-identical content twice under the same source document
    can never create a second row -- the import service looks this up
    before inserting (see app/ingestion.py).

    Invalid/malformed input is still inserted here with
    status='quarantined' (never silently dropped); only its presence in
    *active* ingestion-derived tables (orders/payments) is withheld.
    """

    __tablename__ = "source_versions"
    __table_args__ = (
        UniqueConstraint("source_document_id", "version", name="uq_source_version_seq"),
        UniqueConstraint("source_document_id", "content_hash", name="uq_source_version_hash"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    source_document_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("source_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    mime_type: Mapped[str] = mapped_column(String(150), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_locator: Mapped[str] = mapped_column(String(500), nullable=False)
    parser_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    quarantine_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    document: Mapped[SourceDocument] = relationship(back_populates="versions")


# ---------------------------------------------------------------------------
# 2. Vendors / terms
# ---------------------------------------------------------------------------


class Vendor(Base):
    __tablename__ = "vendors"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class VendorContractTerm(Base):
    """One versioned contract/term record for a vendor (C02/C04 inputs).

    `commission_bps`/`minimum_fee_cents` are nullable: null means "use the
    festival default" (15% / $500 per AGENT_HANDOFF.md), not zero. A04 reads
    these; A02 only stores them traceably with source_refs so the eventual
    15%-vs-10% conflict scenario has somewhere real to live.
    """

    __tablename__ = "vendor_contract_terms"
    __table_args__ = (UniqueConstraint("vendor_id", "version", name="uq_vendor_term_version"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    vendor_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("vendors.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    commission_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    minimum_fee_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    processor_fee_deductible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    source_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    approved_by: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# 3. Orders / payments
# ---------------------------------------------------------------------------


class Order(Base):
    """Idempotency: unique(source_system, source_native_id) -- re-ingesting
    the same POS order a second time must resolve to the existing row, not
    create a duplicate (A03 cares about THIS being distinct from a duplicate
    real charge, which is a payments/matching question, not an ingestion
    one)."""

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("source_system", "source_native_id", name="uq_order_native"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    vendor_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_system: Mapped[str] = mapped_column(String(100), nullable=False)
    source_native_id: Mapped[str] = mapped_column(String(300), nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    ordered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    gross_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tax_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tips_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    # SourceRef, inlined (C01).
    source_document_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("source_documents.id", ondelete="RESTRICT"), nullable=False
    )
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    locator_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    locator: Mapped[str] = mapped_column(String(300), nullable=False)
    quoted_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    lines: Mapped[list[OrderLine]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class OrderLine(Base):
    __tablename__ = "order_lines"
    __table_args__ = (UniqueConstraint("order_id", "line_id", name="uq_order_line"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    order_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True
    )
    line_id: Mapped[str] = mapped_column(String(100), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    eligible_net_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tax_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tips_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    gross_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    order: Mapped[Order] = relationship(back_populates="lines")


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        UniqueConstraint("source_system", "source_native_id", name="uq_payment_native"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    vendor_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    order_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_system: Mapped[str] = mapped_column(String(100), nullable=False)
    source_native_id: Mapped[str] = mapped_column(String(300), nullable=False)
    method: Mapped[str] = mapped_column(String(30), nullable=False)
    custody: Mapped[str] = mapped_column(String(30), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="recorded")
    # A03 additive columns (migration 0004; 0002 is never edited in place).
    # `kind` distinguishes a capture from a refund/chargeback -- always a
    # positive `amount_cents` magnitude, with `kind` carrying the direction.
    # This is what lets app/reconciliation.py tell "two genuinely separate
    # captures" (possible duplicate charge) apart from "a capture plus its
    # own, legitimate, offsetting refund" without guessing from sign alone.
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="capture")
    # `bank_entry_id`: which grouped net bank settlement batch (if any) this
    # payment was deposited through. Nullable because vendor-retained cash
    # payments never go through a bank settlement at all, and a
    # centrally-collected payment may not have settled yet.
    bank_entry_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("bank_entries.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider_reference: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source_document_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("source_documents.id", ondelete="RESTRICT"), nullable=True
    )
    source_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    locator_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    locator: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# 4. Runs / cases / policies
# ---------------------------------------------------------------------------


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    festival_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="QUEUED")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Case(Base):
    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    run_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    vendor_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("vendors.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reason_code: Mapped[str] = mapped_column(String(100), nullable=False)
    evidence_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    financial_impact_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    proposed_resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    required_capability: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PolicyVersion(Base):
    """One row per policy *version*. `policy_id` is the stable logical
    identity shared across versions (DRAFT -> ACTIVE -> SUPERSEDED/...);
    `version` increments per policy_id. A05 owns the approval workflow that
    writes these; A02 only provides the versioned, non-destructive shape."""

    __tablename__ = "policy_versions"
    __table_args__ = (UniqueConstraint("policy_id", "version", name="uq_policy_version"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    policy_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    scope: Mapped[dict] = mapped_column(JSONB, nullable=False)
    predicates: Mapped[dict] = mapped_column(JSONB, nullable=False)
    effect: Mapped[dict] = mapped_column(JSONB, nullable=False)
    evidence_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    approver_ref: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="DRAFT")
    supersedes_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# 5. Payouts
# ---------------------------------------------------------------------------


class Payout(Base):
    __tablename__ = "payouts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    logical_obligation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    vendor_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("vendors.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    beneficiary_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    approval_ref: Mapped[str | None] = mapped_column(String(32), nullable=True)
    state: Mapped[str] = mapped_column(String(30), nullable=False, default="DRAFT")
    provider_reference: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # A04 additive column (migration 0005; 0001-0004 are never edited in
    # place). Which Settlement (app.models_settlement.Settlement) this
    # payout pays out, if any -- lets app/settlement.py detect "a prior
    # CONFIRMED payout already exists for this settlement" without A07's
    # full dispatch machinery existing yet. Nullable because A02's original
    # Payout shape predates settlements and A07 may still create payouts
    # that aren't tied to one settlement row (e.g. a batched payout).
    settlement_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("settlements.id", ondelete="SET NULL"), nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# 6. Invoices (suppliers)
# ---------------------------------------------------------------------------


class Supplier(Base):
    __tablename__ = "suppliers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SupplierInvoice(Base):
    __tablename__ = "supplier_invoices"
    __table_args__ = (
        UniqueConstraint("supplier_id", "invoice_number", name="uq_supplier_invoice_number"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    supplier_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    po_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    invoice_number: Mapped[str] = mapped_column(String(100), nullable=False)
    invoice_date: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    gross_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tax_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    net_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    outstanding_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# 7. Journals (balanced, immutable once posted)
# ---------------------------------------------------------------------------


class JournalEntry(Base):
    __tablename__ = "journal_entries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    posting_rule_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    source_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    correction_of: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("journal_entries.id", ondelete="RESTRICT"), nullable=True
    )
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    lines: Mapped[list[JournalLine]] = relationship(
        back_populates="entry", cascade="all, delete-orphan"
    )


class JournalLine(Base):
    __tablename__ = "journal_lines"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    journal_entry_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("journal_entries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_code: Mapped[str] = mapped_column(String(100), nullable=False)
    debit_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    credit_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")


# ---------------------------------------------------------------------------
# 8. Events / outbox / lineage / snapshots (the flight recorder itself)
# ---------------------------------------------------------------------------


class StreamHead(Base):
    """Per-stream append pointer: last_sequence/last_hash.

    This row is the concurrency-control point for `UnitOfWork.append_event`
    (app/uow.py): the appender takes `SELECT ... FOR UPDATE` on this row
    before computing the next sequence/hash, so concurrent appenders to the
    *same* stream_id are serialized by Postgres's row lock, not by
    application-level locking that could race. This table is the one
    legitimately-mutable piece of the chain (it's a pointer, not history);
    `business_events` itself is append-only.
    """

    __tablename__ = "stream_heads"

    stream_id: Mapped[str] = mapped_column(String(300), primary_key=True)
    last_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class BusinessEvent(Base):
    """Append-only audit event, per C06's required-fields list.

    Ordinary runtime DB role has UPDATE/DELETE revoked on this table (see
    migration 0003 and docs/notes/audit-trust-boundary.md) -- this is
    enforced in Postgres, not merely "the ORM has no update() call".
    """

    __tablename__ = "business_events"
    __table_args__ = (
        UniqueConstraint("stream_id", "sequence", name="uq_business_event_stream_seq"),
    )

    event_id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    stream_id: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    event_type: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_type: Mapped[str] = mapped_column(String(30), nullable=False)
    actor_capability: Mapped[str | None] = mapped_column(String(100), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    aggregate_version: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    vendor_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    branch_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    causation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    command_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    versions: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    previous_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)


class OutboxIntent(Base):
    __tablename__ = "outbox_intents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    event_id: Mapped[str | None] = mapped_column(
        String(32),
        ForeignKey("business_events.event_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    intent_type: Mapped[str] = mapped_column(String(100), nullable=False)
    target: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EventLineage(Base):
    """Explicit event -> evidence/aggregate link, so a later "challenge"
    (A09) can walk from an event to everything it touched without scanning
    JSON payloads. A02 only populates this for the source-import workflow it
    implements; later tasks add rows for their own event types."""

    __tablename__ = "event_lineage"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    event_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("business_events.event_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ref_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    ref_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ref_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AggregateProjection(Base):
    """A real, explicitly-rebuildable mutable projection: "latest known
    state per aggregate", derived purely by replaying `business_events` for
    `stream_id` in sequence order (see app/projections.py
    `rebuild_aggregate_projection`). This row is NOT authoritative history;
    it is a derived read-optimization that can be dropped and recomputed
    from the event log at any time."""

    __tablename__ = "aggregate_projections"

    aggregate_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    aggregate_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    stream_id: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    latest_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    latest_event_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    state: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    projection_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ProjectionSnapshot(Base):
    """Point-in-time snapshot of a projection's derived state, per C06:
    "stream cursor, projection schema version and state hash; explicitly
    derived and rebuildable." Used for historical-state queries later
    (A09) without needing to replay the full event log from sequence 1
    every time."""

    __tablename__ = "projection_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "projection_name", "stream_id", "as_of_sequence", name="uq_projection_snapshot"
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    projection_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    stream_id: Mapped[str] = mapped_column(String(300), nullable=False, index=True)
    as_of_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
