"""A02 canonical records: sources, vendors/terms, orders/payments,
runs/cases/policies, payouts, invoices, journals, events/outbox/lineage/
snapshots.

Revision ID: 0002_canonical_records
Revises: 0001_baseline_auth
Create Date: 2026-10-24

Mirrors services/api/app/models_ledger.py exactly. Built on top of
0001_baseline_auth per git discipline -- 0001 itself is never edited here.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_canonical_records"
down_revision: str | None = "0001_baseline_auth"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- 1. source documents / versions ---------------------------------
    op.create_table(
        "source_documents",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("source_system", sa.String(length=100), nullable=False),
        sa.Column("source_native_id", sa.String(length=300), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("source_system", "source_native_id", name="uq_source_document_native"),
    )
    op.create_index("ix_source_documents_source_system", "source_documents", ["source_system"])
    op.create_index(
        "ix_source_documents_source_native_id", "source_documents", ["source_native_id"]
    )

    op.create_table(
        "source_versions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "source_document_id",
            sa.String(length=32),
            sa.ForeignKey("source_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("mime_type", sa.String(length=150), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("storage_locator", sa.String(length=500), nullable=False),
        sa.Column("parser_version", sa.String(length=50), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("quarantine_reason", sa.String(length=500), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("source_document_id", "version", name="uq_source_version_seq"),
        sa.UniqueConstraint("source_document_id", "content_hash", name="uq_source_version_hash"),
    )
    op.create_index(
        "ix_source_versions_source_document_id", "source_versions", ["source_document_id"]
    )
    op.create_index("ix_source_versions_content_hash", "source_versions", ["content_hash"])

    # --- 2. vendors / terms -----------------------------------------------
    op.create_table(
        "vendors",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("festival_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("display_name", sa.String(length=300), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_vendors_festival_id", "vendors", ["festival_id"])

    op.create_table(
        "vendor_contract_terms",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "vendor_id",
            sa.String(length=32),
            sa.ForeignKey("vendors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("commission_bps", sa.Integer(), nullable=True),
        sa.Column("minimum_fee_cents", sa.BigInteger(), nullable=True),
        sa.Column(
            "processor_fee_deductible", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column(
            "source_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "approved_by",
            sa.String(length=32),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("vendor_id", "version", name="uq_vendor_term_version"),
    )
    op.create_index("ix_vendor_contract_terms_vendor_id", "vendor_contract_terms", ["vendor_id"])

    # --- 3. orders / payments ---------------------------------------------
    op.create_table(
        "orders",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "vendor_id",
            sa.String(length=32),
            sa.ForeignKey("vendors.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_system", sa.String(length=100), nullable=False),
        sa.Column("source_native_id", sa.String(length=300), nullable=False),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("ordered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gross_cents", sa.BigInteger(), nullable=False),
        sa.Column("tax_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("tips_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column(
            "source_document_id",
            sa.String(length=32),
            sa.ForeignKey("source_documents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("locator_kind", sa.String(length=20), nullable=False),
        sa.Column("locator", sa.String(length=300), nullable=False),
        sa.Column("quoted_excerpt", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("source_system", "source_native_id", name="uq_order_native"),
    )
    op.create_index("ix_orders_vendor_id", "orders", ["vendor_id"])
    op.create_index("ix_orders_business_date", "orders", ["business_date"])

    op.create_table(
        "order_lines",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "order_id",
            sa.String(length=32),
            sa.ForeignKey("orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("line_id", sa.String(length=100), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("eligible_net_cents", sa.BigInteger(), nullable=False),
        sa.Column("tax_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("tips_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("gross_cents", sa.BigInteger(), nullable=False),
        sa.Column(
            "source_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("order_id", "line_id", name="uq_order_line"),
    )
    op.create_index("ix_order_lines_order_id", "order_lines", ["order_id"])

    op.create_table(
        "payments",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "vendor_id",
            sa.String(length=32),
            sa.ForeignKey("vendors.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "order_id",
            sa.String(length=32),
            sa.ForeignKey("orders.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_system", sa.String(length=100), nullable=False),
        sa.Column("source_native_id", sa.String(length=300), nullable=False),
        sa.Column("method", sa.String(length=30), nullable=False),
        sa.Column("custody", sa.String(length=30), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="recorded"),
        sa.Column("provider_reference", sa.String(length=300), nullable=True),
        sa.Column(
            "source_document_id",
            sa.String(length=32),
            sa.ForeignKey("source_documents.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("source_version", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("locator_kind", sa.String(length=20), nullable=True),
        sa.Column("locator", sa.String(length=300), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("source_system", "source_native_id", name="uq_payment_native"),
    )
    op.create_index("ix_payments_vendor_id", "payments", ["vendor_id"])
    op.create_index("ix_payments_order_id", "payments", ["order_id"])

    # --- 4. runs / cases / policies ---------------------------------------
    op.create_table(
        "runs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("festival_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="QUEUED"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_runs_festival_id", "runs", ["festival_id"])

    op.create_table(
        "cases",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "run_id",
            sa.String(length=32),
            sa.ForeignKey("runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "vendor_id",
            sa.String(length=32),
            sa.ForeignKey("vendors.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reason_code", sa.String(length=100), nullable=False),
        sa.Column(
            "evidence_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("financial_impact_cents", sa.BigInteger(), nullable=True),
        sa.Column("proposed_resolution", sa.Text(), nullable=True),
        sa.Column("required_capability", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="OPEN"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_cases_run_id", "cases", ["run_id"])
    op.create_index("ix_cases_vendor_id", "cases", ["vendor_id"])

    op.create_table(
        "policy_versions",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("policy_id", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("scope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("predicates", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("effect", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "evidence_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "approver_ref",
            sa.String(length=32),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="DRAFT"),
        sa.Column("supersedes_version", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("policy_id", "version", name="uq_policy_version"),
    )
    op.create_index("ix_policy_versions_policy_id", "policy_versions", ["policy_id"])

    # --- 5. payouts ---------------------------------------------------------
    op.create_table(
        "payouts",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("logical_obligation_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column(
            "vendor_id",
            sa.String(length=32),
            sa.ForeignKey("vendors.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("business_date", sa.Date(), nullable=False),
        sa.Column("beneficiary_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("approval_ref", sa.String(length=32), nullable=True),
        sa.Column("state", sa.String(length=30), nullable=False, server_default="DRAFT"),
        sa.Column("provider_reference", sa.String(length=300), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_unique_constraint("uq_payouts_idempotency_key", "payouts", ["idempotency_key"])
    op.create_index("ix_payouts_logical_obligation_id", "payouts", ["logical_obligation_id"])
    op.create_index("ix_payouts_vendor_id", "payouts", ["vendor_id"])

    # --- 6. invoices (suppliers) --------------------------------------------
    op.create_table(
        "suppliers",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    op.create_table(
        "supplier_invoices",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "supplier_id",
            sa.String(length=32),
            sa.ForeignKey("suppliers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "po_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"
        ),
        sa.Column("invoice_number", sa.String(length=100), nullable=False),
        sa.Column("invoice_date", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="USD"),
        sa.Column("gross_cents", sa.BigInteger(), nullable=False),
        sa.Column("tax_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("net_cents", sa.BigInteger(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("outstanding_cents", sa.BigInteger(), nullable=False),
        sa.Column(
            "source_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("supplier_id", "invoice_number", name="uq_supplier_invoice_number"),
    )
    op.create_index("ix_supplier_invoices_supplier_id", "supplier_invoices", ["supplier_id"])

    # --- 7. journals ---------------------------------------------------------
    op.create_table(
        "journal_entries",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("posting_rule_version", sa.String(length=50), nullable=True),
        sa.Column(
            "source_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "correction_of",
            sa.String(length=32),
            sa.ForeignKey("journal_entries.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    op.create_table(
        "journal_lines",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "journal_entry_id",
            sa.String(length=32),
            sa.ForeignKey("journal_entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("account_code", sa.String(length=100), nullable=False),
        sa.Column("debit_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("credit_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_journal_lines_journal_entry_id", "journal_lines", ["journal_entry_id"])

    # --- 8. events / outbox / lineage / snapshots ---------------------------
    op.create_table(
        "stream_heads",
        sa.Column("stream_id", sa.String(length=300), primary_key=True),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    op.create_table(
        "business_events",
        sa.Column("event_id", sa.String(length=32), primary_key=True),
        sa.Column("stream_id", sa.String(length=300), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("event_type", sa.String(length=150), nullable=False),
        sa.Column("actor_id", sa.String(length=32), nullable=True),
        sa.Column("actor_type", sa.String(length=30), nullable=False),
        sa.Column("actor_capability", sa.String(length=100), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("aggregate_id", sa.String(length=64), nullable=False),
        sa.Column("aggregate_version", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=32), nullable=True),
        sa.Column("vendor_id", sa.String(length=32), nullable=True),
        sa.Column("branch_id", sa.String(length=32), nullable=True),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("causation_id", sa.String(length=64), nullable=True),
        sa.Column("command_id", sa.String(length=64), nullable=True),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column("versions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("previous_hash", sa.String(length=64), nullable=True),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.UniqueConstraint("stream_id", "sequence", name="uq_business_event_stream_seq"),
    )
    op.create_index("ix_business_events_stream_id", "business_events", ["stream_id"])
    op.create_index("ix_business_events_event_type", "business_events", ["event_type"])
    op.create_index("ix_business_events_aggregate_id", "business_events", ["aggregate_id"])
    op.create_index("ix_business_events_run_id", "business_events", ["run_id"])
    op.create_index("ix_business_events_vendor_id", "business_events", ["vendor_id"])
    op.create_index("ix_business_events_branch_id", "business_events", ["branch_id"])
    op.create_index("ix_business_events_correlation_id", "business_events", ["correlation_id"])

    op.create_table(
        "outbox_intents",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(length=32),
            sa.ForeignKey("business_events.event_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("intent_type", sa.String(length=100), nullable=False),
        sa.Column("target", sa.String(length=100), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_outbox_intents_idempotency_key", "outbox_intents", ["idempotency_key"]
    )
    op.create_index("ix_outbox_intents_event_id", "outbox_intents", ["event_id"])

    op.create_table(
        "event_lineage",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "event_id",
            sa.String(length=32),
            sa.ForeignKey("business_events.event_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ref_kind", sa.String(length=30), nullable=False),
        sa.Column("ref_id", sa.String(length=64), nullable=False),
        sa.Column("ref_version", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_event_lineage_event_id", "event_lineage", ["event_id"])
    op.create_index("ix_event_lineage_ref_id", "event_lineage", ["ref_id"])

    op.create_table(
        "aggregate_projections",
        sa.Column("aggregate_id", sa.String(length=64), primary_key=True),
        sa.Column("aggregate_type", sa.String(length=50), nullable=False),
        sa.Column("stream_id", sa.String(length=300), nullable=False),
        sa.Column("latest_sequence", sa.BigInteger(), nullable=False),
        sa.Column("latest_event_id", sa.String(length=32), nullable=True),
        sa.Column(
            "state", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column("projection_schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_aggregate_projections_aggregate_type", "aggregate_projections", ["aggregate_type"]
    )
    op.create_index("ix_aggregate_projections_stream_id", "aggregate_projections", ["stream_id"])

    op.create_table(
        "projection_snapshots",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("projection_name", sa.String(length=100), nullable=False),
        sa.Column("stream_id", sa.String(length=300), nullable=False),
        sa.Column("as_of_sequence", sa.BigInteger(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "computed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "projection_name", "stream_id", "as_of_sequence", name="uq_projection_snapshot"
        ),
    )
    op.create_index(
        "ix_projection_snapshots_projection_name", "projection_snapshots", ["projection_name"]
    )
    op.create_index("ix_projection_snapshots_stream_id", "projection_snapshots", ["stream_id"])


def downgrade() -> None:
    op.drop_table("projection_snapshots")
    op.drop_table("aggregate_projections")
    op.drop_table("event_lineage")
    op.drop_table("outbox_intents")
    op.drop_table("business_events")
    op.drop_table("stream_heads")
    op.drop_table("journal_lines")
    op.drop_table("journal_entries")
    op.drop_table("supplier_invoices")
    op.drop_table("suppliers")
    op.drop_table("payouts")
    op.drop_table("policy_versions")
    op.drop_table("cases")
    op.drop_table("runs")
    op.drop_table("payments")
    op.drop_table("order_lines")
    op.drop_table("orders")
    op.drop_table("vendor_contract_terms")
    op.drop_table("vendors")
    op.drop_table("source_versions")
    op.drop_table("source_documents")
