"""A06 endpoints: run Claude document extraction (spends API credit) and
read what it proposed and what code verified.

`POST /agents/extract-documents` requires `run_agents`. It is bounded: a
USD budget (default $2) and a document cap; completed documents are reused,
never re-called. Synchronous for now -- the per-vendor LangGraph run (next
A06 stage) becomes the durable job runner.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.extraction import configured_model, make_client
from app.agents.runner import festival_documents, run_batch
from app.auth import get_current_user, require_capability
from app.db import get_db
from app.models import CAPABILITY_RUN_AGENTS, User
from app.models_agents import DocumentClaim, DocumentExtraction
from app.storage.local_fs import get_document_storage

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])


class ExtractIn(BaseModel):
    festival_id: str
    source_document_ids: list[str] | None = None
    max_documents: int = Field(200, ge=1, le=1000)
    budget_usd: float = Field(2.0, gt=0, le=50)


@router.post("/extract-documents")
def extract_documents(
    body: ExtractIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_RUN_AGENTS)),
) -> dict:
    ids = body.source_document_ids or festival_documents(db, body.festival_id)
    ids = ids[: body.max_documents]
    try:
        client = make_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail={"code": "model_not_configured"}) from exc
    res = run_batch(
        document_ids=ids,
        storage=get_document_storage(),
        client=client,
        budget_usd=body.budget_usd,
    )
    return {
        "model": configured_model(),
        "documents": len(ids),
        "counts": res.counts(),
        "spent_usd_estimate": round(res.spent_usd, 4),
        "stopped_for_budget": res.stopped_for_budget,
        "results": [
            {
                "source_document_id": r.source_document_id,
                "status": r.status,
                "extraction_id": r.extraction_id,
                "claims": r.claims,
                "verified": r.verified,
                "note": r.note,
            }
            for r in res.results
        ],
    }


@router.get("/extractions")
def list_extractions(
    source_document_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    q = select(DocumentExtraction).order_by(DocumentExtraction.created_at)
    if source_document_id:
        q = q.where(DocumentExtraction.source_document_id == source_document_id)
    return {
        "items": [
            {
                "id": e.id,
                "source_document_id": e.source_document_id,
                "content_hash": e.content_hash,
                "parser": e.parser,
                "model_requested": e.model_requested,
                "model_served": e.model_served,
                "prompt_version": e.prompt_version,
                "attempt": e.attempt,
                "status": e.status,
                "retryable": e.retryable,
                "error": e.error,
                "request_id": e.request_id,
                "usage": {"input_tokens": e.input_tokens, "output_tokens": e.output_tokens},
                "document_kind": e.document_kind,
                "summary": e.summary,
                "embedded_instructions_detected": e.embedded_instructions_detected,
            }
            for e in db.execute(q).scalars()
        ],
        "next_cursor": None,
    }


@router.get("/claims")
def list_claims(
    vendor_id: str | None = None,
    verified: bool | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    q = select(DocumentClaim).order_by(DocumentClaim.created_at)
    if vendor_id:
        q = q.where(DocumentClaim.vendor_id == vendor_id)
    if verified is not None:
        q = q.where(DocumentClaim.verified.is_(verified))
    return {
        "items": [
            {
                "id": c.id,
                "extraction_id": c.extraction_id,
                "vendor_id": c.vendor_id,
                "claim_kind": c.claim_kind,
                "value_raw": c.value_raw,
                "commission_bps": c.commission_bps,
                "minimum_fee_cents": c.minimum_fee_cents,
                "minimum_period": c.minimum_period,
                "processing_fee_deductible": c.processing_fee_deductible,
                "effective_from": c.effective_from.isoformat() if c.effective_from else None,
                "speaker_role": c.speaker_role,
                "restates_existing_terms": c.restates_existing_terms,
                "available_from": c.available_from.isoformat(),
                "verified": c.verified,
                "verification_notes": c.verification_notes,
                "source_ref": c.source_ref,
                "label": "AI-PROPOSED claim; code-verified against the source, not a decision",
            }
            for c in db.execute(q).scalars()
        ],
        "next_cursor": None,
    }
