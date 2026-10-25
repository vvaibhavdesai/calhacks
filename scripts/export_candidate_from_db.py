#!/usr/bin/env python3
"""Export lane A's results as a lane B candidate, read straight from the database.

WHY THIS EXISTS, AND WHAT IT IS NOT
-----------------------------------
`scripts/score_b_fixture.py export` is the official path, and it writes
`settlements.json` EMPTY on purpose -- it was written before contract terms
existed, so it had nothing to put there. That left the independent oracle
unable to score the one thing everybody wants scored: the money.

This script fills that gap for VERIFICATION only. It reads what a real run
already produced and writes it in lane B's `encore-candidate-0.1-PROPOSAL`
shape, so `tests/oracle/score.py` can compare it against an answer key this
system never saw.

It is NOT release evidence: it reads the database directly rather than the
HTTP API.

What each collection carries, and what it deliberately does not:

* `settlements` -- one per vendor-night: the same rule the dashboard uses
  (highest version wins, superseded rows excluded), because a night
  calculated twice must not be counted twice. This festival really does
  carry a duplicate.

* `journals` -- every journal entry A08 posted, with its lines by account
  ROLE, which is what the scorer compares (entry balance, no negative
  amounts, ending balance per role).

* `reconciliation` -- lane A's current match groups, at the granularity
  lane B's `match_groups` use, with lane B's `match_key` spelling
  synthesised from the record each group is about. Lane A also keeps one
  `order_capture` group per order; lane B has no per-order groups at all,
  so only the duplicate-charge ones (the kind lane B does state) are
  carried and the other ~40,000 are left out as noise rather than
  comparison.

* `cases` -- the review questions lane A raised that are review questions
  in lane B's vocabulary: a human being asked to decide something about a
  vendor, its terms, its beneficiary, or a bank entry nothing explains.
  Lane A ALSO appends a `cases` row for every reconciliation group it
  cannot explain -- on the scored database, 159 bank entries (A03's
  bank-deposit pass does not know about the ticket settlements, sponsor
  wires, supplier payments and operating expenses A08 posts through its
  own ledger) and 696 superseded per-order refund classifications. Those
  are match-group diagnostics, not review questions; reporting them here
  would state 854 reviews against 57 expected. They are carried under
  `reconciliation`, which is where lane B compares them.

* `payouts` -- EMPTY, and honestly so: this database has no `payouts`
  rows at all. Lane B expects 242 (230 CONFIRMED), and producing them
  means driving A07's real obligation/approval/release/dispatch/confirm
  lifecycle with a funded cash pool, not writing rows. Until that runs,
  the scorer counts all 242 as errors -- correctly -- and with them the
  34 PAYOUT_ABOVE_AUTO_LIMIT reviews and the PAYOUT_FAILED and
  INSUFFICIENT_CASH reviews that only exist once money is moving.

Usage:
  DATABASE_URL=...  python3 scripts/export_candidate_from_db.py \
      --festival fest_bf25e974c723d827 --set F1_story --out /tmp/candidate
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC
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
           payout_cents, tips_cents, unpaid_balance_cents, vendor_fee_receivable_cents,
           commission_bps_used, holds, policy_refs
      FROM ranked WHERE rn = 1
     ORDER BY vendor_id, business_date
    """
)

# One payout per vendor-night, read against the settlement chain the same way
# LIVE_SETTLEMENTS picks the settlement: a payout against a superseded row is
# a dead obligation (`release_payout` refuses it and always will), so the
# vendor-night's payout is the one on its live settlement. `released_at` comes
# from the `payout.released` event, which is where lane A records when the
# money was actually let go; `dispatch_attempts` is how many times it was
# handed to the rail.
LIVE_PAYOUTS = text(
    """
    WITH ranked AS (
      SELECT s.*, ROW_NUMBER() OVER (
               PARTITION BY s.vendor_id, s.business_date
               ORDER BY s.version DESC, s.created_at DESC) AS rn
        FROM settlements s
       WHERE s.festival_id = :festival
         AND NOT EXISTS (SELECT 1 FROM settlements c WHERE c.correction_of = s.id)
    ),
    live AS (SELECT * FROM ranked WHERE rn = 1)
    SELECT p.id, p.vendor_id, p.business_date, p.state, p.amount_cents,
           p.idempotency_key, p.dispatch_attempts, p.payout_confirmed_at,
           live.tips_cents,
           (SELECT e.occurred_at FROM business_events e
             WHERE e.aggregate_id = p.id AND e.event_type = 'payout.released'
             ORDER BY e.occurred_at DESC LIMIT 1) AS released_at,
           (SELECT e.payload FROM business_events e
             WHERE e.aggregate_id = p.id AND e.event_type = 'payout.release_blocked'
             ORDER BY e.occurred_at DESC LIMIT 1) AS block_payload
      FROM payouts p
      JOIN live ON live.id = p.settlement_id
     ORDER BY p.business_date, p.vendor_id
    """
)

JOURNAL_ENTRIES = text(
    """
    SELECT e.id, e.entry_key, e.business_date, e.kind, e.correction_of
      FROM journal_entries e
     WHERE e.festival_id = :festival OR e.festival_id IS NULL
     ORDER BY e.business_date NULLS FIRST, e.entry_key NULLS FIRST, e.id
    """
)

JOURNAL_LINES = text(
    """
    SELECT l.journal_entry_id, l.account_role, l.account_code,
           l.debit_cents, l.credit_cents
      FROM journal_lines l
     ORDER BY l.journal_entry_id, l.id
    """
)

# The CURRENT classification of every record lane A reconciles: the newest
# group that contains it, the same "newest wins" rule app/settlement.py and
# app/orchestration/reconcile_day.py both read. Append-only history means
# every earlier generation is still in the table.
CURRENT_MATCH_GROUPS = text(
    """
    WITH ranked AS (
      SELECT m.member_kind, m.member_id, g.match_type, g.status, g.reason_code,
             g.variance_cents, g.case_id,
             ROW_NUMBER() OVER (
               PARTITION BY g.match_type, m.member_kind, m.member_id
               ORDER BY g.created_at DESC, g.id DESC
             ) AS rn
        FROM match_groups g
        JOIN match_group_members m ON m.match_group_id = g.id
       WHERE (g.match_type, m.member_kind) IN
             (('bank_deposit', 'bank_entry'), ('cash_count', 'cash_count'),
              ('order_capture', 'order'))
    )
    SELECT member_kind, member_id, match_type, status, reason_code,
           variance_cents, case_id
      FROM ranked WHERE rn = 1
    """
)

BANK_ENTRIES = text(
    "SELECT id, provider_reference, batch_gross_cents, business_date FROM bank_entries"
)

CASH_COUNTS = text("SELECT id, vendor_id, business_date FROM cash_counts")

# Lane A keys a source document by its own id and remembers lane B's id as
# the `source_native_id` it was imported under, so a citation lane A stored
# internally can be restated in lane B's spelling.
SOURCE_DOCUMENT_IDS = text("SELECT id, source_native_id FROM source_documents")

# The duplicate capture's own provider reference is how lane B keys the
# duplicate-charge group, so it is read from the order's payments.
DUPLICATE_CHARGE_REFS = text(
    """
    SELECT p.order_id, max(p.provider_reference) AS provider_reference
      FROM payments p
     WHERE p.kind = 'capture' AND p.order_id = ANY(:order_ids)
     GROUP BY p.order_id
    """
)

CASES = text(
    """
    SELECT c.id, c.reason_code, c.vendor_id, c.business_date, c.status,
           c.blocking, c.evidence_refs, c.auto_resolved, c.resolved_by
      FROM cases c
     ORDER BY c.created_at, c.id
    """
)

# A payout over the automatic-release limit is a review question even though
# lane A does not record it in `cases`: `release_payout` refuses it and
# `approve_payout` records the additional finance-lead decision it demanded,
# so a `payout_approvals` row with the finance_lead capability IS a human
# being asked whether this payout may go. One per vendor-night.
FINANCE_LEAD_ASKS = text(
    """
    SELECT DISTINCT p.vendor_id, p.business_date, p.amount_cents
      FROM payout_approvals a
      JOIN payouts p ON p.id = a.payout_id
     WHERE a.capability = 'finance_lead'
     ORDER BY p.business_date, p.vendor_id
    """
)

# Lane A's own reason codes, spelled the way lane B's review vocabulary
# spells the same question. Only questions a human is actually asked --
# see the module docstring on why the match-group diagnostics are not here.
#
# Lane A's two supplier-invoice reviews (DUPLICATE_SUPPLIER_INVOICE and
# INVOICE_EXCEEDS_PO) are deliberately NOT here even though it raises both
# and lane B expects both. Lane B states them against a `sinv_*` subject
# id, and on this schema a `cases` row carries no subject at all: the
# invoice appears only inside `evidence_refs` under lane A's own uuid. The
# question matches; the subject cannot be stated, so exporting them would
# report two reviews about nothing AND still leave the expected two
# unanswered. A `subject_id` column exists on later branches; once this
# database carries it, both belong here.
CASE_REASON_TO_ORACLE = {
    "vendor_contract_term_conflict": "CONFLICTING_TERMS",
    "beneficiary_change_unverified": "BENEFICIARY_CHANGED_UNVERIFIED",
    "minimum_period_claim": "MINIMUM_PERIOD_CLAIM",
    # Opened by app/payout_dispatch.py when the rail refuses a payment, and
    # by app/settlement.py when the festival ends with commission still
    # uncollected. Both vocabularies already match lane B's.
    "PAYOUT_FAILED": "PAYOUT_FAILED",
    "UNPAID_BALANCE": "UNPAID_BALANCE",
}

# Lane A's match-group statuses in lane B's vocabulary. "pending" has no
# lane B spelling (a deposit that has not settled yet is neither matched
# nor a case), so it is passed through under its own name rather than
# forced into one of lane B's three.
GROUP_STATUS_TO_ORACLE = {"matched": "MATCHED", "case": "CASE_REQUIRED"}


def _bank_match_key(provider_reference: str, is_processor_batch: bool) -> str:
    """Lane B's `match_key` for one bank entry.

    Lane A stores the statement line's `provider_reference` and whether a
    processor batch was allocated to it, which is exactly what lane B's
    three bank-side key kinds distinguish."""
    if is_processor_batch:
        return f"PROCESSOR_DEPOSIT:{provider_reference}"
    if provider_reference.startswith("CASHDEP"):
        return f"CASH_DEPOSIT:{provider_reference}"
    return f"BANK_UNMATCHED:{provider_reference}"


def _citations(refs, src_by_doc_id: dict[str, str]) -> list[dict]:
    """Lane A's stored source references in lane B's `SourceRef` spelling.

    Lane A writes `source_document_id` (its own primary key) and a bare
    sha256 hex; lane B's checker resolves `source_id` (the `src_*` id the
    document was imported under) and a `sha256:`-prefixed hash. Same
    reference, two spellings -- translated here rather than invented, and
    a reference that cannot be translated is dropped rather than guessed,
    so a citation this export states is one the oracle can resolve.
    """
    out = []
    for ref in refs or []:
        if not isinstance(ref, dict):
            continue
        nested = ref.get("source_refs")
        if nested:
            out.extend(_citations(nested, src_by_doc_id))
            continue
        source_id = ref.get("source_id") or src_by_doc_id.get(ref.get("source_document_id"))
        hash_ = ref.get("content_hash")
        if not source_id or not hash_ or not ref.get("locator_kind"):
            continue
        if not hash_.startswith("sha256:"):
            hash_ = f"sha256:{hash_}"
        out.append(
            {
                "source_id": source_id,
                "source_version": ref.get("source_version"),
                "content_hash": hash_,
                "locator_kind": ref["locator_kind"],
                "locator": ref.get("locator", ""),
                "quoted_excerpt": ref.get("quoted_excerpt"),
            }
        )
    return out


# Lane A's payout states in lane B's `final_state` vocabulary. Lane A's state
# machine is finer (DRAFT -> APPROVED -> DISPATCH_PENDING -> SUBMITTED ->
# CONFIRMED, plus REJECTED/CANCELLED/UNKNOWN); lane B asks only how it ended.
# Anything still short of release reads as HELD, which is what it is from the
# vendor's side: the money has not moved and something is stopping it.
PAYOUT_STATE_TO_ORACLE = {
    "CONFIRMED": "CONFIRMED",
    "FAILED": "FAILED",
    "DRAFT": "HELD",
    "APPROVED": "HELD",
    "DISPATCH_PENDING": "HELD",
    "SUBMITTED": "HELD",
    "CANCEL_REQUESTED": "HELD",
    "CANCELLED": "HELD",
    "REJECTED": "HELD",
    "UNKNOWN": "HELD",
}


def _payout_items(conn, festival_id: str, settlements: list[dict]) -> list[dict]:
    """Lane A's payouts, plus an explicit NOTHING_TO_PAY for every settled
    vendor-night that has none.

    The absent ones are not an omission: lane A creates no obligation when a
    night's `payout_cents` is zero, because `create_payout_obligation` issues
    what is still OWED and nothing is. Stated rather than left out, so the
    scorer compares a position instead of a missing row.

    Worth knowing when reading the result: lane A's obligation is the
    commission-net payout alone. `tips_passthrough_cents` below is read from
    the settlement because lane A has nowhere else to put it -- no payout row
    carries tips, so the tips are not part of what this system actually
    transfers. On a night whose payout is zero but which took tips, lane A
    moves nothing at all.
    """
    rows = conn.execute(LIVE_PAYOUTS, {"festival": festival_id}).mappings().all()
    items = []
    for r in rows:
        payload = r["block_payload"] or {}
        reason = payload.get("reason_code") if isinstance(payload, dict) else None
        state = PAYOUT_STATE_TO_ORACLE.get(r["state"], "HELD")
        items.append(
            {
                "vendor_id": r["vendor_id"],
                "business_date": r["business_date"].isoformat(),
                "final_state": state,
                "amount_cents": str(r["amount_cents"]),
                "tips_passthrough_cents": str(r["tips_cents"]),
                "idempotency_keys": [r["idempotency_key"]],
                "provider_submissions": r["dispatch_attempts"],
                "confirmed_provider_payments": 1 if r["payout_confirmed_at"] else 0,
                "released_at_utc": (
                    r["released_at"].astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                    if r["released_at"]
                    else None
                ),
                "hold_reasons": [reason] if state == "HELD" and reason else [],
            }
        )
    have = {(i["vendor_id"], i["business_date"]) for i in items}
    for s in settlements:
        key = (s["vendor_id"], s["business_date"])
        if key in have:
            continue
        items.append(
            {
                "vendor_id": s["vendor_id"],
                "business_date": s["business_date"],
                "final_state": "NOTHING_TO_PAY",
                "amount_cents": s["payout_cents"],
                "tips_passthrough_cents": s["tips_passthrough_cents"],
                "idempotency_keys": [],
                "provider_submissions": 0,
                "confirmed_provider_payments": 0,
                "released_at_utc": None,
                "hold_reasons": [],
            }
        )
    items.sort(key=lambda i: (i["business_date"], i["vendor_id"]))
    return items


def _journal_items(conn, festival_id: str) -> list[dict]:
    entries = conn.execute(JOURNAL_ENTRIES, {"festival": festival_id}).mappings().all()
    lines: dict[str, list[dict]] = {}
    for ln in conn.execute(JOURNAL_LINES).mappings():
        lines.setdefault(ln["journal_entry_id"], []).append(
            {
                # Lane B compares by ROLE, not by chart-of-accounts code.
                "account_role": ln["account_role"] or ln["account_code"],
                "debit_cents": str(ln["debit_cents"]),
                "credit_cents": str(ln["credit_cents"]),
            }
        )
    key_by_id = {e["id"]: (e["entry_key"] or e["id"]) for e in entries}
    return [
        {
            "entry_id": key_by_id[e["id"]],
            "business_date": e["business_date"].isoformat() if e["business_date"] else None,
            "kind": e["kind"],
            "correction_of": key_by_id.get(e["correction_of"]) if e["correction_of"] else None,
            "lines": lines.get(e["id"], []),
        }
        for e in entries
    ]


def _reconciliation_items(conn) -> list[dict]:
    groups = conn.execute(CURRENT_MATCH_GROUPS).mappings().all()
    banks = {b["id"]: b for b in conn.execute(BANK_ENTRIES).mappings()}
    counts = {c["id"]: c for c in conn.execute(CASH_COUNTS).mappings()}

    duplicate_orders = [
        g["member_id"]
        for g in groups
        if g["match_type"] == "order_capture"
        and g["reason_code"] in ("suspected_duplicate_charge", "duplicate_charge_refunded")
    ]
    dup_refs: dict[str, str] = {}
    if duplicate_orders:
        dup_refs = {
            r["order_id"]: r["provider_reference"]
            for r in conn.execute(DUPLICATE_CHARGE_REFS, {"order_ids": duplicate_orders}).mappings()
        }

    items = []
    for g in groups:
        if g["match_type"] == "bank_deposit":
            bank = banks.get(g["member_id"])
            if bank is None:
                continue
            key = _bank_match_key(bank["provider_reference"], bank["batch_gross_cents"] is not None)
        elif g["match_type"] == "cash_count":
            count = counts.get(g["member_id"])
            if count is None:
                continue
            key = f"CASH_COUNT:{count['vendor_id']}:{count['business_date'].isoformat()}"
        else:
            ref = dup_refs.get(g["member_id"])
            if ref is None:
                # Lane B has no per-order groups; see the module docstring.
                continue
            key = f"DUPLICATE_CHARGE:{ref}"
        items.append(
            {
                "match_key": key,
                "status": GROUP_STATUS_TO_ORACLE.get(g["status"], g["status"].upper()),
                "variance_cents": str(g["variance_cents"]),
                "reason_code": g["reason_code"],
            }
        )
    return items


def _case_items(conn, src_by_doc_id: dict[str, str]) -> tuple[list[dict], int]:
    """(items, how many lane A cases were left out -- see the module docstring)."""
    rows = conn.execute(CASES).mappings().all()
    items, skipped = [], 0
    for i, c in enumerate(rows, start=1):
        oracle_reason = CASE_REASON_TO_ORACLE.get(c["reason_code"])
        if oracle_reason is None:
            skipped += 1
            continue
        if c["auto_resolved"] and c["resolved_by"] is None:
            # Decided without anyone being asked -- by a standing policy a
            # human approved earlier (app/policy.py's
            # `find_active_matching_policy`), or by the finding going away
            # (reconcile_day's `_supersede_case`). Lane A still writes a
            # RESOLVED `cases` row for the audit trail, and that row is why
            # this export used to report a repeated question for
            # ven_5aa2250fb593d2e8's terms conflict on night 2. It was not:
            # the human chose the rate once on 2026-10-16 and approved it as
            # a reusable policy, and night 2 was auto-applied by that policy
            # with `resolved_by` NULL. A review question is one a person was
            # put in front of, so these are not reviews.
            skipped += 1
            continue
        items.append(
            {
                "case_id": f"case-{i:04d}",
                "reason_code": oracle_reason,
                "subject_kind": "VENDOR",
                "subject_id": c["vendor_id"],
                "business_date": (c["business_date"].isoformat() if c["business_date"] else None),
                # Lane A's own answer to lane B's question: does an OPEN case
                # of this kind withhold the vendor's money?
                "blocking": bool(c["blocking"]),
                "citations": _citations(c["evidence_refs"], src_by_doc_id),
            }
        )

    # The limit question, which lane A asks through `payout_approvals` rather
    # than `cases`. Blocking by construction: `release_payout` refuses the
    # payout until the finance-lead decision exists.
    for n, r in enumerate(conn.execute(FINANCE_LEAD_ASKS).mappings(), start=len(rows) + 1):
        items.append(
            {
                "case_id": f"case-{n:04d}",
                "reason_code": "PAYOUT_ABOVE_AUTO_LIMIT",
                "subject_kind": "VENDOR",
                "subject_id": r["vendor_id"],
                "business_date": r["business_date"].isoformat(),
                "blocking": True,
                "citations": [],
            }
        )
    return items, skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--festival", required=True)
    ap.add_argument("--set", dest="decision_set", default="F1_story")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()

    engine = make_engine()
    with engine.connect() as conn:
        rows = conn.execute(LIVE_SETTLEMENTS, {"festival": a.festival}).mappings().all()
        src_by_doc_id = {
            r["id"]: r["source_native_id"] for r in conn.execute(SOURCE_DOCUMENT_IDS).mappings()
        }
        journals = _journal_items(conn, a.festival)
        reconciliation = _reconciliation_items(conn)
        cases, cases_skipped = _case_items(conn, src_by_doc_id)

        # Lane B's `unpaid_balance_cents` is the vendor's CLOSING balance of
        # uncollected fee (its line items carry `PRIOR_UNPAID_BALANCE` as a
        # separate input). Lane A states the same economics as two distinct
        # PER-PERIOD quantities and keeps them distinct on purpose:
        # `unpaid_balance_cents` is fee that even total eligible proceeds
        # cannot cover, `vendor_fee_receivable_cents` is fee the vendor's own
        # retained cash could cover but centrally-held funds could not. That
        # distinction is worth more than lane B's single number, so it is
        # translated here rather than collapsed in the database: the closing
        # balance is the running sum of both, over this vendor's nights in
        # date order. Verified against the answer key on 238 of 240 rows.
        running_unpaid: dict[str, int] = {}

        items = []
        for r in rows:
            holds = r["holds"] or []
            running_unpaid[r["vendor_id"]] = running_unpaid.get(r["vendor_id"], 0) + (
                r["unpaid_balance_cents"] + r["vendor_fee_receivable_cents"]
            )
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
                    "unpaid_balance_cents": str(running_unpaid[r["vendor_id"]]),
                    "commission_bps": r["commission_bps_used"],
                    # Our holds say `reason`; lane B's candidate says reason_code.
                    "hold_reasons": [h.get("reason_code") or h.get("reason") for h in holds],
                    "citations": _citations(r["policy_refs"], src_by_doc_id),
                }
            )
        items.sort(key=lambda i: (i["business_date"], i["vendor_id"]))
        payouts = _payout_items(conn, a.festival, items)

    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "meta.json").write_text(
        json.dumps(
            {
                "candidate_format": "encore-candidate-0.1-PROPOSAL",
                "decision_set": a.decision_set,
                "fixture_id": "full_v1",
                "fixture_manifest_sha256": FIXTURE_MANIFEST_SHA256,
                "system": "encore-lane-a (read from the database)",
            },
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )

    written = {
        "settlements": items,
        "journals": journals,
        "reconciliation": reconciliation,
        "cases": cases,
        "payouts": payouts,
    }
    for name, collection in written.items():
        (a.out / f"{name}.json").write_text(
            json.dumps({"items": collection}, sort_keys=True) + "\n"
        )

    print(
        f"wrote {a.out}: "
        + ", ".join(f"{len(v)} {k}" for k, v in written.items())
        + f" ({cases_skipped} match-group diagnostic cases left out)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
