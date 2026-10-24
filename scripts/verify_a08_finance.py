#!/usr/bin/env python3
"""Manual, real-HTTP verification of A08 payables, accounting, tax, cash and
reports.

*** NOT an automated test suite. *** Same convention as
scripts/verify_a05_policy.py and scripts/verify_a07_payouts.py: operational
inputs are seeded by scripts/dev_fixtures_a08.py (hand-checked, NOT B01 --
see its docstring for the full hand-worked Night 1 numbers), then every A08
behavior is driven over real HTTP against a real `uvicorn` and Postgres,
and the responses are compared to hand-worked expectations. Each run uses
its own festival id, so runs are independent.

Covers TASK_IMPLEMENTATION.md A08 acceptance: all journals balance; reports
reconcile; paid invoices cannot be paid twice; tax source totals match;
expenses and other income appear in cash/P&L correctly; every view has a
live API. Plus: duplicate/possible-duplicate invoices, PO variance
approval, credit-note recompute, deferred ticket recognition, protected
tax/tips, provisional 9 AM statements before the 10 AM payout and FINAL
after confirmation, daily close, closed-date refusal, late refund and
settlement correction as linked adjustments, concurrent posting
idempotency, and DB-level immutability for the app role.

Usage:
    1. Postgres migrated to head; scripts/seed_admin.py run (grants
       manage_payables/close_books).
    2. uvicorn app.main:app at API_BASE_URL with packages/domain on
       PYTHONPATH.
    3. DATABASE_URL=... API_BASE_URL=http://127.0.0.1:8010 \\
       ADMIN_EMAIL=... ADMIN_PASSWORD=... \\
       uv run python scripts/verify_a08_finance.py
"""

from __future__ import annotations

import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import psycopg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app.db import get_session_factory, make_engine  # noqa: E402

import dev_fixtures_a08 as fx  # noqa: E402

API_BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8010")
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "LocalDevPassw0rd!")
APP_ROLE_DSN = os.environ.get(
    "ENCORE_APP_DSN", "postgresql://encore_app:encore_app_dev_password@localhost:5432/encore"
)

RUN = uuid.uuid4().hex[:8]
FEST = f"gcmf-2026-a08verify-{RUN}"
D0, D1, D2 = fx.D0, fx.D1, fx.D2

_failures: list[str] = []
_passes: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        _passes.append(label)
        print(f"PASS  {label}")
    else:
        _failures.append(f"{label} -- {detail}")
        print(f"FAIL  {label} -- {detail}")


def login() -> dict:
    with httpx.Client(base_url=API_BASE_URL, timeout=30) as c:
        r = c.post("/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}


class Api:
    def __init__(self, headers: dict) -> None:
        self.c = httpx.Client(base_url=API_BASE_URL, timeout=60, headers=headers)
        self.headers = headers

    def post(self, path, body=None, **headers):
        return self.c.post("/api/v1" + path, json=body or {}, headers=headers)

    def put(self, path, body):
        return self.c.put("/api/v1" + path, json=body)

    def get(self, path, **params):
        return self.c.get("/api/v1" + path, params=params)

    def ok(self, method, path, *args, expect=(200, 201), **kw):
        r = getattr(self, method)(path, *args, **kw)
        assert r.status_code in expect, f"{method} {path}: {r.status_code} {r.text}"
        return r.json()


def code_of(r: httpx.Response):
    try:
        d = r.json().get("detail")
    except ValueError:
        return None
    return d.get("code") if isinstance(d, dict) else None


def concurrently(fns):
    barrier = threading.Barrier(len(fns))

    def run(fn):
        barrier.wait()
        return fn()

    with ThreadPoolExecutor(max_workers=len(fns)) as pool:
        return list(pool.map(run, fns))


def tb_role(api: Api, role: str, through=None) -> int:
    """Signed debit-positive balance for an account role."""
    params = {"festival_id": FEST}
    if through:
        params["through"] = through
    tb = api.ok("get", "/journal/trial-balance", **params)
    row = next(r for r in tb["rows"] if r["account_role"] == role)
    return row["debit_cents"] - row["credit_cents"]


def journal(api: Api, **params) -> list[dict]:
    items, cursor = [], None
    while True:
        extra = {"cursor": cursor} if cursor else {}
        page = api.ok("get", "/journal", festival_id=FEST, **extra, **params)
        items += page["items"]
        cursor = page["next_cursor"]
        if not cursor:
            return items


def main() -> int:  # noqa: C901 -- linear acceptance walkthrough
    make_engine()
    Session = get_session_factory()
    db = Session()
    m = fx.manifest(db, RUN)
    ids = fx.seed_night1(db, FEST, m)
    api = Api(login())
    print(f"verify_a08_finance: run={RUN} festival={FEST}")

    # --- config ----------------------------------------------------------
    cfg = api.ok("get", "/finance/config", festival_id=FEST)
    check(
        "config: default DEMO config v1, 09:00 statements / 10:00 payout, 14-day horizon",
        cfg["version"] == 1
        and cfg["statement_local_time"] == "09:00"
        and cfg["payout_local_time"] == "10:00"
        and cfg["forecast_horizon_days"] == 14
        and "DEMO" in cfg["label"],
        cfg,
    )

    # --- post Night 1 concurrently: idempotent under concurrency ----------
    apis = [Api(api.headers) for _ in range(5)]
    results = concurrently(
        [
            lambda a=a: a.post("/journal/post-day", {"festival_id": FEST, "business_date": str(D1)})
            for a in apis
        ]
    )
    check(
        "post-day: 5 concurrent calls all 200",
        all(r.status_code == 200 for r in results),
        [(r.status_code, r.text[:120]) for r in results],
    )
    posted = [k for r in results for k in r.json()["posted"]]
    check(
        "post-day: each fact posted exactly once across concurrent calls",
        len(posted) == len(set(posted)) and not any("#adj" in k for k in posted),
        posted,
    )
    again = api.ok("post", "/journal/post-day", {"festival_id": FEST, "business_date": str(D1)})
    check("post-day: re-run posts nothing new", again["posted"] == [], again)

    entries = journal(api, business_date=str(D1))
    sales_1100 = next(e for e in entries if e["entry_key"] == f"SALES:{ids['taco']}:{D1}:1100")
    lines = {
        ln["account_role"]: (ln["debit_cents"], ln["credit_cents"]) for ln in sales_1100["lines"]
    }
    check(
        "sales (card, net of 20% refund): Dr clearing 11840 / Cr vendor 10600, tax 840, tips 400",
        lines
        == {
            "PROCESSOR_CLEARING": (11_840, 0),
            "VENDOR_FUNDS_PAYABLE": (0, 10_600),
            "SALES_TAX_LIABILITY": (0, 840),
            "TIPS_LIABILITY": (0, 400),
        },
        lines,
    )
    check(
        "sales: organizer revenue is not credited by sales capture",
        all(ln["account_role"] != "COMMISSION_REVENUE" for ln in sales_1100["lines"]),
    )
    check(
        "vendor-retained sales post nothing",
        not [e for e in entries if e["entry_key"].startswith(f"SALES:{ids['retained']}")],
    )
    drill = api.ok("get", f"/journal/{sales_1100['id']}")
    kinds = {s["record_kind"] for s in drill["sources"]}
    check(
        "drill-down: sales entry resolves to payment and order records with source refs",
        {"payment", "order"} <= kinds and all(s["source_refs"] for s in drill["sources"]),
        drill["sources"][:3],
    )

    # --- other income / expense ------------------------------------------
    api.ok(
        "post",
        "/finance/opening-balances",
        {"festival_id": FEST, "as_of": str(D0), "amount_cents": 1_000_000},
    )
    r = api.post(
        "/finance/opening-balances", {"festival_id": FEST, "as_of": str(D0), "amount_cents": 999}
    )
    check("opening balance: different amount for same account -> 409", r.status_code == 409, r.text)
    api.ok(
        "post",
        "/finance/ticket-settlements",
        {
            "festival_id": FEST,
            "platform": "TIXSIM",
            "settlement_reference": "TIX-1",
            "gross_cents": 200_000,
            "platform_fees_cents": 10_000,
            "net_cents": 190_000,
            "business_date": str(D0),
        },
    )
    check(
        "tickets: presale is deferred revenue (2400 credit 200000)",
        tb_role(api, "DEFERRED_TICKET_REVENUE") == -200_000,
    )
    si = api.ok(
        "post",
        "/finance/sponsor-invoices",
        {
            "festival_id": FEST,
            "sponsor_name": "Sim Sponsor",
            "invoice_number": "SP-1",
            "invoice_date": str(D0),
            "amount_cents": 100_000,
        },
    )
    api.ok(
        "post",
        "/finance/sponsorship-receipts",
        {
            "sponsor_invoice_id": si["id"],
            "receipt_reference": "SR-1",
            "amount_cents": 60_000,
            "business_date": str(D1),
        },
    )
    r = api.post(
        "/finance/sponsorship-receipts",
        {
            "sponsor_invoice_id": si["id"],
            "receipt_reference": "SR-2",
            "amount_cents": 50_000,
            "business_date": str(D1),
        },
    )
    check("sponsorship: receipt beyond invoice -> 409", r.status_code == 409, r.text)
    api.ok(
        "post",
        "/finance/expenses",
        {
            "festival_id": FEST,
            "expense_reference": "EXP-1",
            "category": "security",
            "amount_cents": 15_000,
            "business_date": str(D1),
        },
    )

    # --- payables --------------------------------------------------------
    api.ok("post", "/cash-pool/fund", {"festival_id": FEST, "additional_cents": 2_000_000})
    sup = api.ok("post", "/suppliers", {"festival_id": FEST, "name": "Ice Co", "category": "ice"})
    po = api.ok(
        "post",
        "/purchase-orders",
        {
            "supplier_id": sup["id"],
            "po_number": "PO-1",
            "po_date": str(D0),
            "amount_cents": 50_000,
            "receipt_evidence_refs": [{"kind": "delivery_note", "ref": "DN-1"}],
        },
    )
    src_a = [m.ref("invoice:INV-A")]
    inv_a = api.ok(
        "post",
        "/invoices",
        {
            "supplier_id": sup["id"],
            "po_id": po["id"],
            "invoice_number": "INV-A",
            "invoice_date": str(D1),
            "due_date": "2026-11-15",
            "net_cents": 27_500,
            "tax_cents": 2_500,
            "gross_cents": 30_000,
            "source_refs": src_a,
        },
    )
    a_id = inv_a["invoice"]["id"]
    check(
        "invoice A within PO: OPEN, no cases",
        inv_a["invoice"]["status"] == "OPEN" and inv_a["case_ids"] == [],
        inv_a,
    )
    retry = api.ok(
        "post",
        "/invoices",
        {
            "supplier_id": sup["id"],
            "po_id": po["id"],
            "invoice_number": "INV-A",
            "invoice_date": str(D1),
            "due_date": "2026-11-15",
            "net_cents": 27_500,
            "tax_cents": 2_500,
            "gross_cents": 30_000,
            "source_refs": src_a,
        },
    )
    check(
        "invoice A identical retry: same invoice, created=False",
        retry["invoice"]["id"] == a_id and not retry["created"],
    )
    ap_before = tb_role(api, "SUPPLIER_AP")
    r = api.post(
        "/invoices",
        {
            "supplier_id": sup["id"],
            "po_id": po["id"],
            "invoice_number": "INV-A",
            "invoice_date": str(D1),
            "due_date": "2026-11-15",
            "net_cents": 27_500,
            "tax_cents": 2_500,
            "gross_cents": 30_000,
            "source_refs": [m.ref("invoice:INV-A:resent-copy")],
        },
    )
    check(
        "duplicate invoice (same number, different document): 409 with a case",
        r.status_code == 409
        and code_of(r) == "duplicate_supplier_invoice"
        and r.json()["detail"]["details"].get("case_id"),
        r.text,
    )
    check("duplicate invoice: nothing posted", tb_role(api, "SUPPLIER_AP") == ap_before)

    inv_c = api.ok(
        "post",
        "/invoices",
        {
            "supplier_id": sup["id"],
            "invoice_number": "INV-C",
            "invoice_date": str(D1),
            "due_date": "2026-11-15",
            "net_cents": 30_000,
            "tax_cents": 0,
            "gross_cents": 30_000,
            "source_refs": [m.ref("invoice:INV-C")],
        },
    )
    c_id = inv_c["invoice"]["id"]
    check(
        "possible duplicate (new number, same supplier/amount/date): recorded with a case",
        inv_c["created"] and len(inv_c["case_ids"]) == 1,
        inv_c,
    )

    inv_b = api.ok(
        "post",
        "/invoices",
        {
            "supplier_id": sup["id"],
            "po_id": po["id"],
            "invoice_number": "INV-B",
            "invoice_date": str(D2),
            "due_date": "2026-10-31",
            "net_cents": 25_000,
            "tax_cents": 0,
            "gross_cents": 25_000,
            "source_refs": [m.ref("invoice:INV-B")],
        },
    )
    b_id = inv_b["invoice"]["id"]
    check(
        "invoice B exceeds PO by 5000: PAYMENT_BLOCKED with a case",
        inv_b["invoice"]["po_excess_cents"] == 5_000
        and inv_b["invoice"]["status"] == "PAYMENT_BLOCKED"
        and len(inv_b["case_ids"]) == 1,
        inv_b,
    )
    cn = api.ok(
        "post",
        f"/invoices/{a_id}/credit-notes",
        {
            "credit_note_number": "CN-1",
            "credit_note_date": str(D2),
            "net_cents": 2_000,
            "tax_cents": 0,
            "gross_cents": 2_000,
            "source_refs": [m.ref("invoice:CN-1")],
        },
    )
    check("credit note on A: A outstanding 28000", cn["invoice"]["outstanding_cents"] == 28_000, cn)
    b_now = api.ok("get", f"/invoices/{b_id}")
    check(
        "credit note on A recomputes B's PO excess 5000 -> 3000",
        b_now["po_excess_cents"] == 3_000,
        b_now,
    )
    cn_entry = api.ok("get", f"/journal/{cn['credit_note']['journal_entry_id']}")
    check(
        "credit note journal links correction_of = invoice A's entry",
        cn_entry["correction_of"] == inv_a["invoice"]["journal_entry_id"],
        cn_entry,
    )

    # pay A twice concurrently with different keys
    a1, a2 = Api(api.headers), Api(api.headers)
    body = {"amount_cents": 28_000, "business_date": str(D2)}
    r1, r2 = concurrently(
        [
            lambda: a1.post(f"/invoices/{a_id}/pay", body, **{"Idempotency-Key": f"{RUN}-payA-1"}),
            lambda: a2.post(f"/invoices/{a_id}/pay", body, **{"Idempotency-Key": f"{RUN}-payA-2"}),
        ]
    )
    statuses = sorted([r1.status_code, r2.status_code])
    loser = r1 if r1.status_code == 422 else r2
    check(
        "pay A: two concurrent payments -> exactly one 200, one 422 invoice_already_paid",
        statuses == [200, 422] and code_of(loser) == "invoice_already_paid",
        [(r1.status_code, r1.text[:100]), (r2.status_code, r2.text[:100])],
    )
    winner_key = f"{RUN}-payA-1" if r1.status_code == 200 else f"{RUN}-payA-2"
    r = api.post(f"/invoices/{a_id}/pay", body, **{"Idempotency-Key": winner_key})
    check(
        "pay A: retry with the winning key returns the same payment (created=False)",
        r.status_code == 200 and not r.json()["created"],
        r.text,
    )
    r = api.post(
        f"/invoices/{a_id}/pay",
        {"amount_cents": 1, "business_date": str(D2)},
        **{"Idempotency-Key": f"{RUN}-payA-3"},
    )
    check(
        "pay A again: paid invoice cannot be paid twice (422)",
        r.status_code == 422 and code_of(r) == "invoice_already_paid",
        r.text,
    )
    r = api.post(f"/invoices/{a_id}/pay", body)
    check("pay without Idempotency-Key header -> 422", r.status_code == 422, r.text)

    r = api.post(
        f"/invoices/{b_id}/pay",
        {"amount_cents": 25_000, "business_date": str(D2)},
        **{"Idempotency-Key": f"{RUN}-payB-1"},
    )
    check(
        "pay B before variance approval -> 422",
        r.status_code == 422 and code_of(r) == "invoice_exceeds_po_variance_not_approved",
        r.text,
    )
    v = api.ok("post", f"/invoices/{b_id}/approve-variance", {"note": "extra ice for heatwave"})
    check("finance lead approves B's variance -> OPEN", v["status"] in ("OPEN", "OVERDUE"), v)
    r = api.post(
        f"/invoices/{b_id}/pay",
        {"amount_cents": 25_000, "business_date": str(D2)},
        **{"Idempotency-Key": f"{RUN}-payB-2"},
    )
    check("pay B after approval -> 200", r.status_code == 200, r.text)
    r = api.post(
        f"/invoices/{c_id}/pay",
        {"amount_cents": 30_000, "business_date": str(D2)},
        **{"Idempotency-Key": f"{RUN}-payC-1"},
    )
    check(
        "pay C (possible duplicate, open case) -> 422 open_blocking_case",
        r.status_code == 422 and code_of(r) == "open_blocking_case",
        r.text,
    )

    nocash = f"{FEST}-nocash"
    sup2 = api.ok("post", "/suppliers", {"festival_id": nocash, "name": "Tent Co"})
    inv_n = api.ok(
        "post",
        "/invoices",
        {
            "supplier_id": sup2["id"],
            "invoice_number": "T-1",
            "invoice_date": str(D1),
            "net_cents": 1_000,
            "tax_cents": 0,
            "gross_cents": 1_000,
        },
    )
    r = api.post(
        f"/invoices/{inv_n['invoice']['id']}/pay",
        {"amount_cents": 1_000, "business_date": str(D1)},
        **{"Idempotency-Key": f"{RUN}-nocash"},
    )
    check(
        "supplier payment with no available cash -> 422 insufficient_available_cash",
        r.status_code == 422 and code_of(r) == "insufficient_available_cash",
        r.text,
    )

    ap = api.ok("get", "/invoices", festival_id=FEST, as_of=str(D2))
    check(
        "payables: total outstanding 30000 (only possible-duplicate C)",
        ap["total_outstanding_cents"] == 30_000,
        ap["total_outstanding_cents"],
    )
    check(
        "payables: supplier AP journal balance equals open invoices",
        -tb_role(api, "SUPPLIER_AP") == ap["total_outstanding_cents"],
    )

    # --- statements before payout (09:00 PROVISIONAL), payout, FINAL ------
    early = "2026-10-17T15:30:00Z"  # 08:30 PDT, before the 09:00 deadline
    st = api.ok(
        "post",
        "/statements/issue-day",
        {"festival_id": FEST, "business_date": str(D1), "demo_effective_at": early},
    )
    by_v = {s["vendor_id"]: s for s in st["statements"]}
    check(
        "statements: Taco PROVISIONAL before the payout, issued on time",
        by_v[ids["taco"]]["status"] == "PROVISIONAL" and by_v[ids["taco"]]["issued_on_time"],
        by_v.get(ids["taco"]),
    )
    check(
        "statements: Retained FINAL (no payout due, no holds)",
        by_v[ids["retained"]]["status"] == "FINAL",
    )
    check("statements: Held vendor HELD (open case)", by_v[ids["held"]]["status"] == "HELD")
    stmt_t = api.ok("get", "/statements", vendor_id=ids["taco"], business_date=str(D1))["items"][0]
    keys = {
        e["entry_key"].split("#")[0].split(":")[0] for e in stmt_t["content"]["journal_entries"]
    }
    check(
        "statement links to its journal entries (SALES and COMMISSION) and settlement",
        {"SALES", "COMMISSION"} <= keys
        and stmt_t["settlement_id"] == ids["settlement_taco"]
        and stmt_t["content"]["amounts"]["payout_cents"] == 12_410,
        stmt_t["content"],
    )
    due = stmt_t["due_by"]
    check(
        "statement due_by is 09:00 America/Los_Angeles the next morning",
        due.startswith("2026-10-17T16:00:00"),
        due,
    )
    again = api.ok(
        "post",
        "/statements/issue-day",
        {"festival_id": FEST, "business_date": str(D1), "demo_effective_at": early},
    )
    check(
        "statements: unchanged re-issue creates no new version",
        not any(s["created"] for s in again["statements"]),
        again,
    )

    p = api.ok("post", "/payouts", {"settlement_id": ids["settlement_taco"]})["payout"]
    api.ok("post", f"/payouts/{p['id']}/approve", {})
    api.ok("post", f"/payouts/{p['id']}/release", {})
    api.ok("post", f"/payouts/{p['id']}/dispatch", {"simulate_outcome": "ACCEPTED"})
    stmt_mid = api.ok("post", "/statements", {"vendor_id": ids["taco"], "business_date": str(D1)})
    check(
        "statements: still PROVISIONAL while payout SUBMITTED (not yet confirmed)",
        stmt_mid["statement"]["status"] == "PROVISIONAL",
        stmt_mid["statement"]["status"],
    )
    api.ok("post", f"/payouts/{p['id']}/confirm-payout")
    stmt2 = api.ok("post", "/statements", {"vendor_id": ids["taco"], "business_date": str(D1)})
    check(
        "statements: after confirmation, new FINAL version supersedes the provisional",
        stmt2["created"]
        and stmt2["statement"]["status"] == "FINAL"
        and stmt2["statement"]["supersedes_statement_id"] is not None
        and stmt2["statement"]["statement_version"] >= 2,
        stmt2["statement"],
    )
    check(
        "vendor payout posted once on confirmation; Taco payable now 0",
        len(journal(api, kind="VENDOR_PAYOUT_SIMULATED")) == 1
        and tb_role(api, "VENDOR_FUNDS_PAYABLE") == 0,
    )

    # --- tax -----------------------------------------------------------
    tax = api.ok("get", "/reports/tax", festival_id=FEST)
    t = tax["totals"]
    check("tax: DRAFT, NOT FILED label", tax["status"].startswith("DRAFT, NOT FILED"))
    check(
        "tax: central supplied 1210, recalculated 1203, retained memo 556",
        (
            t["central_supplied_tax_cents"],
            t["central_recalculated_tax_cents"],
            t["vendor_retained_supplied_tax_memo_cents"],
        )
        == (1_210, 1_203, 556),
        t,
    )
    check(
        "tax: source totals match the sales-tax liability in the journal",
        tax["source_totals_match_journal"] and tax["journal_sales_tax_liability_cents"] == 1_210,
    )
    check(
        "tax: exactly one mismatch line (O3 supplied 100 vs 93)",
        [(x["supplied_tax_cents"], x["recalculated_tax_cents"]) for x in tax["mismatch_lines"]]
        == [(100, 93)],
        tax["mismatch_lines"],
    )
    row = tax["by_business_date"][0]
    check(
        "tax: exempt (packaged) central sales 1600 net of refund; taxable 13000",
        row.get("central_exempt_sales_cents") == 1_600
        and row.get("central_taxable_sales_cents") == 13_000,
        row,
    )
    check(
        "tax: liability owner explicit per custody",
        row["organizer_liability_owner"] == "ORGANIZER_AS_AGENT"
        and row["vendor_retained_liability_owner"] == "VENDOR",
        row,
    )

    # --- tickets recognition + post-event ticket -------------------------
    api.ok("post", "/finance/tickets/recognize", {"festival_id": FEST, "business_date": str(D2)})
    api.ok(
        "post",
        "/finance/ticket-settlements",
        {
            "festival_id": FEST,
            "platform": "TIXSIM",
            "settlement_reference": "TIX-2",
            "gross_cents": 5_000,
            "platform_fees_cents": 250,
            "net_cents": 4_750,
            "business_date": str(D2),
        },
    )
    check(
        "tickets: deferred revenue fully recognized; post-event ticket straight to revenue",
        tb_role(api, "DEFERRED_TICKET_REVENUE") == 0 and tb_role(api, "TICKET_REVENUE") == -205_000,
    )

    # --- P&L, cash, trial balance ---------------------------------------
    pnl = api.ok("get", "/reports/pnl", festival_id=FEST)
    rev = {x["account_role"]: x["amount_cents"] for x in pnl["revenue"]}
    exp = {x["account_role"]: x["amount_cents"] for x in pnl["expenses"]}
    check(
        "P&L revenue: commission 3090, tickets 205000, sponsorship 60000",
        (rev["COMMISSION_REVENUE"], rev["TICKET_REVENUE"], rev["SPONSORSHIP_REVENUE"])
        == (3_090, 205_000, 60_000),
        rev,
    )
    check(
        "P&L expenses: processor 355, ticketing 10250, supplier 83000, operating 15000",
        (
            exp["PROCESSOR_FEE_EXPENSE"],
            exp["TICKETING_FEE_EXPENSE"],
            exp["SUPPLIER_EXPENSE"],
            exp["OPERATING_EXPENSE"],
        )
        == (355, 10_250, 83_000, 15_000),
        exp,
    )
    check(
        "P&L net income 159485; vendor gross sales are not revenue",
        pnl["net_income_cents"] == 159_485 and pnl["total_revenue_cents"] == 268_090,
        pnl,
    )

    cash = api.ok("get", "/cash", festival_id=FEST, as_of=str(D2))
    check(
        "cash: bank 1185825 = opening + deposit + tickets + sponsor - expense - AP - payout",
        cash["bank_cents"] == 1_185_825,
        cash["bank_cents"],
    )
    check(
        "cash: pending 4370 (undeposited cash office), protected 1610 (tax 1210 + tips 400)",
        cash["pending_cents"] == 4_370
        and cash["protected_cents"] == 1_610
        and cash["protected"]["sales_tax_cents"] == 1_210
        and cash["protected"]["tips_cents"] == 400,
        cash,
    )
    check(
        "cash: freely available excludes protected tax/tips (1185825 - 1610 = 1184215)",
        cash["freely_available_cents"] == 1_184_215,
        cash["freely_available_cents"],
    )
    check(
        "cash: invoice C due after the 14-day horizon is listed as a later obligation",
        cash["obligations_after_horizon_cents"] == 30_000
        and cash["obligations_due_within_horizon_cents"] == 0,
        cash,
    )
    tb = api.ok("get", "/journal/trial-balance", festival_id=FEST)
    check("trial balance: balanced", tb["balanced"], tb["total_debit_cents"])
    check(
        "trial balance: receivable 900 (R 750 + H 150), equity 1000000",
        tb_role(api, "VENDOR_RECEIVABLE") == 900 and tb_role(api, "OPENING_EQUITY") == -1_000_000,
    )
    rep = api.ok("get", "/reports/festival", festival_id=FEST, as_of=str(D2))
    memo = rep["sponsorship_receivables_memo"][0]
    check(
        "festival report: sponsorship memo 40000 outstanding, PARTIALLY_PAID",
        memo["outstanding_memo_cents"] == 40_000 and memo["status"] == "PARTIALLY_PAID",
        memo,
    )

    # --- daily close ------------------------------------------------------
    checks = api.ok("get", "/close/checks", festival_id=FEST, business_date=str(D1))
    check(
        "close checks for D1: every hard check passes", all(checks["hard"].values()), checks["hard"]
    )
    close = api.ok("post", "/close", {"festival_id": FEST, "business_date": str(D1)})
    check(
        "close D1: created with a balanced trial-balance snapshot",
        close["created"] and close["trial_balance_balanced"],
        close,
    )
    again = api.ok("post", "/close", {"festival_id": FEST, "business_date": str(D1)})
    check("close D1 again: idempotent", not again["created"] and again["id"] == close["id"])
    r = api.post(
        "/finance/expenses",
        {
            "festival_id": FEST,
            "expense_reference": "EXP-LATE",
            "category": "misc",
            "amount_cents": 100,
            "business_date": str(D1),
        },
    )
    check(
        "closed date: a new fact dated D1 is refused (409 business_date_closed)",
        r.status_code == 409 and code_of(r) == "business_date_closed",
        r.text,
    )

    # --- late refund + settlement correction after close -----------------
    fx.late_refund_o3(db, m, FEST, ids["o3"])
    fx.correct_retained(db, m, ids["settlement_retained"])
    out = api.ok("post", "/journal/post-day", {"festival_id": FEST, "business_date": str(D1)})
    adj = sorted(k for k in out["posted"] if "#adj" in k)
    check(
        "late refund + settlement correction post as two linked #adj entries",
        len(adj) == 2
        and any(k.startswith("SALES:") for k in adj)
        and any(k.startswith("COMMISSION:") for k in adj),
        out,
    )
    sales_adj = next(
        e
        for e in journal(api, business_date=str(D1))
        if e["entry_key"] == f"SALES:{ids['taco']}:{D1}:1100#adj1"
    )
    check(
        "late refund adj: correction_of original, flagged posted_after_close",
        sales_adj["correction_of"] == sales_1100["id"]
        and "posted_after_close" in sales_adj["note"],
        sales_adj,
    )
    orig = api.ok("get", f"/journal/{sales_1100['id']}")
    check("original sales entry untouched by the late refund", orig["lines"] == sales_1100["lines"])
    check(
        "commission correction adds exactly +150 receivable/revenue",
        tb_role(api, "VENDOR_RECEIVABLE") == 1_050 and -tb_role(api, "COMMISSION_REVENUE") == 3_240,
    )
    tax2 = api.ok("get", "/reports/tax", festival_id=FEST)
    check(
        "after late refund: tax source totals still match journal (1210 - 50 = 1160)",
        tax2["source_totals_match_journal"] and tax2["journal_sales_tax_liability_cents"] == 1_160,
        tax2["totals"],
    )
    check(
        "after late refund: Taco owes 500 back (vendor payable debit), recovery visible",
        tb_role(api, "VENDOR_FUNDS_PAYABLE") == 500,
    )
    check(
        "trial balance still balanced after adjustments",
        api.ok("get", "/journal/trial-balance", festival_id=FEST)["balanced"],
    )
    closes = api.ok("get", "/close", festival_id=FEST)["items"]
    check(
        "D1 close snapshot unchanged by later adjustments",
        len(closes) == 1 and closes[0]["journal_entry_count"] == close["journal_entry_count"],
    )

    # --- every journal entry balanced (via API) ---------------------------
    all_entries = journal(api)
    unbalanced = [
        e["entry_key"]
        for e in all_entries
        if sum(ln["debit_cents"] for ln in e["lines"])
        != sum(ln["credit_cents"] for ln in e["lines"])
    ]
    check(
        f"all {len(all_entries)} journal entries individually balanced", not unbalanced, unbalanced
    )

    # --- immutability for the app role ------------------------------------
    with psycopg.connect(APP_ROLE_DSN, autocommit=True) as conn:
        for table in (
            "journal_lines",
            "journal_entries",
            "supplier_payments",
            "vendor_statements",
            "daily_closes",
        ):
            try:
                conn.execute(f"UPDATE {table} SET id = id WHERE false")
                check(f"encore_app cannot UPDATE {table}", False, "update was allowed")
            except psycopg.errors.InsufficientPrivilege:
                check(f"encore_app cannot UPDATE {table}", True)

    # --- capability gating ------------------------------------------------
    anon = httpx.post(
        f"{API_BASE_URL}/api/v1/journal/post-day",
        json={"festival_id": FEST, "business_date": str(D1)},
    )
    check("unauthenticated post-day -> 401", anon.status_code == 401, anon.status_code)
    db.close()

    print(f"\n{len(_passes)} passed, {len(_failures)} failed")
    for f in _failures:
        print(f"  FAILED: {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
