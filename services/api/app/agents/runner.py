"""A06: bounded batch runner for document extraction.

Each document is extracted in its own DB session and committed on its own,
so one failure never rolls back another and an interrupted batch resumes
by simply running again (completed documents are REUSED, not re-called).
Concurrency is a small fixed pool; the USD budget is checked between waves
and the batch stops before starting a wave that could exceed it.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import anthropic
from sqlalchemy import or_, select

from app.agents.extraction import ExtractResult, extract_document
from app.db import get_session_factory
from app.models_ledger import SourceDocument, Vendor
from app.storage.base import DocumentStorage

EXTRACTABLE_KINDS = ("CONTRACT", "AMENDMENT", "EMAIL")
# Worst-case spend for one document (8k output tokens + generous input) --
# used to stop BEFORE a wave could cross the budget.
WORST_CASE_DOC_USD = 0.12


@dataclass
class BatchResult:
    results: list[ExtractResult] = field(default_factory=list)
    spent_usd: float = 0.0
    stopped_for_budget: bool = False

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out


def festival_documents(db, festival_id: str) -> list[str]:
    q = (
        select(SourceDocument.id)
        .outerjoin(Vendor, Vendor.id == SourceDocument.vendor_id)
        .where(
            SourceDocument.document_kind.in_(EXTRACTABLE_KINDS),
            or_(Vendor.festival_id == festival_id, SourceDocument.vendor_id.is_(None)),
        )
        .order_by(SourceDocument.source_native_id)
    )
    return [r for (r,) in db.execute(q).all()]


def run_batch(
    *,
    document_ids: list[str],
    storage: DocumentStorage,
    client: anthropic.Anthropic,
    budget_usd: float,
    concurrency: int = 4,
) -> BatchResult:
    factory = get_session_factory()
    out = BatchResult()

    def one(doc_id: str) -> ExtractResult:
        db = factory()
        try:
            r = extract_document(db, storage, client, source_document_id=doc_id)
            db.commit()
            return r
        except Exception as exc:  # never let one document take down the batch
            db.rollback()
            return ExtractResult(doc_id, "ERROR", None, note=f"{type(exc).__name__}: {exc}"[:300])
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for start in range(0, len(document_ids), concurrency):
            wave = document_ids[start : start + concurrency]
            if out.spent_usd + len(wave) * WORST_CASE_DOC_USD > budget_usd:
                out.stopped_for_budget = True
                break
            for r in pool.map(one, wave):
                out.results.append(r)
                out.spent_usd += r.cost_usd
    return out
