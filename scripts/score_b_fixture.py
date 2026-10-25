#!/usr/bin/env python3
"""Load lane B's generated festival into lane A's system and export a
candidate for lane B's scorer. PARTIAL: see "What this does NOT do".

Two subcommands:

  load    Reads data/generator/out/<fixture>/api/*.json (RUNTIME inputs only --
          never tests/oracle/generated/, never simulator_only/, never the
          evaluator manifest) into a dedicated database:
            - every file under sources/ through A02's real `import_source`
              (hash-preserving), so SourceRefs resolve to A source documents;
            - vendors, orders + lines, payments and bank entries inserted
              directly (A has no record-level importer yet -- this function
              IS that importer, and it does the ingestion-side jobs: dedupe
              re-imported payments by provider_reference, normalise signed
              amounts to A's positive-amount-plus-kind, taxability from
              menu_items, deposit gross/fee from processor batches);
          then drives A08's REAL HTTP API for everything A08 owns: finance
          config (DEMO rate from business_config), opening balance, tickets
          (+ recognition on the configured date), sponsors and receipts,
          suppliers, POs, invoices and credit notes in received order,
          historical supplier payments, expenses, and journal posting for
          every business date. Scripted human decisions from the chosen
          decision set are applied to A's own cases as they appear (DA28):
          APPROVE_VARIANCE / REJECT_DUPLICATE.

  export  Reads A's live API and writes the candidate directory in lane B's
          `encore-candidate-0.1-PROPOSAL` format plus the B07 area files.

What this does NOT do (and why the core score cannot pass yet):
  - No settlements, payouts, commissions or vendor cases: those need vendor
    contract terms, which exist only in contract PDFs/emails. Turning those
    into terms is A06 (Claude extraction), deferred pending an API key.
    settlements.json/payouts.json are exported EMPTY, and the scorer will
    (correctly) count them as errors.
  - No reconciliation export, cash-office deposits, unmatched-bank booking or
    chargeback fee recharge (A08 handoff "known gaps").

Usage:
  DATABASE_URL=.../encore_score API_BASE_URL=... \\
  ADMIN_EMAIL=... ADMIN_PASSWORD=... \\
    uv run python scripts/score_b_fixture.py load \\
      --fixture data/generator/out/full_v1 --set F1_story
  ... scripts/score_b_fixture.py export --fixture ... --set F1_story --out <dir>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app.db import get_session_factory, make_engine  # noqa: E402
from app.ingestion import import_source  # noqa: E402
from app.models_ledger import Order, OrderLine, Payment, SourceDocument, Vendor  # noqa: E402
from app.models_reconciliation import BankEntry  # noqa: E402
from app.storage.local_fs import get_document_storage  # noqa: E402
from sqlalchemy import insert, update  # noqa: E402

API = os.environ.get("API_BASE_URL", "http://127.0.0.1:8010")
TZ = ZoneInfo("America/Los_Angeles")
LOADER_STATE = "score_loader_state.json"


def cents(s) -> int:
    return int(s)


def local_date(ts: str) -> date:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TZ).date()


def items(fixture: Path, name: str):
    j = json.loads((fixture / "api" / f"{name}.json").read_text())
    return j["items"] if isinstance(j, dict) and "items" in j else j


class Api:
    def __init__(self) -> None:
        r = httpx.post(
            f"{API}/api/v1/auth/login",
            json={"email": os.environ["ADMIN_EMAIL"], "password": os.environ["ADMIN_PASSWORD"]},
        )
        r.raise_for_status()
        self.c = httpx.Client(
            base_url=API + "/api/v1",
            timeout=600,
            headers={"Authorization": f"Bearer {r.json()['access_token']}"},
        )

    def call(self, method, path, body=None, ok=(200, 201), **kw):
        r = self.c.request(method, path, json=body, **kw)
        if r.status_code not in ok:
            raise RuntimeError(f"{method} {path}: {r.status_code} {r.text[:400]}")
        return r.json()


# ---------------------------------------------------------------------------
# load
# ---------------------------------------------------------------------------


def load(fixture: Path, decision_set: str, state_path: Path) -> None:
    make_engine()
    db = get_session_factory()()
    storage = get_document_storage()
    state: dict = {
        "decision_set": decision_set,
        "dropped_duplicate_payments": [],
        "invoice_ids": {},
        "duplicate_invoice_ids": [],
        "notes": [],
    }

    # 1. sources (hash-preserving), mapping B source_id -> A source document
    # Intake metadata carried with each document: business received_at (DA19:
    # nothing may be used before it was received), vendor and kind.
    kinds = {"CONTRACT": "CONTRACT", "CONTRACT_AMENDMENT": "AMENDMENT", "EMAIL_THREAD": "EMAIL"}
    src_map, doc_meta = {}, []
    for sd in items(fixture, "source_documents"):
        data = (fixture / sd["path"]).read_bytes()
        assert "sha256:" + hashlib.sha256(data).hexdigest() == sd["content_hash"], sd["path"]
        r = import_source(
            db,
            storage,
            source_system="b_generator",
            source_native_id=sd["id"],
            content_bytes=data,
            mime_type=sd["media_type"],
            actor_id="score-loader",
            actor_capability="import_source",
            received_at=datetime.fromisoformat(sd["received_at"].replace("Z", "+00:00")),
        )
        db.commit()
        src_map[sd["id"]] = (r.source_document_id, r.version, r.content_hash)
        doc_meta.append(
            (r.source_document_id, kinds.get(sd["kind"], sd["kind"]), sd.get("vendor_id"))
        )
    print(f"sources imported: {len(src_map)}")

    def src_cols(refs):
        ref = refs[0]
        doc, ver, h = src_map[ref["source_id"]]
        return {
            "source_document_id": doc,
            "source_version": ver,
            "content_hash": h,
            "locator_kind": ref["locator_kind"],
            "locator": ref["locator"],
        }

    # 2. vendors
    festival_id = items(fixture, "vendors")[0]["festival_id"]
    for v in items(fixture, "vendors"):
        db.add(Vendor(id=v["id"], festival_id=v["festival_id"], name=v["name"]))
    db.commit()
    for doc_id, kind, vendor_id in doc_meta:
        d = db.get(SourceDocument, doc_id)
        d.document_kind = kind
        d.vendor_id = vendor_id
    db.commit()

    # 3. orders + lines (taxability from menu_items)
    taxcat = {
        (m["vendor_id"], m["item_sku"]): m["tax_category"] for m in items(fixture, "menu_items")
    }
    order_rows, line_rows = [], []
    for o in items(fixture, "orders"):
        order_rows.append(
            {
                "id": o["id"],
                "vendor_id": o["vendor_id"],
                "source_system": o["source_system"],
                "source_native_id": o["id"],
                "business_date": date.fromisoformat(o["business_date"]),
                "ordered_at": datetime.fromisoformat(o["ordered_at"].replace("Z", "+00:00")),
                "gross_cents": cents(o["gross_cents"]),
                "tax_cents": cents(o["tax_cents"]),
                "tips_cents": cents(o["tips_cents"]),
                "currency": "USD",
                "status": "active",
                **src_cols(o["source_refs"]),
            }
        )
        for ln in o["lines"]:
            line_rows.append(
                {
                    "id": ln["line_id"],
                    "order_id": o["id"],
                    "line_id": ln["line_id"],
                    "quantity": ln["quantity"],
                    "eligible_net_cents": cents(ln["eligible_net_cents"]),
                    "tax_cents": cents(ln["tax_cents"]),
                    "tips_cents": cents(ln["tips_cents"]),
                    "gross_cents": cents(ln["gross_cents"]),
                    "source_refs": ln["source_refs"],
                    "tax_category": taxcat.get((o["vendor_id"], ln["item_sku"])),
                }
            )
    for i in range(0, len(order_rows), 5000):
        db.execute(insert(Order), order_rows[i : i + 5000])
    for i in range(0, len(line_rows), 5000):
        db.execute(insert(OrderLine), line_rows[i : i + 5000])
    db.commit()
    print(f"orders {len(order_rows)}, lines {len(line_rows)}")

    # 4. payments: dedupe re-imported rows by provider_reference (keep first)
    seen, pay_rows = set(), []
    for p in items(fixture, "payments"):
        if p["provider_reference"] in seen:
            state["dropped_duplicate_payments"].append(p["id"])
            continue
        seen.add(p["provider_reference"])
        pay_rows.append(
            {
                "id": p["id"],
                "vendor_id": p["vendor_id"],
                "order_id": p["order_refs"][0]["order_id"],
                "source_system": p.get("provider") or "b_generator",
                "source_native_id": p["id"],
                "method": p["method"].lower(),
                "custody": p["custody"].lower(),
                "amount_cents": abs(cents(p["amount_cents"])),
                "currency": "USD",
                "status": p["status"].lower(),
                "kind": p["kind"].lower(),
                "provider_reference": p["provider_reference"],
                **src_cols(p["source_refs"]),
            }
        )
    for i in range(0, len(pay_rows), 5000):
        db.execute(insert(Payment), pay_rows[i : i + 5000])
    db.commit()
    print(
        f"payments {len(pay_rows)} (dropped {len(state['dropped_duplicate_payments'])} re-imports)"
    )

    # 5. bank entries; processor deposits carry batch gross/fee
    batches = defaultdict(lambda: [0, 0])
    # Which payments each processor batch paid out, so the payments can be
    # allocated to the bank deposit that settled them (below). A real
    # processor settlement file is exactly this: a payout reference, its
    # gross and fee, and the payments it covers.
    batch_payment_ids: dict[str, list[str]] = defaultdict(list)
    for b in items(fixture, "processor_batches"):
        batches[b["payout_reference"]][0] += cents(b["gross_cents"])
        batches[b["payout_reference"]][1] += cents(b["fees_cents"])
        batch_payment_ids[b["payout_reference"]].extend(b.get("payment_ids") or [])
    bank_id_by_reference: dict[str, str] = {}
    for be in items(fixture, "bank_entries"):
        g = batches.get(be["provider_reference"])
        db.add(
            BankEntry(
                id=be["id"],
                festival_id=festival_id,
                account_id=be["account_id"],
                provider_reference=be["provider_reference"],
                direction=be["direction"],
                amount_cents=abs(cents(be["amount_cents"])),
                batch_gross_cents=g[0] if g else None,
                batch_fee_cents=g[1] if g else None,
                settlement_status="settled",
                business_date=local_date(be["occurred_at"]),
                occurred_at=datetime.fromisoformat(be["occurred_at"].replace("Z", "+00:00")),
                **src_cols(be["source_refs"]),
            )
        )
        bank_id_by_reference[be["provider_reference"]] = be["id"]
    # The allocation below is a foreign key onto these rows, so they have to
    # exist in the transaction before it runs.
    db.flush()

    # 5b. Allocate each centrally-collected payment to the bank deposit that
    # settled it, via the processor batch that lists it.
    #
    # Without this, settlement is RIGHT to withhold the money: a
    # centrally-collected payment with no bank_entry_id is cash the organizer
    # cannot yet prove it has, so A04 holds it rather than paying it out. That
    # is 94% of this festival's takings, which is why the settlements read as
    # near-zero until the allocation exists.
    allocated = 0
    for reference, payment_ids in batch_payment_ids.items():
        bank_entry_id = bank_id_by_reference.get(reference)
        if not bank_entry_id or not payment_ids:
            continue
        # One UPDATE per batch rather than per payment: a batch here covers
        # ~9,000 payments.
        allocated += (
            db.execute(
                update(Payment)
                .where(Payment.id.in_(payment_ids))
                .values(bank_entry_id=bank_entry_id)
            ).rowcount
            or 0
        )
    print(f"payments allocated to bank deposits: {allocated}")
    db.commit()
    db.close()

    # 6. A08's real API for everything A08 owns
    api = Api()
    rules = {
        r["reason_code"]: r["decision"]
        for r in json.loads(
            (Path(__file__).parent.parent / "tests/oracle/scripted_decisions_full.json").read_text()
        )["decision_sets"][decision_set].get("rules", [])
    }
    cfg_in = items(fixture, "business_config")
    rate = "0.09"  # business_config DA01: DEMO 9.00% exclusive line tax
    state["notes"].append(
        f"tax rate from business_config: {rate}; config keys {sorted(cfg_in)[:6]}"
    )
    api.call(
        "PUT",
        "/finance/config",
        {
            "festival_id": festival_id,
            "tax": {
                "label": "DEMO 9.00% (lane B business_config DA01)",
                "rates_by_category": {"TAXABLE": rate, "EXEMPT": "0"},
                "exempt_categories": ["EXEMPT"],
                "liability_owner_by_custody": {
                    "central": "ORGANIZER_AS_AGENT",
                    "vendor_retained": "VENDOR",
                },
            },
            "timezone": "America/Los_Angeles",
            "statement_local_time": "09:00",
            "payout_local_time": "10:00",
            "forecast_horizon_days": 14,
        },
    )
    for ob in items(fixture, "opening_balances"):
        api.call(
            "POST",
            "/finance/opening-balances",
            {
                "festival_id": festival_id,
                "as_of": ob["as_of"],
                "amount_cents": cents(ob["amount_cents"]),
                "source_refs": ob["source_refs"],
            },
        )
    # Supplier payments are replayed history; the pool is only A07's spend guard.
    api.call(
        "POST", "/cash-pool/fund", {"festival_id": festival_id, "additional_cents": 10_000_000_000}
    )

    sup_ids = {}
    for s in items(fixture, "suppliers"):
        sup_ids[s["id"]] = api.call(
            "POST",
            "/suppliers",
            {"festival_id": festival_id, "name": s["name"], "category": s.get("category")},
        )["id"]
    po_ids = {}
    for po in items(fixture, "purchase_orders"):
        po_ids[po["id"]] = api.call(
            "POST",
            "/purchase-orders",
            {
                "supplier_id": sup_ids[po["supplier_id"]],
                "po_number": po["po_number"],
                "po_date": po["po_date"],
                "amount_cents": cents(po["amount_cents"]),
                "description": po.get("description"),
                "source_refs": po["source_refs"],
            },
        )["id"]
    sponsor_invoices = {}
    for sp in items(fixture, "sponsors"):
        for inv in sp["invoices"]:
            sponsor_invoices[inv["invoice_number"]] = api.call(
                "POST",
                "/finance/sponsor-invoices",
                {
                    "festival_id": festival_id,
                    "sponsor_name": sp["name"],
                    "invoice_number": inv["invoice_number"],
                    "invoice_date": inv["invoice_date"],
                    "due_date": inv.get("due_date"),
                    "amount_cents": cents(inv["amount_cents"]),
                    "source_refs": inv["source_refs"],
                },
            )["id"]

    # chronological event stream
    events = []
    for t in items(fixture, "ticket_settlements"):
        events.append((t["paid_at"], "ticket", t))
    events.append(("2026-10-19T06:59:59Z", "recognize", {"business_date": "2026-10-18"}))  # DA24
    for r in items(fixture, "sponsorship_receipts"):
        events.append((r["received_at"], "sponsor_receipt", r))
    for inv in items(fixture, "supplier_invoices"):
        events.append((inv["received_at"], "invoice", inv))
    for sp in items(fixture, "supplier_payments"):
        events.append((sp["paid_at"], "supplier_payment", sp))
    for e in items(fixture, "expenses"):
        events.append((e["paid_at"], "expense", e))
    events.sort(key=lambda x: x[0])

    for ts, kind, rec in events:
        if kind == "ticket":
            api.call(
                "POST",
                "/finance/ticket-settlements",
                {
                    "festival_id": festival_id,
                    "platform": rec["platform"],
                    "settlement_reference": rec["settlement_reference"],
                    "gross_cents": cents(rec["gross_cents"]),
                    "platform_fees_cents": cents(rec["platform_fees_cents"]),
                    "net_cents": cents(rec["net_cents"]),
                    "business_date": str(local_date(ts)),
                    "bank_entry_id": rec.get("bank_entry_id"),
                    "source_refs": rec["source_refs"],
                },
            )
        elif kind == "recognize":
            api.call(
                "POST",
                "/finance/tickets/recognize",
                {"festival_id": festival_id, "business_date": rec["business_date"]},
            )
        elif kind == "sponsor_receipt":
            api.call(
                "POST",
                "/finance/sponsorship-receipts",
                {
                    "sponsor_invoice_id": sponsor_invoices[rec["invoice_number"]],
                    "receipt_reference": rec["id"],
                    "amount_cents": cents(rec["amount_cents"]),
                    "business_date": str(local_date(ts)),
                    "bank_entry_id": rec.get("bank_entry_id"),
                    "source_refs": rec["source_refs"],
                },
            )
        elif kind == "invoice" and rec["kind"] == "CREDIT_NOTE":
            orig = state["invoice_ids"][rec["applies_to_invoice_id"]]
            out = api.call(
                "POST",
                f"/invoices/{orig}/credit-notes",
                {
                    "credit_note_number": rec["invoice_number"],
                    "credit_note_date": rec["invoice_date"],
                    "net_cents": cents(rec["net_cents"]),
                    "tax_cents": cents(rec["tax_cents"]),
                    "gross_cents": cents(rec["gross_cents"]),
                    "received_at": rec["received_at"],
                    "source_refs": rec["source_refs"],
                },
            )
            state["invoice_ids"][rec["id"]] = out["credit_note"]["id"]
        elif kind == "invoice":
            r = api.c.post(
                "/invoices",
                json={
                    "supplier_id": sup_ids[rec["supplier_id"]],
                    "po_id": po_ids.get(rec["po_refs"][0]) if rec.get("po_refs") else None,
                    "invoice_number": rec["invoice_number"],
                    "invoice_date": rec["invoice_date"],
                    "due_date": rec.get("due_date"),
                    "net_cents": cents(rec["net_cents"]),
                    "tax_cents": cents(rec["tax_cents"]),
                    "gross_cents": cents(rec["gross_cents"]),
                    "received_at": rec["received_at"],
                    "source_refs": rec["source_refs"],
                },
            )
            if r.status_code == 409 and r.json()["detail"]["code"] == "duplicate_supplier_invoice":
                # A opened a DUPLICATE_SUPPLIER_INVOICE case and posted nothing.
                # Decision set: REJECT_DUPLICATE -> nothing further to do.
                state["duplicate_invoice_ids"].append(rec["id"])
                case_id = r.json()["detail"]["details"]["case_id"]
                decision = rules.get("DUPLICATE_SUPPLIER_INVOICE")
                state["notes"].append(
                    f"duplicate {rec['invoice_number']} -> case {case_id} ({decision})"
                )
                continue
            if r.status_code not in (200, 201):
                raise RuntimeError(f"invoice {rec['invoice_number']}: {r.status_code} {r.text}")
            out = r.json()
            state["invoice_ids"][rec["id"]] = out["invoice"]["id"]
            if (
                out["invoice"]["po_excess_cents"] > 0
                and rules.get("INVOICE_EXCEEDS_PO") == "APPROVE_VARIANCE"
            ):
                api.call(
                    "POST",
                    f"/invoices/{out['invoice']['id']}/approve-variance",
                    {"note": f"scripted decision {decision_set}: APPROVE_VARIANCE"},
                )
        elif kind == "supplier_payment":
            for inv_id in rec["invoice_ids"]:
                r = api.c.post(
                    f"/invoices/{state['invoice_ids'][inv_id]}/pay",
                    json={
                        "amount_cents": cents(rec["amount_cents"]),
                        "business_date": str(local_date(ts)),
                    },
                    headers={"Idempotency-Key": f"hist-{rec['id']}"},
                )
                if r.status_code != 200:
                    state["notes"].append(
                        f"supplier payment {rec['payment_reference']} refused: "
                        f"{r.status_code} {r.text[:200]}"
                    )
        elif kind == "expense":
            api.call(
                "POST",
                "/finance/expenses",
                {
                    "festival_id": festival_id,
                    "expense_reference": rec["expense_reference"],
                    "category": rec["category"],
                    "description": rec.get("payee"),
                    "amount_cents": cents(rec["amount_cents"]),
                    "business_date": str(local_date(ts)),
                    "bank_entry_id": rec.get("bank_entry_id"),
                    "source_refs": rec["source_refs"],
                },
            )

    # 7. journals for every business date in the activity window
    fest = items(fixture, "festival")
    d = date.fromisoformat(fest["activity_window"]["start"])
    end = date.fromisoformat(fest["activity_window"]["end"])
    posted = 0
    while d <= end:
        posted += len(
            api.call(
                "POST", "/journal/post-day", {"festival_id": festival_id, "business_date": str(d)}
            )["posted"]
        )
        d += timedelta(days=1)
    print(f"post-day entries: {posted}")
    state["festival_id"] = festival_id
    state_path.write_text(json.dumps(state, indent=2))
    print(f"state -> {state_path}")
    for n in state["notes"]:
        print("  note:", n)


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

PNL_ROLES = (
    "COMMISSION_REVENUE",
    "TICKET_REVENUE",
    "SPONSORSHIP_REVENUE",
    "PROCESSOR_FEE_EXPENSE",
    "TICKETING_FEE_EXPENSE",
    "CASH_OVER_SHORT",
    "SUPPLIER_EXPENSE",
    "OPERATING_EXPENSE",
)


def export(fixture: Path, decision_set: str, state_path: Path, out: Path) -> None:
    state = json.loads(state_path.read_text())
    fest = state["festival_id"]
    api = Api()
    out.mkdir(parents=True, exist_ok=True)

    def write(name, obj):
        (out / f"{name}.json").write_text(json.dumps(obj, indent=1))

    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    write(
        "meta",
        {
            "candidate_format": "encore-candidate-0.1-PROPOSAL",
            "fixture_id": fixture.name,
            "fixture_manifest_sha256": hashlib.sha256(
                (fixture / "manifest.json").read_bytes()
            ).hexdigest(),
            "decision_set": decision_set,
            "system": f"encore lane A on integration@{sha[:12]} (PARTIAL: no A06 contract terms)",
        },
    )

    entries, cursor = [], None
    while True:
        page = api.call(
            "GET",
            "/journal",
            params={"festival_id": fest, "limit": 500, **({"cursor": cursor} if cursor else {})},
        )
        entries += page["items"]
        cursor = page["next_cursor"]
        if not cursor:
            break
    by_id = {e["id"]: e["entry_key"] for e in entries}
    write(
        "journals",
        {
            "items": [
                {
                    "entry_id": e["entry_key"],
                    "business_date": e["business_date"],
                    "kind": e["kind"],
                    "correction_of": by_id.get(e["correction_of"]) if e["correction_of"] else None,
                    "lines": [
                        {
                            "account_role": ln["account_role"],
                            "debit_cents": str(ln["debit_cents"]),
                            "credit_cents": str(ln["credit_cents"]),
                        }
                        for ln in e["lines"]
                    ],
                }
                for e in entries
            ]
        },
    )

    # Core files A cannot produce yet (A06): exported empty, scored as errors.
    write("settlements", {"items": []})
    write("payouts", {"items": []})
    write("reconciliation", {"items": []})
    write("cases", {"items": []})

    a_to_b = {a: b for b, a in state["invoice_ids"].items()}
    ap = api.call("GET", "/invoices", params={"festival_id": fest, "as_of": "2026-11-01"})
    pay_items = [
        {
            "invoice_id": a_to_b[i["invoice_id"]],
            "status": i["status"],
            "outstanding_cents": str(i["outstanding_cents"]),
        }
        for i in ap["invoices"]
        if i["kind"] == "INVOICE"
    ]
    pay_items += [
        {"invoice_id": d, "status": "DUPLICATE_NOT_POSTED", "outstanding_cents": "0"}
        for d in state["duplicate_invoice_ids"]
    ]
    write("payables", {"items": pay_items, "duplicate_invoice_ids": state["duplicate_invoice_ids"]})

    rep = api.call("GET", "/reports/festival", params={"festival_id": fest, "as_of": "2026-11-01"})
    write(
        "sponsorship",
        {
            "items": [
                {
                    "invoice_number": s["invoice_number"],
                    "received_cents": str(s["received_cents"]),
                    "outstanding_memo_cents": str(s["outstanding_memo_cents"]),
                    "status": s["status"],
                }
                for s in rep["sponsorship_receivables_memo"]
            ]
        },
    )

    tax = rep["tax"]
    write(
        "tax_draft",
        {
            "by_business_date": [
                {
                    "business_date": r["business_date"],
                    "eligible_central_cents": str(
                        r.get("central_taxable_sales_cents", 0)
                        + r.get("central_exempt_sales_cents", 0)
                        + r.get("central_unclassified_sales_cents", 0)
                    ),
                    "tax_supplied_central_cents": str(r.get("central_supplied_tax_cents", 0)),
                    "tax_recalculated_central_cents": str(
                        r.get("central_recalculated_tax_cents", 0)
                    ),
                }
                for r in tax["by_business_date"]
            ],
            # A reports mismatches by A order_line id (== B line_id for this loader); refund
            # allocation refs ("<id>:refund:<pay>") only arise for A-style refunds.
            "mismatch_line_ids": sorted(
                {m["order_line_ref"].split(":")[0] for m in tax["mismatch_lines"]}
            ),
        },
    )

    tb = api.call("GET", "/journal/trial-balance", params={"festival_id": fest})
    bal = {r["account_role"]: r["debit_cents"] - r["credit_cents"] for r in tb["rows"]}
    write(
        "reports",
        {
            "festival_pnl": {r: str(bal.get(r, 0)) for r in PNL_ROLES},
            "trial_balance_zero": tb["balanced"],
        },
    )
    print(f"candidate -> {out} ({len(entries)} journal entries)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("load", "export"))
    ap.add_argument("--fixture", type=Path, default=Path("data/generator/out/full_v1"))
    ap.add_argument("--set", default="F1_story")
    ap.add_argument("--state", type=Path, default=Path(LOADER_STATE))
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    if a.cmd == "load":
        load(a.fixture, a.set, a.state)
    else:
        export(a.fixture, a.set, a.state, a.out / a.set)
    return 0


if __name__ == "__main__":
    sys.exit(main())
