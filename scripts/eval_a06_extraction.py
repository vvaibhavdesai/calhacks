#!/usr/bin/env python3
"""EVALUATOR-ONLY: grade A06's verified document claims against lane B's
independent oracle (`tests/oracle/generated/<fixture>/common.json`
-> `derived_terms`).

This script reads the answer key to SCORE lane A's output; nothing it reads
is ever written back into lane A's database or used at runtime.

Compared per vendor:
  - contract commission rate, minimum fee and processing-fee deduction
    (from verified SIGNED_AGREEMENT claims on the vendor's CONTRACT);
  - amendment rate / fee-deduction changes (verified claims on AMENDMENT docs);
  - correspondence rate claims (the oracle's `claims`, which include
    restatements; conflict detection is a separate step);
  - minimum-period claims (`minimum_period_claims`).

Usage:
  DATABASE_URL=.../encore_score uv run python scripts/eval_a06_extraction.py \\
      --oracle tests/oracle/generated/full_v1/common.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app.db import get_session_factory, make_engine  # noqa: E402
from app.models_agents import DocumentClaim  # noqa: E402
from app.models_ledger import SourceDocument  # noqa: E402
from sqlalchemy import select  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--oracle", default="tests/oracle/generated/full_v1/common.json")
    a = ap.parse_args()
    truth = json.load(open(a.oracle))["derived_terms"]

    make_engine()
    db = get_session_factory()()
    got: dict[str, dict] = defaultdict(
        lambda: {
            "contract": {},
            "amend_bps": set(),
            "amend_fees": set(),
            "email_bps": set(),
            "email_restate_bps": set(),
            "min_period": set(),
        }
    )
    rows = db.execute(
        select(DocumentClaim, SourceDocument.document_kind)
        .join(SourceDocument, SourceDocument.id == DocumentClaim.source_document_id)
        .where(DocumentClaim.verified.is_(True))
    ).all()
    for c, kind in rows:
        g = got[c.vendor_id]
        if kind == "CONTRACT" and c.speaker_role == "SIGNED_AGREEMENT":
            if c.commission_bps is not None:
                g["contract"].setdefault("bps", set()).add(c.commission_bps)
            if c.minimum_fee_cents is not None:
                g["contract"].setdefault("min", set()).add(c.minimum_fee_cents)
            if c.processing_fee_deductible is not None:
                g["contract"].setdefault("fees", set()).add(c.processing_fee_deductible)
        elif kind == "AMENDMENT":
            if c.commission_bps is not None:
                g["amend_bps"].add(c.commission_bps)
            if c.processing_fee_deductible is not None:
                g["amend_fees"].add(c.processing_fee_deductible)
        elif kind == "EMAIL":
            if c.commission_bps is not None:
                (g["email_restate_bps"] if c.restates_existing_terms else g["email_bps"]).add(
                    c.commission_bps
                )
            if c.minimum_period == "NIGHTLY":
                g["min_period"].add("NIGHTLY")

    checks = defaultdict(lambda: [0, 0])
    misses = []

    def score(name, ok, detail):
        checks[name][0] += int(ok)
        checks[name][1] += 1
        if not ok:
            misses.append((name, detail))

    for vid, t in truth.items():
        g = got.get(
            vid,
            {
                "contract": {},
                "amend_bps": set(),
                "amend_fees": set(),
                "email_bps": set(),
                "email_restate_bps": set(),
                "min_period": set(),
            },
        )
        c = g["contract"]
        score(
            "contract_rate",
            c.get("bps") == {t["contract_bps"]},
            (t["vendor_name"], t["contract_bps"], c.get("bps")),
        )
        score(
            "contract_minimum",
            c.get("min") == {t["minimum_cents"]},
            (t["vendor_name"], t["minimum_cents"], c.get("min")),
        )
        score(
            "contract_fee_deduction",
            c.get("fees") == {t["processor_fees_deductible"]},
            (t["vendor_name"], t["processor_fees_deductible"], c.get("fees")),
        )
        exp_amend_bps = {x["bps"] for x in t["amendments"] if x.get("bps") is not None}
        exp_amend_fees = {
            x["fees_deductible"] for x in t["amendments"] if x.get("fees_deductible") is not None
        }
        score(
            "amendments",
            g["amend_bps"] == exp_amend_bps and g["amend_fees"] == exp_amend_fees,
            (t["vendor_name"], exp_amend_bps, exp_amend_fees, g["amend_bps"], g["amend_fees"]),
        )
        # The oracle lists every correspondence rate claim, restatements
        # included; deciding which ones conflict is a later, separate step.
        exp_claims = {x["bps"] for x in t["claims"]}
        got_claims = g["email_bps"] | g["email_restate_bps"]
        score(
            "email_rate_claims",
            got_claims == exp_claims,
            (t["vendor_name"], exp_claims, got_claims),
        )
        exp_minp = {"NIGHTLY"} if t["minimum_period_claims"] else set()
        score(
            "minimum_period_claims",
            g["min_period"] == exp_minp,
            (t["vendor_name"], exp_minp, g["min_period"]),
        )

    total_ok = sum(v[0] for v in checks.values())
    total = sum(v[1] for v in checks.values())
    print(f"A06 extraction vs oracle derived_terms: {total_ok}/{total} checks")
    for k, (ok, n) in checks.items():
        print(f"  {k:28s} {ok}/{n}")
    for name, detail in misses[:30]:
        print("  MISS", name, detail)
    return 0 if total_ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
