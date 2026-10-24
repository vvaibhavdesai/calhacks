"""A05 exceptions/approvals/policies: additive columns on `cases` and
`policy_versions`. No new tables -- A05 builds on A02's existing
`cases`/`policy_versions` rows, per the task instructions ("build on A02/
A03's real Case rows, don't invent parallel case machinery").

Revision ID: 0006_case_policy_resolution
Revises: 0005_settlement
Create Date: 2026-10-24

Mirrors the additive columns added to Case/PolicyVersion in
services/api/app/models_ledger.py. Built on top of 0005_settlement per git
discipline -- 0001-0005 are never edited here, only added to.

`encore_app` already has default SELECT/INSERT/UPDATE/DELETE on `cases`/
`policy_versions` from migration 0003's `ALTER DEFAULT PRIVILEGES` (set up
when those tables were first created in 0002) -- no new GRANT statements
needed. Neither table is added to the append-only REVOKE list: unlike
`settlements`/`business_events`/etc. (computed, historical records that
must never change after the fact), Case.status/PolicyVersion.status are
legitimate WORKFLOW state that transitions in place over time (OPEN ->
RESOLVED, ACTIVE -> SUSPENDED/SUPERSEDED), the same way Run.status and
Payout.state already do. "Old applications of a suspended policy remain
visible/traceable" is satisfied because a Case's `applied_policy_version_id`
references the policy_version ROW BY ID -- that id/version never changes
even after the row's `status` column is later updated to SUSPENDED/
SUPERSEDED -- not because the row itself is frozen.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_case_policy_resolution"
down_revision: str | None = "0005_settlement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- cases: additive columns ------------------------------------------
    op.add_column("cases", sa.Column("business_date", sa.Date(), nullable=True))
    op.create_index("ix_cases_business_date", "cases", ["business_date"])

    op.add_column(
        "cases",
        sa.Column(
            "predicate_values",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column("cases", sa.Column("resolution_decision", sa.String(length=40), nullable=True))
    op.add_column("cases", sa.Column("resolution_note", sa.Text(), nullable=True))
    op.add_column(
        "cases",
        sa.Column("resolution_effect", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "cases",
        sa.Column(
            "resolved_by",
            sa.String(length=32),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("cases", sa.Column("resolved_capability", sa.String(length=100), nullable=True))
    op.add_column("cases", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))

    op.add_column(
        "cases", sa.Column("auto_resolved", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column(
        "cases",
        sa.Column(
            "applied_policy_version_id",
            sa.String(length=32),
            sa.ForeignKey(
                "policy_versions.id",
                ondelete="SET NULL",
                name="fk_cases_applied_policy_version_id",
            ),
            nullable=True,
        ),
    )
    op.create_index("ix_cases_applied_policy_version_id", "cases", ["applied_policy_version_id"])
    op.add_column(
        "cases",
        sa.Column(
            "origin_case_id",
            sa.String(length=32),
            sa.ForeignKey("cases.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_cases_origin_case_id", "cases", ["origin_case_id"])

    op.add_column("cases", sa.Column("challenge_status", sa.String(length=20), nullable=True))
    op.add_column(
        "cases",
        sa.Column(
            "challenged_by",
            sa.String(length=32),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("cases", sa.Column("challenged_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("cases", sa.Column("challenge_note", sa.Text(), nullable=True))

    # --- policy_versions: additive columns ---------------------------------
    op.add_column(
        "policy_versions",
        sa.Column(
            "source_case_id",
            sa.String(length=32),
            sa.ForeignKey("cases.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_policy_versions_source_case_id", "policy_versions", ["source_case_id"])
    op.add_column(
        "policy_versions", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("policy_versions", "approved_at")
    op.drop_index("ix_policy_versions_source_case_id", table_name="policy_versions")
    op.drop_column("policy_versions", "source_case_id")

    op.drop_column("cases", "challenge_note")
    op.drop_column("cases", "challenged_at")
    op.drop_column("cases", "challenged_by")
    op.drop_column("cases", "challenge_status")
    op.drop_index("ix_cases_origin_case_id", table_name="cases")
    op.drop_column("cases", "origin_case_id")
    op.drop_index("ix_cases_applied_policy_version_id", table_name="cases")
    op.drop_column("cases", "applied_policy_version_id")
    op.drop_column("cases", "auto_resolved")
    op.drop_column("cases", "resolved_at")
    op.drop_column("cases", "resolved_capability")
    op.drop_column("cases", "resolved_by")
    op.drop_column("cases", "resolution_effect")
    op.drop_column("cases", "resolution_note")
    op.drop_column("cases", "resolution_decision")
    op.drop_column("cases", "predicate_values")
    op.drop_index("ix_cases_business_date", table_name="cases")
    op.drop_column("cases", "business_date")
