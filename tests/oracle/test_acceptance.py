"""B07 acceptance harness tests (EVALUATOR-ONLY).

Needs the full dataset and its oracle output on this laptop:
    python3 data/generator/generate_full.py && python3 tests/oracle/oracle_full.py
Skipped (with that message) when they are missing. No lane A system is involved.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import acceptance as acc  # noqa: E402
import oracle_full as of  # noqa: E402
import score as sc  # noqa: E402

HAVE_DATA = (of.DEFAULT_FIXTURE / "manifest.json").exists() and (HERE / "generated" / "full_v1" / "common.json").exists()
SKIP = "full_v1 dataset/oracle output not built on this laptop (run generate_full.py and oracle_full.py)"


def run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = acc.main(argv)
    return code, buf.getvalue()


@unittest.skipUnless(HAVE_DATA, SKIP)
class AcceptanceHarnessTest(unittest.TestCase):
    def test_self_test_passes_and_is_labelled_not_release_evidence(self):
        with tempfile.TemporaryDirectory() as t:
            code, out = run(["self-test", "--out", t])
            self.assertEqual(code, 0, out)
            rep = json.loads((Path(t) / "report.json").read_text())
            self.assertEqual(rep["evidence_kind"], "harness-self-test")
            self.assertFalse(rep["release_evidence"])
            self.assertIn("NOT RELEASE EVIDENCE", rep["notice"])
            self.assertTrue(rep["scale_ok"])
            self.assertEqual(rep["scale"], {"vendors": 80, "sales_orders": 40000})
            self.assertNotIn("acceptance_passed", rep)
            f1 = rep["sets"]["F1_story"]
            self.assertTrue(f1["oracle_self_candidate"]["passed"])
            self.assertEqual(f1["oracle_self_candidate"]["scenarios"], "30/30")
            self.assertGreaterEqual(len(f1["perturbations"]), 10)
            self.assertTrue(all(p["detected"] for p in f1["perturbations"].values()), f1["perturbations"])

    def test_live_without_target_is_not_run(self):
        code, out = run(["live"])
        self.assertEqual(code, 2)
        self.assertIn("not run: no live target", out)

    def _oracle_candidate(self, root: Path, set_id="F1_story", system=None, areas=True):
        fx, exp_dir, common, manifest, *_ = acc.load_context(of.DEFAULT_FIXTURE)
        expected = json.loads((exp_dir / f"{set_id}.json").read_text())
        cand = sc.oracle_to_candidate(expected, common, set_id)
        if system:
            cand["meta"]["system"] = system
        sc.write_candidate(cand, root / set_id)
        if areas:
            for name, body in acc.oracle_area_files(expected, common).items():
                (root / set_id / f"{name}.json").write_text(json.dumps(body))

    def test_live_refuses_the_oracles_own_answer(self):
        with tempfile.TemporaryDirectory() as t:
            self._oracle_candidate(Path(t) / "cand")
            code, out = run(["live", "--sets", "F1_story", "--candidate-root", str(Path(t) / "cand"), "--out", str(Path(t) / "o"),
                             "--system-commit", "deadbeef"])
            self.assertEqual(code, 1)
            rep = json.loads((Path(t) / "o" / "report.json").read_text())
            self.assertFalse(rep["acceptance_passed"])
            self.assertIn("oracle's own answer", rep["sets"]["F1_story"]["error"])

    def test_live_missing_area_exports_and_commit_cannot_pass(self):
        # Stands in for an A export that copied the oracle's core answer (system renamed) but no area files.
        with tempfile.TemporaryDirectory() as t:
            self._oracle_candidate(Path(t) / "cand", system="stand-in for the harness test", areas=False)
            code, _ = run(["live", "--sets", "F1_story", "--candidate-root", str(Path(t) / "cand"), "--out", str(Path(t) / "o")])
            self.assertEqual(code, 1)
            rep = json.loads((Path(t) / "o" / "report.json").read_text())
            res = rep["sets"]["F1_story"]
            self.assertTrue(res["core"]["perfect"])
            self.assertEqual({a["status"] for a in res["areas"].values()}, {"NOT_EXPORTED"})
            self.assertFalse(res["passed"])
            self.assertFalse(rep["acceptance_passed"])
            self.assertIn("system commit not recorded", rep["acceptance_note"])

    def test_harness_imports_only_stdlib_and_evaluator_modules(self):
        import ast
        tree = ast.parse((HERE / "acceptance.py").read_text())
        mods = {n.module.split(".")[0] if isinstance(n, ast.ImportFrom) else a.name.split(".")[0]
                for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in (n.names if isinstance(n, ast.Import) else [None])}
        allowed = {"__future__", "argparse", "copy", "json", "os", "subprocess", "sys", "tempfile", "urllib", "collections",
                   "datetime", "pathlib", "oracle_full", "score"}
        self.assertLessEqual(mods, allowed)
        self.assertNotIn("apps/web", (HERE / "acceptance.py").read_text())


if __name__ == "__main__":
    unittest.main()
