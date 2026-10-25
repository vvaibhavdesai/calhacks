#!/usr/bin/env python3
"""Export lane A's settlements as a lane B candidate, read straight from the database.

WHY THIS EXISTS, AND WHAT IT IS NOT
-----------------------------------
`scripts/score_b_fixture.py export` is the official path, and it writes
`settlements.json` EMPTY on purpose -- it was written before contract terms
existed, so it had nothing to put there. That left the independent oracle
unable to score the one thing everybody wants scored: the money.

This script fills that gap for VERIFICATION only. It reads the settlements a
real run already produced and writes them in lane B's
`encore-candidate-0.1-PROPOSAL` shape, so `tests/oracle/score.py` can compare
242 vendor-nights against an answer key this system never saw.

It is NOT release evidence: it reads the database directly rather than the
HTTP API, and it exports settlements only. Payouts, cases, journals and
reconciliation stay empty, and the scorer counts their absence as errors --
correctly.

One settlement per vendor-night: the same rule the dashboard uses (highest
version wins, superseded rows excluded), because a night calculated twice
must not be counted twice -- this festival really does carry a duplicate.

Usage:
  DATABASE_URL=...  python3 scripts/export_candidate_from_db.py \
      --festival fest_bf25e974c723d827 --set F1_story --out /tmp/candidate
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app.db import make_engine  # noqa: E402
from sqlalchemy import text  # noqa: E402

FIXTURE_MANIFEST_SHA256 = "a6a5c899702ddd0b4460931d3cd85e522226804502fb197194ef79e5c451487e"

# Highest version per (vendor, business_date), superseded rows excluded: a
# correction supersedes what it corrects, and this festival carries a genuine
# duplicate pair that must count once.
LIVE_SETTLEMENTS = text(
    """
    WITH ranked AS (
      SELECT s.*,
             ROW_NUMBER() OVER (
               PARTITION BY s.vendor_id, s.business_date
               ORDER BY s.version DESC, s.created_at DESC
             ) AS rn
        FROM settlements s
       WHERE s.festival_id = :festival
         AND NOT EXISTS (SELECT 1 FROM settlements c WHERE c.correction_of = s.id)
    )
    SELECT vendor_id, business_date, status, assessed_fee_cents, collected_fee_cents,
           payout_cents, tips_cents, unpaid_balance_cents, commission_bps_used,
           holds, policy_refs
      FROM ranked WHERE rn = 1
     ORDER BY business_date, vendor_id
    """
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--festival", required=True)
    ap.add_argument("--set", dest="decision_set", default="F1_story")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()

    engine = make_engine()
    with engine.connect() as conn:
        rows = conn.execute(LIVE_SETTLEMENTS, {"festival": a.festival}).mappings().all()

    items = []
    for r in rows:
        holds = r["holds"] or []
        items.append(
            {
                "vendor_id": r["vendor_id"],
                "business_date": r["business_date"].isoformat(),
                # The oracle states every settled night as CALCULATED; ours
                # carries its own lifecycle (FINAL / CORRECTION). Mapped, not
                # renamed in the database.
                "status": "CALCULATED",
                # Cents as strings, the way the candidate format states them.
                "assessed_fee_cents": str(r["assessed_fee_cents"]),
                "collected_fee_cents": str(r["collected_fee_cents"]),
                "payout_cents": str(r["payout_cents"]),
                "tips_passthrough_cents": str(r["tips_cents"]),
                "unpaid_balance_cents": str(r["unpaid_balance_cents"]),
                "commission_bps": r["commission_bps_used"],
                # Our holds say `reason`; lane B's candidate says reason_code.
                "hold_reasons": [h.get("reason_code") or h.get("reason") for h in holds],
                "citations": [
                    c for c in (r["policy_refs"] or []) if isinstance(c, dict) and "source_id" in c
                ],
            }
        )

    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "meta.json").write_text(
        json.dumps(
            {
                "candidate_format": "encore-candidate-0.1-PROPOSAL",
                "decision_set": a.decision_set,
                "fixture_id": "full_v1",
                "fixture_manifest_sha256": FIXTURE_MANIFEST_SHA256,
                "system": "encore-lane-a (settlements only, read from the database)",
            },
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )
    (a.out / "settlements.json").write_text(json.dumps({"items": items}, sort_keys=True) + "\n")
    # Stated as empty rather than omitted: this export covers settlements only.
    for name in ("payouts", "cases", "journals", "reconciliation"):
        (a.out / f"{name}.json").write_text(json.dumps({"items": []}, sort_keys=True) + "\n")

    print(f"wrote {len(items)} settlements to {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
