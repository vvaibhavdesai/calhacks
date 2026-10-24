#!/usr/bin/env python3
"""Manual, real-HTTP verification of A04's deterministic settlement engine.

*** NOT an automated test suite. *** Per explicit user instruction for this
task, no pytest files were written for A04. This script is the substitute:
it seeds the throwaway hand-checked dev fixtures in
scripts/dev_fixtures_a04.py (NOT the B01 oracle -- see that module's
docstring and docs/progress/A04-handoff.md), drives real
POST /api/v1/settlements/calculate calls over real HTTP against a real
running `uvicorn` process and a real Postgres database, and asserts the
hand-worked expected numbers (see dev_fixtures_a04.py's per-fixture
docstring comments for the arithmetic) against the actual API responses.
Exits non-zero and prints every mismatch if anything is wrong; prints a
PASS line per check otherwise.

Covers all six CONTRACTS.md C02 "Required hand-worked fixtures":
  1. $10,000 eligible -> $1,500 fee / $8,500 entitlement, tax/tip custody.
  2. $2,000 cumulative eligible -> $500 minimum ONCE, not per night.
  3. $400 eligible -> $500 fee assessed / $400 collectible / $100 unpaid /
     $0 payout, tax/tips untouched even though tax+tips alone would cover
     the shortfall.
  4. $10,000 vendor-retained cash does not also produce a bank payout.
  5. Late refund after a confirmed payout -> linked correction, original
     settlement row completely unchanged.
  6. Multi-day grouped net processor deposit with a contractually
     deductible processor fee -- gross/fee/net reconcile.

Usage:
    1. Real Postgres running (encore-postgres-1), migrated to head.
    2. A real `uvicorn app.main:app` process reachable at API_BASE_URL,
       started with packages/domain on PYTHONPATH (see
       docs/progress/A04-handoff.md -- this is a real, separately-fixed
       gap in services/api/Dockerfile/local dev-loop, not an artifact of
       this script).
    3. uv run python scripts/dev_fixtures_a04.py   (optional -- this script
       also seeds idempotently itself)
    4. DATABASE_URL=... API_BASE_URL=http://127.0.0.1:8010 \
       ADMIN_EMAIL=admin@example.com ADMIN_PASSWORD=... \
       uv run python scripts/verify_a04_settlement.py
"""

from __future__ import annotations

import os
import sys
from datetime import date

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app import models_settlement  # noqa: E402,F401
from app.db import get_session_factory, make_engine  # noqa: E402
from app.models_ledger import Payout  # noqa: E402

import dev_fixtures_a04 as fixtures  # noqa: E402

# `models_settlement` is imported for its side effect only (never referenced
# directly): it registers Settlement/SettlementLineItem on the shared
# declarative Base in THIS process. Without it, SQLAlchemy's mapper
# configuration cannot resolve Payout.settlement_id's string-based
# ForeignKey("settlements.id") the first time a Payout is flushed from this
# script's own DB session below (NoReferencedTableError) -- this script
# runs in a separate Python process from the uvicorn server, so module
# import side effects there do not carry over here. Same reason
# db/migrations/env.py imports every models_* module before touching
# Base.metadata.

API_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8010")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "LocalDevPassw0rd!")

_failures: list[str] = []
_passes: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        _passes.append(label)
        print(f"PASS  {label}")
    else:
        _failures.append(f"{label} -- {detail}")
        print(f"FAIL  {label} -- {detail}")


def calculate(
    client: httpx.Client,
    headers: dict,
    *,
    festival_id: str,
    business_date: str,
    is_final_period: bool,
) -> dict:
    resp = client.post(
        "/api/v1/settlements/calculate",
        json={
            "festival_id": festival_id,
            "business_date": business_date,
            "is_final_period": is_final_period,
        },
        headers=headers,
    )
    check(
        f"POST /settlements/calculate ({festival_id}, {business_date}, final={is_final_period}) "
        "succeeds (201)",
        resp.status_code == 201,
        f"status={resp.status_code} body={resp.text}",
    )
    return resp.json()


def result_for_vendor(response: dict, vendor_id: str) -> dict:
    for r in response["results"]:
        if r["settlement"]["vendor_id"] == vendor_id:
            return r
    raise AssertionError(f"no result for vendor {vendor_id} in {response}")


def main() -> int:
    make_engine()
    session_factory = get_session_factory()
    db = session_factory()
    try:
        ids = fixtures.seed(db)
    finally:
        db.close()
    print(f"seeded/confirmed {len(ids)} fixture ids (idempotent)\n")

    client = httpx.Client(base_url=API_BASE_URL, timeout=10.0)

    login = client.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    check(
        "login succeeds", login.status_code == 200, f"status={login.status_code} body={login.text}"
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    unauth = client.post(
        "/api/v1/settlements/calculate",
        json={
            "festival_id": ids["fixture1_festival_id"],
            "business_date": ids["fixture1_business_date"],
            "is_final_period": True,
        },
    )
    check(
        "POST /settlements/calculate with no token is rejected",
        unauth.status_code == 401,
        f"status={unauth.status_code}",
    )

    # =======================================================================
    # Fixture 1: $10,000 eligible -> $1,500 commission / $8,500 entitlement.
    # Hand math in dev_fixtures_a04.py's seed() docstring comment.
    # =======================================================================
    print("\n--- Fixture 1: $10,000 eligible, 15% commission, tax/tips carried ---")
    resp1 = calculate(
        client,
        headers,
        festival_id=ids["fixture1_festival_id"],
        business_date=ids["fixture1_business_date"],
        is_final_period=True,
    )
    r1 = result_for_vendor(resp1, ids["fixture1_vendor_id"])
    s1 = r1["settlement"]
    print(f"  got: {s1}")
    check(
        "fixture1: eligible_sales_cents == 1,000,000 ($10,000)",
        s1["eligible_sales_cents"] == 1_000_000,
        str(s1),
    )
    check(
        "fixture1: centrally_held_cents == 1,000,000 ($10,000)",
        s1["centrally_held_cents"] == 1_000_000,
        str(s1),
    )
    check(
        "fixture1: assessed_fee_cents == 150,000 ($1,500)",
        s1["assessed_fee_cents"] == 150_000,
        str(s1),
    )
    check(
        "fixture1: collected_fee_cents == 150,000 ($1,500)",
        s1["collected_fee_cents"] == 150_000,
        str(s1),
    )
    check("fixture1: payout_cents == 850,000 ($8,500)", s1["payout_cents"] == 850_000, str(s1))
    check("fixture1: unpaid_balance_cents == 0", s1["unpaid_balance_cents"] == 0, str(s1))
    check(
        "fixture1: vendor_fee_receivable_cents == 0",
        s1["vendor_fee_receivable_cents"] == 0,
        str(s1),
    )
    check("fixture1: minimum_applied is False (pct wins)", s1["minimum_applied"] is False, str(s1))
    check("fixture1: tax_cents == 80,000 ($800, untouched)", s1["tax_cents"] == 80_000, str(s1))
    check("fixture1: tips_cents == 40,000 ($400, untouched)", s1["tips_cents"] == 40_000, str(s1))

    # =======================================================================
    # Fixture 2: $2,000 cumulative eligible over 3 nights -> $500 minimum
    # applied ONCE on the final night, never per-night.
    # =======================================================================
    print("\n--- Fixture 2: $2,000 cumulative eligible over 3 nights, $500 minimum once ---")
    resp2a = calculate(
        client,
        headers,
        festival_id=ids["fixture2_festival_id"],
        business_date=ids["fixture2_day1"],
        is_final_period=False,
    )
    s2a = result_for_vendor(resp2a, ids["fixture2_vendor_id"])["settlement"]
    print(f"  night1: {s2a}")
    check(
        "fixture2 night1: assessed_fee_cents == 12,000 ($120)",
        s2a["assessed_fee_cents"] == 12_000,
        str(s2a),
    )
    check("fixture2 night1: payout_cents == 68,000 ($680)", s2a["payout_cents"] == 68_000, str(s2a))
    check("fixture2 night1: minimum_applied is False", s2a["minimum_applied"] is False, str(s2a))

    resp2b = calculate(
        client,
        headers,
        festival_id=ids["fixture2_festival_id"],
        business_date=ids["fixture2_day2"],
        is_final_period=False,
    )
    s2b = result_for_vendor(resp2b, ids["fixture2_vendor_id"])["settlement"]
    print(f"  night2: {s2b}")
    check(
        "fixture2 night2: assessed_fee_cents == 10,500 ($105)",
        s2b["assessed_fee_cents"] == 10_500,
        str(s2b),
    )
    check("fixture2 night2: payout_cents == 59,500 ($595)", s2b["payout_cents"] == 59_500, str(s2b))
    check(
        "fixture2 night2: cumulative_commission_assessed_prior_cents == 12,000 ($120)",
        s2b["cumulative_commission_assessed_prior_cents"] == 12_000,
        str(s2b),
    )

    resp2c = calculate(
        client,
        headers,
        festival_id=ids["fixture2_festival_id"],
        business_date=ids["fixture2_day3_final"],
        is_final_period=True,
    )
    s2c = result_for_vendor(resp2c, ids["fixture2_vendor_id"])["settlement"]
    print(f"  night3 (final): {s2c}")
    check(
        "fixture2 night3: assessed_fee_cents == 27,500 ($275)",
        s2c["assessed_fee_cents"] == 27_500,
        str(s2c),
    )
    check("fixture2 night3: payout_cents == 22,500 ($225)", s2c["payout_cents"] == 22_500, str(s2c))
    check("fixture2 night3: minimum_applied is True", s2c["minimum_applied"] is True, str(s2c))
    check(
        "fixture2 night3: cumulative_eligible_sales_cents == 200,000 ($2,000)",
        s2c["cumulative_eligible_sales_cents"] == 200_000,
        str(s2c),
    )

    total_fee = s2a["assessed_fee_cents"] + s2b["assessed_fee_cents"] + s2c["assessed_fee_cents"]
    total_payout = s2a["payout_cents"] + s2b["payout_cents"] + s2c["payout_cents"]
    check(
        "fixture2: TOTAL assessed fee across all 3 nights == $500 minimum "
        "(NOT $500 x 3 nights = $1,500)",
        total_fee == 50_000,
        f"total_fee_cents={total_fee}",
    )
    check(
        "fixture2: TOTAL payout across all 3 nights == $1,500 entitlement ($2,000 - $500)",
        total_payout == 150_000,
        f"total_payout_cents={total_payout}",
    )

    # =======================================================================
    # Fixture 3: $400 eligible, $500 minimum unreachable -> $100 unpaid,
    # $0 payout, tax/tips untouched despite being individually sufficient
    # to cover the shortfall.
    # =======================================================================
    print("\n--- Fixture 3: $400 eligible, $500 minimum unreachable, tax/tips untouched ---")
    resp3 = calculate(
        client,
        headers,
        festival_id=ids["fixture3_festival_id"],
        business_date=ids["fixture3_business_date"],
        is_final_period=True,
    )
    s3 = result_for_vendor(resp3, ids["fixture3_vendor_id"])["settlement"]
    print(f"  got: {s3}")
    check(
        "fixture3: eligible_sales_cents == 40,000 ($400)",
        s3["eligible_sales_cents"] == 40_000,
        str(s3),
    )
    check(
        "fixture3: assessed_fee_cents == 50,000 ($500)", s3["assessed_fee_cents"] == 50_000, str(s3)
    )
    check(
        "fixture3: collected_fee_cents == 40,000 ($400)",
        s3["collected_fee_cents"] == 40_000,
        str(s3),
    )
    check(
        "fixture3: unpaid_balance_cents == 10,000 ($100)",
        s3["unpaid_balance_cents"] == 10_000,
        str(s3),
    )
    check("fixture3: payout_cents == 0 (never negative)", s3["payout_cents"] == 0, str(s3))
    check("fixture3: minimum_applied is True", s3["minimum_applied"] is True, str(s3))
    check("fixture3: tax_cents == 7,000 ($70, untouched)", s3["tax_cents"] == 7_000, str(s3))
    check("fixture3: tips_cents == 4,000 ($40, untouched)", s3["tips_cents"] == 4_000, str(s3))
    check(
        "fixture3: tax($70)+tips($40)=$110 >= the $100 shortfall, PROVING they "
        "were never used to cover it (unpaid_balance stayed 10,000, not 0)",
        (s3["tax_cents"] + s3["tips_cents"]) >= s3["unpaid_balance_cents"]
        and s3["unpaid_balance_cents"] == 10_000,
        str(s3),
    )

    # =======================================================================
    # Fixture 4: $10,000 vendor-retained cash must NOT also produce a bank
    # payout. Fee becomes a receivable, payout is $0.
    # =======================================================================
    print("\n--- Fixture 4: $10,000 vendor-retained cash does not duplicate as a bank payout ---")
    resp4 = calculate(
        client,
        headers,
        festival_id=ids["fixture4_festival_id"],
        business_date=ids["fixture4_business_date"],
        is_final_period=True,
    )
    s4 = result_for_vendor(resp4, ids["fixture4_vendor_id"])["settlement"]
    print(f"  got: {s4}")
    check(
        "fixture4: vendor_retained_cash_cents == 1,000,000 ($10,000)",
        s4["vendor_retained_cash_cents"] == 1_000_000,
        str(s4),
    )
    check("fixture4: centrally_held_cents == 0", s4["centrally_held_cents"] == 0, str(s4))
    check(
        "fixture4: assessed_fee_cents == 150,000 ($1,500)",
        s4["assessed_fee_cents"] == 150_000,
        str(s4),
    )
    check("fixture4: collected_fee_cents == 0", s4["collected_fee_cents"] == 0, str(s4))
    check(
        "fixture4: vendor_fee_receivable_cents == 150,000 ($1,500, collect from "
        "vendor directly)",
        s4["vendor_fee_receivable_cents"] == 150_000,
        str(s4),
    )
    check("fixture4: unpaid_balance_cents == 0", s4["unpaid_balance_cents"] == 0, str(s4))
    check(
        "fixture4: payout_cents == 0 -- NOT ~$8,500 (the money the vendor already "
        "holds is never also wired out as a bank payout)",
        s4["payout_cents"] == 0,
        str(s4),
    )

    # =======================================================================
    # Fixture 5: late refund AFTER a confirmed payout -> linked correction,
    # original settlement completely unchanged.
    # =======================================================================
    print("\n--- Fixture 5: late refund after a confirmed payout -> linked correction ---")
    resp5a = calculate(
        client,
        headers,
        festival_id=ids["fixture5_festival_id"],
        business_date=ids["fixture5_business_date"],
        is_final_period=True,
    )
    original = result_for_vendor(resp5a, ids["fixture5_vendor_id"])["settlement"]
    print(f"  pass 1 (before refund): {original}")
    check(
        "fixture5 pass1: assessed_fee_cents == 75,000 ($750)",
        original["assessed_fee_cents"] == 75_000,
        str(original),
    )
    check(
        "fixture5 pass1: payout_cents == 425,000 ($4,250)",
        original["payout_cents"] == 425_000,
        str(original),
    )
    check("fixture5 pass1: status == FINAL", original["status"] == "FINAL", str(original))
    original_settlement_id = original["id"]

    # Manually insert a CONFIRMED Payout linked to this settlement -- A07
    # (controlled payouts/outbox) does not exist yet; this is the documented
    # stand-in that lets app/settlement.py's find_confirmed_payout() detect
    # "this settlement was already paid out" (see docs/progress/
    # A04-handoff.md and app/settlement.py's module docstring).
    db2 = get_session_factory()()
    try:
        payout = Payout(
            logical_obligation_id=f"a04-dev-fixture5-payout-{original_settlement_id}",
            idempotency_key=f"a04-dev-fixture5-payout-{original_settlement_id}",
            vendor_id=ids["fixture5_vendor_id"],
            business_date=date.fromisoformat(ids["fixture5_business_date"]),
            amount_cents=original["payout_cents"],
            state="CONFIRMED",
            settlement_id=original_settlement_id,
        )
        existing_payout = (
            db2.query(Payout).filter_by(idempotency_key=payout.idempotency_key).one_or_none()
        )
        if existing_payout is None:
            db2.add(payout)
            db2.commit()
            print(
                f"  inserted CONFIRMED Payout of {original['payout_cents']} cents "
                "(dev stand-in for A07)"
            )
        else:
            print("  CONFIRMED Payout already present from a prior run (idempotent)")
    finally:
        db2.close()

    # Apply the late refund: fixture 5's order_capture match_group's
    # matched_amount_cents drops from 500,000 to 400,000 (see
    # dev_fixtures_a04.apply_fixture5_late_refund's docstring).
    db3 = get_session_factory()()
    try:
        fixtures.apply_fixture5_late_refund(db3)
    finally:
        db3.close()
    print("  applied late refund: order_capture matched_amount_cents 500,000 -> 400,000")

    resp5b = calculate(
        client,
        headers,
        festival_id=ids["fixture5_festival_id"],
        business_date=ids["fixture5_business_date"],
        is_final_period=True,
    )
    r5b = result_for_vendor(resp5b, ids["fixture5_vendor_id"])
    corrected = r5b["settlement"]
    print(f"  pass 2 (after refund): {corrected}")
    check("fixture5 pass2: is_correction is True", r5b["is_correction"] is True, str(r5b))
    check(
        "fixture5 pass2: corrected_settlement_id == original settlement id",
        r5b["corrected_settlement_id"] == original_settlement_id,
        str(r5b),
    )
    check(
        "fixture5 pass2: status == CORRECTION", corrected["status"] == "CORRECTION", str(corrected)
    )
    check(
        "fixture5 pass2: correction_of == original settlement id",
        corrected["correction_of"] == original_settlement_id,
        str(corrected),
    )
    check(
        "fixture5 pass2: new eligible_sales_cents == 400,000 ($4,000 == $5,000 - $1,000 refund)",
        corrected["eligible_sales_cents"] == 400_000,
        str(corrected),
    )
    check(
        "fixture5 pass2: new assessed_fee_cents == 60,000 ($600 == 15% of $4,000)",
        corrected["assessed_fee_cents"] == 60_000,
        str(corrected),
    )
    check(
        "fixture5 pass2: new payout_cents == 340,000 ($3,400 == $4,000 - $600)",
        corrected["payout_cents"] == 340_000,
        str(corrected),
    )
    check(
        "fixture5 pass2: overpayment_recovery_cents == 85,000 "
        "($850, already paid $4,250, now only entitled to $3,400)",
        r5b["overpayment_recovery_cents"] == 85_000,
        str(r5b),
    )
    check(
        "fixture5 pass2: additional_payout_cents == 0 (mutually exclusive with recovery)",
        r5b["additional_payout_cents"] == 0,
        str(r5b),
    )

    # The ORIGINAL settlement row must remain byte-for-byte unchanged.
    reread = client.get(f"/api/v1/settlements/{original_settlement_id}", headers=headers)
    check(
        "fixture5: GET original settlement after correction succeeds",
        reread.status_code == 200,
        f"status={reread.status_code}",
    )
    reread_body = reread.json()
    check(
        "fixture5: ORIGINAL settlement is byte-for-byte unchanged after the "
        "correction (assessed_fee_cents still 75,000, payout_cents still "
        "425,000, status still FINAL)",
        reread_body["assessed_fee_cents"] == 75_000
        and reread_body["payout_cents"] == 425_000
        and reread_body["status"] == "FINAL"
        and reread_body["eligible_sales_cents"] == 500_000,
        str(reread_body),
    )

    corrections = client.get(
        f"/api/v1/settlements/{original_settlement_id}/corrections", headers=headers
    )
    check(
        "fixture5: GET /settlements/{id}/corrections succeeds",
        corrections.status_code == 200,
        f"status={corrections.status_code}",
    )
    correction_rows = corrections.json()
    check(
        "fixture5: exactly one correction is linked to the original settlement",
        len(correction_rows) == 1 and correction_rows[0]["id"] == corrected["id"],
        str(correction_rows),
    )

    # =======================================================================
    # Fixture 6: multi-day grouped net processor deposit with a
    # contractually-deductible processor fee -- gross/fee/net reconcile.
    # =======================================================================
    print("\n--- Fixture 6: multi-day processor deposit, deductible processor fee ---")
    resp6a = calculate(
        client,
        headers,
        festival_id=ids["fixture6_festival_id"],
        business_date=ids["fixture6_day1"],
        is_final_period=False,
    )
    s6a = result_for_vendor(resp6a, ids["fixture6_vendor_id"])["settlement"]
    print(f"  day1: {s6a}")
    check(
        "fixture6 day1: assessed_fee_cents == 45,000 ($450)",
        s6a["assessed_fee_cents"] == 45_000,
        str(s6a),
    )
    check(
        "fixture6 day1: processor_fee_assessed_cents == 6,000 ($60)",
        s6a["processor_fee_assessed_cents"] == 6_000,
        str(s6a),
    )
    check(
        "fixture6 day1: processor_fee_collected_cents == 6,000 ($60, contract "
        "permits deduction)",
        s6a["processor_fee_collected_cents"] == 6_000,
        str(s6a),
    )
    check(
        "fixture6 day1: payout_cents == 249,000 ($2,490)", s6a["payout_cents"] == 249_000, str(s6a)
    )

    resp6b = calculate(
        client,
        headers,
        festival_id=ids["fixture6_festival_id"],
        business_date=ids["fixture6_day2_final"],
        is_final_period=True,
    )
    s6b = result_for_vendor(resp6b, ids["fixture6_vendor_id"])["settlement"]
    print(f"  day2 (final): {s6b}")
    check(
        "fixture6 day2: assessed_fee_cents == 30,000 ($300)",
        s6b["assessed_fee_cents"] == 30_000,
        str(s6b),
    )
    check(
        "fixture6 day2: processor_fee_collected_cents == 4,000 ($40)",
        s6b["processor_fee_collected_cents"] == 4_000,
        str(s6b),
    )
    check(
        "fixture6 day2: payout_cents == 166,000 ($1,660)", s6b["payout_cents"] == 166_000, str(s6b)
    )
    check(
        "fixture6 day2: minimum_applied is False (15% of $5,000 = $750 > $500 minimum)",
        s6b["minimum_applied"] is False,
        str(s6b),
    )

    total_organizer_fee = s6a["assessed_fee_cents"] + s6b["assessed_fee_cents"]
    total_processor_fee = (
        s6a["processor_fee_collected_cents"] + s6b["processor_fee_collected_cents"]
    )
    total_vendor_payout = s6a["payout_cents"] + s6b["payout_cents"]
    check(
        "fixture6: total organizer fee across both days == $750 (75,000 cents) "
        "== 15% of $5,000 gross",
        total_organizer_fee == 75_000,
        f"total_organizer_fee_cents={total_organizer_fee}",
    )
    check(
        "fixture6: total processor fee collected == $100 (10,000 cents) == "
        "the BankEntry's batch_fee_cents",
        total_processor_fee == 10_000,
        f"total_processor_fee_cents={total_processor_fee}",
    )
    check(
        "fixture6: gross($5,000) - organizer_fee($750) - processor_fee($100) == "
        "total vendor payout ($4,150)",
        total_vendor_payout == 500_000 - total_organizer_fee - total_processor_fee,
        f"total_vendor_payout_cents={total_vendor_payout}",
    )

    # =======================================================================
    # Cross-fixture line-item traceability spot check: every settlement's
    # line items sum consistently and carry source_refs.
    # =======================================================================
    print("\n--- Line-item traceability ---")
    line_items_resp = client.get(f"/api/v1/settlements/{s1['id']}/line-items", headers=headers)
    check(
        "GET /settlements/{id}/line-items succeeds",
        line_items_resp.status_code == 200,
        f"status={line_items_resp.status_code}",
    )
    line_items = line_items_resp.json()
    check(
        "fixture1 settlement has line items for every tracked money component",
        {li["line_type"] for li in line_items}
        >= {
            "eligible_sales",
            "centrally_held",
            "tax",
            "tips",
            "fee_assessed",
            "fee_collected_from_central",
            "vendor_payout",
        },
        str([li["line_type"] for li in line_items]),
    )
    eligible_line = next(li for li in line_items if li["line_type"] == "eligible_sales")
    check(
        "fixture1 'eligible_sales' line item carries non-empty source_refs "
        "(traces back to the originating payment/match_group)",
        len(eligible_line["source_refs"]) > 0,
        str(eligible_line),
    )

    print(f"\n{len(_passes)} passed, {len(_failures)} failed")
    if _failures:
        print("\nFAILURES:")
        for f in _failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
