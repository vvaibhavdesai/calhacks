"""A09 corrections: pure recalculation, classification and hashing helpers.

Same convention as domain.settlement (A04), domain.policy (A05),
domain.payout_dispatch (A07) and domain.accounting (A08): plain values in,
plain values out. No database session, no HTTP, no clock. The DB-touching
orchestration (locking, reading the affected records, writing linked
correction rows) lives in services/api/app/corrections.py.

What lives here:
  - `recompute_vendor_chain`: re-run A04's own `calculate_vendor_settlement`
    over a vendor's settlement history with a corrected commission rate
    from a given business date. Earlier nights keep their assessed fee;
    later nights are recomputed cumulatively against the corrected prior
    assessments, so the festival-to-date ladder stays consistent.
  - `classify_payout`: map a payout's current state to the correction
    treatment AGENT_HANDOFF.md "Challenges and reversal" requires (draft:
    revise; pending: request cancellation and reconcile; completed:
    offset/recovery; nothing left to offset: human recovery or waiver).
  - `paid_adjustment`: what a corrected entitlement means for an amount
    that already moved (additional payout or recovery, never negative).
  - `offset_cents`: how much of a recovery an approved offset can take
    from one draft payout.
  - `canonical_hash`: the stable hash approvals bind to.
  - journal line builders for a recovery receipt and a recovery waiver.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from domain import accounting as acct
from domain.settlement import EligibleSalesInput, SettlementLineResult, calculate_vendor_settlement

# ---------------------------------------------------------------------------
# Settlement chain recalculation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PeriodInput:
    """The frozen inputs of one live settlement version (A04 pins all of
    them on the immutable `settlements` row)."""

    settlement_id: str
    business_date: date
    centrally_held_cents: int
    vendor_retained_cash_cents: int
    tax_cents: int
    tips_cents: int
    processor_fee_assessed_cents: int
    processor_fee_deductible: bool
    is_final_period: bool
    minimum_fee_cents: int
    commission_bps: int
    assessed_fee_cents: int
    payout_cents: int


@dataclass(frozen=True)
class PeriodResult:
    period: PeriodInput
    changed: bool
    commission_bps: int
    result: SettlementLineResult

    @property
    def fee_delta_cents(self) -> int:
        return self.result.fee_assessed_cents - self.period.assessed_fee_cents

    @property
    def payout_delta_cents(self) -> int:
        return self.result.vendor_payout_cents - self.period.payout_cents


def recompute_vendor_chain(
    periods: list[PeriodInput], *, new_commission_bps: int, effective_from: date
) -> list[PeriodResult]:
    """`periods` must be every live settlement of one vendor/festival in
    business-date order. Periods before `effective_from` are kept as they
    were assessed (their fee still counts toward the cumulative prior);
    periods on/after it are recomputed with `new_commission_bps`."""
    if new_commission_bps < 0:
        raise ValueError("commission_bps cannot be negative")
    ordered = sorted(periods, key=lambda p: (p.business_date, p.settlement_id))
    out: list[PeriodResult] = []
    cumulative_eligible = 0
    cumulative_assessed_prior = 0
    for p in ordered:
        eligible = EligibleSalesInput(
            centrally_held_cents=p.centrally_held_cents,
            vendor_retained_cash_cents=p.vendor_retained_cash_cents,
            tax_cents=p.tax_cents,
            tips_cents=p.tips_cents,
        )
        cumulative_eligible += eligible.total_eligible_cents
        affected = p.business_date >= effective_from
        bps = new_commission_bps if affected else p.commission_bps
        result = calculate_vendor_settlement(
            eligible=eligible,
            cumulative_eligible_sales_cents=cumulative_eligible,
            cumulative_commission_assessed_prior_cents=cumulative_assessed_prior,
            commission_bps=bps,
            minimum_fee_cents=p.minimum_fee_cents,
            is_final_period=p.is_final_period,
            processor_fee_assessed_cents=p.processor_fee_assessed_cents,
            processor_fee_deductible=p.processor_fee_deductible,
        )
        if not affected:
            # Unaffected history counts exactly as it was assessed, even if
            # a recomputation would round differently.
            cumulative_assessed_prior += p.assessed_fee_cents
            out.append(PeriodResult(period=p, changed=False, commission_bps=bps, result=result))
            continue
        cumulative_assessed_prior += result.fee_assessed_cents
        changed = (
            result.fee_assessed_cents != p.assessed_fee_cents
            or result.vendor_payout_cents != p.payout_cents
            or bps != p.commission_bps
        )
        out.append(PeriodResult(period=p, changed=changed, commission_bps=bps, result=result))
    return out


# ---------------------------------------------------------------------------
# Payout classification (AGENT_HANDOFF.md "Challenges and reversal")
# ---------------------------------------------------------------------------

DRAFT_STATES = ("DRAFT", "APPROVED")
NEVER_PAID_STATES = ("FAILED", "CANCELLED", "REJECTED")
PENDING_STATES = ("DISPATCH_PENDING", "UNKNOWN", "CANCEL_REQUESTED")
COMPLETED_STATES = ("SUBMITTED", "CONFIRMED")

# Money that has moved, or that may have moved and we have not been told
# otherwise. This is the set to subtract before creating any further payout
# obligation for the same entitlement.
#
# It is PENDING + COMPLETED rather than COMPLETED alone, and the distinction
# is not academic: a payout in UNKNOWN means the rail never answered, so the
# transfer may well have happened. Counting it as unpaid and issuing a fresh
# obligation is how a vendor gets paid twice. DISPATCH_PENDING and
# CANCEL_REQUESTED are the same argument -- the amount is already known and
# the money is already in motion.
#
# DRAFT_STATES are excluded because nothing has been sent, and
# NEVER_PAID_STATES because the rail told us the money did not move. Together
# with those two, this covers every state a payout can hold.
COMMITTED_STATES = PENDING_STATES + COMPLETED_STATES


def classify_payout(state: str | None) -> tuple[str, str]:
    """Returns (state_group, treatment) using lane B's draft vocabulary
    (openapi.draft.yaml CorrectionStateGroup / CorrectionTreatment)."""
    if state is None or state in DRAFT_STATES or state in NEVER_PAID_STATES:
        return "DRAFT", "DRAFT_REVISE"
    if state in PENDING_STATES:
        return "PENDING", "PENDING_CANCEL_REQUEST"
    if state in COMPLETED_STATES:
        return "COMPLETED", "COMPLETED_RECOVERY_OFFSET"
    raise ValueError(f"unknown payout state {state!r}")


def paid_adjustment(*, paid_cents: int, corrected_payout_cents: int) -> tuple[int, int]:
    """(additional_payout_cents, recovery_cents); at most one is non-zero
    and neither is negative. Money that moved is never 'unsent'."""
    if paid_cents < 0 or corrected_payout_cents < 0:
        raise ValueError("amounts cannot be negative")
    if paid_cents > corrected_payout_cents:
        return 0, paid_cents - corrected_payout_cents
    return corrected_payout_cents - paid_cents, 0


def offset_cents(*, remaining_cents: int, payout_amount_cents: int) -> int:
    """An approved offset takes at most what the draft payout carries; the
    rest stays an open recovery request. Never makes a payout negative."""
    if remaining_cents < 0 or payout_amount_cents < 0:
        raise ValueError("amounts cannot be negative")
    return min(remaining_cents, payout_amount_cents)


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def canonical_hash(data: Any) -> str:
    return hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()


def normalize_content_hash(value: str) -> str:
    """Lane B's draft writes `sha256:<hex>`; A02 stores bare hex."""
    value = (value or "").strip().lower()
    return value[len("sha256:") :] if value.startswith("sha256:") else value


# ---------------------------------------------------------------------------
# Recovery journal lines (DRAFT posting map, same accounts as A08)
# ---------------------------------------------------------------------------


def recovery_receipt(amount_cents: int) -> list[acct.Line]:
    """The vendor pays back an overpayment: cash in, vendor payable's debit
    balance (what the vendor owed) cleared."""
    return acct.build([("1000", amount_cents, 0), ("2000", 0, amount_cents)])


def recovery_waiver(amount_cents: int) -> list[acct.Line]:
    """A finance lead waives an overpayment: written off to operating
    expense. DRAFT choice of account, not an approved accounting policy."""
    return acct.build([("5400", amount_cents, 0), ("2000", 0, amount_cents)])
