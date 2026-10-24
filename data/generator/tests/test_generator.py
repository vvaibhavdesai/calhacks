"""Generator tests (stdlib unittest). Run from repo root:

    python3 -m unittest discover -s data/generator/tests -v
"""
import hashlib
import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path

GEN_DIR = Path(__file__).resolve().parents[1]
REPO = GEN_DIR.parents[1]
CONFIG = GEN_DIR / "config" / "compact_v1.json"
COMMITTED = GEN_DIR / "fixtures" / "compact_v1"

spec = importlib.util.spec_from_file_location("encore_generate", GEN_DIR / "generate.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

MONEY_RE = re.compile(r"^-?(0|[1-9][0-9]*)$")
ID_RE = re.compile(r"^[a-z]+_[0-9a-f]{16}$")
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# C03 "Record field minimums" (CONTRACTS.md v0.1 DRAFT) beyond identity/version.
C03_MIN = {
    "vendors": ["festival_id", "name", "booth_ids", "beneficiary_version", "active_contract_version"],
    "orders": ["source_native_id", "vendor_id", "business_date", "ordered_at", "lines", "source_refs"],
    "order_lines": ["line_id", "quantity", "eligible_net_cents", "tax_cents", "tips_cents", "gross_cents", "source_refs"],
    "payments": ["order_refs", "vendor_id", "method", "custody", "amount_cents", "currency", "status",
                 "provider_reference", "source_refs"],
    "bank_entries": ["account_id", "provider_reference", "amount_cents", "occurred_at", "direction", "source_refs"],
}
SOURCE_REF_KEYS = {"source_id", "source_version", "content_hash", "locator_kind", "locator"}
# Fields that would be answers, not inputs. They must never appear in runtime fixtures.
ANSWER_KEYS = {"assessed_fee_cents", "collected_fee_cents", "payout_cents", "unpaid_balance_cents",
               "expected", "journal_entries", "required_reviews", "settlements", "match_groups",
               "commission_rate_resolved", "scripted_human_decision"}


def load(root: Path, name: str):
    return json.loads((root / "api" / f"{name}.json").read_text())


def walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield path + "/" + k, k, v
            yield from walk(v, path + "/" + k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, f"{path}[{i}]")


class GeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.a = Path(cls.tmp.name) / "a"
        cls.b = Path(cls.tmp.name) / "b"
        cls.files_a = gen.generate(CONFIG, cls.a)
        cls.files_b = gen.generate(CONFIG, cls.b)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_determinism_two_runs_byte_identical(self):
        self.assertEqual(sorted(self.files_a), sorted(self.files_b))
        for p in self.files_a:
            self.assertEqual((self.a / p).read_bytes(), (self.b / p).read_bytes(), p)

    def test_committed_fixture_matches_regeneration(self):
        on_disk = sorted(str(p.relative_to(COMMITTED)) for p in COMMITTED.rglob("*") if p.is_file())
        self.assertEqual(on_disk, sorted(self.files_a))
        for p, data in self.files_a.items():
            self.assertEqual((COMMITTED / p).read_bytes(), data, f"committed fixture drift: {p}")

    def test_manifest_hashes_and_metadata(self):
        m = json.loads((self.a / "manifest.json").read_text())
        listed = {f["path"] for f in m["files"]}
        actual = {str(p.relative_to(self.a)) for p in self.a.rglob("*") if p.is_file()} - {"manifest.json"}
        self.assertEqual(listed, actual)
        for f in m["files"]:
            data = (self.a / f["path"]).read_bytes()
            self.assertEqual(hashlib.sha256(data).hexdigest(), f["sha256"], f["path"])
            self.assertEqual(len(data), f["bytes"])
        self.assertEqual(m["config_sha256"], hashlib.sha256(CONFIG.read_bytes()).hexdigest())
        self.assertEqual(m["seed"], json.loads(CONFIG.read_text())["seed"])
        for k in ("generator_version", "scenario_labels", "source_versions", "expected_record_counts"):
            self.assertIn(k, m)
        counts = m["expected_record_counts"]
        for name in ("vendors", "orders", "payments", "processor_batches", "bank_entries", "cash_counts",
                     "source_documents"):
            self.assertEqual(counts[name], len(load(self.a, name)["items"]), name)
        self.assertEqual(counts["order_lines"], sum(len(o["lines"]) for o in load(self.a, "orders")["items"]))

    def test_source_hashes_and_refs_resolve(self):
        docs = {d["id"]: d for d in load(self.a, "source_documents")["items"]}
        for d in docs.values():
            data = (self.a / d["path"]).read_bytes()
            self.assertEqual("sha256:" + hashlib.sha256(data).hexdigest(), d["content_hash"])
        n = 0
        for name in ("orders", "payments", "processor_batches", "bank_entries", "cash_counts"):
            for path, key, val in walk(load(self.a, name)):
                if key == "source_refs":
                    self.assertTrue(val, path)
                    for r in val:
                        n += 1
                        self.assertTrue(SOURCE_REF_KEYS <= set(r), path)
                        self.assertIn(r["source_id"], docs, path)
                        self.assertEqual(r["content_hash"], docs[r["source_id"]]["content_hash"])
                        self.assertIn(r["locator_kind"], ("row", "page", "span"))
        self.assertGreater(n, 50)

    def test_c03_field_minimums(self):
        for name in ("vendors", "orders", "payments", "bank_entries"):
            for item in load(self.a, name)["items"]:
                self.assertTrue(ID_RE.match(item["id"]), item["id"])
                self.assertIn("version", item)
                for f in C03_MIN[name]:
                    self.assertIn(f, item, f"{name}.{f}")
                if name == "orders":
                    for ln in item["lines"]:
                        for f in C03_MIN["order_lines"]:
                            self.assertIn(f, ln, f"line.{f}")
                if name == "payments":
                    self.assertIn(item["custody"], ("CENTRALLY_COLLECTED", "VENDOR_RETAINED"))
                    self.assertEqual(item["currency"], "USD")
                if name == "bank_entries":
                    self.assertIn(item["direction"], ("credit", "debit"))
                    self.assertFalse(item["amount_cents"].startswith("-"))

    def test_money_fields_canonical_integer_strings(self):
        n = 0
        for f in (self.a / "api").glob("*.json"):
            for path, key, val in walk(json.loads(f.read_text())):
                if key.endswith("_cents") and val is not None:
                    n += 1
                    self.assertIsInstance(val, str, f"{f.name}{path}")
                    self.assertRegex(val, MONEY_RE, f"{f.name}{path}")
                    self.assertNotEqual(val, "-0")
        self.assertGreater(n, 200)

    def test_time_and_ids(self):
        ids = []
        for f in (self.a / "api").glob("*.json"):
            for path, key, val in walk(json.loads(f.read_text())):
                if key in ("ordered_at", "occurred_at", "recorded_at", "counted_at"):
                    self.assertRegex(val, UTC_RE, path)
                if key == "business_date":
                    self.assertRegex(val, DATE_RE, path)
                if key in ("id", "line_id"):
                    ids.append(val)
        self.assertEqual(len(ids), len(set(ids)), "duplicate IDs")
        # UTC-midnight case: an order after 00:00Z still belongs to the prior Pacific day.
        late = [o for o in load(self.a, "orders")["items"] if o["ordered_at"] == "2026-10-17T06:30:00Z"]
        self.assertEqual(len(late), 1)
        self.assertEqual(late[0]["business_date"], "2026-10-16")

    def test_no_answers_in_runtime_fixture(self):
        for f in self.a.rglob("*.json"):
            if f.name == "manifest.json":
                continue
            for path, key, _ in walk(json.loads(f.read_text())):
                self.assertNotIn(key, ANSWER_KEYS, f"{f}{path}")
        for p in self.a.rglob("*"):
            self.assertNotRegex(p.name.lower(), r"oracle|expected|answer|settlement|journal")

    def test_custody_not_inferred_from_cash_word(self):
        pays = load(self.a, "payments")["items"]
        cash = {p["custody"] for p in pays if p["method"] == "CASH"}
        self.assertEqual(cash, {"CENTRALLY_COLLECTED", "VENDOR_RETAINED"})

    def test_other_seed_changes_ids_not_amounts(self):
        with tempfile.TemporaryDirectory() as t:
            gen.generate(CONFIG, Path(t), seed=7)
            o1 = load(self.a, "orders")["items"]
            o2 = load(Path(t), "orders")["items"]
            self.assertNotEqual([o["id"] for o in o1], [o["id"] for o in o2])
            self.assertEqual([o["gross_cents"] for o in o1], [o["gross_cents"] for o in o2])

    def test_money_helper_rejects_floats(self):
        with self.assertRaises(TypeError):
            gen.money(1.5)
        self.assertEqual(gen.money(-10900), "-10900")


if __name__ == "__main__":
    unittest.main()
