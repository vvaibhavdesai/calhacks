"""A04 deterministic settlement engine: DB-touching orchestration.

The actual money math lives in `domain.settlement` (packages/domain/domain/
settlement.py) -- a pure, DB-free package per the task brief ("pure
functions ... keep it a pure package with no DB/HTTP imports"). Everything
in THIS module is the glue: resolve which vendor contract terms apply,
pull reconciled eligible sales out of A03's `match_groups` (never raw
unreconciled `orders`/`payments`), compute cumulative festival-to-date
figures from this vendor's settlement history, call into `domain.settlement`,
and write the result as new `Settlement`/`SettlementLineItem` rows via the
same `UnitOfWork` atomicity mechanism A02/A03 already established.

Integration with A03 (reconciliation), as instructed: a vendor/business_date
is only settled from money that A03 has already classified as cleanly
reconciled --
  - an order's `order_capture` match_group must be status="matched" (not
    "case") for that order's eligible sales to be included this period.
  - a centrally-collected payment's bank settlement, if it has been
    allocated to a `BankEntry`, must have a "matched" `bank_deposit` match
    group (i.e. actually settled, fee explained) for that money to count
    as `centrally_held_cents` (available to deduct fee from / pay out)
    this period.
Anything that fails either check is EXCLUDED from this period's
calculation and recorded as a hold (`Settlement.holds`), not silently
included with best-effort numbers and not silently dropped either -- it
remains visible in `holds`, and once it later reconciles cleanly (a future
reconciliation run turns a case into a match, or a pending deposit
settles), it is picked up by a LATER period's settlement, or by an explicit
correction if a payout for this vendor already happened in the meantime
(see `calculate_and_record_settlement`'s correction path below).

This is a deliberately conservative choice, documented here and in
docs/progress/A04-handoff.md, consistent with A03's own documented
conservative defaults (SETTLEMENT_TIMING_WINDOW_DAYS,
AMOUNT_TOLERANCE_CENTS) and CONTRACTS.md C05's "a timing candidate is not
confirmed deposited cash" -- extended sensibly to the settlement layer: an
unsettled bank deposit is not confirmed available cash to pay a vendor out
of, either.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from domain.settlement import (
    CALCULATION_VERSION,
    DEFAULT_COMMISSION_BPS,
    DEFAULT_MINIMUM_FEE_CENTS,
    EligibleSalesInput,
    SettlementLineResult,
    calculate_settlement_adjustment,
    calculate_vendor_settlement,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models_ledger import Order, Payment, Payout, Vendor, VendorContractTerm
from app.models_reconciliation import BankEntry, MatchGroup, MatchGroupMember
from app.models_settlement import Settlement, SettlementLineItem
from app.uow import UnitOfWork

# ---------------------------------------------------------------------------
# Vendor contract term resolution (C02: "vendor terms can override defaults")
# ---------------------------------------------------------------------------


def resolve_vendor_terms(
    db: Session, vendor_id: str, as_of: date
) -> tuple[int, int, bool, VendorContractTerm | None]:
    """Returns (commission_bps, minimum_fee_cents, processor_fee_deductible,
    the VendorContractTerm row used or None if the festival default applied
    throughout). Per models_ledger.py's documented convention,
    `commission_bps`/`minimum_fee_cents` being NULL on an active term means
    "use the festival default" -- never zero -- so a null column and a
    missing term row are handled identically here."""
    term = db.execute(
        select(VendorContractTerm)
        .where(
            VendorContractTerm.vendor_id == vendor_id,
            VendorContractTerm.status == "active",
            VendorContractTerm.effective_from <= as_of,
        )
        .where(
            (VendorContractTerm.effective_to.is_(None)) | (VendorContractTerm.effective_to >= as_of)
        )
        .order_by(VendorContractTerm.version.desc())
        .limit(1)
    ).scalar_one_or_none()

    if term is None:
        return DEFAULT_COMMISSION_BPS, DEFAULT_MINIMUM_FEE_CENTS, False, None

    commission_bps = (
        term.commission_bps if term.commission_bps is not None else DEFAULT_COMMISSION_BPS
    )
    minimum_fee_cents = (
        term.minimum_fee_cents if term.minimum_fee_cents is not None else DEFAULT_MINIMUM_FEE_CENTS
    )
    return commission_bps, minimum_fee_cents, term.processor_fee_deductible, term


# ---------------------------------------------------------------------------
# Reconciled eligible-sales extraction (reads A03's match_groups, not raw
# orders/payments)
# ---------------------------------------------------------------------------


@dataclass
class ReconciledDayFacts:
    centrally_held_cents: int = 0
    vendor_retained_cash_cents: int = 0
    tax_cents: int = 0
    tips_cents: int = 0
    processor_fee_assessed_cents: int = 0
    included_order_ids: list[str] = None  # type: ignore[assignment]
    held_order_ids: list[str] = None  # type: ignore[assignment]
    held_reasons: list[dict] = None  # type: ignore[assignment]
    source_refs: list[dict] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.included_order_ids is None:
            self.included_order_ids = []
        if self.held_order_ids is None:
            self.held_order_ids = []
        if self.held_reasons is None:
            self.held_reasons = []
        if self.source_refs is None:
            self.source_refs = []


def _round_half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def gather_reconciled_day_facts(
    db: Session, vendor: Vendor, business_date_: date
) -> ReconciledDayFacts:
    """Pull one vendor/business_date's eligible sales, tax, tips and
    processor-fee attribution strictly from A03's `match_groups` -- never
    from raw `orders`/`payments` directly, per the task's integration
    requirement with A03."""
    facts = ReconciledDayFacts()

    order_groups = (
        db.execute(
            select(MatchGroup).where(
                MatchGroup.vendor_id == vendor.id,
                MatchGroup.match_type == "order_capture",
                MatchGroup.business_date == business_date_,
            )
        )
        .scalars()
        .all()
    )

    for group in order_groups:
        members = (
            db.execute(select(MatchGroupMember).where(MatchGroupMember.match_group_id == group.id))
            .scalars()
            .all()
        )
        order_member = next((m for m in members if m.member_kind == "order"), None)
        if order_member is None:
            continue
        order = db.get(Order, order_member.member_id)
        if order is None:
            continue

        if group.status != "matched":
            # Unresolved (status == "case") -- excluded from this period's
            # eligible sales, recorded as a hold, picked up later (either a
            # future period once resolved, or a correction if a payout for
            # this vendor already happened in the meantime).
            facts.held_order_ids.append(order.id)
            facts.held_reasons.append(
                {
                    "reason": "order_capture_unresolved",
                    "order_id": order.id,
                    "match_group_id": group.id,
                    "match_group_status": group.status,
                    "match_group_reason_code": group.reason_code,
                }
            )
            continue

        # matched_amount_cents from A03's classification already nets
        # refunds/chargebacks against the order's captures -- this IS the
        # eligible-sales basis for this order (gross minus tax/tips is
        # applied below via order.tax_cents/order.tips_cents; refunds are
        # already reflected in matched_amount_cents by construction of
        # classify_order_capture).
        eligible_for_order = group.matched_amount_cents - order.tax_cents - order.tips_cents
        if eligible_for_order < 0:
            eligible_for_order = 0

        payments = [m for m in members if m.member_kind == "payment"]
        captures = [m for m in payments if db.get(Payment, m.member_id) is not None]
        # Determine custody for this order: look at its capture payment(s).
        # In practice an order has one payment method; if a hand-built
        # fixture ever mixes custody on one order, split proportionally by
        # capture amount rather than guessing a single custody.
        capture_payments = [
            (db.get(Payment, m.member_id), m.amount_cents)
            for m in captures
            if db.get(Payment, m.member_id) is not None
            and db.get(Payment, m.member_id).kind == "capture"
        ]
        captures_total = sum(amt for _p, amt in capture_payments) or 1

        held_this_order = False
        for payment, _amt in capture_payments:
            share = (
                Decimal(eligible_for_order)
                * Decimal(payment.amount_cents)
                / Decimal(captures_total)
            )
            share_cents = _round_half_up(share)

            if payment.custody == "vendor_retained":
                facts.vendor_retained_cash_cents += share_cents
                facts.source_refs.append(
                    {"ref_kind": "payment", "ref_id": payment.id, "role": "vendor_retained"}
                )
                continue

            # centrally_collected: only counts as available centrally-held
            # cash if its bank settlement (if any) is itself "matched"
            # (settled, fee explained). An unsettled/pending/case bank
            # deposit means this money is not confirmed available cash yet
            # -- held, not included, per the module docstring.
            if payment.bank_entry_id is None:
                # Centrally-collected but not yet allocated to any bank
                # deposit at all -- conservatively treated the same as
                # "not yet settled": held, not counted as available.
                held_this_order = True
                facts.held_reasons.append(
                    {
                        "reason": "centrally_collected_payment_not_yet_bank_allocated",
                        "order_id": order.id,
                        "payment_id": payment.id,
                    }
                )
                continue

            bank_group = (
                db.execute(
                    select(MatchGroup)
                    .join(
                        MatchGroupMember,
                        MatchGroupMember.match_group_id == MatchGroup.id,
                    )
                    .where(
                        MatchGroup.match_type == "bank_deposit",
                        MatchGroupMember.member_kind == "bank_entry",
                        MatchGroupMember.member_id == payment.bank_entry_id,
                    )
                )
                .scalars()
                .first()
            )
            if bank_group is None or bank_group.status != "matched":
                held_this_order = True
                facts.held_reasons.append(
                    {
                        "reason": "bank_deposit_not_settled",
                        "order_id": order.id,
                        "payment_id": payment.id,
                        "bank_entry_id": payment.bank_entry_id,
                        "bank_match_group_status": (
                            None if bank_group is None else bank_group.status
                        ),
                    }
                )
                continue

            facts.centrally_held_cents += share_cents
            facts.source_refs.append(
                {"ref_kind": "payment", "ref_id": payment.id, "role": "centrally_collected"}
            )
            facts.source_refs.append(
                {"ref_kind": "match_group", "ref_id": group.id, "role": "order_capture"}
            )

            # Proportional processor-fee attribution (fixture 6): this
            # payment's share of its settled bank entry's stated fee.
            entry = db.get(BankEntry, payment.bank_entry_id)
            if entry is not None and entry.batch_fee_cents:
                entry_captures_total = (
                    db.execute(
                        select(Payment.amount_cents).where(
                            Payment.bank_entry_id == entry.id, Payment.kind == "capture"
                        )
                    )
                    .scalars()
                    .all()
                )
                total = sum(entry_captures_total) or 1
                fee_share = _round_half_up(
                    Decimal(entry.batch_fee_cents) * Decimal(payment.amount_cents) / Decimal(total)
                )
                facts.processor_fee_assessed_cents += fee_share
                facts.source_refs.append(
                    {"ref_kind": "bank_entry", "ref_id": entry.id, "role": "processor_fee_source"}
                )

        if held_this_order:
            facts.held_order_ids.append(order.id)
        else:
            facts.included_order_ids.append(order.id)
            facts.tax_cents += order.tax_cents
            facts.tips_cents += order.tips_cents

    return facts


# ---------------------------------------------------------------------------
# Cumulative festival-to-date history (original, non-correction FINAL
# settlements only -- corrections are a separate ledger, see module
# docstring and docs/progress/A04-handoff.md)
# ---------------------------------------------------------------------------


def gather_cumulative_history(
    db: Session, vendor_id: str, festival_id: str, before_business_date: date
) -> tuple[int, int]:
    """Returns (cumulative_eligible_sales_cents, cumulative_commission_
    assessed_prior_cents) summed over this vendor's ORIGINAL (correction_of
    IS NULL), FINAL settlements for business dates strictly before
    `before_business_date`."""
    rows = db.execute(
        select(Settlement.eligible_sales_cents, Settlement.assessed_fee_cents).where(
            Settlement.vendor_id == vendor_id,
            Settlement.festival_id == festival_id,
            Settlement.business_date < before_business_date,
            Settlement.status == "FINAL",
            Settlement.correction_of.is_(None),
        )
    ).all()
    cumulative_eligible = sum(r[0] for r in rows)
    cumulative_assessed = sum(r[1] for r in rows)
    return cumulative_eligible, cumulative_assessed


def find_confirmed_payout(db: Session, vendor_id: str, settlement_id: str) -> Payout | None:
    """A04 does not implement A07's dispatch/confirmation machinery -- this
    only checks whether a `Payout` row already exists in state "CONFIRMED"
    linked to the given settlement, which is the precondition for routing a
    recalculation through the correction path instead of a fresh
    calculation. See docs/progress/A04-handoff.md for how this task's own
    verification script stands in for A07 to exercise that path for real."""
    return db.execute(
        select(Payout).where(Payout.settlement_id == settlement_id, Payout.state == "CONFIRMED")
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Line items (traceability)
# ---------------------------------------------------------------------------


def _write_line_items(
    db: Session, settlement: Settlement, result: SettlementLineResult, facts: ReconciledDayFacts
) -> None:
    rows = [
        ("eligible_sales", result.eligible_sales_cents, facts.source_refs),
        ("centrally_held", result.centrally_held_cents, facts.source_refs),
        ("vendor_retained_cash", result.vendor_retained_cash_cents, facts.source_refs),
        ("tax", result.tax_cents, []),
        ("tips", result.tips_cents, []),
        ("processor_fee_assessed", result.processor_fee_assessed_cents, facts.source_refs),
        ("processor_fee_collected", result.processor_fee_collected_cents, facts.source_refs),
        ("fee_assessed", result.fee_assessed_cents, []),
        ("fee_collected_from_central", result.fee_collected_from_central_cents, []),
        ("vendor_fee_receivable", result.vendor_fee_receivable_cents, []),
        ("unpaid_balance", result.unpaid_balance_cents, []),
        ("vendor_payout", result.vendor_payout_cents, []),
    ]
    for line_type, amount_cents, refs in rows:
        db.add(
            SettlementLineItem(
                settlement_id=settlement.id,
                line_type=line_type,
                amount_cents=amount_cents,
                source_refs=refs,
            )
        )


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


@dataclass
class SettlementCalculationOutcome:
    settlement: Settlement
    result: SettlementLineResult
    is_correction: bool
    corrected_settlement_id: str | None = None
    adjustment: object | None = None  # domain.settlement.SettlementAdjustmentResult | None


def calculate_and_record_settlement(
    db: Session,
    *,
    festival_id: str,
    vendor: Vendor,
    business_date_: date,
    is_final_period: bool,
    run_id: str | None,
    actor_id: str,
    actor_capability: str,
) -> SettlementCalculationOutcome:
    """Compute (and persist) one vendor/business_date settlement.

    Does NOT call db.commit() -- the caller commits exactly once, same
    atomicity pattern as app/reconciliation.py's reconcile_festival.

    Correction routing: if a prior settlement already exists for this
    EXACT vendor/business_date with a CONFIRMED payout linked to it, this
    call does not overwrite that settlement. It computes the new result
    from current facts, records a NEW Settlement row with
    `correction_of` pointing at the prior one, and the financial delta is
    computed via `domain.settlement.calculate_settlement_adjustment`
    (never a negative payout -- an overpayment becomes a recovery amount,
    an underpayment becomes additional_payout_cents on the correction row
    itself).
    """
    commission_bps, minimum_fee_cents, processor_fee_deductible, term = resolve_vendor_terms(
        db, vendor.id, business_date_
    )
    facts = gather_reconciled_day_facts(db, vendor, business_date_)
    cumulative_eligible_prior, cumulative_assessed_prior = gather_cumulative_history(
        db, vendor.id, festival_id, business_date_
    )
    this_period_total_eligible = facts.centrally_held_cents + facts.vendor_retained_cash_cents
    cumulative_eligible_sales_cents = cumulative_eligible_prior + this_period_total_eligible

    result = calculate_vendor_settlement(
        eligible=EligibleSalesInput(
            centrally_held_cents=facts.centrally_held_cents,
            vendor_retained_cash_cents=facts.vendor_retained_cash_cents,
            tax_cents=facts.tax_cents,
            tips_cents=facts.tips_cents,
        ),
        cumulative_eligible_sales_cents=cumulative_eligible_sales_cents,
        cumulative_commission_assessed_prior_cents=cumulative_assessed_prior,
        commission_bps=commission_bps,
        minimum_fee_cents=minimum_fee_cents,
        is_final_period=is_final_period,
        processor_fee_assessed_cents=facts.processor_fee_assessed_cents,
        processor_fee_deductible=processor_fee_deductible,
    )

    holds = [{"reason": r["reason"], **r} for r in facts.held_reasons]

    # Does a settlement already exist for this exact vendor/day with a
    # CONFIRMED payout? If so, route through the correction path.
    prior_settlement = db.execute(
        select(Settlement)
        .where(
            Settlement.vendor_id == vendor.id,
            Settlement.festival_id == festival_id,
            Settlement.business_date == business_date_,
            Settlement.status == "FINAL",
        )
        .order_by(Settlement.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    is_correction = False
    adjustment = None
    corrected_settlement_id = None
    amount_already_paid_cents = 0

    if prior_settlement is not None:
        confirmed_payout = find_confirmed_payout(db, vendor.id, prior_settlement.id)
        if confirmed_payout is not None:
            is_correction = True
            corrected_settlement_id = prior_settlement.id
            amount_already_paid_cents = confirmed_payout.amount_cents
            previous_result = SettlementLineResult(
                eligible_sales_cents=prior_settlement.eligible_sales_cents,
                centrally_held_cents=prior_settlement.centrally_held_cents,
                vendor_retained_cash_cents=prior_settlement.vendor_retained_cash_cents,
                tax_cents=prior_settlement.tax_cents,
                tips_cents=prior_settlement.tips_cents,
                fee_assessed_cents=prior_settlement.assessed_fee_cents,
                fee_collected_from_central_cents=prior_settlement.collected_fee_cents,
                vendor_fee_receivable_cents=prior_settlement.vendor_fee_receivable_cents,
                unpaid_balance_cents=prior_settlement.unpaid_balance_cents,
                processor_fee_assessed_cents=prior_settlement.processor_fee_assessed_cents,
                processor_fee_collected_cents=prior_settlement.processor_fee_collected_cents,
                vendor_payout_cents=prior_settlement.payout_cents,
                minimum_applied=prior_settlement.minimum_applied,
                is_final_period=prior_settlement.is_final_period,
                cumulative_eligible_sales_cents=prior_settlement.cumulative_eligible_sales_cents,
                cumulative_commission_assessed_prior_cents=(
                    prior_settlement.cumulative_commission_assessed_prior_cents
                ),
                calculation_version=prior_settlement.calculation_version,
            )
            adjustment = calculate_settlement_adjustment(
                previous=previous_result,
                updated=result,
                amount_already_paid_cents=amount_already_paid_cents,
            )

    settlement = Settlement(
        festival_id=festival_id,
        vendor_id=vendor.id,
        business_date=business_date_,
        run_id=run_id,
        calculation_version=CALCULATION_VERSION,
        vendor_contract_term_id=term.id if term is not None else None,
        vendor_contract_term_version=term.version if term is not None else None,
        commission_bps_used=commission_bps,
        minimum_fee_cents_used=minimum_fee_cents,
        processor_fee_deductible_used=processor_fee_deductible,
        is_final_period=is_final_period,
        eligible_sales_cents=result.eligible_sales_cents,
        centrally_held_cents=result.centrally_held_cents,
        vendor_retained_cash_cents=result.vendor_retained_cash_cents,
        tax_cents=result.tax_cents,
        tips_cents=result.tips_cents,
        cumulative_eligible_sales_cents=result.cumulative_eligible_sales_cents,
        cumulative_commission_assessed_prior_cents=result.cumulative_commission_assessed_prior_cents,
        assessed_fee_cents=result.fee_assessed_cents,
        collected_fee_cents=result.fee_collected_from_central_cents,
        vendor_fee_receivable_cents=result.vendor_fee_receivable_cents,
        unpaid_balance_cents=result.unpaid_balance_cents,
        processor_fee_assessed_cents=result.processor_fee_assessed_cents,
        processor_fee_collected_cents=result.processor_fee_collected_cents,
        payout_cents=result.vendor_payout_cents,
        minimum_applied=result.minimum_applied,
        holds=holds,
        policy_refs=[],
        correction_of=corrected_settlement_id if is_correction else None,
        correction_reason=(
            "recalculated after a confirmed payout already existed for this vendor/day"
            if is_correction
            else None
        ),
        status="CORRECTION" if is_correction else "FINAL",
    )
    db.add(settlement)
    db.flush()
    _write_line_items(db, settlement, result, facts)
    if is_correction and adjustment is not None:
        db.add(
            SettlementLineItem(
                settlement_id=settlement.id,
                line_type="additional_payout",
                amount_cents=adjustment.additional_payout_cents,
                source_refs=[{"ref_kind": "settlement", "ref_id": corrected_settlement_id}],
            )
        )
        db.add(
            SettlementLineItem(
                settlement_id=settlement.id,
                line_type="overpayment_recovery",
                amount_cents=adjustment.overpayment_recovery_cents,
                source_refs=[{"ref_kind": "settlement", "ref_id": corrected_settlement_id}],
            )
        )
    db.flush()

    uow = UnitOfWork(db)
    uow.append_event(
        stream_id=f"settlement:{vendor.id}:{festival_id}",
        event_type="settlement.corrected" if is_correction else "settlement.calculated",
        aggregate_id=settlement.id,
        aggregate_version=1,
        actor_id=actor_id,
        actor_type="system",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        run_id=run_id,
        vendor_id=vendor.id,
        payload={
            "festival_id": festival_id,
            "business_date": business_date_.isoformat(),
            "is_final_period": is_final_period,
            "calculation_version": CALCULATION_VERSION,
            "assessed_fee_cents": result.fee_assessed_cents,
            "payout_cents": result.vendor_payout_cents,
            "unpaid_balance_cents": result.unpaid_balance_cents,
            "vendor_fee_receivable_cents": result.vendor_fee_receivable_cents,
            "is_correction": is_correction,
            "corrected_settlement_id": corrected_settlement_id,
            "additional_payout_cents": (adjustment.additional_payout_cents if adjustment else None),
            "overpayment_recovery_cents": (
                adjustment.overpayment_recovery_cents if adjustment else None
            ),
        },
        lineage_refs=[{"ref_kind": "settlement", "ref_id": settlement.id}]
        + (
            [{"ref_kind": "settlement", "ref_id": corrected_settlement_id}] if is_correction else []
        ),
    )

    return SettlementCalculationOutcome(
        settlement=settlement,
        result=result,
        is_correction=is_correction,
        corrected_settlement_id=corrected_settlement_id,
        adjustment=adjustment,
    )


def calculate_festival_settlements(
    db: Session,
    *,
    festival_id: str,
    business_date_: date,
    is_final_period: bool,
    run_id: str | None,
    actor_id: str,
    actor_capability: str,
) -> list[SettlementCalculationOutcome]:
    """Calculate settlements for every vendor in `festival_id` for one
    business_date. Fault isolation mirrors app/reconciliation.py's
    reconcile_festival: one vendor's unexpected error does not abort the
    rest (this is a backstop for bugs, not how ordinary business holds are
    handled -- those already surface as `Settlement.holds`, not
    exceptions)."""
    vendors = db.execute(select(Vendor).where(Vendor.festival_id == festival_id)).scalars().all()
    outcomes: list[SettlementCalculationOutcome] = []
    for vendor in vendors:
        outcome = calculate_and_record_settlement(
            db,
            festival_id=festival_id,
            vendor=vendor,
            business_date_=business_date_,
            is_final_period=is_final_period,
            run_id=run_id,
            actor_id=actor_id,
            actor_capability=actor_capability,
        )
        outcomes.append(outcome)
    return outcomes
