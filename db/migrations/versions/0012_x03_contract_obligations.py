"""X0.3 (prevention extension): contract obligations and links.

Revision ID: 0012_x03_contract_obligations
Revises: 0011_x02_term_provenance
Create Date: 2026-10-24

Two new tables, additive on top of 0011 (0001-0011 are never edited here):

1. `contract_obligations` -- one deposit/milestone/final payment commitment
   under a contract (TICKET_SPEC.md X0.3). `amount_cents` is written once,
   at creation (app/contract_obligations.py's `propose_obligation`), and
   never again by any code in this ticket -- see that module's docstring
   and `ContractObligation`'s own docstring in services/api/app/
   models_finance.py for the full "never total $40,000" rationale. This
   table is NOT append-only: `status`/`version`/`updated_at` are updated in
   place as the obligation moves through its explicit state machine
   (packages/domain/domain/contract_obligations.py), same mutability
   precedent as `supplier_invoices` (A08) -- not everything that gets
   amended in place is `supplier_payments`/`approvals`-style append-only.

2. `obligation_links` -- one link from an obligation to a PO/invoice/
   payment/credit_note, each carrying the portion of that subject's amount
   attributed to this obligation (TICKET_SPEC.md: "Table obligation_link:
   obligation_id, subject_type (po | invoice | payment | credit_note),
   subject_id, amount_cents"). THIS table IS append-only (same convention
   as `supplier_payments`/`approvals`): migration REVOKEs UPDATE/DELETE on
   it for `encore_app`, same as 0008/0010.

No DB-level CHECK constraint on either table's enum-like string columns
(`counterparty_type`, `kind`, `status` on contract_obligations;
`subject_type` on obligation_links) -- matches this schema's own
established precedent (zero CHECK constraints anywhere in the migration
history; every enum-like status column is validated in application code
only -- see packages/domain/domain/contract_obligations.py's
`validate_transition`/vocabulary tuples and services/api/app/
contract_obligations.py's validation at each write site).

No new `capabilities` rows: this ticket reuses two already-seeded
capabilities (`finance_lead` for proposing/transitioning an obligation,
`manage_payables` for linking a PO/invoice/payment/credit_note to one --
see docs/progress/X0.3-handoff.md for the exact mapping), not any of the
five X0.1 introduced and not a new one of its own.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_x03_contract_obligations"
down_revision: str | None = "0011_x02_term_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "encore_app"
_NEWLY_PROTECTED_TABLES = ("obligation_links",)


def upgrade() -> None:
    op.create_table(
        "contract_obligations",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("festival_id", sa.String(length=64), nullable=False),
        sa.Column(
            "contract_id",
            sa.String(length=32),
            sa.ForeignKey("source_documents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("counterparty_type", sa.String(length=20), nullable=False),
        sa.Column("counterparty_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=True),
        sa.Column("condition", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="proposed"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
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
        sa.Column(
            "proposed_by",
            sa.String(length=32),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_contract_obligations_festival_id", "contract_obligations", ["festival_id"])
    op.create_index("ix_contract_obligations_contract_id", "contract_obligations", ["contract_id"])
    op.create_index(
        "ix_contract_obligations_counterparty",
        "contract_obligations",
        ["counterparty_type", "counterparty_id"],
    )

    op.create_table(
        "obligation_links",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "obligation_id",
            sa.String(length=32),
            sa.ForeignKey("contract_obligations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("subject_type", sa.String(length=20), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "actor_id",
            sa.String(length=32),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_capability", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_obligation_links_obligation_id", "obligation_links", ["obligation_id"])
    op.create_index(
        "ix_obligation_links_subject_type_subject_id",
        "obligation_links",
        ["subject_type", "subject_id"],
    )

    protected = ", ".join(_NEWLY_PROTECTED_TABLES)
    op.execute(sa.text(f"REVOKE UPDATE, DELETE ON {protected} FROM {APP_ROLE}"))


def downgrade() -> None:
    protected = ", ".join(_NEWLY_PROTECTED_TABLES)
    op.execute(sa.text(f"GRANT UPDATE, DELETE ON {protected} TO {APP_ROLE}"))

    op.drop_index("ix_obligation_links_subject_type_subject_id", table_name="obligation_links")
    op.drop_index("ix_obligation_links_obligation_id", table_name="obligation_links")
    op.drop_table("obligation_links")

    op.drop_index("ix_contract_obligations_counterparty", table_name="contract_obligations")
    op.drop_index("ix_contract_obligations_contract_id", table_name="contract_obligations")
    op.drop_index("ix_contract_obligations_festival_id", table_name="contract_obligations")
    op.drop_table("contract_obligations")
