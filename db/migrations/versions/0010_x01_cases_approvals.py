"""X0.1 (prevention extension): generalize cases and approvals.

Revision ID: 0010_x01_cases_approvals
Revises: 0017_payables_v2
Create Date: 2026-10-24

Re-pointed onto 0017_payables_v2 when the analyst-assistant and
prevention-extension branches were consolidated: this chain and A08's
payables-v2 chain both originally branched off 0009, each independently
numbering their own migrations 0010-0013. Linearized here rather than left
as two Alembic heads, following the same precedent as commit 9ad68a0
("Linearize the new migrations into one chain").

Two things, additive on top of 0009 (0001-0009 are never edited here):

1. `cases.subject_type` / `cases.subject_id` -- a loose, indexed
   (subject_type, subject_id) pair generalizing "what is this case about"
   beyond the implicit "every case is about one vendor's night" shape A02-
   A05 built in (`vendor_id`/`run_id`/`business_date`, all left exactly as
   they were). Deliberately NOT a foreign key to any one table: X0.1's own
   "Done when" requires a case to be able to carry
   `subject_type='forecast_snapshot'` pointing at an arbitrary id string
   BEFORE the `forecast_snapshot` table exists at all (that's F1's job). A
   later ticket may add a real FK for one specific subject_type once its
   table exists; nothing here blocks that, and nothing here is undone by
   it (a new FK is additive, same discipline as everywhere else in this
   migration history).

   Backfill for pre-existing rows (there is no existing `subject_type`
   concept to preserve -- this is new vocabulary, not a renamed column, so
   "backfill" here means "assign the best-fit label given what each row's
   OTHER columns already say it's about," not "copy an old value
   forward"):
     - A row whose `evidence_refs` contains an entry with
       `ref_kind="supplier_invoice"` and `role="subject"` (the exact shape
       app/payables.py's `_open_case` has always written -- A08's
       DUPLICATE_SUPPLIER_INVOICE / POSSIBLE_DUPLICATE_SUPPLIER_INVOICE /
       INVOICE_EXCEEDS_PO cases) backfills as
       subject_type="supplier_invoice", subject_id=<that invoice's id>.
     - Every other row (A05's vendor_contract_term_conflict cases, which
       always carry a real `vendor_id`) backfills as
       subject_type="vendor_run", subject_id=<vendor_id>. This matches
       TICKET_SPEC.md's literal instruction ("Backfill existing rows as
       vendor_run") for the rows it actually describes -- the
       per-vendor-night contract-conflict case -- while giving the OTHER
       kind of existing case (A08's, which were never about a vendor's
       night at all) the label that is actually true of them. See
       docs/progress/X0.1-handoff.md for the full reasoning; this was an
       explicit judgment call, not a literal match of every word of
       TICKET_SPEC.md's X0.1 section.
     - A row matching neither pattern (vendor_id AND evidence_refs both
       absent/unrecognized -- not expected to exist in practice, since
       every Case constructor in this codebase sets one or the other)
       falls back to subject_type="vendor_run", subject_id=<the case's own
       id>, so the column is never left half-backfilled silently.

2. A new, append-only `approvals` table: the generic Approval mechanism
   TICKET_SPEC.md's X0.1 section asks for, parallel to (not replacing) the
   three existing hand-rolled approval call-sites. See `Approval`'s
   docstring in services/api/app/models_ledger.py for the full column
   rationale. `encore_app` gets the usual default SELECT/INSERT/UPDATE/
   DELETE from migration 0003's `ALTER DEFAULT PRIVILEGES` automatically;
   this migration immediately REVOKEs UPDATE/DELETE on it, same as every
   other append-only table in this history (0003, 0008).

No new capability rows are created here -- `capabilities` is seeded
dynamically by scripts/seed_admin.py from app.models.ALL_CAPABILITY_CODES
at runtime, not by migration DDL; re-run that script after this migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_x01_cases_approvals"
down_revision: str | None = "0017_payables_v2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "encore_app"
_NEWLY_PROTECTED_TABLES = ("approvals",)


def upgrade() -> None:
    # --- cases: additive columns + backfill ---------------------------------
    op.add_column("cases", sa.Column("subject_type", sa.String(length=40), nullable=True))
    op.add_column("cases", sa.Column("subject_id", sa.String(length=64), nullable=True))
    op.create_index("ix_cases_subject_type_subject_id", "cases", ["subject_type", "subject_id"])

    conn = op.get_bind()
    cases_tbl = sa.table(
        "cases",
        sa.column("id", sa.String),
        sa.column("vendor_id", sa.String),
        sa.column("evidence_refs", postgresql.JSONB),
        sa.column("subject_type", sa.String),
        sa.column("subject_id", sa.String),
    )
    rows = conn.execute(
        sa.select(cases_tbl.c.id, cases_tbl.c.vendor_id, cases_tbl.c.evidence_refs)
    ).fetchall()
    for row in rows:
        subject_type = "vendor_run"
        subject_id = row.vendor_id
        for ref in row.evidence_refs or []:
            if (
                isinstance(ref, dict)
                and ref.get("ref_kind") == "supplier_invoice"
                and ref.get("role") == "subject"
                and ref.get("ref_id")
            ):
                subject_type = "supplier_invoice"
                subject_id = ref["ref_id"]
                break
        if subject_id is None:
            subject_id = row.id
        conn.execute(
            cases_tbl.update()
            .where(cases_tbl.c.id == row.id)
            .values(subject_type=subject_type, subject_id=subject_id)
        )

    # --- approvals: new append-only table -----------------------------------
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("subject_type", sa.String(length=40), nullable=False),
        sa.Column("subject_id", sa.String(length=64), nullable=False),
        sa.Column(
            "case_id",
            sa.String(length=32),
            sa.ForeignKey("cases.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.String(length=60), nullable=False),
        sa.Column("required_capability", sa.String(length=100), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "actor_id",
            sa.String(length=32),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_capability", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_approvals_subject_type_subject_id", "approvals", ["subject_type", "subject_id"]
    )
    op.create_index("ix_approvals_case_id", "approvals", ["case_id"])

    protected = ", ".join(_NEWLY_PROTECTED_TABLES)
    op.execute(sa.text(f"REVOKE UPDATE, DELETE ON {protected} FROM {APP_ROLE}"))


def downgrade() -> None:
    protected = ", ".join(_NEWLY_PROTECTED_TABLES)
    op.execute(sa.text(f"GRANT UPDATE, DELETE ON {protected} TO {APP_ROLE}"))

    op.drop_index("ix_approvals_case_id", table_name="approvals")
    op.drop_index("ix_approvals_subject_type_subject_id", table_name="approvals")
    op.drop_table("approvals")

    op.drop_index("ix_cases_subject_type_subject_id", table_name="cases")
    op.drop_column("cases", "subject_id")
    op.drop_column("cases", "subject_type")
