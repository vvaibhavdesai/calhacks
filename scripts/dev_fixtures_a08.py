#!/usr/bin/env python3
"""THROWAWAY, HAND-CHECKED dev fixtures for A08 books verification.

*** THIS IS NOT B01. *** On `lane` there is no B01; lane B's independent
oracle lives on `lane-b` (tests/oracle/). Same convention as
dev_fixtures_a03..a07.py: a small, clearly labeled, hand-checkable scenario
written by the same author as the code under test -- a development aid,
not independent verification.

What is seeded directly in the database (operational inputs A08 reads but
does not own): a source manifest (via A02's real `import_source`), vendors,
orders with tax-categorized lines, payments, a processor deposit and
settlements. Settlement numbers are computed by calling A04's REAL engine
(`domain.settlement.calculate_vendor_settlement`), never hand-typed.
Everything A08 owns (payables, other income/expense, journals, reports,
statements, close) is then driven over real HTTP by
scripts/verify_a08_finance.py.

Night 1 (D1 = 2026-10-16), DEMO tax 9.25% prepared food, packaged exempt:

  Taco (T), centrally collected:
    O1 card: L1 prepared 100.00 + tax 9.25 + tips 5.00; L2 packaged 20.00
             (gross 134.25). Partial card refund of 26.85 (20%).
    O2 cash (organizer cash office): prepared 40.00 + tax 3.70 = 43.70
    O3 card: prepared 10.00 + tax 1.00 (supplied; expected 0.93) = 11.00
  Retained (R): O4 cash vendor_retained: prepared 50.00 + tax 4.63
  Held (H):     O5 cash vendor_retained: prepared 10.00 + tax 0.93; an OPEN
                case on H.
  Processor deposit DEP-1: gross 118.40, fee 3.55, bank 114.85.

Hand-worked expectations (cents):
  refund allocation: tax round(925 x 2685/13425) = 185, tips 100, eligible 2400
  T eligible central = 12000 + 4000 + 1000 - 2400 = 14600
  T commission (15%, non-final) = 2190; payout = 12410
  R commission 750 (all receivable); H commission 150 (all receivable)
  sales tax liability (central supplied) = 925 + 370 + 100 - 185 = 1210
  recalculated central tax = 925 + 370 + 93 - 185 = 1203 (variance 7, one line)
  vendor-retained tax memo = 463 + 93 = 556
  tips liability = 500 - 100 = 400
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app.ingestion import import_source  # noqa: E402
from app.models_ledger import Case, Order, OrderLine, Payment, Vendor  # noqa: E402
from app.models_reconciliation import BankEntry  # noqa: E402
from app.models_settlement import Settlement, SettlementLineItem  # noqa: E402
from app.storage.local_fs import get_document_storage  # noqa: E402
from domain.settlement import EligibleSalesInput, calculate_vendor_settlement  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

SOURCE_SYSTEM = "a08_devfixtures"
D0 = date(2026, 10, 15)
D1 = date(2026, 10, 16)
D2 = date(2026, 10, 17)


@dataclass
class Manifest:
    doc_id: str
    version: int
    content_hash: str

    def ref(self, locator: str) -> dict:
        return {
            "source_document_id": self.doc_id,
            "source_version": self.version,
            "content_hash": self.content_hash,
            "locator_kind": "row",
            "locator": locator,
        }


def manifest(db: Session, run: str) -> Manifest:
    content = json.dumps(
        {"name": "a08-dev-fixture-manifest", "run": run, "note": "hand-checked, not B01"}
    ).encode()
    r = import_source(
        db,
        get_document_storage(),
        source_system=SOURCE_SYSTEM,
        source_native_id=f"a08-manifest-{run}",
        content_bytes=content,
        mime_type="application/json",
        actor_id="dev-fixture-script",
        actor_capability="import_source",
    )
    db.commit()
    return Manifest(r.source_document_id, r.version, r.content_hash)


def vendor(db: Session, festival_id: str, name: str) -> Vendor:
    v = Vendor(festival_id=festival_id, name=name)
    db.add(v)
    db.flush()
    return v


def order(db: Session, m: Manifest, v: Vendor, native: str, d: date, lines: list[tuple]) -> Order:
    """lines: (line_id, tax_category, eligible, tax, tips)"""
    gross = sum(e + t + tp for _l, _c, e, t, tp in lines)
    ref = m.ref(f"order:{native}")
    o = Order(
        vendor_id=v.id,
        source_system=SOURCE_SYSTEM,
        source_native_id=native,
        business_date=d,
        ordered_at=datetime(d.year, d.month, d.day, 20, 0, tzinfo=UTC),
        gross_cents=gross,
        tax_cents=sum(t for *_x, t, _tp in lines),
        tips_cents=sum(tp for *_x, tp in lines),
        source_document_id=ref["source_document_id"],
        source_version=ref["source_version"],
        content_hash=ref["content_hash"],
        locator_kind=ref["locator_kind"],
        locator=ref["locator"],
    )
    db.add(o)
    db.flush()
    for line_id, cat, e, t, tp in lines:
        db.add(
            OrderLine(
                order_id=o.id,
                line_id=line_id,
                quantity=1,
                eligible_net_cents=e,
                tax_cents=t,
                tips_cents=tp,
                gross_cents=e + t + tp,
                tax_category=cat,
                source_refs=[m.ref(f"order:{native}:line:{line_id}")],
            )
        )
    db.flush()
    return o


def payment(
    db: Session,
    m: Manifest,
    o: Order,
    native: str,
    *,
    method: str,
    custody: str,
    amount: int,
    kind: str = "capture",
) -> Payment:
    ref = m.ref(f"payment:{native}")
    p = Payment(
        vendor_id=o.vendor_id,
        order_id=o.id,
        source_system=SOURCE_SYSTEM,
        source_native_id=native,
        method=method,
        custody=custody,
        amount_cents=amount,
        kind=kind,
        **ref,
    )
    db.add(p)
    db.flush()
    return p


def deposit(
    db: Session, m: Manifest, festival_id: str, ref_id: str, d: date, gross: int, fee: int
) -> BankEntry:
    ref = m.ref(f"bank:{ref_id}")
    be = BankEntry(
        festival_id=festival_id,
        account_id="operating-001",
        provider_reference=ref_id,
        direction="credit",
        amount_cents=gross - fee,
        batch_gross_cents=gross,
        batch_fee_cents=fee,
        settlement_status="settled",
        business_date=d,
        occurred_at=datetime(d.year, d.month, d.day, 23, 0, tzinfo=UTC),
        **ref,
    )
    db.add(be)
    db.flush()
    return be


def settlement(
    db: Session,
    m: Manifest,
    v: Vendor,
    d: date,
    *,
    central: int,
    retained: int,
    tax: int,
    tips: int,
    correction_of: str | None = None,
) -> Settlement:
    """Computed by A04's real engine (non-final period, 15% / $500)."""
    r = calculate_vendor_settlement(
        eligible=EligibleSalesInput(
            centrally_held_cents=central,
            vendor_retained_cash_cents=retained,
            tax_cents=tax,
            tips_cents=tips,
        ),
        cumulative_eligible_sales_cents=central + retained,
        cumulative_commission_assessed_prior_cents=0,
        is_final_period=False,
    )
    s = Settlement(
        festival_id=v.festival_id,
        vendor_id=v.id,
        business_date=d,
        calculation_version=r.calculation_version + ("-correction" if correction_of else ""),
        commission_bps_used=1500,
        minimum_fee_cents_used=50_000,
        processor_fee_deductible_used=False,
        is_final_period=False,
        eligible_sales_cents=r.eligible_sales_cents,
        centrally_held_cents=r.centrally_held_cents,
        vendor_retained_cash_cents=r.vendor_retained_cash_cents,
        tax_cents=r.tax_cents,
        tips_cents=r.tips_cents,
        cumulative_eligible_sales_cents=r.cumulative_eligible_sales_cents,
        cumulative_commission_assessed_prior_cents=0,
        assessed_fee_cents=r.fee_assessed_cents,
        collected_fee_cents=r.fee_collected_from_central_cents,
        vendor_fee_receivable_cents=r.vendor_fee_receivable_cents,
        unpaid_balance_cents=r.unpaid_balance_cents,
        processor_fee_assessed_cents=0,
        processor_fee_collected_cents=0,
        payout_cents=r.vendor_payout_cents,
        minimum_applied=r.minimum_applied,
        holds=[],
        policy_refs=[],
        correction_of=correction_of,
        status="CORRECTION" if correction_of else "FINAL",
    )
    db.add(s)
    db.flush()
    for code, amt in (
        ("eligible_sales", r.eligible_sales_cents),
        ("commission_assessed", r.fee_assessed_cents),
        ("vendor_payout", r.vendor_payout_cents),
    ):
        db.add(
            SettlementLineItem(
                settlement_id=s.id,
                line_type=code,
                amount_cents=amt,
                source_refs=[m.ref(f"settlement:{v.name}:{d.isoformat()}:{code}")],
            )
        )
    db.flush()
    return s


def seed_night1(db: Session, festival_id: str, m: Manifest) -> dict:
    t = vendor(db, festival_id, "Taco")
    r = vendor(db, festival_id, "Retained")
    h = vendor(db, festival_id, "Held")
    o1 = order(
        db,
        m,
        t,
        f"{festival_id}-O1",
        D1,
        [("L1", "prepared_food", 10_000, 925, 500), ("L2", "packaged_food", 2_000, 0, 0)],
    )
    payment(
        db, m, o1, f"{festival_id}-P1", method="card", custody="centrally_collected", amount=13_425
    )
    payment(
        db,
        m,
        o1,
        f"{festival_id}-P1R",
        method="card",
        custody="centrally_collected",
        amount=2_685,
        kind="refund",
    )
    o2 = order(db, m, t, f"{festival_id}-O2", D1, [("L1", "prepared_food", 4_000, 370, 0)])
    payment(
        db, m, o2, f"{festival_id}-P2", method="cash", custody="centrally_collected", amount=4_370
    )
    o3 = order(db, m, t, f"{festival_id}-O3", D1, [("L1", "prepared_food", 1_000, 100, 0)])
    payment(
        db, m, o3, f"{festival_id}-P3", method="card", custody="centrally_collected", amount=1_100
    )
    o4 = order(db, m, r, f"{festival_id}-O4", D1, [("L1", "prepared_food", 5_000, 463, 0)])
    payment(db, m, o4, f"{festival_id}-P4", method="cash", custody="vendor_retained", amount=5_463)
    o5 = order(db, m, h, f"{festival_id}-O5", D1, [("L1", "prepared_food", 1_000, 93, 0)])
    payment(db, m, o5, f"{festival_id}-P5", method="cash", custody="vendor_retained", amount=1_093)
    be = deposit(db, m, festival_id, f"{festival_id}-DEP1", D1, gross=11_840, fee=355)
    st = settlement(db, m, t, D1, central=14_600, retained=0, tax=1_210, tips=400)
    sr = settlement(db, m, r, D1, central=0, retained=5_000, tax=463, tips=0)
    sh = settlement(db, m, h, D1, central=0, retained=1_000, tax=93, tips=0)
    case = Case(
        vendor_id=h.id,
        reason_code="A08_VERIFY_HOLD",
        evidence_refs=[],
        required_capability="resolve_case",
        status="OPEN",
    )
    db.add(case)
    db.commit()
    return {
        "taco": t.id,
        "retained": r.id,
        "held": h.id,
        "o3": o3.id,
        "deposit": be.id,
        "settlement_taco": st.id,
        "settlement_retained": sr.id,
        "settlement_held": sh.id,
        "taco_payout_cents": st.payout_cents,
        "taco_commission_cents": st.assessed_fee_cents,
        "case_held": case.id,
    }


def late_refund_o3(db: Session, m: Manifest, festival_id: str, order_id: str) -> None:
    o = db.get(Order, order_id)
    payment(
        db,
        m,
        o,
        f"{festival_id}-P3R",
        method="card",
        custody="centrally_collected",
        amount=550,
        kind="refund",
    )
    db.commit()


def correct_retained(db: Session, m: Manifest, settlement_id: str) -> Settlement:
    original = db.get(Settlement, settlement_id)
    v = db.get(Vendor, original.vendor_id)
    s = settlement(
        db,
        m,
        v,
        original.business_date,
        central=0,
        retained=6_000,
        tax=555,
        tips=0,
        correction_of=original.id,
    )
    db.commit()
    return s
