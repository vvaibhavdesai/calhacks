"""S-track gap closure: receipt acceptor, partial-payment-allowed flag.

Revision ID: 0018_s_track_gaps
Revises: 0013_r1_readiness
Create Date: 2026-10-25

Two additive columns only, both nullable/defaulted so every existing row
is unaffected and every existing caller keeps working unchanged:

1. `delivery_receipts.accepted_by` (S1 "delivery_record... accepted_by") --
   nullable FK to `users.id`, SET NULL on delete. Append-only table
   (0017 already revoked UPDATE/DELETE); this column is populated at
   insert time only, never edited after.
2. `purchase_orders.partial_payment_allowed` (S3's contract flag) --
   NOT NULL, default true. True (the default, matching every PO before
   this) holds only the disputed portion, exactly today's verified
   delivery-supported-payment-limit behavior; false holds the whole
   invoice until fully delivered (domain.payables.check_payment's new
   guard).

0001-0017 are never edited here.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_s_track_gaps"
down_revision: str | None = "0013_r1_readiness"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "delivery_receipts",
        sa.Column(
            "accepted_by",
            sa.String(length=32),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "purchase_orders",
        sa.Column(
            "partial_payment_allowed", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )


def downgrade() -> None:
    op.drop_column("purchase_orders", "partial_payment_allowed")
    op.drop_column("delivery_receipts", "accepted_by")
