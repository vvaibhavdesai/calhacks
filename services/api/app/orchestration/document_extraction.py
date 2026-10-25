"""The nightly close's document-extraction step.

Terms resolution (app/terms_resolution.py) turns VERIFIED `document_claims`
into contract terms. Nothing in the nightly close produced those claims:
extraction only ever ran because a human called
POST /api/v1/agents/extract-documents first. A night nobody primed
therefore resolved terms from no claims at all -- which, before
`vendor_terms_unevidenced` existed, meant silently billing the festival
default.

So the close extracts first, as a festival-level step ahead of the vendor
threads that read what it produced. What this module adds is only the
decision to run it, the budget it runs under and the record that it ran;
the extraction itself is A06's existing `run_batch`, unchanged, which is
also what keeps a paid call from happening twice: `extract_document` is
idempotent per (document version, model, prompt version), so a second
night, a re-run or a resumed worker reports REUSED and is not charged.

Spend comes out of the run's OWN budget (`orchestration_runs.budget_usd`),
the same budget the explanation node draws on, so a run's declared budget
bounds everything the run can spend rather than just one part of it.

Deliberately not fatal. A run with no model configured, no budget left or a
failing extractor records what happened and carries on: the vendors whose
terms it could not establish then hold on their own
`vendor_terms_unevidenced` cases, which is the honest outcome -- money
waiting on a human -- rather than a close that dies, or one that bills a
rate nobody evidenced.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.agents import limits
from app.agents.extraction import configured_model, make_client
from app.agents.runner import festival_documents, pending_documents, run_batch
from app.config import get_settings
from app.storage.local_fs import get_document_storage

logger = logging.getLogger("orchestration.extract")

STEP = "festival:extract_documents"


def _remaining_budget(db: Session, run_id: str) -> float:
    row = db.execute(
        text("SELECT budget_usd - spent_usd - reserved_usd FROM orchestration_runs WHERE id = :id"),
        {"id": run_id},
    ).scalar_one_or_none()
    return float(row or 0)


def _settle(db: Session, run_id: str, cost: float) -> None:
    db.execute(
        text("UPDATE orchestration_runs SET spent_usd = spent_usd + :c WHERE id = :id"),
        {"c": Decimal(str(round(cost, 6))), "id": run_id},
    )


def extract_for_run(db: Session, *, run_id: str, festival_id: str) -> dict:
    """Extract this festival's not-yet-extracted documents. Never commits
    (the caller's `run_once` commits the effect and its step row together)
    and never raises: every outcome is a JSON-safe dict for the step row.
    """
    document_ids = festival_documents(db, festival_id)
    pending = pending_documents(db, document_ids)
    base = {"documents": len(document_ids), "pending": len(pending)}
    if not pending:
        # Nothing new since the last run. No client is built, so a festival
        # whose documents are all extracted closes with no model configured
        # at all.
        return {**base, "status": "nothing_to_extract"}

    settings = get_settings()
    budget = _remaining_budget(db, run_id)
    if budget <= 0:
        return {**base, "status": "no_budget_left", "budget_usd": 0.0}
    # Clamped to A10's per-request caps rather than offered up whole: the
    # run's budget is what the operator allowed this run, and asking for
    # more than a single batch may have is a refusal, not a smaller batch.
    budget = min(budget, settings.claude_max_budget_usd_per_request)
    batch = pending[: settings.claude_max_documents_per_request]

    try:
        grant = limits.check_request(db, settings, budget_usd=budget, max_documents=len(batch))
    except limits.LimitError as exc:
        return {**base, "status": "refused_by_limits", "detail": exc.detail}

    try:
        client = make_client()
    except RuntimeError:
        # No key configured here. Said plainly, not as a crash: the nights
        # whose terms depend on it will hold.
        return {**base, "status": "model_not_configured"}

    try:
        limits.gate.try_start(settings.claude_max_batches_per_hour)
    except limits.LimitError as exc:
        return {**base, "status": "refused_by_limits", "detail": exc.detail}
    try:
        res = run_batch(
            document_ids=batch[: grant.max_documents],
            storage=get_document_storage(),
            client=client,
            budget_usd=grant.budget_usd,
            concurrency=grant.concurrency,
        )
    except Exception as exc:  # noqa: BLE001 -- a failed extractor never fails the close
        error = f"{type(exc).__name__}: {exc}"[:300]
        logger.warning(json.dumps({"event": "extraction_batch_failed", "error": error}))
        return {**base, "status": "error", "error": error}
    finally:
        limits.gate.finish()

    # `run_batch` commits each document's own call; what is settled here is
    # the run's budget, in the same transaction as the step row.
    if res.spent_usd:
        _settle(db, run_id, res.spent_usd)
    return {
        **base,
        "status": "ran",
        "model": configured_model(),
        "counts": res.counts(),
        "spent_usd_estimate": round(res.spent_usd, 4),
        "stopped_for_budget": res.stopped_for_budget,
        "budget_usd_applied": round(grant.budget_usd, 4),
    }
