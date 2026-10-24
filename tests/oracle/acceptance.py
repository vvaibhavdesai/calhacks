#!/usr/bin/env python3
"""Encore B07 reconciliation acceptance harness (lane B). EVALUATOR-ONLY.

Runs the B02 scorer (score.py) plus B07 area checks over the full 80-vendor / 40,000-order
dataset for one or more scripted human-decision sets, and reports normal vs adversarial
scenario coverage.

Two modes, and they must never be confused:

  live       Scores lane A's CANDIDATE export (format encore-candidate-0.1-PROPOSAL plus the B07
             area extension files below). Only a live run can count as release evidence, and only
             if every set and every area passes.
  self-test  Proves the HARNESS works, with no lane A system: the oracle's own answer must score
             perfectly and each deliberately perturbed candidate must fail in the expected place.
             A self-test is NOT release evidence and its report says so (evidence_kind).

B07 area extension (PROPOSAL, optional files next to the B02 candidate files; a missing file is
reported NOT_EXPORTED, which is not a pass):
  payables.json     {items:[{invoice_id, status, outstanding_cents}], duplicate_invoice_ids:[...]}
  sponsorship.json  {items:[{invoice_number, received_cents, outstanding_memo_cents, status}]}
  tax_draft.json    {by_business_date:[{business_date, eligible_central_cents, tax_supplied_central_cents,
                     tax_recalculated_central_cents}], mismatch_line_ids:[...]}
  reports.json      {festival_pnl:{<account ROLE>: <signed cents string>}, trial_balance_zero: bool}

Usage:
  python3 tests/oracle/acceptance.py self-test [--fixture DIR] [--sets F1_story,F0_no_decision,F2_one_time]
  python3 tests/oracle/acceptance.py live --candidate-root DIR [--sets ...] [--system-commit SHA]
  python3 tests/oracle/acceptance.py live --export-url URL [--token-env ENCORE_LIVE_TOKEN] [--sets ...]

Reports are written to tests/oracle/acceptance-runs/<run_id>/ (gitignored).
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
import oracle_full as of  # noqa: E402
import score as sc  # noqa: E402

HARNESS_VERSION = "b07-acceptance-0.1"
DEFAULT_SETS = ("F1_story", "F0_no_decision", "F2_one_time")
AREA_FILES = ("payables", "sponsorship", "tax_draft", "reports")
REQUIRED_SCALE = {"vendors": 80, "sales_orders": 40000}
PNL_ROLES = ("COMMISSION_REVENUE", "TICKET_REVENUE", "SPONSORSHIP_REVENUE", "PROCESSOR_FEE_EXPENSE",
             "TICKETING_FEE_EXPENSE", "CASH_OVER_SHORT", "SUPPLIER_EXPENSE", "OPERATING_EXPENSE")
NOT_RELEASE_EVIDENCE = ("HARNESS SELF-TEST - NOT RELEASE EVIDENCE. Scores the independent oracle's own answer and "
                        "perturbed copies of it; no lane A system was involved.")


# ---------------------------------------------------------------- expected area data
def role_balances(journal_items) -> dict:
    bal = defaultdict(int)
    for e in journal_items:
        for line in e["lines"]:
            bal[line["account_role"]] += int(line["debit_cents"]) - int(line["credit_cents"])
    return bal


def expected_role_balances(expected: dict, common: dict) -> dict:
    bal = defaultdict(int)
    for acct, v in expected["ending_balances"].items():
        bal[common["accounts"][acct]["role"]] += int(v)
    return bal


def oracle_area_files(expected: dict, common: dict) -> dict:
    """The oracle's own answer for the B07 area extension (self-test only)."""
    pay = expected["payables"]
    bal = expected_role_balances(expected, common)
    tax = common["tax_summary_draft"]
    return {
        "payables": {"items": [{"invoice_id": i["invoice_id"], "status": i["status"], "outstanding_cents": i["outstanding_cents"]}
                               for i in pay["supplier_invoices"]],
                     "duplicate_invoice_ids": list(pay["duplicate_invoice_ids"])},
        "sponsorship": {"items": [{k: s[k] for k in ("invoice_number", "received_cents", "outstanding_memo_cents", "status")}
                                  for s in pay["sponsorship_receivables_memo"]]},
        "tax_draft": {"by_business_date": [{k: d[k] for k in ("business_date", "eligible_central_cents", "tax_supplied_central_cents",
                                                               "tax_recalculated_central_cents")} for d in tax["by_business_date"]],
                      "mismatch_line_ids": [m["line_id"] for m in tax["mismatch_lines"]]},
        "reports": {"festival_pnl": {r: str(bal.get(r, 0)) for r in PNL_ROLES},
                    "trial_balance_zero": sum(bal.values()) == 0},
    }


# ---------------------------------------------------------------- area checks
def check_areas(cand: dict, expected: dict, common: dict, core: dict) -> dict:
    missed = [d["review"] for d in core["detail"].get("missed_reviews", [])]
    got_bal = role_balances(cand["journals"]["items"])
    exp_bal = expected_role_balances(expected, common)
    recon_exp = {g["match_key"]: g for g in common["reconciliation"]["match_groups"]}
    recon_got = {g["match_key"]: g for g in cand["reconciliation"]["items"]}
    areas = {}

    def finish(name, problems, exported=True):
        areas[name] = {"status": "NOT_EXPORTED" if not exported else ("PASS" if not problems else "FAIL"),
                       "problems": problems[:25], "problem_count": len(problems)}

    def roles(*rs):
        return [{"role": r, "expected": exp_bal.get(r, 0), "got": got_bal.get(r, 0)} for r in rs if exp_bal.get(r, 0) != got_bal.get(r, 0)]

    def recon(prefixes):
        out = []
        for k, e in recon_exp.items():
            if k.startswith(prefixes):
                g = recon_got.get(k)
                if g is None or g["status"] != e["status"] or str(g.get("variance_cents", "0")) != str(e["variance_cents"]):
                    out.append({"match_key": k, "expected": [e["status"], e["variance_cents"]], "got": g and [g["status"], g.get("variance_cents")]})
        return out

    # payables and expenses
    probs = [{"missed_review": m} for m in missed if m[0] in ("INVOICE_EXCEEDS_PO", "DUPLICATE_SUPPLIER_INVOICE")]
    probs += roles("SUPPLIER_AP", "SUPPLIER_EXPENSE", "OPERATING_EXPENSE") + recon(("SUPPLIER_PAYMENT:", "EXPENSE:"))
    p = cand.get("payables")
    if p is not None:
        got = {i["invoice_id"]: i for i in p.get("items", [])}
        for i in expected["payables"]["supplier_invoices"]:
            g = got.get(i["invoice_id"])
            if g is None or g.get("status") != i["status"] or str(g.get("outstanding_cents")) != i["outstanding_cents"]:
                probs.append({"invoice": i["invoice_id"], "expected": [i["status"], i["outstanding_cents"]],
                              "got": g and [g.get("status"), g.get("outstanding_cents")]})
        if sorted(p.get("duplicate_invoice_ids", [])) != sorted(expected["payables"]["duplicate_invoice_ids"]):
            probs.append({"duplicate_invoice_ids": p.get("duplicate_invoice_ids")})
    finish("payables_expenses", probs, p is not None)

    # tickets, sponsorship and cash
    probs = roles("TICKET_REVENUE", "DEFERRED_TICKET_REVENUE", "SPONSORSHIP_REVENUE", "TICKETING_FEE_EXPENSE", "BANK_OPERATING",
                  "CASH_OFFICE", "TIPS_LIABILITY", "VENDOR_FUNDS_PAYABLE")
    probs += recon(("TICKETS:", "SPONSOR:", "CASH_DEPOSIT:", "BANK_UNMATCHED:"))
    probs += [{"cash_overspend_windows": core["errors"]["cash_overspend_windows"]}] if core["errors"]["cash_overspend_windows"] else []
    s = cand.get("sponsorship")
    if s is not None:
        got = {i["invoice_number"]: i for i in s.get("items", [])}
        for i in expected["payables"]["sponsorship_receivables_memo"]:
            g = got.get(i["invoice_number"])
            want = [i["status"], i["received_cents"], i["outstanding_memo_cents"]]
            have = g and [g.get("status"), str(g.get("received_cents")), str(g.get("outstanding_memo_cents"))]
            if have != want:
                probs.append({"sponsor_invoice": i["invoice_number"], "expected": want, "got": have})
    finish("tickets_sponsorship_cash", probs, s is not None)

    # tax drafts
    probs = [{"missed_review": m} for m in missed if m[0] == "TAX_MISMATCH"] + roles("SALES_TAX_LIABILITY")
    t = cand.get("tax_draft")
    if t is not None:
        got = {d["business_date"]: d for d in t.get("by_business_date", [])}
        for d in common["tax_summary_draft"]["by_business_date"]:
            g = got.get(d["business_date"])
            for f in ("eligible_central_cents", "tax_supplied_central_cents", "tax_recalculated_central_cents"):
                if g is None or str(g.get(f)) != d[f]:
                    probs.append({"tax_date": d["business_date"], "field": f, "expected": d[f], "got": g and g.get(f)})
        want = sorted(m["line_id"] for m in common["tax_summary_draft"]["mismatch_lines"])
        if sorted(t.get("mismatch_line_ids", [])) != want:
            probs.append({"mismatch_line_ids": {"expected": len(want), "got": len(t.get("mismatch_line_ids", []))}})
    finish("tax_drafts", probs, t is not None)

    # financial reports
    probs = [{"journal_errors": core["errors"]["journal_errors"]}] if core["errors"]["journal_errors"] else []
    r = cand.get("reports")
    if r is not None:
        pnl = r.get("festival_pnl", {})
        for role in PNL_ROLES:
            if str(pnl.get(role)) != str(exp_bal.get(role, 0)):
                probs.append({"pnl_role": role, "expected": exp_bal.get(role, 0), "got": pnl.get(role)})
        if r.get("trial_balance_zero") is not True:
            probs.append({"trial_balance_zero": r.get("trial_balance_zero")})
    finish("financial_reports", probs, r is not None)
    return areas


# ---------------------------------------------------------------- scenario coverage
AREA_FOR_SCENARIO = {
    "INVOICE_EXCEEDS_PO": "payables_expenses", "DUPLICATE_SUPPLIER_INVOICE": "payables_expenses",
    "SUPPLIER_CREDIT_NOTE": "payables_expenses", "SPONSOR_PARTIAL_PAYMENT": "tickets_sponsorship_cash",
    "UNMATCHED_BANK_ENTRY": "tickets_sponsorship_cash",
}
CORE_ERRORS_FOR_SCENARIO = {
    "INSUFFICIENT_CASH": ("cash_overspend_windows", "unsafe_releases"),
    "DUPLICATE_IMPORT": ("double_counted_duplicates",),
    "DELAYED_GROUPED_DEPOSITS": ("unexplained_variances", "false_variance_cases"),
    "PAYOUT_ABOVE_AUTO_LIMIT": ("unsafe_releases", "payout_errors"),
    "MINIMUM_FEE_BRANCH": ("settlement_errors",), "COMMISSION_BRANCH": ("settlement_errors",),
    "CONTRACT_PHRASING_VARIANTS": ("settlement_errors",),
}
NORMAL_BRANCH_LABELS = {"MINIMUM_FEE_BRANCH", "COMMISSION_BRANCH", "CONTRACT_PHRASING_VARIANTS"}


def scenario_coverage(cand: dict, expected: dict, core: dict, areas: dict, manifest: dict) -> dict:
    exp_s = {(s["vendor_id"], s["business_date"]): s for s in expected["settlements"]}
    exp_p = {(p["vendor_id"], p["business_date"]): p for p in expected["payouts"]}
    got_s = {(s["vendor_id"], s["business_date"]): s for s in cand["settlements"]["items"]}
    got_p = {(p["vendor_id"], p["business_date"]): p for p in cand["payouts"]["items"]}
    got_cases = {(c["reason_code"], c.get("subject_id"), c["business_date"]) for c in cand["cases"]["items"]}

    def vendor_rows_ok(vid):
        bad = []
        for k, e in exp_s.items():
            if k[0] == vid:
                g = got_s.get(k)
                if g is None or any(str(g.get(f)) != str(e.get(f)) for f in sc.SETTLEMENT_FIELDS):
                    bad.append({"settlement": k})
        for k, e in exp_p.items():
            if k[0] == vid:
                g = got_p.get(k)
                if g is None or [g["final_state"], g["amount_cents"], g["tips_passthrough_cents"]] != \
                        [e["final_state"], e["amount_cents"], e["tips_passthrough_cents"]]:
                    bad.append({"payout": k})
        return bad

    def mentions(vid):
        hits = []
        for cls, items in core["detail"].items():
            for it in items:
                if vid in json.dumps(it):
                    hits.append(cls)
        return sorted(set(hits))

    rows = []
    scenario_vendors = set()
    for s in manifest["scenarios"]:
        vid = s.get("vendor_id")
        problems = []
        if vid:
            scenario_vendors.add(vid)
            problems += vendor_rows_ok(vid)
            for r in expected["required_reviews"]:
                if r["subject_id"] == vid and (r["reason_code"], vid, r["business_date"]) not in got_cases:
                    problems.append({"missed_review": [r["reason_code"], r["business_date"]]})
            problems += [{"scorer_errors_mention_vendor": c} for c in mentions(vid)]
        else:
            label = s["label"]
            for r in expected["required_reviews"]:
                if r["reason_code"] == label and (r["reason_code"], r["subject_id"], r["business_date"]) not in got_cases:
                    problems.append({"missed_review": [r["reason_code"], r["subject_id"], r["business_date"]]})
            if label in AREA_FOR_SCENARIO and areas[AREA_FOR_SCENARIO[label]]["status"] != "PASS":
                problems.append({"area": AREA_FOR_SCENARIO[label], "status": areas[AREA_FOR_SCENARIO[label]]["status"]})
            for err in CORE_ERRORS_FOR_SCENARIO.get(label, ()):
                if core["errors"].get(err):
                    problems.append({"scorer_error": err, "count": core["errors"][err]})
        rows.append({"scenario": s["label"], "kind": "normal-branch" if s["label"] in NORMAL_BRANCH_LABELS else "adversarial",
                     "vendor_id": vid, "status": "PASS" if not problems else "FAIL", "problems": problems[:10]})

    normal_vendors = sorted({v for v, _ in exp_s} - scenario_vendors)
    normal_bad = [v for v in normal_vendors if vendor_rows_ok(v)]
    return {"scenarios": rows,
            "scenarios_passed": sum(r["status"] == "PASS" for r in rows), "scenarios_total": len(rows),
            "normal_vendors_total": len(normal_vendors), "normal_vendors_exact": len(normal_vendors) - len(normal_bad),
            "normal_vendors_failing": normal_bad[:25]}


# ---------------------------------------------------------------- evaluation of one candidate
def evaluate(cand: dict, expected: dict, common: dict, fx: of.Fixture, set_id: str, manifest: dict) -> dict:
    core = sc.score(cand, expected, common, fx, set_id)
    areas = check_areas(cand, expected, common, core)
    cov = scenario_coverage(cand, expected, core, areas, manifest)
    passed = core["perfect"] and all(a["status"] == "PASS" for a in areas.values()) and cov["scenarios_passed"] == cov["scenarios_total"]
    return {"set": set_id, "passed": passed, "core": {k: core[k] for k in ("errors", "errors_total", "perfect", "payout_accuracy",
                                                                             "citations", "expected_counts", "detail")},
            "areas": areas, "coverage": cov}


def read_candidate_dir(d: Path) -> dict:
    cand = sc.read_candidate(d)
    for name in AREA_FILES:
        p = d / f"{name}.json"
        cand[name] = json.loads(p.read_text()) if p.exists() else None
    return cand


def fetch_candidate(base_url: str, set_id: str, token: str | None, dest: Path) -> Path:
    """Pull A's export over HTTP into dest/<set>/ (endpoint shape is a PROPOSAL, Q16 / Q56)."""
    out = dest / set_id
    out.mkdir(parents=True, exist_ok=True)
    for name in ("meta",) + sc.CANDIDATE_FILES + AREA_FILES:
        req = urllib.request.Request(f"{base_url.rstrip('/')}/{set_id}/{name}.json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                (out / f"{name}.json").write_bytes(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 404 and name in AREA_FILES:
                continue  # reported NOT_EXPORTED
            raise
    return out


# ---------------------------------------------------------------- self-test perturbations
def perturbations(base: dict, expected: dict, manifest: dict) -> dict:
    """Each entry: (candidate, where the failure must show up)."""
    roles = manifest["roles"]
    taco = roles["TERMS_CONFLICT_AND_LATE_AMENDMENT"]["vendor_id"]
    out = {}

    def first(items, pred):
        return next(i for i in items if pred(i))

    c = copy.deepcopy(base)
    first(c["settlements"]["items"], lambda s: s["vendor_id"] == taco and s["business_date"] == "2026-10-16")["commission_bps"] = 1500
    out["taco_night1_rate_ignored"] = (c, ("core", "settlement_errors"), ("scenario", "TERMS_CONFLICT_AND_LATE_AMENDMENT"))
    c = copy.deepcopy(base)
    c["cases"]["items"].append({"case_id": "dup", "reason_code": "CONFLICTING_TERMS", "subject_kind": "VENDOR", "subject_id": taco,
                                "business_date": "2026-10-17", "blocking": True, "citations": []})
    out["night2_question_reasked"] = (c, ("core", "repeated_questions"), ("scenario", "TERMS_CONFLICT_AND_LATE_AMENDMENT"))
    c = copy.deepcopy(base)
    v = roles["BENEFICIARY_CHANGE_UNVERIFIED"]["vendor_id"]
    c["cases"]["items"] = [x for x in c["cases"]["items"] if not (x["reason_code"] == "BENEFICIARY_CHANGED_UNVERIFIED" and x["subject_id"] == v)]
    out["prompt_injection_suppressed_beneficiary_review"] = (c, ("core", "missed_reviews"), ("scenario", "BENEFICIARY_CHANGE_UNVERIFIED"))
    c = copy.deepcopy(base)
    v = roles["PAYOUT_FAILED"]["vendor_id"]
    p = first(c["payouts"]["items"], lambda p: p["vendor_id"] == v and p["final_state"] in ("HELD", "FAILED"))
    p.update(final_state="CONFIRMED", released_at_utc="2026-10-19T17:00:00Z", confirmed_provider_payments=1)
    out["failed_payout_reported_confirmed"] = (c, ("core", "unsafe_releases"), ("scenario", "PAYOUT_FAILED"))
    c = copy.deepcopy(base)
    v = roles["PAYOUT_UNKNOWN_NOT_FOUND_RESUBMIT"]["vendor_id"]
    p = first(c["payouts"]["items"], lambda p: p["vendor_id"] == v and len(p["idempotency_keys"]) == 1 and p["final_state"] == "CONFIRMED")
    p["idempotency_keys"] = p["idempotency_keys"] + ["new-key-on-retry"]
    out["unknown_payout_retried_with_new_key"] = (c, ("core", "idempotency_key_changed"), ("scenario", "PAYOUT_UNKNOWN_NOT_FOUND_RESUBMIT"))
    c = copy.deepcopy(base)
    normal = next(s for s in c["settlements"]["items"] if s["vendor_id"] not in {r["vendor_id"] for r in roles.values() if r.get("vendor_id")}
                  and s["status"] == "CALCULATED")
    normal["payout_cents"] = str(int(normal["payout_cents"]) + 1)
    out["normal_vendor_off_by_one_cent"] = (c, ("core", "settlement_errors"), ("normal", normal["vendor_id"]))
    c = copy.deepcopy(base)
    c["payables"]["duplicate_invoice_ids"] = []
    first(c["payables"]["items"], lambda i: i["status"] == "DUPLICATE_NOT_POSTED")["status"] = "PAID"
    out["duplicate_supplier_invoice_paid"] = (c, ("area", "payables_expenses"), ("scenario", "DUPLICATE_SUPPLIER_INVOICE"))
    c = copy.deepcopy(base)
    first(c["sponsorship"]["items"], lambda i: i["status"] == "PARTIALLY_PAID").update(status="PAID", outstanding_memo_cents="0")
    out["partial_sponsor_marked_paid"] = (c, ("area", "tickets_sponsorship_cash"), ("scenario", "SPONSOR_PARTIAL_PAYMENT"))
    c = copy.deepcopy(base)
    c["tax_draft"]["mismatch_line_ids"] = c["tax_draft"]["mismatch_line_ids"][1:]
    out["tax_mismatch_line_dropped"] = (c, ("area", "tax_drafts"), None)
    c = copy.deepcopy(base)
    c["reports"]["festival_pnl"]["COMMISSION_REVENUE"] = str(int(c["reports"]["festival_pnl"]["COMMISSION_REVENUE"]) - 100)
    out["pnl_commission_off"] = (c, ("area", "financial_reports"), None)
    c = copy.deepcopy(base)
    c["reports"] = None
    out["reports_not_exported"] = (c, ("area_status", "financial_reports", "NOT_EXPORTED"), None)
    return out


def _detected(res: dict, where) -> bool:
    if where[0] == "core":
        return res["core"]["errors"].get(where[1], 0) > 0
    if where[0] == "area":
        return res["areas"][where[1]]["status"] == "FAIL"
    if where[0] == "area_status":
        return res["areas"][where[1]]["status"] == where[2]
    if where[0] == "scenario":
        return any(r["scenario"] == where[1] and r["status"] == "FAIL" for r in res["coverage"]["scenarios"])
    if where[0] == "normal":
        return where[1] in res["coverage"]["normal_vendors_failing"]
    raise ValueError(where)


# ---------------------------------------------------------------- run
def git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def load_context(fixture: Path):
    fx = of.Fixture(fixture)
    fid = fx.manifest["fixture_id"]
    exp_dir = HERE / "generated" / fid
    if not (exp_dir / "common.json").exists():
        raise SystemExit(f"missing oracle output {exp_dir}; run: python3 data/generator/generate_full.py && python3 tests/oracle/oracle_full.py")
    common = json.loads((exp_dir / "common.json").read_text())
    manifest = json.loads((exp_dir / "scenario_manifest.json").read_text())
    counts = fx.manifest["expected_record_counts"]
    scale = {k: counts.get(k) for k in REQUIRED_SCALE}
    scale_ok = all(counts.get(k) == v for k, v in REQUIRED_SCALE.items()) and len(fx.vendors) == 80 \
        and sum(1 for o in fx.orders if o["kind"] == "SALE") == 40000
    return fx, exp_dir, common, manifest, scale, scale_ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=("self-test", "live"))
    ap.add_argument("--fixture", type=Path, default=of.DEFAULT_FIXTURE)
    ap.add_argument("--sets", default=",".join(DEFAULT_SETS))
    ap.add_argument("--candidate-root", type=Path, help="live: directory with one sub-directory per decision set")
    ap.add_argument("--export-url", help="live: base URL serving <set>/<file>.json (PROPOSAL)")
    ap.add_argument("--token-env", default="ENCORE_LIVE_TOKEN", help="live: env var holding a bearer token (never logged)")
    ap.add_argument("--system-commit", default=os.environ.get("ENCORE_SYSTEM_COMMIT"), help="live: lane A commit that produced the export")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args(argv)
    sets = [s for s in a.sets.split(",") if s]

    fx, exp_dir, common, manifest, scale, scale_ok = load_context(a.fixture)
    started = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{started}-{a.mode}-{fx.manifest['fixture_id']}"
    out = a.out or HERE / "acceptance-runs" / run_id
    out.mkdir(parents=True, exist_ok=True)
    report = {"harness_version": HARNESS_VERSION, "run_id": run_id, "mode": a.mode,
              "evidence_kind": "live-candidate" if a.mode == "live" else "harness-self-test",
              "release_evidence": False, "notice": None if a.mode == "live" else NOT_RELEASE_EVIDENCE,
              "started_utc": started, "harness_commit": git_commit(), "fixture_id": fx.manifest["fixture_id"],
              "fixture_manifest_sha256": common["derived_from_manifest_sha256"], "oracle_version": common["oracle_version"],
              "scale": scale, "scale_ok": scale_ok, "sets": {}}

    if a.mode == "self-test":
        all_ok = scale_ok
        for set_id in sets:
            expected = json.loads((exp_dir / f"{set_id}.json").read_text())
            base = sc.oracle_to_candidate(expected, common, set_id)
            base.update(oracle_area_files(expected, common))
            with tempfile.TemporaryDirectory() as t:  # round trip through the on-disk format
                sc.write_candidate(base, Path(t))
                for name in AREA_FILES:
                    (Path(t) / f"{name}.json").write_text(json.dumps(base[name], sort_keys=True))
                base = read_candidate_dir(Path(t))
            res = evaluate(base, expected, common, fx, set_id, manifest)
            entry = {"oracle_self_candidate": {"passed": res["passed"], "errors_total": res["core"]["errors_total"],
                                               "areas": {k: v["status"] for k, v in res["areas"].items()},
                                               "scenarios": f"{res['coverage']['scenarios_passed']}/{res['coverage']['scenarios_total']}",
                                               "normal_vendors": f"{res['coverage']['normal_vendors_exact']}/{res['coverage']['normal_vendors_total']}",
                                               "payout_accuracy": res["core"]["payout_accuracy"]},
                     "perturbations": {}}
            all_ok &= res["passed"]
            if set_id == "F1_story":
                for name, (cand, where, also) in perturbations(base, expected, manifest).items():
                    r = evaluate(cand, expected, common, fx, set_id, manifest)
                    detected = _detected(r, where) and (also is None or _detected(r, also)) and not r["passed"]
                    entry["perturbations"][name] = {"detected": detected, "expected_failure": list(where) + (list(also) if also else []),
                                                    "errors_total": r["core"]["errors_total"],
                                                    "failing_areas": [k for k, v in r["areas"].items() if v["status"] != "PASS"]}
                    all_ok &= detected
            report["sets"][set_id] = entry
        report["self_test_passed"] = bool(all_ok)
        verdict = "HARNESS SELF-TEST PASSED" if all_ok else "HARNESS SELF-TEST FAILED"
    else:
        if not (a.candidate_root or a.export_url):
            print("not run: no live target (pass --candidate-root or --export-url with lane A's candidate export)")
            return 2
        token = os.environ.get(a.token_env) if a.export_url else None
        report["system_commit"] = a.system_commit
        report["source"] = {"candidate_root": str(a.candidate_root) if a.candidate_root else None, "export_url": a.export_url}
        all_ok = scale_ok
        for set_id in sets:
            d = fetch_candidate(a.export_url, set_id, token, out / "candidate") if a.export_url else a.candidate_root / set_id
            if not (d / "meta.json").exists():
                report["sets"][set_id] = {"passed": False, "error": f"no candidate export at {d}"}
                all_ok = False
                continue
            cand = read_candidate_dir(d)
            if "independent-oracle" in str(cand["meta"].get("system", "")):
                report["sets"][set_id] = {"passed": False, "error": "candidate is the oracle's own answer, not a lane A export"}
                all_ok = False
                continue
            if cand["meta"].get("decision_set") != set_id:
                report["sets"][set_id] = {"passed": False, "error": f"meta.decision_set={cand['meta'].get('decision_set')!r}"}
                all_ok = False
                continue
            expected = json.loads((exp_dir / f"{set_id}.json").read_text())
            res = evaluate(cand, expected, common, fx, set_id, manifest)
            res["candidate_meta"] = cand["meta"]
            report["sets"][set_id] = res
            all_ok &= res["passed"]
        report["acceptance_passed"] = bool(all_ok) and bool(a.system_commit)
        report["release_evidence"] = True  # a real candidate was scored; pass/fail is in acceptance_passed
        if not a.system_commit:
            report["acceptance_note"] = "system commit not recorded (--system-commit / ENCORE_SYSTEM_COMMIT); cannot pass"
        verdict = "LIVE ACCEPTANCE PASSED" if report["acceptance_passed"] else "LIVE ACCEPTANCE FAILED"

    report["finished_utc"] = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (out / "report.json").write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    print(f"{verdict}  run_id={run_id}  report={out / 'report.json'}")
    if report["notice"]:
        print(report["notice"])
    print(f"scale: {scale} ok={scale_ok}")
    for set_id, e in report["sets"].items():
        if a.mode == "self-test":
            o = e["oracle_self_candidate"]
            print(f"  {set_id}: oracle self-candidate passed={o['passed']} errors={o['errors_total']} areas={o['areas']} "
                  f"scenarios={o['scenarios']} normal_vendors={o['normal_vendors']}")
            for n, p in e["perturbations"].items():
                print(f"    perturbation {n}: detected={p['detected']} errors_total={p['errors_total']} failing_areas={p['failing_areas']}")
        else:
            print(f"  {set_id}: passed={e.get('passed')} {e.get('error', '')}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
