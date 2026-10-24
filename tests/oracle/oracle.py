#!/usr/bin/env python3
"""Encore independent evaluator oracle (lane B, task B01).

EVALUATOR-ONLY. Never ship this directory into runtime containers, agent prompt
context, retrieval indexes or the document bucket.

Independence rules:
  * Imports nothing from data/generator/ or any production package.
  * Derives every expectation from the EMITTED fixture files (api/*.json and
    sources/*), plus the evaluator's scripted human decisions. It re-parses
    contract/email text itself and re-verifies the generator's arithmetic
    (line tax, business dates, batch sums) instead of trusting it.

Rules implemented (CONTRACTS.md v0.1 DRAFT C01/C02/C05 + AGENT_HANDOFF approved rules):
  * eligible sales exclude tax and tips; refunds reduce eligible sales on the refund's
    business_date;
  * before the final festival day: assessed = round_half_up(rate * cumulative eligible)
    - prior assessed;
  * on/after the final day: target = max(round_half_up(rate * cumulative), minimum);
    assessed = target - prior assessed (minimum applied once per festival);
  * commission is assessed on ALL custody (incl. vendor-retained cash) but can only be
    collected from centrally held eligible proceeds; shortfall -> unpaid balance
    (receivable) for human collection/waiver; never negative payout; never from tax/tips;
  * processor fees deducted only when the contract text says the vendor bears them;
  * a contract-vs-email rate conflict is a HUMAN REVIEW. Expected numbers exist only
    conditionally on a scripted evaluator decision.

Usage:
    python3 tests/oracle/oracle.py [--fixture DIR] [--decisions FILE] [--out DIR]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ORACLE_VERSION = "oracle-0.1.0"
REPO = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE = REPO / "data" / "generator" / "fixtures" / "compact_v1"
HERE = Path(__file__).resolve().parent

MONEY_RE = re.compile(r"^-?(0|[1-9][0-9]*)$")

# DRAFT posting map (C02 proposed accounts; numbers are oracle-local placeholders).
ACCOUNTS = {
    "1000": "Bank cash - operating",
    "1050": "Cash on hand - organizer cash office (DRAFT addition, not in C02 list)",
    "1100": "Processor clearing",
    "1200": "Vendor commission receivable / unpaid balance",
    "2000": "Vendor funds payable",
    "2100": "Sales tax liability/control",
    "2200": "Tips liability/control",
    "4000": "Organizer commission revenue",
    "5100": "Processor fee expense",
}


class FixtureError(Exception):
    pass


def cents(s) -> int:
    if not isinstance(s, str) or not MONEY_RE.match(s) or s == "-0":
        raise FixtureError(f"non-canonical money string: {s!r}")
    return int(s)


def rhu(x: Decimal) -> int:
    return int(x.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def pct_of(amount_cents: int, bps: int) -> int:
    return rhu(Decimal(amount_cents) * Decimal(bps) / Decimal(10000))


def parse_utc(s: str) -> datetime:
    if not s.endswith("Z"):
        raise FixtureError(f"timestamp not UTC RFC3339 'Z': {s}")
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- loading
class Fixture:
    def __init__(self, root: Path):
        self.root = root
        self.manifest_bytes = (root / "manifest.json").read_bytes()
        self.manifest = json.loads(self.manifest_bytes)
        api = root / "api"
        self.festival = json.loads((api / "festival.json").read_text())
        self.config = json.loads((api / "business_config.json").read_text())
        self.vendors = self._items("vendors")
        self.docs = self._items("source_documents")
        self.orders = self._items("orders")
        self.payments = self._items("payments")
        self.batches = self._items("processor_batches")
        self.bank = self._items("bank_entries")
        self.cash_counts = self._items("cash_counts")
        self.tz = ZoneInfo(self.festival["timezone"])
        self.vendor_by_id = {v["id"]: v for v in self.vendors}
        self.vendor_by_name = {v["name"]: v for v in self.vendors}
        self.order_by_id = {o["id"]: o for o in self.orders}
        self.payment_by_id = {p["id"]: p for p in self.payments}

    def _items(self, name):
        return json.loads((self.root / "api" / f"{name}.json").read_text())["items"]

    def doc_text(self, doc) -> str:
        data = (self.root / doc["path"]).read_bytes()
        if "sha256:" + hashlib.sha256(data).hexdigest() != doc["content_hash"]:
            raise FixtureError(f"content hash mismatch for {doc['path']}")
        return data.decode("utf-8")

    def local_date(self, ts: str) -> str:
        return parse_utc(ts).astimezone(self.tz).date().isoformat()

    def local_to_utc(self, d: str, hhmm: str) -> datetime:
        y, m, dd = (int(x) for x in d.split("-"))
        hh, mi = (int(x) for x in hhmm.split(":"))
        return datetime(y, m, dd, hh, mi, tzinfo=self.tz).astimezone(timezone.utc)


# ---------------------------------------------------------------- terms
COMMISSION_RE = re.compile(r"Commission:\s*(\d+(?:\.\d+)?)%")
MINIMUM_RE = re.compile(r"Festival minimum:\s*\$([\d,]+)\.(\d{2})")
PROC_RE = re.compile(r"Processor fees:\s*(Vendor|Organizer) bears")
EMAIL_RATE_RE = re.compile(r"(\d+(?:\.\d+)?)% commission for ([A-Z][A-Za-z ]+?)[,.]")


def pct_to_bps(p: str) -> int:
    v = Decimal(p) * 100
    if v != v.to_integral_value():
        raise FixtureError(f"rate {p}% is not whole basis points")
    return int(v)


def derive_terms(fx: Fixture) -> dict:
    terms = {}
    for d in fx.docs:
        if d["kind"] != "CONTRACT":
            continue
        t = fx.doc_text(d)
        c, m, p = COMMISSION_RE.search(t), MINIMUM_RE.search(t), PROC_RE.search(t)
        if not (c and m and p):
            raise FixtureError(f"cannot parse terms in {d['path']}")
        terms[d["vendor_id"]] = {
            "contract_bps": pct_to_bps(c.group(1)),
            "minimum_cents": int(m.group(1).replace(",", "")) * 100 + int(m.group(2)),
            "processor_fees_deductible": p.group(1) == "Vendor",
            "contract_source_id": d["id"],
            "contract_source_version": d["version"],
            "other_rate_claims": [],
        }
    for d in fx.docs:
        if d["kind"] != "EMAIL_THREAD":
            continue
        t = fx.doc_text(d)
        for mt in EMAIL_RATE_RE.finditer(t):
            v = fx.vendor_by_name.get(mt.group(2).strip())
            if v:
                terms[v["id"]]["other_rate_claims"].append(
                    {"bps": pct_to_bps(mt.group(1)), "source_id": d["id"], "source_version": d["version"],
                     "excerpt": mt.group(0)})
    for vid, t in terms.items():
        t["conflict"] = any(c["bps"] != t["contract_bps"] for c in t["other_rate_claims"])
    return terms


# ---------------------------------------------------------------- input checks
def verify_inputs(fx: Fixture) -> dict:
    """Re-verify generator arithmetic from emitted data; raise on integrity errors."""
    rate = Decimal(fx.config["tax"]["rate"])
    line_by_id = {}
    for o in fx.orders:
        for ln in o["lines"]:
            line_by_id[ln["line_id"]] = (o, ln)
    tax_mismatches = []
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
            if "refund_of_line_id" in ln:
                _, ol = line_by_id[ln["refund_of_line_id"]]
                expect = -rhu(Decimal(cents(ol["tax_cents"])) * (-ln["quantity"]) / ol["quantity"])
            else:
                expect = rhu(Decimal(e) * rate)
            if expect != t:
                tax_mismatches.append({"line_id": ln["line_id"], "supplied": t, "recalculated": expect})
    for p in fx.payments:
        o = fx.order_by_id[p["order_refs"][0]["order_id"]]
        if cents(p["amount_cents"]) != cents(o["gross_cents"]):
            raise FixtureError(f"payment {p['id']} != order gross")
        if p["custody"] not in ("CENTRALLY_COLLECTED", "VENDOR_RETAINED"):
            raise FixtureError(f"unknown custody {p['custody']}")
    for b in fx.batches:
        mem = [fx.payment_by_id[i] for i in b["payment_ids"]]
        if sum(cents(p["amount_cents"]) for p in mem) != cents(b["gross_cents"]):
            raise FixtureError(f"batch {b['provider_reference']} gross mismatch")
        if sum(cents(p["processor_fee_cents"]) for p in mem) != cents(b["fees_cents"]):
            raise FixtureError(f"batch {b['provider_reference']} fee mismatch")
        if cents(b["gross_cents"]) - cents(b["fees_cents"]) != cents(b["net_cents"]):
            raise FixtureError(f"batch {b['provider_reference']} net mismatch")
    return {"tax_mismatches": tax_mismatches, "orders_checked": len(fx.orders),
            "payments_checked": len(fx.payments), "batches_checked": len(fx.batches)}


# ---------------------------------------------------------------- reconciliation
def reconcile(fx: Fixture) -> dict:
    window = fx.config["matching"]["deposit_window_days"]
    groups, by_ref = [], defaultdict(list)
    for b in fx.batches:
        by_ref[b["payout_reference"]].append(b)
    matched_bank = set()
    for be in fx.bank:
        if be["provider_reference"] in by_ref:
            mem = by_ref[be["provider_reference"]]
            net = sum(cents(b["net_cents"]) for b in mem)
            signed = cents(be["amount_cents"]) * (1 if be["direction"] == "credit" else -1)
            bank_date = fx.local_date(be["occurred_at"])
            lags = [(date.fromisoformat(bank_date) - date.fromisoformat(b["business_date"])).days for b in mem]
            groups.append({
                "kind": "PROCESSOR_DEPOSIT", "bank_entry_id": be["id"],
                "bank_provider_reference": be["provider_reference"],
                "member_batch_ids": [b["id"] for b in mem],
                "member_batch_dates": [b["business_date"] for b in mem],
                "gross_cents": str(sum(cents(b["gross_cents"]) for b in mem)),
                "fees_cents": str(sum(cents(b["fees_cents"]) for b in mem)),
                "matched_amount_cents": str(signed), "variance_cents": str(signed - net),
                "deposit_lag_days": lags,
                "reason_code": ("GROUPED_DEPOSIT_TIMING" if len(mem) > 1 or max(lags) > 1 else "NORMAL"),
                "status": "MATCHED" if signed == net and max(lags) <= window else "CASE_REQUIRED",
            })
            matched_bank.add(be["id"])
    central_counts = [c for c in fx.cash_counts if c["custody"] == "CENTRALLY_COLLECTED"]
    for be in fx.bank:
        if be["id"] in matched_bank:
            continue
        amt = cents(be["amount_cents"])
        hits = [c for c in central_counts if cents(c["amount_cents"]) == amt]
        groups.append({"kind": "CASH_DEPOSIT", "bank_entry_id": be["id"],
                       "member_cash_count_ids": [c["id"] for c in hits][:1],
                       "matched_amount_cents": str(amt),
                       "variance_cents": "0" if hits else str(amt),
                       "reason_code": "NORMAL" if hits else "UNMATCHED_BANK_ENTRY",
                       "status": "MATCHED" if hits else "CASE_REQUIRED"})
    # cash count vs POS cash payments per vendor-day-custody
    cash_checks = []
    for c in fx.cash_counts:
        pos = sum(cents(p["amount_cents"]) for p in fx.payments
                  if p["method"] == "CASH" and p["vendor_id"] == c["vendor_id"]
                  and p["business_date"] == c["business_date"] and p["custody"] == c["custody"])
        cash_checks.append({"cash_count_id": c["id"], "vendor_name": fx.vendor_by_id[c["vendor_id"]]["name"],
                            "business_date": c["business_date"], "custody": c["custody"],
                            "counted_cents": c["amount_cents"], "pos_cash_cents": str(pos),
                            "variance_cents": str(cents(c["amount_cents"]) - pos),
                            "status": "MATCHED" if pos == cents(c["amount_cents"]) else "CASE_REQUIRED"})
    # As-of view at each statement time: which batches are deposited?
    stmt = fx.config["payout_controls"]["statement_local_time"]
    as_of = []
    for bd in fx.festival["business_dates"]:
        t = fx.local_to_utc((date.fromisoformat(bd) + timedelta(days=1)).isoformat(), stmt)
        for b in fx.batches:
            if b["business_date"] > bd:
                continue
            be = [x for x in fx.bank if x["provider_reference"] == b["payout_reference"]]
            arrived = bool(be) and parse_utc(be[0]["occurred_at"]) <= t
            as_of.append({"as_of_utc": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "statement_for": bd,
                          "batch_business_date": b["business_date"],
                          "state": "DEPOSITED" if arrived else "TIMING_PENDING_NOT_AVAILABLE_CASH"})
    return {"match_groups": groups, "cash_count_checks": cash_checks, "deposit_state_at_statement_time": as_of}


# ---------------------------------------------------------------- settlements
def settle(fx: Fixture, terms: dict, decisions: list[dict]) -> dict:
    fest_dates = fx.festival["business_dates"]
    final_day = fest_dates[-1]
    limit = cents(fx.config["payout_controls"]["auto_limit_cents_per_vendor_day"])
    payout_time = fx.config["payout_controls"]["payout_local_time"]
    manifest_hash = hashlib.sha256(fx.manifest_bytes).hexdigest()

    def find_decision(vname, reason, bdate):
        for d in decisions:
            if d["vendor_name"] == vname and d["reason_code"] == reason and d["business_date"] == bdate:
                return d
        return None

    settlements, reviews, payouts = [], [], []
    for v in fx.vendors:
        vid, t = v["id"], terms[v["id"]]
        pays = [p for p in fx.payments if p["vendor_id"] == vid]
        activity_dates = {p["business_date"] for p in pays}
        days = sorted(set(fest_dates) | activity_dates)
        cum_elig = prior_assessed = open_bal = 0
        reusable_policy = None
        held_chain = None
        for d in days:
            day_pays = [p for p in pays if p["business_date"] == d]
            lines = [(p, ln) for p in day_pays for ln in fx.order_by_id[p["order_refs"][0]["order_id"]]["lines"]]
            elig_central = sum(cents(ln["eligible_net_cents"]) for p, ln in lines if p["custody"] == "CENTRALLY_COLLECTED")
            elig_retained = sum(cents(ln["eligible_net_cents"]) for p, ln in lines if p["custody"] == "VENDOR_RETAINED")
            refunds = sum(cents(ln["eligible_net_cents"]) for p, ln in lines if p["kind"] == "REFUND")
            tax_central = sum(cents(ln["tax_cents"]) for p, ln in lines if p["custody"] == "CENTRALLY_COLLECTED")
            tax_retained = sum(cents(ln["tax_cents"]) for p, ln in lines if p["custody"] == "VENDOR_RETAINED")
            tips_central = sum(cents(ln["tips_cents"]) for p, ln in lines if p["custody"] == "CENTRALLY_COLLECTED")
            proc_fees = sum(cents(p["processor_fee_cents"]) for p in day_pays if p["method"] == "CARD")
            deducted = proc_fees if t["processor_fees_deductible"] else 0
            cum_elig += elig_central + elig_retained
            holds, policy_refs = [], [{"source_id": t["contract_source_id"], "source_version": t["contract_source_version"], "role": "CONTRACT"}]

            # --- terms resolution
            bps, terms_basis = t["contract_bps"], "CONTRACT"
            if t["conflict"]:
                for c in t["other_rate_claims"]:
                    policy_refs.append({"source_id": c["source_id"], "source_version": c["source_version"], "role": "CONFLICTING_CLAIM"})
                if reusable_policy:
                    bps, terms_basis = reusable_policy["commission_bps"], "APPROVED_REUSABLE_POLICY"
                    policy_refs.append({"policy_from_decision": reusable_policy["decision_id"], "role": "APPLIED_POLICY",
                                        "originating_case": f"CONFLICTING_TERMS/{v['name']}/{reusable_policy['business_date']}"})
                else:
                    dec = find_decision(v["name"], "CONFLICTING_TERMS", d)
                    claims = sorted({t["contract_bps"]} | {c["bps"] for c in t["other_rate_claims"]})
                    impact = {f"{b}bps": str(pct_of(cum_elig, b)) for b in claims}
                    reviews.append({"vendor_id": vid, "vendor_name": v["name"], "business_date": d,
                                    "reason_code": "CONFLICTING_TERMS", "blocking": True,
                                    "evidence_source_ids": [t["contract_source_id"]] + [c["source_id"] for c in t["other_rate_claims"]],
                                    "financial_impact": {"cumulative_eligible_cents": str(cum_elig),
                                                         "cumulative_fee_by_candidate_rate_cents": impact},
                                    "expected_system_behavior": "Hold this vendor only; show both sources; do not pick a rate autonomously.",
                                    "scripted_human_decision": dec})
                    if dec and held_chain is None:
                        bps, terms_basis = dec["commission_bps"], "HUMAN_DECISION_" + dec["scope"]
                        policy_refs.append({"human_decision": dec["decision_id"], "role": "CASE_RESOLUTION"})
                        if dec["scope"] == "REUSABLE_VENDOR_FESTIVAL":
                            reusable_policy = dec
                    else:
                        held_chain = held_chain or d
            if held_chain:
                holds.append({"reason_code": "CONFLICTING_TERMS_UNRESOLVED", "since": held_chain})
                settlements.append({
                    "vendor_id": vid, "vendor_name": v["name"], "festival_id": fx.festival["id"], "business_date": d,
                    "status": "HELD", "calculation_version": ORACLE_VERSION, "source_manifest_hash": manifest_hash,
                    "policy_refs": policy_refs, "line_items": [
                        {"code": "ELIGIBLE_SALES_CENTRAL", "amount_cents": str(elig_central)},
                        {"code": "TAX_COLLECTED_CENTRAL_PROTECTED", "amount_cents": str(tax_central)},
                        {"code": "TIPS_COLLECTED_CENTRAL_PROTECTED", "amount_cents": str(tips_central)}],
                    "assessed_fee_cents": None, "collected_fee_cents": None, "payout_cents": "0",
                    "tips_passthrough_cents": "0", "unpaid_balance_cents": None, "holds": holds})
                payouts.append({"vendor_name": v["name"], "vendor_id": vid, "business_date": d, "state": "HELD",
                                "amount_cents": "0", "tips_passthrough_cents": "0", "hold_reasons": ["CONFLICTING_TERMS_UNRESOLVED"]})
                continue

            # --- fee math
            pct_fee_cum = pct_of(cum_elig, bps)
            target = max(pct_fee_cum, t["minimum_cents"]) if d >= final_day else pct_fee_cum
            min_adj = target - pct_fee_cum
            assessed = target - prior_assessed
            due = assessed + open_bal
            avail = elig_central - deducted
            net = avail - due
            payout = max(0, net)
            new_open = max(0, -net)
            collected = due if due <= 0 else min(due, max(avail, 0))
            receivable_delta = new_open - open_bal
            prior = prior_assessed
            prior_assessed, open_bal = target, new_open

            transfer = payout + tips_central
            state = "RELEASE_ELIGIBLE"
            if transfer > limit:
                dec = find_decision(v["name"], "PAYOUT_ABOVE_AUTO_LIMIT", d)
                reviews.append({"vendor_id": vid, "vendor_name": v["name"], "business_date": d,
                                "reason_code": "PAYOUT_ABOVE_AUTO_LIMIT", "blocking": True,
                                "financial_impact": {"transfer_cents": str(transfer), "auto_limit_cents": str(limit)},
                                "required_capability": "finance_lead",
                                "expected_system_behavior": "Hold payout until finance-lead approval bound to this amount/version.",
                                "scripted_human_decision": dec})
                if dec and dec["decision"] == "APPROVE":
                    state = "RELEASE_ELIGIBLE_AFTER_APPROVAL"
                else:
                    state = "HELD"
                    holds.append({"reason_code": "PAYOUT_ABOVE_AUTO_LIMIT"})
            if d == final_day and new_open > 0:
                reviews.append({"vendor_id": vid, "vendor_name": v["name"], "business_date": d,
                                "reason_code": "UNPAID_BALANCE", "blocking": False,
                                "financial_impact": {"unpaid_balance_cents": str(new_open)},
                                "expected_system_behavior": "Record receivable for human collection or waiver; never take tax/tips; no negative payout.",
                                "scripted_human_decision": find_decision(v["name"], "UNPAID_BALANCE", d)})
            if d > final_day and receivable_delta > 0:
                reviews.append({"vendor_id": vid, "vendor_name": v["name"], "business_date": d,
                                "reason_code": "LATE_REFUND_RECOVERY", "blocking": False,
                                "financial_impact": {"refund_eligible_cents": str(refunds), "fee_change_cents": str(assessed),
                                                     "recovery_receivable_cents": str(receivable_delta)},
                                "expected_system_behavior": "Linked adjustment + balanced correcting entries; no rewrite of original postings; recovery/waiver by human.",
                                "scripted_human_decision": find_decision(v["name"], "LATE_REFUND_RECOVERY", d)})

            settlements.append({
                "vendor_id": vid, "vendor_name": v["name"], "festival_id": fx.festival["id"], "business_date": d,
                "status": "CALCULATED" if not holds else "HELD",
                "phase": "POST_FESTIVAL_ADJUSTMENT" if d > final_day else ("FINAL" if d == final_day else "INTERIM"),
                "calculation_version": ORACLE_VERSION, "source_manifest_hash": manifest_hash,
                "terms_basis": terms_basis, "commission_bps": bps, "policy_refs": policy_refs,
                "line_items": [
                    {"code": "ELIGIBLE_SALES_CENTRAL", "amount_cents": str(elig_central)},
                    {"code": "ELIGIBLE_SALES_VENDOR_RETAINED_CASH", "amount_cents": str(elig_retained)},
                    {"code": "REFUNDS_INCLUDED_IN_ELIGIBLE", "amount_cents": str(refunds)},
                    {"code": "CUMULATIVE_ELIGIBLE", "amount_cents": str(cum_elig)},
                    {"code": "CUMULATIVE_PERCENT_FEE", "amount_cents": str(pct_fee_cum)},
                    {"code": "MINIMUM_ADJUSTMENT_CUMULATIVE", "amount_cents": str(min_adj)},
                    {"code": "PRIOR_ASSESSED", "amount_cents": str(prior)},
                    {"code": "PRIOR_UNPAID_BALANCE", "amount_cents": str(due - assessed)},
                    {"code": "PROCESSOR_FEES_ON_VENDOR_PAYMENTS", "amount_cents": str(proc_fees)},
                    {"code": "PROCESSOR_FEES_DEDUCTED", "amount_cents": str(deducted)},
                    {"code": "TAX_COLLECTED_CENTRAL_PROTECTED", "amount_cents": str(tax_central)},
                    {"code": "TAX_IN_VENDOR_RETAINED_CASH_MEMO", "amount_cents": str(tax_retained)},
                    {"code": "TIPS_PASSTHROUGH", "amount_cents": str(tips_central)},
                ],
                "assessed_fee_cents": str(assessed), "collected_fee_cents": str(collected),
                "payout_cents": str(payout), "tips_passthrough_cents": str(tips_central),
                "unpaid_balance_cents": str(new_open), "receivable_change_cents": str(receivable_delta),
                "holds": holds,
                "_journal": {"collected_from_payable": assessed - receivable_delta, "assessed": assessed,
                             "receivable_delta": receivable_delta, "deducted": deducted},
            })
            pay_at = fx.local_to_utc((date.fromisoformat(d) + timedelta(days=1)).isoformat(), payout_time)
            payouts.append({"vendor_name": v["name"], "vendor_id": vid, "business_date": d,
                            "logical_obligation_key": f"payout:{vid}:{d}:1",
                            "state": state if transfer > 0 else "NOTHING_TO_PAY",
                            "amount_cents": str(payout), "tips_passthrough_cents": str(tips_central),
                            "transfer_cents": str(transfer), "scheduled_at_utc": pay_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "requires_finance_lead": transfer > limit, "hold_reasons": [h["reason_code"] for h in holds]})
    return {"settlements": settlements, "reviews": reviews, "payouts": payouts}


# ---------------------------------------------------------------- cash check
def cash_availability(fx: Fixture, payouts: list[dict]) -> list[dict]:
    opening = cents(fx.festival["bank_accounts"][0]["opening_balance_cents"])
    released = [p for p in payouts if p["state"].startswith("RELEASE_ELIGIBLE")]
    out = []
    for t in sorted({p["scheduled_at_utc"] for p in released}):
        tt = parse_utc(t)
        bank = sum(cents(b["amount_cents"]) * (1 if b["direction"] == "credit" else -1)
                   for b in fx.bank if parse_utc(b["occurred_at"]) <= tt)
        before = sum(cents(p["transfer_cents"]) for p in released if parse_utc(p["scheduled_at_utc"]) < tt)
        now = sum(cents(p["transfer_cents"]) for p in released if p["scheduled_at_utc"] == t)
        avail = opening + bank - before
        out.append({"payout_time_utc": t, "available_before_cents": str(avail), "requested_cents": str(now),
                    "remaining_cents": str(avail - now), "sufficient": avail - now >= 0})
    return out


# ---------------------------------------------------------------- journals
def journals(fx: Fixture, result: dict) -> list[dict]:
    entries = []

    def add(key, bdate, kind, lines, refs, correction_of=None, note=None):
        lines = [l for l in lines if l[1] or l[2]]
        norm = []
        for acct, dr, cr in lines:
            if dr < 0 or cr < 0:  # flip sign onto the other side; never negative amounts
                dr, cr = max(dr, 0) + max(-cr, 0), max(cr, 0) + max(-dr, 0)
            norm.append({"account": acct, "account_name": ACCOUNTS[acct], "debit_cents": str(dr), "credit_cents": str(cr)})
        e = {"entry_key": key, "business_date": bdate, "kind": kind, "posting_rule_version": "DRAFT-posting-map-0.1",
             "source_record_ids": refs, "correction_of": correction_of, "lines": norm}
        if note:
            e["note"] = note
        entries.append(e)

    vname = lambda vid: fx.vendor_by_id[vid]["name"]
    capture_key = {}
    for p in fx.payments:
        if p["custody"] != "CENTRALLY_COLLECTED":
            continue
        o = fx.order_by_id[p["order_refs"][0]["order_id"]]
        e, t, tp, g = (cents(o[f]) for f in ("eligible_net_cents", "tax_cents", "tips_cents", "gross_cents"))
        asset = "1100" if p["method"] == "CARD" else "1050"
        if p["kind"] == "CAPTURE":
            k = f"SALE:{o['source_native_id']}"
            capture_key[o["id"]] = k
            add(k, p["business_date"], "SALES_CAPTURE_CENTRAL",
                [(asset, g, 0), ("2000", 0, e), ("2100", 0, t), ("2200", 0, tp)], [o["id"], p["id"]])
        else:
            add(f"REFUND:{o['source_native_id']}", p["business_date"], "REFUND_CENTRAL",
                [("2000", -e, 0), ("2100", -t, 0), ("2200", -tp, 0), (asset, 0, -g)], [o["id"], p["id"]],
                correction_of=capture_key.get(o["refund_of_order_id"]),
                note="Linked to original sale; original posting untouched.")
    by_ref = defaultdict(list)
    for b in fx.batches:
        by_ref[b["payout_reference"]].append(b)
    for be in fx.bank:
        bd = fx.local_date(be["occurred_at"])
        amt = cents(be["amount_cents"])
        if be["provider_reference"] in by_ref:
            mem = by_ref[be["provider_reference"]]
            gross = sum(cents(b["gross_cents"]) for b in mem)
            fees = sum(cents(b["fees_cents"]) for b in mem)
            signed = amt if be["direction"] == "credit" else -amt
            add(f"DEPOSIT:{be['provider_reference']}", bd, "PROCESSOR_SETTLEMENT",
                [("1000", signed, 0), ("5100", fees, 0), ("1100", 0, gross)], [be["id"]] + [b["id"] for b in mem])
        else:
            add(f"CASHDEP:{be['provider_reference']}", bd, "CASH_OFFICE_DEPOSIT",
                [("1000", amt, 0), ("1050", 0, amt)], [be["id"]])
    for s in result["settlements"]:
        if s["status"] == "HELD" and s["assessed_fee_cents"] is None:
            continue
        j, tag = s["_journal"], f"{s['vendor_name']}:{s['business_date']}"
        if j["deducted"]:
            add(f"FEE_RECHARGE:{tag}", s["business_date"], "PROCESSOR_FEE_RECHARGE",
                [("2000", j["deducted"], 0), ("5100", 0, j["deducted"])], [s["vendor_id"]],
                note="Contract permits deducting processor fees from vendor settlement.")
        if j["assessed"] or j["receivable_delta"]:
            add(f"COMMISSION:{tag}", s["business_date"], "COMMISSION_SETTLEMENT",
                [("2000", j["collected_from_payable"], 0), ("1200", j["receivable_delta"], 0), ("4000", 0, j["assessed"])],
                [s["vendor_id"]],
                correction_of=(f"COMMISSION:{s['vendor_name']}:{fx.festival['business_dates'][-1]}"
                               if s.get("phase") == "POST_FESTIVAL_ADJUSTMENT" else None))
    for p in result["payouts"]:
        if not p["state"].startswith("RELEASE_ELIGIBLE"):
            continue
        a, tp = cents(p["amount_cents"]), cents(p["tips_passthrough_cents"])
        add(f"PAYOUT:{p['vendor_name']}:{p['business_date']}", p["business_date"], "VENDOR_PAYOUT_SIMULATED",
            [("2000", a, 0), ("2200", tp, 0), ("1000", 0, a + tp)], [p["vendor_id"]],
            note="Expected only once the simulated payout is CONFIRMED.")
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


# ---------------------------------------------------------------- driver
def run(fixture: Path = DEFAULT_FIXTURE, decisions_path: Path = HERE / "scripted_decisions.json") -> dict:
    fx = Fixture(fixture)
    terms = derive_terms(fx)
    checks = verify_inputs(fx)
    recon = reconcile(fx)
    dec_doc = json.loads(decisions_path.read_text())
    out = {"_common": {
        "oracle_version": ORACLE_VERSION, "evaluator_only": True,
        "derived_from_manifest_sha256": hashlib.sha256(fx.manifest_bytes).hexdigest(),
        "fixture_id": fx.manifest["fixture_id"], "seed": fx.manifest["seed"],
        "derived_terms": {fx.vendor_by_id[k]["name"]: v for k, v in terms.items()},
        "input_checks": checks, "reconciliation": recon, "accounts": ACCOUNTS}}
    for set_id, ds in dec_doc["decision_sets"].items():
        res = settle(fx, terms, ds["decisions"])
        ents = journals(fx, res)
        for s in res["settlements"]:
            s.pop("_journal", None)
        out[set_id] = {"description": ds["description"], "settlements": res["settlements"],
                       "required_reviews": res["reviews"], "payouts": res["payouts"],
                       "cash_availability": cash_availability(fx, res["payouts"]),
                       "journal_entries": ents, "ending_balances": balances(ents)}
    return out


def dump(obj) -> bytes:
    return (json.dumps(obj, sort_keys=True, indent=2) + "\n").encode()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    ap.add_argument("--decisions", type=Path, default=HERE / "scripted_decisions.json")
    ap.add_argument("--out", type=Path, default=HERE / "expected" / "compact_v1")
    a = ap.parse_args(argv)
    out = run(a.fixture, a.decisions)
    a.out.mkdir(parents=True, exist_ok=True)
    for k, v in out.items():
        name = "common.json" if k == "_common" else f"{k}.json"
        (a.out / name).write_bytes(dump(v))
    for k, v in out.items():
        if k == "_common":
            continue
        print(f"{k}: {len(v['settlements'])} settlements, {len(v['required_reviews'])} reviews, "
              f"{len(v['journal_entries'])} journal entries (all balanced), ending={v['ending_balances']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
