#!/usr/bin/env python3
"""Encore independent evaluator oracle for the FULL dataset (lane B, task B02).

EVALUATOR-ONLY. Never ship this directory into runtime containers, agent prompt
context, retrieval indexes or the document bucket.

Independence rules (same as oracle.py, enforced by an AST import allowlist test):
  * stdlib only; imports nothing from data/generator/ or any production package;
  * reads only the emitted runtime files (api/*.json, sources/*), the simulator
    outcome script (an external-fact input) and the evaluator's scripted human
    decisions. It never reads the generator config or the scenario manifest;
  * contract terms are re-extracted from the PDF text with this file's own PDF
    text extractor and several phrasing patterns; arithmetic (line tax with
    taxability, business dates, batch sums) is re-verified, not trusted.

Rules: CONTRACTS.md v0.1 DRAFT C01/C02/C05, AGENT_HANDOFF approved rules, compact_v1
assumptions DA01-DA12 and the full_v1 DRAFT assumptions DA13-DA30 (listed in
api/business_config.json and docs/progress/lane-b.md). Expected numbers are always
conditional on a scripted evaluator decision set (scripted_decisions_full.json).

Usage:
    python3 tests/oracle/oracle_full.py [--fixture DIR] [--decisions FILE] [--out DIR]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ORACLE_VERSION = "oracle-full-0.1.0"
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DEFAULT_FIXTURE = REPO / "data" / "generator" / "out" / "full_v1"
DEFAULT_DECISIONS = HERE / "scripted_decisions_full.json"
MONEY_RE = re.compile(r"^-?(0|[1-9][0-9]*)$")
AS_OF_PAYABLES = "2026-11-01"

# DRAFT posting map (oracle-local numbers; compare by ROLE, not number).
ACCOUNTS = {
    "1000": ("BANK_OPERATING", "Bank cash - operating"),
    "1050": ("CASH_OFFICE", "Cash on hand - organizer cash office"),
    "1100": ("PROCESSOR_CLEARING", "Processor clearing (card + wristband)"),
    "1200": ("VENDOR_RECEIVABLE", "Vendor commission receivable / unpaid balance"),
    "1999": ("SUSPENSE", "Unclassified bank activity (suspense)"),
    "2000": ("VENDOR_FUNDS_PAYABLE", "Vendor funds payable"),
    "2100": ("SALES_TAX_LIABILITY", "Sales tax liability/control (DRAFT, not filed)"),
    "2200": ("TIPS_LIABILITY", "Tips liability/control"),
    "2300": ("SUPPLIER_AP", "Supplier accounts payable"),
    "2400": ("DEFERRED_TICKET_REVENUE", "Deferred ticket revenue"),
    "2500": ("CUSTOMER_REFUNDS_PAYABLE", "Customer refunds payable (duplicate charges)"),
    "3000": ("OPENING_EQUITY", "Opening balance equity"),
    "4000": ("COMMISSION_REVENUE", "Organizer commission revenue"),
    "4100": ("TICKET_REVENUE", "Ticket revenue"),
    "4200": ("SPONSORSHIP_REVENUE", "Sponsorship revenue"),
    "5100": ("PROCESSOR_FEE_EXPENSE", "Processor fee expense"),
    "5110": ("TICKETING_FEE_EXPENSE", "Ticketing platform fee expense"),
    "5200": ("CASH_OVER_SHORT", "Cash over/short"),
    "5300": ("SUPPLIER_EXPENSE", "Supplier costs (incl. supplier tax)"),
    "5400": ("OPERATING_EXPENSE", "Operating expenses"),
}
ROLE = {k: v[0] for k, v in ACCOUNTS.items()}


class FixtureError(Exception):
    pass


def cents(s) -> int:
    if not isinstance(s, str) or not MONEY_RE.match(s) or s == "-0":
        raise FixtureError(f"non-canonical money string: {s!r}")
    return int(s)


def rhu(x: Decimal) -> int:
    return int(x.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def pct_of(amount: int, bps: int) -> int:
    return rhu(Decimal(amount) * Decimal(bps) / Decimal(10000))


def parse_utc(s: str) -> datetime:
    if not s.endswith("Z"):
        raise FixtureError(f"timestamp not UTC RFC3339 'Z': {s}")
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- PDF text (independent extractor)
_OBJ_RE = re.compile(rb"(\d+) 0 obj\s*(.*?)\s*endobj", re.S)
_TJ_RE = re.compile(rb"\(((?:\\.|[^\\()])*)\)\s*Tj")


def _unescape_pdf(b: bytes) -> str:
    out, i = [], 0
    while i < len(b):
        c = b[i:i + 1]
        if c == b"\\" and i + 1 < len(b):
            n = b[i + 1:i + 2]
            out.append({b"n": "\n", b"r": "\r", b"t": "\t"}.get(n, n.decode("latin-1")))
            i += 2
        else:
            out.append(c.decode("latin-1"))
            i += 1
    return "".join(out)


def pdf_pages(data: bytes) -> list[str]:
    """Return the text of each page (lines joined by newlines). Handles uncompressed streams only."""
    if not data.startswith(b"%PDF-"):
        raise FixtureError("not a PDF")
    objs = {int(m.group(1)): m.group(2) for m in _OBJ_RE.finditer(data)}
    root = re.search(rb"/Root (\d+) 0 R", data)
    if not root:
        raise FixtureError("PDF without /Root")
    pages_ref = re.search(rb"/Pages (\d+) 0 R", objs[int(root.group(1))])
    kids = re.search(rb"/Kids \[([^\]]*)\]", objs[int(pages_ref.group(1))])
    texts = []
    for k in re.findall(rb"(\d+) 0 R", kids.group(1)):
        page = objs[int(k)]
        if b"/FlateDecode" in page:
            raise FixtureError("compressed PDF streams are not supported by the oracle extractor")
        cref = re.search(rb"/Contents (\d+) 0 R", page)
        body = objs[int(cref.group(1))]
        stream = re.search(rb"stream\r?\n(.*)endstream", body, re.S).group(1)
        texts.append("\n".join(_unescape_pdf(m.group(1)) for m in _TJ_RE.finditer(stream)))
    return texts


# ---------------------------------------------------------------- loading
class Fixture:
    def __init__(self, root: Path):
        self.root = root
        self.manifest_bytes = (root / "manifest.json").read_bytes()
        self.manifest = json.loads(self.manifest_bytes)
        api = root / "api"
        self.festival = json.loads((api / "festival.json").read_text())
        self.config = json.loads((api / "business_config.json").read_text())
        for name in ("vendors", "menu_items", "source_documents", "orders", "payments", "processor_batches", "bank_entries",
                     "cash_counts", "cash_deposit_slips", "beneficiary_change_requests", "ticket_settlements", "sponsors",
                     "sponsorship_receipts", "suppliers", "purchase_orders", "supplier_invoices", "supplier_payments",
                     "expenses", "opening_balances", "prior_settlements", "prior_policies", "prior_cases"):
            setattr(self, name, json.loads((api / f"{name}.json").read_text())["items"])
        sim = root / "simulator_only" / "payout_outcome_script.json"
        self.sim = json.loads(sim.read_text())
        self.tz = ZoneInfo(self.festival["timezone"])
        self.dates = self.festival["business_dates"]
        self.final_day = self.dates[-1]
        self.vendor_by_id = {v["id"]: v for v in self.vendors}
        self.booth = {v["id"]: v["booths"][0]["label"] for v in self.vendors}
        self.order_by_id = {o["id"]: o for o in self.orders}
        self.doc_by_id = {d["id"]: d for d in self.source_documents}
        self.doc_by_path = {d["path"]: d for d in self.source_documents}
        self.menu = {(m["vendor_id"], m["item_sku"]): m for m in self.menu_items}
        self._text_cache = {}

    def doc_bytes(self, doc) -> bytes:
        data = (self.root / doc["path"]).read_bytes()
        if "sha256:" + hashlib.sha256(data).hexdigest() != doc["content_hash"]:
            raise FixtureError(f"content hash mismatch for {doc['path']}")
        return data

    def doc_pages(self, doc) -> list[str]:
        if doc["id"] not in self._text_cache:
            data = self.doc_bytes(doc)
            self._text_cache[doc["id"]] = pdf_pages(data) if doc["media_type"] == "application/pdf" else [data.decode("utf-8")]
        return self._text_cache[doc["id"]]

    def local_date(self, ts: str) -> str:
        return parse_utc(ts).astimezone(self.tz).date().isoformat()

    def local_to_utc(self, d: str, hhmm: str) -> datetime:
        y, m, dd = (int(x) for x in d.split("-"))
        hh, mi = (int(x) for x in hhmm.split(":"))
        return datetime(y, m, dd, hh, mi, tzinfo=self.tz).astimezone(timezone.utc)

    def next_day_at(self, d: str, hhmm: str) -> datetime:
        return self.local_to_utc((date.fromisoformat(d) + timedelta(days=1)).isoformat(), hhmm)

    def statement_time(self, d: str) -> datetime:
        return self.next_day_at(d, self.config["payout_controls"]["statement_local_time"])

    def payout_time(self, d: str) -> datetime:
        return self.next_day_at(d, self.config["payout_controls"]["payout_local_time"])

    def src_ref(self, doc, kind, locator, excerpt=None):
        r = {"source_id": doc["id"], "source_version": doc["version"], "content_hash": doc["content_hash"],
             "locator_kind": kind, "locator": locator}
        if excerpt is not None:
            r["quoted_excerpt"] = excerpt
        return r

    def page_ref(self, doc, excerpt: str) -> dict:
        for i, t in enumerate(self.doc_pages(doc), start=1):
            if excerpt in norm_ws(t):
                return self.src_ref(doc, "page", f"page={i}", excerpt)
        raise FixtureError(f"excerpt not found in {doc['path']}: {excerpt!r}")

    def span_ref(self, doc, excerpt: str) -> dict:
        t = self.doc_pages(doc)[0]
        a = t.index(excerpt)
        return self.src_ref(doc, "span", f"chars={a}-{a + len(excerpt)}", excerpt)


# ---------------------------------------------------------------- terms extraction
RATE_RES = [re.compile(r"(?:commission|fee)[^.%]{0,80}?(\d+(?:\.\d+)?)\s*(?:%|percent)", re.I)]
MIN_RE = re.compile(r"minimum[^$]{0,80}?(?:\$|USD\s?)([\d,]+\.\d{2})", re.I)
FESTIVAL_WIDE_RE = re.compile(r"entire festival|whole festival|all three festival days|not per (?:day|night)", re.I)
PROC_NEG_RE = re.compile(r"absorb|not (?:be )?deducted|will not be deducted", re.I)
PROC_POS_RE = re.compile(r"deducted from|netted against", re.I)
EMAIL_RATE_RES = [re.compile(r"(\d+(?:\.\d+)?)\s*%\s*commission", re.I),
                  re.compile(r"commission[^.%\d]{0,60}?(\d+(?:\.\d+)?)\s*%", re.I)]
MIN_PERIOD_CLAIM_RE = re.compile(r"minimum[^.]{0,80}?(?:per night|each night|nightly|every night)", re.I)


def pct_to_bps(p: str) -> int:
    v = Decimal(p) * 100
    if v != v.to_integral_value():
        raise FixtureError(f"rate {p}% is not whole basis points")
    return int(v)


def _rates(text: str) -> list[tuple[int, str]]:
    out = []
    for rx in RATE_RES:
        for m in rx.finditer(text):
            out.append((pct_to_bps(m.group(1)), m.group(0)))
    return out


def _processing(text: str):
    """Classify the processing-fee clause. Return (deductible: bool|None, excerpt)."""
    m = re.search(r"processing (?:fees|charges)", text, re.I)
    if not m:
        return None, None
    window = text[m.start():m.start() + 170]
    pre_start = max(0, m.start() - 40)
    pre = re.search(r"absorb", text[pre_start:m.start()], re.I)
    if pre:
        return False, text[pre_start + pre.start():m.end()]
    neg = PROC_NEG_RE.search(window)
    if neg:
        return False, window[:neg.end()]
    pos = PROC_POS_RE.search(window)
    if pos:
        return True, window[:pos.end()]
    raise FixtureError(f"cannot classify processing-fee clause: {window!r}")


def derive_terms(fx: Fixture) -> dict:
    terms = {}
    for v in fx.vendors:
        cid = v["active_contract_version"]["source_id"]
        doc = fx.doc_by_id[cid]
        text = norm_ws(" ".join(fx.doc_pages(doc)))
        rates = _rates(text)
        if not rates or len({b for b, _ in rates}) != 1:
            raise FixtureError(f"ambiguous or missing commission rate in {doc['path']}: {rates}")
        mm = MIN_RE.search(text)
        if not mm:
            raise FixtureError(f"no festival minimum in {doc['path']}")
        window = text[mm.start():mm.end() + 160]
        if not FESTIVAL_WIDE_RE.search(window):
            raise FixtureError(f"minimum in {doc['path']} is not stated as festival-wide")
        fees, fee_excerpt = _processing(text)
        if fees is None:
            raise FixtureError(f"no processing-fee clause in {doc['path']}")
        t = {"vendor_name": v["name"], "contract_source_id": cid, "contract_source_version": doc["version"],
             "contract_received_at": doc["received_at"],
             "contract_bps": rates[0][0], "minimum_cents": int(mm.group(1).replace(",", "").replace(".", "")),
             "processor_fees_deductible": fees,
             "evidence": {"rate": fx.page_ref(doc, rates[0][1]), "minimum": fx.page_ref(doc, mm.group(0)),
                          "processor_fees": fx.page_ref(doc, fee_excerpt)},
             "amendments": [], "claims": [], "minimum_period_claims": []}
        terms[v["id"]] = t
    for d in fx.source_documents:
        if d["kind"] != "CONTRACT_AMENDMENT":
            continue
        t = terms[d["vendor_id"]]
        if d["amends_source_id"] != t["contract_source_id"]:
            raise FixtureError(f"amendment {d['path']} does not amend the active contract")
        text = norm_ws(" ".join(fx.doc_pages(d)))
        am = {"source_id": d["id"], "source_version": d["version"], "received_at": d["received_at"], "bps": None,
              "fees_deductible": None, "evidence": []}
        rates = _rates(text)
        if rates:
            am["bps"] = rates[0][0]
            am["evidence"].append(fx.page_ref(d, rates[0][1]))
        if re.search(r"processing fees", text, re.I):
            fees, ex = _processing(text)
            am["fees_deductible"] = fees
            am["evidence"].append(fx.page_ref(d, ex))
        t["amendments"].append(am)
    for t in terms.values():
        t["amendments"].sort(key=lambda a: a["received_at"])
    names = {v["name"]: v["id"] for v in fx.vendors}
    for d in fx.source_documents:
        if d["kind"] != "EMAIL_THREAD":
            continue
        text = fx.doc_pages(d)[0]
        named = [vid for n, vid in names.items() if n in text]
        if len(named) != 1:
            continue
        t = terms[named[0]]
        seen = set()
        for rx in EMAIL_RATE_RES:
            for m in rx.finditer(text):
                b = pct_to_bps(m.group(1))
                if b in seen:
                    continue
                seen.add(b)
                t["claims"].append({"bps": b, "source_id": d["id"], "source_version": d["version"], "received_at": d["received_at"],
                                    "evidence": fx.span_ref(d, m.group(0))})
        for m in MIN_PERIOD_CLAIM_RE.finditer(text):
            t["minimum_period_claims"].append({"source_id": d["id"], "received_at": d["received_at"],
                                               "evidence": fx.span_ref(d, m.group(0))})
    return terms


def terms_at(t: dict, at: datetime) -> dict:
    """Terms known at instant `at` (DA18/DA19: documents usable from received_at)."""
    bps, fees = t["contract_bps"], t["processor_fees_deductible"]
    refs = [t["evidence"]["rate"]]
    amend_ids, latest_amend = [], None
    for a in t["amendments"]:
        if parse_utc(a["received_at"]) <= at:
            amend_ids.append(a["source_id"])
            latest_amend = a["received_at"]
            if a["bps"] is not None:
                bps = a["bps"]
            if a["fees_deductible"] is not None:
                fees = a["fees_deductible"]
            refs += a["evidence"]
    claims = [c for c in t["claims"] if parse_utc(c["received_at"]) <= at
              and (latest_amend is None or parse_utc(c["received_at"]) > parse_utc(latest_amend))]
    conflicting = [c for c in claims if c["bps"] != bps]
    return {"bps": bps, "fees_deductible": fees, "minimum_cents": t["minimum_cents"], "refs": refs,
            "amendment_ids": tuple(amend_ids), "conflicting_claims": conflicting,
            "evidence_key": (tuple(amend_ids), tuple(sorted(c["source_id"] + ":" + str(c["bps"]) for c in conflicting)))}


# ---------------------------------------------------------------- input verification + payment classification
def classify_payments(fx: Fixture) -> dict:
    """Deduplicate imports, detect genuine duplicate charges and their reversals."""
    by_ref = defaultdict(list)
    for p in fx.payments:
        by_ref[(p["provider"], p["provider_reference"])].append(p)
    in_batch = {pid for b in fx.processor_batches for pid in b["payment_ids"]}
    canonical, dup_imports = [], []
    for key, ps in by_ref.items():
        if len(ps) == 1:
            canonical.append(ps[0])
            continue
        sig = {(p["amount_cents"], p["occurred_at"], p["order_refs"][0]["order_id"], p["kind"]) for p in ps}
        if len(sig) != 1:
            raise FixtureError(f"same provider reference {key} with different content")
        keep = sorted(ps, key=lambda p: (p["id"] not in in_batch, p["id"]))[0]
        canonical.append(keep)
        for p in ps:
            if p is not keep:
                dup_imports.append({"duplicate_payment_id": p["id"], "kept_payment_id": keep["id"],
                                    "provider_reference": p["provider_reference"], "source_refs": p["source_refs"]})
    canonical.sort(key=lambda p: (p["occurred_at"], p["provider_reference"]))
    captures = defaultdict(list)
    for p in canonical:
        if p["kind"] == "CAPTURE":
            captures[p["order_refs"][0]["order_id"]].append(p)
    dup_charges = {}
    for oid, ps in captures.items():
        o = fx.order_by_id[oid]
        ps = sorted(ps, key=lambda p: (p["occurred_at"], p["provider_reference"]))
        total = sum(cents(p["amount_cents"]) for p in ps)
        if total == cents(o["gross_cents"]):
            continue
        if len(ps) >= 2 and all(cents(p["amount_cents"]) == cents(o["gross_cents"]) for p in ps):
            for p in ps[1:]:
                dup_charges[p["id"]] = {"payment": p, "original": ps[0], "order": o}
        else:
            raise FixtureError(f"captures for order {o['source_native_id']} do not equal order gross")
    reversals = {p["id"]: dup_charges[p["refund_of_payment_id"]] for p in canonical
                 if p["kind"] == "REFUND" and p.get("refund_of_payment_id") in dup_charges}
    return {"canonical": canonical, "duplicate_imports": dup_imports, "duplicate_charges": dup_charges, "reversals": reversals}


def verify_inputs(fx: Fixture, pc: dict) -> dict:
    tax_cats = {k: Decimal(v) for k, v in fx.config["tax"]["categories"].items()}
    line_by_id = {ln["line_id"]: (o, ln) for o in fx.orders for ln in o["lines"]}
    tax_mismatch_lines = []
    for o in fx.orders:
        if fx.local_date(o["ordered_at"]) != o["business_date"]:
            raise FixtureError(f"business_date mismatch {o['source_native_id']}")
        for f in ("eligible_net_cents", "tax_cents", "tips_cents", "gross_cents"):
            if sum(cents(ln[f]) for ln in o["lines"]) != cents(o[f]):
                raise FixtureError(f"{f} total mismatch on {o['source_native_id']}")
        for ln in o["lines"]:
            e, t, tp = cents(ln["eligible_net_cents"]), cents(ln["tax_cents"]), cents(ln["tips_cents"])
            if e + t + tp != cents(ln["gross_cents"]):
                raise FixtureError(f"gross != elig+tax+tips on {ln['line_id']}")
            if e != ln["quantity"] * cents(ln["unit_price_cents"]):
                raise FixtureError(f"qty*unit != eligible on {ln['line_id']}")
            if ln["item_sku"] == "TIP":
                expect = 0
            elif "refund_of_line_id" in ln:
                _, ol = line_by_id[ln["refund_of_line_id"]]
                expect = -rhu(Decimal(cents(ol["tax_cents"])) * (-ln["quantity"]) / ol["quantity"])
                # refunds inherit the original line's supplied tax; mismatch is reported on the original line
                if expect != t:
                    raise FixtureError(f"refund tax not proportional on {ln['line_id']}")
                continue
            else:
                m = fx.menu.get((o["vendor_id"], ln["item_sku"]))
                if not m:
                    raise FixtureError(f"unknown menu item {ln['item_sku']} for {o['source_native_id']}")
                if cents(m["unit_price_cents"]) != cents(ln["unit_price_cents"]):
                    raise FixtureError(f"price differs from menu on {ln['line_id']}")
                expect = rhu(Decimal(e) * tax_cats[m["tax_category"]])
            if expect != t:
                tax_mismatch_lines.append({"order": o, "line": ln, "supplied": t, "recalculated": expect})
    for p in pc["canonical"]:
        o = fx.order_by_id[p["order_refs"][0]["order_id"]]
        if p["kind"] in ("CAPTURE",) or (p["kind"] == "REFUND" and o["kind"] == "REFUND"):
            if p["id"] not in pc["duplicate_charges"] and cents(p["amount_cents"]) != cents(o["gross_cents"]):
                raise FixtureError(f"payment {p['provider_reference']} != order gross")
        if p["custody"] not in ("CENTRALLY_COLLECTED", "VENDOR_RETAINED"):
            raise FixtureError(f"unknown custody {p['custody']}")
    pay_by_id = {p["id"]: p for p in fx.payments}
    for b in fx.processor_batches:
        mem = [pay_by_id[i] for i in b["payment_ids"]]
        if sum(cents(p["amount_cents"]) for p in mem) != cents(b["gross_cents"]):
            raise FixtureError(f"batch {b['provider_reference']} gross mismatch")
        if sum(cents(p["processor_fee_cents"]) for p in mem) != cents(b["fees_cents"]):
            raise FixtureError(f"batch {b['provider_reference']} fee mismatch")
        if cents(b["gross_cents"]) - cents(b["fees_cents"]) != cents(b["net_cents"]):
            raise FixtureError(f"batch {b['provider_reference']} net mismatch")
    return {"orders_checked": len(fx.orders), "payments_checked": len(fx.payments),
            "canonical_payments": len(pc["canonical"]), "duplicate_import_records": len(pc["duplicate_imports"]),
            "duplicate_charges": len(pc["duplicate_charges"]), "batches_checked": len(fx.processor_batches),
            "tax_mismatch_lines": len(tax_mismatch_lines)}, tax_mismatch_lines


# ---------------------------------------------------------------- per vendor-day aggregates
def aggregates(fx: Fixture, pc: dict) -> dict:
    """(vendor_id, business_date) -> sums; derived from POS orders (+ chargebacks), custody from payments."""
    custody_of = {}
    for p in pc["canonical"]:
        if p["id"] in pc["duplicate_charges"] or p["id"] in pc["reversals"] or p["kind"] == "CHARGEBACK":
            continue
        custody_of.setdefault(p["order_refs"][0]["order_id"], p)
    agg = defaultdict(lambda: defaultdict(int))
    lines_ref = defaultdict(list)
    for o in fx.orders:
        p = custody_of.get(o["id"])
        if p is None:
            raise FixtureError(f"order {o['source_native_id']} has no payment")
        a = agg[(o["vendor_id"], o["business_date"])]
        c = "central" if p["custody"] == "CENTRALLY_COLLECTED" else "retained"
        for ln in o["lines"]:
            a[f"elig_{c}"] += cents(ln["eligible_net_cents"])
            a[f"tax_{c}"] += cents(ln["tax_cents"])
            a[f"tips_{c}"] += cents(ln["tips_cents"])
            if o["kind"] == "REFUND":
                a["refunds_elig"] += cents(ln["eligible_net_cents"])
        if p["method"] == "CASH":
            a[f"cash_{c}"] += cents(o["gross_cents"])
        lines_ref[(o["vendor_id"], o["business_date"])].append(o)
    chargebacks = defaultdict(list)
    for p in pc["canonical"]:
        if p["kind"] == "CHARGEBACK":
            o = fx.order_by_id[p["order_refs"][0]["order_id"]]
            a = agg[(p["vendor_id"], p["business_date"])]
            a["elig_central"] -= cents(o["eligible_net_cents"])
            a["tax_central"] -= cents(o["tax_cents"])
            a["tips_central"] -= cents(o["tips_cents"])
            a["chargeback_elig"] -= cents(o["eligible_net_cents"])
            chargebacks[(p["vendor_id"], p["business_date"])].append((p, o))
    for p in pc["canonical"]:
        if p["provider"] and p["id"] not in pc["duplicate_charges"] and p["id"] not in pc["reversals"]:
            agg[(p["vendor_id"], p["business_date"])]["proc_fees"] += cents(p["processor_fee_cents"])
    return {"agg": agg, "orders": lines_ref, "chargebacks": chargebacks, "custody_payment": custody_of}


# ---------------------------------------------------------------- decisions
class Decisions:
    def __init__(self, rules: list[dict]):
        self.rules = rules
        self.used = defaultdict(int)

    def find(self, reason: str, business_date: str, vendor_name: str | None = None, subject_kind: str | None = None):
        for r in self.rules:
            if r["reason_code"] != reason:
                continue
            if "business_date" in r and r["business_date"] != business_date:
                continue
            if "vendor_name" in r and r["vendor_name"] != vendor_name:
                continue
            if "subject_kind" in r and r["subject_kind"] != subject_kind:
                continue
            self.used[r["rule_id"]] += 1
            return r
        return None


def review(reason, subject_kind, subject_id, business_date, blocking, behavior, decision, evidence=None, impact=None,
           vendor=None, capability=None):
    r = {"reason_code": reason, "subject_kind": subject_kind, "subject_id": subject_id, "business_date": business_date,
         "blocking": blocking, "expected_system_behavior": behavior, "evidence_refs": evidence or [],
         "financial_impact": impact or {}, "scripted_human_decision": decision}
    if vendor:
        r["vendor_id"], r["vendor_name"] = vendor["id"], vendor["name"]
    if capability:
        r["required_capability"] = capability
    return r


# ---------------------------------------------------------------- reconciliation (decision independent)
def reconcile(fx: Fixture, pc: dict, ag: dict) -> dict:
    windows = fx.config["matching"].get("deposit_window_days_by_provider", {})
    default_window = fx.config["matching"]["deposit_window_days"]
    groups = []
    batches_by_ref = defaultdict(list)
    for b in fx.processor_batches:
        batches_by_ref[b["payout_reference"]].append(b)
    bank_by_ref = {b["provider_reference"]: b for b in fx.bank_entries}
    explained = set()

    def signed(be):
        return cents(be["amount_cents"]) * (1 if be["direction"] == "credit" else -1)

    for pref, mem in sorted(batches_by_ref.items()):
        be = bank_by_ref.get(pref)
        net = sum(cents(b["net_cents"]) for b in mem)
        if be is None:
            groups.append({"match_key": f"PROCESSOR_DEPOSIT:{pref}", "kind": "PROCESSOR_DEPOSIT", "status": "CASE_REQUIRED",
                           "variance_cents": str(net), "reason_code": "MISSING_DEPOSIT"})
            continue
        explained.add(be["id"])
        lags = [(date.fromisoformat(fx.local_date(be["occurred_at"])) - date.fromisoformat(b["business_date"])).days for b in mem]
        w = windows.get(mem[0]["provider"], default_window)
        groups.append({
            "match_key": f"PROCESSOR_DEPOSIT:{pref}", "kind": "PROCESSOR_DEPOSIT", "bank_entry_id": be["id"],
            "member_batch_ids": [b["id"] for b in mem], "member_batch_dates": [b["business_date"] for b in mem],
            "gross_cents": str(sum(cents(b["gross_cents"]) for b in mem)), "fees_cents": str(sum(cents(b["fees_cents"]) for b in mem)),
            "matched_amount_cents": str(signed(be)), "variance_cents": str(signed(be) - net), "deposit_lag_days": lags,
            "reason_code": ("NET_DEBIT_REFUNDS" if net < 0 else "GROUPED_DEPOSIT_TIMING" if len(mem) > 1 or max(lags) > 1 else "NORMAL"),
            "status": "MATCHED" if signed(be) == net and max(lags) <= w else "CASE_REQUIRED",
            "source_refs": be["source_refs"] + [r for b in mem for r in b["source_refs"]]})
    # cash counts vs POS cash
    count_by = {(c["vendor_id"], c["business_date"], c["custody"]): c for c in fx.cash_counts}
    cash_checks, missing_counts = [], []
    for (vid, d), a in sorted(ag["agg"].items()):
        for custody, key in (("CENTRALLY_COLLECTED", "cash_central"), ("VENDOR_RETAINED", "cash_retained")):
            pos = a.get(key, 0)
            c = count_by.get((vid, d, custody))
            if c is None:
                if pos != 0 or (custody == "CENTRALLY_COLLECTED" and any(
                        ag["custody_payment"][o["id"]]["method"] == "CASH" and ag["custody_payment"][o["id"]]["custody"] == custody
                        for o in ag["orders"].get((vid, d), []))):
                    missing_counts.append({"vendor_id": vid, "business_date": d, "custody": custody, "pos_cash_cents": str(pos)})
                    groups.append({"match_key": f"MISSING_COUNT:{vid}:{d}:{custody}", "kind": "CASH_COUNT", "status": "CASE_REQUIRED",
                                   "variance_cents": str(pos), "reason_code": "MISSING_CASH_COUNT"})
                continue
            var = cents(c["amount_cents"]) - pos
            chk = {"cash_count_id": c["id"], "vendor_id": vid, "business_date": d, "custody": custody,
                   "counted_cents": c["amount_cents"], "pos_cash_cents": str(pos), "variance_cents": str(var),
                   "status": "MATCHED" if var == 0 else "CASE_REQUIRED", "source_refs": c["source_refs"]}
            cash_checks.append(chk)
            groups.append({"match_key": f"CASH_COUNT:{c['source_native_id']}", "kind": "CASH_COUNT", "status": chk["status"],
                           "variance_cents": str(var), "reason_code": "NORMAL" if var == 0 else "CASH_VARIANCE",
                           "source_refs": c["source_refs"]})
    for c in fx.cash_counts:
        if (c["vendor_id"], c["business_date"]) not in ag["agg"]:
            raise FixtureError(f"cash count without POS activity {c['source_native_id']}")
    # cash deposit slips vs bank and counts
    cc_by_id = {c["id"]: c for c in fx.cash_counts}
    bank_by_id = {b["id"]: b for b in fx.bank_entries}
    for s in fx.cash_deposit_slips:
        be = bank_by_id[s["bank_entry_id"]]
        explained.add(be["id"])
        bag_total = sum(cents(b["amount_cents"]) for b in s["bags"])
        unlabeled = [b for b in s["bags"] if b["cash_count_id"] is None]
        bad_bag = [b for b in s["bags"] if b["cash_count_id"] and cents(b["amount_cents"]) != cents(cc_by_id[b["cash_count_id"]]["amount_cents"])]
        var = signed(be) - bag_total
        groups.append({"match_key": f"CASH_DEPOSIT:{s['slip_reference']}", "kind": "CASH_DEPOSIT", "bank_entry_id": be["id"],
                       "variance_cents": str(var),
                       "unlabeled_bag_cents": str(sum(cents(b["amount_cents"]) for b in unlabeled)),
                       "reason_code": "UNLABELED_BAG" if unlabeled else ("BAG_COUNT_MISMATCH" if bad_bag else "NORMAL"),
                       "status": "CASE_REQUIRED" if (var or unlabeled or bad_bag) else "MATCHED",
                       "source_refs": s["source_refs"] + be["source_refs"]})
    for d in pc["duplicate_imports"]:
        groups.append({"match_key": f"DUPLICATE_IMPORT:{d['provider_reference']}", "kind": "DUPLICATE_IMPORT",
                       "status": "DUPLICATE_IGNORED", "variance_cents": "0", "reason_code": "DUPLICATE_IMPORT",
                       "duplicate_payment_id": d["duplicate_payment_id"], "kept_payment_id": d["kept_payment_id"]})
    for pid, dc in pc["duplicate_charges"].items():
        rev = [r for r, x in pc["reversals"].items() if x is dc]
        groups.append({"match_key": f"DUPLICATE_CHARGE:{dc['payment']['provider_reference']}", "kind": "DUPLICATE_CHARGE",
                       "status": "CASE_REQUIRED", "variance_cents": dc["payment"]["amount_cents"], "reason_code": "GENUINE_DUPLICATE_CHARGE",
                       "reversal_payment_ids": rev, "source_refs": dc["payment"]["source_refs"] + dc["original"]["source_refs"]})
    for kind, items, amt_key, sign in (("TICKETS", fx.ticket_settlements, "net_cents", 1),
                                       ("SPONSOR", fx.sponsorship_receipts, "amount_cents", 1),
                                       ("SUPPLIER_PAYMENT", fx.supplier_payments, "amount_cents", -1),
                                       ("EXPENSE", fx.expenses, "amount_cents", -1)):
        for it in items:
            be = bank_by_id[it["bank_entry_id"]]
            explained.add(be["id"])
            var = signed(be) - sign * cents(it[amt_key])
            groups.append({"match_key": f"{kind}:{be['provider_reference']}", "kind": kind, "bank_entry_id": be["id"],
                           "variance_cents": str(var), "status": "MATCHED" if var == 0 else "CASE_REQUIRED",
                           "reason_code": "NORMAL" if var == 0 else "AMOUNT_MISMATCH"})
    unmatched = [be for be in fx.bank_entries if be["id"] not in explained]
    for be in unmatched:
        groups.append({"match_key": f"BANK_UNMATCHED:{be['provider_reference']}", "kind": "BANK_UNMATCHED", "bank_entry_id": be["id"],
                       "variance_cents": str(signed(be)), "status": "CASE_REQUIRED", "reason_code": "UNMATCHED_BANK_ENTRY",
                       "source_refs": be["source_refs"]})
    # timing view at each statement time
    as_of = []
    for bd in fx.dates:
        t = fx.statement_time(bd)
        for b in fx.processor_batches:
            if b["business_date"] > bd:
                continue
            be = bank_by_ref.get(b["payout_reference"])
            arrived = be is not None and parse_utc(be["occurred_at"]) <= t
            as_of.append({"as_of_utc": fmt_utc(t), "statement_for": bd, "provider": b["provider"],
                          "batch_business_date": b["business_date"],
                          "state": "DEPOSITED" if arrived else "TIMING_PENDING_NOT_AVAILABLE_CASH"})
    return {"match_groups": groups, "cash_count_checks": cash_checks, "missing_cash_counts": missing_counts,
            "unmatched_bank_entries": [be["id"] for be in unmatched], "deposit_state_at_statement_time": as_of}


# ---------------------------------------------------------------- settlements
def settle(fx: Fixture, terms: dict, ag: dict, recon: dict, pc: dict, tax_lines: list, dec: Decisions) -> dict:
    limit = cents(fx.config["payout_controls"]["auto_limit_cents_per_vendor_day"])
    manifest_hash = hashlib.sha256(fx.manifest_bytes).hexdigest()
    settlements, reviews, obligations, corrections, policy_events = [], [], [], [], []
    cash_var = {(c["vendor_id"], c["business_date"]): c for c in recon["cash_count_checks"]
                if c["status"] != "MATCHED" and c["custody"] == "CENTRALLY_COLLECTED"}
    missing = {(m["vendor_id"], m["business_date"]): m for m in recon["missing_cash_counts"] if m["custody"] == "CENTRALLY_COLLECTED"}
    dup_by_vday = defaultdict(list)
    for dc in pc["duplicate_charges"].values():
        dup_by_vday[(dc["order"]["vendor_id"], dc["order"]["business_date"])].append(dc)
    tax_by_vday = defaultdict(list)
    for tl in tax_lines:
        tax_by_vday[(tl["order"]["vendor_id"], tl["order"]["business_date"])].append(tl)
    slip_refs = {}
    for s in fx.cash_deposit_slips:
        slip_refs[s["business_date"]] = s["source_refs"]

    for v in fx.vendors:
        vid, t = v["id"], terms[v["id"]]
        days = sorted(set(fx.dates) | {d for (x, d) in ag["agg"] if x == vid})
        cum = prior_assessed = open_bal = 0
        policy = None           # active reusable decision
        onetime = []            # one-time decisions taken: (day, bps, evidence_key)
        held_since = None
        settled_days = []       # dicts for corrections
        min_claim_done = False
        for d in days:
            a = ag["agg"].get((vid, d), {})
            T = fx.statement_time(d)
            tk = terms_at(t, T)
            elig_c, elig_r = a.get("elig_central", 0), a.get("elig_retained", 0)
            tax_c, tax_r, tips_c = a.get("tax_central", 0), a.get("tax_retained", 0), a.get("tips_central", 0)
            proc_fees = a.get("proc_fees", 0)
            deducted = proc_fees if tk["fees_deductible"] else 0
            cum += elig_c + elig_r
            refs = list(tk["refs"])
            holds = []
            vday_reviews = []
            # ---- non-terms reviews for this vendor-day (raised regardless of terms state)
            for dc in dup_by_vday.get((vid, d), []):
                decn = dec.find("DUPLICATE_CHARGE", d, v["name"])
                vday_reviews.append(review("DUPLICATE_CHARGE", "VENDOR", vid, d, True,
                                           "Genuine duplicate customer charge: not vendor sales; hold this vendor-day until a human confirms; reversal clears it.",
                                           decn, dc["payment"]["source_refs"] + dc["original"]["source_refs"],
                                           {"duplicate_amount_cents": dc["payment"]["amount_cents"]}, v))
                if not decn:
                    holds.append({"reason_code": "DUPLICATE_CHARGE_UNRESOLVED"})
            if (vid, d) in cash_var:
                c = cash_var[(vid, d)]
                decn = dec.find("CASH_VARIANCE", d, v["name"])
                vday_reviews.append(review("CASH_VARIANCE", "VENDOR", vid, d, True,
                                           "Cash-office count differs from POS cash: case, hold only this vendor-day, never silently write off.",
                                           decn, c["source_refs"], {"variance_cents": c["variance_cents"]}, v))
                if not decn:
                    holds.append({"reason_code": "CASH_VARIANCE_UNRESOLVED"})
            if (vid, d) in missing:
                decn = dec.find("MISSING_CASH_COUNT", d, v["name"])
                vday_reviews.append(review("MISSING_CASH_COUNT", "VENDOR", vid, d, True,
                                           "POS shows cash-office cash but no count exists; hold this vendor-day until a human accepts other evidence.",
                                           decn, slip_refs.get(d, []), {"pos_cash_cents": missing[(vid, d)]["pos_cash_cents"]}, v))
                if not decn:
                    holds.append({"reason_code": "MISSING_CASH_COUNT_UNRESOLVED"})
            if tax_by_vday.get((vid, d)):
                tl = tax_by_vday[(vid, d)]
                decn = dec.find("TAX_MISMATCH", d, v["name"])
                vday_reviews.append(review("TAX_MISMATCH", "VENDOR", vid, d, False,
                                           "Supplied POS tax differs from recalculated DEMO tax: keep supplied tax as the liability, show both on the DRAFT tax schedule; payout unaffected.",
                                           decn, [x["line"]["source_refs"][0] for x in tl[:5]],
                                           {"lines": len(tl), "supplied_tax_cents": str(sum(x["supplied"] for x in tl)),
                                            "recalculated_tax_cents": str(sum(x["recalculated"] for x in tl))}, v))
            if not min_claim_done and d in fx.dates:
                mc = [c for c in t["minimum_period_claims"] if parse_utc(c["received_at"]) <= T]
                if mc:
                    min_claim_done = True
                    vday_reviews.append(review("MINIMUM_PERIOD_CLAIM", "VENDOR", vid, d, False,
                                               "A source claims a nightly minimum; the agreement says once per festival. Apply the minimum once at final settlement; do not hold.",
                                               dec.find("MINIMUM_PERIOD_CLAIM", d, v["name"]),
                                               [mc[0]["evidence"], t["evidence"]["minimum"]], {"minimum_cents": str(t["minimum_cents"])}, v))
            # ---- terms resolution
            bps, basis = tk["bps"], "CONTRACT" if not tk["amendment_ids"] else "CONTRACT_AS_AMENDED"
            corr_here = []
            last_ev = policy["evidence_key"] if policy else (onetime[-1][2] if onetime else None)
            if held_since:
                pass
            elif (policy or onetime) and tk["evidence_key"][0] != last_ev[0]:
                # new signed evidence (amendment) after a human decision -> challenge / re-escalation
                decn = dec.find("SUPERSEDING_EVIDENCE", d, v["name"])
                prev = [(s["business_date"], s["commission_bps"]) for s in settled_days]
                preview = []
                pt = 0
                for s in settled_days:
                    ct = pct_of(s["cum"], tk["bps"]) if s["business_date"] < fx.final_day else max(pct_of(s["cum"], tk["bps"]), tk["minimum_cents"])
                    preview.append({"business_date": s["business_date"], "original_assessed_cents": str(s["assessed"]),
                                    "corrected_assessed_cents": str(ct - pt), "delta_cents": str(ct - pt - s["assessed"]),
                                    "original_payout_state": "PAID_OR_SCHEDULED", "original_commission_bps": s["commission_bps"]})
                    pt = ct
                vday_reviews.append(review("SUPERSEDING_EVIDENCE", "VENDOR", vid, d, True,
                                           ("New signed evidence contradicts the applied policy/decision: pause policy use, preview every affected "
                                            "settlement/payout/journal, require correction approval; completed payouts are recovered by approved offset, "
                                            "posted entries corrected by linked entries."),
                                           decn, [r for r in tk["refs"] if r not in t["evidence"].values()][:2] + [t["evidence"]["rate"]],
                                           {"new_commission_bps": tk["bps"], "applied_history": prev, "impact_preview": preview,
                                            "total_delta_cents": str(sum(int(p["delta_cents"]) for p in preview))}, v))
                if policy:
                    policy_events.append({"vendor_id": vid, "vendor_name": v["name"], "business_date": d,
                                          "policy_from_decision": policy["decision"]["rule_id"],
                                          "event": "SUSPENDED_PENDING_REVIEW" if not decn else "SUPERSEDED",
                                          "superseded_by_source_ids": list(tk["amendment_ids"])})
                if decn:
                    new_prior = 0
                    for s in settled_days:
                        ct = pct_of(s["cum"], tk["bps"]) if s["business_date"] < fx.final_day else max(pct_of(s["cum"], tk["bps"]), tk["minimum_cents"])
                        delta = (ct - new_prior) - s["assessed"]
                        new_prior = ct
                        if delta:
                            corr_here.append({"vendor_id": vid, "vendor_name": v["name"], "corrects_business_date": s["business_date"],
                                              "corrected_on_business_date": d, "original_assessed_cents": str(s["assessed"]),
                                              "corrected_assessed_cents": str(s["assessed"] + delta), "delta_cents": str(delta),
                                              "correction_of_entry": f"COMMISSION:{v['name']}:{s['business_date']}",
                                              "recovery": "OFFSET_FROM_NEXT_PAYOUT_APPROVED", "approved_by_decision": decn["rule_id"]})
                    open_bal += sum(int(c["delta_cents"]) for c in corr_here)
                    prior_assessed = new_prior
                    corrections.extend(corr_here)
                    policy, onetime = None, []
                    basis = "CORRECTED_AFTER_CHALLENGE"
                    refs.append({"human_decision": decn["rule_id"], "role": "CORRECTION_APPROVAL"})
                else:
                    held_since = d
            elif policy and tk["evidence_key"] == policy["evidence_key"]:
                bps, basis = policy["bps"], "APPROVED_REUSABLE_POLICY"
                refs.append({"policy_from_decision": policy["decision"]["rule_id"], "role": "APPLIED_POLICY",
                             "originating_case": f"CONFLICTING_TERMS/{v['name']}/{policy['since']}"})
            elif tk["conflicting_claims"]:
                decn = dec.find("CONFLICTING_TERMS", d, v["name"])
                cands = sorted({tk["bps"]} | {c["bps"] for c in tk["conflicting_claims"]})
                vday_reviews.append(review("CONFLICTING_TERMS", "VENDOR", vid, d, True,
                                           "Hold this vendor only; show both sources and the fee impact of each rate; do not pick a rate autonomously.",
                                           decn, [tk["refs"][-1]] + [c["evidence"] for c in tk["conflicting_claims"]],
                                           {"cumulative_eligible_cents": str(cum),
                                            "cumulative_fee_by_candidate_rate_cents": {f"{b}bps": str(pct_of(cum, b)) for b in cands}}, v))
                if decn:
                    chosen = tk["conflicting_claims"][-1]["bps"] if decn["decision"] == "CHOOSE_OTHER_CLAIM" else tk["bps"]
                    bps, basis = chosen, "HUMAN_DECISION_" + decn["scope"]
                    refs.append({"human_decision": decn["rule_id"], "role": "CASE_RESOLUTION"})
                    if decn["scope"] == "REUSABLE_VENDOR_FESTIVAL":
                        policy = {"bps": chosen, "decision": decn, "evidence_key": tk["evidence_key"], "since": d}
                        policy_events.append({"vendor_id": vid, "vendor_name": v["name"], "business_date": d,
                                              "policy_from_decision": decn["rule_id"], "event": "ACTIVATED",
                                              "scope": {"vendor_id": vid, "festival_id": fx.festival["id"]}, "commission_bps": chosen})
                    else:
                        onetime.append((d, chosen, tk["evidence_key"]))
                else:
                    held_since = d
            reviews.extend(vday_reviews)
            base_lines = [{"code": "ELIGIBLE_SALES_CENTRAL", "amount_cents": str(elig_c)},
                          {"code": "TAX_COLLECTED_CENTRAL_PROTECTED", "amount_cents": str(tax_c)},
                          {"code": "TIPS_COLLECTED_CENTRAL_PROTECTED", "amount_cents": str(tips_c)}]
            if held_since:
                holds.insert(0, {"reason_code": "CONFLICTING_TERMS_UNRESOLVED", "since": held_since})
                settlements.append({"vendor_id": vid, "vendor_name": v["name"], "festival_id": fx.festival["id"], "business_date": d,
                                    "status": "HELD", "phase": phase(fx, d), "calculation_version": ORACLE_VERSION,
                                    "source_manifest_hash": manifest_hash, "commission_bps": None, "terms_basis": "UNRESOLVED",
                                    "policy_refs": refs, "line_items": base_lines, "assessed_fee_cents": None,
                                    "collected_fee_cents": None, "payout_cents": "0", "tips_passthrough_cents": "0",
                                    "unpaid_balance_cents": None, "receivable_change_cents": None, "holds": holds,
                                    "_j": None})
                obligations.append({"vendor": v, "business_date": d, "held_static": [h["reason_code"] for h in holds],
                                    "amount": 0, "tips": 0, "transfer": 0, "scheduled": fx.payout_time(d), "settled": False})
                continue
            # ---- fee math (C02 cumulative; minimum once on/after final day)
            pct_cum = pct_of(cum, bps)
            target = max(pct_cum, tk["minimum_cents"]) if d >= fx.final_day else pct_cum
            assessed = target - prior_assessed
            due = assessed + open_bal
            u0 = open_bal - sum(int(c["delta_cents"]) for c in corr_here)
            avail = elig_c - deducted
            net = avail - due
            payout = max(0, net)
            new_open = max(0, -net)
            collected = due if due <= 0 else min(due, max(avail, 0))
            receivable_delta = new_open - open_bal
            prior = prior_assessed
            prior_corr = sum(int(c["delta_cents"]) for c in corr_here)
            prior_assessed, open_bal = target, new_open
            transfer = payout + tips_c
            if transfer > limit:
                decn = dec.find("PAYOUT_ABOVE_AUTO_LIMIT", d, v["name"])
                reviews.append(review("PAYOUT_ABOVE_AUTO_LIMIT", "VENDOR", vid, d, True,
                                      "Hold payout until finance-lead approval bound to this amount and version; splitting cannot bypass the limit.",
                                      decn, [], {"transfer_cents": str(transfer), "auto_limit_cents": str(limit)}, v, "finance_lead"))
                if not (decn and decn["decision"] == "APPROVE"):
                    holds.append({"reason_code": "PAYOUT_ABOVE_AUTO_LIMIT"})
            if d == fx.final_day and new_open > 0:
                reviews.append(review("UNPAID_BALANCE", "VENDOR", vid, d, False,
                                      "Record receivable for human collection or waiver; never take tax/tips; no negative payout.",
                                      dec.find("UNPAID_BALANCE", d, v["name"]), [t["evidence"]["minimum"]],
                                      {"unpaid_balance_cents": str(new_open)}, v))
            if d > fx.final_day and receivable_delta > 0:
                cbs = ag["chargebacks"].get((vid, d), [])
                reason = "CHARGEBACK_RECOVERY" if cbs else "LATE_REFUND_RECOVERY"
                ev = [r for p, _ in cbs for r in p["source_refs"]] or [r for o in ag["orders"].get((vid, d), []) for r in o["source_refs"]][:5]
                reviews.append(review(reason, "VENDOR", vid, d, False,
                                      "Linked adjustment + balanced correcting entries; no rewrite of original postings; recovery or waiver by a human; no negative payout.",
                                      dec.find(reason, d, v["name"]), ev,
                                      {"eligible_reduction_cents": str(elig_c), "fee_change_cents": str(assessed),
                                       "recovery_receivable_cents": str(receivable_delta)}, v))
            settled_days.append({"business_date": d, "cum": cum, "assessed": assessed, "commission_bps": bps})
            st = {"vendor_id": vid, "vendor_name": v["name"], "festival_id": fx.festival["id"], "business_date": d,
                  "status": "HELD" if holds else "CALCULATED", "phase": phase(fx, d), "calculation_version": ORACLE_VERSION,
                  "source_manifest_hash": manifest_hash, "terms_basis": basis, "commission_bps": bps, "policy_refs": refs,
                  "line_items": [
                      {"code": "ELIGIBLE_SALES_CENTRAL", "amount_cents": str(elig_c)},
                      {"code": "ELIGIBLE_SALES_VENDOR_RETAINED_CASH", "amount_cents": str(elig_r)},
                      {"code": "REFUNDS_INCLUDED_IN_ELIGIBLE", "amount_cents": str(a.get("refunds_elig", 0))},
                      {"code": "CHARGEBACKS_INCLUDED_IN_ELIGIBLE", "amount_cents": str(a.get("chargeback_elig", 0))},
                      {"code": "CUMULATIVE_ELIGIBLE", "amount_cents": str(cum)},
                      {"code": "CUMULATIVE_PERCENT_FEE", "amount_cents": str(pct_cum)},
                      {"code": "MINIMUM_ADJUSTMENT_CUMULATIVE", "amount_cents": str(target - pct_cum)},
                      {"code": "PRIOR_ASSESSED", "amount_cents": str(prior)},
                      {"code": "PRIOR_CORRECTIONS", "amount_cents": str(prior_corr)},
                      {"code": "PRIOR_UNPAID_BALANCE", "amount_cents": str(u0)},
                      {"code": "PROCESSOR_FEES_ON_VENDOR_PAYMENTS", "amount_cents": str(proc_fees)},
                      {"code": "PROCESSOR_FEES_DEDUCTED", "amount_cents": str(deducted)},
                      {"code": "TAX_COLLECTED_CENTRAL_PROTECTED", "amount_cents": str(tax_c)},
                      {"code": "TAX_IN_VENDOR_RETAINED_CASH_MEMO", "amount_cents": str(tax_r)},
                      {"code": "TIPS_PASSTHROUGH", "amount_cents": str(tips_c)}],
                  "assessed_fee_cents": str(assessed), "collected_fee_cents": str(collected), "payout_cents": str(payout),
                  "tips_passthrough_cents": str(tips_c), "unpaid_balance_cents": str(new_open),
                  "receivable_change_cents": str(new_open - u0), "holds": holds,
                  "_j": {"assessed": assessed, "receivable_delta": receivable_delta, "deducted": deducted,
                         "collected_from_payable": assessed - receivable_delta, "corrections": corr_here}}
            settlements.append(st)
            obligations.append({"vendor": v, "business_date": d, "held_static": [h["reason_code"] for h in holds],
                                "amount": payout, "tips": tips_c, "transfer": transfer, "scheduled": fx.payout_time(d), "settled": True})
    return {"settlements": settlements, "reviews": reviews, "obligations": obligations, "corrections": corrections,
            "policy_events": policy_events}


def phase(fx, d):
    return "POST_FESTIVAL_ADJUSTMENT" if d > fx.final_day else ("FINAL" if d == fx.final_day else "INTERIM")


# ---------------------------------------------------------------- payout windows (cash, beneficiary, rail outcomes)
def run_payouts(fx: Fixture, res: dict, dec: Decisions) -> dict:
    opening = sum(cents(o["amount_cents"]) for o in fx.opening_balances if o["account_role"] == "BANK_OPERATING")
    bank = sorted(((parse_utc(b["occurred_at"]), cents(b["amount_cents"]) * (1 if b["direction"] == "credit" else -1))
                   for b in fx.bank_entries))
    script = {(i["vendor_id"], i["business_date"]): i["attempts"] for i in fx.sim["items"]}
    ben = {b["vendor_id"]: b for b in fx.beneficiary_change_requests}
    ben_review_done, ben_verified = {}, {}
    failed = {}
    reviews, windows_out, payouts = [], [], []
    by_time = defaultdict(list)
    for ob in res["obligations"]:
        by_time[ob["scheduled"]].append(ob)
    carried = []
    released_before = 0
    for T in sorted(by_time):
        avail = opening + sum(a for t, a in bank if t <= T) - released_before
        queue = sorted(carried, key=lambda o: (o["business_date"], fx.booth[o["vendor"]["id"]])) + \
            sorted(by_time[T], key=lambda o: fx.booth[o["vendor"]["id"]])
        carried = []
        short = []
        requested = released = 0
        for ob in queue:
            v = ob["vendor"]
            reasons = list(ob["held_static"])
            b = ben.get(v["id"])
            ben_version = 1
            if b and parse_utc(b["received_at"]) < ob["scheduled"] and ob["settled"]:
                if v["id"] not in ben_review_done:
                    decn = dec.find("BENEFICIARY_CHANGED_UNVERIFIED", ob["business_date"], v["name"])
                    ben_review_done[v["id"]] = True
                    ben_verified[v["id"]] = bool(decn and decn["decision"] == "VERIFIED_BY_CALLBACK")
                    reviews.append(review("BENEFICIARY_CHANGED_UNVERIFIED", "VENDOR", v["id"], ob["business_date"], True,
                                          ("Unverified payout-account change blocks every later payout for this vendor until a human verifies it; "
                                           "instructions embedded in the email are untrusted data."),
                                          decn, b["source_refs"], {"requested_beneficiary_version": b["requested_beneficiary_version"]}, v))
                if ben_verified[v["id"]]:
                    ben_version = b["requested_beneficiary_version"]
                else:
                    reasons.append("BENEFICIARY_CHANGED_UNVERIFIED")
            if v["id"] in failed and failed[v["id"]] != ob["business_date"]:
                fd = dec.find("PAYOUT_FAILED", failed[v["id"]], v["name"])
                if not (fd and fd["decision"] == "RETRY_TO_VERIFIED_NEW_BENEFICIARY"):
                    reasons.append("BENEFICIARY_INVALID_AFTER_FAILED_PAYOUT")
            out = {"vendor_id": v["id"], "vendor_name": v["name"], "business_date": ob["business_date"],
                   "logical_obligation_key": f"payout:{v['id']}:{ob['business_date']}:1",
                   "idempotency_key": f"payout:{v['id']}:{ob['business_date']}:1",
                   "amount_cents": str(ob["amount"]), "tips_passthrough_cents": str(ob["tips"]), "transfer_cents": str(ob["transfer"]),
                   "scheduled_at_utc": fmt_utc(ob["scheduled"]), "beneficiary_version": ben_version,
                   "requires_finance_lead": ob["transfer"] > cents(fx.config["payout_controls"]["auto_limit_cents_per_vendor_day"]),
                   "provider_submissions": 0, "confirmed_provider_payments": 0, "released_at_utc": None,
                   "cash_order_dependent": False}
            if reasons:
                out.update(final_state="HELD", hold_reasons=reasons)
                payouts.append(out)
                continue
            if ob["transfer"] == 0:
                out.update(final_state="NOTHING_TO_PAY", hold_reasons=[])
                payouts.append(out)
                continue
            requested += ob["transfer"]
            if ob["transfer"] > avail:
                short.append(ob)
                ob.setdefault("short_at", []).append(fmt_utc(T))
                carried.append(ob)
                continue
            attempts = script.get((v["id"], ob["business_date"]), [{"attempt": 1, "submit_outcome": "CONFIRMED"}])
            final = attempts[-1].get("lookup_outcome") if attempts[-1]["submit_outcome"] == "UNKNOWN_TIMEOUT" else attempts[-1]["submit_outcome"]
            out["provider_submissions"] = len(attempts)
            out["outcome_path"] = [a["submit_outcome"] + (f"->{a['lookup_outcome']}" if "lookup_outcome" in a else "") for a in attempts]
            out["cash_order_dependent"] = bool(ob.get("short_at"))
            if ob.get("short_at"):
                out["held_insufficient_cash_at_utc"] = ob["short_at"]
            if final == "FAILED":
                out.update(final_state="FAILED", hold_reasons=[], failure_code=attempts[-1].get("failure_code"))
                failed[v["id"]] = ob["business_date"]
                reviews.append(review("PAYOUT_FAILED", "VENDOR", v["id"], ob["business_date"], True,
                                      ("Permanent rail failure: payout FAILED, obligation stays payable, no automatic retry with a new key; "
                                       "later payouts for this vendor held until a verified beneficiary exists."),
                                      dec.find("PAYOUT_FAILED", ob["business_date"], v["name"]), [],
                                      {"transfer_cents": str(ob["transfer"]), "failure_code": attempts[-1].get("failure_code")}, v))
            elif final == "CONFIRMED":
                out.update(final_state="CONFIRMED", hold_reasons=[], confirmed_provider_payments=1, released_at_utc=fmt_utc(T))
                avail -= ob["transfer"]
                released_before += ob["transfer"]
                released += ob["transfer"]
            else:
                raise FixtureError(f"unsupported scripted final outcome {final}")
            payouts.append(out)
        if short:
            d = (T.astimezone(fx.tz).date() - timedelta(days=1)).isoformat()
            reviews.append(review("INSUFFICIENT_CASH", "SYSTEM", None, d, True,
                                  "Available cash below release-eligible payouts: release only what fits (no overspend), hold the rest, retry at the next window; no model discretion.",
                                  dec.find("INSUFFICIENT_CASH", d, None, "SYSTEM"), [],
                                  {"held_obligations": len(short), "held_transfer_cents": str(sum(o["transfer"] for o in short))}))
        windows_out.append({"payout_time_utc": fmt_utc(T), "available_before_cents": str(avail + released),
                            "requested_cents": str(requested), "released_cents": str(released),
                            "remaining_cents": str(avail), "held_for_cash": len(short), "sufficient": not short})
    for ob in carried:
        v = ob["vendor"]
        payouts.append({"vendor_id": v["id"], "vendor_name": v["name"], "business_date": ob["business_date"],
                        "logical_obligation_key": f"payout:{v['id']}:{ob['business_date']}:1",
                        "idempotency_key": f"payout:{v['id']}:{ob['business_date']}:1", "amount_cents": str(ob["amount"]),
                        "tips_passthrough_cents": str(ob["tips"]), "transfer_cents": str(ob["transfer"]),
                        "scheduled_at_utc": fmt_utc(ob["scheduled"]), "final_state": "HELD", "hold_reasons": ["INSUFFICIENT_CASH"],
                        "provider_submissions": 0, "confirmed_provider_payments": 0, "released_at_utc": None,
                        "cash_order_dependent": True, "beneficiary_version": 1, "requires_finance_lead": False})
    payouts.sort(key=lambda p: (p["business_date"], fx.booth[p["vendor_id"]]))
    return {"payouts": payouts, "reviews": reviews, "cash_windows": windows_out}


# ---------------------------------------------------------------- payables, receivables, tax
def payables(fx: Fixture, dec: Decisions) -> dict:
    po_by_id = {p["id"]: p for p in fx.purchase_orders}
    sup = {s["id"]: s for s in fx.suppliers}
    reviews, invoices = [], []
    seen = {}
    dups = set()
    for inv in sorted(fx.supplier_invoices, key=lambda i: (i["received_at"], i["id"])):
        if inv["kind"] != "INVOICE":
            continue
        k = (inv["supplier_id"], inv["invoice_number"])
        if k in seen:
            dups.add(inv["id"])
            d = fx.local_date(inv["received_at"])
            reviews.append(review("DUPLICATE_SUPPLIER_INVOICE", "SUPPLIER_INVOICE", inv["id"], d, True,
                                  "Same supplier and invoice number already received: never post or pay twice; human confirms rejection.",
                                  dec.find("DUPLICATE_SUPPLIER_INVOICE", d, None, "SUPPLIER_INVOICE"),
                                  inv["source_refs"] + seen[k]["source_refs"], {"gross_cents": inv["gross_cents"]}))
        else:
            seen[k] = inv
    paid = defaultdict(int)
    for p in fx.supplier_payments:
        for iid in p["invoice_ids"]:
            paid[iid] += cents(p["amount_cents"])
    credited = defaultdict(int)
    for cn in fx.supplier_invoices:
        if cn["kind"] == "CREDIT_NOTE":
            credited[cn["applies_to_invoice_id"]] += cents(cn["gross_cents"])
    po_used = defaultdict(int)
    blocked = set()
    for inv in sorted(fx.supplier_invoices, key=lambda i: (i["invoice_date"], i["id"])):
        if inv["kind"] != "INVOICE" or inv["id"] in dups:
            continue
        po = po_by_id[inv["po_refs"][0]]
        po_used[po["id"]] += cents(inv["gross_cents"]) - credited[inv["id"]]
        if po_used[po["id"]] > cents(po["amount_cents"]):
            decn = dec.find("INVOICE_EXCEEDS_PO", inv["invoice_date"], None, "SUPPLIER_INVOICE")
            reviews.append(review("INVOICE_EXCEEDS_PO", "SUPPLIER_INVOICE", inv["id"], inv["invoice_date"], True,
                                  "Invoice total exceeds the remaining PO amount: post the liability, block payment until a human approves the variance.",
                                  decn, inv["source_refs"] + po["source_refs"],
                                  {"po_amount_cents": po["amount_cents"], "invoiced_to_date_cents": str(po_used[po["id"]]),
                                   "excess_cents": str(po_used[po["id"]] - cents(po["amount_cents"]))}))
            if not (decn and decn["decision"] == "APPROVE_VARIANCE"):
                blocked.add(inv["id"])
    for inv in sorted(fx.supplier_invoices, key=lambda i: (i["invoice_date"], i["invoice_number"], i["id"])):
        if inv["kind"] != "INVOICE":
            continue
        if inv["id"] in dups:
            invoices.append({"invoice_id": inv["id"], "supplier_name": sup[inv["supplier_id"]]["name"], "invoice_number": inv["invoice_number"],
                             "status": "DUPLICATE_NOT_POSTED", "outstanding_cents": "0", "gross_cents": inv["gross_cents"]})
            continue
        out = cents(inv["gross_cents"]) - credited[inv["id"]] - paid[inv["id"]]
        if out < 0:
            raise FixtureError(f"overpaid supplier invoice {inv['invoice_number']}")
        status = "PAID" if out == 0 else ("PAYMENT_BLOCKED" if inv["id"] in blocked else
                                          "OVERDUE" if inv["due_date"] < AS_OF_PAYABLES else "OPEN")
        invoices.append({"invoice_id": inv["id"], "supplier_name": sup[inv["supplier_id"]]["name"], "invoice_number": inv["invoice_number"],
                         "invoice_date": inv["invoice_date"], "due_date": inv["due_date"], "gross_cents": inv["gross_cents"],
                         "credited_cents": str(credited[inv["id"]]), "paid_cents": str(paid[inv["id"]]),
                         "outstanding_cents": str(out), "status": status})
    receivables = []
    rec = defaultdict(int)
    for r in fx.sponsorship_receipts:
        rec[(r["sponsor_id"], r["invoice_number"])] += cents(r["amount_cents"])
    for s in fx.sponsors:
        for i in s["invoices"]:
            got = rec[(s["id"], i["invoice_number"])]
            receivables.append({"sponsor_name": s["name"], "invoice_number": i["invoice_number"], "amount_cents": i["amount_cents"],
                                "received_cents": str(got), "outstanding_memo_cents": str(cents(i["amount_cents"]) - got),
                                "status": "PAID" if got == cents(i["amount_cents"]) else "PARTIALLY_PAID"})
    return {"as_of": AS_OF_PAYABLES, "supplier_invoices": invoices, "duplicate_invoice_ids": sorted(dups),
            "sponsorship_receivables_memo": receivables, "reviews": reviews,
            "total_outstanding_cents": str(sum(int(i["outstanding_cents"]) for i in invoices))}


def tax_summary(fx: Fixture, ag: dict, tax_lines: list) -> dict:
    by_date = defaultdict(lambda: defaultdict(int))
    for (vid, d), a in ag["agg"].items():
        x = by_date[d]
        x["eligible_central_cents"] += a.get("elig_central", 0)
        x["tax_supplied_central_cents"] += a.get("tax_central", 0)
        x["tax_vendor_retained_memo_cents"] += a.get("tax_retained", 0)
    for tl in tax_lines:
        p = ag["custody_payment"][tl["order"]["id"]]
        if p["custody"] == "CENTRALLY_COLLECTED":
            by_date[tl["order"]["business_date"]]["tax_variance_central_cents"] += tl["supplied"] - tl["recalculated"]
    rows = []
    for d in sorted(by_date):
        x = by_date[d]
        rows.append({"business_date": d, **{k: str(v) for k, v in sorted(x.items())},
                     "tax_recalculated_central_cents": str(x["tax_supplied_central_cents"] - x.get("tax_variance_central_cents", 0))})
    return {"status": "DRAFT, NOT FILED - DEMO fictional rate, no compliance claim", "by_business_date": rows,
            "mismatch_lines": [{"order_id": tl["order"]["id"], "line_id": tl["line"]["line_id"], "supplied_tax_cents": str(tl["supplied"]),
                                "recalculated_tax_cents": str(tl["recalculated"])} for tl in tax_lines]}


# ---------------------------------------------------------------- journals
def journals(fx: Fixture, pc: dict, ag: dict, res: dict, pays: dict, pay_info: dict, dec: Decisions) -> list[dict]:
    entries = []

    def add(key, bdate, kind, lines, refs, correction_of=None, note=None):
        norm = []
        for acct, dr, cr in lines:
            if not dr and not cr:
                continue
            if dr < 0 or cr < 0:
                dr, cr = max(dr, 0) + max(-cr, 0), max(cr, 0) + max(-dr, 0)
            norm.append({"account": acct, "account_role": ROLE[acct], "debit_cents": str(dr), "credit_cents": str(cr)})
        if not norm:
            return
        e = {"entry_key": key, "business_date": bdate, "kind": kind, "posting_rule_version": "DRAFT-posting-map-full-0.1",
             "source_record_ids": refs, "correction_of": correction_of, "lines": norm}
        if note:
            e["note"] = note
        entries.append(e)

    bank_by_id = {b["id"]: b for b in fx.bank_entries}
    open_amt = sum(cents(o["amount_cents"]) for o in fx.opening_balances)
    add("OPENING", fx.festival["bank_accounts"][0]["opening_as_of"], "OPENING_BALANCE", [("1000", open_amt, 0), ("3000", 0, open_amt)],
        [o["id"] for o in fx.opening_balances])
    vname = {v["id"]: v["name"] for v in fx.vendors}
    # sales / refund summaries per vendor, day, asset, kind
    summ = defaultdict(lambda: [0, 0, 0, 0, []])
    orig_date = {}
    for o in fx.orders:
        p = ag["custody_payment"][o["id"]]
        if p["custody"] != "CENTRALLY_COLLECTED":
            continue
        asset = "1100" if p["method"] in ("CARD", "WRISTBAND") else "1050"
        k = (o["vendor_id"], o["business_date"], asset, o["kind"])
        s = summ[k]
        s[0] += cents(o["eligible_net_cents"]); s[1] += cents(o["tax_cents"]); s[2] += cents(o["tips_cents"]); s[3] += cents(o["gross_cents"])
        s[4].append(o["id"])
        if o["kind"] == "REFUND":
            od = fx.order_by_id[o["refund_of_order_id"]]["business_date"]
            orig_date.setdefault(k, od)
    for (vid, d, asset, kind), (e, t, tp, g, ids) in sorted(summ.items()):
        tag = f"{vname[vid]}:{d}:{asset}"
        if kind == "SALE":
            add(f"SALES:{tag}", d, "SALES_CAPTURE_SUMMARY", [(asset, g, 0), ("2000", 0, e), ("2100", 0, t), ("2200", 0, tp)], ids)
        else:
            add(f"REFUNDS:{tag}", d, "REFUND_SUMMARY", [("2000", -e, 0), ("2100", -t, 0), ("2200", -tp, 0), (asset, 0, -g)], ids,
                correction_of=f"SALES:{vname[vid]}:{orig_date[(vid, d, asset, kind)]}:{asset}",
                note="Linked to the original sales summary; original posting untouched.")
    for (vid, d), cbs in sorted(ag["chargebacks"].items()):
        for p, o in cbs:
            add(f"CHARGEBACK:{p['provider_reference']}", d, "CHARGEBACK",
                [("2000", cents(o["eligible_net_cents"]), 0), ("2100", cents(o["tax_cents"]), 0), ("2200", cents(o["tips_cents"]), 0),
                 ("1100", 0, cents(o["gross_cents"]))], [p["id"], o["id"]],
                correction_of=f"SALES:{vname[vid]}:{o['business_date']}:1100")
    for pid, dc in sorted(pc["duplicate_charges"].items()):
        p = dc["payment"]
        add(f"DUPCHARGE:{p['provider_reference']}", p["business_date"], "DUPLICATE_CHARGE_CAPTURE",
            [("1100", cents(p["amount_cents"]), 0), ("2500", 0, cents(p["amount_cents"]))], [p["id"]],
            note="Customer money, not vendor sales.")
    for rid, dc in sorted(pc["reversals"].items()):
        p = next(x for x in pc["canonical"] if x["id"] == rid)
        add(f"DUPREVERSAL:{p['provider_reference']}", p["business_date"], "DUPLICATE_CHARGE_REVERSAL",
            [("2500", -cents(p["amount_cents"]), 0), ("1100", 0, -cents(p["amount_cents"]))], [p["id"]],
            correction_of=f"DUPCHARGE:{dc['payment']['provider_reference']}")
    by_ref = defaultdict(list)
    for b in fx.processor_batches:
        by_ref[b["payout_reference"]].append(b)
    for be in fx.bank_entries:
        bd = fx.local_date(be["occurred_at"])
        amt = cents(be["amount_cents"])
        sgn = amt if be["direction"] == "credit" else -amt
        if be["provider_reference"] in by_ref:
            mem = by_ref[be["provider_reference"]]
            add(f"DEPOSIT:{be['provider_reference']}", bd, "PROCESSOR_SETTLEMENT",
                [("1000", sgn, 0), ("5100", sum(cents(b["fees_cents"]) for b in mem), 0), ("1100", 0, sum(cents(b["gross_cents"]) for b in mem))],
                [be["id"]] + [b["id"] for b in mem])
    for s in fx.cash_deposit_slips:
        be = bank_by_id[s["bank_entry_id"]]
        add(f"CASHDEP:{s['slip_reference']}", fx.local_date(be["occurred_at"]), "CASH_OFFICE_DEPOSIT",
            [("1000", cents(be["amount_cents"]), 0), ("1050", 0, cents(be["amount_cents"]))], [be["id"], s["id"]])
    for r in res["reviews"]:
        if r["reason_code"] == "CASH_VARIANCE" and r["scripted_human_decision"]:
            var = int(r["financial_impact"]["variance_cents"])
            add(f"CASH_SHORT:{r['vendor_name']}:{r['business_date']}", r["business_date"], "CASH_OVER_SHORT",
                [("5200", -var, 0), ("1050", 0, -var)], [r["subject_id"]], note="Organizer absorbs the counted shortage (human decision).")
    final = fx.final_day
    for s in res["settlements"]:
        j = s["_j"]
        if j is None:
            continue
        tag = f"{s['vendor_name']}:{s['business_date']}"
        for c in j["corrections"]:
            dlt = int(c["delta_cents"])
            add(f"COMMISSION_CORRECTION:{s['vendor_name']}:{c['corrects_business_date']}", s["business_date"], "COMMISSION_CORRECTION",
                [("1200", dlt, 0), ("4000", 0, dlt)], [s["vendor_id"]], correction_of=c["correction_of_entry"],
                note="Approved correction after challenge; original entry preserved; recovered by offset.")
        if j["deducted"]:
            add(f"FEE_RECHARGE:{tag}", s["business_date"], "PROCESSOR_FEE_RECHARGE", [("2000", j["deducted"], 0), ("5100", 0, j["deducted"])],
                [s["vendor_id"]])
        rd = j["receivable_delta"]
        if j["assessed"] or rd:
            add(f"COMMISSION:{tag}", s["business_date"], "COMMISSION_SETTLEMENT",
                [("2000", j["collected_from_payable"], 0), ("1200", rd, 0), ("4000", 0, j["assessed"])], [s["vendor_id"]],
                correction_of=(f"COMMISSION:{s['vendor_name']}:{final}" if s["phase"] == "POST_FESTIVAL_ADJUSTMENT" else None))
    for p in pays["payouts"]:
        if p["final_state"] != "CONFIRMED":
            continue
        a, tp = cents(p["amount_cents"]), cents(p["tips_passthrough_cents"])
        add(f"PAYOUT:{p['vendor_name']}:{p['business_date']}", fx.local_date(p["released_at_utc"]), "VENDOR_PAYOUT_SIMULATED",
            [("2000", a, 0), ("2200", tp, 0), ("1000", 0, a + tp)], [p["vendor_id"]], note="Simulated payout, CONFIRMED.")
    end = fx.final_day
    recog = 0
    for t in fx.ticket_settlements:
        d = fx.local_date(t["paid_at"])
        g, f, n = cents(t["gross_cents"]), cents(t["platform_fees_cents"]), cents(t["net_cents"])
        acct = "2400" if d <= end else "4100"
        if d <= end:
            recog += g
        add(f"TICKETS:{t['settlement_reference']}", d, "TICKET_SETTLEMENT", [("1000", n, 0), ("5110", f, 0), (acct, 0, g)], [t["id"], t["bank_entry_id"]])
    add(f"TICKET_RECOGNITION:{end}", end, "TICKET_REVENUE_RECOGNITION", [("2400", recog, 0), ("4100", 0, recog)],
        [t["id"] for t in fx.ticket_settlements if fx.local_date(t["paid_at"]) <= end])
    for r in fx.sponsorship_receipts:
        add(f"SPONSOR:{bank_by_id[r['bank_entry_id']]['provider_reference']}", fx.local_date(r["received_at"]), "SPONSORSHIP_RECEIPT",
            [("1000", cents(r["amount_cents"]), 0), ("4200", 0, cents(r["amount_cents"]))], [r["id"], r["bank_entry_id"]])
    dups = set(pay_info["duplicate_invoice_ids"])
    inv_by_id = {i["id"]: i for i in fx.supplier_invoices}
    for inv in sorted(fx.supplier_invoices, key=lambda i: (i["invoice_date"], i["invoice_number"], i["id"])):
        if inv["id"] in dups:
            continue
        g = cents(inv["gross_cents"])
        if inv["kind"] == "INVOICE":
            add(f"SUPPLIER_INVOICE:{inv['invoice_number']}", inv["invoice_date"], "SUPPLIER_INVOICE", [("5300", g, 0), ("2300", 0, g)], [inv["id"]])
        else:
            orig = inv_by_id[inv["applies_to_invoice_id"]]
            add(f"SUPPLIER_CREDIT_NOTE:{inv['invoice_number']}", inv["invoice_date"], "SUPPLIER_CREDIT_NOTE", [("2300", g, 0), ("5300", 0, g)],
                [inv["id"]], correction_of=f"SUPPLIER_INVOICE:{orig['invoice_number']}")
    for sp in fx.supplier_payments:
        add(f"SUPPLIER_PAYMENT:{sp['payment_reference']}", fx.local_date(sp["paid_at"]), "SUPPLIER_PAYMENT",
            [("2300", cents(sp["amount_cents"]), 0), ("1000", 0, cents(sp["amount_cents"]))], [sp["id"], sp["bank_entry_id"]])
    for e in fx.expenses:
        add(f"EXPENSE:{e['expense_reference']}", fx.local_date(e["paid_at"]), "OPERATING_EXPENSE",
            [("5400", cents(e["amount_cents"]), 0), ("1000", 0, cents(e["amount_cents"]))], [e["id"], e["bank_entry_id"]])
    for r in pay_info["unmatched_reviews"]:
        be = bank_by_id[r["subject_id"]]
        amt = cents(be["amount_cents"])
        sgn = amt if be["direction"] == "credit" else -amt
        acct = "5400" if (r["scripted_human_decision"] and r["scripted_human_decision"]["decision"] == "BOOK_AS_BANK_FEE") else "1999"
        add(f"BANK_UNMATCHED:{be['provider_reference']}", r["business_date"], "UNMATCHED_BANK_ENTRY", [("1000", sgn, 0), (acct, -sgn, 0)],
            [be["id"]], note="Booked per human decision." if acct == "5400" else "Held in suspense pending review; not written off.")
    for e in entries:
        dr = sum(int(l["debit_cents"]) for l in e["lines"])
        cr = sum(int(l["credit_cents"]) for l in e["lines"])
        if dr != cr:
            raise FixtureError(f"unbalanced entry {e['entry_key']}: {dr} vs {cr}")
    return entries


def balances(entries: list[dict]) -> dict:
    bal = defaultdict(int)
    for e in entries:
        for l in e["lines"]:
            bal[l["account"]] += int(l["debit_cents"]) - int(l["credit_cents"])
    return {a: str(bal[a]) for a in sorted(ACCOUNTS)}


def economics_checks(fx: Fixture, res: dict, pays: dict, bal: dict, pay_info: dict) -> dict:
    bank_closing = sum(cents(o["amount_cents"]) for o in fx.opening_balances) + sum(
        cents(b["amount_cents"]) * (1 if b["direction"] == "credit" else -1) for b in fx.bank_entries)
    confirmed = sum(cents(p["transfer_cents"]) for p in pays["payouts"] if p["final_state"] == "CONFIRMED")
    payable = 0
    for s in res["settlements"]:
        li = {x["code"]: int(x["amount_cents"]) for x in s["line_items"]}
        payable += li["ELIGIBLE_SALES_CENTRAL"]
        if s["_j"]:
            payable -= s["_j"]["deducted"] + s["_j"]["collected_from_payable"]
    payable -= sum(cents(p["amount_cents"]) for p in pays["payouts"] if p["final_state"] == "CONFIRMED")
    checks = {
        "bank_book_equals_statement_minus_simulated_payouts": int(bal["1000"]) == bank_closing - confirmed,
        "processor_clearing_zero_after_all_deposits": bal["1100"] == "0",
        "customer_refunds_payable_zero": bal["2500"] == "0",
        "vendor_payable_matches_settlements": -int(bal["2000"]) == payable,
        "supplier_ap_matches_outstanding": -int(bal["2300"]) == int(pay_info["total_outstanding_cents"]),
        "trial_balance_zero": sum(int(v) for v in bal.values()) == 0,
    }
    return {"checks": checks, "all_pass": all(checks.values()), "bank_statement_closing_cents": str(bank_closing),
            "confirmed_simulated_payouts_cents": str(confirmed), "cash_office_residual_cents": bal["1050"]}


# ---------------------------------------------------------------- driver
def run(fixture: Path = DEFAULT_FIXTURE, decisions_path: Path = DEFAULT_DECISIONS) -> dict:
    t0 = time.perf_counter()
    fx = Fixture(fixture)
    terms = derive_terms(fx)
    pc = classify_payments(fx)
    checks, tax_lines = verify_inputs(fx, pc)
    ag = aggregates(fx, pc)
    recon = reconcile(fx, pc, ag)
    dec_doc = json.loads(decisions_path.read_text())

    def public_terms(t):
        return {k: v for k, v in t.items()}
    common = {"oracle_version": ORACLE_VERSION, "evaluator_only": True,
              "derived_from_manifest_sha256": hashlib.sha256(fx.manifest_bytes).hexdigest(),
              "fixture_id": fx.manifest["fixture_id"], "split": fx.manifest.get("split"), "seed": fx.manifest["seed"],
              "derived_terms": {vid: public_terms(t) for vid, t in terms.items()},
              "input_checks": checks, "reconciliation": recon, "tax_summary_draft": tax_summary(fx, ag, tax_lines),
              "accounts": {k: {"role": r, "name": n} for k, (r, n) in ACCOUNTS.items()}}
    out = {"_common": common}
    for set_id, ds in dec_doc["decision_sets"].items():
        dec = Decisions(ds["rules"])
        res = settle(fx, terms, ag, recon, pc, tax_lines, dec)
        pays = run_payouts(fx, res, dec)
        pinfo = payables(fx, dec)
        unmatched = []
        for be_id in recon["unmatched_bank_entries"]:
            be = next(b for b in fx.bank_entries if b["id"] == be_id)
            d = fx.local_date(be["occurred_at"])
            unmatched.append(review("UNMATCHED_BANK_ENTRY", "BANK_ENTRY", be_id, d, False,
                                    "Bank entry with no source record: case + suspense until a human classifies it; never silently written off.",
                                    dec.find("UNMATCHED_BANK_ENTRY", d, None, "BANK_ENTRY"), be["source_refs"],
                                    {"amount_cents": be["amount_cents"], "direction": be["direction"]}))
        pinfo["unmatched_reviews"] = unmatched
        ents = journals(fx, pc, ag, res, pays, pinfo, dec)
        bal = balances(ents)
        econ = economics_checks(fx, res, pays, bal, pinfo)
        for s in res["settlements"]:
            s.pop("_j", None)
        all_reviews = res["reviews"] + pays["reviews"] + pinfo["reviews"] + unmatched
        all_reviews.sort(key=lambda r: (r["business_date"], r["reason_code"], r["subject_id"] or ""))
        pinfo.pop("unmatched_reviews")
        pinfo.pop("reviews")
        out[set_id] = {"description": ds["description"], "settlements": res["settlements"], "required_reviews": all_reviews,
                       "payouts": pays["payouts"], "cash_windows": pays["cash_windows"], "corrections": res["corrections"],
                       "policy_events": res["policy_events"], "payables": pinfo, "journal_entries": ents,
                       "ending_balances": bal, "economics": econ,
                       "decision_rules_used": dict(sorted(dec.used.items()))}
    common["oracle_seconds"] = round(time.perf_counter() - t0, 1)
    return out


def dump(obj) -> bytes:
    return (json.dumps(obj, sort_keys=True, indent=1) + "\n").encode()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    ap.add_argument("--decisions", type=Path, default=DEFAULT_DECISIONS)
    ap.add_argument("--out", type=Path, default=None, help="default tests/oracle/generated/<fixture_id>/ (gitignored)")
    a = ap.parse_args(argv)
    out = run(a.fixture, a.decisions)
    dest = a.out or HERE / "generated" / out["_common"]["fixture_id"]
    dest.mkdir(parents=True, exist_ok=True)
    secs = out["_common"].pop("oracle_seconds")
    for k, v in out.items():
        (dest / ("common.json" if k == "_common" else f"{k}.json")).write_bytes(dump(v))
    print(f"oracle {ORACLE_VERSION} on {out['_common']['fixture_id']} in {secs}s -> {dest}")
    for k, v in out.items():
        if k == "_common":
            continue
        rv = defaultdict(int)
        for r in v["required_reviews"]:
            rv[r["reason_code"]] += 1
        st = defaultdict(int)
        for p in v["payouts"]:
            st[p["final_state"]] += 1
        print(f"{k}: {len(v['settlements'])} settlements, {len(v['required_reviews'])} reviews {dict(sorted(rv.items()))}, "
              f"payouts {dict(sorted(st.items()))}, {len(v['journal_entries'])} journal entries (all balanced), "
              f"economics_all_pass={v['economics']['all_pass']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
