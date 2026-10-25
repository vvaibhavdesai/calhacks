"""Synthetic procurement cash-impact baseline (TICKET section 7).

The real festival bank account opens 2026-10-15 (per the fixture), so a
real-books cash position at Oct 2 would show ~0 for everything and every
quote would falsely look like a buffer breach. `cash_inputs.json`'s
opening $60,000 / assumed $50,000 buffer is a LABELLED SYNTHETIC baseline
for this one comparison screen -- not approved business policy, and never
written to `cash_pools`, `opening_balances` or the journal (this module
takes no DB session; it's pure arithmetic over plain numbers the caller
already loaded from the fixture). Once `as_of` reaches the real opening
date, the caller should show `domain.cash.cash_position` alongside this as
the real baseline -- this module does not decide when that switch happens.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class CashBreachResult:
    minimum_closing_cents: int
    first_breach_date: date | None
    buffer_gap_cents: int


def synthetic_cash_breach(
    *,
    as_of: date,
    opening_posted_cents: int,
    buffer_cents: int,
    scheduled_outflows: list[tuple[date, int]],
    deposit_due: date,
    deposit_cents: int,
    horizon_end: date,
) -> CashBreachResult:
    """Day-by-day running balance from `as_of` to `horizon_end`, counting
    ONLY posted opening cash, confirmed scheduled outflows and the quote's
    own deposit -- never expected/future-actual receipts (those must never
    count as available cash for a decision; project-wide rule). Verified
    against the fixture: a $6,000,000 opening with a $500,000 site payment
    on Oct 4 and a $500,000 deposit due Oct 5 gives a $5,000,000 minimum
    (no breach against a $5,000,000 buffer); a $1,200,000 deposit due the
    same day gives $4,300,000 (a $700,000 breach)."""
    balance = opening_posted_cents
    minimum = balance
    first_breach: date | None = None
    if balance < buffer_cents:
        first_breach = as_of

    day = as_of
    while day <= horizon_end:
        if day != as_of:
            for outflow_date, amount in scheduled_outflows:
                if outflow_date == day:
                    balance -= amount
            if day == deposit_due:
                balance -= deposit_cents
            if balance < minimum:
                minimum = balance
            if balance < buffer_cents and first_breach is None:
                first_breach = day
        day = day + timedelta(days=1)

    gap = max(buffer_cents - minimum, 0)
    return CashBreachResult(
        minimum_closing_cents=minimum, first_breach_date=first_breach, buffer_gap_cents=gap
    )
