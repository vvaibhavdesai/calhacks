#!/usr/bin/env python3
"""Encore evaluation harness / scorer (lane B, task B02). EVALUATOR-ONLY.

Scores a CANDIDATE output directory (what lane A's system would export after being
driven with one scripted human-decision set) against the independent oracle's
expectations for the same fixture and decision set.

Error classes reported:
  * missed_reviews          expected review/case not raised
  * false_holds             vendor-day held (settlement or payout) or blocking case raised when not expected
  * repeated_questions      a question re-asked although a reusable decision already answers it (or asked twice)
  * unexplained_variances   reconciliation variance marked MATCHED/absent, or expected case-required variance not flagged
  * payout_errors           wrong final state/amount, unsafe releases, duplicate provider payments, changed keys, overspend
  * settlement_errors       wrong fee/payout/unpaid/rate fields
  * invalid_citations       SourceRef that does not resolve to the exact source/version/hash/locator/excerpt, or cites an
                            expired/out-of-scope prior-year policy
  * journal_errors          unbalanced entries, negative amounts, ending balance by account ROLE differs

The candidate format is a PROPOSAL for lane A (see docs/progress/lane-b.md, "Candidate export format").

Usage:
    python3 tests/oracle/score.py --candidate DIR [--fixture DIR] [--expected DIR] [--set F1_story] [--json]
    python3 tests/oracle/score.py --emit-oracle-candidate DIR --set F1_story   # write the oracle's own answer as a candidate
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import oracle_full as of  # noqa: E402  (evaluator-side helper: PDF text extraction, fixture loading)

CANDIDATE_FORMAT = "encore-candidate-0.1-PROPOSAL"
SETTLEMENT_FIELDS = ("status", "assessed_fee_cents", "collected_fee_cents", "payout_cents", "tips_passthrough_cents",
                     "unpaid_balance_cents", "commission_bps")
CANDIDATE_FILES = ("settlements", "payouts", "cases", "journals", "reconciliation")


# ---------------------------------------------------------------- candidate IO
def oracle_to_candidate(expected: dict, common: dict, set_id: str) -> dict:
    """The oracle's own answer expressed in the candidate format (used to prove the scorer gives a perfect score)."""
    cand = {"meta": {"candidate_format": CANDIDATE_FORMAT, "fixture_id": common["fixture_id"],
                     "fixture_manifest_sha256": common["derived_from_manifest_sha256"], "decision_set": set_id,
                     "system": "independent-oracle (self-check)"}}
    cand["settlements"] = {"items": [{
        "vendor_id": s["vendor_id"], "business_date": s["business_date"], "status": s["status"],
        **{k: s[k] for k in SETTLEMENT_FIELDS if k != "status"},
        "hold_reasons": [h["reason_code"] for h in s["holds"]],
        "citations": [r for r in s["policy_refs"] if "source_id" in r]} for s in expected["settlements"]]}
    cand["payouts"] = {"items": [{
        "vendor_id": p["vendor_id"], "business_date": p["business_date"], "final_state": p["final_state"],
        "amount_cents": p["amount_cents"], "tips_passthrough_cents": p["tips_passthrough_cents"],
        "idempotency_keys": [p["idempotency_key"]], "provider_submissions": p["provider_submissions"],
        "confirmed_provider_payments": p["confirmed_provider_payments"], "released_at_utc": p["released_at_utc"],
        "hold_reasons": p["hold_reasons"]} for p in expected["payouts"]]}
    cand["cases"] = {"items": [{
        "case_id": f"case-{i:04d}", "reason_code": r["reason_code"], "subject_kind": r["subject_kind"], "subject_id": r["subject_id"],
        "business_date": r["business_date"], "blocking": r["blocking"], "citations": r["evidence_refs"]}
        for i, r in enumerate(expected["required_reviews"], start=1)]}
    cand["journals"] = {"items": [{
        "entry_id": e["entry_key"], "business_date": e["business_date"], "kind": e["kind"], "correction_of": e["correction_of"],
        "lines": [{"account_role": l["account_role"], "debit_cents": l["debit_cents"], "credit_cents": l["credit_cents"]}
                  for l in e["lines"]]} for e in expected["journal_entries"]]}
    cand["reconciliation"] = {"items": [{
        "match_key": g["match_key"], "status": g["status"], "variance_cents": g["variance_cents"], "reason_code": g["reason_code"]}
        for g in common["reconciliation"]["match_groups"]]}
    return cand


def write_candidate(cand: dict, d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps(cand["meta"], indent=1, sort_keys=True) + "\n")
    for name in CANDIDATE_FILES:
        (d / f"{name}.json").write_text(json.dumps(cand[name], sort_keys=True) + "\n")


def read_candidate(d: Path) -> dict:
    cand = {"meta": json.loads((d / "meta.json").read_text())}
    for name in CANDIDATE_FILES:
        p = d / f"{name}.json"
        cand[name] = json.loads(p.read_text()) if p.exists() else {"items": []}
    return cand


# ---------------------------------------------------------------- citations
class CitationChecker:
    def __init__(self, fx: of.Fixture):
        self.fx = fx
        self.rows = {}

    def _csv_rows(self, doc) -> list[str]:
        if doc["id"] not in self.rows:
            text = self.fx.doc_bytes(doc).decode("utf-8")
            self.rows[doc["id"]] = text.splitlines()
        return self.rows[doc["id"]]

    def check(self, ref: dict) -> str | None:
        """Return None if valid, else a short reason."""
        doc = self.fx.doc_by_id.get(ref.get("source_id"))
        if doc is None:
            return "UNKNOWN_SOURCE"
        if ref.get("source_version") != doc["version"]:
            return "WRONG_VERSION"
        if ref.get("content_hash") != doc["content_hash"]:
            return "WRONG_HASH"
        try:
            self.fx.doc_bytes(doc)
        except of.FixtureError:
            return "SOURCE_BYTES_CHANGED"
        if doc["kind"] == "HISTORY_EXPORT" and "policies" in doc["path"]:
            return "CITES_EXPIRED_PRIOR_YEAR_POLICY"
        kind, loc, ex = ref.get("locator_kind"), ref.get("locator", ""), ref.get("quoted_excerpt")
        if kind == "row":
            m = re.fullmatch(r"row=(\d+)(?:-(\d+))?", loc)
            if not m:
                return "BAD_LOCATOR"
            a, b = int(m.group(1)), int(m.group(2) or m.group(1))
            rows = self._csv_rows(doc)
            if not (1 <= a <= b < len(rows)):
                return "ROW_OUT_OF_RANGE"
            if ex and not any(ex in r for r in rows[a:b + 1]):
                return "EXCERPT_NOT_AT_LOCATOR"
            return None
        if kind == "page":
            m = re.fullmatch(r"page=(\d+)", loc)
            pages = self.fx.doc_pages(doc)
            if not m or not (1 <= int(m.group(1)) <= len(pages)):
                return "PAGE_OUT_OF_RANGE"
            if ex and of.norm_ws(ex) not in of.norm_ws(pages[int(m.group(1)) - 1]):
                return "EXCERPT_NOT_AT_LOCATOR"
            return None
        if kind == "span":
            m = re.fullmatch(r"chars=(\d+)-(\d+)", loc)
            text = self.fx.doc_pages(doc)[0] if doc["media_type"] != "application/pdf" else None
            if not m or text is None:
                return "BAD_LOCATOR"
            a, b = int(m.group(1)), int(m.group(2))
            if not (0 <= a < b <= len(text)):
                return "SPAN_OUT_OF_RANGE"
            if ex is not None and text[a:b] != ex:
                return "EXCERPT_NOT_AT_LOCATOR"
            return None
        return "BAD_LOCATOR_KIND"


# ---------------------------------------------------------------- scoring
def score(cand: dict, expected: dict, common: dict, fx: of.Fixture, set_id: str) -> dict:
    rep: dict = {"decision_set": set_id, "fixture_id": common["fixture_id"], "candidate_format": cand["meta"].get("candidate_format")}
    errors = Counter()
    detail = defaultdict(list)
    if cand["meta"].get("fixture_manifest_sha256") != common["derived_from_manifest_sha256"]:
        errors["wrong_fixture"] += 1
        detail["wrong_fixture"].append(cand["meta"].get("fixture_manifest_sha256"))

    # ---- reviews / cases
    exp_rev = {(r["reason_code"], r["subject_id"], r["business_date"]): r for r in expected["required_reviews"]}
    reusable = {}
    for r in expected["required_reviews"]:
        d = r.get("scripted_human_decision")
        if d and d.get("scope") == "REUSABLE_VENDOR_FESTIVAL":
            reusable[(r["reason_code"], r["subject_id"])] = r["business_date"]
    seen = Counter()
    for c in cand["cases"]["items"]:
        k = (c["reason_code"], c.get("subject_id"), c["business_date"])
        seen[k] += 1
        if k in exp_rev:
            if seen[k] > 1:
                errors["repeated_questions"] += 1
                detail["repeated_questions"].append({"case": k, "why": "asked more than once"})
            elif bool(c.get("blocking")) != bool(exp_rev[k]["blocking"]):
                errors["blocking_flag_mismatch"] += 1
                detail["blocking_flag_mismatch"].append(k)
            continue
        rk = (c["reason_code"], c.get("subject_id"))
        if rk in reusable and reusable[rk] < c["business_date"]:
            errors["repeated_questions"] += 1
            detail["repeated_questions"].append({"case": k, "why": f"answered reusably on {reusable[rk]}"})
        elif c.get("blocking"):
            errors["false_holds"] += 1
            detail["false_holds"].append({"unexpected_blocking_case": k})
        else:
            errors["unexpected_nonblocking_cases"] += 1
            detail["unexpected_nonblocking_cases"].append(k)
    for k, r in exp_rev.items():
        if seen[k] == 0:
            errors["missed_reviews"] += 1
            detail["missed_reviews"].append({"review": k, "blocking": r["blocking"]})

    # ---- settlements
    exp_s = {(s["vendor_id"], s["business_date"]): s for s in expected["settlements"]}
    got_s = {(s["vendor_id"], s["business_date"]): s for s in cand["settlements"]["items"]}
    for k, e in exp_s.items():
        g = got_s.get(k)
        if g is None:
            errors["settlement_errors"] += 1
            detail["settlement_errors"].append({"missing": k})
            continue
        if g["status"] == "HELD" and e["status"] != "HELD":
            errors["false_holds"] += 1
            detail["false_holds"].append({"settlement": k})
        elif e["status"] == "HELD" and g["status"] != "HELD":
            errors["missed_holds"] += 1
            detail["missed_holds"].append({"settlement": k})
        bad = [f for f in SETTLEMENT_FIELDS if f != "status" and str(g.get(f)) != str(e.get(f))]
        if bad:
            errors["settlement_errors"] += 1
            detail["settlement_errors"].append({"settlement": k, "fields": bad})
    for k in got_s.keys() - exp_s.keys():
        errors["settlement_errors"] += 1
        detail["settlement_errors"].append({"unexpected": k})

    # ---- payouts
    exp_p = {(p["vendor_id"], p["business_date"]): p for p in expected["payouts"]}
    got_p = {(p["vendor_id"], p["business_date"]): p for p in cand["payouts"]["items"]}
    exact = 0
    abs_err = 0
    for k, e in exp_p.items():
        g = got_p.get(k)
        if g is None:
            errors["payout_errors"] += 1
            detail["payout_errors"].append({"missing": k})
            continue
        ok = g["final_state"] == e["final_state"] and g["amount_cents"] == e["amount_cents"] \
            and g["tips_passthrough_cents"] == e["tips_passthrough_cents"]
        if g["final_state"] == "CONFIRMED" and e["final_state"] in ("HELD", "FAILED"):
            errors["unsafe_releases"] += 1
            detail["unsafe_releases"].append({"payout": k, "expected": e["final_state"], "hold_reasons": e["hold_reasons"]})
        elif g["final_state"] == "HELD" and e["final_state"] == "CONFIRMED":
            errors["false_holds"] += 1
            detail["false_holds"].append({"payout": k})
        elif not ok:
            errors["payout_errors"] += 1
            detail["payout_errors"].append({"payout": k, "expected": [e["final_state"], e["amount_cents"], e["tips_passthrough_cents"]],
                                            "got": [g["final_state"], g["amount_cents"], g["tips_passthrough_cents"]]})
        if ok:
            exact += 1
        if g["final_state"] == "CONFIRMED" or e["final_state"] == "CONFIRMED":
            got_t = int(g["amount_cents"]) + int(g["tips_passthrough_cents"]) if g["final_state"] == "CONFIRMED" else 0
            exp_t = int(e["amount_cents"]) + int(e["tips_passthrough_cents"]) if e["final_state"] == "CONFIRMED" else 0
            abs_err += abs(got_t - exp_t)
        if int(g.get("confirmed_provider_payments", 0)) > 1:
            errors["duplicate_provider_payments"] += 1
            detail["duplicate_provider_payments"].append(k)
        if len(set(g.get("idempotency_keys") or [])) > 1:
            errors["idempotency_key_changed"] += 1
            detail["idempotency_key_changed"].append(k)
    for k in got_p.keys() - exp_p.keys():
        errors["payout_errors"] += 1
        detail["payout_errors"].append({"unexpected": k})
    # overspend: candidate releases never exceed bank cash posted by then (oracle-independent check)
    opening = sum(of.cents(o["amount_cents"]) for o in fx.opening_balances if o["account_role"] == "BANK_OPERATING")
    bank = [(of.parse_utc(b["occurred_at"]), of.cents(b["amount_cents"]) * (1 if b["direction"] == "credit" else -1)) for b in fx.bank_entries]
    rel = sorted((of.parse_utc(p["released_at_utc"]), int(p["amount_cents"]) + int(p["tips_passthrough_cents"]))
                 for p in cand["payouts"]["items"] if p["final_state"] == "CONFIRMED" and p.get("released_at_utc"))
    for t in sorted({t for t, _ in rel}):
        released = sum(a for tt, a in rel if tt <= t)
        cash = opening + sum(a for tt, a in bank if tt <= t)
        if released > cash:
            errors["cash_overspend_windows"] += 1
            detail["cash_overspend_windows"].append({"at": of.fmt_utc(t), "released_cents": released, "cash_cents": cash})
    if any(p["final_state"] == "CONFIRMED" and not p.get("released_at_utc") for p in cand["payouts"]["items"]):
        errors["payout_errors"] += 1
        detail["payout_errors"].append("confirmed payout without released_at_utc")
    rep["payout_accuracy"] = {"exact": exact, "expected": len(exp_p), "rate": round(exact / len(exp_p), 4) if exp_p else 1.0,
                              "abs_transfer_error_cents": abs_err}

    # ---- reconciliation / variances
    exp_g = {g["match_key"]: g for g in common["reconciliation"]["match_groups"]}
    got_g = {g["match_key"]: g for g in cand["reconciliation"]["items"]}
    for k, e in exp_g.items():
        g = got_g.get(k)
        if e["status"] == "CASE_REQUIRED" and (g is None or g["status"] != "CASE_REQUIRED"):
            errors["unexplained_variances"] += 1
            detail["unexplained_variances"].append({"expected_case": k, "got": g and g["status"]})
        elif e["status"] == "DUPLICATE_IGNORED" and g is not None and g["status"] != "DUPLICATE_IGNORED":
            errors["double_counted_duplicates"] += 1
            detail["double_counted_duplicates"].append(k)
        elif e["status"] == "MATCHED" and g is not None and g["status"] == "CASE_REQUIRED":
            errors["false_variance_cases"] += 1
            detail["false_variance_cases"].append(k)
    for k, g in got_g.items():
        if g["status"] == "MATCHED" and str(g.get("variance_cents", "0")) != "0":
            errors["unexplained_variances"] += 1
            detail["unexplained_variances"].append({"matched_with_variance": k, "variance_cents": g["variance_cents"]})

    # ---- citations
    cc = CitationChecker(fx)
    total = invalid = 0
    for coll in ("cases", "settlements"):
        for it in cand[coll]["items"]:
            for ref in it.get("citations") or []:
                total += 1
                why = cc.check(ref)
                if why:
                    invalid += 1
                    detail["invalid_citations"].append({"in": coll, "subject": it.get("subject_id") or it.get("vendor_id"),
                                                        "reason": why, "source_id": ref.get("source_id")})
    errors["invalid_citations"] = invalid
    rep["citations"] = {"total": total, "valid": total - invalid}
    blocking_without_citation = [c for c in cand["cases"]["items"]
                                 if c.get("blocking") and not c.get("citations")
                                 and c["reason_code"] not in ("PAYOUT_ABOVE_AUTO_LIMIT", "INSUFFICIENT_CASH", "PAYOUT_FAILED")]
    errors["blocking_cases_without_citation"] = len(blocking_without_citation)

    # ---- journals
    bal = defaultdict(int)
    for e in cand["journals"]["items"]:
        dr = cr = 0
        for l in e["lines"]:
            d, c = int(l["debit_cents"]), int(l["credit_cents"])
            if d < 0 or c < 0:
                errors["journal_errors"] += 1
                detail["journal_errors"].append({"negative_amount": e["entry_id"]})
            dr += d
            cr += c
            bal[l["account_role"]] += d - c
        if dr != cr:
            errors["journal_errors"] += 1
            detail["journal_errors"].append({"unbalanced": e["entry_id"], "debit": dr, "credit": cr})
    exp_bal = defaultdict(int)
    for acct, v in expected["ending_balances"].items():
        exp_bal[common["accounts"][acct]["role"]] += int(v)
    for role in sorted(set(exp_bal) | set(bal)):
        if exp_bal[role] != bal[role]:
            errors["journal_errors"] += 1
            detail["journal_errors"].append({"ending_balance_role": role, "expected": exp_bal[role], "got": bal[role]})

    keys = ("missed_reviews", "false_holds", "missed_holds", "repeated_questions", "unexplained_variances", "double_counted_duplicates",
            "false_variance_cases", "payout_errors", "unsafe_releases", "duplicate_provider_payments", "idempotency_key_changed",
            "cash_overspend_windows", "settlement_errors", "invalid_citations", "blocking_cases_without_citation", "journal_errors",
            "blocking_flag_mismatch", "unexpected_nonblocking_cases", "wrong_fixture")
    rep["errors"] = {k: errors.get(k, 0) for k in keys}
    rep["errors_total"] = sum(rep["errors"].values())
    rep["perfect"] = rep["errors_total"] == 0
    rep["expected_counts"] = {"reviews": len(exp_rev), "settlements": len(exp_s), "payouts": len(exp_p),
                              "journal_entries": len(expected["journal_entries"]), "match_groups": len(exp_g)}
    rep["detail"] = {k: v[:25] for k, v in detail.items()}
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixture", type=Path, default=of.DEFAULT_FIXTURE)
    ap.add_argument("--expected", type=Path, default=None, help="default tests/oracle/generated/<fixture_id>/")
    ap.add_argument("--set", default="F1_story")
    ap.add_argument("--candidate", type=Path)
    ap.add_argument("--emit-oracle-candidate", type=Path)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    fx = of.Fixture(a.fixture)
    exp_dir = a.expected or HERE / "generated" / fx.manifest["fixture_id"]
    common = json.loads((exp_dir / "common.json").read_text())
    expected = json.loads((exp_dir / f"{a.set}.json").read_text())
    if a.emit_oracle_candidate:
        write_candidate(oracle_to_candidate(expected, common, a.set), a.emit_oracle_candidate)
        print(f"wrote oracle self-candidate to {a.emit_oracle_candidate}")
        return 0
    if not a.candidate:
        ap.error("--candidate or --emit-oracle-candidate is required")
    rep = score(read_candidate(a.candidate), expected, common, fx, a.set)
    if a.json:
        print(json.dumps(rep, indent=1, sort_keys=True))
    else:
        print(f"set={a.set} fixture={rep['fixture_id']} perfect={rep['perfect']} errors_total={rep['errors_total']}")
        for k, v in rep["errors"].items():
            if v:
                print(f"  {k}: {v}")
        print(f"  payout_accuracy: {rep['payout_accuracy']}")
        print(f"  citations: {rep['citations']}")
    return 0 if rep["perfect"] else 1


if __name__ == "__main__":
    sys.exit(main())
