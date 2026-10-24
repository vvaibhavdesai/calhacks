#!/usr/bin/env python3
"""THROWAWAY, HAND-CHECKED dev fixtures for A03 reconciliation verification.

*** THIS IS NOT B01. *** B01 ("compact synthetic fixture and independent
oracle") does not exist in this repository -- see docs/progress/A02-handoff.md
and docs/progress/A03-handoff.md. Per the user's explicit decision recorded
in this task's brief, this script builds a small, clearly-labeled,
hand-checkable substitute so A03's matching logic can be exercised against
real data in a real Postgres database instead of being merely described.
Every expected outcome below was worked out by hand (see the comment next
to each scenario) and is re-checked mechanically by
scripts/verify_a03_reconciliation.py. This is NOT an independently derived
oracle: the same person who wrote app/reconciliation.py also wrote the
expected answers here. Treat it as a development/demo aid, not evidence of
independent verification. If/when a real B01 lands, these fixtures should be
retired in favor of it.

What this script does NOT do: it does not exercise a structured, idempotent
orders/payments *ingestion* endpoint, because A02 deliberately did not build
one (see docs/progress/A02-handoff.md, open question #3 -- only the generic
raw-bytes `import_source()` exists). Instead, the get_or_create_* helpers
below implement the same idempotency *pattern* A02's `import_source` uses
(look up by natural/business key before inserting) directly against the
Order/Payment/BankEntry/CashCount tables, so that running this script twice
is provably a no-op (see verify_a03_reconciliation.py's duplicate-import
check) -- without overclaiming that a production structured-ingestion
service exists.

Usage (from repo root, real Postgres required, no SQLite):
    DATABASE_URL=... uv run python scripts/dev_fixtures_a03.py

Safe to re-run: every entity is looked up by a natural key before being
created, and the Order/Payment unique constraints
(uq_order_native/uq_payment_native) back that up at the database level.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app.db import get_session_factory, make_engine  # noqa: E402
from app.ingestion import import_source  # noqa: E402
from app.models_ledger import Order, Payment, Vendor  # noqa: E402
from app.models_reconciliation import BankEntry, CashCount  # noqa: E402
from app.storage.local_fs import get_document_storage  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

FESTIVAL_ID = "gcmf-2026-a03dev"
SOURCE_SYSTEM = "a03_devfixtures"

# Single shared source document that every fixture row cites via SourceRef,
# per C01: hashed, retrievable, not fabricated. A real generator (B02) would
# give each row its own source document; this is an honest simplification
# for a hand-built fixture set, documented rather than hidden.
_MANIFEST_NATIVE_ID = "a03-dev-fixture-manifest-v1"


def _ingest_manifest(db: Session) -> tuple[str, int, str]:
    """Idempotently import one tiny JSON manifest; returns
    (source_document_id, version, content_hash) to cite from every fixture
    row's SourceRef fields."""
    storage = get_document_storage()
    content = (
        b'{"name": "a03-dev-fixture-manifest", "version": 1, '
        b'"note": "throwaway hand-checked A03 dev fixtures, not B01"}'
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


def get_or_create_vendor(db: Session, *, name: str) -> tuple[Vendor, bool]:
    existing = db.execute(
        select(Vendor).where(Vendor.festival_id == FESTIVAL_ID, Vendor.name == name)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    vendor = Vendor(festival_id=FESTIVAL_ID, name=name)
    db.add(vendor)
    db.flush()
    return vendor, True


def get_or_create_order(
    db: Session,
    *,
    vendor: Vendor,
    native_id: str,
    business_date_: date,
    gross_cents: int,
    manifest: tuple[str, int, str],
    tax_cents: int = 0,
    tips_cents: int = 0,
) -> tuple[Order, bool]:
    existing = db.execute(
        select(Order).where(
            Order.source_system == SOURCE_SYSTEM, Order.source_native_id == native_id
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
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
    return order, True


def get_or_create_payment(
    db: Session,
    *,
    vendor: Vendor,
    order: Order | None,
    native_id: str,
    method: str,
    custody: str,
    amount_cents: int,
    kind: str,
    manifest: tuple[str, int, str],
    bank_entry_id: str | None = None,
) -> tuple[Payment, bool]:
    existing = db.execute(
        select(Payment).where(
            Payment.source_system == SOURCE_SYSTEM, Payment.source_native_id == native_id
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    ref = _source_ref(manifest, f"payment:{native_id}")
    payment = Payment(
        vendor_id=vendor.id,
        order_id=order.id if order is not None else None,
        source_system=SOURCE_SYSTEM,
        source_native_id=native_id,
        method=method,
        custody=custody,
        amount_cents=amount_cents,
        kind=kind,
        bank_entry_id=bank_entry_id,
        source_document_id=ref["source_document_id"],
        source_version=ref["source_version"],
        content_hash=ref["content_hash"],
        locator_kind=ref["locator_kind"],
        locator=ref["locator"],
    )
    db.add(payment)
    db.flush()
    return payment, True


def get_or_create_bank_entry(
    db: Session,
    *,
    provider_reference: str,
    amount_cents: int,
    batch_fee_cents: int | None,
    settlement_status: str,
    business_date_: date,
    manifest: tuple[str, int, str],
) -> tuple[BankEntry, bool]:
    existing = db.execute(
        select(BankEntry).where(
            BankEntry.festival_id == FESTIVAL_ID,
            BankEntry.provider_reference == provider_reference,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    ref = _source_ref(manifest, f"bank_entry:{provider_reference}")
    entry = BankEntry(
        festival_id=FESTIVAL_ID,
        account_id="operating-clearing-001",
        provider_reference=provider_reference,
        direction="credit",
        amount_cents=amount_cents,
        batch_fee_cents=batch_fee_cents,
        settlement_status=settlement_status,
        business_date=business_date_,
        occurred_at=datetime(
            business_date_.year, business_date_.month, business_date_.day, 23, 0, tzinfo=UTC
        ),
        settled_at=(
            datetime(
                business_date_.year, business_date_.month, business_date_.day, 23, 0, tzinfo=UTC
            )
            if settlement_status == "settled"
            else None
        ),
        source_document_id=ref["source_document_id"],
        source_version=ref["source_version"],
        content_hash=ref["content_hash"],
        locator_kind=ref["locator_kind"],
        locator=ref["locator"],
    )
    db.add(entry)
    db.flush()
    return entry, True


def get_or_create_cash_count(
    db: Session,
    *,
    vendor: Vendor,
    business_date_: date,
    counted_cents: int,
    manifest: tuple[str, int, str],
) -> tuple[CashCount, bool]:
    existing = db.execute(
        select(CashCount).where(
            CashCount.vendor_id == vendor.id, CashCount.business_date == business_date_
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    ref = _source_ref(manifest, f"cash_count:{vendor.id}:{business_date_.isoformat()}")
    count = CashCount(
        vendor_id=vendor.id,
        business_date=business_date_,
        counted_cents=counted_cents,
        counted_at=datetime(
            business_date_.year, business_date_.month, business_date_.day, 23, 30, tzinfo=UTC
        ),
        counted_by="dev-fixture-script",
        source_document_id=ref["source_document_id"],
        source_version=ref["source_version"],
        content_hash=ref["content_hash"],
        locator_kind=ref["locator_kind"],
        locator=ref["locator"],
    )
    db.add(count)
    db.flush()
    return count, True


DAY1 = date(2026, 10, 16)
PRE_FESTIVAL_DAY = date(2026, 10, 1)  # deliberately far in the past -- see
# "overdue bank settlement" scenario below.


def seed(db: Session) -> dict:
    """Create (or reuse, if already present) every A03 dev-fixture scenario.
    Returns a dict of vendor_id/order_id/etc keyed by scenario name, for
    scripts/verify_a03_reconciliation.py to assert against."""
    manifest = _ingest_manifest(db)
    ids: dict[str, str] = {}

    # --- Vendor A: normal vendor, clean single order/payment match --------
    vendor_a, _ = get_or_create_vendor(db, name="Normal Taco Vendor")
    order_a1, _ = get_or_create_order(
        db,
        vendor=vendor_a,
        native_id="order-A-1",
        business_date_=DAY1,
        gross_cents=5000,
        manifest=manifest,
    )
    pay_a1, _ = get_or_create_payment(
        db,
        vendor=vendor_a,
        order=order_a1,
        native_id="payment-A-1-capture",
        method="card",
        custody="centrally_collected",
        amount_cents=5000,
        kind="capture",
        manifest=manifest,
    )
    ids["vendor_a"] = vendor_a.id
    ids["order_a1"] = order_a1.id
    ids["payment_a1"] = pay_a1.id

    # --- Vendor B: one order, split across two captures (one-to-many) -----
    vendor_b, _ = get_or_create_vendor(db, name="One-to-Many Split Vendor")
    order_b1, _ = get_or_create_order(
        db,
        vendor=vendor_b,
        native_id="order-B-1",
        business_date_=DAY1,
        gross_cents=8000,
        manifest=manifest,
    )
    pay_b1, _ = get_or_create_payment(
        db,
        vendor=vendor_b,
        order=order_b1,
        native_id="payment-B-1-capture-1",
        method="card",
        custody="centrally_collected",
        amount_cents=3000,
        kind="capture",
        manifest=manifest,
    )
    pay_b2, _ = get_or_create_payment(
        db,
        vendor=vendor_b,
        order=order_b1,
        native_id="payment-B-1-capture-2",
        method="card",
        custody="centrally_collected",
        amount_cents=5000,
        kind="capture",
        manifest=manifest,
    )
    ids["vendor_b"] = vendor_b.id
    ids["order_b1"] = order_b1.id
    ids["payment_b1"] = pay_b1.id
    ids["payment_b2"] = pay_b2.id

    # --- Vendor C: refund / chargeback / duplicate-charge scenarios -------
    vendor_c, _ = get_or_create_vendor(db, name="Fee Timing Refund Vendor")
    ids["vendor_c"] = vendor_c.id

    # C-order1: captured correctly, then refunded -- matched (refund_applied)
    order_c1, _ = get_or_create_order(
        db,
        vendor=vendor_c,
        native_id="order-C-1",
        business_date_=DAY1,
        gross_cents=4000,
        manifest=manifest,
    )
    get_or_create_payment(
        db,
        vendor=vendor_c,
        order=order_c1,
        native_id="payment-C-1-capture",
        method="card",
        custody="centrally_collected",
        amount_cents=4000,
        kind="capture",
        manifest=manifest,
    )
    get_or_create_payment(
        db,
        vendor=vendor_c,
        order=order_c1,
        native_id="payment-C-1-refund",
        method="card",
        custody="centrally_collected",
        amount_cents=1000,
        kind="refund",
        manifest=manifest,
    )
    ids["order_c1"] = order_c1.id

    # C-order2: duplicate real charge, fully explained by a refund -- matched
    # (duplicate_charge_refunded). This is the "duplicate REAL charge that
    # was resolved" half of the duplicate-import-vs-duplicate-charge pair.
    order_c2, _ = get_or_create_order(
        db,
        vendor=vendor_c,
        native_id="order-C-2",
        business_date_=DAY1,
        gross_cents=3000,
        manifest=manifest,
    )
    get_or_create_payment(
        db,
        vendor=vendor_c,
        order=order_c2,
        native_id="payment-C-2-capture-1",
        method="card",
        custody="centrally_collected",
        amount_cents=3000,
        kind="capture",
        manifest=manifest,
    )
    get_or_create_payment(
        db,
        vendor=vendor_c,
        order=order_c2,
        native_id="payment-C-2-capture-2-duplicate",
        method="card",
        custody="centrally_collected",
        amount_cents=3000,
        kind="capture",
        manifest=manifest,
    )
    get_or_create_payment(
        db,
        vendor=vendor_c,
        order=order_c2,
        native_id="payment-C-2-refund-of-duplicate",
        method="card",
        custody="centrally_collected",
        amount_cents=3000,
        kind="refund",
        manifest=manifest,
    )
    ids["order_c2"] = order_c2.id

    # C-order3: duplicate real charge, UNRESOLVED -- must become a case
    # (suspected_duplicate_charge), never silently matched/dropped. This is
    # the other half of the duplicate-import-vs-duplicate-charge pair: two
    # genuinely separate Payment rows (different source_native_id), same
    # amount, no refund.
    order_c3, _ = get_or_create_order(
        db,
        vendor=vendor_c,
        native_id="order-C-3",
        business_date_=DAY1,
        gross_cents=2000,
        manifest=manifest,
    )
    get_or_create_payment(
        db,
        vendor=vendor_c,
        order=order_c3,
        native_id="payment-C-3-capture-1",
        method="card",
        custody="centrally_collected",
        amount_cents=2000,
        kind="capture",
        manifest=manifest,
    )
    get_or_create_payment(
        db,
        vendor=vendor_c,
        order=order_c3,
        native_id="payment-C-3-capture-2-duplicate-unresolved",
        method="card",
        custody="centrally_collected",
        amount_cents=2000,
        kind="capture",
        manifest=manifest,
    )
    ids["order_c3"] = order_c3.id

    # C-order4: capture + chargeback -- matched (chargeback_applied)
    order_c4, _ = get_or_create_order(
        db,
        vendor=vendor_c,
        native_id="order-C-4",
        business_date_=DAY1,
        gross_cents=5000,
        manifest=manifest,
    )
    get_or_create_payment(
        db,
        vendor=vendor_c,
        order=order_c4,
        native_id="payment-C-4-capture",
        method="card",
        custody="centrally_collected",
        amount_cents=5000,
        kind="capture",
        manifest=manifest,
    )
    get_or_create_payment(
        db,
        vendor=vendor_c,
        order=order_c4,
        native_id="payment-C-4-chargeback",
        method="card",
        custody="centrally_collected",
        amount_cents=5000,
        kind="chargeback",
        manifest=manifest,
    )
    ids["order_c4"] = order_c4.id

    # C-order5: refund LARGER than anything captured -- always a case
    # (refund_exceeds_captured_amount), never netted to a smaller number.
    order_c5, _ = get_or_create_order(
        db,
        vendor=vendor_c,
        native_id="order-C-5",
        business_date_=DAY1,
        gross_cents=1000,
        manifest=manifest,
    )
    get_or_create_payment(
        db,
        vendor=vendor_c,
        order=order_c5,
        native_id="payment-C-5-capture",
        method="card",
        custody="centrally_collected",
        amount_cents=1000,
        kind="capture",
        manifest=manifest,
    )
    get_or_create_payment(
        db,
        vendor=vendor_c,
        order=order_c5,
        native_id="payment-C-5-refund-too-big",
        method="card",
        custody="centrally_collected",
        amount_cents=1500,
        kind="refund",
        manifest=manifest,
    )
    ids["order_c5"] = order_c5.id

    bank_c1_payment_id = db.execute(
        select(Payment.id).where(Payment.source_native_id == "payment-C-1-capture")
    ).scalar_one()
    ids["payment_c1_capture_id"] = bank_c1_payment_id

    # --- Vendor D: booth cash counts, one matching day one short day ------
    vendor_d, _ = get_or_create_vendor(db, name="Cash Variance Vendor")
    order_d1, _ = get_or_create_order(
        db,
        vendor=vendor_d,
        native_id="order-D-1",
        business_date_=DAY1,
        gross_cents=2000,
        manifest=manifest,
    )
    get_or_create_payment(
        db,
        vendor=vendor_d,
        order=order_d1,
        native_id="payment-D-1-cash",
        method="cash",
        custody="vendor_retained",
        amount_cents=2000,
        kind="capture",
        manifest=manifest,
    )
    get_or_create_cash_count(
        db, vendor=vendor_d, business_date_=DAY1, counted_cents=1800, manifest=manifest
    )

    day2 = date(2026, 10, 17)
    order_d2, _ = get_or_create_order(
        db,
        vendor=vendor_d,
        native_id="order-D-2",
        business_date_=day2,
        gross_cents=1500,
        manifest=manifest,
    )
    get_or_create_payment(
        db,
        vendor=vendor_d,
        order=order_d2,
        native_id="payment-D-2-cash",
        method="cash",
        custody="vendor_retained",
        amount_cents=1500,
        kind="capture",
        manifest=manifest,
    )
    get_or_create_cash_count(
        db, vendor=vendor_d, business_date_=day2, counted_cents=1500, manifest=manifest
    )
    ids["vendor_d"] = vendor_d.id

    # --- Vendor E: order with NO payment at all -- missing_or_partial ------
    # This is the "one vendor has a real problem" vendor for the
    # fault-isolation acceptance criterion.
    vendor_e, _ = get_or_create_vendor(db, name="Missing Payment Vendor")
    order_e1, _ = get_or_create_order(
        db,
        vendor=vendor_e,
        native_id="order-E-1",
        business_date_=DAY1,
        gross_cents=10000,
        manifest=manifest,
    )
    ids["vendor_e"] = vendor_e.id
    ids["order_e1"] = order_e1.id

    # --- Vendor F: single overcharge, NOT duplicate-like -- order_overcharged
    # Also supplies the payment attached to the overdue bank-settlement
    # scenario below (business_date deliberately far in the past).
    vendor_f, _ = get_or_create_vendor(db, name="Overcharged Vendor")
    order_f1, _ = get_or_create_order(
        db,
        vendor=vendor_f,
        native_id="order-F-1",
        business_date_=PRE_FESTIVAL_DAY,
        gross_cents=1000,
        manifest=manifest,
    )
    pay_f1, _ = get_or_create_payment(
        db,
        vendor=vendor_f,
        order=order_f1,
        native_id="payment-F-1-capture",
        method="card",
        custody="centrally_collected",
        amount_cents=1200,
        kind="capture",
        manifest=manifest,
    )
    ids["vendor_f"] = vendor_f.id
    ids["order_f1"] = order_f1.id
    ids["payment_f1"] = pay_f1.id

    # --- Vendor G: settled bank deposit with an unexplained shortfall -----
    vendor_g, _ = get_or_create_vendor(db, name="Bank Variance Vendor")
    order_g1, _ = get_or_create_order(
        db,
        vendor=vendor_g,
        native_id="order-G-1",
        business_date_=DAY1,
        gross_cents=6000,
        manifest=manifest,
    )
    pay_g1, _ = get_or_create_payment(
        db,
        vendor=vendor_g,
        order=order_g1,
        native_id="payment-G-1-capture",
        method="card",
        custody="centrally_collected",
        amount_cents=6000,
        kind="capture",
        manifest=manifest,
    )
    ids["vendor_g"] = vendor_g.id
    ids["order_g1"] = order_g1.id
    ids["payment_g1"] = pay_g1.id

    db.flush()

    # --- Bank settlements (festival-level, many-to-one, timing) -----------

    # BE1: settled, many-to-one net deposit covering Vendor A's + Vendor B's
    # captures (5000 + 3000 + 5000 = 13000), minus a known $3.00 processor
    # fee -> matched, processor_fee_explained.
    be1, _ = get_or_create_bank_entry(
        db,
        provider_reference="bank-batch-BE1",
        amount_cents=12700,
        batch_fee_cents=300,
        settlement_status="settled",
        business_date_=DAY1,
        manifest=manifest,
    )
    db.execute(
        Payment.__table__.update()
        .where(Payment.id.in_([pay_a1.id, pay_b1.id, pay_b2.id]))
        .values(bank_entry_id=be1.id)
    )

    # BE2: NOT YET SETTLED, within the timing window -- must stay "pending",
    # never "matched"/available cash. Covers Vendor C's order-1 capture
    # (business_date DAY1); run as_of is DAY1 + 3 days.
    be2, _ = get_or_create_bank_entry(
        db,
        provider_reference="bank-batch-BE2",
        amount_cents=4000,
        batch_fee_cents=None,
        settlement_status="pending",
        business_date_=DAY1,
        manifest=manifest,
    )
    db.execute(
        Payment.__table__.update()
        .where(Payment.id == bank_c1_payment_id)
        .values(bank_entry_id=be2.id)
    )

    # BE3: NOT YET SETTLED, OVERDUE -- the covered payment's order is
    # PRE_FESTIVAL_DAY, far outside the 5-day timing window relative to the
    # run's as_of -> must become a case (deposit_settlement_overdue), not
    # stay silently "pending" forever.
    be3, _ = get_or_create_bank_entry(
        db,
        provider_reference="bank-batch-BE3",
        amount_cents=1200,
        batch_fee_cents=None,
        settlement_status="pending",
        business_date_=PRE_FESTIVAL_DAY,
        manifest=manifest,
    )
    db.execute(
        Payment.__table__.update().where(Payment.id == pay_f1.id).values(bank_entry_id=be3.id)
    )

    # BE4: settled, but the net amount is $0.50 short of captures-minus-fee
    # with NO stated fee to explain it -- must become a case
    # (bank_deposit_variance_unexplained), never silently written off.
    be4, _ = get_or_create_bank_entry(
        db,
        provider_reference="bank-batch-BE4",
        amount_cents=5950,
        batch_fee_cents=None,
        settlement_status="settled",
        business_date_=DAY1,
        manifest=manifest,
    )
    db.execute(
        Payment.__table__.update().where(Payment.id == pay_g1.id).values(bank_entry_id=be4.id)
    )

    ids["bank_entry_be1"] = be1.id
    ids["bank_entry_be2"] = be2.id
    ids["bank_entry_be3"] = be3.id
    ids["bank_entry_be4"] = be4.id

    db.commit()
    return ids


def main() -> int:
    make_engine()
    session_factory = get_session_factory()
    db = session_factory()
    try:
        ids = seed(db)
        print(f"dev_fixtures_a03: seeded/verified {len(ids)} tracked fixture ids")
        for key, value in sorted(ids.items()):
            print(f"  {key} = {value}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
