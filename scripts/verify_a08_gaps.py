#!/usr/bin/env python3
"""Manual, real-HTTP verification of the A08 gap closure: cash-office
deposits, cash count variance, unmatched bank entries and chargeback fees.

*** NOT an automated test suite, and the fixture is NOT B01. *** Same
convention as scripts/verify_a08_finance.py: a small, hand-checked
scenario written by the same author as the code under test. Operational
inputs A08 reads but does not own (vendors, orders, payments, bank entries,
vendor contract terms) are inserted directly; everything A08 owns is then
driven over real HTTP against a real `uvicorn` and Postgres. Each run uses
its own festival id.

Scenario (cents; all lines packaged_food, tax 0, so every number below is
plain arithmetic):

  D1 = 2026-10-16. Centrally collected CASH: A 10000 + 5000 = 15000,
  B 8000, C 4000. Card: E 9156 (charged back) + 2000, F 5000 (charged
  back) + 1000. D2: A cash 3000.

  Cash office D1: counts A 15000 (matches), B 7600 (400 SHORT); no count
  for C. Slip CASHDEP-D1: bags A 15000, B 7600, one UNLABELLED bag 4000
  (total 26600). Bank credit 26600 lands on D2, AFTER the slip is recorded.
  Cash office D2: count A 3000; slip CASHDEP-D2 3000, bank credits only
  2950 (50 short at the bank).

  Bank: DEP-1 credit 16656 (gross 17156, fee 500) D2; DEP-CB debit 15656
  (gross -14156, fee 1500 = E's chargeback fee) D3; EXP debit 15000 D2
  (expense); ACH debit 30000 D2 (supplier payment); BANKFEE debit 8500 D3
  (nothing explains it); LATE debit 2000 D3 (explained only after the
  first scan).

  Contract terms: E permits processing-fee deduction; F does not (until a
  term is activated mid-run).

Hand-worked ending balances (debit-positive):
  CASH_OFFICE 30000 - 26600 - 2950 - 400 - 50 = 0
  CASH_OVER_SHORT 400 + 50 = 450 (only after two finance-lead decisions)
  SUSPENSE 0 (8500 reclassified; 2000 reversed once explained)
  OPERATING_EXPENSE 15000 + 2000 + 8500 = 25500
  PROCESSOR_FEE_EXPENSE 500 + 1500 + 1500 (F accrual) - 1500 (E) - 1500 (F) = 500
  PROCESSOR_CLEARING 3000 - 17156 + 14156 - 1500 = -1500 (F's fee owed to processor)
  VENDOR_FUNDS_PAYABLE -(33000 - 3000) = -30000
  BANK_OPERATING 1000000 + 16656 - 15656 + 26600 + 2950 - 8500 - 15000
                 - 30000 - 2000 = 975050

Usage (API at API_BASE_URL, DB migrated to head, seed_admin.py run):
    DATABASE_URL=... API_BASE_URL=http://127.0.0.1:8051 \\
    ADMIN_EMAIL=... ADMIN_PASSWORD=... ENCORE_APP_DSN=postgresql://encore_app:...@.../db \\
      uv run python scripts/verify_a08_gaps.py
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import UTC, date, datetime

import httpx
import psycopg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app.auth import hash_password  # noqa: E402
from app.db import get_session_factory, make_engine  # noqa: E402
from app.models import CAPABILITY_CLOSE_BOOKS, Capability, User, UserCapability  # noqa: E402
from app.models_ledger import BusinessEvent, VendorContractTerm  # noqa: E402
from app.models_reconciliation import BankEntry  # noqa: E402
from sqlalchemy import select  # noqa: E402

import dev_fixtures_a08 as fx  # noqa: E402
from verify_a08_finance import (  # noqa: E402  # noqa: E402
    Api,
    _failures,
    _passes,
    check,
    code_of,
    concurrently,
)

API_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8010")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "LocalDevPassw0rd!")
APP_ROLE_DSN = os.environ.get(
    "ENCORE_APP_DSN", "postgresql://encore_app:encore_app_dev_password@localhost:5432/encore"
)
RUN = uuid.uuid4().hex[:8]
FEST = f"gcmf-2026-a08gaps-{RUN}"
D0, D1, D2, D3 = date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 17), date(2026, 10, 18)
LIMITED_PASSWORD = "LimitedPassw0rd!"


def login(email: str, password: str) -> dict:
    r = httpx.post(f"{API_BASE_URL}/api/v1/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def bank(db, m, ref: str, d: date, direction: str, amount: int, gross=None, fee=None):
    be = BankEntry(
        festival_id=FEST,
        account_id="operating-001",
        provider_reference=ref,
        direction=direction,
        amount_cents=amount,
        batch_gross_cents=gross,
        batch_fee_cents=fee,
        settlement_status="settled",
        business_date=d,
        occurred_at=datetime(d.year, d.month, d.day, 18, 0, tzinfo=UTC),
        **m.ref(f"bank:{ref}"),
    )
    db.add(be)
    db.flush()
    return be.id


def term(db, vendor_id: str, *, deductible: bool, version: int = 1) -> None:
    db.add(
        VendorContractTerm(
            vendor_id=vendor_id,
            version=version,
            processor_fee_deductible=deductible,
            effective_from=D0,
            status="active",
            source_refs=[{"note": "hand-checked dev fixture, not B01"}],
        )
    )


def seed(db) -> dict:
    m = fx.manifest(db, f"gaps-{RUN}")
    ids: dict = {"m": m}
    for key in ("A", "B", "C", "E", "F"):
        ids[key] = fx.vendor(db, FEST, f"{key}-vendor-{RUN}").id

    def sale(key, native, d, amount, method, custody="centrally_collected"):
        v = db.get(fx.Vendor, ids[key])
        o = fx.order(db, m, v, f"{native}-{RUN}", d, [("L1", "packaged_food", amount, 0, 0)])
        p = fx.payment(db, m, o, f"{native}-{RUN}-p", method=method, custody=custody, amount=amount)
        return o, p

    sale("A", "OA1", D1, 10_000, "cash")
    sale("A", "OA2", D1, 5_000, "cash")
    sale("B", "OB1", D1, 8_000, "cash")
    sale("C", "OC1", D1, 4_000, "cash")
    oe1, pe1 = sale("E", "OE1", D1, 9_156, "card")
    sale("E", "OE2", D1, 2_000, "card")
    of1, pf1 = sale("F", "OF1", D1, 5_000, "card")
    sale("F", "OF2", D1, 1_000, "card")
    sale("A", "OA3", D2, 3_000, "cash")
    ids["pe1_capture"] = pe1.id
    ids["cb_e"] = fx.payment(
        db,
        m,
        oe1,
        f"CB-E-{RUN}",
        method="card",
        custody="centrally_collected",
        amount=9_156,
        kind="chargeback",
    ).id
    ids["cb_f"] = fx.payment(
        db,
        m,
        of1,
        f"CB-F-{RUN}",
        method="card",
        custody="centrally_collected",
        amount=5_000,
        kind="chargeback",
    ).id
    term(db, ids["E"], deductible=True)

    ids["dep1"] = bank(db, m, f"DEP-1-{RUN}", D2, "credit", 16_656, 17_156, 500)
    ids["depcb"] = bank(db, m, f"DEP-CB-{RUN}", D3, "debit", 15_656, -14_156, 1_500)
    ids["exp"] = bank(db, m, f"EXP-{RUN}", D2, "debit", 15_000)
    ids["ach"] = bank(db, m, f"ACH-{RUN}", D2, "debit", 30_000)
    ids["bankfee"] = bank(db, m, f"BANKFEE-{RUN}", D3, "debit", 8_500)
    ids["late"] = bank(db, m, f"LATE-{RUN}", D3, "debit", 2_000)
    ids["cashdep2"] = bank(db, m, f"CASHDEP-D2-{RUN}", D3, "credit", 2_950)

    email = f"a08gaps-closer-{RUN}@example.com"
    u = User(email=email, hashed_password=hash_password(LIMITED_PASSWORD))
    db.add(u)
    db.flush()
    cap = db.execute(
        select(Capability).where(Capability.code == CAPABILITY_CLOSE_BOOKS)
    ).scalar_one()
    db.add(UserCapability(user_id=u.id, capability_id=cap.id, granted_by=None))
    ids["limited_email"] = email
    db.commit()
    return ids


def role_bal(api: Api, role: str) -> int:
    tb = api.ok("get", "/journal/trial-balance", festival_id=FEST)
    row = next(r for r in tb["rows"] if r["account_role"] == role)
    return row["debit_cents"] - row["credit_cents"]


def entries(api: Api, prefix: str) -> list[dict]:
    items, cursor = [], None
    while True:
        extra = {"cursor": cursor} if cursor else {}
        page = api.ok("get", "/journal", festival_id=FEST, **extra)
        items += page["items"]
        cursor = page["next_cursor"]
        if not cursor:
            return [e for e in items if e["entry_key"].startswith(prefix)]


def lines_of(e: dict) -> dict:
    return {ln["account_role"]: (ln["debit_cents"], ln["credit_cents"]) for ln in e["lines"]}


def main() -> int:  # noqa: C901 -- linear acceptance walkthrough
    make_engine()
    db = get_session_factory()()
    ids = seed(db)
    api = Api(login(ADMIN_EMAIL, ADMIN_PASSWORD))
    limited = Api(login(ids["limited_email"], LIMITED_PASSWORD))
    print(f"verify_a08_gaps: run={RUN} festival={FEST}")

    api.ok(
        "post",
        "/finance/opening-balances",
        {"festival_id": FEST, "as_of": str(D0), "amount_cents": 1_000_000},
    )
    api.ok(
        "post",
        "/finance/expenses",
        {
            "festival_id": FEST,
            "expense_reference": f"EXP-{RUN}",
            "category": "security",
            "amount_cents": 15_000,
            "business_date": str(D2),
            "bank_entry_id": ids["exp"],
        },
    )
    api.ok("post", "/cash-pool/fund", {"festival_id": FEST, "additional_cents": 100_000})
    sup = api.ok("post", "/suppliers", {"festival_id": FEST, "name": f"Ice {RUN}"})
    inv = api.ok(
        "post",
        "/invoices",
        {
            "supplier_id": sup["id"],
            "invoice_number": f"INV-{RUN}",
            "invoice_date": str(D2),
            "due_date": "2026-11-15",
            "net_cents": 30_000,
            "gross_cents": 30_000,
        },
    )["invoice"]
    r = api.post(
        f"/invoices/{inv['id']}/pay",
        {"amount_cents": 30_000, "business_date": str(D2), "bank_entry_id": "nope"},
        **{"Idempotency-Key": f"gaps-bad-{RUN}"},
    )
    check("pay with unknown bank_entry_id -> 404, nothing paid", r.status_code == 404, r.text)
    api.ok(
        "post",
        f"/invoices/{inv['id']}/pay",
        {"amount_cents": 30_000, "business_date": str(D2), "bank_entry_id": ids["ach"]},
        **{"Idempotency-Key": f"gaps-pay-{RUN}"},
    )
    pay_entry = entries(api, "SUPPLIER_PAYMENT:")[0]
    check(
        "supplier payment journal cites its bank debit (so the scan treats it as explained)",
        ids["ach"] in pay_entry["source_record_ids"],
        pay_entry["source_record_ids"],
    )
    for d in (D1, D2, D3):
        api.ok("post", "/journal/post-day", {"festival_id": FEST, "business_date": str(d)})
    check(
        "sales: central cash 30000 sits in CASH_OFFICE before any deposit",
        role_bal(api, "CASH_OFFICE") == 30_000,
        role_bal(api, "CASH_OFFICE"),
    )

    # --- 1. counts + slip, bank credit not landed yet ---------------------
    def count(key, bag, d, cents):
        return api.post(
            "/finance/cash-office/counts",
            {
                "festival_id": FEST,
                "vendor_id": ids[key],
                "business_date": str(d),
                "bag_reference": f"{bag}-{RUN}",
                "counted_cents": cents,
                "counted_by": "organizer cash office",
                "source_refs": [ids["m"].ref(f"count:{bag}")],
            },
        )

    ca = count("A", "BAG-A-D1", D1, 15_000)
    cb = count("B", "BAG-B-D1", D1, 7_600)
    check("count A recorded (201)", ca.status_code == 201 and ca.json()["created"], ca.text)
    again = count("B", "BAG-B-D1", D1, 7_600)
    check(
        "count retry is idempotent (same row, created=false)",
        not again.json()["created"] and again.json()["count"]["id"] == cb.json()["count"]["id"],
        again.text,
    )
    r = count("B", "BAG-B-D1", D1, 7_700)
    check(
        "count with same bag, different amount -> 409",
        r.status_code == 409 and code_of(r) == "cash_count_conflict",
        r.text,
    )
    ca_id, cb_id = ca.json()["count"]["id"], cb.json()["count"]["id"]
    slip_body = {
        "festival_id": FEST,
        "slip_reference": f"CASHDEP-D1-{RUN}",
        "business_date": str(D1),
        "total_cents": 26_600,
        "bags": [
            {"bag_reference": f"BAG-A-D1-{RUN}", "amount_cents": 15_000, "cash_count_id": ca_id},
            {"bag_reference": f"BAG-B-D1-{RUN}", "amount_cents": 7_600, "cash_count_id": cb_id},
            {"bag_reference": f"UNLABELLED-{RUN}", "amount_cents": 4_000, "cash_count_id": None},
        ],
        "source_refs": [ids["m"].ref("slip:CASHDEP-D1")],
    }
    results = concurrently([lambda: api.post("/finance/cash-office/deposit-slips", slip_body)] * 4)
    created = [r.json()["created"] for r in results if r.status_code == 201]
    check(
        "4 concurrent identical slip records: all 201, exactly one created",
        len(created) == 4 and created.count(True) == 1,
        [(r.status_code, r.text[:100]) for r in results],
    )
    slip1 = results[0].json()
    check(
        "slip D1: bank credit not landed -> AWAITING_BANK_CREDIT, unlabelled bag counted",
        slip1["posting"]["status"] == "AWAITING_BANK_CREDIT"
        and slip1["check_status"] == "AWAITING_BANK_CREDIT"
        and slip1["unlabelled_bag_count"] == 1
        and slip1["bag_total_cents"] == 26_600,
        slip1,
    )
    r = api.post(
        "/finance/cash-office/deposit-slips",
        {
            **slip_body,
            "slip_reference": f"OTHER-{RUN}",
            "bags": slip_body["bags"][:1],
            "total_cents": 15_000,
        },
    )
    check(
        "a count already on a slip cannot be deposited again -> 409",
        r.status_code == 409 and code_of(r) == "cash_count_already_deposited",
        r.text,
    )
    check(
        "cash office unchanged until the bank credit lands", role_bal(api, "CASH_OFFICE") == 30_000
    )

    # --- 2. variance reconciliation --------------------------------------
    rec = api.ok(
        "post", "/finance/cash-office/reconcile", {"festival_id": FEST, "business_date": str(D1)}
    )
    by_v = {v["vendor_id"]: v for v in rec["vendors"]}
    check(
        "reconcile D1: A matched (15000 vs 15000)",
        by_v[ids["A"]]["status"] == "MATCHED" and by_v[ids["A"]]["case_id"] is None,
        by_v.get(ids["A"]),
    )
    check(
        "reconcile D1: B counted 7600 vs POS 8000 -> variance -400 with a case",
        by_v[ids["B"]]["variance_cents"] == -400 and by_v[ids["B"]]["case_id"],
        by_v.get(ids["B"]),
    )
    check(
        "reconcile D1: C has POS cash 4000 but no count -> MISSING_CASH_COUNT case",
        by_v[ids["C"]]["status"] == "MISSING_CASH_COUNT" and by_v[ids["C"]]["case_id"],
        by_v.get(ids["C"]),
    )
    check(
        "card-only vendors are not in the cash reconciliation",
        ids["E"] not in by_v and ids["F"] not in by_v,
    )
    rec2 = api.ok(
        "post", "/finance/cash-office/reconcile", {"festival_id": FEST, "business_date": str(D1)}
    )
    check(
        "reconcile re-run: same cases, none duplicated",
        sorted(rec2["open_case_ids"]) == sorted(rec["open_case_ids"])
        and len(rec["open_case_ids"]) == 2,
        (rec["open_case_ids"], rec2["open_case_ids"]),
    )
    cases = {
        c["case_id"]: c for c in api.ok("get", "/finance/cash-cases", festival_id=FEST)["items"]
    }
    var_case = cases[by_v[ids["B"]]["case_id"]]
    miss_case = cases[by_v[ids["C"]]["case_id"]]
    check(
        "CASH_VARIANCE case is vendor-scoped to B (holds only B), impact -400, finance_lead",
        var_case["reason_code"] == "CASH_VARIANCE"
        and var_case["vendor_id"] == ids["B"]
        and var_case["financial_impact_cents"] == -400
        and var_case["required_capability"] == "finance_lead",
        var_case,
    )
    open_a = api.ok("get", "/cases", vendor_id=ids["A"], status_="OPEN")
    check("no open case for vendor A (unrelated vendors are not held)", open_a == [], open_a)
    from app.models_ledger import Case  # noqa: E402

    mc = db.get(Case, miss_case["case_id"])
    check(
        "MISSING_CASH_COUNT for C cites the unlabelled bag as candidate evidence",
        mc.vendor_id == ids["C"]
        and any(r.get("role") == "candidate_unlabelled_bag" for r in mc.evidence_refs),
        mc.evidence_refs,
    )
    check(
        "no festival-level bag case when a vendor/night explains the bag",
        rec["unlabelled_bag_case_ids"] == [],
        rec["unlabelled_bag_case_ids"],
    )

    # --- close D1 (warning only for open cash cases) ----------------------
    close1 = api.ok("post", "/close", {"festival_id": FEST, "business_date": str(D1)})
    check(
        "close D1: hard checks pass; warning flags the open cash cases",
        close1["created"]
        and close1["checks"]["hard"]["bank_entries_accounted_for"]
        and close1["checks"]["hard"]["cash_deposits_posted"]
        and close1["checks"]["warnings"]["cash_office_and_bank_cases_resolved"] is False,
        close1["checks"],
    )

    # --- 1b. bank credit lands ---------------------------------------------
    ids["cashdep1"] = bank(db, ids["m"], f"CASHDEP-D1-{RUN}", D2, "credit", 26_600)
    db.commit()
    chk = api.ok("get", "/close/checks", festival_id=FEST, business_date=str(D2))
    check(
        "D2 close checks: landed-but-unposted deposit fails cash_deposits_posted (hard)",
        chk["hard"]["cash_deposits_posted"] is False
        and chk["unposted_cash_deposit_slip_ids"] == [slip1["slip_id"]],
        {k: chk[k] for k in ("hard", "unposted_cash_deposit_slip_ids")},
    )
    pd = api.ok("post", "/journal/post-day", {"festival_id": FEST, "business_date": str(D2)})
    check(
        "post-day D2 posts CASHDEP for the slip",
        f"CASHDEP:{slip1['slip_id']}" in pd["posted"],
        pd["posted"],
    )
    dep = entries(api, f"CASHDEP:{slip1['slip_id']}")[0]
    check(
        "CASHDEP: Dr BANK_OPERATING 26600 / Cr CASH_OFFICE 26600 on the bank credit's date",
        lines_of(dep) == {"BANK_OPERATING": (26_600, 0), "CASH_OFFICE": (0, 26_600)}
        and dep["business_date"] == str(D2),
        dep,
    )
    drill = api.ok("get", f"/journal/{dep['id']}")
    check(
        "CASHDEP drill-down resolves to the bank entry and the deposit slip",
        {s["record_kind"] for s in drill["sources"]} == {"bank_entry", "cash_deposit_slip"},
        drill["sources"],
    )
    again = api.ok("post", "/journal/post-day", {"festival_id": FEST, "business_date": str(D2)})
    check("post-day D2 re-run posts nothing new", again["posted"] == [], again["posted"])
    slip_view = api.ok("get", f"/finance/cash-office/deposit-slips/{slip1['slip_id']}")
    check(
        "slip D1 now POSTED; still CASE_REQUIRED because of the unlabelled bag",
        slip_view["posting"]["status"] == "POSTED" and slip_view["check_status"] == "CASE_REQUIRED",
        slip_view,
    )

    # --- D2: slip whose bank credit is short -------------------------------
    ca2 = count("A", "BAG-A-D2", D2, 3_000).json()["count"]["id"]
    s2 = api.ok(
        "post",
        "/finance/cash-office/deposit-slips",
        {
            "festival_id": FEST,
            "slip_reference": f"CASHDEP-D2-{RUN}",
            "business_date": str(D2),
            "total_cents": 3_000,
            "bank_entry_id": ids["cashdep2"],
            "bags": [
                {"bag_reference": f"BAG-A-D2-{RUN}", "amount_cents": 3_000, "cash_count_id": ca2}
            ],
        },
    )
    check(
        "slip D2 with a settled bank credit posts immediately for what the bank credited (2950)",
        s2["posting"]["status"] == "POSTED" and s2["check_status"] == "CASE_REQUIRED",
        s2,
    )
    rec_d2 = api.ok(
        "post", "/finance/cash-office/reconcile", {"festival_id": FEST, "business_date": str(D2)}
    )
    slip_row = rec_d2["slips"][0]
    check(
        "reconcile D2: count matches POS; bank 2950 vs bags 3000 -> CASH_DEPOSIT_VARIANCE -50",
        rec_d2["vendors"][0]["status"] == "MATCHED"
        and slip_row["bank_variance_cents"] == -50
        and slip_row["case_id"],
        rec_d2,
    )
    dep_case_id = slip_row["case_id"]
    check(
        "cash office holds exactly the two unexplained differences (400 + 50)",
        role_bal(api, "CASH_OFFICE") == 450,
        role_bal(api, "CASH_OFFICE"),
    )

    # --- 3. unmatched bank entries -----------------------------------------
    r = api.post("/close", {"festival_id": FEST, "business_date": str(D3)})
    det = r.json().get("detail", {})
    check(
        "close D3 before the scan is refused: bank_entries_accounted_for fails",
        r.status_code == 422 and "bank_entries_accounted_for" in det.get("failing", []),
        r.text[:300],
    )
    scans = concurrently(
        [lambda: api.post("/finance/bank-entries/scan", {"festival_id": FEST})] * 4
    )
    check(
        "4 concurrent scans: all 200",
        all(s.status_code == 200 for s in scans),
        [(s.status_code, s.text[:120]) for s in scans],
    )
    rows = {
        r["bank_entry_id"]: r
        for r in api.ok("post", "/finance/bank-entries/scan", {"festival_id": FEST})["bank_entries"]
    }
    check(
        "scan: processor deposits, cash deposits, expense and supplier payment are explained",
        all(
            rows[ids[k]]["status"] == "EXPLAINED"
            for k in ("dep1", "depcb", "cashdep1", "cashdep2", "exp", "ach")
        ),
        {
            k: rows[ids[k]]["status"]
            for k in ("dep1", "depcb", "cashdep1", "cashdep2", "exp", "ach")
        },
    )
    check(
        "scan: the bank fee and the not-yet-explained debit are IN_SUSPENSE with cases",
        rows[ids["bankfee"]]["status"] == "IN_SUSPENSE"
        and rows[ids["late"]]["status"] == "IN_SUSPENSE"
        and rows[ids["bankfee"]]["case_id"]
        and rows[ids["late"]]["case_id"],
        (rows[ids["bankfee"]], rows[ids["late"]]),
    )
    susp = entries(api, f"BANK_UNMATCHED:{ids['bankfee']}")
    check(
        "concurrent + repeated scans: exactly one suspense entry per unmatched bank entry",
        len(susp) == 1 and len(entries(api, f"BANK_UNMATCHED:{ids['late']}")) == 1,
        [e["entry_key"] for e in susp],
    )
    check(
        "suspense entry: Dr SUSPENSE 8500 / Cr BANK_OPERATING 8500",
        lines_of(susp[0]) == {"SUSPENSE": (8_500, 0), "BANK_OPERATING": (0, 8_500)},
        susp[0],
    )
    check(
        "SUSPENSE holds 10500 after the scan",
        role_bal(api, "SUSPENSE") == 10_500,
        role_bal(api, "SUSPENSE"),
    )
    late_case = rows[ids["late"]]["case_id"]
    fee_case = rows[ids["bankfee"]]["case_id"]
    check(
        "UNMATCHED_BANK_ENTRY cases are festival-level (no vendor held)",
        cases_get(api, fee_case)["vendor_id"] is None,
    )
    api.ok(
        "post",
        "/finance/expenses",
        {
            "festival_id": FEST,
            "expense_reference": f"LATE-{RUN}",
            "category": "radios",
            "amount_cents": 2_000,
            "business_date": str(D3),
            "bank_entry_id": ids["late"],
        },
    )
    rows = {
        r["bank_entry_id"]: r
        for r in api.ok("post", "/finance/bank-entries/scan", {"festival_id": FEST})["bank_entries"]
    }
    late_fam = entries(api, f"BANK_UNMATCHED:{ids['late']}")
    check(
        "late-explained debit: suspense reversed by a linked #adj (original untouched)",
        rows[ids["late"]]["status"] == "EXPLAINED_SUSPENSE_REVERSED"
        and len(late_fam) == 2
        and late_fam[1]["correction_of"] == late_fam[0]["id"]
        and lines_of(late_fam[0]) == {"SUSPENSE": (2_000, 0), "BANK_OPERATING": (0, 2_000)},
        late_fam,
    )
    lc = cases_get(api, late_case)
    check(
        "its case is RESOLVED as EXPLAINED_BY_RECORD with no human resolver (not a write-off)",
        lc["status"] == "RESOLVED"
        and lc["resolution_decision"] == "EXPLAINED_BY_RECORD"
        and db_case(db, late_case).resolved_by is None,
        lc,
    )

    # --- 2b/3b. human decisions --------------------------------------------
    r = limited.post(
        f"/finance/cash-cases/{var_case['case_id']}/decision",
        {"decision": "ORGANIZER_ABSORBS_SHORTAGE"},
    )
    check("close_books-only user cannot decide a cash case (403)", r.status_code == 403, r.text)
    r = api.post(
        f"/finance/cash-cases/{var_case['case_id']}/decision", {"decision": "WRITE_IT_OFF"}
    )
    check(
        "unknown decision -> 422 decision_not_allowed",
        r.status_code == 422 and code_of(r) == "decision_not_allowed",
        r.text,
    )
    r = api.post(
        f"/finance/cash-cases/{var_case['case_id']}/decision", {"decision": "BOOK_OVERAGE"}
    )
    check(
        "BOOK_OVERAGE on a shortage -> 422",
        r.status_code == 422 and code_of(r) == "decision_does_not_match_variance",
        r.text,
    )
    r = api.post(
        f"/finance/cash-cases/{var_case['case_id']}/decision",
        {"decision": "ORGANIZER_ABSORBS_SHORTAGE"},
    )
    check(
        "decision defaulting to the closed night D1 -> 409 business_date_closed",
        r.status_code == 409 and code_of(r) == "business_date_closed",
        r.text,
    )
    check(
        "refused decision left the case OPEN",
        cases_get(api, var_case["case_id"])["status"] == "OPEN",
    )
    dec = api.ok(
        "post",
        f"/finance/cash-cases/{var_case['case_id']}/decision",
        {
            "decision": "ORGANIZER_ABSORBS_SHORTAGE",
            "business_date": str(D2),
            "note": "organizer absorbs B's counted shortage",
        },
    )
    cos = entries(api, f"CASH_OVER_SHORT:{var_case['case_id']}")
    check(
        "ORGANIZER_ABSORBS_SHORTAGE: Dr CASH_OVER_SHORT 400 / Cr CASH_OFFICE 400; case RESOLVED",
        dec["case"]["status"] == "RESOLVED"
        and dec["effect"]["variance_cents"] == -400
        and len(cos) == 1
        and lines_of(cos[0]) == {"CASH_OVER_SHORT": (400, 0), "CASH_OFFICE": (0, 400)},
        (dec, cos),
    )
    vc = db_case(db, var_case["case_id"])
    check(
        "decision records the real resolver and capability",
        vc.resolved_by is not None and vc.resolved_capability == "finance_lead",
        (vc.resolved_by, vc.resolved_capability),
    )
    r = api.post(
        f"/finance/cash-cases/{var_case['case_id']}/decision",
        {"decision": "ORGANIZER_ABSORBS_SHORTAGE", "business_date": str(D2)},
    )
    check(
        "deciding again -> 409 case_not_open; nothing re-posted",
        r.status_code == 409
        and code_of(r) == "case_not_open"
        and len(entries(api, f"CASH_OVER_SHORT:{var_case['case_id']}")) == 1,
        r.text,
    )
    api.ok(
        "post",
        f"/finance/cash-cases/{dep_case_id}/decision",
        {"decision": "ORGANIZER_ABSORBS_SHORTAGE"},
    )
    api.ok(
        "post",
        f"/finance/cash-cases/{miss_case['case_id']}/decision",
        {"decision": "ACCEPT_POS_AND_BANK_DEPOSIT_AS_EVIDENCE"},
    )
    check(
        "MISSING_CASH_COUNT accepted: resolved, nothing posted",
        cases_get(api, miss_case["case_id"])["status"] == "RESOLVED"
        and not entries(api, f"CASH_OVER_SHORT:{miss_case['case_id']}"),
    )
    check(
        "CASH_OFFICE back to 0; CASH_OVER_SHORT 450 (only via decisions)",
        role_bal(api, "CASH_OFFICE") == 0 and role_bal(api, "CASH_OVER_SHORT") == 450,
        (role_bal(api, "CASH_OFFICE"), role_bal(api, "CASH_OVER_SHORT")),
    )

    before = api.ok("get", f"/journal/{susp[0]['id']}")
    fee_dec = api.ok(
        "post",
        f"/finance/cash-cases/{fee_case}/decision",
        {"decision": "BOOK_AS_BANK_FEE", "note": "monthly analysis fee"},
    )
    reclass = entries(api, f"BANK_FEE_RECLASS:{ids['bankfee']}")
    after = api.ok("get", f"/journal/{susp[0]['id']}")
    check(
        "BOOK_AS_BANK_FEE: linked correction Dr OPERATING_EXPENSE 8500 / Cr SUSPENSE 8500",
        len(reclass) == 1
        and reclass[0]["correction_of"] == susp[0]["id"]
        and lines_of(reclass[0]) == {"OPERATING_EXPENSE": (8_500, 0), "SUSPENSE": (0, 8_500)}
        and fee_dec["case"]["status"] == "RESOLVED",
        reclass,
    )
    check(
        "the suspense entry itself is unchanged (never rewritten)",
        before["lines"] == after["lines"] and after["corrections"] == [reclass[0]["entry_key"]],
        (before["lines"], after["lines"], after.get("corrections")),
    )
    check("SUSPENSE back to 0", role_bal(api, "SUSPENSE") == 0, role_bal(api, "SUSPENSE"))
    rows = {
        r["bank_entry_id"]: r
        for r in api.ok("post", "/finance/bank-entries/scan", {"festival_id": FEST})["bank_entries"]
    }
    check(
        "re-scan after the decision changes nothing",
        rows[ids["bankfee"]]["status"] == "RECLASSIFIED"
        and len(entries(api, f"BANK_UNMATCHED:{ids['bankfee']}")) == 1,
        rows[ids["bankfee"]],
    )

    # --- 4. chargeback fees -------------------------------------------------
    def cbfee(pid, fee, bank_entry_id=None):
        return api.post(
            "/finance/chargeback-fees",
            {
                "payment_id": pid,
                "fee_cents": fee,
                "business_date": str(D3),
                "bank_entry_id": bank_entry_id,
                "source_refs": [ids["m"].ref(f"cbfee:{pid}")],
            },
        )

    r = cbfee(ids["pe1_capture"], 1_500)
    check(
        "fee on a capture payment -> 422 not_a_chargeback",
        r.status_code == 422 and code_of(r) == "not_a_chargeback",
        r.text,
    )
    r = cbfee(ids["cb_f"], 9_999, ids["depcb"])
    check(
        "fee larger than the cited batch's stated fees -> 409",
        r.status_code == 409 and code_of(r) == "fee_not_in_batch_fees",
        r.text,
    )
    e_fee = cbfee(ids["cb_e"], 1_500, ids["depcb"])
    ef = e_fee.json()["chargeback_fee"]
    check(
        "E (contract permits deduction): expense via the batch deposit, recharged 1500",
        e_fee.status_code == 201
        and ef["expense_booked_via"] == f"DEPOSIT:{ids['depcb']}"
        and ef["recharge_permitted_by_contract"] is True
        and ef["recharged_cents"] == 1_500,
        e_fee.text,
    )
    rch = entries(api, f"CHARGEBACK_FEE_RECHARGE:{ids['cb_e']}")
    check(
        "E recharge entry: Dr VENDOR_FUNDS_PAYABLE 1500 / Cr PROCESSOR_FEE_EXPENSE 1500",
        len(rch) == 1
        and lines_of(rch[0])
        == {"VENDOR_FUNDS_PAYABLE": (1_500, 0), "PROCESSOR_FEE_EXPENSE": (0, 1_500)},
        rch,
    )
    check(
        "no separate expense entry for E (already inside DEP-CB's stated fee)",
        not entries(api, f"CHARGEBACK_FEE:{ids['cb_e']}"),
    )
    r = cbfee(ids["cb_e"], 1_400, ids["depcb"])
    check(
        "same chargeback, different fee -> 409",
        r.status_code == 409 and code_of(r) == "chargeback_fee_conflict",
        r.text,
    )
    f_fee = cbfee(ids["cb_f"], 1_500)
    ff = f_fee.json()["chargeback_fee"]
    acc = entries(api, f"CHARGEBACK_FEE:{ids['cb_f']}")
    check(
        "F (no batch states it, contract does not permit): accrued expense, NOT recharged",
        ff["recharge_permitted_by_contract"] is False
        and ff["recharged_cents"] == 0
        and len(acc) == 1
        and lines_of(acc[0])
        == {"PROCESSOR_FEE_EXPENSE": (1_500, 0), "PROCESSOR_CLEARING": (0, 1_500)}
        and not entries(api, f"CHARGEBACK_FEE_RECHARGE:{ids['cb_f']}"),
        (f_fee.text, acc),
    )
    term(db, ids["F"], deductible=True)
    db.commit()
    pd3 = api.ok("post", "/journal/post-day", {"festival_id": FEST, "business_date": str(D3)})
    fees = {
        f["payment_id"]: f
        for f in api.ok("get", "/finance/chargeback-fees", festival_id=FEST)["items"]
    }
    check(
        "after F's contract term permits deduction, post-day recharges F (1500), E unchanged",
        f"CHARGEBACK_FEE_RECHARGE:{ids['cb_f']}" in pd3["posted"]
        and fees[ids["cb_f"]]["recharged_cents"] == 1_500
        and fees[ids["cb_e"]]["recharged_cents"] == 1_500
        and len(entries(api, f"CHARGEBACK_FEE_RECHARGE:{ids['cb_e']}")) == 1,
        (pd3["posted"], fees),
    )

    # --- reports -------------------------------------------------------------
    expected = {
        "CASH_OFFICE": 0,
        "CASH_OVER_SHORT": 450,
        "SUSPENSE": 0,
        "OPERATING_EXPENSE": 25_500,
        "PROCESSOR_FEE_EXPENSE": 500,
        "PROCESSOR_CLEARING": -1_500,
        "VENDOR_FUNDS_PAYABLE": -30_000,
        "BANK_OPERATING": 975_050,
        "SUPPLIER_AP": 0,
        "SUPPLIER_EXPENSE": 30_000,
    }
    got = {k: role_bal(api, k) for k in expected}
    check("ending balances by role match the hand-worked numbers", got == expected, got)
    tb = api.ok("get", "/journal/trial-balance", festival_id=FEST)
    check(
        "trial balance balanced (1031500 each side: clearing owed to processor is a credit row)",
        tb["balanced"] and tb["total_debit_cents"] == 1_031_500,
        tb["total_debit_cents"],
    )
    cash = api.ok("get", "/cash", festival_id=FEST, as_of=str(D3))
    check(
        "cash report: cash-office block (0, 2 slips posted, 29550 deposited, no open cases)",
        cash["cash_office"]["cash_office_cents"] == 0
        and len(cash["cash_office"]["deposit_slips"]) == 2
        and cash["cash_office"]["deposited_cents"] == 29_550
        and cash["cash_office"]["open_cases"] == [],
        cash["cash_office"],
    )
    check(
        "cash report: suspense block shows the reclassified and reversed items, 0 held",
        cash["suspense"]["suspense_cents"] == 0
        and {i["status"] for i in cash["suspense"]["items"]} == {"RECLASSIFIED", "REVERSED"},
        cash["suspense"],
    )
    rep = api.ok("get", "/reports/festival", festival_id=FEST, as_of=str(D3))
    check(
        "festival report lists the 5 cash/bank cases (all resolved) and both chargeback fees",
        len(rep["cash_office_and_bank_cases"]) == 5
        and all(c["status"] == "RESOLVED" for c in rep["cash_office_and_bank_cases"])
        and len(rep["chargeback_fees"]) == 2
        and rep["journal_entry_counts_by_kind"].get("CASH_OFFICE_DEPOSIT") == 2,
        (rep["cash_office_and_bank_cases"], rep["journal_entry_counts_by_kind"]),
    )
    view = api.ok("get", "/finance/cash-office", festival_id=FEST, business_date=str(D1))
    check(
        "GET /finance/cash-office D1: 2 counts, 1 slip",
        len(view["counts"]) == 2 and len(view["deposit_slips"]) == 1,
        view,
    )

    for d in (D2, D3):
        c = api.ok("post", "/close", {"festival_id": FEST, "business_date": str(d)})
        check(
            f"close {d}: every hard check passes and cash/bank cases are resolved",
            c["created"]
            and all(c["checks"]["hard"].values())
            and c["checks"]["warnings"]["cash_office_and_bank_cases_resolved"],
            c["checks"],
        )

    # --- audit events ---------------------------------------------------------
    ev = {
        (e.stream_id, e.event_type)
        for e in db.execute(
            select(BusinessEvent).where(
                BusinessEvent.stream_id.in_(
                    [f"cash_office:{FEST}", f"case:{var_case['case_id']}", f"case:{fee_case}"]
                )
            )
        ).scalars()
    }
    check(
        "audit events: count/slip/fee recorded, cases opened and resolved",
        {
            (f"cash_office:{FEST}", "cash_office.count_recorded"),
            (f"cash_office:{FEST}", "cash_office.deposit_slip_recorded"),
            (f"cash_office:{FEST}", "chargeback_fee.recorded"),
            (f"case:{var_case['case_id']}", "case.opened"),
            (f"case:{var_case['case_id']}", "case.resolved"),
            (f"case:{fee_case}", "case.resolved"),
        }
        <= ev,
        sorted(ev),
    )
    all_entries = entries(api, "")
    unbalanced = [
        e["entry_key"]
        for e in all_entries
        if sum(ln["debit_cents"] for ln in e["lines"])
        != sum(ln["credit_cents"] for ln in e["lines"])
    ]
    check(
        f"all {len(all_entries)} journal entries individually balanced", not unbalanced, unbalanced
    )

    with psycopg.connect(APP_ROLE_DSN, autocommit=True) as conn:
        for table in (
            "cash_office_counts",
            "cash_deposit_slips",
            "cash_deposit_bags",
            "chargeback_fees",
        ):
            try:
                conn.execute(f"UPDATE {table} SET id = id WHERE false")
                check(f"encore_app cannot UPDATE {table}", False, "update was allowed")
            except psycopg.errors.InsufficientPrivilege:
                check(f"encore_app cannot UPDATE {table}", True)
    anon = httpx.post(
        f"{API_BASE_URL}/api/v1/finance/bank-entries/scan", json={"festival_id": FEST}
    )
    check("unauthenticated scan -> 401", anon.status_code == 401, anon.status_code)
    db.close()

    print(f"\n{len(_passes)} passed, {len(_failures)} failed")
    for f in _failures:
        print(f"  FAILED: {f}")
    return 1 if _failures else 0


def cases_get(api: Api, case_id: str) -> dict:
    return next(
        c
        for c in api.ok("get", "/finance/cash-cases", festival_id=FEST)["items"]
        if c["case_id"] == case_id
    )


def db_case(db, case_id: str):
    from app.models_ledger import Case

    db.expire_all()
    return db.get(Case, case_id)


if __name__ == "__main__":
    sys.exit(main())
