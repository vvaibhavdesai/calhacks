"""A04 deterministic settlement engine: pure, side-effect-free finance math.

This is the first real consumer of `packages/domain/` (A01 left it an empty
placeholder with the docstring "pure deterministic finance functions ...
must stay free of I/O"). Every function in this module takes plain values
(ints, Decimals, dataclasses) and returns plain values. No database session,
no HTTP, no datetime.now(), no randomness. `services/api/app/settlement.py`
is the only caller that touches a database -- it resolves vendor contract
terms, reconciled orders/payments, and cumulative history, then calls into
this module to do the actual arithmetic.

Money rule source: AGENT_HANDOFF.md "Financial rules already approved" and
CONTRACTS.md C02 "custody and accounting contract". Read both before
changing anything here. Summary of what this module encodes:

  - Eligible sales = gross sales minus tax, minus tips, minus refunds.
    (Refunds are netted in by the caller before eligible sales cents reach
    this module -- see services/api/app/settlement.py's use of A03's
    reconciled `matched_amount_cents`. This module receives already-net
    eligible-sales figures, split by custody, and never re-derives them
    from gross/tax/tips itself.)
  - Default organizer commission: greater of 15% of CUMULATIVE eligible
    FESTIVAL sales, or $500 ONCE for the entire festival -- never $500 per
    night. Earlier nights assess cumulative-percentage-to-date minus
    commission already assessed on prior nights. The $500 minimum is only
    ever compared against on the final night of the festival
    (`is_final_period=True`), which is what makes "not multiplied by
    nights" true by construction: `calculate_commission_increment` never
    even evaluates `minimum_fee_cents` unless `is_final_period` is True.
  - Processor fees are deducted from a vendor's payout only when
    `processor_fee_deductible` says the vendor's contract permits it.
  - Tax and tips are carried through untouched (`tax_cents`/`tips_cents` on
    `SettlementLineResult`) and are NEVER read by any commission/minimum/
    fee-recovery calculation in this module. There is no code path in this
    file that subtracts from, or even reads, tax_cents/tips_cents when
    computing fee_assessed/fee_collected/unpaid_balance/vendor_payout.
  - If eligible proceeds cannot cover the assessed fee, the shortfall
    becomes `unpaid_balance_cents` -- for a human to later collect or
    waive -- not an automatic seizure of tax/tips and not a negative
    payout. `vendor_payout_cents` is always `max(..., 0)`.
  - Cash the vendor already holds (vendor-retained custody) never also
    produces a second bank payout for the same money: `vendor_payout_cents`
    is derived only from `centrally_held_cents` (funds the organizer
    actually holds for this vendor/period), never from
    `vendor_retained_cash_cents`. Any fee owed against vendor-retained cash
    that the organizer cannot net out of centrally-held funds becomes
    `vendor_fee_receivable_cents` -- collected from the vendor directly
    later, never conjured out of funds the organizer doesn't have.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

# Calculation version pinned onto every settlement row this module's output
# feeds (per the task's "freeze the calculation" requirement). Bump this
# string whenever the arithmetic in this module changes in a way that could
# alter a historical result; old settlement rows keep citing whatever
# version was actually used to compute them.
CALCULATION_VERSION = "a04-settlement-v1"

# AGENT_HANDOFF.md: "Default organizer fee: greater of 15% of eligible
# sales or $500 per vendor for the entire festival". Used only when a
# vendor's active VendorContractTerm leaves commission_bps/
# minimum_fee_cents null (per models_ledger.py's documented convention:
# "null means use the festival default, never zero").
DEFAULT_COMMISSION_BPS = 1500
DEFAULT_MINIMUM_FEE_CENTS = 50_000  # $500.00


def round_half_up_cents(value: Decimal) -> int:
    """ROUND_HALF_UP to the nearest cent, per C01's documented rounding
    point. `value` is already expressed in cents (not dollars); this just
    rounds a fractional-cent Decimal (e.g. from a basis-point multiply) to
    the nearest whole cent, half rounding away from zero."""
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def percentage_commission_cents(eligible_sales_cents: int, commission_bps: int) -> int:
    """15% of cumulative eligible sales, as basis points, rounded once to
    the cent. `commission_bps=1500` means 15.00%."""
    if eligible_sales_cents < 0:
        raise ValueError("eligible_sales_cents cannot be negative")
    if commission_bps < 0:
        raise ValueError("commission_bps cannot be negative")
    raw = Decimal(eligible_sales_cents) * Decimal(commission_bps) / Decimal(10_000)
    return round_half_up_cents(raw)


@dataclass(frozen=True)
class CommissionCalculation:
    """Result of evaluating the commission ladder for one period
    (vendor-day), given cumulative festival-to-date eligible sales."""

    cumulative_eligible_sales_cents: int
    cumulative_percentage_commission_cents: int
    cumulative_commission_assessed_prior_cents: int
    commission_increment_cents: int
    minimum_applied: bool
    is_final_period: bool


def calculate_commission_increment(
    *,
    cumulative_eligible_sales_cents: int,
    cumulative_commission_assessed_prior_cents: int,
    commission_bps: int = DEFAULT_COMMISSION_BPS,
    minimum_fee_cents: int = DEFAULT_MINIMUM_FEE_CENTS,
    is_final_period: bool,
) -> CommissionCalculation:
    """Core of the "greater of 15% cumulative or $500 once" rule.

    `cumulative_eligible_sales_cents` is the running total of eligible
    sales for this vendor across the WHOLE festival through and including
    this period -- not just this period's sales. `cumulative_commission_
    assessed_prior_cents` is the sum of `commission_increment_cents`
    already returned by this function for this vendor's EARLIER periods
    this festival (never including corrections/adjustments -- those are a
    separate ledger, see `calculate_settlement_adjustment` below).

    On a non-final period, only the percentage-to-date is ever compared
    against (minimum_fee_cents is not read at all) -- so the $500 minimum
    can never be assessed more than once no matter how many nights there
    are. On the final period, the festival-wide total commission is
    `max(percentage_to_date, minimum_fee_cents)`; the increment charged
    THIS period is that total minus whatever was already assessed on
    earlier periods, which could be the percentage top-up, the minimum
    top-up, or (if the percentage already exceeded the minimum on an
    earlier night) zero.
    """
    if cumulative_commission_assessed_prior_cents < 0:
        raise ValueError("cumulative_commission_assessed_prior_cents cannot be negative")

    percentage_to_date = percentage_commission_cents(
        cumulative_eligible_sales_cents, commission_bps
    )

    if is_final_period:
        total_due_cents = max(percentage_to_date, minimum_fee_cents)
        minimum_applied = total_due_cents > percentage_to_date
    else:
        total_due_cents = percentage_to_date
        minimum_applied = False

    increment_cents = total_due_cents - cumulative_commission_assessed_prior_cents
    # Defensive floor only: with monotonically non-decreasing cumulative
    # eligible sales across periods (the normal case), total_due_cents is
    # non-decreasing too, so this should never actually trigger. It exists
    # so a caller error (e.g. periods processed out of order) produces a
    # visible $0 increment rather than a negative commission charge.
    if increment_cents < 0:
        increment_cents = 0

    return CommissionCalculation(
        cumulative_eligible_sales_cents=cumulative_eligible_sales_cents,
        cumulative_percentage_commission_cents=percentage_to_date,
        cumulative_commission_assessed_prior_cents=cumulative_commission_assessed_prior_cents,
        commission_increment_cents=increment_cents,
        minimum_applied=minimum_applied,
        is_final_period=is_final_period,
    )


@dataclass(frozen=True)
class EligibleSalesInput:
    """Eligible-sales facts for one vendor/period, already split by
    custody and already net of refunds (the caller derives this from A03's
    reconciled `matched_amount_cents`, not from raw gross/tax/tips). Tax
    and tips are carried for display/traceability only -- see module
    docstring: nothing downstream reads them for fee purposes."""

    centrally_held_cents: int
    vendor_retained_cash_cents: int
    tax_cents: int = 0
    tips_cents: int = 0

    @property
    def total_eligible_cents(self) -> int:
        return self.centrally_held_cents + self.vendor_retained_cash_cents


@dataclass(frozen=True)
class SettlementLineResult:
    """Everything the task brief asks to "track separately and keep
    distinct at all times": fee assessed, fee actually collected,
    vendor-owned/retained cash, funds held centrally, and vendor payout
    payable -- plus the two new, equally distinct quantities needed to
    keep the minimum-fee-shortfall and vendor-retained-cash cases honest
    instead of collapsing into one ambiguous "amount owed" number:

      - `unpaid_balance_cents`: the part of fee_assessed that cannot be
        covered even by this vendor's TOTAL eligible proceeds (central +
        vendor-retained) -- the genuine "proceeds insufficient" case
        (fixture 3). For a human to later collect or waive. NEVER funded
        by tax_cents/tips_cents.
      - `vendor_fee_receivable_cents`: the part of fee_assessed that total
        eligible proceeds COULD cover, but centrally-held funds alone
        couldn't, because the money is vendor-retained cash the organizer
        never touched (fixture 4). Collected from the vendor directly (or
        offset against a later period's centrally-held funds for the same
        vendor), never manufactured as a bank payout, and never conflated
        with unpaid_balance_cents.

    `fee_uncollected_cents = unpaid_balance_cents + vendor_fee_receivable_
    cents` always holds (asserted in __post_init__).
    """

    eligible_sales_cents: int
    centrally_held_cents: int
    vendor_retained_cash_cents: int
    tax_cents: int
    tips_cents: int
    fee_assessed_cents: int
    fee_collected_from_central_cents: int
    vendor_fee_receivable_cents: int
    unpaid_balance_cents: int
    processor_fee_assessed_cents: int
    processor_fee_collected_cents: int
    vendor_payout_cents: int
    minimum_applied: bool
    is_final_period: bool
    cumulative_eligible_sales_cents: int
    cumulative_commission_assessed_prior_cents: int
    calculation_version: str = CALCULATION_VERSION

    def __post_init__(self) -> None:
        # Invariants re-checked at construction time, not just at the call
        # site below -- a future edit that builds this dataclass a
        # different way still can't silently violate them.
        if self.vendor_payout_cents < 0:
            raise ValueError("vendor_payout_cents must never be negative")
        if self.unpaid_balance_cents < 0 or self.vendor_fee_receivable_cents < 0:
            raise ValueError("unpaid_balance_cents/vendor_fee_receivable_cents cannot be negative")
        fee_uncollected = self.fee_assessed_cents - self.fee_collected_from_central_cents
        if fee_uncollected != self.unpaid_balance_cents + self.vendor_fee_receivable_cents:
            raise ValueError(
                "fee_assessed_cents - fee_collected_from_central_cents must equal "
                "unpaid_balance_cents + vendor_fee_receivable_cents"
            )


def calculate_vendor_settlement(
    *,
    eligible: EligibleSalesInput,
    cumulative_eligible_sales_cents: int,
    cumulative_commission_assessed_prior_cents: int,
    commission_bps: int = DEFAULT_COMMISSION_BPS,
    minimum_fee_cents: int = DEFAULT_MINIMUM_FEE_CENTS,
    is_final_period: bool,
    processor_fee_assessed_cents: int = 0,
    processor_fee_deductible: bool = False,
) -> SettlementLineResult:
    """Compute one vendor/period's full settlement line, per C02.

    `cumulative_eligible_sales_cents` must already include THIS period's
    `eligible.total_eligible_cents` (it is the running festival-to-date
    total through and including this period) -- the caller is responsible
    for that running sum; this function does not re-derive it from a
    history list to keep the function's contract simple and the caller's
    intent explicit at each call site.
    """
    commission = calculate_commission_increment(
        cumulative_eligible_sales_cents=cumulative_eligible_sales_cents,
        cumulative_commission_assessed_prior_cents=cumulative_commission_assessed_prior_cents,
        commission_bps=commission_bps,
        minimum_fee_cents=minimum_fee_cents,
        is_final_period=is_final_period,
    )
    fee_assessed = commission.commission_increment_cents

    central = max(eligible.centrally_held_cents, 0)
    vendor_retained = max(eligible.vendor_retained_cash_cents, 0)
    total_eligible = central + vendor_retained

    # Fee is collected from centrally-held funds first, and ONLY from
    # centrally-held funds -- this is the mechanism that keeps
    # vendor-retained cash from ever being "seized" by the fee logic.
    fee_collected_from_central = min(fee_assessed, central)
    fee_uncollected = fee_assessed - fee_collected_from_central

    # Genuine insufficiency: even total eligible proceeds (central +
    # vendor-retained) can't cover the assessed fee. This portion becomes
    # unpaid_balance, never funded by tax/tips, never a negative payout.
    unpaid_balance = max(fee_assessed - total_eligible, 0)
    # The remainder of fee_uncollected (if any) is money the vendor is
    # holding (vendor-retained cash) that the organizer is owed but never
    # held itself -- a receivable, not a payout deduction.
    vendor_fee_receivable = fee_uncollected - unpaid_balance

    payout_before_processor_fee = max(central - fee_collected_from_central, 0)

    if processor_fee_deductible and processor_fee_assessed_cents > 0:
        processor_fee_collected = min(processor_fee_assessed_cents, payout_before_processor_fee)
        vendor_payout = payout_before_processor_fee - processor_fee_collected
    else:
        processor_fee_collected = 0
        vendor_payout = payout_before_processor_fee

    return SettlementLineResult(
        eligible_sales_cents=total_eligible,
        centrally_held_cents=central,
        vendor_retained_cash_cents=vendor_retained,
        tax_cents=eligible.tax_cents,
        tips_cents=eligible.tips_cents,
        fee_assessed_cents=fee_assessed,
        fee_collected_from_central_cents=fee_collected_from_central,
        vendor_fee_receivable_cents=vendor_fee_receivable,
        unpaid_balance_cents=unpaid_balance,
        processor_fee_assessed_cents=(
            processor_fee_assessed_cents if processor_fee_deductible else 0
        ),
        processor_fee_collected_cents=processor_fee_collected,
        vendor_payout_cents=max(vendor_payout, 0),
        minimum_applied=commission.minimum_applied,
        is_final_period=is_final_period,
        cumulative_eligible_sales_cents=cumulative_eligible_sales_cents,
        cumulative_commission_assessed_prior_cents=cumulative_commission_assessed_prior_cents,
    )


@dataclass(frozen=True)
class SettlementAdjustmentResult:
    """A linked correction's financial impact, computed by comparing a
    freshly recalculated `SettlementLineResult` against the result that was
    actually paid out before. Never mutates or re-derives the original --
    the caller (services/api/app/settlement.py) is responsible for storing
    this as a NEW settlement row with `correction_of` pointing at the
    original, never overwriting it.

    `additional_payout_cents` and `overpayment_recovery_cents` are mutually
    exclusive by construction (at most one is nonzero) and both are always
    >= 0 -- this is deliberately how "late refund after confirmed payout"
    (fixture 5) and any other post-payout recalculation stay representable
    without ever emitting a negative payout: an overpayment becomes a
    receivable to recover, not a negative number flowing back through the
    payout field.
    """

    previous_fee_assessed_cents: int
    updated_fee_assessed_cents: int
    delta_fee_assessed_cents: int
    previous_vendor_payout_cents: int
    updated_vendor_payout_cents: int
    amount_already_paid_cents: int
    additional_payout_cents: int
    overpayment_recovery_cents: int
    updated: SettlementLineResult

    def __post_init__(self) -> None:
        if self.additional_payout_cents < 0 or self.overpayment_recovery_cents < 0:
            raise ValueError("adjustment payout/recovery amounts cannot be negative")
        if self.additional_payout_cents > 0 and self.overpayment_recovery_cents > 0:
            raise ValueError(
                "an adjustment cannot simultaneously owe additional payout and demand recovery"
            )


def calculate_settlement_adjustment(
    *,
    previous: SettlementLineResult,
    updated: SettlementLineResult,
    amount_already_paid_cents: int,
) -> SettlementAdjustmentResult:
    """Given a settlement that was already computed (and, per the caller,
    already paid out `amount_already_paid_cents` for real), and a freshly
    recomputed `updated` result reflecting new facts (e.g. a late refund
    that reduced eligible sales), compute the correction's financial
    impact.

    If the vendor is now owed MORE than they were already paid,
    `additional_payout_cents` is the extra amount still owed -- itself
    still subject to the "never negative" rule because it is a plain
    subtraction of a smaller number from a larger one. If the vendor was
    overpaid (the new, lower entitlement is less than what already went
    out), the shortfall becomes `overpayment_recovery_cents`: an amount for
    a human to recover from the vendor, never a negative payout and never
    auto-deducted from tax/tips.
    """
    if amount_already_paid_cents < 0:
        raise ValueError("amount_already_paid_cents cannot be negative")

    if amount_already_paid_cents > updated.vendor_payout_cents:
        overpayment_recovery = amount_already_paid_cents - updated.vendor_payout_cents
        additional_payout = 0
    else:
        overpayment_recovery = 0
        additional_payout = updated.vendor_payout_cents - amount_already_paid_cents

    return SettlementAdjustmentResult(
        previous_fee_assessed_cents=previous.fee_assessed_cents,
        updated_fee_assessed_cents=updated.fee_assessed_cents,
        delta_fee_assessed_cents=updated.fee_assessed_cents - previous.fee_assessed_cents,
        previous_vendor_payout_cents=previous.vendor_payout_cents,
        updated_vendor_payout_cents=updated.vendor_payout_cents,
        amount_already_paid_cents=amount_already_paid_cents,
        additional_payout_cents=additional_payout,
        overpayment_recovery_cents=overpayment_recovery,
        updated=updated,
    )
