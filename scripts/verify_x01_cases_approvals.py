#!/usr/bin/env python3
"""Manual, real-HTTP verification of X0.1 (generalize cases and approvals).

*** NOT an automated test suite. *** Per standing user instruction for this
extension (see docs/prevention-extension/CONTEXT.md), no pytest files were
written for X0.1. This script is the substitute: it seeds the throwaway
hand-checked dev fixtures in scripts/dev_fixtures_x01.py (NOT B01), drives
real HTTP calls against a real running `uvicorn` process and a real
Postgres database, and asserts hand-worked expected outcomes against the
actual API responses. Exits non-zero and prints every mismatch; prints a
PASS line per check otherwise.

Covers TICKET_SPEC.md's X0.1 "Done when" criteria plus the routing table
and the backfill:

  1. Backfill: every pre-existing `cases` row has a non-null subject_type/
     subject_id (checked directly against the DB -- the migration already
     ran before this script does anything).
  2. A case can reference an invoice: the real A08 duplicate-invoice call
     site opens a NEW case tagged subject_type="supplier_invoice",
     subject_id=<the real invoice id>.
  3. A case can reference a forecast snapshot: a case carries
     subject_type="forecast_snapshot" and an arbitrary subject_id string,
     with no forecast_snapshot table existing at all (not even created by
     this script) -- proving the column is a loose reference.
  4. Existing night-settlement/case/policy verification scripts are
     unaffected -- this script does NOT re-run them (the orchestrating
     agent re-runs verify_a03/a04/a05/a07/a08 separately and compares pass
     counts before/after); this script only adds X0.1's own new checks.
  5. The approval endpoint rejects a capability the user does not hold:
     the SAME limited user is granted for one action
     (approve_invoice_exception, which it holds) and rejected (403) for
     another (release_supplier_payment, which it does NOT hold) -- proving
     the check is genuinely per-action/dynamic, not all-or-nothing.
  6. Deterministic validation: an unknown action, an unknown subject_type,
     an action against an illegal subject_type, and a case_id whose
     subject doesn't match the request are all rejected with 422 -- never
     silently allowed.
  7. domain.case_routing's routing table: every owner capability it emits
     is a real, currently-seeded capability code.

Usage:
    1. Real Postgres running (encore-postgres-1), migrated to head
       (0010_x01_cases_approvals).
    2. A real `uvicorn app.main:app` process reachable at API_BASE_URL,
       started with packages/domain on PYTHONPATH.
    3. uv run python scripts/seed_admin.py   (grants the 5 new capabilities
       to the existing admin user -- re-run after the migration)
    4. uv run python scripts/dev_fixtures_x01.py   (optional -- this script
       also seeds idempotently itself)
    5. DATABASE_URL=... API_BASE_URL=http://127.0.0.1:8010 \
       ADMIN_EMAIL=admin@example.com ADMIN_PASSWORD=... \
       uv run python scripts/verify_x01_cases_approvals.py
"""

from __future__ import annotations

import os
import sys

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app.auth import get_user_capability_codes  # noqa: E402
from app.db import get_session_factory, make_engine  # noqa: E402
from app.models import ALL_CAPABILITY_CODES, User  # noqa: E402
from app.models_ledger import Case  # noqa: E402
from domain.case_routing import known_routes, route_case  # noqa: E402
from sqlalchemy import select  # noqa: E402

import dev_fixtures_x01 as fixtures  # noqa: E402

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


def login(client: httpx.Client, email: str, password: str) -> dict:
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    check(
        f"login succeeds ({email})",
        resp.status_code == 200,
        f"status={resp.status_code} body={resp.text}",
    )
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def main() -> int:  # noqa: C901 -- a long, linear acceptance walkthrough; not meant to be split
    make_engine()
    session_factory = get_session_factory()
    db = session_factory()
    try:
        admin = db.execute(select(User).where(User.email == ADMIN_EMAIL)).scalar_one()
        ids = fixtures.seed(db, admin_id=admin.id)

        # ---------------------------------------------------------------
        # Check 1: backfill -- no pre-existing case left with a null
        # subject_type/subject_id. The migration itself backfilled every
        # row at upgrade time; this just confirms none were missed.
        # ---------------------------------------------------------------
        unbackfilled = db.execute(
            select(Case.id, Case.reason_code).where(
                (Case.subject_type.is_(None)) | (Case.subject_id.is_(None))
            )
        ).all()
        # Known, documented exception (see docs/progress/X0.1-handoff.md):
        # scripts/verify_a07_payouts.py and scripts/dev_fixtures_a08.py each
        # construct ONE raw `Case(...)` row directly as test-only blocking
        # scaffolding for THEIR OWN already-landed verification (not through
        # any of the four real production case-opening call sites X0.1
        # patched: app.policy, app.payables, app.reconciliation). X0.1
        # deliberately does not edit other tickets' already-reviewed
        # verify/dev_fixtures scripts to retrofit this tagging -- it is
        # test scaffolding, not a production gap. Every REAL call site is
        # covered (checks below confirm this for policy.py and payables.py
        # specifically); this only excludes those two known fixture rows.
        unexpected_unbackfilled = [
            (cid, reason)
            for cid, reason in unbackfilled
            if not (reason.startswith("a07-verify-hold-") or reason == "A08_VERIFY_HOLD")
        ]
        check(
            "every case row is backfilled, except the two known other-ticket "
            "raw test-fixture rows (a07-verify-hold-*, A08_VERIFY_HOLD)",
            len(unexpected_unbackfilled) == 0,
            f"unexpected unbackfilled case ids: {unexpected_unbackfilled}",
        )
    finally:
        db.close()
    print(f"seeded/confirmed X0.1 dev fixtures: run={ids['run']}\n")

    client = httpx.Client(base_url=API_BASE_URL, timeout=10.0)
    admin_headers = login(client, ADMIN_EMAIL, ADMIN_PASSWORD)

    # -----------------------------------------------------------------------
    # Check 2: a case can reference an invoice (real A08 call site, tagged).
    # -----------------------------------------------------------------------
    inv_case_resp = client.get(
        f"/api/v1/cases/{ids['supplier_invoice_case_id']}", headers=admin_headers
    )
    check(
        "GET the real A08 duplicate-invoice case succeeds",
        inv_case_resp.status_code == 200,
        f"status={inv_case_resp.status_code} body={inv_case_resp.text}",
    )
    inv_case = inv_case_resp.json()
    check(
        "duplicate-invoice case is tagged subject_type=supplier_invoice, subject_id=<real inv id>",
        inv_case["subject_type"] == "supplier_invoice"
        and inv_case["subject_id"] == ids["supplier_invoice_id"],
        str(inv_case),
    )

    # -----------------------------------------------------------------------
    # Check 3: a case can reference a forecast snapshot (loose string, no
    # forecast_snapshot table exists at all).
    # -----------------------------------------------------------------------
    fcst_case_resp = client.get(
        f"/api/v1/cases/{ids['forecast_snapshot_case_id']}", headers=admin_headers
    )
    check(
        "GET the fixture-only forecast_snapshot case succeeds",
        fcst_case_resp.status_code == 200,
        f"status={fcst_case_resp.status_code} body={fcst_case_resp.text}",
    )
    fcst_case = fcst_case_resp.json()
    check(
        "forecast_snapshot case carries the arbitrary, non-FK subject_id string",
        fcst_case["subject_type"] == "forecast_snapshot"
        and fcst_case["subject_id"] == ids["forecast_snapshot_subject_id"],
        str(fcst_case),
    )

    # -----------------------------------------------------------------------
    # Check 4: vendor_run case from the real A05 call site is also tagged.
    # -----------------------------------------------------------------------
    if ids["vendor_run_case_id"] is not None:
        vendor_case_resp = client.get(
            f"/api/v1/cases/{ids['vendor_run_case_id']}", headers=admin_headers
        )
        check(
            "GET the real A05 contract-conflict case succeeds",
            vendor_case_resp.status_code == 200,
            f"status={vendor_case_resp.status_code} body={vendor_case_resp.text}",
        )
        vendor_case = vendor_case_resp.json()
        check(
            "contract-conflict case is tagged subject_type=vendor_run, subject_id=<real vendor id>",
            vendor_case["subject_type"] == "vendor_run"
            and vendor_case["subject_id"] == ids["vendor_id"],
            str(vendor_case),
        )

    # -----------------------------------------------------------------------
    # Check 5: the approval endpoint rejects a capability the user does not
    # hold, and accepts one it does -- SAME limited user, two actions.
    # -----------------------------------------------------------------------
    limited_headers = login(client, ids["limited_user_email"], ids["limited_user_password"])

    unauth_resp = httpx.Client(base_url=API_BASE_URL, timeout=10.0).post(
        "/api/v1/approvals",
        json={
            "subject_type": "supplier_invoice",
            "subject_id": ids["supplier_invoice_id"],
            "action": "approve_invoice_exception",
        },
    )
    check(
        "POST /approvals with no token is rejected (401)",
        unauth_resp.status_code == 401,
        f"status={unauth_resp.status_code}",
    )

    denied_resp = client.post(
        "/api/v1/approvals",
        json={
            "subject_type": "supplier_invoice",
            "subject_id": ids["supplier_invoice_id"],
            "action": "release_supplier_payment",
            "note": "limited user should not be able to do this",
        },
        headers=limited_headers,
    )
    check(
        "limited user WITHOUT release_supplier_payment is rejected (403)",
        denied_resp.status_code == 403,
        f"status={denied_resp.status_code} body={denied_resp.text}",
    )

    granted_resp = client.post(
        "/api/v1/approvals",
        json={
            "subject_type": "supplier_invoice",
            "subject_id": ids["supplier_invoice_id"],
            "case_id": ids["supplier_invoice_case_id"],
            "action": "approve_invoice_exception",
            "note": "limited user DOES hold this one",
            "payload": {"example": "x01-fixture"},
        },
        headers=limited_headers,
    )
    check(
        "SAME limited user WITH approve_invoice_exception is accepted (201)",
        granted_resp.status_code == 201,
        f"status={granted_resp.status_code} body={granted_resp.text}",
    )
    granted = granted_resp.json() if granted_resp.status_code == 201 else {}
    check(
        "recorded approval stores the actual capability actually checked, never a fabricated one",
        granted.get("required_capability") == "approve_invoice_exception"
        and granted.get("actor_capability") == "approve_invoice_exception"
        and granted.get("actor_id") == ids["limited_user_id"],
        str(granted),
    )

    admin_release_resp = client.post(
        "/api/v1/approvals",
        json={
            "subject_type": "supplier_invoice",
            "subject_id": ids["supplier_invoice_id"],
            "action": "release_supplier_payment",
            "note": "admin holds every capability",
        },
        headers=admin_headers,
    )
    check(
        "admin user (holds every capability) IS accepted for release_supplier_payment (201)",
        admin_release_resp.status_code == 201,
        f"status={admin_release_resp.status_code} body={admin_release_resp.text}",
    )

    # -----------------------------------------------------------------------
    # Check 6: deterministic validation -- never silently allowed.
    # -----------------------------------------------------------------------
    bad_action_resp = client.post(
        "/api/v1/approvals",
        json={
            "subject_type": "supplier_invoice",
            "subject_id": ids["supplier_invoice_id"],
            "action": "not_a_real_action",
        },
        headers=admin_headers,
    )
    check(
        "unknown action is rejected (422), never silently allowed",
        bad_action_resp.status_code == 422,
        f"status={bad_action_resp.status_code} body={bad_action_resp.text}",
    )

    bad_subject_type_resp = client.post(
        "/api/v1/approvals",
        json={
            "subject_type": "not_a_real_subject_type",
            "subject_id": "x",
            "action": "acknowledge_forecast",
        },
        headers=admin_headers,
    )
    check(
        "unknown subject_type is rejected (422)",
        bad_subject_type_resp.status_code == 422,
        f"status={bad_subject_type_resp.status_code} body={bad_subject_type_resp.text}",
    )

    mismatched_action_resp = client.post(
        "/api/v1/approvals",
        # release_supplier_payment is only legal against a supplier_invoice subject.
        json={
            "subject_type": "vendor_run",
            "subject_id": ids["vendor_id"],
            "action": "release_supplier_payment",
        },
        headers=admin_headers,
    )
    check(
        "action against an illegal subject_type is rejected (422)",
        mismatched_action_resp.status_code == 422,
        f"status={mismatched_action_resp.status_code} body={mismatched_action_resp.text}",
    )

    mismatched_case_resp = client.post(
        "/api/v1/approvals",
        json={
            "subject_type": "supplier_invoice",
            "subject_id": ids["supplier_invoice_id"],
            "action": "approve_invoice_exception",
            "case_id": ids["vendor_run_case_id"],  # a real case, but about a DIFFERENT subject
        },
        headers=admin_headers,
    )
    check(
        "case_id citing a case about a DIFFERENT subject is rejected (422)",
        mismatched_case_resp.status_code == 422,
        f"status={mismatched_case_resp.status_code} body={mismatched_case_resp.text}",
    )

    missing_case_resp = client.post(
        "/api/v1/approvals",
        json={
            "subject_type": "supplier_invoice",
            "subject_id": ids["supplier_invoice_id"],
            "action": "approve_invoice_exception",
            "case_id": "not-a-real-case-id",
        },
        headers=admin_headers,
    )
    check(
        "case_id citing a case that doesn't exist is rejected (404)",
        missing_case_resp.status_code == 404,
        f"status={missing_case_resp.status_code} body={missing_case_resp.text}",
    )

    # -----------------------------------------------------------------------
    # Check 7: reads -- list/get round-trip what was just recorded.
    # -----------------------------------------------------------------------
    list_resp = client.get(
        "/api/v1/approvals",
        params={"subject_type": "supplier_invoice", "subject_id": ids["supplier_invoice_id"]},
        headers=admin_headers,
    )
    check(
        "GET /approvals filtered by subject succeeds",
        list_resp.status_code == 200,
        f"status={list_resp.status_code} body={list_resp.text}",
    )
    listed_actions = {a["action"] for a in list_resp.json()}
    check(
        "both recorded approvals for this invoice are listed",
        {"approve_invoice_exception", "release_supplier_payment"} <= listed_actions,
        str(listed_actions),
    )

    get_one_resp = client.get(f"/api/v1/approvals/{granted['id']}", headers=admin_headers)
    check(
        "GET /approvals/{id} succeeds for what was just recorded",
        get_one_resp.status_code == 200 and get_one_resp.json()["id"] == granted["id"],
        f"status={get_one_resp.status_code} body={get_one_resp.text}",
    )

    missing_get_resp = client.get("/api/v1/approvals/not-a-real-id", headers=admin_headers)
    check(
        "GET /approvals/{id} for a nonexistent id is 404",
        missing_get_resp.status_code == 404,
        f"status={missing_get_resp.status_code}",
    )

    # -----------------------------------------------------------------------
    # Check 8: routing table emits only real, currently-seeded capabilities.
    # -----------------------------------------------------------------------
    routes = known_routes()
    check("routing table has at least one route configured", len(routes) > 0, str(routes))
    all_caps_ok = True
    bad = []
    for subject_type, case_kind, capability in routes:
        resolved = route_case(subject_type, case_kind)
        if resolved != capability or capability not in ALL_CAPABILITY_CODES:
            all_caps_ok = False
            bad.append((subject_type, case_kind, capability))
    check(
        "every routing-table owner capability is a real seeded capability code",
        all_caps_ok,
        str(bad),
    )

    unknown_route_raised = False
    try:
        route_case("vendor_run", "no_such_case_kind")
    except ValueError:
        unknown_route_raised = True
    check(
        "an unconfigured (subject_type, case_kind) pair raises, never silently defaults",
        unknown_route_raised,
    )

    # -----------------------------------------------------------------------
    # Check 9: authenticity -- the limited user's DB-held capabilities match
    # what login/the approval checks actually saw (never trusted from a
    # client-supplied value).
    # -----------------------------------------------------------------------
    db2 = session_factory()
    try:
        held = get_user_capability_codes(db2, ids["limited_user_id"])
    finally:
        db2.close()
    check(
        "limited user's real DB capabilities match the fixture's intent "
        "(resolve_case + approve_invoice_exception, NOT release_supplier_payment)",
        held == {"resolve_case", "approve_invoice_exception"},
        str(held),
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
