"""Persist the processor fee the source states per payment.

Revision ID: 0019_payment_processor_fee
Revises: 0018_s_track_gaps
Create Date: 2026-10-25

One additive, nullable column: `payments.processor_fee_cents`.

Why it has to be stored rather than derived. A processor settlement file
states a fee PER PAYMENT, and the batch total is the sum of them. Lane A
was keeping only the batch total (`bank_entries.batch_fee_cents`), so
app/settlement.py had to attribute a vendor's share of it by apportioning
the batch fee across the payments by amount. That apportionment is not the
fee: the processor's own schedule is per-transaction (a fixed component
plus a percentage), so a vendor with many small tickets really does pay a
larger share of the batch fee than its share of the batch amount. Scored
against the independent oracle on full_v1, the apportionment was the ONLY
remaining difference on 43 of 240 vendor-nights -- always in the
processor-fee line and therefore in the payout, never in the eligible
sales or the commission.

Nullable, with no backfill: a payment imported before this column existed
has no stated fee to record, and settlement falls back to apportioning the
batch total for exactly those rows (see `_processor_fee_share`). A cash
payment never has one either -- it carries no processor.

`payments` is append-only for the app role from 0005 onward and this
column is populated at insert time, so no grant changes.

0001-0018 are never edited here.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_payment_processor_fee"
down_revision: str | None = "0018_s_track_gaps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payments",
        sa.Column("processor_fee_cents", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("payments", "processor_fee_cents")
