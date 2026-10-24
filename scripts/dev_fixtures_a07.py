#!/usr/bin/env python3
"""THROWAWAY, HAND-CHECKED dev fixtures for A07 controlled-payouts
verification.

*** THIS IS NOT B01. *** B01 ("compact synthetic fixture and independent
oracle") does not exist in this repository -- see docs/progress/A02 through
A06-handoff.md. Per the user's explicit decision, carried forward unchanged
into this task: this script builds a small, clearly-labeled, hand-checkable
substitute. The same person who wrote app/payout_dispatch.py also wrote the
scenarios here -- a development/demo aid, not evidence of independent
verification.

Scope note, deliberately different from dev_fixtures_a03/a04/a05.py: A07's
job is release CONTROL (limits, holds, idempotency, the simulated rail), not
settlement math (already verified in A04) or policy matching (already
verified in A05). Rather than re-running a full ingestion -> reconciliation
-> settlement pipeline just to get a `Settlement` row to point a `Payout`
at, this script inserts `Settlement` rows DIRECTLY with hand-chosen
`payout_cents` figures -- clearly labeled below, never claiming to
re-verify A04's commission arithmetic (fixture 1-6 of dev_fixtures_a04.py
already do that). The one exception is the "distinct obligation per
correction" scenario, where TWO settlement rows (original + correction,
linked by `correction_of`) are constructed directly to mirror the shape
app/settlement.py's real correction path produces, without re-running that
path.

One further deliberate simplification: the "$5,000/day cumulative limit"
concurrency scenario needs TWO INDEPENDENT payout obligations for the SAME
vendor/business_date. A04's real settlement engine only ever produces ONE
FINAL settlement per vendor/business_date (a second calculation for the
same day either is a no-op-shaped repeat or routes through the correction
path -- see docs/progress/A04-handoff.md). This script constructs two
separate, hand-labeled Settlement rows for the same vendor/day purely as a
vehicle to get two independently-releasable Payout obligations into the
same VendorDayPayoutAggregate row -- it is NOT claiming A04 would ever
produce two FINAL settlements for one vendor/day itself. A07's own job
(the aggregate/limit mechanism) is what is under test here, not A04's
one-settlement-per-day invariant.

Usage (from repo root, real Postgres required, no SQLite):
    DATABASE_URL=... uv run python scripts/dev_fixtures_a07.py

Safe to re-run: every entity is looked up by a natural key before being
created, same convention as dev_fixtures_a03/a04/a05.
"""

from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app import models_payout_dispatch  # noqa: E402,F401 -- registers tables on Base
from app.db import get_session_factory, make_engine  # noqa: E402
from app.models_ledger import Case, Vendor  # noqa: E402
from app.models_settlement import Settlement  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

FESTIVAL_RACE = "gcmf-2026-a07dev-race"
FESTIVAL_BLOCKER = "gcmf-2026-a07dev-blocker"
FESTIVAL_CASH = "gcmf-2026-a07dev-cash"
FESTIVAL_MAIN = "gcmf-2026-a07dev-main"

D1 = date(2026, 10, 16)


def get_or_create_vendor(db: Session, *, festival_id: str, name: str) -> Vendor:
    existing = db.execute(
        select(Vendor).where(Vendor.festival_id == festival_id, Vendor.name == name)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    vendor = Vendor(festival_id=festival_id, name=name)
    db.add(vendor)
    db.flush()
    return vendor


def make_settlement(
    db: Session,
    *,
    vendor: Vendor,
    business_date_: date,
    payout_cents: int,
    label: str,
    correction_of: str | None = None,
) -> Settlement:
    """Hand-constructed Settlement row -- see module docstring. `label` is
    folded into `calculation_version` purely so repeated runs of this
    script can find "the same" settlement by a readable natural key
    without re-deriving one from scratch (safe to re-run)."""
    calculation_version = f"a07-devfixture:{label}"
    existing = db.execute(
        select(Settlement).where(
            Settlement.vendor_id == vendor.id,
            Settlement.business_date == business_date_,
            Settlement.calculation_version == calculation_version,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    settlement = Settlement(
        festival_id=vendor.festival_id,
        vendor_id=vendor.id,
        business_date=business_date_,
        calculation_version=calculation_version,
        commission_bps_used=1500,
        minimum_fee_cents_used=50_000,
        processor_fee_deductible_used=False,
        is_final_period=True,
        eligible_sales_cents=payout_cents,
        centrally_held_cents=payout_cents,
        vendor_retained_cash_cents=0,
        tax_cents=0,
        tips_cents=0,
        cumulative_eligible_sales_cents=payout_cents,
        cumulative_commission_assessed_prior_cents=0,
        assessed_fee_cents=0,
        collected_fee_cents=0,
        vendor_fee_receivable_cents=0,
        unpaid_balance_cents=0,
        processor_fee_assessed_cents=0,
        processor_fee_collected_cents=0,
        payout_cents=payout_cents,
        minimum_applied=False,
        holds=[],
        policy_refs=[],
        correction_of=correction_of,
        status="CORRECTION" if correction_of else "FINAL",
    )
    db.add(settlement)
    db.flush()
    return settlement


def get_or_create_open_case(db: Session, *, vendor: Vendor, reason_code: str) -> Case:
    existing = db.execute(
        select(Case).where(
            Case.vendor_id == vendor.id, Case.reason_code == reason_code, Case.status == "OPEN"
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    case = Case(
        vendor_id=vendor.id,
        reason_code=reason_code,
        evidence_refs=[],
        proposed_resolution=None,
        required_capability="resolve_case",
        status="OPEN",
    )
    db.add(case)
    db.flush()
    return case


def seed(db: Session) -> dict:
    ids: dict[str, str] = {}

    # === Scenario A: concurrent $5,000/day cumulative limit race =========
    # Two INDEPENDENT $3,000 obligations for the SAME vendor/day. Together
    # they total $6,000 > the $5,000 automatic limit. Neither has a
    # finance-lead approval. Fired as two REAL concurrent release requests
    # by scripts/verify_a07_payouts.py -- exactly one must succeed.
    race_vendor = get_or_create_vendor(db, festival_id=FESTIVAL_RACE, name="A07 Race Vendor")
    race_settlement_1 = make_settlement(
        db, vendor=race_vendor, business_date_=D1, payout_cents=300_000, label="race-1"
    )
    race_settlement_2 = make_settlement(
        db, vendor=race_vendor, business_date_=D1, payout_cents=300_000, label="race-2"
    )
    ids["race_vendor_id"] = race_vendor.id
    ids["race_settlement_1_id"] = race_settlement_1.id
    ids["race_settlement_2_id"] = race_settlement_2.id

    # === Scenario B: hard blocker (open case) overrides an amount that HAS
    # finance-lead approval ================================================
    blocker_vendor = get_or_create_vendor(
        db, festival_id=FESTIVAL_BLOCKER, name="A07 Blocker Vendor"
    )
    blocker_settlement = make_settlement(
        db,
        vendor=blocker_vendor,
        business_date_=D1,
        payout_cents=600_000,  # $6,000 -- over the $5,000 limit on its own
        label="blocker",
    )
    ids["blocker_vendor_id"] = blocker_vendor.id
    ids["blocker_settlement_id"] = blocker_settlement.id
    # The open case itself is created by the verify script AFTER approval,
    # so the sequencing ("approved first, case opened after, release still
    # blocked") is explicit and visible there, not hidden in seeding.

    # === Scenario C: insufficient available cash, atomically checked ======
    cash_vendor = get_or_create_vendor(db, festival_id=FESTIVAL_CASH, name="A07 Cash Vendor")
    cash_settlement = make_settlement(
        db,
        vendor=cash_vendor,
        business_date_=D1,
        payout_cents=100_000,
        label="cash",  # $1,000
    )
    ids["cash_vendor_id"] = cash_vendor.id
    ids["cash_settlement_id"] = cash_settlement.id

    # === Scenario D: worker-crash-mid-dispatch resume ======================
    crash_vendor = get_or_create_vendor(db, festival_id=FESTIVAL_MAIN, name="A07 Crash Vendor")
    crash_settlement = make_settlement(
        db, vendor=crash_vendor, business_date_=D1, payout_cents=100_000, label="crash"
    )
    ids["crash_vendor_id"] = crash_vendor.id
    ids["crash_settlement_id"] = crash_settlement.id

    # === Scenario E: cancel-race (deterministic + concurrent trials) ======
    cancel_vendor_lost = get_or_create_vendor(
        db, festival_id=FESTIVAL_MAIN, name="A07 Cancel Vendor Lost"
    )
    cancel_settlement_lost = make_settlement(
        db, vendor=cancel_vendor_lost, business_date_=D1, payout_cents=50_000, label="cancel-lost"
    )
    ids["cancel_vendor_lost_id"] = cancel_vendor_lost.id
    ids["cancel_settlement_lost_id"] = cancel_settlement_lost.id

    cancel_vendor_won = get_or_create_vendor(
        db, festival_id=FESTIVAL_MAIN, name="A07 Cancel Vendor Won"
    )
    cancel_settlement_won = make_settlement(
        db, vendor=cancel_vendor_won, business_date_=D1, payout_cents=50_000, label="cancel-won"
    )
    ids["cancel_vendor_won_id"] = cancel_vendor_won.id
    ids["cancel_settlement_won_id"] = cancel_settlement_won.id

    # Concurrent-race trial vendors/settlements are created fresh, per
    # trial, by the verify script itself (it needs N independent payouts),
    # not pre-seeded here.

    # === Scenario F: UNKNOWN outcome stays unknown until reconciled =======
    unknown_vendor = get_or_create_vendor(db, festival_id=FESTIVAL_MAIN, name="A07 Unknown Vendor")
    unknown_settlement = make_settlement(
        db, vendor=unknown_vendor, business_date_=D1, payout_cents=100_000, label="unknown"
    )
    ids["unknown_vendor_id"] = unknown_vendor.id
    ids["unknown_settlement_id"] = unknown_settlement.id

    # === Scenario G: beneficiary/vendor-terms version changed since
    # approval blocks release ==============================================
    beneficiary_vendor = get_or_create_vendor(
        db, festival_id=FESTIVAL_MAIN, name="A07 Beneficiary Vendor"
    )
    beneficiary_settlement = make_settlement(
        db, vendor=beneficiary_vendor, business_date_=D1, payout_cents=100_000, label="beneficiary"
    )
    ids["beneficiary_vendor_id"] = beneficiary_vendor.id
    ids["beneficiary_settlement_id"] = beneficiary_settlement.id

    # === Scenario H: optimistic version conflict ===========================
    version_vendor = get_or_create_vendor(db, festival_id=FESTIVAL_MAIN, name="A07 Version Vendor")
    version_settlement = make_settlement(
        db, vendor=version_vendor, business_date_=D1, payout_cents=100_000, label="version"
    )
    ids["version_vendor_id"] = version_vendor.id
    ids["version_settlement_id"] = version_settlement.id

    # === Scenario J: a correction settlement gets its OWN distinct
    # obligation/key; the superseded original cannot be released =========
    correction_vendor = get_or_create_vendor(
        db, festival_id=FESTIVAL_MAIN, name="A07 Correction Vendor"
    )
    correction_original = make_settlement(
        db,
        vendor=correction_vendor,
        business_date_=D1,
        payout_cents=200_000,
        label="correction-original",
    )
    ids["correction_vendor_id"] = correction_vendor.id
    ids["correction_original_settlement_id"] = correction_original.id
    # The correction settlement row itself is created by the verify script
    # AFTER the original's payout has already been created/approved, so the
    # sequencing is explicit there (not hidden in seeding) -- see
    # scripts/verify_a07_payouts.py's correction scenario.

    db.commit()
    return ids


def main() -> int:
    make_engine()
    session_factory = get_session_factory()
    db = session_factory()
    try:
        ids = seed(db)
        print(f"dev_fixtures_a07: seeded/confirmed {len(ids)} ids")
        for k, v in ids.items():
            print(f"  {k} = {v}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
