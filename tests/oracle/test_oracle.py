"""Oracle tests (stdlib unittest). EVALUATOR-ONLY. Run from repo root:

    python3 -m unittest discover -s tests/oracle -v

The constants below were worked by hand (see HAND_WORKED_EXAMPLES.md) BEFORE
being compared with oracle output; they are not copied from oracle results.
"""
import ast
import importlib.util
import json
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
FIXTURE = REPO / "data" / "generator" / "fixtures" / "compact_v1"
EXPECTED = HERE / "expected" / "compact_v1"

spec = importlib.util.spec_from_file_location("encore_oracle", HERE / "oracle.py")
oracle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oracle)

# (vendor, business_date) -> (assessed, collected, payout, tips, unpaid) in cents. Hand-worked.
HAND = {
    # C02 fixture 1: $10,000 eligible, 15%, processor fees deductible by contract
    ("Sunrise Smoothies", "2026-10-16"): (60000, 60000, 328904, 7000, 0),
    ("Sunrise Smoothies", "2026-10-17"): (45000, 45000, 246740, 3000, 0),
    ("Sunrise Smoothies", "2026-10-18"): (45000, 45000, 246715, 4000, 0),
    # C02 fixture 2: $2,000 eligible -> $500 minimum once, on final day only
    ("Kettle Corn Co", "2026-10-16"): (12000, 12000, 68000, 0, 0),
    ("Kettle Corn Co", "2026-10-17"): (10500, 10500, 59500, 0, 0),
    ("Kettle Corn Co", "2026-10-18"): (27500, 27500, 22500, 0, 0),
    # C02 fixture 3: $400 eligible -> $500 assessed, $400 collected, $100 unpaid, $0 payout
    ("Late Bloom Lemonade", "2026-10-18"): (50000, 40000, 0, 500, 10000),
    # C02 fixture 4: $10,000 vendor-retained cash + $2,000 central card
    ("Grill Masters", "2026-10-16"): (75000, 75000, 25000, 1000, 0),
    ("Grill Masters", "2026-10-17"): (52500, 50000, 0, 0, 2500),
    ("Grill Masters", "2026-10-18"): (52500, 50000, 0, 0, 5000),
    # C02 fixture 5: late refund after final payout
    ("Churro Cart", "2026-10-16"): (30000, 30000, 170000, 0, 0),
    ("Churro Cart", "2026-10-18"): (30000, 30000, 170000, 0, 0),
    ("Churro Cart", "2026-10-20"): (-1500, -1500, 0, 0, 8500),
}
TACO_10 = {"2026-10-16": (60000, 540000, 6000), "2026-10-17": (50000, 450000, 2000), "2026-10-18": (40000, 360000, 1500)}
TACO_15 = {"2026-10-16": (90000, 510000, 6000), "2026-10-17": (75000, 425000, 2000), "2026-10-18": (60000, 340000, 1500)}

# Ending account balances (debit positive), hand-derived in HAND_WORKED_EXAMPLES.md section 8.
ENDING_S1 = {"1000": 893298, "1050": 0, "1100": 0, "1200": 23500, "2000": 0, "2100": -317701,
             "2200": 0, "4000": -668500, "5100": 69403}


class OracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = oracle.run(FIXTURE)

    def s(self, set_id, vendor, d):
        hits = [x for x in self.out[set_id]["settlements"] if x["vendor_name"] == vendor and x["business_date"] == d]
        self.assertEqual(len(hits), 1, (vendor, d))
        return hits[0]

    def test_hand_worked_settlements_match(self):
        for set_id in ("S1_reusable_10pct", "S2_one_time_10pct", "S3_reusable_contract_15pct", "S0_no_decision"):
            for (v, d), (a, c, p, t, u) in HAND.items():
                s = self.s(set_id, v, d)
                got = tuple(int(s[k]) for k in ("assessed_fee_cents", "collected_fee_cents", "payout_cents",
                                                "tips_passthrough_cents", "unpaid_balance_cents"))
                self.assertEqual(got, (a, c, p, t, u), (set_id, v, d))

    def test_taco_conditional_on_human_decision(self):
        for set_id, table in (("S1_reusable_10pct", TACO_10), ("S2_one_time_10pct", TACO_10),
                              ("S3_reusable_contract_15pct", TACO_15)):
            for d, (a, p, t) in table.items():
                s = self.s(set_id, "Taco Libre", d)
                self.assertEqual((int(s["assessed_fee_cents"]), int(s["payout_cents"]), int(s["tips_passthrough_cents"])),
                                 (a, p, t), (set_id, d))
        for d in TACO_10:
            s = self.s("S0_no_decision", "Taco Libre", d)
            self.assertEqual(s["status"], "HELD")
            self.assertIsNone(s["assessed_fee_cents"])
            self.assertEqual(s["payout_cents"], "0")

    def test_policy_reuse_vs_one_time(self):
        s1 = self.s("S1_reusable_10pct", "Taco Libre", "2026-10-17")
        self.assertEqual(s1["terms_basis"], "APPROVED_REUSABLE_POLICY")
        self.assertTrue(any(r.get("role") == "APPLIED_POLICY" for r in s1["policy_refs"]))
        s2 = self.s("S2_one_time_10pct", "Taco Libre", "2026-10-17")
        self.assertEqual(s2["terms_basis"], "HUMAN_DECISION_ONE_TIME")

    def reviews(self, set_id):
        return sorted((r["vendor_name"], r["business_date"], r["reason_code"]) for r in self.out[set_id]["required_reviews"])

    def test_required_reviews(self):
        common = [("Churro Cart", "2026-10-20", "LATE_REFUND_RECOVERY"),
                  ("Grill Masters", "2026-10-18", "UNPAID_BALANCE"),
                  ("Late Bloom Lemonade", "2026-10-18", "UNPAID_BALANCE")]
        n1 = [("Taco Libre", "2026-10-16", "CONFLICTING_TERMS"), ("Taco Libre", "2026-10-16", "PAYOUT_ABOVE_AUTO_LIMIT")]
        self.assertEqual(self.reviews("S1_reusable_10pct"), sorted(common + n1))
        self.assertEqual(self.reviews("S3_reusable_contract_15pct"), sorted(common + n1))
        self.assertEqual(self.reviews("S2_one_time_10pct"), sorted(common + n1 + [
            ("Taco Libre", "2026-10-17", "CONFLICTING_TERMS"), ("Taco Libre", "2026-10-18", "CONFLICTING_TERMS")]))
        self.assertEqual(self.reviews("S0_no_decision"), sorted(common + [
            ("Taco Libre", d, "CONFLICTING_TERMS") for d in ("2026-10-16", "2026-10-17", "2026-10-18")]))
        conflict = [r for r in self.out["S0_no_decision"]["required_reviews"] if r["reason_code"] == "CONFLICTING_TERMS"][0]
        self.assertIsNone(conflict["scripted_human_decision"], "conflict must not carry a fabricated answer")
        self.assertEqual(conflict["financial_impact"]["cumulative_fee_by_candidate_rate_cents"],
                         {"1000bps": "60000", "1500bps": "90000"})

    def test_conflict_is_derived_from_documents(self):
        t = self.out["_common"]["derived_terms"]
        self.assertTrue(t["Taco Libre"]["conflict"])
        self.assertEqual(t["Taco Libre"]["contract_bps"], 1500)
        self.assertEqual([c["bps"] for c in t["Taco Libre"]["other_rate_claims"]], [1000])
        self.assertEqual([n for n, v in t.items() if v["conflict"]], ["Taco Libre"])
        self.assertEqual([n for n, v in t.items() if v["processor_fees_deductible"]], ["Sunrise Smoothies"])

    def test_all_journals_balance_and_ending_balances(self):
        for set_id, res in self.out.items():
            if set_id == "_common":
                continue
            for e in res["journal_entries"]:
                dr = sum(int(l["debit_cents"]) for l in e["lines"])
                cr = sum(int(l["credit_cents"]) for l in e["lines"])
                self.assertEqual(dr, cr, e["entry_key"])
                for l in e["lines"]:
                    self.assertGreaterEqual(int(l["debit_cents"]), 0)
                    self.assertGreaterEqual(int(l["credit_cents"]), 0)
            self.assertEqual(sum(int(v) for v in res["ending_balances"].values()), 0)
        self.assertEqual({k: int(v) for k, v in self.out["S1_reusable_10pct"]["ending_balances"].items()}, ENDING_S1)
        s3 = {k: int(v) for k, v in self.out["S3_reusable_contract_15pct"]["ending_balances"].items()}
        self.assertEqual(s3["4000"], -743500)
        self.assertEqual(s3["1000"], 968298)
        s0 = {k: int(v) for k, v in self.out["S0_no_decision"]["ending_balances"].items()}
        self.assertEqual(s0["2000"], -1500000)  # Taco Libre's central eligible still owed, held
        self.assertEqual(s0["2200"], -9500)     # its tips protected, not released

    def test_late_refund_entries_link_not_rewrite(self):
        ents = {e["entry_key"]: e for e in self.out["S1_reusable_10pct"]["journal_entries"]}
        refund = [e for e in ents.values() if e["kind"] == "REFUND_CENTRAL" and e["business_date"] == "2026-10-20"][0]
        self.assertIn(refund["correction_of"], ents)
        lines = {(l["account"], int(l["debit_cents"]), int(l["credit_cents"])) for l in refund["lines"]}
        self.assertEqual(lines, {("2000", 10000, 0), ("2100", 900, 0), ("1100", 0, 10900)})
        adj = ents["COMMISSION:Churro Cart:2026-10-20"]
        self.assertEqual(adj["correction_of"], "COMMISSION:Churro Cart:2026-10-18")
        lines = {(l["account"], int(l["debit_cents"]), int(l["credit_cents"])) for l in adj["lines"]}
        self.assertEqual(lines, {("4000", 1500, 0), ("1200", 8500, 0), ("2000", 0, 10000)})
        orig = ents["COMMISSION:Churro Cart:2026-10-18"]
        self.assertEqual({(l["account"], l["credit_cents"]) for l in orig["lines"] if l["credit_cents"] != "0"},
                         {("4000", "30000")})

    def test_processor_deposit_fixture6(self):
        ents = {e["entry_key"]: e for e in self.out["S1_reusable_10pct"]["journal_entries"]}
        e = ents["DEPOSIT:CARDCO-PO-0002"]
        lines = {(l["account"], int(l["debit_cents"]), int(l["credit_cents"])) for l in e["lines"]}
        self.assertEqual(lines, {("1000", 2295483, 0), ("5100", 59018, 0), ("1100", 0, 2354501)})
        recon = self.out["_common"]["reconciliation"]
        grp = [g for g in recon["match_groups"] if g.get("bank_provider_reference") == "CARDCO-PO-0002"][0]
        self.assertEqual(grp["member_batch_dates"], ["2026-10-17", "2026-10-18"])
        self.assertEqual((grp["status"], grp["variance_cents"], grp["reason_code"]), ("MATCHED", "0", "GROUPED_DEPOSIT_TIMING"))
        pending = [x for x in recon["deposit_state_at_statement_time"]
                   if x["statement_for"] == "2026-10-17" and x["batch_business_date"] == "2026-10-17"]
        self.assertEqual(pending[0]["state"], "TIMING_PENDING_NOT_AVAILABLE_CASH")
        self.assertTrue(all(g["status"] == "MATCHED" for g in recon["match_groups"]))
        self.assertTrue(all(c["status"] == "MATCHED" for c in recon["cash_count_checks"]))

    def test_cash_availability_excludes_delayed_deposit(self):
        ca = self.out["S1_reusable_10pct"]["cash_availability"]
        self.assertEqual([(c["available_before_cents"], c["requested_cents"]) for c in ca],
                         [("3980174", "1145904"), ("2834270", "931240"), ("4198513", "805215")])
        self.assertTrue(all(c["sufficient"] for c in ca))

    def test_invariants(self):
        for set_id, res in self.out.items():
            if set_id == "_common":
                continue
            for s in res["settlements"]:
                self.assertGreaterEqual(int(s["payout_cents"]), 0)
                li = {x["code"]: int(x["amount_cents"]) for x in s["line_items"]}
                if s["assessed_fee_cents"] is None:  # held on unresolved terms: tips stay protected, unreleased
                    self.assertEqual(s["tips_passthrough_cents"], "0")
                    self.assertIn("TIPS_COLLECTED_CENTRAL_PROTECTED", li)
                else:  # tips pass through in full, never reduced by fees or unpaid balance
                    self.assertEqual(int(s["tips_passthrough_cents"]), li["TIPS_PASSTHROUGH"])
                    self.assertLessEqual(int(s["payout_cents"]),
                                         max(0, li["ELIGIBLE_SALES_CENTRAL"] - li["PROCESSOR_FEES_DEDUCTED"]))
            # minimum applied once per festival, never per night
            by_v = {}
            for s in res["settlements"]:
                if s["assessed_fee_cents"] is not None and s["business_date"] <= "2026-10-18":
                    by_v.setdefault(s["vendor_name"], 0)
                    by_v[s["vendor_name"]] += int(s["assessed_fee_cents"])
            self.assertEqual(by_v["Kettle Corn Co"], 50000)
            self.assertEqual(by_v["Late Bloom Lemonade"], 50000)
            self.assertEqual(by_v["Sunrise Smoothies"], 150000)
            self.assertEqual(by_v["Grill Masters"], 180000)
        self.assertEqual(self.out["_common"]["input_checks"]["tax_mismatches"], [])

    def test_committed_expected_outputs_are_current(self):
        for k, v in self.out.items():
            name = "common.json" if k == "_common" else f"{k}.json"
            self.assertEqual((EXPECTED / name).read_bytes(), oracle.dump(v), f"stale expected output {name}")

    def test_oracle_does_not_import_generator_or_production_code(self):
        tree = ast.parse((HERE / "oracle.py").read_text())
        mods = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom):
                mods.add((n.module or "").split(".")[0])
        allowed = {"__future__", "argparse", "hashlib", "json", "re", "sys", "collections", "datetime",
                   "decimal", "pathlib", "zoneinfo"}
        self.assertLessEqual(mods, allowed, mods - allowed)

    def test_oracle_separation_from_runtime_fixture(self):
        gen_root = REPO / "data" / "generator"
        for p in gen_root.rglob("*"):
            if p.is_file():
                self.assertNotRegex(p.name.lower(), r"oracle|expected|answer|scripted_decision", p)
        for p in FIXTURE.rglob("*.json"):
            text = p.read_text()
            for marker in ("scripted_human_decision", "assessed_fee_cents", "oracle-0", "S1_reusable"):
                self.assertNotIn(marker, text, p)
        # expected outputs exist only under tests/oracle
        self.assertTrue((EXPECTED / "S1_reusable_10pct.json").exists())


if __name__ == "__main__":
    unittest.main()
