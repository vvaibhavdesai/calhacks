#!/usr/bin/env python3
"""Manual, real-HTTP verification of A03 reconciliation.

*** NOT an automated test suite. *** Per explicit user instruction for this
task, no pytest files were written for A03. This script is the substitute:
it seeds the throwaway hand-checked dev fixtures in
scripts/dev_fixtures_a03.py (NOT the B01 oracle -- see that module's
docstring and docs/progress/A03-handoff.md), drives a real reconciliation
run over real HTTP against a real running `uvicorn` process and a real
Postgres database, and asserts the hand-worked expected outcomes by reading
the actual API responses. Exits non-zero and prints every mismatch if
anything is wrong; prints a PASS line per check otherwise.

Usage:
    1. Real Postgres running (encore-postgres-1), migrated to head.
    2. A real `uvicorn app.main:app` process reachable at API_BASE_URL.
    3. uv run python scripts/dev_fixtures_a03.py   (optional -- this script
       also seeds idempotently itself)
    4. DATABASE_URL=... API_BASE_URL=http://127.0.0.1:8010 \
       ADMIN_EMAIL=admin@example.com ADMIN_PASSWORD=... \
       uv run python scripts/verify_a03_reconciliation.py
"""

from __future__ import annotations

import os
import sys
from datetime import date

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app.db import get_session_factory, make_engine  # noqa: E402
from app.models_ledger import Order, Payment  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

import dev_fixtures_a03 as fixtures  # noqa: E402

API_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8010")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "LocalDevPassw0rd!")

RUN_AS_OF = date(2026, 10, 19)

_failures: list[str] = []
_passes: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        _passes.append(label)
        print(f"PASS  {label}")
    else:
        _failures.append(f"{label} -- {detail}")
        print(f"FAIL  {label} -- {detail}")


def main() -> int:
    make_engine()
    session_factory = get_session_factory()
    db = session_factory()
    try:
        ids = fixtures.seed(db)

        # --- duplicate IMPORT must be a no-op (distinct from a duplicate
        # REAL charge, which app/reconciliation.py classifies as a case --
        # checked separately below via order_c3). Re-running the exact same
        # fixture-seeding pass a second time, against the real database,
        # must not create a single additional Order or Payment row.
        orders_before = db.execute(select(func.count()).select_from(Order)).scalar_one()
        payments_before = db.execute(select(func.count()).select_from(Payment)).scalar_one()
        fixtures.seed(db)  # re-import: same source_system/source_native_id values throughout
        orders_after = db.execute(select(func.count()).select_from(Order)).scalar_one()
        payments_after = db.execute(select(func.count()).select_from(Payment)).scalar_one()
        check(
            "duplicate import (re-seeding identical orders) creates zero new Order rows",
            orders_before == orders_after,
            f"before={orders_before} after={orders_after}",
        )
        check(
            "duplicate import (re-seeding identical payments) creates zero new Payment rows",
            payments_before == payments_after,
            f"before={payments_before} after={payments_after}",
        )
    finally:
        db.close()
    print(f"\nseeded/confirmed {len(ids)} fixture ids (idempotent)\n")

    client = httpx.Client(base_url=API_BASE_URL, timeout=10.0)

    # --- real login over real HTTP ----------------------------------------
    login = client.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    check(
        "login succeeds", login.status_code == 200, f"status={login.status_code} body={login.text}"
    )
    token = login.json()["access_token"]
    auth_headers = {"Authorization": f"Bearer {token}"}

    # --- unauthenticated trigger is rejected -------------------------------
    unauth = client.post(
        "/api/v1/reconciliation/runs",
        json={"festival_id": fixtures.FESTIVAL_ID, "as_of": RUN_AS_OF.isoformat()},
    )
    check(
        "POST /reconciliation/runs with no token is rejected",
        unauth.status_code == 401,
        f"status={unauth.status_code}",
    )

    # --- real reconciliation run over real HTTP ----------------------------
    run_resp = client.post(
        "/api/v1/reconciliation/runs",
        json={"festival_id": fixtures.FESTIVAL_ID, "as_of": RUN_AS_OF.isoformat()},
        headers=auth_headers,
    )
    check(
        "POST /reconciliation/runs succeeds (201)",
        run_resp.status_code == 201,
        f"status={run_resp.status_code} body={run_resp.text}",
    )
    run = run_resp.json()
    run_id = run["run_id"]
    print(
        f"\nrun_id={run_id} status={run['status']} total_cases_opened={run['total_cases_opened']}"
    )
    for vr in run["vendor_results"]:
        print(
            f"  vendor {vr['vendor_id']}: orders={vr['order_groups']} cash={vr['cash_groups']} "
            f"cases={vr['cases_opened']} error={vr['error']}"
        )
    print(
        f"  bank_deposit_groups={run['bank_deposit_groups']} "
        f"bank_deposit_cases={run['bank_deposit_cases']}"
    )

    # Fault isolation: the run completes (not FAILED), even though Vendor E
    # has a genuine missing-payment problem baked into the fixtures.
    check(
        "run completes despite Vendor E's real data problem (fault isolation)",
        run["status"] == "COMPLETED",
        f"status={run['status']}",
    )

    vendor_result_by_id = {vr["vendor_id"]: vr for vr in run["vendor_results"]}

    # Vendor A: normal vendor -- zero cases.
    check(
        "Vendor A (normal) opens zero cases",
        vendor_result_by_id[ids["vendor_a"]]["cases_opened"] == 0,
        str(vendor_result_by_id[ids["vendor_a"]]),
    )
    # Vendor B: one-to-many split capture -- still zero cases.
    check(
        "Vendor B (one order, two captures) opens zero cases",
        vendor_result_by_id[ids["vendor_b"]]["cases_opened"] == 0,
        str(vendor_result_by_id[ids["vendor_b"]]),
    )
    # Vendor D: one cash-variance day + one matching day -> exactly 1 case.
    check(
        "Vendor D (cash: 1 short day + 1 matching day) opens exactly 1 case",
        vendor_result_by_id[ids["vendor_d"]]["cases_opened"] == 1,
        str(vendor_result_by_id[ids["vendor_d"]]),
    )
    # Vendor E: the deliberately broken vendor -- exactly 1 case, and it
    # does NOT prevent A/B/D from completing (checked above + below).
    check(
        "Vendor E (missing payment) opens exactly 1 case",
        vendor_result_by_id[ids["vendor_e"]]["cases_opened"] == 1,
        str(vendor_result_by_id[ids["vendor_e"]]),
    )
    # Vendor F: single non-duplicate overcharge -- exactly 1 case.
    check(
        "Vendor F (overcharge, not duplicate-like) opens exactly 1 case",
        vendor_result_by_id[ids["vendor_f"]]["cases_opened"] == 1,
        str(vendor_result_by_id[ids["vendor_f"]]),
    )
    # Vendor C: 5 orders -- 3 should be "matched" (refund, duplicate-resolved,
    # chargeback) and 2 should be cases (unresolved duplicate, refund > capture).
    check(
        "Vendor C (fee/timing/refund/chargeback) opens exactly 2 cases out of 5 orders",
        vendor_result_by_id[ids["vendor_c"]]["cases_opened"] == 2,
        str(vendor_result_by_id[ids["vendor_c"]]),
    )

    # --- fetch match groups for this run and check specific classifications
    groups_resp = client.get(
        "/api/v1/reconciliation/groups", params={"run_id": run_id}, headers=auth_headers
    )
    check("GET /reconciliation/groups succeeds", groups_resp.status_code == 200)
    groups = groups_resp.json()
    print(f"\n{len(groups)} match groups created by this run")

    def order_groups_by_order_id() -> dict[str, dict]:
        by_order: dict[str, dict] = {}
        for g in groups:
            if g["match_type"] != "order_capture":
                continue
            for m in g["members"]:
                if m["member_kind"] == "order":
                    by_order[m["member_id"]] = g
        return by_order

    def bank_group_by_entry_id() -> dict[str, dict]:
        by_entry: dict[str, dict] = {}
        for g in groups:
            if g["match_type"] != "bank_deposit":
                continue
            for m in g["members"]:
                if m["member_kind"] == "bank_entry":
                    by_entry[m["member_id"]] = g
        return by_entry

    order_groups = order_groups_by_order_id()
    bank_groups = bank_group_by_entry_id()

    expected_order_outcomes = {
        "order_a1": ("matched", "order_payment_match"),
        "order_b1": ("matched", "order_payment_match"),
        "order_c1": ("matched", "refund_applied"),
        "order_c2": ("matched", "duplicate_charge_refunded"),
        "order_c3": ("case", "suspected_duplicate_charge"),
        "order_c4": ("matched", "chargeback_applied"),
        "order_c5": ("case", "refund_exceeds_captured_amount"),
        "order_e1": ("case", "missing_or_partial_payment"),
        "order_f1": ("case", "order_overcharged"),
        "order_g1": ("matched", "order_payment_match"),
    }
    for key, (expected_status, expected_reason) in expected_order_outcomes.items():
        order_id = ids[key]
        group = order_groups.get(order_id)
        ok = (
            group is not None
            and group["status"] == expected_status
            and group["reason_code"] == expected_reason
        )
        check(
            f"{key}: order_capture classification == ({expected_status}, {expected_reason})",
            ok,
            f"got={None if group is None else (group['status'], group['reason_code'])}",
        )

    expected_bank_outcomes = {
        "bank_entry_be1": ("matched", "processor_fee_explained"),
        "bank_entry_be2": ("pending", "deposit_pending_settlement"),
        "bank_entry_be3": ("case", "deposit_settlement_overdue"),
        "bank_entry_be4": ("case", "bank_deposit_variance_unexplained"),
    }
    for key, (expected_status, expected_reason) in expected_bank_outcomes.items():
        entry_id = ids[key]
        group = bank_groups.get(entry_id)
        ok = (
            group is not None
            and group["status"] == expected_status
            and group["reason_code"] == expected_reason
        )
        check(
            f"{key}: bank_deposit classification == ({expected_status}, {expected_reason})",
            ok,
            f"got={None if group is None else (group['status'], group['reason_code'])}",
        )

    # BE2 is explicitly NOT "matched" -- pending/in-transit funds must never
    # look like available cash.
    be2_group = bank_groups.get(ids["bank_entry_be2"])
    check(
        "pending bank deposit (BE2) is never classified as matched/available cash",
        be2_group is not None and be2_group["status"] != "matched",
        str(be2_group),
    )

    # --- cases: every "case" match group has a real, fetchable Case row ---
    cases_resp = client.get(
        "/api/v1/reconciliation/cases", params={"run_id": run_id}, headers=auth_headers
    )
    check("GET /reconciliation/cases succeeds", cases_resp.status_code == 200)
    cases = cases_resp.json()
    print(f"{len(cases)} cases opened by this run")
    case_groups = [g for g in groups if g["status"] == "case"]
    check(
        "every case-status match group has a non-null case_id",
        all(g["case_id"] is not None for g in case_groups),
        str([g for g in case_groups if g["case_id"] is None]),
    )
    case_ids_from_groups = {g["case_id"] for g in case_groups}
    case_ids_fetched = {c["id"] for c in cases}
    check(
        "every match-group case_id is independently fetchable via GET /cases",
        case_ids_from_groups.issubset(case_ids_fetched),
        f"missing={case_ids_from_groups - case_ids_fetched}",
    )
    # No silent write-off: total of (order+cash+bank) cases from the run
    # summary matches the count of case-status match groups from this run.
    check(
        "no silent write-offs: run summary's total_cases_opened matches case-status match groups",
        run["total_cases_opened"] == len(case_groups),
        f"run={run['total_cases_opened']} groups={len(case_groups)}",
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
