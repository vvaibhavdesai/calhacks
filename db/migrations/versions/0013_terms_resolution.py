"""A06 stage 2 terms resolution: one additive column on `cases`.

Revision ID: 0013_terms_resolution
Revises: 0012_a09_corrections
Create Date: 2026-10-24

`cases.blocking`: whether an OPEN case withholds the vendor's money. Every
case kind that existed before A06 stage 2 did withhold it -- A07's
`release_payout` and the nightly close's `check_controls` both treat any
OPEN case on a vendor as a hard blocker -- so the column is NOT NULL with a
`true` server default and existing rows keep exactly their current meaning.

Terms resolution opens the first deliberately non-blocking case kind
(`minimum_period_claim`: a vendor states the minimum fee is per night,
the signed contract says per festival). The contract stands, a human still
sees the question, and no money is held while they look at it. Its two
blocking siblings (`vendor_contract_term_conflict` from A05's detector and
`beneficiary_change_unverified`) are written with the default.

0001-0012 are never edited. `cases` is not an append-only table and its
privileges do not change: `encore_app` already has SELECT/INSERT/UPDATE/
DELETE on it and the ALTER DEFAULT PRIVILEGES from 0003 covers nothing new
here (no table is created).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_terms_resolution"
down_revision: str | None = "0012_a09_corrections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column("blocking", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )


def downgrade() -> None:
    op.drop_column("cases", "blocking")
