#!/usr/bin/env python3
"""Encore compact synthetic fixture generator (lane B, task B01).

Deterministic: the same config + seed produces byte-identical output.
Stdlib only. Emits INPUTS ONLY (raw source documents + API-shaped JSON +
manifest). It never computes settlements, journals or review expectations;
those live in the independent evaluator oracle under tests/oracle/.

Usage:
    python3 data/generator/generate.py \
        --config data/generator/config/compact_v1.json \
        --out data/generator/fixtures/compact_v1 [--seed N]

Built against CONTRACTS.md v0.1 DRAFT. Not frozen; see config draft_assumptions.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import random
import shutil
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

GENERATOR_VERSION = "0.1.0"
SCHEMA_NOTE = "CONTRACTS.md v0.1 DRAFT C01/C03 field minimums (not frozen)"


# ---------------------------------------------------------------- helpers
def canonical_json(obj) -> bytes:
    return (json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def money(cents: int) -> str:
    """Canonical wire money: base-10 integer string (signed, no '-0')."""
    if not isinstance(cents, int) or isinstance(cents, bool):
        raise TypeError(f"money must be int cents, got {cents!r}")
    return str(cents)


def dollars(cents: int) -> str:
    """Human/export rendering used only inside raw CSV source files."""
    sign = "-" if cents < 0 else ""
    c = abs(cents)
    return f"{sign}{c // 100}.{c % 100:02d}"


def round_half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def utc_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Ids:
    """Stable opaque IDs derived from seed + namespace + natural key."""

    def __init__(self, seed: int):
        self.seed = seed

    def __call__(self, prefix: str, *key: str) -> str:
        raw = f"{self.seed}|{prefix}|{'|'.join(key)}".encode()
        return f"{prefix}_{sha256_hex(raw)[:16]}"


def write_csv(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for r in rows:
        w.writerow(r)
    return buf.getvalue().encode("utf-8")


# ---------------------------------------------------------------- generator
class Generator:
    def __init__(self, config: dict, config_bytes: bytes, seed: int):
        self.cfg = config
        self.config_hash = sha256_hex(config_bytes)
        self.seed = seed
        self.ids = Ids(seed)
        self.rng = random.Random(seed)
        self.tz = ZoneInfo(config["festival"]["timezone"])
        self.tax_rate = Decimal(config["tax"]["rate"])
        self.files: dict[str, bytes] = {}
        self.sources: dict[str, dict] = {}  # path -> source doc record

    # -- time -----------------------------------------------------------
    def local_dt(self, business_date: str, hhmm: str | None) -> datetime:
        d = date.fromisoformat(business_date)
        if hhmm is None:
            minute_of_day = self.rng.randrange(11 * 60, 22 * 60 + 59)
            hh, mm = divmod(minute_of_day, 60)
        else:
            hh, mm = (int(x) for x in hhmm.split(":"))
        return datetime(d.year, d.month, d.day, hh, mm, 0, tzinfo=self.tz)

    def local_iso(self, s: str) -> datetime:
        return datetime.fromisoformat(s).replace(tzinfo=self.tz)

    # -- money ----------------------------------------------------------
    def line_tax(self, eligible_cents: int) -> int:
        return round_half_up(Decimal(eligible_cents) * self.tax_rate)

    def processor_fee(self, gross_cents: int) -> int:
        p = self.cfg["processor"]
        if gross_cents <= 0:
            return p["refund_fee_cents"]
        return round_half_up(Decimal(gross_cents) * Decimal(p["percent"])) + p["fixed_cents"]

    # -- source registration -------------------------------------------
    def add_source(self, path: str, data: bytes, kind: str, title: str, media_type: str,
                   vendor_key: str | None = None):
        self.files[path] = data
        sid = self.ids("src", path)
        self.sources[path] = {
            "id": sid,
            "version": 1,
            "source_id": sid,
            "source_version": 1,
            "kind": kind,
            "title": title,
            "media_type": media_type,
            "path": path,
            "content_hash": "sha256:" + sha256_hex(data),
            "byte_length": len(data),
            "vendor_key": vendor_key,
        }

    def ref(self, path: str, locator_kind: str, locator: str, excerpt: str | None = None) -> dict:
        s = self.sources[path]
        r = {
            "source_id": s["source_id"],
            "source_version": s["source_version"],
            "content_hash": s["content_hash"],
            "locator_kind": locator_kind,
            "locator": locator,
        }
        if excerpt is not None:
            r["quoted_excerpt"] = excerpt
        return r

    # -- documents -------------------------------------------------------
    def contract_text(self, v: dict) -> tuple[str, dict[str, tuple[int, int, str]]]:
        c = v["contract"]
        f = self.cfg["festival"]
        if c["processor_fees"] == "vendor":
            proc = ("3.3 Processor fees: Vendor bears card processing fees, which are deducted "
                    "from the vendor settlement.")
        else:
            proc = ("3.3 Processor fees: Organizer bears card processing fees; they are not "
                    "deducted from the vendor settlement.")
        commission = (f"3.1 Commission: {c['commission_percent']}% of eligible festival sales. "
                      "Eligible sales exclude sales tax and tips and are reduced by refunds.")
        minimum = (f"3.2 Festival minimum: ${c['minimum']} for the entire festival, not per night. "
                   "If the percentage commission for the whole festival is lower, the final "
                   "settlement adds the difference. Sales tax and tips are never used to pay it.")
        lines = [
            f"# Vendor Participation Agreement: {v['name']}",
            "",
            "FICTIONAL DOCUMENT - synthetic test data for the Encore demo. Not a real agreement.",
            "",
            f"- Agreement version: 1",
            f"- Festival: {f['name']} (fictional), {f['start_date']} to {f['end_date']}, {f['timezone']}",
            f"- Organizer: {f['organizer_name']}",
            f"- Vendor: {v['name']} (fictional), booth {v['booth']}",
            "",
            "## 1. Scope",
            "",
            "Vendor sells food and non-alcoholic drinks only. No alcohol.",
            "",
            "## 2. Payment collection",
            "",
            "2.1 Card payments are collected centrally through the organizer's processor.",
            f"2.2 Cash: {c['cash_custody']}",
            "",
            "## 3. Organizer fee",
            "",
            commission,
            minimum,
            proc,
            "",
            "## 4. Settlement",
            "",
            "4.1 The organizer issues a statement for each festival business day by 9:00 AM Pacific "
            "the following morning and releases any payout (simulated in this demo) from 10:00 AM, "
            "subject to controls.",
            "4.2 Earlier nights assess the cumulative percentage less commission already assessed.",
            "4.3 Any amount the vendor still owes after eligible proceeds is recorded as an unpaid "
            "balance for collection or waiver. No negative payouts.",
            "",
        ]
        text = "\n".join(lines)
        spans = {}
        for key, sentence in (("commission", commission), ("minimum", minimum), ("processor_fees", proc)):
            start = text.index(sentence)
            spans[key] = (start, start + len(sentence), sentence)
        return text, spans

    def email_text(self, e: dict) -> str:
        parts = []
        for i, m in enumerate(e["messages"]):
            parts.append(
                f"From: {m['from']}\nTo: {m['to']}\nDate: {m['date']}\n"
                f"Subject: {e['subject'] if i else e['subject'][4:]}\n"
                f"X-Fictional-Notice: synthetic test data\n\n{m['body']}\n"
            )
        return ("\n" + "-" * 8 + " message boundary " + "-" * 8 + "\n\n").join(parts)

    # -- main build ------------------------------------------------------
    def build(self) -> dict[str, bytes]:
        cfg = self.cfg
        fest = cfg["festival"]
        fid = self.ids("fest", fest["key"])
        bank_id = self.ids("bacct", fest["bank_account"]["key"])
        cfg_id = self.ids("cfg", cfg["fixture_id"], cfg["config_version"])

        vendors = cfg["vendors"]
        vid = {v["key"]: self.ids("ven", v["key"]) for v in vendors}
        booth_id = {v["key"]: self.ids("booth", v["booth"]) for v in vendors}

        # 1. Documents (contracts, email) ------------------------------
        contract_spans = {}
        for v in vendors:
            path = f"sources/contracts/{v['key']}_agreement_v1.md"
            text, spans = self.contract_text(v)
            self.add_source(path, text.encode("utf-8"), "CONTRACT",
                            f"Vendor Participation Agreement: {v['name']} v1", "text/markdown", v["key"])
            contract_spans[v["key"]] = (path, spans)
        email_paths = []
        for e in cfg["emails"]:
            path = f"sources/email/{e['filename']}"
            self.add_source(path, self.email_text(e).encode("utf-8"), "EMAIL_THREAD",
                            e["subject"], "message/rfc822")
            email_paths.append(path)

        # 2. Orders/lines + payments (in memory, then CSV, then JSON) ---
        orders = []      # dicts with internal fields
        pos_rows = [["pos_order_id", "pos_line_no", "booth", "vendor_name", "ordered_at_utc",
                     "business_date", "item_sku", "description", "quantity", "unit_price",
                     "eligible_net", "tax", "tip", "gross", "tender", "cash_drawer",
                     "refund_of_pos_order", "refund_of_line_no"]]
        pos_seq = 0
        by_key = {}
        for v in vendors:
            for o in v["orders"]:
                pos_seq += 1
                native = f"POS-{v['booth']}-{pos_seq:04d}"
                dt = self.local_dt(o["business_date"], o.get("local_time"))
                lines = []
                for n, (sku, desc, qty, unit) in enumerate(o["lines"], start=1):
                    elig = qty * unit
                    tax = self.line_tax(elig)
                    lines.append({"no": n, "sku": sku, "desc": desc, "qty": qty, "unit": unit,
                                  "elig": elig, "tax": tax, "tip": 0})
                if o["tip_cents"]:
                    lines.append({"no": len(lines) + 1, "sku": "TIP", "desc": "Tip", "qty": 1,
                                  "unit": 0, "elig": 0, "tax": 0, "tip": o["tip_cents"]})
                rec = {"vkey": v["key"], "key": o["key"], "native": native, "kind": "SALE",
                       "bdate": o["business_date"], "dt": dt, "lines": lines,
                       "tender": o["tender"], "drawer": o.get("cash_drawer"),
                       "refund_of": None}
                orders.append(rec)
                by_key[o["key"]] = rec
            for r in v.get("refunds", []):
                pos_seq += 1
                native = f"POS-{v['booth']}-{pos_seq:04d}"
                orig = by_key[r["of_order"]]
                dt = self.local_dt(r["business_date"], r.get("local_time"))
                lines = []
                for n, (orig_no, qty) in enumerate(r["lines"], start=1):
                    ol = orig["lines"][orig_no - 1]
                    elig = -qty * ol["unit"]
                    tax = -round_half_up(Decimal(ol["tax"]) * qty / ol["qty"])
                    lines.append({"no": n, "sku": ol["sku"], "desc": ol["desc"], "qty": -qty,
                                  "unit": ol["unit"], "elig": elig, "tax": tax, "tip": 0,
                                  "refund_of_line": orig_no})
                rec = {"vkey": v["key"], "key": r["key"], "native": native, "kind": "REFUND",
                       "bdate": r["business_date"], "dt": dt, "lines": lines,
                       "tender": orig["tender"], "drawer": orig["drawer"], "refund_of": orig,
                       "reason": r["reason"]}
                orders.append(rec)
                by_key[r["key"]] = rec

        vname = {v["key"]: v for v in vendors}
        for o in orders:
            for ln in o["lines"]:
                ln["gross"] = ln["elig"] + ln["tax"] + ln["tip"]
                ln["csv_row"] = len(pos_rows)  # 1-based data row index (header = row 0)
                pos_rows.append([
                    o["native"], str(ln["no"]), vname[o["vkey"]]["booth"], vname[o["vkey"]]["name"],
                    utc_str(o["dt"]), o["bdate"], ln["sku"], ln["desc"], str(ln["qty"]),
                    dollars(ln["unit"]), dollars(ln["elig"]), dollars(ln["tax"]), dollars(ln["tip"]),
                    dollars(ln["gross"]), o["tender"], o["drawer"] or "",
                    o["refund_of"]["native"] if o["refund_of"] else "",
                    str(ln.get("refund_of_line", "")) if o["refund_of"] else "",
                ])
            o["gross"] = sum(ln["gross"] for ln in o["lines"])
        pos_path = "sources/pos/pos_order_lines.csv"
        self.add_source(pos_path, write_csv(pos_rows), "POS_EXPORT", "POS order-line export", "text/csv")

        # Payments: one per order (compact fixture)
        payments = []
        pay_seq = 0
        for o in orders:
            pay_seq += 1
            custody = ("CENTRALLY_COLLECTED" if o["tender"] == "card" or o["drawer"] == "ORGANIZER_CASH_OFFICE"
                       else "VENDOR_RETAINED")
            p = {"order": o, "seq": pay_seq, "method": o["tender"].upper(), "custody": custody,
                 "amount": o["gross"], "at": o["dt"] + timedelta(seconds=30)}
            if o["tender"] == "card":
                p["provider_ref"] = f"CARDCO-PAY-{pay_seq:06d}"
                p["fee"] = self.processor_fee(o["gross"])
            else:
                p["provider_ref"] = f"CASH-{o['native']}"
                p["fee"] = 0
            payments.append(p)
            o["payment"] = p

        # 3. Processor batches + payment report -------------------------
        card = [p for p in payments if p["method"] == "CARD"]
        batch_dates = sorted({p["order"]["bdate"] for p in card})
        deposit_for = {}
        for d in cfg["processor"]["deposits"]:
            for b in d["batches"]:
                deposit_for[b] = d
        batches = []
        proc_pay_rows = [["provider_reference", "pos_order_id", "captured_at_utc", "type", "amount",
                          "fee", "net", "batch_reference"]]
        for bd in batch_dates:
            members = [p for p in card if p["order"]["bdate"] == bd]
            bref = f"CARDCO-BATCH-{bd.replace('-', '')}"
            for p in members:
                p["batch_ref"] = bref
                p["proc_row"] = len(proc_pay_rows)
                proc_pay_rows.append([p["provider_ref"], p["order"]["native"], utc_str(p["at"]),
                                      "refund" if p["amount"] < 0 else "capture", dollars(p["amount"]),
                                      dollars(p["fee"]), dollars(p["amount"] - p["fee"]), bref])
            gross = sum(p["amount"] for p in members)
            fees = sum(p["fee"] for p in members)
            dep = deposit_for[bd]
            batches.append({"bdate": bd, "ref": bref, "members": members, "gross": gross, "fees": fees,
                            "net": gross - fees, "payout_ref": f"CARDCO-{dep['key'].upper()}"})
        proc_pay_path = "sources/processor/cardco_payments.csv"
        self.add_source(proc_pay_path, write_csv(proc_pay_rows), "PROCESSOR_REPORT",
                        "CardCo payment-level report", "text/csv")
        batch_rows = [["batch_reference", "business_date", "payment_count", "gross", "fees", "net",
                       "payout_reference"]]
        for b in batches:
            b["row"] = len(batch_rows)
            batch_rows.append([b["ref"], b["bdate"], str(len(b["members"])), dollars(b["gross"]),
                               dollars(b["fees"]), dollars(b["net"]), b["payout_ref"]])
        batch_path = "sources/processor/cardco_batches.csv"
        self.add_source(batch_path, write_csv(batch_rows), "PROCESSOR_REPORT",
                        "CardCo batch settlement report", "text/csv")

        # 4. Cash counts ----------------------------------------------------
        cash_counts = []
        for v in vendors:
            for cc in v.get("cash_counts", []):
                drawer = "ORGANIZER_CASH_OFFICE" if cc["location"] == "ORGANIZER_CASH_OFFICE" else "VENDOR"
                amt = sum(p["amount"] for p in payments
                          if p["method"] == "CASH" and p["order"]["vkey"] == v["key"]
                          and p["order"]["bdate"] == cc["business_date"] and p["order"]["drawer"] == drawer)
                cash_counts.append({"v": v, "cc": cc, "amount": amt,
                                    "at": self.local_dt(cc["business_date"], cc["local_time"])})
        cash_rows = [["count_reference", "business_date", "counted_at_utc", "booth", "location",
                      "custody", "counted_by", "amount"]]
        for c in cash_counts:
            c["ref"] = f"CASHCOUNT-{c['v']['booth']}-{c['cc']['business_date'].replace('-', '')}"
            c["row"] = len(cash_rows)
            cash_rows.append([c["ref"], c["cc"]["business_date"], utc_str(c["at"]), c["v"]["booth"],
                              c["cc"]["location"], c["cc"]["custody"], c["cc"]["counted_by_role"],
                              dollars(c["amount"])])
        cash_path = "sources/cash/cash_counts.csv"
        self.add_source(cash_path, write_csv(cash_rows), "CASH_COUNT", "Cash count sheet", "text/csv")

        # 5. Bank statement ------------------------------------------------
        bank = []
        for d in cfg["processor"]["deposits"]:
            members = [b for b in batches if b["bdate"] in d["batches"]]
            net = sum(b["net"] for b in members)
            bank.append({"at": self.local_iso(d["local_datetime"]), "ref": f"CARDCO-{d['key'].upper()}",
                         "desc": f"CARDCO PAYOUT {d['key'].upper()}", "amount": abs(net),
                         "direction": "credit" if net >= 0 else "debit"})
        cc_by_key = {c["cc"]["key"]: c for c in cash_counts}
        for d in cfg["central_cash_deposits"]:
            amt = sum(cc_by_key[k]["amount"] for k in d["count_keys"])
            bank.append({"at": self.local_iso(d["local_datetime"]), "ref": f"CASHDEP-{d['key'].upper()}",
                         "desc": "BRANCH CASH DEPOSIT - ORGANIZER CASH OFFICE", "amount": amt,
                         "direction": "credit"})
        bank.sort(key=lambda b: (b["at"], b["ref"]))
        ob = fest["bank_account"]
        bank_rows = [["posted_at_utc", "description", "reference", "debit", "credit", "running_balance"]]
        bank_rows.append([f"{ob['opening_as_of']}T23:59:59Z", "OPENING BALANCE", "OPENING", "", "",
                          dollars(ob["opening_balance_cents"])])
        running = ob["opening_balance_cents"]
        for b in bank:
            running += b["amount"] if b["direction"] == "credit" else -b["amount"]
            b["row"] = len(bank_rows)
            bank_rows.append([utc_str(b["at"]), b["desc"], b["ref"],
                              dollars(b["amount"]) if b["direction"] == "debit" else "",
                              dollars(b["amount"]) if b["direction"] == "credit" else "",
                              dollars(running)])
        bank_path = "sources/bank/operating_account_statement.csv"
        self.add_source(bank_path, write_csv(bank_rows), "BANK_STATEMENT",
                        "Operating account bank statement", "text/csv")

        # 6. API-shaped JSON (inputs only) ----------------------------------
        api = {}
        api["festival.json"] = {
            "id": fid, "version": 1, "status": "ACTIVE", "name": fest["name"],
            "fictional_notice": fest["fictional_notice"], "organizer_name": fest["organizer_name"],
            "start_date": fest["start_date"], "end_date": fest["end_date"],
            "timezone": fest["timezone"], "currency": fest["currency"],
            "business_dates": [str(date.fromisoformat(fest["start_date"]) + timedelta(days=i))
                               for i in range((date.fromisoformat(fest["end_date"]) - date.fromisoformat(fest["start_date"])).days + 1)],
            "bank_accounts": [{"id": bank_id, "name": ob["name"],
                               "opening_balance_cents": money(ob["opening_balance_cents"]),
                               "opening_as_of": ob["opening_as_of"],
                               "source_refs": [self.ref(bank_path, "row", "row=1")]}],
            "business_config_id": cfg_id,
        }
        bc = cfg["business_config"]
        api["business_config.json"] = {
            "id": cfg_id, "version": 1, "status": "DRAFT", "festival_id": fid,
            "draft_notice": "DRAFT configuration (C05 not frozen). Assumptions listed in draft_assumptions.",
            "timezone": fest["timezone"], "currency": fest["currency"],
            "default_terms": {"commission_bps": bc["default_commission_bps"],
                              "festival_minimum_cents": money(bc["festival_minimum_cents"])},
            "payout_controls": {"auto_limit_cents_per_vendor_day": money(bc["auto_payout_limit_cents_per_vendor_day"]),
                                "statement_local_time": bc["statement_local_time"],
                                "payout_local_time": bc["payout_local_time"], "payment_mode": "SIMULATED"},
            "tax": {"label": cfg["tax"]["label"], "rate": cfg["tax"]["rate"],
                    "price_mode": cfg["tax"]["price_mode"], "rounding": cfg["tax"]["rounding"],
                    "boundary": cfg["tax"]["boundary"], "report_status": "DRAFT, NOT FILED"},
            "custody_rules": {"CARD": "CENTRALLY_COLLECTED",
                              "CASH": "BY_CASH_DRAWER: VENDOR -> VENDOR_RETAINED, ORGANIZER_CASH_OFFICE -> CENTRALLY_COLLECTED"},
            "matching": {"deposit_window_days": bc["deposit_matching_window_days"]},
            "processor_fee_model": {"processor": cfg["processor"]["name"], "percent": cfg["processor"]["percent"],
                                    "fixed_cents": money(cfg["processor"]["fixed_cents"]),
                                    "rounding": cfg["processor"]["rounding"],
                                    "refund_fee_cents": money(cfg["processor"]["refund_fee_cents"])},
            "draft_assumptions": cfg["draft_assumptions"],
        }
        api["vendors.json"] = {"items": [{
            "id": vid[v["key"]], "version": 1, "status": "ACTIVE", "festival_id": fid,
            "name": v["name"], "booth_ids": [booth_id[v["key"]]],
            "booths": [{"id": booth_id[v["key"]], "label": v["booth"]}],
            "beneficiary_id": self.ids("ben", v["key"]), "beneficiary_version": 1,
            "active_contract_version": {"source_id": self.sources[contract_spans[v["key"]][0]]["source_id"],
                                        "source_version": 1},
            "fictional_notice": "Fictional vendor",
        } for v in vendors], "next_cursor": None}

        src_items = []
        for path, s in sorted(self.sources.items()):
            item = {k: s[k] for k in ("id", "version", "kind", "title", "media_type", "path",
                                      "content_hash", "byte_length")}
            item["status"] = "ACTIVE"
            item["vendor_id"] = vid[s["vendor_key"]] if s["vendor_key"] else None
            item["parser_version"] = None
            src_items.append(item)
        api["source_documents.json"] = {"items": src_items, "next_cursor": None}

        order_items = []
        for o in orders:
            oid = self.ids("ord", o["native"])
            o["id"] = oid
            line_items = []
            for ln in o["lines"]:
                ln["id"] = self.ids("oln", o["native"], str(ln["no"]))
                li = {"line_id": ln["id"], "line_no": ln["no"], "item_sku": ln["sku"],
                      "description": ln["desc"], "quantity": ln["qty"],
                      "unit_price_cents": money(ln["unit"]), "eligible_net_cents": money(ln["elig"]),
                      "tax_cents": money(ln["tax"]), "tips_cents": money(ln["tip"]),
                      "gross_cents": money(ln["gross"]),
                      "source_refs": [self.ref(pos_path, "row", f"row={ln['csv_row']}")]}
                if o["refund_of"]:
                    li["refund_of_line_id"] = o["refund_of"]["lines"][ln["refund_of_line"] - 1]["id"]
                line_items.append(li)
            item = {
                "id": oid, "version": 1, "status": "COMPLETED", "kind": o["kind"],
                "source_native_id": o["native"], "source_system": "POS",
                "festival_id": fid, "vendor_id": vid[o["vkey"]], "booth_id": booth_id[o["vkey"]],
                "business_date": o["bdate"], "ordered_at": utc_str(o["dt"]),
                "recorded_at": utc_str(o["dt"] + timedelta(minutes=2)),
                "lines": line_items,
                "eligible_net_cents": money(sum(l["elig"] for l in o["lines"])),
                "tax_cents": money(sum(l["tax"] for l in o["lines"])),
                "tips_cents": money(sum(l["tip"] for l in o["lines"])),
                "gross_cents": money(o["gross"]),
                "source_refs": [self.ref(pos_path, "row", f"row={o['lines'][0]['csv_row']}-{o['lines'][-1]['csv_row']}")],
            }
            if o["refund_of"]:
                item["refund_of_order_id"] = o["refund_of"]["id"]
                item["refund_reason"] = o["reason"]
            order_items.append(item)
        api["orders.json"] = {"items": order_items, "next_cursor": None}

        batch_id_by_ref = {b["ref"]: self.ids("pbatch", b["ref"]) for b in batches}
        pay_items = []
        for p in payments:
            p["id"] = self.ids("pay", p["provider_ref"])
            o = p["order"]
            refs = [self.ref(pos_path, "row", f"row={o['lines'][0]['csv_row']}-{o['lines'][-1]['csv_row']}")]
            if p["method"] == "CARD":
                refs.append(self.ref(proc_pay_path, "row", f"row={p['proc_row']}"))
            item = {
                "id": p["id"], "version": 1, "status": "SUCCEEDED",
                "kind": "REFUND" if p["amount"] < 0 else "CAPTURE",
                "vendor_id": vid[o["vkey"]], "festival_id": fid, "business_date": o["bdate"],
                "order_refs": [{"order_id": o["id"], "amount_cents": money(p["amount"])}],
                "method": p["method"], "custody": p["custody"],
                "cash_drawer": o["drawer"] if p["method"] == "CASH" else None,
                "amount_cents": money(p["amount"]), "currency": fest["currency"],
                "provider": "CARDCO" if p["method"] == "CARD" else None,
                "provider_reference": p["provider_ref"],
                "processor_fee_cents": money(p["fee"]) if p["method"] == "CARD" else None,
                "processor_batch_id": batch_id_by_ref[p["batch_ref"]] if p["method"] == "CARD" else None,
                "occurred_at": utc_str(p["at"]),
                "source_refs": refs,
            }
            if o["refund_of"]:
                item["refund_of_payment_id"] = o["refund_of"]["payment"]["id"]
            pay_items.append(item)
        api["payments.json"] = {"items": pay_items, "next_cursor": None}

        api["processor_batches.json"] = {"items": [{
            "id": batch_id_by_ref[b["ref"]], "version": 1, "status": "SETTLED", "provider": "CARDCO",
            "provider_reference": b["ref"], "business_date": b["bdate"],
            "payment_ids": [p["id"] for p in b["members"]], "payment_count": len(b["members"]),
            "gross_cents": money(b["gross"]), "fees_cents": money(b["fees"]), "net_cents": money(b["net"]),
            "currency": fest["currency"], "payout_reference": b["payout_ref"],
            "source_refs": [self.ref(batch_path, "row", f"row={b['row']}")],
        } for b in batches], "next_cursor": None}

        api["bank_entries.json"] = {"items": [{
            "id": self.ids("bank", b["ref"]), "version": 1, "status": "POSTED", "account_id": bank_id,
            "provider_reference": b["ref"], "description": b["desc"],
            "amount_cents": money(b["amount"]), "direction": b["direction"], "currency": fest["currency"],
            "occurred_at": utc_str(b["at"]),
            "source_refs": [self.ref(bank_path, "row", f"row={b['row']}")],
        } for b in bank], "next_cursor": None}

        api["cash_counts.json"] = {"items": [{
            "id": self.ids("cashct", c["ref"]), "version": 1, "status": "RECORDED",
            "source_native_id": c["ref"], "festival_id": fid, "vendor_id": vid[c["v"]["key"]],
            "booth_id": booth_id[c["v"]["key"]], "business_date": c["cc"]["business_date"],
            "counted_at": utc_str(c["at"]), "location": c["cc"]["location"], "custody": c["cc"]["custody"],
            "counted_by_role": c["cc"]["counted_by_role"], "amount_cents": money(c["amount"]),
            "currency": fest["currency"],
            "source_refs": [self.ref(cash_path, "row", f"row={c['row']}")],
        } for c in cash_counts], "next_cursor": None}

        # Document evidence pointers (spans) for frontend evidence drawers.
        evid = []
        for v in vendors:
            path, spans = contract_spans[v["key"]]
            for k, (a, b, sentence) in spans.items():
                evid.append({"id": self.ids("evid", path, k), "version": 1, "vendor_id": vid[v["key"]],
                             "topic": k, "source_ref": self.ref(path, "span", f"chars={a}-{b}", sentence)})
        for path in email_paths:
            text = self.files[path].decode("utf-8")
            needle = "we agreed 10% commission for Taco Libre"
            a = text.index(needle)
            evid.append({"id": self.ids("evid", path, "commission"), "version": 1, "vendor_id": None,
                         "topic": "commission",
                         "source_ref": self.ref(path, "span", f"chars={a}-{a + len(needle)}", needle)})
        # Kept OUT of api/: pre-located spans would hint the runtime agent at the
        # conflict. Frontend-dev only (evidence drawer mock data); never agent input.
        dev_only = {"evidence_locators.json": {
            "items": evid, "next_cursor": None,
            "note": ("FRONTEND DEV ONLY. Span locators into source documents for evidence-drawer "
                     "mock data. Must not be given to agents, retrieval or prompts: it would "
                     "pre-locate the terms an agent is meant to find itself.")}}

        for name, obj in api.items():
            self.files[f"api/{name}"] = canonical_json(obj)
        for name, obj in dev_only.items():
            self.files[f"dev_only/{name}"] = canonical_json(obj)

        # 7. Manifest --------------------------------------------------------
        counts = {name[:-5]: len(obj["items"]) for name, obj in api.items() if "items" in obj}
        counts["order_lines"] = sum(len(o["lines"]) for o in orders)
        counts["dev_only_evidence_locators"] = len(evid)
        manifest = {
            "fixture_id": cfg["fixture_id"],
            "generator": "data/generator/generate.py",
            "generator_version": GENERATOR_VERSION,
            "python_requirement": "CPython 3.13 (stdlib random.Random(seed) stream)",
            "schema_note": SCHEMA_NOTE,
            "seed": self.seed,
            "config_path": "data/generator/config/compact_v1.json",
            "config_version": cfg["config_version"],
            "config_sha256": self.config_hash,
            "status": "DRAFT - inputs only; evaluator expectations live in tests/oracle (never shipped to runtime)",
            "festival_id": fid,
            "scenario_labels": {vid[v["key"]]: {"vendor_name": v["name"], "labels": v["scenario_labels"]}
                                for v in vendors},
            "fixture_wide_scenarios": ["DELAYED_GROUPED_PROCESSOR_DEPOSIT", "PROCESSOR_NET_OF_FEES",
                                       "POST_FESTIVAL_REFUND_BANK_DEBIT", "C02_FIXTURE_6"],
            "source_versions": [{"source_id": s["source_id"], "source_version": s["source_version"],
                                 "path": p, "content_hash": s["content_hash"], "kind": s["kind"]}
                                for p, s in sorted(self.sources.items())],
            "expected_record_counts": dict(sorted(counts.items())),
            "files": [{"path": p, "sha256": sha256_hex(b), "bytes": len(b)} for p, b in sorted(self.files.items())],
        }
        self.files["manifest.json"] = canonical_json(manifest)
        return self.files


def generate(config_path: Path, out_dir: Path, seed: int | None = None) -> dict[str, bytes]:
    config_bytes = config_path.read_bytes()
    cfg = json.loads(config_bytes)
    files = Generator(cfg, config_bytes, cfg["seed"] if seed is None else seed).build()
    if out_dir.exists():
        shutil.rmtree(out_dir)
    for rel, data in files.items():
        p = out_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return files


def main(argv=None) -> int:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", type=Path, default=here / "config" / "compact_v1.json")
    ap.add_argument("--out", type=Path, default=here / "fixtures" / "compact_v1")
    ap.add_argument("--seed", type=int, default=None, help="override config seed")
    a = ap.parse_args(argv)
    files = generate(a.config, a.out, a.seed)
    m = json.loads(files["manifest.json"])
    print(f"wrote {len(files)} files to {a.out}")
    print(f"seed={m['seed']} config_sha256={m['config_sha256']}")
    print(f"manifest_sha256={sha256_hex(files['manifest.json'])}")
    print("counts:", json.dumps(m["expected_record_counts"], sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
