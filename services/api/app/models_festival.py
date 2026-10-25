"""The festival entity: the record every festival-scoped read needs.

Why this table did not already exist, and why it has to now: `festival_id`
is a plain `String(64)` on ~25 tables (`vendors`, `settlements`,
`bank_entries`, `finance_configs`, `daily_closes`, `cash_pools`, `runs`,
...) but the festival itself was never stored anywhere. The values were
minted by whoever inserted the first row -- the dev fixtures hardcode them
(`scripts/dev_fixtures_a03.py`'s "gcmf-2026-a03dev", a05's
"gcmf-2026-a05dev", a07's "gcmf-2026-a07dev-main", ...), and the import
path never mints one at all: a source document is festival-scoped only
through its vendor (`app/agents/runner.py`'s `festival_documents`). The
consequence A11 ran into is that the scheduler has to be *told*
`SCHEDULE_FESTIVAL_ID` from the environment, because there was nothing in
the database to discover a festival from.

Two deliberate design decisions follow from that history:

  1. `festivals.id` is NOT a generated id (no `default=_new_id`, unlike
     every other table here). It is the externally minted `festival_id`
     string that existing rows already carry, `String(64)` to match every
     column that references it. Registering a festival is therefore how
     already-inserted rows start resolving -- no backfill, no re-pointing
     of existing data, no foreign key added to tables another task owns.

  2. Bank accounts are NOT stored here. They already exist as
     `opening_balances` rows (`account_role`/`as_of`/`amount_cents`/
     `source_refs`) and as `bank_entries.account_id`; a second copy on the
     festival could disagree with the journal and with reconciliation.
     `app/festivals.py` reads them from those tables instead, tagging each
     one with where it came from.

Mutable for `encore_app`: this is configuration (a name, a timezone, a date
range), not committed history, so it takes migration 0003's default grants
and migration 0015 adds no GRANT and no REVOKE. The audit trail of a
registration is a `business_events` row appended through
`app.uow.UnitOfWork`, and that table stays append-only and untouched.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Festival(Base):
    """One festival. `status`/`currency` are uppercase wire values, matching
    the drafted Festival contract shape (ACTIVE, USD); `currency` is kept as
    a column rather than assumed so a second festival can never silently
    inherit USD, even though C01 fixes v1 to USD."""

    __tablename__ = "festivals"

    # The externally minted festival_id -- see the module docstring.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    organizer_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="America/Los_Angeles")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Carried through to the read so the synthetic-data disclaimer travels
    # with the record instead of being hardcoded in a screen.
    fictional_notice: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
