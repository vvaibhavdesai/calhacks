"""Parent run for one festival/business_date: commands, worker claims,
festival-level steps, bounded fan-out over vendor graphs, run projection.

Lifecycle
---------
1. `start_run` (API, one transaction): a shared `runs` row + an
   `orchestration_runs` row + the start command + `orchestration.run_queued`
   event. Idempotent on command_id (same id + same payload returns the same
   run; a different payload is a conflict). One run per festival/date.
2. A worker `claim_next_run`s it: an atomic UPDATE ... FOR UPDATE SKIP
   LOCKED that takes a time-limited lease. `execute_run` renews the lease
   from a heartbeat thread. If the worker dies, the lease expires and any
   worker reclaims the run; that is the only "recovery" step needed,
   because re-executing is safe (recorder.run_once + A07/A08 keys).
3. `execute_run`: festival-level steps (bank deposit reconciliation, day
   sales/deposit postings) once each, then every vendor's LangGraph thread
   with at most `max_concurrency` running at once:
      - no checkpoint yet          -> start it
      - interrupted, resume cmd    -> Command(resume=...)
      - interrupted, no command    -> leave it (it is waiting for a human)
      - mid-run (crashed worker)   -> invoke(None): continue from checkpoint
      - FAILED                     -> only after an explicit retry command
      - finished                   -> nothing
   One vendor's hold or failure never stops another vendor's thread.
4. The run projection is derived from vendor rows (C04): COMPLETED when all
   completed, WAITING_REVIEW when any is held, PARTIALLY_COMPLETE when some
   failed, mirrored to runs.status.
5. `request_resume` (API): validates the hold is actually cleared for case
   holds, records an idempotent resume/retry command, wakes the run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime

from langgraph.types import Command
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

import app.accounting as accounting
from app.models import CAPABILITY_RUN_AGENTS, CAPABILITY_RUN_RECONCILIATION
from app.models_ledger import Case, Run, Vendor
from app.models_orchestration import (
    OrchestrationCommand,
    OrchestrationRun,
    OrchestrationVendorRun,
)
from app.orchestration import document_extraction
from app.orchestration.checkpoints import get_checkpointer
from app.orchestration.reconcile_day import reconcile_bank_deposits_day
from app.orchestration.recorder import (
    GRAPH_VERSION,
    crash_point,
    record_event,
    run_once,
    session,
)
from app.orchestration.vendor_graph import build_graph

logger = logging.getLogger("orchestration")

RETRY_BACKOFF_SECONDS = 15.0

_graph = None
_graph_lock = threading.Lock()


def get_graph():
    global _graph
    with _graph_lock:
        if _graph is None:
            _graph = build_graph(get_checkpointer())
        return _graph


def reset_graph_for_tests() -> None:
    global _graph
    with _graph_lock:
        _graph = None


def thread_id_for(run_id: str, vendor_id: str) -> str:
    return f"nightly:{run_id}:{vendor_id}"


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": 200}


# ---------------------------------------------------------------------------
# Commands (API side)
# ---------------------------------------------------------------------------


class CommandConflictError(ValueError):
    def __init__(self, message: str, run_id: str | None = None) -> None:
        super().__init__(message)
        self.run_id = run_id


class ResumeRefusedError(ValueError):
    def __init__(self, code: str, details: dict | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = details or {}


def _payload_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


@dataclass
class StartRequest:
    festival_id: str
    business_date: date
    is_final_period: bool = False
    max_concurrency: int = 4
    budget_usd: float = 0.0
    explain_cases: bool = False

    def payload(self) -> dict:
        return {
            "festival_id": self.festival_id,
            "business_date": self.business_date.isoformat(),
            "is_final_period": self.is_final_period,
            "max_concurrency": self.max_concurrency,
            "budget_usd": round(float(self.budget_usd), 4),
            "explain_cases": self.explain_cases,
        }


def start_run(
    db: Session,
    *,
    request: StartRequest,
    command_id: str,
    actor_id: str,
    actor_capability: str = CAPABILITY_RUN_RECONCILIATION,
) -> tuple[OrchestrationRun, bool]:
    """Returns (run, created). Caller commits."""
    payload = request.payload()
    digest = _payload_hash(payload)
    existing_cmd = db.get(OrchestrationCommand, command_id)
    if existing_cmd is not None:
        if existing_cmd.kind != "start" or existing_cmd.payload_hash != digest:
            raise CommandConflictError("command_id reused with a different payload")
        return db.get(OrchestrationRun, existing_cmd.run_id), False
    same_day = db.execute(
        select(OrchestrationRun).where(
            OrchestrationRun.festival_id == request.festival_id,
            OrchestrationRun.business_date == request.business_date,
        )
    ).scalar_one_or_none()
    if same_day is not None:
        raise CommandConflictError(
            "a nightly close run already exists for this festival/business_date",
            run_id=same_day.id,
        )
    run = Run(festival_id=request.festival_id, status="QUEUED")
    db.add(run)
    db.flush()
    orch = OrchestrationRun(
        id=run.id,
        festival_id=request.festival_id,
        business_date=request.business_date,
        is_final_period=request.is_final_period,
        status="QUEUED",
        requested_by=actor_id,
        requested_capability=actor_capability,
        start_command_id=command_id,
        graph_version=GRAPH_VERSION,
        max_concurrency=request.max_concurrency,
        budget_usd=request.budget_usd,
        spent_usd=0,
        reserved_usd=0,
        explain_cases=request.explain_cases,
        needs_work=True,
        wake_seq=1,
    )
    db.add(orch)
    db.flush()
    db.add(
        OrchestrationCommand(
            id=command_id,
            run_id=run.id,
            vendor_id=None,
            kind="start",
            payload_hash=digest,
            payload=payload,
            actor_id=actor_id,
            actor_capability=actor_capability,
            status="APPLIED",
            applied_at=datetime.now(UTC),
        )
    )
    db.flush()
    record_event(
        db,
        run_id=run.id,
        vendor_id=None,
        event_type="orchestration.run_queued",
        actor_id=actor_id,
        actor_type="human",
        actor_capability=actor_capability,
        command_id=command_id,
        payload=payload,
        lineage_refs=[{"ref_kind": "run", "ref_id": run.id}],
    )
    return orch, True


def request_resume(
    db: Session,
    *,
    run_id: str,
    vendor_id: str,
    command_id: str,
    actor_id: str,
    actor_capability: str,
    note: str | None = None,
) -> tuple[OrchestrationCommand, bool]:
    """Records a resume (HELD vendor) or retry (FAILED vendor) command.
    Refuses (422) when a case that holds the vendor is still OPEN -- the
    human acts through the existing case endpoints first. Other holds
    (payout approval, unknown rail outcome) are re-checked by the graph
    itself, which simply holds again if nothing changed. Caller commits."""
    payload = {"run_id": run_id, "vendor_id": vendor_id, "note": note}
    digest = _payload_hash(payload)
    existing = db.get(OrchestrationCommand, command_id)
    if existing is not None:
        if existing.payload_hash != digest:
            raise CommandConflictError("command_id reused with a different payload")
        return existing, False
    row = db.execute(
        select(OrchestrationVendorRun)
        .where(
            OrchestrationVendorRun.run_id == run_id,
            OrchestrationVendorRun.vendor_id == vendor_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise LookupError("vendor is not part of this run")
    if row.status == "HELD":
        kind = "resume"
        still_open = [
            r["case_id"]
            for r in row.hold_reasons or []
            if r.get("case_id")
            and (c := db.get(Case, r["case_id"])) is not None
            and c.status == "OPEN"
        ]
        if still_open:
            raise ResumeRefusedError("hold_not_cleared", {"open_case_ids": still_open})
    elif row.status == "FAILED":
        kind = "retry"
    else:
        raise ResumeRefusedError("vendor_not_waiting", {"status": row.status})
    pending = db.execute(
        select(OrchestrationCommand).where(
            OrchestrationCommand.run_id == run_id,
            OrchestrationCommand.vendor_id == vendor_id,
            OrchestrationCommand.status == "PENDING",
        )
    ).scalar_one_or_none()
    if pending is not None:
        raise ResumeRefusedError("command_already_pending", {"command_id": pending.id})
    cmd = OrchestrationCommand(
        id=command_id,
        run_id=run_id,
        vendor_id=vendor_id,
        kind=kind,
        payload_hash=digest,
        payload=payload,
        actor_id=actor_id,
        actor_capability=actor_capability,
        status="PENDING",
    )
    db.add(cmd)
    db.execute(
        update(OrchestrationRun)
        .where(OrchestrationRun.id == run_id)
        .values(needs_work=True, wake_seq=OrchestrationRun.wake_seq + 1)
    )
    db.flush()
    record_event(
        db,
        run_id=run_id,
        vendor_id=vendor_id,
        event_type=f"orchestration.{kind}_requested",
        actor_id=actor_id,
        actor_type="human",
        actor_capability=actor_capability,
        command_id=command_id,
        payload={"kind": kind, "note": note, "held_at": row.held_at},
    )
    return cmd, True


# ---------------------------------------------------------------------------
# Worker claims
# ---------------------------------------------------------------------------


def claim_next_run(worker_id: str, lease_seconds: float) -> str | None:
    with session() as db:
        row = db.execute(
            text(
                """
                UPDATE orchestration_runs SET lease_owner = :w,
                    lease_expires_at = now() + make_interval(secs => :s),
                    claim_count = claim_count + 1
                WHERE id = (
                    SELECT id FROM orchestration_runs
                    WHERE needs_work AND status NOT IN ('CANCELLED')
                      AND (lease_expires_at IS NULL OR lease_expires_at < now())
                    ORDER BY created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                RETURNING id, claim_count
                """
            ),
            {"w": worker_id, "s": lease_seconds},
        ).first()
        if row is None:
            db.commit()
            return None
        run_id, claims = row
        record_event(
            db,
            run_id=run_id,
            vendor_id=None,
            event_type="orchestration.run_claimed",
            payload={"worker_id": worker_id, "claim_count": claims, "lease_seconds": lease_seconds},
        )
        db.commit()
        return run_id


def _renew_lease(run_id: str, worker_id: str, lease_seconds: float) -> bool:
    with session() as db:
        ok = db.execute(
            text(
                "UPDATE orchestration_runs SET lease_expires_at = now() + make_interval(secs => :s)"
                " WHERE id = :id AND lease_owner = :w RETURNING id"
            ),
            {"s": lease_seconds, "id": run_id, "w": worker_id},
        ).first()
        db.commit()
        return ok is not None


def _release_lease(
    run_id: str, worker_id: str, seen_wake_seq: int, *, retry_after_seconds: float | None = None
) -> None:
    """Clean pass: drop the lease and clear needs_work unless a command
    arrived meanwhile (wake_seq moved). Failed pass: keep needs_work and
    leave a short back-off lease so the retry is bounded, not a hot loop."""
    with session() as db:
        if retry_after_seconds is None:
            db.execute(
                text(
                    "UPDATE orchestration_runs SET lease_owner = NULL, lease_expires_at = NULL,"
                    " needs_work = CASE WHEN wake_seq = :seq THEN false ELSE needs_work END"
                    " WHERE id = :id AND lease_owner = :w"
                ),
                {"seq": seen_wake_seq, "id": run_id, "w": worker_id},
            )
        else:
            db.execute(
                text(
                    "UPDATE orchestration_runs SET lease_owner = NULL,"
                    " lease_expires_at = now() + make_interval(secs => :s)"
                    " WHERE id = :id AND lease_owner = :w"
                ),
                {"s": retry_after_seconds, "id": run_id, "w": worker_id},
            )
        db.commit()


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def _festival_steps(orch: OrchestrationRun) -> None:
    run_id, bd = orch.id, orch.business_date

    # First, because the vendor threads' TERMS_RESOLVED milestone reads the
    # claims this produces. Idempotent, budgeted against this run, and not
    # fatal -- see app/orchestration/document_extraction.py.
    def extract(db) -> dict:
        return document_extraction.extract_for_run(db, run_id=run_id, festival_id=orch.festival_id)

    extracted, _replayed = run_once(run_id, None, document_extraction.STEP, extract)
    with session() as db:
        record_event(
            db,
            run_id=run_id,
            vendor_id=None,
            event_type="orchestration.documents_extracted",
            actor_id=orch.requested_by,
            actor_capability=CAPABILITY_RUN_AGENTS,
            payload=extracted,
        )
        db.commit()
    crash_point("festival:after_extract")

    def bank(db) -> dict:
        return reconcile_bank_deposits_day(
            db,
            run=db.get(Run, run_id),
            festival_id=orch.festival_id,
            business_date=bd,
            actor_id=orch.requested_by,
            actor_capability=CAPABILITY_RUN_RECONCILIATION,
        )

    run_once(run_id, None, "festival:reconcile_bank_deposits", bank)
    crash_point("festival:after_bank")

    def post_day(db) -> dict:
        sales = accounting.post_sales_for_day(
            db,
            festival_id=orch.festival_id,
            business_date=bd,
            actor_id=orch.requested_by,
            actor_capability="close_books",
        )
        deposits = accounting.post_processor_deposits(
            db,
            festival_id=orch.festival_id,
            business_date=bd,
            actor_id=orch.requested_by,
            actor_capability="close_books",
        )
        return {
            "sales_entries": [r.base_key for r in sales if r.created],
            "deposit_entries": [r.base_key for r in deposits if r.created],
        }

    try:
        run_once(run_id, None, "festival:post_day_sales_and_deposits", post_day)
    except accounting.BusinessDateClosedError:
        with session() as db:
            record_event(
                db,
                run_id=run_id,
                vendor_id=None,
                event_type="orchestration.festival_step_skipped",
                payload={"step": "post_day_sales_and_deposits", "reason": "business_date_closed"},
            )
            db.commit()


def _ensure_vendor_rows(orch: OrchestrationRun) -> list[OrchestrationVendorRun]:
    with session() as db:
        vendors = (
            db.execute(
                select(Vendor)
                .where(Vendor.festival_id == orch.festival_id, Vendor.status == "active")
                .order_by(Vendor.name)
            )
            .scalars()
            .all()
        )
        for v in vendors:
            db.execute(
                text(
                    "INSERT INTO orchestration_vendor_runs (id, run_id, vendor_id, thread_id,"
                    " milestone, status, hold_reasons, retryable, version)"
                    " VALUES (md5(random()::text), :r, :v, :t, 'PENDING', 'PENDING', '[]',"
                    " false, 1) ON CONFLICT (run_id, vendor_id) DO NOTHING"
                ),
                {"r": orch.id, "v": v.id, "t": thread_id_for(orch.id, v.id)},
            )
        db.commit()
        rows = (
            db.execute(
                select(OrchestrationVendorRun).where(OrchestrationVendorRun.run_id == orch.id)
            )
            .scalars()
            .all()
        )
        db.expunge_all()
        return list(rows)


def _initial_state(orch: OrchestrationRun, vendor_id: str) -> dict:
    return {
        "run_id": orch.id,
        "vendor_id": vendor_id,
        "festival_id": orch.festival_id,
        "business_date": orch.business_date.isoformat(),
        "is_final_period": orch.is_final_period,
        "actor_id": orch.requested_by,
        "explain_cases": bool(orch.explain_cases),
        "milestone": "PENDING",
        "hold_seq": 0,
        "payout_cents": 0,
    }


def drive_vendor(orch: OrchestrationRun, row: OrchestrationVendorRun) -> str:
    """Advance one vendor's thread as far as it can go now. Returns what
    was done (for logs/tests)."""
    graph = get_graph()
    config = _config(row.thread_id)
    snapshot = graph.get_state(config)
    with session() as db:
        cmd = db.execute(
            select(OrchestrationCommand)
            .where(
                OrchestrationCommand.run_id == orch.id,
                OrchestrationCommand.vendor_id == row.vendor_id,
                OrchestrationCommand.status == "PENDING",
            )
            .order_by(OrchestrationCommand.created_at)
        ).scalar_one_or_none()
        cmd_id, cmd_kind = (cmd.id, cmd.kind) if cmd else (None, None)

    if not snapshot.values:
        action, arg = "start", _initial_state(orch, row.vendor_id)
    elif snapshot.interrupts:
        if cmd_kind != "resume":
            return "waiting"
        action, arg = "resume", Command(resume={"command_id": cmd_id})
    elif not snapshot.next:
        if cmd_id:
            _mark_command(cmd_id, "OBSOLETE")
        return "done"
    elif row.status == "FAILED":
        if cmd_kind != "retry":
            return "failed_waiting_retry"
        action, arg = "retry", None
    else:
        action, arg = "continue", None

    if action == "retry":
        with session() as db:
            record_event(
                db,
                run_id=orch.id,
                vendor_id=row.vendor_id,
                event_type="orchestration.vendor_retry",
                command_id=cmd_id,
                payload={"next": list(snapshot.next)},
            )
            db.commit()
        _mark_command(cmd_id, "APPLIED")
    elif action == "continue":
        with session() as db:
            record_event(
                db,
                run_id=orch.id,
                vendor_id=row.vendor_id,
                event_type="orchestration.vendor_recovered",
                payload={
                    "next": list(snapshot.next),
                    "note": "continuing from last checkpoint after an interrupted worker",
                },
            )
            db.commit()
    graph.invoke(arg, config, durability="sync")
    return action


def _mark_command(command_id: str, status: str) -> None:
    with session() as db:
        db.execute(
            update(OrchestrationCommand)
            .where(OrchestrationCommand.id == command_id)
            .values(status=status, applied_at=datetime.now(UTC))
        )
        db.commit()


def summarize_run(run_id: str) -> str:
    with session() as db:
        orch = db.get(OrchestrationRun, run_id)
        statuses = [
            s
            for (s,) in db.execute(
                select(OrchestrationVendorRun.status).where(OrchestrationVendorRun.run_id == run_id)
            ).all()
        ]
        if statuses and all(s == "COMPLETED" for s in statuses):
            new = "COMPLETED"
        elif "HELD" in statuses:
            new = "WAITING_REVIEW"
        elif "FAILED" in statuses:
            new = "PARTIALLY_COMPLETE"
        elif not statuses:
            new = "COMPLETED"
        else:
            new = "RUNNING"
        if new != orch.status:
            old = orch.status
            orch.status = new
            orch.version += 1
            run = db.get(Run, run_id)
            run.status = new
            run.version += 1
            if new == "COMPLETED":
                orch.completed_at = run.completed_at = datetime.now(UTC)
            counts: dict[str, int] = {}
            for s in statuses:
                counts[s] = counts.get(s, 0) + 1
            record_event(
                db,
                run_id=run_id,
                vendor_id=None,
                event_type="orchestration.run_status_changed",
                payload={"from": old, "to": new, "vendor_counts": counts},
            )
        db.commit()
        return new


def execute_run(run_id: str, *, worker_id: str, lease_seconds: float = 30.0) -> str:
    """Runs one pass over the run while holding its lease."""
    stop = threading.Event()

    def heartbeat() -> None:
        while not stop.wait(lease_seconds / 3):
            if not _renew_lease(run_id, worker_id, lease_seconds):
                logger.warning(json.dumps({"event": "lease_lost", "run_id": run_id}))
                return

    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()
    seen_seq = 1
    failed = False
    try:
        with session() as db:
            orch = db.get(OrchestrationRun, run_id)
            seen_seq = orch.wake_seq
            if orch.status == "QUEUED":
                orch.status = "RUNNING"
                orch.started_at = datetime.now(UTC)
                run = db.get(Run, run_id)
                run.status, run.started_at = "RUNNING", orch.started_at
                record_event(
                    db,
                    run_id=run_id,
                    vendor_id=None,
                    event_type="orchestration.run_started",
                    payload={"worker_id": worker_id, "max_concurrency": orch.max_concurrency},
                )
            db.commit()
            db.refresh(orch)
            db.expunge(orch)
        _festival_steps(orch)
        rows = _ensure_vendor_rows(orch)

        def one(row: OrchestrationVendorRun) -> tuple[str, str]:
            try:
                return row.vendor_id, drive_vendor(orch, row)
            except Exception as exc:  # noqa: BLE001 -- one vendor never stops the others
                logger.warning(
                    json.dumps(
                        {
                            "event": "vendor_graph_error",
                            "run_id": run_id,
                            "vendor_id": row.vendor_id,
                            "error": f"{type(exc).__name__}: {exc}"[:300],
                        }
                    )
                )
                return row.vendor_id, "error"

        with ThreadPoolExecutor(max_workers=max(1, orch.max_concurrency)) as pool:
            results = dict(pool.map(one, rows))
        logger.info(json.dumps({"event": "run_pass", "run_id": run_id, "results": results}))
        return summarize_run(run_id)
    except Exception as exc:
        failed = True
        with session() as db:
            db.execute(
                update(OrchestrationRun)
                .where(OrchestrationRun.id == run_id)
                .values(last_error=f"{type(exc).__name__}: {exc}"[:1000])
            )
            record_event(
                db,
                run_id=run_id,
                vendor_id=None,
                event_type="orchestration.run_pass_failed",
                payload={"error_type": type(exc).__name__, "error": str(exc)[:500]},
            )
            db.commit()
        raise
    finally:
        stop.set()
        _release_lease(
            run_id,
            worker_id,
            seen_seq,
            retry_after_seconds=RETRY_BACKOFF_SECONDS if failed else None,
        )


def work_once(worker_id: str, lease_seconds: float = 30.0) -> str | None:
    run_id = claim_next_run(worker_id, lease_seconds)
    if run_id is None:
        return None
    execute_run(run_id, worker_id=worker_id, lease_seconds=lease_seconds)
    return run_id
