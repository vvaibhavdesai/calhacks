"""Full-dataset generator tests (stdlib unittest). Run from repo root:

    python3 -m unittest discover -s data/generator/tests -v

Generates full_v1 twice plus the train and held-out splits into temporary
directories (about 2 s each). Checks determinism, the committed manifests,
exact counts, cross-record references, SourceRefs, money/time formats and that
runtime-facing files carry no oracle answers or scenario labels.
"""
import hashlib
import importlib.util
import json
import re
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path

GEN_DIR = Path(__file__).resolve().parents[1]
REPO = GEN_DIR.parents[1]
CONFIGS = {k: GEN_DIR / "config" / f"{k}.json" for k in ("full_v1", "full_v1_train", "full_v1_heldout")}
COMMITTED = GEN_DIR / "manifests"

spec = importlib.util.spec_from_file_location("encore_generate_full", GEN_DIR / "generate_full.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

MONEY_RE = re.compile(r"^-?(0|[1-9][0-9]*)$")
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
ANSWER_KEYS = {"assessed_fee_cents", "collected_fee_cents", "payout_cents", "unpaid_balance_cents", "expected",
               "journal_entries", "required_reviews", "settlements", "match_groups", "commission_rate_resolved",
               "scripted_human_decision", "expected_control_behavior", "scenario_id", "scenarios", "outstanding_cents",
               "final_state", "contract_terms_truth", "tier"}
# Scenario role / label codes the generator knows; none may appear in runtime-facing files.
LABEL_RE = re.compile(r"SCN-|GENUINE_DUPLICATE_CHARGE|DUPLICATE_IMPORT|CASH_VARIANCE|MISSING_CASH_COUNT|INSUFFICIENT_CASH|"
                      r"TERMS_CONFLICT|WRONG_MINIMUM|EXPIRED_PRIOR_POLICY|TAX_OVERCOLLECTED|TAX_UNDERCOLLECTED|"
                      r"PAYOUT_FAILED|PAYOUT_UNKNOWN|scenario|oracle|evaluator", re.I)
ALCOHOL_RE = re.compile(r"\b(beer|wine|ale|lager|cocktail|margarita|sangria|vodka|whiskey|tequila|seltzer|cider|mimosa)\b", re.I)


def load(root, name):
    return json.loads((root / "api" / f"{name}.json").read_text())


def items(root, name):
    return load(root, name)["items"]


def walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield path + "/" + k, k, v
            yield from walk(v, path + "/" + k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, f"{path}[{i}]")


class FullGeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        t = Path(cls.tmp.name)
        cls.timings = {}
        cls.roots, cls.files = {}, {}
        for name in ("full_v1", "full_v1_train", "full_v1_heldout"):
            t0 = time.perf_counter()
            cls.files[name], _ = gen.generate(CONFIGS[name], t / name, t / "evaluator" / name)
            cls.timings[name] = time.perf_counter() - t0
            cls.roots[name] = t / name
        t0 = time.perf_counter()
        cls.files_again, _ = gen.generate(CONFIGS["full_v1"], t / "again", t / "evaluator" / "again")
        cls.timings["full_v1_again"] = time.perf_counter() - t0
        cls.a = cls.roots["full_v1"]
        cls.evaluator = t / "evaluator"
        print("\n[generation seconds] " + ", ".join(f"{k}={v:.1f}" for k, v in cls.timings.items()))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # -------------------------------------------------- reproducibility
    def test_regeneration_byte_identical(self):
        self.assertEqual(sorted(self.files["full_v1"]), sorted(self.files_again))
        for p, b in self.files["full_v1"].items():
            self.assertEqual(b, self.files_again[p], p)

    def test_committed_manifests_match_regeneration(self):
        for name, files in self.files.items():
            committed = (COMMITTED / f"{name}.manifest.json").read_bytes()
            self.assertEqual(committed, files["manifest.json"],
                             f"{name}: regenerated manifest differs from data/generator/manifests/{name}.manifest.json")

    def test_manifest_lists_every_file_with_hash(self):
        for name, root in self.roots.items():
            m = json.loads((root / "manifest.json").read_text())
            listed = {f["path"]: f for f in m["files"]}
            on_disk = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()} - {"manifest.json"}
            self.assertEqual(set(listed), on_disk, name)
            for p, f in listed.items():
                data = (root / p).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), f["sha256"], p)
                self.assertEqual(len(data), f["bytes"], p)
            self.assertEqual(m["seed"], json.loads(CONFIGS[name].read_text())["seed"])
            self.assertIn("scenario_manifest_sha256", m)
            self.assertNotIn("scenarios", m)

    def test_splits_differ(self):
        ids = {n: [v["id"] for v in items(r, "vendors")] for n, r in self.roots.items()}
        self.assertEqual(len({tuple(v) for v in ids.values()}), 3)
        sm = {n: json.loads((self.evaluator / n / "scenario_manifest.json").read_text()) for n in self.roots}
        roles = {n: {r: x["vendor_name"] for r, x in s["roles"].items()} for n, s in sm.items()}
        self.assertNotEqual(roles["full_v1"], roles["full_v1_heldout"])
        self.assertNotEqual(roles["full_v1_train"], roles["full_v1_heldout"])
        self.assertEqual(roles["full_v1"]["TERMS_CONFLICT_AND_LATE_AMENDMENT"], "Taco Libre")
        self.assertEqual({s["split"] for s in sm.values()}, {"validation", "train", "heldout"})

    # -------------------------------------------------- exact counts
    def test_exact_counts(self):
        for name, root in self.roots.items():
            m = json.loads((root / "manifest.json").read_text())
            c = m["expected_record_counts"]
            orders = items(root, "orders")
            self.assertEqual(len(items(root, "vendors")), 80, name)
            self.assertEqual(sum(1 for o in orders if o["kind"] == "SALE"), 40000, name)
            self.assertEqual(c["sales_orders"], 40000)
            for api_name in ("vendors", "orders", "payments", "processor_batches", "bank_entries", "cash_counts", "source_documents",
                             "menu_items", "supplier_invoices", "purchase_orders", "ticket_settlements", "prior_settlements"):
                self.assertEqual(c[api_name], len(items(root, api_name)), (name, api_name))
            self.assertEqual(c["order_lines"], sum(len(o["lines"]) for o in orders))
            self.assertEqual(sorted({o["business_date"] for o in orders if o["kind"] == "SALE"}),
                             ["2026-10-16", "2026-10-17", "2026-10-18"])
            self.assertGreaterEqual(c["pdf_documents"], 80 + 3 + 10)
            self.assertEqual(json.loads((root / "api" / "festival.json").read_text())["timezone"], "America/Los_Angeles")

    def test_uneven_sales_and_no_alcohol(self):
        per_vendor = Counter(o["vendor_id"] for o in items(self.a, "orders") if o["kind"] == "SALE")
        counts = sorted(per_vendor.values())
        self.assertEqual(len(counts), 80)
        self.assertGreater(counts[-1], 8 * counts[0], "sales should be uneven across vendors")
        for m in items(self.a, "menu_items"):
            self.assertNotRegex(m["description"], ALCOHOL_RE)
        methods = Counter(p["method"] for p in items(self.a, "payments"))
        self.assertEqual(set(methods), {"CARD", "WRISTBAND", "CASH"})

    def test_six_weeks_of_activity(self):
        bank = items(self.a, "bank_entries")
        days = sorted(e["occurred_at"][:10] for e in bank)
        self.assertLessEqual(days[0], "2026-09-23")
        self.assertGreaterEqual(days[-1], "2026-10-29")
        kinds = {d["kind"] for d in items(self.a, "source_documents")}
        for k in ("CONTRACT", "CONTRACT_AMENDMENT", "EMAIL_THREAD", "SUPPLIER_INVOICE", "SUPPLIER_CREDIT_NOTE", "SPONSORSHIP_INVOICE",
                  "POS_EXPORT", "PROCESSOR_REPORT", "BANK_STATEMENT", "CASH_COUNT", "CASH_DEPOSIT_SLIP", "TICKET_SETTLEMENT_REPORT",
                  "HISTORY_EXPORT", "MENU_CATALOG", "PURCHASE_ORDER_EXPORT", "AP_PAYMENT_REGISTER", "EXPENSE_REGISTER"):
            self.assertIn(k, kinds)

    # -------------------------------------------------- references
    def test_cross_record_references_resolve(self):
        r = self.a
        vendors = {v["id"] for v in items(r, "vendors")}
        orders = {o["id"]: o for o in items(r, "orders")}
        lines = {ln["line_id"] for o in orders.values() for ln in o["lines"]}
        pays = {p["id"]: p for p in items(r, "payments")}
        batches = {b["id"]: b for b in items(r, "processor_batches")}
        bank = {b["id"]: b for b in items(r, "bank_entries")}
        docs = {d["id"] for d in items(r, "source_documents")}
        counts = {c["id"] for c in items(r, "cash_counts")}
        invs = {i["id"] for i in items(r, "supplier_invoices")}
        pos = {p["id"] for p in items(r, "purchase_orders")}
        sups = {s["id"] for s in items(r, "suppliers")}
        for o in orders.values():
            self.assertIn(o["vendor_id"], vendors)
            if o["kind"] == "REFUND":
                self.assertIn(o["refund_of_order_id"], orders)
                for ln in o["lines"]:
                    self.assertIn(ln["refund_of_line_id"], lines)
        for p in pays.values():
            self.assertIn(p["vendor_id"], vendors)
            self.assertIn(p["order_refs"][0]["order_id"], orders)
            for k in ("refund_of_payment_id", "chargeback_of_payment_id"):
                if k in p:
                    self.assertIn(p[k], pays)
            if p["processor_batch_id"]:
                self.assertIn(p["processor_batch_id"], batches)
        for b in batches.values():
            for pid in b["payment_ids"]:
                self.assertIn(pid, pays)
            self.assertEqual(sum(int(pays[i]["amount_cents"]) for i in b["payment_ids"]), int(b["gross_cents"]))
        refs_by_payout = Counter()
        for b in batches.values():
            refs_by_payout[b["payout_reference"]] += int(b["net_cents"])
        bank_by_ref = {b["provider_reference"]: b for b in bank.values()}
        for ref, net in refs_by_payout.items():
            be = bank_by_ref[ref]
            self.assertEqual(int(be["amount_cents"]) * (1 if be["direction"] == "credit" else -1), net, ref)
        for name in ("cash_deposit_slips", "ticket_settlements", "sponsorship_receipts", "supplier_payments", "expenses"):
            for it in items(r, name):
                self.assertIn(it["bank_entry_id"], bank, name)
        for s in items(r, "cash_deposit_slips"):
            self.assertEqual(sum(int(b["amount_cents"]) for b in s["bags"]), int(bank[s["bank_entry_id"]]["amount_cents"]))
            for b in s["bags"]:
                if b["cash_count_id"]:
                    self.assertIn(b["cash_count_id"], counts)
        for i in items(r, "supplier_invoices"):
            self.assertIn(i["supplier_id"], sups)
            for p in i["po_refs"]:
                self.assertIn(p, pos)
            if i["kind"] == "CREDIT_NOTE":
                self.assertIn(i["applies_to_invoice_id"], invs)
        for sp in items(r, "supplier_payments"):
            for iid in sp["invoice_ids"]:
                self.assertIn(iid, invs)
        for d in items(r, "source_documents"):
            if d["amends_source_id"]:
                self.assertIn(d["amends_source_id"], docs)
        for v in items(r, "vendors"):
            self.assertIn(v["active_contract_version"]["source_id"], docs)
        # bank running balance in the statement equals opening + entries
        stmt = (r / "sources/bank/operating_account_statement.csv").read_text().splitlines()
        closing = int(stmt[-1].split(",")[-1].replace(".", ""))
        opening = int(items(r, "opening_balances")[0]["amount_cents"])
        total = sum(int(b["amount_cents"]) * (1 if b["direction"] == "credit" else -1) for b in bank.values())
        self.assertEqual(closing, opening + total)

    def test_source_refs_resolve_to_bytes_rows_pages_spans(self):
        r = self.a
        docs = {d["id"]: d for d in items(r, "source_documents")}
        rows_cache = {}
        n = 0
        for f in sorted((r / "api").glob("*.json")):
            for path, key, val in walk(json.loads(f.read_text())):
                if key != "source_refs":
                    continue
                self.assertTrue(val, f"{f.name}{path}")
                for ref in val:
                    n += 1
                    d = docs[ref["source_id"]]
                    self.assertEqual(ref["content_hash"], d["content_hash"])
                    self.assertEqual(ref["source_version"], d["version"])
                    if ref["locator_kind"] == "row":
                        if d["id"] not in rows_cache:
                            rows_cache[d["id"]] = (r / d["path"]).read_text().splitlines()
                        m = re.fullmatch(r"row=(\d+)(?:-(\d+))?", ref["locator"])
                        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
                        self.assertTrue(1 <= a <= b < len(rows_cache[d["id"]]), ref)
                    elif ref["locator_kind"] == "page":
                        p = int(ref["locator"].split("=")[1])
                        self.assertTrue(1 <= p <= d["page_count"])
                    else:
                        a, b = (int(x) for x in ref["locator"].split("=")[1].split("-"))
                        text = (r / d["path"]).read_text()
                        self.assertEqual(text[a:b], ref["quoted_excerpt"])
        self.assertGreater(n, 100000)
        for d in docs.values():
            data = (r / d["path"]).read_bytes()
            self.assertEqual("sha256:" + hashlib.sha256(data).hexdigest(), d["content_hash"], d["path"])
            self.assertTrue(UTC_RE.match(d["received_at"]))
            if d["media_type"] == "application/pdf":
                self.assertTrue(data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF"))

    def test_money_and_time_formats(self):
        n = 0
        for f in sorted((self.a / "api").glob("*.json")):
            for path, key, val in walk(json.loads(f.read_text())):
                if key.endswith("_cents") and val is not None:
                    n += 1
                    self.assertIsInstance(val, str, f"{f.name}{path}")
                    self.assertRegex(val, MONEY_RE)
                    self.assertNotEqual(val, "-0")
                if key in ("ordered_at", "occurred_at", "recorded_at", "counted_at", "received_at", "paid_at"):
                    self.assertRegex(val, UTC_RE, f"{f.name}{path}")
        self.assertGreater(n, 500000)
        for o in items(self.a, "orders"):
            self.assertEqual(int(o["gross_cents"]), sum(int(ln["gross_cents"]) for ln in o["lines"]))

    def test_ids_unique(self):
        seen = Counter()
        for f in (self.a / "api").glob("*.json"):
            for _, key, val in walk(json.loads(f.read_text())):
                if key in ("id", "line_id"):
                    seen[val] += 1
        dups = [k for k, c in seen.items() if c > 1]
        self.assertEqual(dups, [])

    # -------------------------------------------------- leakage
    def test_no_answers_or_labels_in_runtime_facing_files(self):
        for name, root in self.roots.items():
            for p in list((root / "api").rglob("*")) + list((root / "sources").rglob("*")) + list((root / "simulator_only").rglob("*")):
                if not p.is_file():
                    continue
                self.assertNotRegex(p.name.lower(), r"oracle|expected|answer|scenario|settlement_result|journal")
                if p.suffix == ".json":
                    for path, key, _ in walk(json.loads(p.read_text())):
                        self.assertNotIn(key, ANSWER_KEYS, f"{name}:{p}{path}")
                if p.suffix in (".json", ".csv", ".eml"):
                    text = p.read_text()
                    # draft assumptions, prior-year case history and the simulator note legitimately use these words
                    if p.name not in ("business_config.json", "prior_cases.json", "gcmf_2025_cases.json", "payout_outcome_script.json"):
                        self.assertIsNone(LABEL_RE.search(text), f"{name}:{p}: {LABEL_RE.search(text)}")
            self.assertFalse((root / "scenario_manifest.json").exists())
            self.assertFalse(any("generated" in str(p) for p in root.rglob("*")))

    def test_fictional_only(self):
        for p in (self.a / "sources" / "email").glob("*.eml"):
            text = p.read_text()
            for addr in re.findall(r"[\w.+-]+@([\w.-]+)", text):
                self.assertTrue(addr.endswith(".example"), (p.name, addr))
            self.assertNotRegex(text, r"\b\d{9,}\b")
        for d in items(self.a, "vendors"):
            self.assertEqual(d["fictional_notice"], "Fictional vendor")

    def test_runtime_reasonable(self):
        for k, v in self.timings.items():
            self.assertLess(v, 60, f"{k} took {v:.1f}s")


if __name__ == "__main__":
    unittest.main()
