"""Flight recorder helpers, the effect ledger and crash points.

Effect ledger (the resume-safety mechanism)
-------------------------------------------
LangGraph re-executes a node from its start whenever a run resumes from a
checkpoint taken BEFORE that node finished -- e.g. the worker died after the
node's database commit but before LangGraph saved the next checkpoint.
`run_once` makes such re-execution harmless: the domain effect and an
`orchestration_steps` row (UNIQUE(run_id, vendor_id, step)) commit in ONE
transaction, so

  * crash before the commit  -> nothing happened; the re-run does it once;
  * crash after the commit   -> the re-run finds the row and returns the
                                recorded result without touching the domain;
  * two workers at once      -> the loser's INSERT violates the unique
                                constraint, its whole transaction (including
                                its domain writes) rolls back, and it reads
                                the winner's result.

Effects that cannot share our transaction (the simulated rail commits on
its own, C06) rely on the existing A07 idempotency keys instead; see
vendor_graph.pay.

Events
------
Every node start/end/failure, hold and resume is appended through
`UnitOfWork.append_event` (hash-chained, append-only for encore_app), with
graph/library/model versions. Payloads carry ids, codes and amounts only --
never document text, prompts, hidden reasoning or credentials.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_session_factory
from app.models_ledger import BusinessEvent
from app.models_orchestration import OrchestrationStep
from app.orchestration.checkpoints import library_versions
from app.uow import UnitOfWork

GRAPH_VERSION = "nightly-close-v1"


@contextmanager
def session() -> Iterator[Session]:
    db = get_session_factory()()
    try:
        yield db
    finally:
        db.close()


def stream_for(run_id: str, vendor_id: str | None) -> str:
    return f"orchestration:{run_id}:{vendor_id}" if vendor_id else f"orchestration:{run_id}"


def versions(extra: dict | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"graph_version": GRAPH_VERSION, **library_versions()}
    if extra:
        out.update(extra)
    return out


def record_event(
    db: Session,
    *,
    run_id: str,
    vendor_id: str | None,
    event_type: str,
    payload: dict,
    actor_id: str | None = None,
    actor_type: str = "system",
    actor_capability: str | None = None,
    command_id: str | None = None,
    extra_versions: dict | None = None,
    lineage_refs: list[dict] | None = None,
) -> BusinessEvent:
    """Appends on the caller's session; the caller commits."""
    stream = stream_for(run_id, vendor_id)
    seq = db.execute(
        select(func.count()).select_from(BusinessEvent).where(BusinessEvent.stream_id == stream)
    ).scalar_one()
    return UnitOfWork(db).append_event(
        stream_id=stream,
        event_type=event_type,
        aggregate_id=vendor_id or run_id,
        aggregate_version=seq + 1,
        actor_id=actor_id,
        actor_type=actor_type,
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        run_id=run_id,
        vendor_id=vendor_id,
        command_id=command_id,
        correlation_id=run_id,
        versions=versions(extra_versions),
        payload=payload,
        lineage_refs=lineage_refs,
    )


def get_step(db: Session, run_id: str, vendor_id: str | None, step: str) -> dict | None:
    row = db.execute(
        select(OrchestrationStep).where(
            OrchestrationStep.run_id == run_id,
            OrchestrationStep.vendor_id == (vendor_id or ""),
            OrchestrationStep.step == step,
        )
    ).scalar_one_or_none()
    return None if row is None else row.result


def run_once(
    run_id: str,
    vendor_id: str | None,
    step: str,
    effect: Callable[[Session], dict],
) -> tuple[dict, bool]:
    """Returns (result, replayed). `effect` must not commit; it writes on the
    session it is given and returns a JSON-safe result."""
    with session() as db:
        done = get_step(db, run_id, vendor_id, step)
        if done is not None:
            return done, True
        try:
            result = effect(db)
            db.add(
                OrchestrationStep(
                    run_id=run_id, vendor_id=vendor_id or "", step=step, result=result
                )
            )
            db.commit()
            return result, False
        except IntegrityError:
            db.rollback()
            done = get_step(db, run_id, vendor_id, step)
            if done is None:
                raise
            return done, True
        except BaseException:
            db.rollback()
            raise


# ---------------------------------------------------------------------------
# Crash points (fault injection for the worker-crash verification)
# ---------------------------------------------------------------------------


def crash_point(name: str, vendor_id: str | None = None) -> None:
    """ORCHESTRATION_CRASH_POINTS="pay:after_release@<vendor_id>,after:post"
    makes the process die at that point: ORCHESTRATION_CRASH_MODE=exit
    (default) calls os._exit(137) -- no cleanup, like SIGKILL -- and
    ORCHESTRATION_CRASH_MODE=hang blocks forever so a test can SIGKILL the
    worker from outside. Disabled when APP_ENV=production."""
    spec = os.environ.get("ORCHESTRATION_CRASH_POINTS")
    if not spec or os.environ.get("APP_ENV") == "production":
        return
    for item in spec.split(","):
        point, _, vid = item.strip().partition("@")
        if point == name and (not vid or vid == vendor_id):
            marker = os.environ.get("ORCHESTRATION_CRASH_MARKER")
            if marker:
                with open(marker, "w") as fh:
                    fh.write(f"{name}@{vendor_id}\n")
            if os.environ.get("ORCHESTRATION_CRASH_MODE", "exit") == "hang":
                while True:
                    time.sleep(3600)
            os._exit(137)
