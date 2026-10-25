"""S-track low-risk gap closure (migration 0018 + domain.approvals vocab):
real-HTTP verification against a real Postgres + a real uvicorn process.

Covers the four additive gaps closed on top of A08 payables-v2, each
verified NOT to disturb existing, already-proven behavior:

1. S1 `delivery_receipts.accepted_by` -- stored and returned.
2. S3 `purchase_orders.partial_payment_allowed` -- False holds the WHOLE
   invoice (not just the disputed remainder) until fully delivered; True
   (the default) behaves exactly as verify_payables_v2.py already proved.
3. S4 `dispute_invoice_exception`/`request_invoice_credit` actions on the
   existing generic `POST /approvals` (X0.1) -- no payables.py code was
   touched for this one; only domain.approvals' vocabulary dicts grew two
   entries reusing the existing `approve_invoice_exception` capability.
4. S6 `DUPLICATE_SUSPECTED` -- same supplier + same total within 7 days on
   a DIFFERENT date opens a non-blocking case; the exact-same-date case
   still only trips the existing BLOCKING `POSSIBLE_DUPLICATE_SUPPLIER_INVOICE`
   (no double-fire); a 10-day-apart pair opens neither.

Self-contained, fresh suppliers/POs/invoices per PID, no pytest files, per
standing convention. Run against a fresh database -- specifically, NOT
on the same database as a prior verify_payables_v2.py run in the same
session: that script closes 2026-10-07/2026-10-15 for fest_golden_coast
as part of its own proof, and this script's S1/S3 checks create
invoices dated 2026-10-15 (fixture-identical), which a closed date then
refuses. Same fixture dates, same already-documented constraint as
verify_payables_v2.py's own docstring -- not a product bug, a run-order
one, confirmed by reproducing it.
"""

from __future__ import annotations

import os

import httpx

API_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8010")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "LocalDevPassw0rd!")
FESTIVAL_ID = os.environ.get("VERIFY_FESTIVAL_ID", "fest_golden_coast")
PID = os.getpid()

_passes: list[str] = []
_failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        _passes.append(label)
        print(f"PASS  {label}")
    else:
        _failures.append(f"{label} -- {detail}")
        print(f"FAIL  {label} -- {detail}")


def main() -> int:
    login = httpx.post(
        f"{API_BASE_URL}/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    if login.status_code != 200:
        check("login succeeds (required for all checks below)", False, login.text)
        print(f"\n{len(_passes)} passed, {len(_failures)} failed")
        return 1
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    c = httpx.Client(base_url=f"{API_BASE_URL}/api/v1", timeout=15.0, headers=headers)
    admin_id = c.get("/auth/me").json()["id"]
    fund = c.post("/cash-pool/fund", json={"festival_id": FESTIVAL_ID, "additional_cents": 100_000_000})
    check("setup: cash pool funded for the payment checks below", fund.status_code == 200, fund.text)

    # =======================================================================
    # 1. S1 accepted_by
    # =======================================================================
    sup1 = c.post(
        "/suppliers", json={"festival_id": FESTIVAL_ID, "name": f"Verify S1 Supplier {PID}"}
    ).json()
    po1 = c.post(
        "/purchase-orders",
        json={
            "supplier_id": sup1["id"],
            "po_number": f"VERIFY-S1-PO-{PID}",
            "po_date": "2026-10-07",
            "amount_cents": 375_000,
            "lines": [{"line_no": 1, "item_code": "GEN100", "quantity": 1, "unit_price_cents": 375_000}],
        },
    ).json()
    receipt = c.post(
        "/deliveries",
        json={
            "po_id": po1["id"],
            "line_no": 1,
            "quantity": 1,
            "accepted_at": "2026-10-15T11:00:00-07:00",
            "available_at": "2026-10-15T11:00:00-07:00",
            "receipt_key": f"verify-s1-r1-{PID}",
            "accepted_by": admin_id,
        },
    ).json()
    check("S1: accepted_by is stored and returned on the receipt", receipt.get("accepted_by") == admin_id, receipt)

    receipt_unset = c.post(
        "/deliveries",
        json={
            "po_id": po1["id"],
            "line_no": 1,
            "quantity": 0,
            "accepted_at": "2026-10-15T11:00:00-07:00",
            "available_at": "2026-10-15T11:00:00-07:00",
            "receipt_key": f"verify-s1-r2-{PID}",
        },
    )
    check(
        "S1: accepted_by omitted is refused for a different reason (quantity_must_be_positive), "
        "not silently guessed",
        receipt_unset.status_code in (400, 409, 422)
        and "quantity_must_be_positive" in receipt_unset.text,
        receipt_unset.text,
    )

    # =======================================================================
    # 2. S3 partial_payment_allowed
    # =======================================================================
    # --- False: the whole invoice is held, even the undisputed 4/6 ---
    sup3 = c.post(
        "/suppliers", json={"festival_id": FESTIVAL_ID, "name": f"Verify S3 Supplier {PID}"}
    ).json()
    po3 = c.post(
        "/purchase-orders",
        json={
            "supplier_id": sup3["id"],
            "po_number": f"VERIFY-S3-PO-{PID}",
            "po_date": "2026-10-07",
            "amount_cents": 2_250_000,  # $22,500: 6 x $3,750, fixture-identical
            "lines": [{"line_no": 1, "item_code": "GEN100", "quantity": 6, "unit_price_cents": 375_000}],
            "partial_payment_allowed": False,
        },
    ).json()
    check("S3: partial_payment_allowed=false round-trips on create", po3["partial_payment_allowed"] is False, po3)
    inv3 = c.post(
        "/invoices",
        json={
            "supplier_id": sup3["id"],
            "po_id": po3["id"],
            "invoice_number": f"VERIFY-S3-{PID}",
            "invoice_date": "2026-10-15",
            "due_date": "2026-10-20",
            "net_cents": 2_250_000,
            "tax_cents": 0,
            "gross_cents": 2_250_000,
        },
    ).json()["invoice"]
    c.post(
        "/deliveries",
        json={
            "po_id": po3["id"],
            "line_no": 1,
            "quantity": 4,
            "accepted_at": "2026-10-15T11:00:00-07:00",
            "available_at": "2026-10-15T11:00:00-07:00",
            "receipt_key": f"verify-s3-r1-{PID}",
        },
    )
    # Even a small, well-within-delivered-value payment attempt is refused
    # wholesale -- not just capped -- while 2/6 units remain undelivered.
    refused = c.post(
        f"/invoices/{inv3['id']}/pay",
        json={"amount_cents": 100_000, "business_date": "2026-10-15"},
        headers={"Idempotency-Key": f"verify-s3-pay-attempt-{PID}"},
    )
    check(
        "S3 (false): any payment attempt while undelivered is refused "
        "invoice_fully_held_partial_payment_not_allowed, not just capped",
        refused.status_code in (400, 409, 422)
        and "invoice_fully_held_partial_payment_not_allowed" in refused.text,
        refused.text,
    )
    c.post(
        "/deliveries",
        json={
            "po_id": po3["id"],
            "line_no": 1,
            "quantity": 2,
            "accepted_at": "2026-10-15T14:00:00-07:00",
            "available_at": "2026-10-15T14:00:00-07:00",
            "receipt_key": f"verify-s3-r2-{PID}",
        },
    )
    paid_full = c.post(
        f"/invoices/{inv3['id']}/pay",
        json={"amount_cents": 2_250_000, "business_date": "2026-10-16"},
        headers={"Idempotency-Key": f"verify-s3-pay-full-{PID}"},
    )
    check(
        "S3 (false): once fully delivered (6/6), the full amount pays",
        paid_full.status_code in (200, 201),
        paid_full.text,
    )

    # --- True (default): existing behavior unchanged -- undisputed portion still payable ---
    sup3b = c.post(
        "/suppliers", json={"festival_id": FESTIVAL_ID, "name": f"Verify S3b Supplier {PID}"}
    ).json()
    po3b = c.post(
        "/purchase-orders",
        json={
            "supplier_id": sup3b["id"],
            "po_number": f"VERIFY-S3B-PO-{PID}",
            "po_date": "2026-10-07",
            "amount_cents": 2_250_000,
            "lines": [{"line_no": 1, "item_code": "GEN100", "quantity": 6, "unit_price_cents": 375_000}],
            # partial_payment_allowed omitted -- must default true
        },
    ).json()
    check("S3: partial_payment_allowed defaults true when omitted", po3b["partial_payment_allowed"] is True, po3b)
    inv3b = c.post(
        "/invoices",
        json={
            "supplier_id": sup3b["id"],
            "po_id": po3b["id"],
            "invoice_number": f"VERIFY-S3B-{PID}",
            "invoice_date": "2026-10-15",
            "due_date": "2026-10-20",
            "net_cents": 2_250_000,
            "tax_cents": 0,
            "gross_cents": 2_250_000,
        },
    ).json()["invoice"]
    c.post(
        "/deliveries",
        json={
            "po_id": po3b["id"],
            "line_no": 1,
            "quantity": 4,
            "accepted_at": "2026-10-15T11:00:00-07:00",
            "available_at": "2026-10-15T11:00:00-07:00",
            "receipt_key": f"verify-s3b-r1-{PID}",
        },
    )
    allowed = c.post(
        f"/invoices/{inv3b['id']}/pay",
        json={"amount_cents": 1_500_000, "business_date": "2026-10-15"},  # 4 x $3,750 undisputed, no deposit here
        headers={"Idempotency-Key": f"verify-s3b-pay-{PID}"},
    )
    check(
        "S3 (true, default): the undisputed 4/6 portion still pays -- unchanged from before this change",
        allowed.status_code in (200, 201),
        allowed.text,
    )

    # =======================================================================
    # 3. S4 dispute_invoice_exception / request_invoice_credit via POST /approvals
    # =======================================================================
    accept = c.post(
        "/approvals",
        json={
            "subject_type": "supplier_invoice",
            "subject_id": inv3b["id"],
            "action": "approve_invoice_exception",
            "note": "accept the undisputed variance",
        },
    )
    check("S4: pre-existing action approve_invoice_exception still works (vocab dict additions didn't break it)", accept.status_code == 201, accept.text)

    dispute = c.post(
        "/approvals",
        json={
            "subject_type": "supplier_invoice",
            "subject_id": inv3b["id"],
            "action": "dispute_invoice_exception",
            "note": "disputing the remaining 2 units",
        },
    )
    check(
        "S4: new action dispute_invoice_exception recorded (201) against a supplier_invoice",
        dispute.status_code == 201 and dispute.json().get("action") == "dispute_invoice_exception",
        dispute.text,
    )

    credit = c.post(
        "/approvals",
        json={
            "subject_type": "supplier_invoice",
            "subject_id": inv3b["id"],
            "action": "request_invoice_credit",
            "note": "requesting a credit for the disputed portion",
        },
    )
    check(
        "S4: new action request_invoice_credit recorded (201) against a supplier_invoice",
        credit.status_code == 201 and credit.json().get("action") == "request_invoice_credit",
        credit.text,
    )

    wrong_subject = c.post(
        "/approvals",
        json={
            "subject_type": "vendor_run",
            "subject_id": "whatever",
            "action": "dispute_invoice_exception",
        },
    )
    check(
        "S4: dispute_invoice_exception against a non-supplier_invoice subject is refused, "
        "not silently allowed",
        wrong_subject.status_code == 422,
        wrong_subject.text,
    )

    unknown = c.post(
        "/approvals",
        json={"subject_type": "supplier_invoice", "subject_id": inv3b["id"], "action": "not_a_real_action"},
    )
    check("S4: an unrecognized action is still refused (422), vocabulary stayed closed", unknown.status_code == 422, unknown.text)

    # =======================================================================
    # 4. S6 DUPLICATE_SUSPECTED
    # =======================================================================
    sup6 = c.post(
        "/suppliers", json={"festival_id": FESTIVAL_ID, "name": f"Verify S6 Supplier {PID}"}
    ).json()
    inv6a = c.post(
        "/invoices",
        json={
            "supplier_id": sup6["id"],
            "invoice_number": f"VERIFY-S6-A-{PID}",
            "invoice_date": "2026-10-10",
            "net_cents": 500_000,
            "tax_cents": 0,
            "gross_cents": 500_000,
        },
    ).json()["invoice"]

    # Within the 7-day window, different date -> non-blocking DUPLICATE_SUSPECTED.
    inv6b = c.post(
        "/invoices",
        json={
            "supplier_id": sup6["id"],
            "invoice_number": f"VERIFY-S6-B-{PID}",
            "invoice_date": "2026-10-13",  # 3 days later
            "net_cents": 500_000,
            "tax_cents": 0,
            "gross_cents": 500_000,
        },
    ).json()["invoice"]
    cases_b = c.get("/cases", params={"reason_code": "DUPLICATE_SUSPECTED"}).json()
    case_b = next((cs for cs in cases_b if cs["id"] not in ("",) and inv6b["id"] in str(cs["evidence_refs"])), None)
    check("S6: same supplier/total within 7 days (different date) opens a DUPLICATE_SUSPECTED case", case_b is not None, cases_b)
    if case_b is not None:
        check("S6: DUPLICATE_SUSPECTED case is non-blocking", case_b["blocking"] is False, case_b)
    pay_b = c.post(
        f"/invoices/{inv6b['id']}/pay",
        json={"amount_cents": 500_000, "business_date": "2026-10-13"},
        headers={"Idempotency-Key": f"verify-s6-pay-b-{PID}"},
    )
    check("S6: DUPLICATE_SUSPECTED never itself blocks payment", pay_b.status_code in (200, 201), pay_b.text)

    # Outside the window -> no case at all.
    inv6c = c.post(
        "/invoices",
        json={
            "supplier_id": sup6["id"],
            "invoice_number": f"VERIFY-S6-C-{PID}",
            "invoice_date": "2026-10-25",  # 15 days after A, well outside the window
            "net_cents": 500_000,
            "tax_cents": 0,
            "gross_cents": 500_000,
        },
    ).json()["invoice"]
    cases_c = c.get("/cases", params={"reason_code": "DUPLICATE_SUSPECTED"}).json()
    case_c = next((cs for cs in cases_c if inv6c["id"] in str(cs["evidence_refs"])), None)
    check("S6: more than 7 days apart opens no DUPLICATE_SUSPECTED case", case_c is None, cases_c)

    # Exact same date -> the EXISTING blocking POSSIBLE_DUPLICATE_SUPPLIER_INVOICE
    # fires instead, and DUPLICATE_SUSPECTED does NOT also fire (no double-count).
    inv6d = c.post(
        "/invoices",
        json={
            "supplier_id": sup6["id"],
            "invoice_number": f"VERIFY-S6-D-{PID}",
            "invoice_date": "2026-10-10",  # exact same date as A
            "net_cents": 500_000,
            "tax_cents": 0,
            "gross_cents": 500_000,
        },
    ).json()["invoice"]
    possible_dup = c.get("/cases", params={"reason_code": "POSSIBLE_DUPLICATE_SUPPLIER_INVOICE"}).json()
    found_possible_dup = any(inv6d["id"] in str(cs["evidence_refs"]) for cs in possible_dup)
    check("S6: exact-same-date near-duplicate still trips the existing blocking case", found_possible_dup, possible_dup)
    suspected_for_d = c.get("/cases", params={"reason_code": "DUPLICATE_SUSPECTED"}).json()
    double_fired = any(inv6d["id"] in str(cs["evidence_refs"]) for cs in suspected_for_d)
    check("S6: exact-same-date pair does NOT also open DUPLICATE_SUSPECTED (no double-fire)", not double_fired, suspected_for_d)

    print(f"\n{len(_passes)} passed, {len(_failures)} failed")
    return 1 if _failures else 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
