"""A08 cash position: pure classification of organizer cash.

Inputs are journal balances (debit-positive, by account role), the amount
currently reserved for in-flight vendor payouts (A07), and dated upcoming
obligations. Output separates:

  - bank_cents: book balance of the operating bank account;
  - pending_cents: money the organizer holds but that is not in the bank
    yet (processor clearing, cash office) -- not spendable today;
  - reserved_payouts_cents: bank cash reserved for released-but-unconfirmed
    vendor payouts (A07 reservations; DISPATCH_PENDING/SUBMITTED/UNKNOWN);
  - protected_cents: tax, tips and customer refunds held for others. These
    can never become freely spendable income;
  - vendor_funds_payable_cents: vendor entitlements not yet paid out (the
    reserved payouts are a subset of this, so they are not subtracted twice);
  - obligations due within the horizon (supplier AP etc.);
  - freely_available_cents = bank - protected - vendor payable - obligations
    due within horizon. May be negative: that is a shortfall to show, not a
    number to hide.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Obligation:
    kind: str
    ref_id: str
    due_date: date | None
    amount_cents: int


@dataclass(frozen=True)
class CountCheck:
    """Counted cash vs what the POS says was collected centrally in cash."""

    counted_cents: int
    expected_cents: int

    @property
    def variance_cents(self) -> int:
        """counted - expected: negative is a shortage, positive an overage."""
        return self.counted_cents - self.expected_cents

    @property
    def status(self) -> str:
        return "MATCHED" if self.variance_cents == 0 else "VARIANCE"


@dataclass(frozen=True)
class SlipCheck:
    """A deposit slip: bags (each optionally tied to a count) vs the bank."""

    bag_total_cents: int
    stated_total_cents: int
    bank_cents: int | None  # None while the bank credit has not landed
    unlabelled_bag_count: int
    bag_count_mismatches: int

    @property
    def bank_variance_cents(self) -> int | None:
        return None if self.bank_cents is None else self.bank_cents - self.bag_total_cents

    @property
    def status(self) -> str:
        if self.bank_cents is None:
            return "AWAITING_BANK_CREDIT"
        if (
            self.bank_variance_cents
            or self.unlabelled_bag_count
            or self.bag_count_mismatches
            or self.stated_total_cents != self.bag_total_cents
        ):
            return "CASE_REQUIRED"
        return "MATCHED"


def _liability(bal: dict[str, int], role: str) -> int:
    return -bal.get(role, 0)


def cash_position(
    *,
    balances_by_role: dict[str, int],
    reserved_payouts_cents: int,
    obligations: list[Obligation],
    as_of: date,
    horizon_days: int,
) -> dict:
    horizon_end = as_of + timedelta(days=horizon_days)
    bank = balances_by_role.get("BANK_OPERATING", 0)
    pending = balances_by_role.get("PROCESSOR_CLEARING", 0) + balances_by_role.get("CASH_OFFICE", 0)
    protected = {
        "sales_tax_cents": _liability(balances_by_role, "SALES_TAX_LIABILITY"),
        "tips_cents": _liability(balances_by_role, "TIPS_LIABILITY"),
        "customer_refunds_cents": _liability(balances_by_role, "CUSTOMER_REFUNDS_PAYABLE"),
    }
    protected_total = sum(protected.values())
    vendor_payable = _liability(balances_by_role, "VENDOR_FUNDS_PAYABLE")

    within = [o for o in obligations if o.due_date is None or o.due_date <= horizon_end]
    later = [o for o in obligations if o.due_date is not None and o.due_date > horizon_end]
    due_within = sum(o.amount_cents for o in within)

    return {
        "as_of": as_of.isoformat(),
        "forecast_horizon_days": horizon_days,
        "horizon_end": horizon_end.isoformat(),
        "bank_cents": bank,
        "pending_cents": pending,
        "reserved_payouts_cents": reserved_payouts_cents,
        "protected": protected,
        "protected_cents": protected_total,
        "vendor_funds_payable_cents": vendor_payable,
        "obligations_due_within_horizon_cents": due_within,
        "obligations_after_horizon_cents": sum(o.amount_cents for o in later),
        "freely_available_cents": bank - protected_total - vendor_payable - due_within,
        "upcoming_obligations": [
            {
                "kind": o.kind,
                "ref_id": o.ref_id,
                "due_date": o.due_date.isoformat() if o.due_date else None,
                "amount_cents": o.amount_cents,
                "within_horizon": o in within,
            }
            for o in sorted(obligations, key=lambda o: (o.due_date or date.min, o.ref_id))
        ],
    }
