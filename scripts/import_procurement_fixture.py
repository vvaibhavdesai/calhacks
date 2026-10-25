"""Import fixtures/encore-procurement-synthetic-v1/ through the live A08
API -- HTTP only, idempotent, gated by --through (TICKET section 1).

Every step below is naturally idempotent server-side (unique idempotency
keys, receipt_key, UNIQUE(deposit_id, invoice_id), normalized duplicate
detection) -- re-running this script with the same or a later --through
is always safe: already-recorded facts come back as no-ops, only facts
whose trigger time has newly crossed --through get recorded.

Usage:
    python scripts/import_procurement_fixture.py --through 2026-10-15T12:30:00-07:00
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import httpx

API = os.environ.get("API_BASE_URL", "http://127.0.0.1:8010")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "LocalDevPassw0rd!")

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "encore-procurement-synthetic-v1"

FESTIVAL_NAME = "Golden Coast Music Festival"
FESTIVAL_START = "2026-10-16"
FESTIVAL_END = "2026-10-18"
SOURCE_SYSTEM = "encore_procurement_v1"

# Clause A3: $500/started hour, capped at $2,000, notice within 24h of deadline.
CREDIT_RATE_CENTS_PER_HOUR = 50_000
CREDIT_CAP_CENTS = 200_000


def _load(name: str):
    with open(FIXTURE_DIR / "data" / f"{name}.json") as f:
        return json.load(f)


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


class Importer:
    def __init__(self) -> None:
        r = httpx.post(
            f"{API}/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
        )
        r.raise_for_status()
        self.c = httpx.Client(
            base_url=f"{API}/api/v1",
            timeout=30,
            headers={"Authorization": f"Bearer {r.json()['access_token']}"},
        )

    def call(self, method: str, path: str, body: dict | None = None, ok=(200, 201)) -> dict:
        r = self.c.request(method, path, json=body)
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text}")
        return r.json()

    def find_festival(self) -> str:
        items = self.call("GET", "/festivals")["items"]
        matches = [
            f
            for f in items
            if f["name"] == FESTIVAL_NAME
            and f["start_date"] == FESTIVAL_START
            and f["end_date"] == FESTIVAL_END
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"expected exactly one festival named {FESTIVAL_NAME!r} "
                f"({FESTIVAL_START}..{FESTIVAL_END}), found {len(matches)}"
            )
        return matches[0]["id"]


def _source_ref(
    doc_id: str, version: int, content_hash: str, clause: str, excerpt: str | None = None
) -> dict:
    return {
        "source_document_id": doc_id,
        "source_version": version,
        "content_hash": content_hash,
        "locator_kind": "clause",
        "locator": clause,
        "quoted_excerpt": excerpt,
    }


def import_source(imp: Importer, native_id: str) -> tuple[str, int, str]:
    path = FIXTURE_DIR / "sources" / f"{native_id}.md"
    content_b64 = base64.b64encode(path.read_bytes()).decode()
    result = imp.call(
        "POST",
        "/imports",
        {
            "source_system": SOURCE_SYSTEM,
            "source_native_id": native_id,
            "content_base64": content_b64,
            "mime_type": "text/markdown",
        },
    )
    return result["source_document_id"], result["source_version"], result["content_hash"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--through",
        required=True,
        help="timezone-aware ISO instant; only records available_at " "<= this are written",
    )
    args = ap.parse_args()
    through = _ts(args.through)

    imp = Importer()
    festival_id = imp.find_festival()
    print(f"festival_id={festival_id}")

    source_index = {s["id"]: s for s in _load("source_index")}
    counterparties = _load("counterparties")
    suppliers_by_fixture_id: dict[str, str] = {}
    for c in counterparties:
        if c["id"] == "partner_d":
            continue  # revenue partner, never imported as a supplier
        result = imp.call(
            "POST",
            "/suppliers",
            {"festival_id": festival_id, "name": c["name"], "category": c["category"]},
        )
        suppliers_by_fixture_id[c["id"]] = result["id"]
    print(f"suppliers: {suppliers_by_fixture_id}")

    # Quotes (gated individually by each quote's own available_at).
    quotes = _load("quotes")
    quotes_by_fixture_id: dict[str, str] = {}
    for q in quotes:
        avail = _ts(source_index[q["id"]]["available_at"])
        if avail > through:
            continue
        sup_id = suppliers_by_fixture_id.get(q["supplier_id"])
        if sup_id is None:
            continue
        result = imp.call(
            "POST",
            f"/suppliers/{sup_id}/quotes",
            {
                "request_key": q["request_id"],
                "version": q["version"],
                "available_at": q["available_at"],
                "base_cents": q["base_cents"],
                "delivery_cents": q["delivery_cents"],
                "tax_cents": q["tax_cents"],
                "deposit_cents": q["deposit_cents"],
                "deposit_due": q["deposit_due"],
                "balance_due": q["balance_due"],
                "insurance_evidence": q["insurance_evidence"],
                "citations": q.get("citations", {}),
                "source_refs": [],
                # Fixture dict has e.g. "supersedes": "q_b_v1" -- a fixture
                # native id, not a real row id, so it only resolves once
                # that earlier quote has itself already been imported in
                # this same loop (quotes.json's own order, and the
                # available_at gate above, both already guarantee that).
                "supersedes_id": quotes_by_fixture_id.get(q["supersedes"])
                if q.get("supersedes")
                else None,
            },
        )
        quotes_by_fixture_id[q["id"]] = result["id"]
    print("quotes imported")

    contract_avail = _ts(source_index["contract_b_demo"]["available_at"])
    if contract_avail > through:
        print(f"contract_b_demo not yet available ({contract_avail} > {through}); stopping here")
        return 0

    doc_id, ver, chash = import_source(imp, "contract_b_demo")
    sup_b = suppliers_by_fixture_id["sup_b"]

    po_body = _load("purchase_orders")[0]  # po_gen_b
    po = imp.call(
        "POST",
        "/purchase-orders",
        {
            "supplier_id": sup_b,
            "po_number": po_body["id"],
            "po_date": "2026-10-07",
            "amount_cents": sum(line_["line_cents"] for line_ in po_body["lines"]),
            "source_refs": [_source_ref(doc_id, ver, chash, "A1")],
            "lines": [
                {
                    "line_no": i + 1,
                    "item_code": line_["item_code"],
                    "quantity": line_["qty"],
                    "unit_price_cents": line_["unit_cents"],
                }
                for i, line_ in enumerate(po_body["lines"])
            ],
        },
    )
    print(f"po={po['id']}")

    # --- deposit payment (Oct 7 12:30) ---
    payments = _load("payments")
    pay = payments[0]  # pay_deposit_b
    if _ts(pay["posted_at"]) <= through:
        deposit = imp.call(
            "POST",
            f"/suppliers/{sup_b}/deposits",
            {
                "po_id": po["id"],
                "amount_cents": pay["amount_cents"],
                "paid_business_date": "2026-10-07",
                "paid_at": pay["posted_at"],
                "idempotency_key": f"deposit:{pay['id']}",
                "source_refs": [_source_ref(doc_id, ver, chash, "A1")],
            },
        )
        print(f"deposit paid: {deposit}")

        # --- B-001 (Oct 15 12:15) ---
        invoices = _load("invoices")
        inv_b_001 = next(i for i in invoices if i["id"] == "inv_b_001")
        inv_avail = _ts(invoices[0].get("available_at") or "2026-10-15T12:15:00-07:00")
        if inv_avail <= through:
            inv_doc_id, inv_ver, inv_hash = import_source(imp, "invoice_b_001")
            inv = imp.call(
                "POST",
                "/invoices",
                {
                    "supplier_id": sup_b,
                    "po_id": po["id"],
                    "invoice_number": inv_b_001["invoice_number"],
                    "invoice_date": "2026-10-15",
                    "due_date": "2026-10-20",
                    "net_cents": inv_b_001["total_cents"],
                    "tax_cents": 0,
                    "gross_cents": inv_b_001["total_cents"],
                    "received_at": "2026-10-15T12:15:00-07:00",
                    "source_refs": [_source_ref(inv_doc_id, inv_ver, inv_hash, "A1")],
                },
            )
            invoice_id = inv["invoice"]["id"]
            print(f"invoice={invoice_id} created={inv['created']}")

            apply_result = imp.call(
                "POST",
                f"/invoices/{invoice_id}/apply-deposit",
                {"deposit_id": deposit["id"], "amount_cents": pay["amount_cents"]},
            )
            print(f"deposit applied: {apply_result['application_id']}")

            # --- duplicate submission " b-001 " (same source doc, normalized
            # match against "B-001" -- expect 409, no new obligation/payment) ---
            dup_resp = imp.c.post(
                "/invoices",
                json={
                    "supplier_id": sup_b,
                    "po_id": po["id"],
                    "invoice_number": " b-001 ",
                    "invoice_date": "2026-10-15",
                    "due_date": "2026-10-20",
                    "net_cents": inv_b_001["total_cents"],
                    "tax_cents": 0,
                    "gross_cents": inv_b_001["total_cents"],
                    "received_at": "2026-10-15T12:16:00-07:00",
                    "source_refs": [_source_ref(inv_doc_id, inv_ver, inv_hash, "A1")],
                },
            )
            print(f"duplicate ' b-001 ' -> {dup_resp.status_code}: {dup_resp.json()}")

            # --- delivery receipts ---
            sla_obs = {o["id"]: o for o in _load("sla_observations")}
            late_supplier = sla_obs["late_supplier"]
            for d in late_supplier["deliveries"]:
                if _ts(d["available_at"]) <= through:
                    rec = imp.call(
                        "POST",
                        "/deliveries",
                        {
                            "po_id": po["id"],
                            "line_no": 1,
                            "quantity": d["qty"],
                            "accepted_at": d["accepted_at"],
                            "available_at": d["available_at"],
                            "receipt_key": d["id"],
                            "source_refs": [],
                        },
                    )
                    print(f"receipt {d['id']}: created={rec['created']}")

            # --- SLA potential credit (Oct 17 21:00) ---
            assess_avail = _ts(late_supplier["assessment_evidence_available_at"])
            if assess_avail <= through:
                deadline = _ts("2026-10-15T12:00:00-07:00")
                last_accepted = max(_ts(d["accepted_at"]) for d in late_supplier["deliveries"])
                delay_minutes = int((last_accepted - deadline).total_seconds() // 60)
                notice_at = _ts(late_supplier["notice_at"])
                notice_in_time = notice_at <= deadline + timedelta(hours=24)
                potential = 0
                if (
                    late_supplier["delay_cause"] == "supplier"
                    and notice_in_time
                    and delay_minutes > 0
                ):
                    started_hours = -(-delay_minutes // 60)  # ceil
                    potential = min(started_hours * CREDIT_RATE_CENTS_PER_HOUR, CREDIT_CAP_CENTS)
                sla_result = imp.call(
                    "POST",
                    f"/invoices/{invoice_id}/sla-potential-credit",
                    {
                        "potential_credit_cents": potential,
                        "clause": "A3",
                        "evidence_refs": [_source_ref(doc_id, ver, chash, "A3")],
                        "note": (
                            f"Supplier-caused delay, {delay_minutes} minutes past the Oct 15 "
                            f"12:00 deadline, notice at {late_supplier['notice_at']} "
                            f"({'in time' if notice_in_time else 'late'}). Potential credit "
                            f"{potential} cents per clause A3 ($500/started hour, $2,000 cap). "
                            "NOT an approved credit -- requires documented human review."
                        ),
                    },
                )
                print(f"sla potential credit: {sla_result}")

    print("import complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
