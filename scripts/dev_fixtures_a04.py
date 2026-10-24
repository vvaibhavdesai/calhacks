#!/usr/bin/env python3
"""THROWAWAY, HAND-CHECKED dev fixtures for A04 settlement verification.

*** THIS IS NOT B01. *** B01 ("compact synthetic fixture and independent
oracle") does not exist in this repository -- see docs/progress/A02-handoff.md,
docs/progress/A03-handoff.md and docs/progress/A04-handoff.md. Per the user's
explicit decision for this task, this script builds a small, clearly-labeled,
hand-checkable substitute: it directly constructs the six CONTRACTS.md C02
"Required hand-worked fixtures" scenarios against the real database schema
(vendors/orders/payments/bank_entries, and A03's match_groups/match_group_members
as the "already reconciled" input A04's settlement.py reads), with every
expected number worked out by hand in the comment next to each scenario and
re-checked mechanically by scripts/verify_a04_settlement.py. The same person
who wrote app/settlement.py and packages/domain/domain/settlement.py also
wrote the expected answers here -- this is a development/demo aid, not
evidence of independent verification. If/when a real B01 lands, these
fixtures should be retired in favor of it.

Why match_groups are constructed directly instead of running a full A03
reconciliation pass first: A04's settlement engine is documented (see
app/settlement.py's module docstring) to read ONLY A03's already-reconciled
match_groups/match_group_members -- never raw orders/payments. Hand-
constructing "matched" (and, where a scenario calls for it, "pending"/"case")
match_groups directly is the most direct way to control exactly what A04
sees as "already reconciled" for each of the six fixtures, without needing
to also reverse-engineer A03's classification thresholds to produce a
specific match_group outcome. A03's own classification logic is already
exercised by scripts/verify_a03_reconciliation.py; this script is scoped to
A04's math only.

Usage (from repo root, real Postgres required, no SQLite):
    DATABASE_URL=... uv run python scripts/dev_fixtures_a04.py

Safe to re-run: every entity is looked up by a natural key before being
created (vendors by name, orders/payments by source_native_id, bank entries
by provider_reference, match groups/members by a deterministic lookup on
their parent row) so running this script twice does not create duplicate
rows. Fixture 5 (late refund after confirmed payout) is the one scenario
with real *sequencing* -- see `apply_fixture5_late_refund()` below, which
scripts/verify_a04_settlement.py calls explicitly between its two
/settlements/calculate calls for that vendor, not from `seed()` itself.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app.db import get_session_factory, make_engine  # noqa: E402
from app.ingestion import import_source  # noqa: E402
from app.models_ledger import Order, Payment, Vendor, VendorContractTerm  # noqa: E402
from app.models_reconciliation import BankEntry, MatchGroup, MatchGroupMember  # noqa: E402
from app.storage.local_fs import get_document_storage  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

SOURCE_SYSTEM = "a04_devfixtures"
_MANIFEST_NATIVE_ID = "a04-dev-fixture-manifest-v1"

DAY1 = date(2026, 10, 16)
DAY2 = date(2026, 10, 17)
DAY3 = date(2026, 10, 18)


def festival_id_for(n: int) -> str:
    """Each of the six fixtures gets its OWN festival_id, deliberately --
    not because C02's scenarios are described as separate festivals, but
    because `POST /settlements/calculate` takes one `is_final_period` flag
    applied to every vendor in a festival_id for one call (see
    app/settlement.py's `calculate_festival_settlements`). Fixture 2 needs
    `is_final_period=False` on day 1/2 and `True` on day 3 for the SAME
    festival_id/business_date combination that fixture 1/3/4/5 need `True`
    on day 1 and fixture 6 needs `False` on day 1 -- sharing one festival_id
    across fixtures would make one fixture's required flag clobber
    another's. Scoping each fixture to its own festival_id keeps every
    /settlements/calculate call unambiguous without inventing a per-vendor
    override the real API does not have."""
    return f"gcmf-2026-a04dev-f{n}"


def _ingest_manifest(db: Session) -> tuple[str, int, str]:
    storage = get_document_storage()
    content = (
        b'{"name": "a04-dev-fixture-manifest", "version": 1, '
        b'"note": "throwaway hand-checked A04 dev fixtures, not B01"}'
    )
    result = import_source(
        db,
        storage,
        source_system=SOURCE_SYSTEM,
        source_native_id=_MANIFEST_NATIVE_ID,
        content_bytes=content,
        mime_type="application/json",
        actor_id="dev-fixture-script",
        actor_capability="import_source",
    )
    db.commit()
    return result.source_document_id, result.version, result.content_hash


def _source_ref(manifest: tuple[str, int, str], locator: str) -> dict:
    doc_id, version, content_hash = manifest
    return {
        "source_document_id": doc_id,
        "source_version": version,
        "content_hash": content_hash,
        "locator_kind": "row",
        "locator": locator,
    }


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


def get_or_create_contract_term(
    db: Session,
    *,
    vendor: Vendor,
    effective_from: date,
    processor_fee_deductible: bool,
    commission_bps: int | None = None,
    minimum_fee_cents: int | None = None,
) -> VendorContractTerm:
    existing = db.execute(
        select(VendorContractTerm).where(
            VendorContractTerm.vendor_id == vendor.id,
            VendorContractTerm.effective_from == effective_from,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    term = VendorContractTerm(
        vendor_id=vendor.id,
        version=1,
        commission_bps=commission_bps,
        minimum_fee_cents=minimum_fee_cents,
        processor_fee_deductible=processor_fee_deductible,
        effective_from=effective_from,
        effective_to=None,
        status="active",
        source_refs=[],
    )
    db.add(term)
    db.flush()
    return term


def get_or_create_order(
    db: Session,
    *,
    vendor: Vendor,
    native_id: str,
    business_date_: date,
    gross_cents: int,
    tax_cents: int,
    tips_cents: int,
    manifest: tuple[str, int, str],
) -> Order:
    existing = db.execute(
        select(Order).where(
            Order.source_system == SOURCE_SYSTEM, Order.source_native_id == native_id
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    ref = _source_ref(manifest, f"order:{native_id}")
    order = Order(
        vendor_id=vendor.id,
        source_system=SOURCE_SYSTEM,
        source_native_id=native_id,
        business_date=business_date_,
        ordered_at=datetime(
            business_date_.year, business_date_.month, business_date_.day, 20, 0, tzinfo=UTC
        ),
        gross_cents=gross_cents,
        tax_cents=tax_cents,
        tips_cents=tips_cents,
        **ref,
    )
    db.add(order)
    db.flush()
    return order


def get_or_create_payment(
    db: Session,
    *,
    vendor: Vendor,
    order: Order,
    native_id: str,
    custody: str,
    amount_cents: int,
    manifest: tuple[str, int, str],
    bank_entry_id: str | None = None,
) -> Payment:
    existing = db.execute(
        select(Payment).where(
            Payment.source_system == SOURCE_SYSTEM, Payment.source_native_id == native_id
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    ref = _source_ref(manifest, f"payment:{native_id}")
    payment = Payment(
        vendor_id=vendor.id,
        order_id=order.id,
        source_system=SOURCE_SYSTEM,
        source_native_id=native_id,
        method=("cash" if custody == "vendor_retained" else "card"),
        custody=custody,
        amount_cents=amount_cents,
        kind="capture",
        bank_entry_id=bank_entry_id,
        source_document_id=ref["source_document_id"],
        source_version=ref["source_version"],
        content_hash=ref["content_hash"],
        locator_kind=ref["locator_kind"],
        locator=ref["locator"],
    )
    db.add(payment)
    db.flush()
    return payment


def get_or_create_bank_entry(
    db: Session,
    *,
    festival_id: str,
    provider_reference: str,
    amount_cents: int,
    batch_fee_cents: int | None,
    business_date_: date,
    manifest: tuple[str, int, str],
) -> BankEntry:
    existing = db.execute(
        select(BankEntry).where(
            BankEntry.festival_id == festival_id,
            BankEntry.provider_reference == provider_reference,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    ref = _source_ref(manifest, f"bank_entry:{provider_reference}")
    entry = BankEntry(
        festival_id=festival_id,
        account_id="operating-clearing-001",
        provider_reference=provider_reference,
        direction="credit",
        amount_cents=amount_cents,
        batch_fee_cents=batch_fee_cents,
        settlement_status="settled",
        business_date=business_date_,
        occurred_at=datetime(
            business_date_.year, business_date_.month, business_date_.day, 23, 0, tzinfo=UTC
        ),
        settled_at=datetime(
            business_date_.year, business_date_.month, business_date_.day, 23, 0, tzinfo=UTC
        ),
        source_document_id=ref["source_document_id"],
        source_version=ref["source_version"],
        content_hash=ref["content_hash"],
        locator_kind=ref["locator_kind"],
        locator=ref["locator"],
    )
    db.add(entry)
    db.flush()

    # Matching "matched" bank_deposit MatchGroup, festival-level (vendor_id
    # null -- per A03's convention: a grouped net bank settlement is not
    # attributable to one vendor by construction). This is the mechanism
    # app/settlement.py's gather_reconciled_day_facts checks before treating
    # any centrally-collected payment on this bank entry as available cash.
    existing_group = db.execute(
        select(MatchGroup)
        .join(MatchGroupMember, MatchGroupMember.match_group_id == MatchGroup.id)
        .where(
            MatchGroup.match_type == "bank_deposit",
            MatchGroupMember.member_kind == "bank_entry",
            MatchGroupMember.member_id == entry.id,
        )
    ).scalar_one_or_none()
    if existing_group is None:
        group = MatchGroup(
            festival_id=festival_id,
            vendor_id=None,
            match_type="bank_deposit",
            business_date=business_date_,
            matched_amount_cents=amount_cents,
            variance_cents=0,
            reason_code=(
                "processor_fee_explained" if batch_fee_cents else "deposit_settled_no_fee"
            ),
            status="matched",
            source_refs=[_source_ref(manifest, f"bank_entry:{provider_reference}")],
        )
        db.add(group)
        db.flush()
        db.add(
            MatchGroupMember(
                match_group_id=group.id,
                member_kind="bank_entry",
                member_id=entry.id,
                role="deposit",
                amount_cents=amount_cents,
            )
        )
        db.flush()
    return entry


def create_matched_order_group(
    db: Session,
    *,
    festival_id: str,
    order: Order,
    payment: Payment,
    matched_amount_cents: int,
    manifest: tuple[str, int, str],
    reason_code: str = "order_payment_match",
) -> MatchGroup:
    """A "matched" order_capture MatchGroup for one order + its one capture
    payment. `matched_amount_cents` is the net captured amount AFTER any
    refund (per A03's classify_order_capture semantics) -- for fixtures with
    no refund this equals the capture amount; for fixture 5's second pass it
    is deliberately lower than the original capture, simulating a late
    refund A03 has already netted in."""
    existing = db.execute(
        select(MatchGroup)
        .join(MatchGroupMember, MatchGroupMember.match_group_id == MatchGroup.id)
        .where(
            MatchGroup.match_type == "order_capture",
            MatchGroupMember.member_kind == "order",
            MatchGroupMember.member_id == order.id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    group = MatchGroup(
        festival_id=festival_id,
        vendor_id=order.vendor_id,
        match_type="order_capture",
        business_date=order.business_date,
        matched_amount_cents=matched_amount_cents,
        variance_cents=0,
        reason_code=reason_code,
        status="matched",
        source_refs=[_source_ref(manifest, f"order:{order.source_native_id}")],
    )
    db.add(group)
    db.flush()
    db.add(
        MatchGroupMember(
            match_group_id=group.id,
            member_kind="order",
            member_id=order.id,
            role="order",
            amount_cents=order.gross_cents,
        )
    )
    db.add(
        MatchGroupMember(
            match_group_id=group.id,
            member_kind="payment",
            member_id=payment.id,
            role="capture",
            amount_cents=payment.amount_cents,
        )
    )
    db.flush()
    return group


def seed(db: Session) -> dict:
    """Create (or reuse, if already present) all six C02 fixture scenarios.
    Returns a dict of vendor_id/settlement-relevant ids keyed by scenario
    name, for scripts/verify_a04_settlement.py to drive and assert against."""
    manifest = _ingest_manifest(db)
    ids: dict[str, str] = {}

    # ===================================================================
    # Fixture 1: $10,000 eligible sales (single day, final period) ->
    # $1,500 commission (15%), $8,500 entitlement. Tax $800 / tips $400
    # carried through, untouched by the fee math. All amounts in CENTS
    # (money columns are integer cents per C01 -- $10,000.00 = 1,000,000).
    #   gross capture = 1,120,000 cents ($11,200); tax=80,000 ($800);
    #     tips=40,000 ($400)
    #   eligible_for_order = 1,120,000 - 80,000 - 40,000 = 1,000,000  <-- $10,000 target
    #   commission = 15% * 1,000,000 = 150,000; max(150000, 50000) = 150000 (pct wins)
    #   fee_collected_from_central = min(150000, 1000000) = 150000
    #   payout = 1,000,000 - 150,000 = 850,000  ($8,500)
    # ===================================================================
    fid1 = festival_id_for(1)
    v1 = get_or_create_vendor(db, festival_id=fid1, name="Fixture1 TenK Vendor")
    o1 = get_or_create_order(
        db,
        vendor=v1,
        native_id="f1-order-1",
        business_date_=DAY1,
        gross_cents=1_120_000,
        tax_cents=80_000,
        tips_cents=40_000,
        manifest=manifest,
    )
    be1 = get_or_create_bank_entry(
        db,
        festival_id=fid1,
        provider_reference="f1-bank-batch-1",
        amount_cents=1_120_000,
        batch_fee_cents=None,
        business_date_=DAY1,
        manifest=manifest,
    )
    p1 = get_or_create_payment(
        db,
        vendor=v1,
        order=o1,
        native_id="f1-payment-1",
        custody="centrally_collected",
        amount_cents=1_120_000,
        manifest=manifest,
        bank_entry_id=be1.id,
    )
    create_matched_order_group(
        db,
        festival_id=fid1,
        order=o1,
        payment=p1,
        matched_amount_cents=1_120_000,
        manifest=manifest,
    )
    ids["fixture1_festival_id"] = fid1
    ids["fixture1_vendor_id"] = v1.id
    ids["fixture1_business_date"] = DAY1.isoformat()

    # ===================================================================
    # Fixture 2: $2,000 CUMULATIVE eligible sales across three nights ->
    # $500 festival minimum ONCE, $1,500 entitlement -- never $500/night.
    # All amounts in CENTS.
    #   Night1 (not final): eligible=80,000 ($800). cum=80,000. pct_to_date=12,000.
    #     increment = 12,000 - 0 = 12,000.  payout = 80,000 - 12,000 = 68,000 ($680).
    #   Night2 (not final): eligible=70,000 ($700). cum=150,000. pct_to_date=22,500.
    #     increment = 22,500 - 12,000 = 10,500.  payout = 70,000 - 10,500 = 59,500 ($595).
    #   Night3 (FINAL): eligible=50,000 ($500). cum=200,000 ($2,000). pct_to_date=30,000.
    #     total_due = max(30000, 50000) = 50000 (minimum wins, applied ONCE).
    #     increment = 50,000 - (12,000+10,500) = 27,500.  payout = 50,000-27,500 = 22,500 ($225).
    #   Sum of fee increments = 12,000+10,500+27,500 = 50,000 ($500, the minimum, ONCE)
    #   Sum of payouts        = 68,000+59,500+22,500 = 150,000 ($1,500 == $2,000 - $500)
    # ===================================================================
    fid2 = festival_id_for(2)
    v2 = get_or_create_vendor(db, festival_id=fid2, name="Fixture2 Minimum Vendor")
    for day, native_suffix, gross in (
        (DAY1, "n1", 80_000),
        (DAY2, "n2", 70_000),
        (DAY3, "n3", 50_000),
    ):
        o = get_or_create_order(
            db,
            vendor=v2,
            native_id=f"f2-order-{native_suffix}",
            business_date_=day,
            gross_cents=gross,
            tax_cents=0,
            tips_cents=0,
            manifest=manifest,
        )
        be = get_or_create_bank_entry(
            db,
            festival_id=fid2,
            provider_reference=f"f2-bank-batch-{native_suffix}",
            amount_cents=gross,
            batch_fee_cents=None,
            business_date_=day,
            manifest=manifest,
        )
        p = get_or_create_payment(
            db,
            vendor=v2,
            order=o,
            native_id=f"f2-payment-{native_suffix}",
            custody="centrally_collected",
            amount_cents=gross,
            manifest=manifest,
            bank_entry_id=be.id,
        )
        create_matched_order_group(
            db, festival_id=fid2, order=o, payment=p, matched_amount_cents=gross, manifest=manifest
        )
    ids["fixture2_festival_id"] = fid2
    ids["fixture2_vendor_id"] = v2.id
    ids["fixture2_day1"] = DAY1.isoformat()
    ids["fixture2_day2"] = DAY2.isoformat()
    ids["fixture2_day3_final"] = DAY3.isoformat()

    # ===================================================================
    # Fixture 3: $400 eligible sales (single day, final period), proceeds
    # cannot cover the $500 minimum -> $100 unpaid, $0 payout. Tax $70 /
    # tips $40 (sum $110) are DELIBERATELY MORE than the $100 shortfall,
    # to prove they are never used to cover it anyway. All amounts in CENTS.
    #   gross capture = 51,000 ($510); tax=7,000 ($70); tips=4,000 ($40)
    #   eligible_for_order = 51,000 - 7,000 - 4,000 = 40,000  <-- $400 target
    #   commission: pct=15%*40,000=6,000; final -> total_due=max(6000,50000)=50000
    #   fee_assessed=50,000 ($500); fee_collected_from_central=min(50000,40000)=40000
    #   unpaid_balance = max(50,000-40,000, 0) = 10,000  ($100)
    #   payout = max(40,000-40,000, 0) = 0
    #   tax_cents=7,000, tips_cents=4,000 unchanged (sum 11,000 >= 10,000 shortfall,
    #   but never read by the fee/unpaid-balance calculation)
    # ===================================================================
    fid3 = festival_id_for(3)
    v3 = get_or_create_vendor(db, festival_id=fid3, name="Fixture3 Shortfall Vendor")
    o3 = get_or_create_order(
        db,
        vendor=v3,
        native_id="f3-order-1",
        business_date_=DAY1,
        gross_cents=51_000,
        tax_cents=7_000,
        tips_cents=4_000,
        manifest=manifest,
    )
    be3 = get_or_create_bank_entry(
        db,
        festival_id=fid3,
        provider_reference="f3-bank-batch-1",
        amount_cents=51_000,
        batch_fee_cents=None,
        business_date_=DAY1,
        manifest=manifest,
    )
    p3 = get_or_create_payment(
        db,
        vendor=v3,
        order=o3,
        native_id="f3-payment-1",
        custody="centrally_collected",
        amount_cents=51_000,
        manifest=manifest,
        bank_entry_id=be3.id,
    )
    create_matched_order_group(
        db, festival_id=fid3, order=o3, payment=p3, matched_amount_cents=51_000, manifest=manifest
    )
    ids["fixture3_festival_id"] = fid3
    ids["fixture3_vendor_id"] = v3.id
    ids["fixture3_business_date"] = DAY1.isoformat()

    # ===================================================================
    # Fixture 4: vendor already holding $10,000 VENDOR-RETAINED cash
    # (single day, final period) must NOT also produce a ~$8,500 bank
    # payout -- the organizer fee becomes a receivable, payout is $0.
    # All amounts in CENTS.
    #   vendor_retained capture = 1,000,000 ($10,000); tax=0; tips=0
    #   commission: pct=15%*1,000,000=150,000; final -> total_due=max(150000,50000)=150000
    #   fee_assessed=150,000; central=0 -> fee_collected_from_central=0
    #   total_eligible = 0 (central) + 1,000,000 (vendor_retained) = 1,000,000
    #   unpaid_balance = max(150,000-1,000,000, 0) = 0  (total eligible covers it)
    #   vendor_fee_receivable = (150,000-0) - 0 = 150,000 ($1,500, collect from
    #     vendor directly -- never fabricated as a bank payout)
    #   payout_before_fee = max(central(0) - collected(0), 0) = 0
    #   payout = 0   <-- NOT ~850,000 ($8,500)
    # ===================================================================
    fid4 = festival_id_for(4)
    v4 = get_or_create_vendor(db, festival_id=fid4, name="Fixture4 CashHeld Vendor")
    o4 = get_or_create_order(
        db,
        vendor=v4,
        native_id="f4-order-1",
        business_date_=DAY1,
        gross_cents=1_000_000,
        tax_cents=0,
        tips_cents=0,
        manifest=manifest,
    )
    p4 = get_or_create_payment(
        db,
        vendor=v4,
        order=o4,
        native_id="f4-payment-1",
        custody="vendor_retained",
        amount_cents=1_000_000,
        manifest=manifest,
        bank_entry_id=None,
    )
    create_matched_order_group(
        db,
        festival_id=fid4,
        order=o4,
        payment=p4,
        matched_amount_cents=1_000_000,
        manifest=manifest,
    )
    ids["fixture4_festival_id"] = fid4
    ids["fixture4_vendor_id"] = v4.id
    ids["fixture4_business_date"] = DAY1.isoformat()

    # ===================================================================
    # Fixture 5: late refund AFTER a confirmed payout -> linked correction,
    # original untouched. See apply_fixture5_late_refund() below -- the
    # refund is applied by verify_a04_settlement.py AFTER the first
    # /settlements/calculate call and the manual CONFIRMED Payout insert,
    # not during this seed() pass. All amounts in CENTS.
    #   Pass 1: gross capture = 500,000 ($5,000); tax/tips=0.
    #     commission: pct=15%*500,000=75,000; final -> total_due=max(75000,50000)=75000
    #     fee_assessed=75,000 ($750); collected=75,000 (central=500,000 >= 75,000)
    #     payout = 500,000 - 75,000 = 425,000 ($4,250)
    #   A CONFIRMED Payout of 425,000 is inserted (dev stand-in for A07).
    #   Late refund: matched_amount_cents 500,000 -> 400,000 ($5,000 -> $4,000).
    #   Pass 2 (recompute, same vendor/day): eligible=400,000.
    #     pct=15%*400,000=60,000; final -> total_due=max(60000,50000)=60000 (pct wins)
    #     fee_assessed=60,000 ($600); collected=60,000; payout=400,000-60,000=340,000 ($3,400)
    #     amount_already_paid(425,000) > updated_payout(340,000)
    #       -> overpayment_recovery_cents = 425,000-340,000 = 85,000 ($850)
    #       -> additional_payout_cents = 0
    # ===================================================================
    fid5 = festival_id_for(5)
    v5 = get_or_create_vendor(db, festival_id=fid5, name="Fixture5 LateRefund Vendor")
    o5 = get_or_create_order(
        db,
        vendor=v5,
        native_id="f5-order-1",
        business_date_=DAY1,
        gross_cents=500_000,
        tax_cents=0,
        tips_cents=0,
        manifest=manifest,
    )
    be5 = get_or_create_bank_entry(
        db,
        festival_id=fid5,
        provider_reference="f5-bank-batch-1",
        amount_cents=500_000,
        batch_fee_cents=None,
        business_date_=DAY1,
        manifest=manifest,
    )
    p5 = get_or_create_payment(
        db,
        vendor=v5,
        order=o5,
        native_id="f5-payment-1",
        custody="centrally_collected",
        amount_cents=500_000,
        manifest=manifest,
        bank_entry_id=be5.id,
    )
    create_matched_order_group(
        db,
        festival_id=fid5,
        order=o5,
        payment=p5,
        matched_amount_cents=500_000,
        manifest=manifest,
    )
    ids["fixture5_festival_id"] = fid5
    ids["fixture5_vendor_id"] = v5.id
    ids["fixture5_order_id"] = o5.id
    ids["fixture5_business_date"] = DAY1.isoformat()

    # ===================================================================
    # Fixture 6: multi-day grouped net processor deposit, fee deductible
    # by contract. Captures DAY1=300,000 ($3,000) + DAY2(final)=200,000
    # ($2,000) = 500,000 ($5,000) total, one shared BankEntry with
    # batch_fee_cents=10,000 ($100), deposit amount=490,000 ($4,900 ==
    # 500,000-10,000). All amounts in CENTS.
    #   Day1 (not final): pct_to_date=15%*300,000=45,000. increment=45,000-0=45,000.
    #     fee_collected_from_central=45,000.
    #     processor fee share = 10,000 * 300,000/500,000 = 6,000 ($60, deductible:
    #       term says so)
    #     payout_before_fee = 300,000-45,000=255,000; payout = 255,000-6,000 = 249,000 ($2,490)
    #   Day2 (FINAL): cum=500,000. pct_to_date=15%*500,000=75,000.
    #     total_due=max(75000,50000)=75000 (pct wins, minimum NOT applied).
    #     increment = 75,000-45,000=30,000. fee_collected_from_central=30,000.
    #     processor fee share = 10,000 * 200,000/500,000 = 4,000 ($40)
    #     payout_before_fee = 200,000-30,000=170,000; payout = 170,000-4,000 = 166,000 ($1,660)
    #   Reconciliation: total organizer fee 45,000+30,000=75,000 ($750) = 15%*500,000
    #   (checks out against the minimum NOT being triggered); total processor
    #   fee collected 6,000+40,000=10,000 ($100) = batch_fee_cents; total vendor
    #   payout 249,000+166,000=415,000 ($4,150) = 500,000(gross captures) -
    #   75,000(organizer fee) - 10,000(processor fee), i.e. gross/fee/net all
    #   reconcile.
    # ===================================================================
    fid6 = festival_id_for(6)
    v6 = get_or_create_vendor(db, festival_id=fid6, name="Fixture6 MultiDay ProcessorFee Vendor")
    get_or_create_contract_term(db, vendor=v6, effective_from=DAY1, processor_fee_deductible=True)
    be6 = get_or_create_bank_entry(
        db,
        festival_id=fid6,
        provider_reference="f6-bank-batch-shared",
        amount_cents=490_000,
        batch_fee_cents=10_000,
        business_date_=DAY1,
        manifest=manifest,
    )
    o6a = get_or_create_order(
        db,
        vendor=v6,
        native_id="f6-order-day1",
        business_date_=DAY1,
        gross_cents=300_000,
        tax_cents=0,
        tips_cents=0,
        manifest=manifest,
    )
    p6a = get_or_create_payment(
        db,
        vendor=v6,
        order=o6a,
        native_id="f6-payment-day1",
        custody="centrally_collected",
        amount_cents=300_000,
        manifest=manifest,
        bank_entry_id=be6.id,
    )
    create_matched_order_group(
        db,
        festival_id=fid6,
        order=o6a,
        payment=p6a,
        matched_amount_cents=300_000,
        manifest=manifest,
    )
    o6b = get_or_create_order(
        db,
        vendor=v6,
        native_id="f6-order-day2",
        business_date_=DAY2,
        gross_cents=200_000,
        tax_cents=0,
        tips_cents=0,
        manifest=manifest,
    )
    p6b = get_or_create_payment(
        db,
        vendor=v6,
        order=o6b,
        native_id="f6-payment-day2",
        custody="centrally_collected",
        amount_cents=200_000,
        manifest=manifest,
        bank_entry_id=be6.id,
    )
    create_matched_order_group(
        db,
        festival_id=fid6,
        order=o6b,
        payment=p6b,
        matched_amount_cents=200_000,
        manifest=manifest,
    )
    ids["fixture6_festival_id"] = fid6
    ids["fixture6_vendor_id"] = v6.id
    ids["fixture6_day1"] = DAY1.isoformat()
    ids["fixture6_day2_final"] = DAY2.isoformat()

    db.commit()
    return ids


def apply_fixture5_late_refund(db: Session) -> None:
    """Mutate fixture 5's order_capture MatchGroup to simulate a late refund
    that a (hypothetical) later A03 reconciliation run already netted in --
    matched_amount_cents drops from 500,000 to 400,000, representing a
    $1,000 refund discovered after the vendor was already paid out.

    This directly updates the MatchGroup row rather than going through A03's
    reconciliation engine, because this fixture is scoped to exercising
    A04's *correction* path (calculate_and_record_settlement's "prior
    CONFIRMED payout exists" branch), not A03's refund-netting logic (already
    covered by scripts/verify_a03_reconciliation.py). Idempotent: setting the
    same value twice is a no-op.
    """
    payment = db.execute(
        select(Payment).where(Payment.source_native_id == "f5-payment-1")
    ).scalar_one()
    group = db.execute(
        select(MatchGroup)
        .join(MatchGroupMember, MatchGroupMember.match_group_id == MatchGroup.id)
        .where(
            MatchGroup.match_type == "order_capture",
            MatchGroupMember.member_kind == "payment",
            MatchGroupMember.member_id == payment.id,
        )
    ).scalar_one()
    group.matched_amount_cents = 400_000
    db.add(group)
    db.flush()
    db.commit()


def main() -> int:
    make_engine()
    session_factory = get_session_factory()
    db = session_factory()
    try:
        ids = seed(db)
        print(f"dev_fixtures_a04: seeded/verified {len(ids)} tracked fixture ids")
        for key, value in sorted(ids.items()):
            print(f"  {key} = {value}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
