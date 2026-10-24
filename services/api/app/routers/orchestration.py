"""A06 stage 3 endpoints: start a nightly close run, read run/vendor status
and the run's flight recorder, resume a held (or retry a failed) vendor.

Commands are recorded and executed by the worker (services/worker), so
start/resume return 202 with a status URL (C03 long-job semantics). Both
take an Idempotency-Key (header) or `command_id`; same key + same payload
returns the original command, a different payload is a 409.

Capabilities: `run_reconciliation` to start/resume/read; additionally
`run_agents` to start a run that may spend Claude credit
(`explain_cases` with a budget above zero).
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_user_capability_codes, require_capability
from app.db import get_db
from app.models import CAPABILITY_RUN_AGENTS, CAPABILITY_RUN_RECONCILIATION, User
from app.models_ledger import BusinessEvent, Case
from app.models_orchestration import (
    AgentCaseExplanation,
    OrchestrationCommand,
    OrchestrationRun,
    OrchestrationVendorRun,
)
from app.orchestration import coordinator

router = APIRouter(prefix="/api/v1/orchestration", tags=["orchestration"])


class StartRunIn(BaseModel):
    festival_id: str
    business_date: date
    is_final_period: bool = False
    max_concurrency: int = Field(4, ge=1, le=16)
    explain_cases: bool = False
    budget_usd: float = Field(0.0, ge=0, le=5)
    command_id: str | None = Field(None, max_length=64)


class ResumeIn(BaseModel):
    note: str | None = Field(None, max_length=500)
    command_id: str | None = Field(None, max_length=64)


def _command_id(body_id: str | None, header_id: str | None) -> str:
    if body_id and header_id and body_id != header_id:
        raise HTTPException(status_code=422, detail={"code": "command_id_mismatch"})
    value = body_id or header_id or uuid.uuid4().hex
    if len(value) > 64:
        raise HTTPException(status_code=422, detail={"code": "command_id_too_long"})
    return value


def _status_url(run_id: str) -> str:
    return f"/api/v1/orchestration/runs/{run_id}"


def _run_out(orch: OrchestrationRun) -> dict:
    return {
        "run_id": orch.id,
        "festival_id": orch.festival_id,
        "business_date": orch.business_date.isoformat(),
        "is_final_period": orch.is_final_period,
        "status": orch.status,
        "graph_version": orch.graph_version,
        "max_concurrency": orch.max_concurrency,
        "explain_cases": orch.explain_cases,
        "budget_usd": float(orch.budget_usd),
        "spent_usd": float(orch.spent_usd),
        "reserved_usd": float(orch.reserved_usd),
        "needs_work": orch.needs_work,
        "lease_owner": orch.lease_owner,
        "lease_expires_at": orch.lease_expires_at.isoformat() if orch.lease_expires_at else None,
        "claim_count": orch.claim_count,
        "last_error": orch.last_error,
        "requested_by": orch.requested_by,
        "version": orch.version,
        "created_at": orch.created_at.isoformat() if orch.created_at else None,
        "started_at": orch.started_at.isoformat() if orch.started_at else None,
        "completed_at": orch.completed_at.isoformat() if orch.completed_at else None,
        "status_url": _status_url(orch.id),
    }


def _vendor_out(v: OrchestrationVendorRun) -> dict:
    return {
        "vendor_id": v.vendor_id,
        "thread_id": v.thread_id,
        "milestone": v.milestone,
        "status": v.status,
        "held_at": v.held_at,
        "hold_reasons": v.hold_reasons,
        "settlement_id": v.settlement_id,
        "payout_id": v.payout_id,
        "statement_id": v.statement_id,
        "last_error": v.last_error,
        "retryable": v.retryable,
        "version": v.version,
        "updated_at": v.updated_at.isoformat() if v.updated_at else None,
    }


@router.post("/runs", status_code=202)
def start_run(
    body: StartRunIn,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_RUN_RECONCILIATION)),
):
    if body.explain_cases and body.budget_usd > 0:
        if CAPABILITY_RUN_AGENTS not in get_user_capability_codes(db, user.id):
            raise HTTPException(
                status_code=403, detail=f"missing required capability: {CAPABILITY_RUN_AGENTS}"
            )
    command_id = _command_id(body.command_id, idempotency_key)
    try:
        orch, created = coordinator.start_run(
            db,
            request=coordinator.StartRequest(
                festival_id=body.festival_id,
                business_date=body.business_date,
                is_final_period=body.is_final_period,
                max_concurrency=body.max_concurrency,
                budget_usd=body.budget_usd,
                explain_cases=body.explain_cases,
            ),
            command_id=command_id,
            actor_id=user.id,
        )
    except coordinator.CommandConflictError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail={"code": "conflict", "message": str(exc), "run_id": exc.run_id}
        ) from exc
    db.commit()
    return JSONResponse(
        status_code=202 if created else 200,
        content={"command_id": command_id, "created": created, **_run_out(orch)},
    )


@router.get("/runs")
def list_runs(
    festival_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_RUN_RECONCILIATION)),
) -> dict:
    q = select(OrchestrationRun).order_by(OrchestrationRun.created_at.desc())
    if festival_id:
        q = q.where(OrchestrationRun.festival_id == festival_id)
    return {"items": [_run_out(r) for r in db.execute(q).scalars()], "next_cursor": None}


@router.get("/runs/{run_id}")
def get_run(
    run_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_RUN_RECONCILIATION)),
) -> dict:
    orch = db.get(OrchestrationRun, run_id)
    if orch is None:
        raise HTTPException(status_code=404, detail="run not found")
    vendors = (
        db.execute(
            select(OrchestrationVendorRun)
            .where(OrchestrationVendorRun.run_id == run_id)
            .order_by(OrchestrationVendorRun.created_at)
        )
        .scalars()
        .all()
    )
    counts: dict[str, int] = {}
    for v in vendors:
        counts[v.status] = counts.get(v.status, 0) + 1
    return {**_run_out(orch), "vendor_counts": counts, "vendors": [_vendor_out(v) for v in vendors]}


def _checkpoint_view(thread_id: str) -> dict | None:
    """Workflow position from the LangGraph checkpoint (not the audit
    record). None if the checkpointer is unavailable."""
    try:
        snap = coordinator.get_graph().get_state(coordinator._config(thread_id))
    except Exception:  # noqa: BLE001 -- status must still render
        return None
    if not snap.values:
        return {"started": False}
    return {
        "started": True,
        "next": list(snap.next),
        "waiting_for_human": bool(snap.interrupts),
        "interrupts": [i.value for i in snap.interrupts],
        "checkpoint_id": snap.config.get("configurable", {}).get("checkpoint_id"),
        "created_at": snap.created_at,
    }


@router.get("/runs/{run_id}/vendors/{vendor_id}")
def get_vendor(
    run_id: str,
    vendor_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_RUN_RECONCILIATION)),
) -> dict:
    v = db.execute(
        select(OrchestrationVendorRun).where(
            OrchestrationVendorRun.run_id == run_id, OrchestrationVendorRun.vendor_id == vendor_id
        )
    ).scalar_one_or_none()
    if v is None:
        raise HTTPException(status_code=404, detail="vendor run not found")
    case_ids = [r["case_id"] for r in v.hold_reasons or [] if r.get("case_id")]
    cases = []
    for cid in case_ids:
        c = db.get(Case, cid)
        if c is None:
            continue
        expl = (
            db.execute(
                select(AgentCaseExplanation)
                .where(AgentCaseExplanation.case_id == cid)
                .order_by(AgentCaseExplanation.created_at)
            )
            .scalars()
            .all()
        )
        cases.append(
            {
                "case_id": c.id,
                "status": c.status,
                "reason_code": c.reason_code,
                "financial_impact_cents": c.financial_impact_cents,
                "explanations": [
                    {
                        "id": e.id,
                        "attempt": e.attempt,
                        "status": e.status,
                        "retryable": e.retryable,
                        "error": e.error,
                        "model_requested": e.model_requested,
                        "model_served": e.model_served,
                        "prompt_version": e.prompt_version,
                        "explanation": e.explanation if e.status == "SUCCEEDED" else None,
                        "citations": e.citations,
                        "label": "AI-PROPOSED explanation; not a decision",
                    }
                    for e in expl
                ],
            }
        )
    commands = (
        db.execute(
            select(OrchestrationCommand)
            .where(
                OrchestrationCommand.run_id == run_id, OrchestrationCommand.vendor_id == vendor_id
            )
            .order_by(OrchestrationCommand.created_at)
        )
        .scalars()
        .all()
    )
    return {
        **_vendor_out(v),
        "run_id": run_id,
        "checkpoint": _checkpoint_view(v.thread_id),
        "cases": cases,
        "commands": [
            {"command_id": c.id, "kind": c.kind, "status": c.status, "actor_id": c.actor_id}
            for c in commands
        ],
    }


@router.post("/runs/{run_id}/vendors/{vendor_id}/resume", status_code=202)
def resume_vendor(
    run_id: str,
    vendor_id: str,
    body: ResumeIn,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_RUN_RECONCILIATION)),
):
    command_id = _command_id(body.command_id, idempotency_key)
    try:
        cmd, created = coordinator.request_resume(
            db,
            run_id=run_id,
            vendor_id=vendor_id,
            command_id=command_id,
            actor_id=user.id,
            actor_capability=CAPABILITY_RUN_RECONCILIATION,
            note=body.note,
        )
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except coordinator.CommandConflictError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail={"code": "conflict", "message": str(exc)})
    except coordinator.ResumeRefusedError as exc:
        db.rollback()
        raise HTTPException(
            status_code=422, detail={"code": exc.code, **exc.details, "retryable": False}
        ) from exc
    db.commit()
    return JSONResponse(
        status_code=202 if created else 200,
        content={
            "command_id": cmd.id,
            "kind": cmd.kind,
            "status": cmd.status,
            "created": created,
            "status_url": f"{_status_url(run_id)}/vendors/{vendor_id}",
        },
    )


@router.get("/runs/{run_id}/events")
def run_events(
    run_id: str,
    vendor_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_RUN_RECONCILIATION)),
) -> dict:
    """The flight recorder for this run: orchestration streams plus every
    domain event stamped with this run id."""
    q = (
        select(BusinessEvent)
        .where(BusinessEvent.run_id == run_id)
        .order_by(BusinessEvent.recorded_at, BusinessEvent.sequence)
    )
    if vendor_id:
        q = q.where(BusinessEvent.vendor_id == vendor_id)
    return {
        "items": [
            {
                "event_id": e.event_id,
                "stream_id": e.stream_id,
                "sequence": e.sequence,
                "event_type": e.event_type,
                "vendor_id": e.vendor_id,
                "actor_id": e.actor_id,
                "actor_type": e.actor_type,
                "actor_capability": e.actor_capability,
                "command_id": e.command_id,
                "recorded_at": e.recorded_at.isoformat() if e.recorded_at else None,
                "versions": e.versions,
                "payload": e.payload,
                "hash": e.hash,
            }
            for e in db.execute(q).scalars()
        ],
        "next_cursor": None,
    }
