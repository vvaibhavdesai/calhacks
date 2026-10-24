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
from sqlalchemy import func, select
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


class RunVendorSummaryOut(BaseModel):
    """Server-derived counts of vendor runs by state. C04 makes completion a
    property of every vendor obligation, so the count is computed here and the
    UI displays it rather than recounting from a partial page."""

    total: int
    active: int
    held: int
    failed: int
    completed: int


class HoldOut(BaseModel):
    reason_code: str
    description: str
    blocking: bool
    case_id: str | None = None
    since: str | None = None


class VendorRunOut(BaseModel):
    id: str
    version: int
    run_id: str
    vendor_id: str
    business_date: str
    milestone: str | None
    state: str
    holds: list[HoldOut]
    case_ids: list[str]
    settlement_id: str | None = None
    payout_id: str | None = None
    statement_id: str | None = None
    failure: str | None = None
    retryable: bool = False
    updated_at: str | None = None


class VendorRunListOut(BaseModel):
    items: list[VendorRunOut]
    next_cursor: str | None = None


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


# The graph tracks PENDING/RUNNING separately because only one of them can be
# claimed; to a reader both mean "this vendor is still moving", which is the
# distinction C04's VendorRunState draws.
_STATE_BY_STATUS = {
    "PENDING": "ACTIVE",
    "RUNNING": "ACTIVE",
    "HELD": "HELD",
    "FAILED": "FAILED",
    "COMPLETED": "COMPLETED",
}


def _vendor_state(status: str) -> str:
    return _STATE_BY_STATUS.get(status, "ACTIVE")


def _hold_out(raw: dict, *, since: str | None) -> dict:
    """One persisted hold reason as a Hold record.

    The graph writes these as `{"code": ..., ...}` with whatever the holding
    node knew (a case id, a payout id, a terms source), so the code is the
    stable part and everything else is best-effort. `blocking` defaults to
    true: a reason that reached this list is one that stopped the vendor.
    """
    code = raw.get("code") or raw.get("reason_code") or "unknown"
    extra = {
        k: v
        for k, v in raw.items()
        if k not in ("code", "reason_code", "description", "blocking", "case_id", "since")
    }
    description = raw.get("description") or code.replace("_", " ")
    if raw.get("reason_code") and raw.get("code") == "open_case":
        description = f"open case: {raw['reason_code'].replace('_', ' ')}"
    if extra:
        description = f"{description} ({', '.join(f'{k}={v}' for k, v in sorted(extra.items()))})"
    return {
        "reason_code": code,
        "description": description,
        "blocking": bool(raw.get("blocking", True)),
        "case_id": raw.get("case_id"),
        "since": raw.get("since") or since,
    }


def _vendor_run_out(v: OrchestrationVendorRun, *, business_date: str) -> dict:
    since = v.updated_at.isoformat() if v.updated_at else None
    raw_holds = [r for r in (v.hold_reasons or []) if isinstance(r, dict)]
    holds = [_hold_out(r, since=since) for r in raw_holds]
    case_ids: list[str] = []
    for h in holds:
        if h["case_id"] and h["case_id"] not in case_ids:
            case_ids.append(h["case_id"])
    return {
        "id": v.id,
        "version": v.version,
        "run_id": v.run_id,
        "vendor_id": v.vendor_id,
        "business_date": business_date,
        "milestone": None if v.milestone == "PENDING" else v.milestone,
        "state": _vendor_state(v.status),
        "holds": holds,
        "case_ids": case_ids,
        "settlement_id": v.settlement_id,
        "payout_id": v.payout_id,
        "statement_id": v.statement_id,
        "failure": v.last_error,
        "retryable": v.retryable,
        "updated_at": since,
    }


def _summary(counts: dict[str, int]) -> dict:
    """Per-state counts keyed the way C04 names the states."""
    by_state: dict[str, int] = {"ACTIVE": 0, "HELD": 0, "FAILED": 0, "COMPLETED": 0}
    for status, n in counts.items():
        by_state[_vendor_state(status)] = by_state.get(_vendor_state(status), 0) + n
    return {
        "total": sum(by_state.values()),
        "active": by_state["ACTIVE"],
        "held": by_state["HELD"],
        "failed": by_state["FAILED"],
        "completed": by_state["COMPLETED"],
    }


def _run_out(orch: OrchestrationRun, *, summary: dict | None = None) -> dict:
    out = {
        # `run_id` is the name A06's own callers and status URLs use; `id` is
        # the name every other record in the API exposes. Both, so neither a
        # worker nor a reader has to special-case this one resource.
        "id": orch.id,
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
    if summary is not None:
        out["vendor_summary"] = summary
    return out


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
    runs = list(db.execute(q).scalars())

    # One grouped query for every run on the page rather than a count per run.
    counts: dict[str, dict[str, int]] = {r.id: {} for r in runs}
    if runs:
        rows = db.execute(
            select(
                OrchestrationVendorRun.run_id,
                OrchestrationVendorRun.status,
                func.count().label("n"),
            )
            .where(OrchestrationVendorRun.run_id.in_([r.id for r in runs]))
            .group_by(OrchestrationVendorRun.run_id, OrchestrationVendorRun.status)
        ).all()
        for run_id, status, n in rows:
            counts[run_id][status] = n
    return {
        "items": [_run_out(r, summary=_summary(counts[r.id])) for r in runs],
        "next_cursor": None,
    }


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
    business_date = orch.business_date.isoformat()
    return {
        **_run_out(orch, summary=_summary(counts)),
        # `vendor_counts` is the raw graph status tally this endpoint has always
        # returned; kept so the worker's own readers don't break.
        "vendor_counts": counts,
        "vendors": [_vendor_out(v) for v in vendors],
        "vendor_runs": [_vendor_run_out(v, business_date=business_date) for v in vendors],
    }


@router.get("/runs/{run_id}/vendors", response_model=VendorRunListOut)
def list_run_vendors(
    run_id: str,
    state: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_RUN_RECONCILIATION)),
) -> dict:
    """The run's vendor obligations as C04 vendor-run records.

    `GET /runs/{run_id}` embeds the same rows, but a reader paging or
    filtering the pipeline wants them as a collection of their own, with
    `hold_reasons` rendered as Hold records rather than raw node output.
    """
    orch = db.get(OrchestrationRun, run_id)
    if orch is None:
        raise HTTPException(status_code=404, detail="run not found")
    if state is not None and state not in ("ACTIVE", "HELD", "FAILED", "COMPLETED"):
        raise HTTPException(status_code=422, detail=f"unknown vendor run state: {state}")
    vendors = (
        db.execute(
            select(OrchestrationVendorRun)
            .where(OrchestrationVendorRun.run_id == run_id)
            .order_by(OrchestrationVendorRun.created_at)
        )
        .scalars()
        .all()
    )
    business_date = orch.business_date.isoformat()
    items = [_vendor_run_out(v, business_date=business_date) for v in vendors]
    if state is not None:
        items = [i for i in items if i["state"] == state]
    return {"items": items, "next_cursor": None}


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
