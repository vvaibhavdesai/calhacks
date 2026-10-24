"""A09 what-if branches: isolated counterfactual settlement runs.

A branch answers "what would this vendor's settlements have been, as known
at time T, if this rule had said X instead?" without touching production.

Isolation, in three layers:
  1. Inputs are read in a separate session whose transaction is
     `SET TRANSACTION READ ONLY` -- Postgres itself refuses any write.
  2. The computation runs inside `effects_guard.effects_disabled`, so a
     model call, a rail submission or a production journal posting raises
     instead of acting (and is counted).
  3. Output goes only to the branch-tagged tables `whatif_branches` /
     `whatif_results` and to `business_events` rows whose `branch_id` is
     set (production event reads filter `branch_id IS NULL`). Production
     row counts are measured before and after and stored on the branch.

There is no promote/merge/apply route. Changing production needs a new,
reviewed production command: a challenge (POST /challenges) with its own
preview and approval (C06).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from domain import corrections as dc
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db import get_session_factory
from app.effects_guard import effects_disabled
from app.models_agents import DocumentExtraction
from app.models_corrections import WhatIfBranch, WhatIfResult
from app.models_finance import VendorStatement
from app.models_ledger import (
    BusinessEvent,
    JournalEntry,
    OutboxIntent,
    Payout,
    PolicyVersion,
    Vendor,
)
from app.models_payout_dispatch import RailSimulatorRecord
from app.models_settlement import Settlement
from app.uow import UnitOfWork

ISOLATION = {"storage": "ISOLATED", "external_effects": "DISABLED", "production_writes": "NONE"}


class BranchError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def production_counts(db: Session) -> dict:
    def n(model, *where):
        q = select(func.count()).select_from(model)
        for w in where:
            q = q.where(w)
        return db.execute(q).scalar_one()

    return {
        "settlements": n(Settlement),
        "payouts": n(Payout),
        "journal_entries": n(JournalEntry),
        "vendor_statements": n(VendorStatement),
        "outbox_intents": n(OutboxIntent),
        "rail_simulator_records": n(RailSimulatorRecord),
        "document_extractions": n(DocumentExtraction),
        "production_events": n(BusinessEvent, BusinessEvent.branch_id.is_(None)),
    }


def payout_amount_sum(db: Session) -> int:
    return int(db.execute(select(func.coalesce(func.sum(Payout.amount_cents), 0))).scalar_one())


def _known_chain(
    db: Session, vendor_id: str, festival_id: str, cutoff: datetime
) -> list[Settlement]:
    rows = (
        db.execute(
            select(Settlement)
            .where(
                Settlement.vendor_id == vendor_id,
                Settlement.festival_id == festival_id,
                Settlement.created_at <= cutoff,
            )
            .order_by(Settlement.business_date, Settlement.created_at)
        )
        .scalars()
        .all()
    )
    superseded = {s.correction_of for s in rows if s.correction_of}
    by_date: dict[date, Settlement] = {}
    for s in rows:
        if s.id not in superseded:
            by_date[s.business_date] = s
    return [by_date[d] for d in sorted(by_date)]


def _compute(
    ro: Session, *, festival_id: str, cutoff: datetime, business_date: date | None, overrides: list
) -> list[dict]:
    out = []
    for ov in overrides:
        vendor_id = ov.get("vendor_id")
        effective_from = date.min
        policy = None
        if ov.get("policy_version_id"):
            policy = ro.get(PolicyVersion, ov["policy_version_id"])
            if policy is None:
                raise BranchError("UNKNOWN_POLICY", "override policy_version_id not found")
            vendor_id = (policy.scope or {}).get("vendor_id")
            effective_from = policy.valid_from or date.min
        if ov.get("effective_from"):
            effective_from = date.fromisoformat(ov["effective_from"])
        vendor = ro.get(Vendor, vendor_id) if vendor_id else None
        if vendor is None or vendor.festival_id != festival_id:
            raise BranchError("UNKNOWN_VENDOR", "override does not resolve to a festival vendor")
        bps = ov.get("commission_bps")
        if not isinstance(bps, int) or isinstance(bps, bool) or not 0 <= bps <= 10_000:
            raise BranchError("INVALID_OVERRIDE", "commission_bps must be an integer 0..10000")
        chain = _known_chain(ro, vendor.id, festival_id, cutoff)
        periods = [
            dc.PeriodInput(
                settlement_id=s.id,
                business_date=s.business_date,
                centrally_held_cents=s.centrally_held_cents,
                vendor_retained_cash_cents=s.vendor_retained_cash_cents,
                tax_cents=s.tax_cents,
                tips_cents=s.tips_cents,
                processor_fee_assessed_cents=s.processor_fee_assessed_cents,
                processor_fee_deductible=s.processor_fee_deductible_used,
                is_final_period=s.is_final_period,
                minimum_fee_cents=s.minimum_fee_cents_used,
                commission_bps=s.commission_bps_used,
                assessed_fee_cents=s.assessed_fee_cents,
                payout_cents=s.payout_cents,
            )
            for s in chain
        ]
        for r in dc.recompute_vendor_chain(
            periods, new_commission_bps=bps, effective_from=effective_from
        ):
            if business_date is not None and r.period.business_date != business_date:
                continue
            out.append(
                {
                    "production_settlement_id": r.period.settlement_id,
                    "vendor_id": vendor.id,
                    "vendor_name": vendor.name,
                    "business_date": r.period.business_date,
                    "production_payout_cents": r.period.payout_cents,
                    "branch_payout_cents": r.result.vendor_payout_cents,
                    "production_fee_cents": r.period.assessed_fee_cents,
                    "branch_fee_cents": r.result.fee_assessed_cents,
                    "branch_commission_bps": r.commission_bps,
                    "production_commission_bps": r.period.commission_bps,
                    "policy_version_id": policy.id if policy else None,
                }
            )
    return out


def create_branch(
    db: Session,
    *,
    festival_id: str,
    label: str,
    base_knowledge_cutoff: datetime,
    business_date: date | None,
    overrides: list[dict],
    actor_id: str,
    capability: str,
) -> WhatIfBranch:
    if not overrides:
        raise BranchError("OVERRIDES_REQUIRED", "at least one override is required")
    if not (label or "").strip():
        raise BranchError("LABEL_REQUIRED", "label is required")
    before = production_counts(db)
    before_amount = payout_amount_sum(db)
    ro = get_session_factory()()
    try:
        ro.execute(text("SET TRANSACTION READ ONLY"))
        with effects_disabled("what-if branch") as attempts:
            computed = _compute(
                ro,
                festival_id=festival_id,
                cutoff=base_knowledge_cutoff,
                business_date=business_date,
                overrides=overrides,
            )
        ro.rollback()
    finally:
        ro.close()

    branch = WhatIfBranch(
        status="READY",
        label=label.strip(),
        festival_id=festival_id,
        base_knowledge_cutoff=base_knowledge_cutoff,
        business_date=business_date,
        overrides=overrides,
        isolation=dict(ISOLATION),
        created_by=actor_id,
        note=(
            "BRANCH - not production. Inputs read as known at the base cutoff in a READ ONLY "
            "transaction; nothing here can be promoted. To change production, open a challenge."
        ),
    )
    db.add(branch)
    db.flush()
    for row in computed:
        db.add(
            WhatIfResult(
                branch_id=branch.id,
                production_settlement_id=row["production_settlement_id"],
                vendor_id=row["vendor_id"],
                business_date=row["business_date"],
                label=f"{row['vendor_name']} {row['business_date']}",
                production_payout_cents=row["production_payout_cents"],
                branch_payout_cents=row["branch_payout_cents"],
                production_fee_cents=row["production_fee_cents"],
                branch_fee_cents=row["branch_fee_cents"],
                branch_commission_bps=row["branch_commission_bps"],
                branch_state={
                    "production_commission_bps": row["production_commission_bps"],
                    "policy_version_id": row["policy_version_id"],
                },
            )
        )
    db.flush()
    UnitOfWork(db).append_event(
        stream_id=f"branch:{branch.id}",
        event_type="whatif.branch_computed",
        aggregate_id=branch.id,
        aggregate_version=branch.version,
        actor_id=actor_id,
        actor_type="human",
        actor_capability=capability,
        occurred_at=datetime.now(UTC),
        branch_id=branch.id,
        payload={
            "festival_id": festival_id,
            "label": branch.label,
            "base_knowledge_cutoff": base_knowledge_cutoff.isoformat(),
            "overrides": overrides,
            "results": len(computed),
            "isolation": ISOLATION,
        },
        lineage_refs=[{"ref_kind": "branch", "ref_id": branch.id}],
    )
    after = production_counts(db)
    # the branch's own event is branch-tagged, so production counts must match
    branch.summary = {
        "production_counts_before": before,
        "production_counts_after": after,
        "production_payout_amount_sum_before": before_amount,
        "production_payout_amount_sum_after": payout_amount_sum(db),
        "production_unchanged": before == after,
        "effect_attempts": attempts,
        "results": len(computed),
    }
    db.flush()
    return branch


def branch_out(db: Session, b: WhatIfBranch) -> dict:
    rows = (
        db.execute(
            select(WhatIfResult)
            .where(WhatIfResult.branch_id == b.id)
            .order_by(WhatIfResult.business_date, WhatIfResult.label)
        )
        .scalars()
        .all()
    )
    comparison = []
    totals = {"production_payout": 0, "branch_payout": 0, "production_fee": 0, "branch_fee": 0}
    for r in rows:
        totals["production_payout"] += r.production_payout_cents
        totals["branch_payout"] += r.branch_payout_cents
        totals["production_fee"] += r.production_fee_cents
        totals["branch_fee"] += r.branch_fee_cents
        for what, prod, br in (
            ("payout", r.production_payout_cents, r.branch_payout_cents),
            ("commission", r.production_fee_cents, r.branch_fee_cents),
        ):
            comparison.append(
                {
                    "record": {
                        "kind": "SETTLEMENT",
                        "id": r.production_settlement_id,
                        "version": None,
                        "label": r.label,
                    },
                    "label": f"{r.label} {what} ({r.branch_commission_bps} bps in branch)",
                    "measure": what,
                    "production_cents": str(prod),
                    "branch_cents": str(br),
                    "delta_cents": str(br - prod),
                }
            )
    return {
        "id": b.id,
        "version": b.version,
        "status": b.status,
        "label": b.label,
        "festival_id": b.festival_id,
        "base_knowledge_cutoff": b.base_knowledge_cutoff.astimezone(UTC).isoformat(),
        "business_date": b.business_date.isoformat() if b.business_date else None,
        "overrides": b.overrides,
        "isolation": b.isolation,
        "comparison": comparison,
        "totals": {k: str(v) for k, v in totals.items()}
        | {
            "payout_delta": str(totals["branch_payout"] - totals["production_payout"]),
            "fee_delta": str(totals["branch_fee"] - totals["production_fee"]),
        },
        "summary": b.summary,
        "note": b.note,
        "promotion": {
            "allowed": False,
            "how": "POST /api/v1/challenges (new reviewed production command with its own preview "
            "and approval); a branch is never merged or applied.",
        },
        "created_by": b.created_by,
        "created_at": b.created_at.isoformat() if b.created_at else None,
    }
