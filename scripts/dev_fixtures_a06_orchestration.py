#!/usr/bin/env python3
"""THROWAWAY, HAND-CHECKED dev fixture for A06 stage 3 (nightly close
orchestration). *** NOT B01. *** Same convention as dev_fixtures_a03..a08:
written by the author of the code under test; a development aid, not
independent verification.

Raw operational inputs only (orders, payments, one processor deposit, a
cash count, two conflicting proposed contract terms, cash pool funding).
Everything downstream -- match groups, cases, settlements, payouts,
journals, statements -- is produced by the orchestration run itself through
the existing A03/A04/A05/A07/A08 code.

Night 1 (D1 = 2026-10-16), non-final period, default 15% / $500 festival
minimum (minimum not applied on a non-final night), processor fees not
deductible:

  Alpha (A), card:  O1 100.00 + tax 9.25 = 109.25; O2 50.00 + tax 4.63
                    eligible 15000; fee 2250; payout 12750
  Beta (B), card:   O3 200.00 + tax 18.50; eligible 20000; fee 3000; payout 17000
  Gamma (G), card:  O4 80.00 + tax 7.40; eligible 8000. Signed contract
                    15% vs producer email 10% (two proposed terms) ->
                    A05 opens a vendor_contract_term_conflict case
                    (impact 1200 - 800 = 400) -> Gamma HELD at
                    TERMS_RESOLVED. Human resolves 10% -> resume ->
                    fee 800; payout 7200
  Delta (D), cash retained: O5 30.00 + tax 2.78 = 32.78; cash count 32.78
                    (matched); fee 450, all receivable; payout 0 ->
                    no payout step, posted and reported
  Epsilon (E), card: O6 6000.00 + tax 555.00; eligible 600000;
                    fee 90000; payout 510000 > $5,000/day automatic limit
                    -> HELD for a finance_lead approval -> approve ->
                    resume -> paid

  Deposit DEP-1 (settled): gross 109.25+54.63+218.50+87.40+6555.00 =
  7024.78, fee 21.07, bank 7003.71 -> bank_deposit matched.
  Cash pool funded $10,000.00 (1,000,000 cents); total paid
  12750+17000+7200+510000 = 546950.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))
sys.path.insert(0, os.path.dirname(__file__))

from app.models_ledger import VendorContractTerm  # noqa: E402
from app.models_reconciliation import CashCount  # noqa: E402
from app.payout_dispatch import fund_cash_pool  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import dev_fixtures_a08 as a08f  # noqa: E402

D1 = date(2026, 10, 16)

EXPECTED = {
    "Alpha": {"fee": 2250, "payout": 12750},
    "Beta": {"fee": 3000, "payout": 17000},
    "Gamma": {"fee": 800, "payout": 7200, "fee_at_15": 1200, "case_impact": 400},
    "Delta": {"fee": 450, "payout": 0, "receivable": 450},
    "Epsilon": {"fee": 90000, "payout": 510000},
}
CASH_POOL_CENTS = 1_000_000
DEPOSIT = {"gross": 702_478, "fee": 2_107}


def seed(db: Session, festival_id: str, run_tag: str) -> dict:
    m = a08f.manifest(db, f"a06orch-{run_tag}")
    ids: dict[str, str] = {}
    vendors = {n: a08f.vendor(db, festival_id, n) for n in EXPECTED}
    ids.update({n: v.id for n, v in vendors.items()})
    p = f"{festival_id}"
    card = []

    def card_order(vname: str, native: str, eligible: int, tax: int) -> None:
        o = a08f.order(
            db, m, vendors[vname], f"{p}-{native}", D1, [("L1", "prepared_food", eligible, tax, 0)]
        )
        card.append(
            a08f.payment(
                db,
                m,
                o,
                f"{p}-{native}-P",
                method="card",
                custody="centrally_collected",
                amount=eligible + tax,
            )
        )

    card_order("Alpha", "O1", 10_000, 925)
    card_order("Alpha", "O2", 5_000, 463)
    card_order("Beta", "O3", 20_000, 1_850)
    card_order("Gamma", "O4", 8_000, 740)
    card_order("Epsilon", "O6", 600_000, 55_500)

    o5 = a08f.order(
        db, m, vendors["Delta"], f"{p}-O5", D1, [("L1", "prepared_food", 3_000, 278, 0)]
    )
    a08f.payment(db, m, o5, f"{p}-O5-P", method="cash", custody="vendor_retained", amount=3_278)
    db.add(
        CashCount(
            vendor_id=vendors["Delta"].id,
            business_date=D1,
            counted_cents=3_278,
            counted_at=datetime(2026, 10, 17, 6, 0, tzinfo=UTC),
            counted_by="dev fixture",
            **m.ref("cash_count:delta"),
        )
    )

    assert sum(pm.amount_cents for pm in card) == DEPOSIT["gross"]
    be = a08f.deposit(db, m, festival_id, f"{p}-DEP1", D1, DEPOSIT["gross"], DEPOSIT["fee"])
    for pm in card:
        pm.bank_entry_id = be.id

    # Every vendor has a signed agreement at the festival rate. Without one
    # the nightly close holds the vendor on a `vendor_terms_unevidenced`
    # case instead of billing the festival default at nobody's authority
    # (app/terms_resolution.py), so "no contract on file" is not the state
    # this fixture is about -- Gamma's 15%-vs-10% conflict is.
    #
    # `minimum_fee_cents=None` means "use the festival default" (per
    # models_ledger.py), so the hand-worked figures above are unchanged by
    # these rows: 15% with a $500 festival minimum, exactly as before.
    for name in EXPECTED:
        db.add(
            VendorContractTerm(
                vendor_id=vendors[name].id,
                version=1,
                commission_bps=1500,
                minimum_fee_cents=None,
                processor_fee_deductible=False,
                effective_from=date(2026, 10, 1),
                status="active",
                source_refs=[m.ref(f"{name.lower()}:signed_contract")],
            )
        )
    db.flush()

    # Gamma's conflict: the same two candidate rates A06 stage 2 would
    # propose from documents, seeded directly. `version` continues after
    # the standing terms above.
    for version, bps, kind in ((2, 1500, "signed_contract"), (3, 1000, "producer_email")):
        db.add(
            VendorContractTerm(
                vendor_id=vendors["Gamma"].id,
                version=version,
                commission_bps=bps,
                minimum_fee_cents=None,
                processor_fee_deductible=False,
                effective_from=date(2026, 10, 1),
                status="proposed",
                source_refs=[m.ref(f"gamma:{kind}")],
            )
        )
    db.flush()
    fund_cash_pool(
        db,
        festival_id=festival_id,
        additional_cents=CASH_POOL_CENTS,
        actor_id="dev-fixture-script",
        actor_capability="admin",
    )
    db.commit()
    return ids
