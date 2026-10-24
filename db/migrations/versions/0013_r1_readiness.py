"""R1 (prevention extension): readiness records and checks.

Revision ID: 0013_r1_readiness
Revises: 0012_x03_contract_obligations
Create Date: 2026-10-24

Two new tables, additive on top of 0012 (0001-0012 are never edited here):

1. `readiness_checks` -- one computed pre-event readiness result for a
   (festival, counterparty) pair (TICKET_SPEC.md R1: "Table readiness_check:
   festival, counterparty, version, status (ready | ready_with_interpretation
   | blocked | stale), checks (jsonb), input_fingerprint, computed_at").
   Versioned the same way `vendor_contract_terms`/`policy_versions` are
   (a new computation with different inputs gets a NEW row, `version+1`; an
   unchanged re-run returns the SAME latest row -- see app/readiness.py's
   `compute_readiness`). NOT append-only: `status` is updated in place by
   this ticket's own writer only to its own freshly-computed value at
   INSERT time, but R3 (not built by this ticket) will need to flip an
   existing row's `status` to "stale" in place when new evidence arrives,
   same mutability precedent as `contract_obligations.status` (X0.3) --
   this table is deliberately NOT in the append-only/REVOKE list below.

2. `readiness_inputs` -- one row per (document_version | rule_version |
   contract-term row) actually consulted while computing one
   `readiness_checks` row (TICKET_SPEC.md: "Table readiness_input: check_id
   -> the document_version or rule_version used"). `input_kind` has a third
   value beyond the spec's literal two ("vendor_contract_term"), covering a
   VendorContractTerm row with no real document citation of its own --
   documented in docs/progress/R1-handoff.md and PROGRESS.md's Deviations,
   same "add a documented vocabulary value, don't silently invent one"
   discipline X0.1/X0.3 already followed for `case_routing.SUBJECT_TYPES`.
   THIS table IS append-only (same convention as `obligation_links`/
   `approvals`): migration REVOKEs UPDATE/DELETE on it for `encore_app`.

`counterparty_type`/`counterparty_id` is a loose, generic string pair (NOT a
foreign key), same convention X0.1 established for `Case`/`Approval`'s
`subject_type`/`subject_id` and X0.3 established for `ContractObligation`'s
`counterparty_type`/`counterparty_id` -- "vendor" | "supplier" (TICKET_SPEC.md:
"counterparty here can be vendor OR supplier"). R1's own checks only
implement the vendor side (see services/api/app/readiness.py); the schema
stays generic so S-track's own supplier-readiness angle is not blocked by a
vendor-only table shape.

No DB-level CHECK constraint on either table's enum-like string columns
(`counterparty_type`, `status` on readiness_checks; `input_kind` on
readiness_inputs) -- matches this schema's own established precedent (zero
CHECK constraints anywhere in the migration history; every enum-like status
column is validated in application code only -- see
packages/domain/domain/readiness.py's vocabulary tuples and
services/api/app/readiness.py's validation at each write site).

No new HTTP-unrelated capability rows are inserted by this migration;
`CAPABILITY_RUN_READINESS` (new, app/models.py) is granted to the seeded
admin the same way X0.1's five new capabilities were -- re-run
scripts/seed_admin.py after this migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_r1_readiness"
down_revision: str | None = "0012_x03_contract_obligations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "encore_app"
_NEWLY_PROTECTED_TABLES = ("readiness_inputs",)


def upgrade() -> None:
    op.create_table(
        "readiness_checks",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("festival_id", sa.String(length=64), nullable=False),
        sa.Column("counterparty_type", sa.String(length=20), nullable=False),
        sa.Column("counterparty_id", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("checks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("input_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "computed_by",
            sa.String(length=32),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "festival_id",
            "counterparty_type",
            "counterparty_id",
            "version",
            name="uq_readiness_check_version",
        ),
    )
    op.create_index("ix_readiness_checks_festival_id", "readiness_checks", ["festival_id"])
    op.create_index(
        "ix_readiness_checks_counterparty",
        "readiness_checks",
        ["festival_id", "counterparty_type", "counterparty_id"],
    )

    op.create_table(
        "readiness_inputs",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column(
            "readiness_check_id",
            sa.String(length=32),
            sa.ForeignKey("readiness_checks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # "source_version" | "policy_version" | "vendor_contract_term" --
        # see module docstring for why a third value was added beyond the
        # spec's literal "document_version or rule_version" pair.
        sa.Column("input_kind", sa.String(length=30), nullable=False),
        sa.Column("input_id", sa.String(length=64), nullable=False),
        sa.Column("input_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_readiness_inputs_readiness_check_id", "readiness_inputs", ["readiness_check_id"]
    )

    protected = ", ".join(_NEWLY_PROTECTED_TABLES)
    op.execute(sa.text(f"REVOKE UPDATE, DELETE ON {protected} FROM {APP_ROLE}"))


def downgrade() -> None:
    protected = ", ".join(_NEWLY_PROTECTED_TABLES)
    op.execute(sa.text(f"GRANT UPDATE, DELETE ON {protected} TO {APP_ROLE}"))

    op.drop_index("ix_readiness_inputs_readiness_check_id", table_name="readiness_inputs")
    op.drop_table("readiness_inputs")

    op.drop_index("ix_readiness_checks_counterparty", table_name="readiness_checks")
    op.drop_index("ix_readiness_checks_festival_id", table_name="readiness_checks")
    op.drop_table("readiness_checks")
