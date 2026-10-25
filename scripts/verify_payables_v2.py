"""Payables v2 (deposits/delivery-limit/duplicates/SLA-credit/comparison):
real-HTTP verification against a real Postgres + a real uvicorn process.

Self-contained: it builds its own throwaway supplier/PO/invoice (fixture-
identical numbers: a 6-unit, $3,750/unit PO, an $800,000 deposit, a
$2,250,000 invoice) and drives the full proof sequence itself, in the
correct temporal order (check *before* each action, not after everything
has already happened) -- an earlier version of this script read an
ALREADY-FULLY-COMPLETED external invoice and asserted numbers that only
held at an earlier point in that invoice's own history, which is a bug in
the verification, not the product. No pytest files, per standing
convention.

NOT safely re-runnable against the same database twice: its own
DailyClose-refusal check closes 2026-10-07/2026-10-15 for the festival,
which its own main sequence needs open on the next run. Same requirement
as every other scripts/verify_*.py here -- run against a fresh database.

Every money figure is INTEGER CENTS, labelled explicitly -- e.g.
`750_000  # $7,500 (NOT $750)` -- because a prior chat report of this
same proof used the wrong notation.
"""

from __future__ import annotations

import ast
import os
import sys
from datetime import date
from pathlib import Path

import httpx

API_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8010")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "LocalDevPassw0rd!")
FESTIVAL_ID = os.environ.get("VERIFY_FESTIVAL_ID", "fest_golden_coast")

REPO_ROOT = Path(__file__).resolve().parents[1]

_passes: list[str] = []
_failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        _passes.append(label)
        print(f"PASS  {label}")
    else:
        _failures.append(f"{label} -- {detail}")
        print(f"FAIL  {label} -- {detail}")


# ---------------------------------------------------------------------------
# Static: no import/scheduler/agent module calls record_credit_note
# ---------------------------------------------------------------------------

_FORBIDDEN_DIRS = [
    REPO_ROOT / "services" / "api" / "app" / "agents",
    REPO_ROOT / "services" / "api" / "app" / "orchestration",
    REPO_ROOT / "services" / "worker",
]
_FORBIDDEN_FILES = [
    REPO_ROOT / "services" / "api" / "app" / "ingestion.py",
    REPO_ROOT / "services" / "api" / "app" / "routers" / "imports.py",
    REPO_ROOT / "services" / "api" / "app" / "scheduler.py",
]


def _calls_record_credit_note(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name == "record_credit_note":
                return True
    return False


def check_no_automated_credit_note_calls() -> None:
    offenders = []
    candidates = list(_FORBIDDEN_FILES)
    for d in _FORBIDDEN_DIRS:
        if d.exists():
            candidates += sorted(d.rglob("*.py"))
    for path in candidates:
        if _calls_record_credit_note(path):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    check(
        "no import/scheduler/agent module calls record_credit_note (AST scan)",
        not offenders,
        f"offenders: {offenders}",
    )


# ---------------------------------------------------------------------------
# Self-contained live-HTTP proof, cents everywhere, correct temporal order
# ---------------------------------------------------------------------------


def main() -> int:
    check_no_automated_credit_note_calls()

    login = httpx.post(
        f"{API_BASE_URL}/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    if login.status_code != 200:
        check("login succeeds (required for all live checks below)", False, login.text)
        print(f"\n{len(_passes)} passed, {len(_failures)} failed")
        return 1
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    c = httpx.Client(base_url=f"{API_BASE_URL}/api/v1", timeout=15.0, headers=headers)

    # --- build a fresh, fixture-identical supplier/PO/invoice ---
    sup = c.post(
        "/suppliers",
        json={"festival_id": FESTIVAL_ID, "name": f"Verify ClearPeak (standalone) {os.getpid()}"},
    ).json()
    po = c.post(
        "/purchase-orders",
        json={
            "supplier_id": sup["id"],
            "po_number": f"VERIFY-PO-{os.getpid()}",
            "po_date": "2026-10-07",
            "amount_cents": 2_250_000,  # $22,500: 6 x $3,750
            "lines": [
                {"line_no": 1, "item_code": "GEN100", "quantity": 6, "unit_price_cents": 375_000}
            ],
        },
    ).json()
    inv = c.post(
        "/invoices",
        json={
            "supplier_id": sup["id"],
            "po_id": po["id"],
            "invoice_number": f"VERIFY-B-001-{os.getpid()}",
            "invoice_date": "2026-10-15",
            "due_date": "2026-10-20",
            "net_cents": 2_250_000,
            "tax_cents": 0,
            "gross_cents": 2_250_000,  # full-contract invoice, same as the demo's B-001
        },
    ).json()["invoice"]
    deposit = c.post(
        f"/suppliers/{sup['id']}/deposits",
        json={
            "po_id": po["id"],
            "amount_cents": 800_000,  # $8,000
            "paid_business_date": "2026-10-07",
            "paid_at": "2026-10-07T12:30:00-07:00",
            "idempotency_key": f"verify-deposit-{os.getpid()}",
        },
    ).json()
    c.post(
        f"/invoices/{inv['id']}/apply-deposit",
        json={"deposit_id": deposit["id"], "amount_cents": 800_000},
    )
    c.post(
        "/deliveries",
        json={
            "po_id": po["id"],
            "line_no": 1,
            "quantity": 4,
            "accepted_at": "2026-10-15T11:00:00-07:00",
            "available_at": "2026-10-15T11:00:00-07:00",
            "receipt_key": f"verify-r1-{os.getpid()}",
        },
    )

    # --- proof (c): BEFORE paying, at 12:30 ---
    at_1230 = c.get(
        f"/invoices/{inv['id']}/payability", params={"as_of": "2026-10-15T12:30:00-07:00"}
    ).json()
    check(
        "at 12:30 (before any payment): delivery_supported_cents == 1_500_000 cents ($15,000)",
        at_1230["delivery_supported_cents"] == 1_500_000,
        at_1230,
    )
    check(
        "at 12:30 (before any payment): max_payable_now_cents == 700_000 cents "
        "($7,000, not $700)",
        at_1230["max_payable_now_cents"] == 700_000,
        at_1230,
    )
    refused = c.post(
        f"/invoices/{inv['id']}/pay",
        headers={"Idempotency-Key": f"verify-700100-{os.getpid()}"},
        json={"amount_cents": 700_100, "business_date": "2026-10-15"},
    )
    check(
        "paying 700_100 cents ($7,001) is refused (exceeds the $7,000 limit)",
        refused.status_code == 422
        and refused.json()["detail"]["code"] == "amount_exceeds_delivery_supported",
        refused.text,
    )

    paid = c.post(
        f"/invoices/{inv['id']}/pay",
        headers={"Idempotency-Key": f"verify-700000-{os.getpid()}"},
        json={"amount_cents": 700_000, "business_date": "2026-10-15"},
    ).json()
    check("paying 700_000 cents ($7,000) succeeds", paid["created"] is True, paid)
    retry = c.post(
        f"/invoices/{inv['id']}/pay",
        headers={"Idempotency-Key": f"verify-700000-{os.getpid()}"},
        json={"amount_cents": 700_000, "business_date": "2026-10-15"},
    ).json()
    check(
        "retrying the same idempotency key returns the SAME payment, not a new one",
        retry["payment"]["id"] == paid["payment"]["id"] and retry["created"] is False,
        retry,
    )

    # --- proof (f): AFTER paying $7,000, record r2, check at 14:00 ---
    c.post(
        "/deliveries",
        json={
            "po_id": po["id"],
            "line_no": 1,
            "quantity": 2,
            "accepted_at": "2026-10-15T14:00:00-07:00",
            "available_at": "2026-10-15T14:00:00-07:00",
            "receipt_key": f"verify-r2-{os.getpid()}",
        },
    )
    at_1400 = c.get(
        f"/invoices/{inv['id']}/payability", params={"as_of": "2026-10-15T14:00:00-07:00"}
    ).json()
    check(
        "at 14:00 (after the $7,000 payment, both receipts in): max_payable_now_cents == "
        "750_000 cents ($7,500 -- NOT $750)",
        at_1400["max_payable_now_cents"] == 750_000,
        at_1400,
    )
    refused_2 = c.post(
        f"/invoices/{inv['id']}/pay",
        headers={"Idempotency-Key": f"verify-750100-{os.getpid()}"},
        json={"amount_cents": 750_100, "business_date": "2026-10-15"},
    )
    check("paying 750_100 cents ($7,501) is refused", refused_2.status_code == 422, refused_2.text)

    # --- proof (h): a historical read still shows only 4 units, even though
    # r2 now exists in the database ---
    historical = c.get(
        f"/invoices/{inv['id']}/payability", params={"as_of": "2026-10-15T12:30:00-07:00"}
    ).json()
    check(
        "as_of=12:30, read NOW (after r2 exists): still accepted_units == 4",
        historical["accepted_units"] == 4,
        historical,
    )
    check(
        "as_of=12:30, read NOW: still delivery_supported_cents == 1_500_000 cents",
        historical["delivery_supported_cents"] == 1_500_000,
        historical,
    )

    # --- proof (g): SLA potential credit, guards, credit note ---
    sla = c.post(
        f"/invoices/{inv['id']}/sla-potential-credit",
        json={
            "potential_credit_cents": 100_000,  # $1,000
            "clause": "A3",
            "evidence_refs": [],
            "note": "verify script: supplier-caused delay, notice in time",
        },
    ).json()
    case_id = sla["case_id"]
    check("SLA potential credit case opened", sla["created"] is True, sla)

    case = c.get(f"/cases/{case_id}").json()
    check("the SLA case is non-blocking (Case.blocking == False)", case["blocking"] is False, case)

    approve_refused = c.post(
        f"/cases/{case_id}/resolve", json={"decision": "approve", "commission_bps": 0}
    )
    check(
        "a generic 'approve' on the SLA case is refused (422)",
        approve_refused.status_code == 422,
        approve_refused.text,
    )

    before_credit = c.get(
        f"/invoices/{inv['id']}/payability", params={"as_of": "2026-10-17T21:00:00-07:00"}
    ).json()
    check(
        "before the credit note: outstanding_cents still 750_000 cents ($7,500)",
        before_credit["outstanding_cents"] == 750_000,
        before_credit,
    )

    cn = c.post(
        f"/invoices/{inv['id']}/credit-notes",
        json={
            "credit_note_number": f"VERIFY-CN-{os.getpid()}",
            "credit_note_date": "2026-10-18",
            "net_cents": 100_000,
            "tax_cents": 0,
            "gross_cents": 100_000,
            "source_refs": [],
            "resolves_case_id": case_id,
        },
    ).json()
    check(
        "after the $1,000 credit note: outstanding_cents == 650_000 cents " "($6,500, from $7,500)",
        cn["invoice"]["outstanding_cents"] == 650_000,
        cn,
    )
    check(
        "invoice.credited_cents == 100_000 cents ($1,000, not $100,000)",
        cn["invoice"]["credited_cents"] == 100_000,
        cn,
    )
    case_after = c.get(f"/cases/{case_id}").json()
    check(
        "the SLA case is now RESOLVED as ISSUED_CREDIT_NOTE",
        case_after["status"] == "RESOLVED"
        and case_after["resolution_decision"] == "ISSUED_CREDIT_NOTE",
        case_after,
    )
    final = c.get(
        f"/invoices/{inv['id']}/payability", params={"as_of": "2026-10-18T00:00:00-07:00"}
    ).json()
    check(
        "final max_payable_now_cents == 650_000 cents ($6,500)",
        final["max_payable_now_cents"] == 650_000,
        final,
    )

    # --- duplicate submission, normalized match ---
    dup = c.post(
        "/invoices",
        json={
            "supplier_id": sup["id"],
            "po_id": po["id"],
            "invoice_number": f"  {inv['invoice_number'].strip().lower()}  ",
            "invoice_date": "2026-10-15",
            "net_cents": 2_250_000,
            "tax_cents": 0,
            "gross_cents": 2_250_000,
            "source_refs": [],
        },
    )
    check(
        "a resubmission with normalized-matching invoice_number (different case/whitespace) "
        "opens DUPLICATE_SUPPLIER_INVOICE (409), not a silent resend",
        dup.status_code == 409 and dup.json()["detail"]["code"] == "duplicate_supplier_invoice",
        dup.text,
    )

    # --- supplier comparison: evaluation/ isolation + demo-clock gating ---
    from app.routers.finance import _load_fixture_json  # noqa: E402

    try:
        _load_fixture_json("../evaluation/answer_key")
        evaluation_blocked = False
    except (ValueError, FileNotFoundError):
        evaluation_blocked = True
    check("_load_fixture_json refuses to escape data/ into evaluation/", evaluation_blocked)

    # Own request_key namespace (not the demo's "req_gen_2026") so this is a
    # genuine assertion, not a vacuous pass over an empty quote list: v1
    # available Oct 1, v2 (supersedes v1) available Oct 6 09:00 -- the real
    # fixture's own q_b_v1/q_b_v2 timing.
    request_key = f"VERIFY-REQ-{os.getpid()}"
    v1 = c.post(
        f"/suppliers/{sup['id']}/quotes",
        json={
            "request_key": request_key,
            "version": 1,
            "available_at": "2026-10-01T09:00:00-07:00",
            "base_cents": 2_100_000,
            "delivery_cents": 0,
            "tax_cents": 0,
            "deposit_cents": 1_200_000,
            "deposit_due": "2026-10-05",
            "balance_due": "2026-10-20",
            "insurance_evidence": True,
        },
    ).json()
    c.post(
        f"/suppliers/{sup['id']}/quotes",
        json={
            "request_key": request_key,
            "version": 2,
            "supersedes_id": v1["id"],
            "available_at": "2026-10-06T09:00:00-07:00",
            "base_cents": 2_250_000,
            "delivery_cents": 0,
            "tax_cents": 0,
            "deposit_cents": 800_000,
            "deposit_due": "2026-10-07",
            "balance_due": "2026-10-20",
            "insurance_evidence": True,
        },
    )

    cmp_before = c.get(
        "/supplier-comparison",
        params={"request_key": request_key, "as_of": "2026-10-02T09:00:00-07:00"},
    ).json()
    check(
        "v1 visible at as_of=2026-10-02T09:00 (endpoint genuinely returns data)",
        any(q.get("version") == 1 for q in cmp_before.get("quotes", [])),
        cmp_before,
    )
    check(
        "v2 invisible at as_of=2026-10-02T09:00 (full datetime precision, not just date)",
        not any(q.get("version") == 2 for q in cmp_before.get("quotes", [])),
        cmp_before,
    )
    cmp_after = c.get(
        "/supplier-comparison",
        params={"request_key": request_key, "as_of": "2026-10-06T09:00:00-07:00"},
    ).json()
    check(
        "v2 visible at as_of=2026-10-06T09:00 (exactly its own available_at)",
        any(q.get("version") == 2 for q in cmp_after.get("quotes", [])),
        cmp_after,
    )

    # --- DailyClose refusal: deposit payment and deposit application ---
    sup2 = c.post(
        "/suppliers",
        json={"festival_id": FESTIVAL_ID, "name": f"Verify Closed-Date Supplier {os.getpid()}"},
    ).json()
    po2 = c.post(
        "/purchase-orders",
        json={
            "supplier_id": sup2["id"],
            "po_number": f"VERIFY-PO2-{os.getpid()}",
            "po_date": "2026-10-07",
            "amount_cents": 100_000,
        },
    ).json()
    inv2 = c.post(
        "/invoices",
        json={
            "supplier_id": sup2["id"],
            "po_id": po2["id"],
            "invoice_number": f"VERIFY-INV2-{os.getpid()}",
            "invoice_date": "2026-10-15",  # while still open
            "net_cents": 100_000,
            "gross_cents": 100_000,
        },
    ).json()["invoice"]
    deposit2 = c.post(
        f"/suppliers/{sup2['id']}/deposits",
        json={
            "po_id": po2["id"],
            "amount_cents": 50_000,
            "paid_business_date": "2026-10-20",  # an unrelated, never-closed date
            "paid_at": "2026-10-20T12:00:00-07:00",
            "idempotency_key": f"verify-deposit2-{os.getpid()}",
        },
    ).json()

    from app.db import get_session_factory, make_engine
    from app.models_finance import DailyClose

    make_engine()
    db = get_session_factory()()
    try:
        for closed_date in (date(2026, 10, 7), date(2026, 10, 15)):
            existing = (
                db.query(DailyClose)
                .filter_by(festival_id=FESTIVAL_ID, business_date=closed_date)
                .first()
            )
            if existing is None:
                db.add(
                    DailyClose(
                        festival_id=FESTIVAL_ID,
                        business_date=closed_date,
                        trial_balance={},
                        checks={},
                        journal_entry_count=0,
                    )
                )
        db.commit()
    finally:
        db.close()

    deposit_refused = c.post(
        f"/suppliers/{sup2['id']}/deposits",
        json={
            "po_id": po2["id"],
            "amount_cents": 25_000,
            "paid_business_date": "2026-10-07",  # now closed
            "paid_at": "2026-10-07T12:00:00-07:00",
            "idempotency_key": f"verify-closed-deposit-{os.getpid()}",
        },
    )
    check(
        "paying a deposit on a CLOSED business_date (2026-10-07) is refused",
        deposit_refused.status_code == 409
        and deposit_refused.json().get("detail", {}).get("code") == "business_date_closed",
        deposit_refused.text,
    )
    apply_refused = c.post(
        f"/invoices/{inv2['id']}/apply-deposit",
        json={"deposit_id": deposit2["id"], "amount_cents": 25_000},
    )
    check(
        "applying a deposit against an invoice dated a now-CLOSED business_date "
        "(2026-10-15) is refused",
        apply_refused.status_code == 409
        and apply_refused.json().get("detail", {}).get("code") == "business_date_closed",
        apply_refused.text,
    )

    print(f"\n{len(_passes)} passed, {len(_failures)} failed")
    for f in _failures:
        print(f"FAILED: {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
