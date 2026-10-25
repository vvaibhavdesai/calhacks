"""The scheduler: fires the nightly close at the designed times instead of
waiting for somebody to POST /api/v1/orchestration/runs.

The approved schedule (AGENT_HANDOFF.md, confirmed in CONTRACTS.md's
"Confirmed defaults": "9 AM statements / 10 AM simulated payout, Pacific
time") is two festival-local times of day, both configurable:

  09:00 America/Los_Angeles  `nightly_close`   -- start the close of LAST
                                                 night. The vendor graph
                                                 issues every vendor's
                                                 statement, provisional or
                                                 held, so the 9 AM statement
                                                 never waits for a human.
  10:00 America/Los_Angeles  `payout_release`  -- the night's simulated
                                                 payout release window
                                                 opens. The outbox
                                                 dispatcher will not deliver
                                                 a `payout.dispatch` intent
                                                 for business date D before
                                                 D+1 10:00 (see
                                                 app/outbox_dispatch.py
                                                 `release_window_opens_at`);
                                                 this job records the
                                                 window opening and wakes
                                                 the run.

Known deviation, stated rather than papered over: A06's vendor graph calls
`dispatch_payout` itself inside its `pay` node, so a run started by the 9 AM
job dispatches those payouts immediately instead of going through the outbox
and its 10 AM window. Closing that gap is a change to
app/orchestration/vendor_graph.py (drop the in-node dispatch and let the
dispatcher deliver the intent `release_payout` already enqueues), which is
another task's file. See docs/progress/A11-worker-scheduler-handoff.md.

Exactly-once across N workers: an occurrence is identified by (job_name,
scheduled_for) and claimed by INSERTing that pair -- the UNIQUE constraint
is the mutual exclusion, so no lock, no leader election and no assumption of
a singleton process (CONTRACTS.md: "Multiple worker processes need database
claiming, not an assumption of singleton execution"). The claimed row then
carries a lease, so an occurrence whose worker died is retried rather than
silently lost, and the work itself is idempotent anyway: the nightly close
is started with a deterministic command_id, which is C03's Idempotency-Key.

Authority: the scheduler has none of its own. It acts as the user named by
SCHEDULE_ACTOR_EMAIL and that user's capabilities are re-read from the
database at fire time. A payout over the $5,000 automatic daily limit still
holds for a finance lead, because A06's approve node never uses
`finance_lead` on the starter's behalf.

The clock is app/demo_clock.py, so a demo can drive 2026-10-16..18 forward
deterministically. With no demo clock set it is the wall clock.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app import demo_clock
from app.auth import get_user_capability_codes
from app.models import CAPABILITY_RUN_RECONCILIATION, User
from app.models_orchestration import OrchestrationRun
from app.models_worker import ScheduleFiring
from app.orchestration import coordinator
from app.orchestration.recorder import record_event, session

logger = logging.getLogger("worker.scheduler")

JOB_NIGHTLY_CLOSE = "nightly_close"
JOB_PAYOUT_RELEASE = "payout_release"

DEFAULT_STATEMENTS_AT = "09:00"
DEFAULT_PAYOUT_RELEASE_AT = "10:00"
# The festival in AGENT_HANDOFF.md ("Golden Coast Music Festival ... October
# 16-18, 2026"). Business dates outside this range have nothing to close, so
# an occurrence for one is recorded as SKIPPED instead of starting an empty
# run every morning forever.
DEFAULT_FIRST_BUSINESS_DATE = "2026-10-16"
DEFAULT_LAST_BUSINESS_DATE = "2026-10-18"
DEFAULT_CATCHUP_HOURS = 24.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_LEASE_SECONDS = 300.0
DEFAULT_RETRY_BACKOFF_SECONDS = 60.0


class SchedulerNotConfiguredError(RuntimeError):
    pass


def _parse_local_time(raw: str, fallback: str) -> time:
    for candidate in (raw, fallback):
        try:
            hour_s, _, minute_s = (candidate or "").partition(":")
            return time(int(hour_s), int(minute_s or 0))
        except ValueError:
            continue
    raise ValueError(f"unparseable schedule time {raw!r}")


def _parse_date(raw: str, fallback: str) -> date:
    for candidate in (raw, fallback):
        try:
            return date.fromisoformat(candidate)
        except (TypeError, ValueError):
            continue
    raise ValueError(f"unparseable schedule date {raw!r}")


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


@dataclass(frozen=True)
class ScheduleConfig:
    festival_id: str | None
    statements_at: time
    payout_release_at: time
    first_business_date: date
    last_business_date: date
    actor_email: str | None
    max_concurrency: int
    budget_usd: float
    explain_cases: bool
    catchup_hours: float
    max_attempts: int
    lease_seconds: float

    @property
    def enabled(self) -> bool:
        return bool(self.festival_id)


def load_config() -> ScheduleConfig:
    return ScheduleConfig(
        festival_id=(os.environ.get("SCHEDULE_FESTIVAL_ID") or "").strip() or None,
        statements_at=_parse_local_time(
            os.environ.get("SCHEDULE_STATEMENTS_AT", ""), DEFAULT_STATEMENTS_AT
        ),
        payout_release_at=_parse_local_time(
            os.environ.get("SCHEDULE_PAYOUT_RELEASE_AT", ""), DEFAULT_PAYOUT_RELEASE_AT
        ),
        first_business_date=_parse_date(
            os.environ.get("SCHEDULE_FIRST_BUSINESS_DATE", ""), DEFAULT_FIRST_BUSINESS_DATE
        ),
        last_business_date=_parse_date(
            os.environ.get("SCHEDULE_LAST_BUSINESS_DATE", ""), DEFAULT_LAST_BUSINESS_DATE
        ),
        actor_email=(os.environ.get("SCHEDULE_ACTOR_EMAIL") or "").strip() or None,
        max_concurrency=_int_env("SCHEDULE_MAX_CONCURRENCY", 4),
        budget_usd=_float_env("SCHEDULE_BUDGET_USD", 0.0),
        explain_cases=os.environ.get("SCHEDULE_EXPLAIN_CASES", "0").strip().lower()
        in ("1", "true", "yes"),
        catchup_hours=_float_env("SCHEDULE_CATCHUP_HOURS", DEFAULT_CATCHUP_HOURS),
        max_attempts=max(1, _int_env("SCHEDULE_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)),
        lease_seconds=_float_env("SCHEDULE_LEASE_SECONDS", DEFAULT_LEASE_SECONDS),
    )


# ---------------------------------------------------------------------------
# Occurrences
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Occurrence:
    job_name: str
    scheduled_for: datetime  # UTC instant
    business_date: date  # the night this occurrence acts on


def _occurrences_for(
    job_name: str, at_local: time, *, now: datetime, catchup_hours: float
) -> list[Occurrence]:
    """Every occurrence of a daily festival-local time in
    (now - catchup, now], oldest first. The morning close of night D happens
    on D+1, so the business date is the occurrence's local date minus a day."""
    earliest = now - timedelta(hours=catchup_hours)
    local_today = demo_clock.business_date_of(now)
    out: list[Occurrence] = []
    days = int(catchup_hours // 24) + 2
    for delta in range(days, -1, -1):
        local_day = local_today - timedelta(days=delta)
        instant = demo_clock.local_time_to_utc(local_day, at_local.hour, at_local.minute)
        if earliest < instant <= now:
            out.append(
                Occurrence(
                    job_name=job_name,
                    scheduled_for=instant,
                    business_date=local_day - timedelta(days=1),
                )
            )
    return out


def due_occurrences(config: ScheduleConfig, *, now: datetime) -> list[Occurrence]:
    jobs = (
        (JOB_NIGHTLY_CLOSE, config.statements_at),
        (JOB_PAYOUT_RELEASE, config.payout_release_at),
    )
    out: list[Occurrence] = []
    for name, at_local in jobs:
        out.extend(_occurrences_for(name, at_local, now=now, catchup_hours=config.catchup_hours))
    return sorted(out, key=lambda o: (o.scheduled_for, o.job_name))


# ---------------------------------------------------------------------------
# Claiming an occurrence
# ---------------------------------------------------------------------------

_CLAIM_NEW_SQL = text(
    """
    INSERT INTO schedule_firings
        (id, job_name, scheduled_for, business_date, status, attempts, clock_mode,
         lease_owner, lease_expires_at, detail)
    VALUES
        (md5(random()::text || clock_timestamp()::text), :job, :scheduled_for, :business_date,
         'CLAIMED', 1, :clock_mode, :worker, now() + make_interval(secs => :lease), '{}'::jsonb)
    ON CONFLICT (job_name, scheduled_for) DO NOTHING
    RETURNING id, attempts
    """
)

_CLAIM_STALE_SQL = text(
    """
    UPDATE schedule_firings SET
        lease_owner = :worker,
        lease_expires_at = now() + make_interval(secs => :lease),
        attempts = attempts + 1,
        updated_at = now()
    WHERE id = (
        SELECT id FROM schedule_firings
        WHERE job_name = :job AND scheduled_for = :scheduled_for
          AND status = 'CLAIMED'
          AND (lease_expires_at IS NULL OR lease_expires_at < now())
          AND attempts < :max_attempts
        FOR UPDATE SKIP LOCKED
        LIMIT 1
    )
    RETURNING id, attempts
    """
)


def claim_occurrence(
    db: Session, occurrence: Occurrence, *, worker: str, config: ScheduleConfig, clock_mode: str
) -> tuple[str, int] | None:
    """Returns (firing_id, attempts) for the worker that may run it, else
    None (somebody else owns it, it is finished, or it is out of retries)."""
    params = {
        "job": occurrence.job_name,
        "scheduled_for": occurrence.scheduled_for,
        "business_date": occurrence.business_date,
        "worker": worker,
        "lease": config.lease_seconds,
        "clock_mode": clock_mode,
    }
    row = db.execute(_CLAIM_NEW_SQL, params).first()
    if row is None:
        row = db.execute(_CLAIM_STALE_SQL, {**params, "max_attempts": config.max_attempts}).first()
    db.commit()
    return (row[0], int(row[1])) if row else None


def _complete(
    db: Session,
    firing_id: str,
    *,
    status: str,
    detail: dict,
    run_id: str | None = None,
    last_error: str | None = None,
    retry_after_seconds: float | None = None,
) -> None:
    db.execute(
        update(ScheduleFiring)
        .where(ScheduleFiring.id == firing_id)
        .values(
            status=status,
            detail=detail,
            run_id=run_id,
            last_error=None if last_error is None else last_error[:1000],
            lease_owner=None,
            lease_expires_at=(
                None
                if retry_after_seconds is None
                else datetime.now(UTC) + timedelta(seconds=retry_after_seconds)
            ),
            completed_at=datetime.now(UTC) if status in ("DONE", "SKIPPED", "FAILED") else None,
        )
    )
    db.commit()


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


@dataclass
class JobResult:
    status: str  # DONE | SKIPPED | FAILED (FAILED = out of attempts)
    detail: dict
    run_id: str | None = None


def _resolve_actor(db: Session, config: ScheduleConfig) -> tuple[str, str]:
    """The configured user, re-reading its capabilities now (not at config
    time). Raises SchedulerNotConfiguredError with a specific reason."""
    if not config.actor_email:
        raise SchedulerNotConfiguredError("SCHEDULE_ACTOR_EMAIL is not set")
    user = db.execute(select(User).where(User.email == config.actor_email)).scalar_one_or_none()
    if user is None:
        raise SchedulerNotConfiguredError(f"no user {config.actor_email!r}")
    if not user.is_active:
        raise SchedulerNotConfiguredError(f"user {config.actor_email!r} is not active")
    codes = get_user_capability_codes(db, user.id)
    if CAPABILITY_RUN_RECONCILIATION not in codes:
        raise SchedulerNotConfiguredError(
            f"user {config.actor_email!r} lacks {CAPABILITY_RUN_RECONCILIATION}"
        )
    return user.id, CAPABILITY_RUN_RECONCILIATION


def run_nightly_close(db: Session, occurrence: Occurrence, config: ScheduleConfig) -> JobResult:
    bd = occurrence.business_date
    if not (config.first_business_date <= bd <= config.last_business_date):
        return JobResult(
            "SKIPPED",
            {
                "reason": "business_date outside the configured festival range",
                "business_date": bd.isoformat(),
                "range": [
                    config.first_business_date.isoformat(),
                    config.last_business_date.isoformat(),
                ],
            },
        )
    actor_id, capability = _resolve_actor(db, config)
    command_id = f"schedule:{occurrence.job_name}:{config.festival_id}:{bd.isoformat()}"
    request = coordinator.StartRequest(
        festival_id=config.festival_id,
        business_date=bd,
        is_final_period=bd == config.last_business_date,
        max_concurrency=config.max_concurrency,
        budget_usd=config.budget_usd,
        explain_cases=config.explain_cases,
    )
    try:
        orch, created = coordinator.start_run(
            db,
            request=request,
            command_id=command_id,
            actor_id=actor_id,
            actor_capability=capability,
        )
        db.commit()
    except coordinator.CommandConflictError as exc:
        # Somebody (or an earlier occurrence with a different payload)
        # already opened this night's run. One run per festival/business_date
        # is A06's rule; the schedule does not get a second one.
        db.rollback()
        return JobResult(
            "DONE",
            {"reason": str(exc), "already_running": True, "business_date": bd.isoformat()},
            run_id=exc.run_id,
        )
    return JobResult(
        "DONE",
        {
            "created": created,
            "business_date": bd.isoformat(),
            "is_final_period": request.is_final_period,
            "command_id": command_id,
            "actor_id": actor_id,
        },
        run_id=orch.id,
    )


def open_payout_release_window(
    db: Session, occurrence: Occurrence, config: ScheduleConfig
) -> JobResult:
    """The 10 AM window. Enforcement is time-based in the dispatcher, so
    this job's job is to make the opening visible and to wake the night's
    run, which is what moves anything that was waiting on it."""
    bd = occurrence.business_date
    orch = db.execute(
        select(OrchestrationRun).where(
            OrchestrationRun.festival_id == config.festival_id,
            OrchestrationRun.business_date == bd,
        )
    ).scalar_one_or_none()
    if orch is None:
        return JobResult(
            "SKIPPED",
            {
                "reason": "no nightly-close run for this business date",
                "business_date": bd.isoformat(),
            },
        )
    db.execute(
        update(OrchestrationRun)
        .where(OrchestrationRun.id == orch.id)
        .values(needs_work=True, wake_seq=OrchestrationRun.wake_seq + 1)
    )
    record_event(
        db,
        run_id=orch.id,
        vendor_id=None,
        event_type="schedule.payout_window_opened",
        actor_capability="scheduler",
        payload={
            "business_date": bd.isoformat(),
            "scheduled_for": occurrence.scheduled_for.isoformat(),
            "window_local_time": config.payout_release_at.isoformat(),
        },
    )
    db.commit()
    return JobResult("DONE", {"woke_run": True, "business_date": bd.isoformat()}, run_id=orch.id)


JOBS: dict[str, Callable[[Session, Occurrence, ScheduleConfig], JobResult]] = {
    JOB_NIGHTLY_CLOSE: run_nightly_close,
    JOB_PAYOUT_RELEASE: open_payout_release_window,
}


# ---------------------------------------------------------------------------
# One tick
# ---------------------------------------------------------------------------


def tick(worker: str, *, config: ScheduleConfig | None = None) -> list[dict]:
    """Fire every due, unclaimed occurrence. Safe to call from several
    processes at once and safe to call often: an occurrence already DONE is
    not re-fired, and the work itself is idempotent on its command_id."""
    config = config or load_config()
    if not config.enabled:
        return []
    fired: list[dict] = []
    with session() as db:
        clock = demo_clock.read_state(db)
        now = clock.instant()
        occurrences = due_occurrences(config, now=now)

    for occurrence in occurrences:
        with session() as db:
            claim = claim_occurrence(
                db,
                occurrence,
                worker=worker,
                config=config,
                clock_mode=clock.mode if clock.enabled else demo_clock.MODE_WALL,
            )
            if claim is None:
                continue
            firing_id, attempts = claim
            job = JOBS[occurrence.job_name]
            try:
                result = job(db, occurrence, config)
            except Exception as exc:  # noqa: BLE001 -- one occurrence never stops the tick
                db.rollback()
                error = f"{type(exc).__name__}: {exc}"
                exhausted = attempts >= config.max_attempts
                _complete(
                    db,
                    firing_id,
                    status="FAILED" if exhausted else "CLAIMED",
                    detail={"error": error[:500], "attempts": attempts},
                    last_error=error,
                    retry_after_seconds=(
                        None
                        if exhausted
                        else _float_env(
                            "SCHEDULE_RETRY_BACKOFF_SECONDS", DEFAULT_RETRY_BACKOFF_SECONDS
                        )
                    ),
                )
                logger.error(
                    json.dumps(
                        {
                            "event": "schedule_job_error",
                            "job": occurrence.job_name,
                            "scheduled_for": occurrence.scheduled_for.isoformat(),
                            "attempts": attempts,
                            "exhausted": exhausted,
                            "error": error[:300],
                        }
                    )
                )
                fired.append(
                    {
                        "job": occurrence.job_name,
                        "scheduled_for": occurrence.scheduled_for.isoformat(),
                        "status": "FAILED" if exhausted else "RETRY",
                        "error": error[:300],
                    }
                )
                continue
            _complete(
                db,
                firing_id,
                status=result.status,
                detail=result.detail,
                run_id=result.run_id,
            )
        entry = {
            "job": occurrence.job_name,
            "scheduled_for": occurrence.scheduled_for.isoformat(),
            "business_date": occurrence.business_date.isoformat(),
            "status": result.status,
            "run_id": result.run_id,
            "detail": result.detail,
        }
        logger.info(json.dumps({"event": "schedule_job_fired", **entry}, default=str))
        fired.append(entry)
    return fired


def upcoming(config: ScheduleConfig | None = None, *, days: int = 3) -> list[dict]:
    """Operator view: the next few occurrences on the current clock."""
    config = config or load_config()
    with session() as db:
        now = demo_clock.now(db)
        rows = {
            (r.job_name, r.scheduled_for): r
            for r in db.execute(select(ScheduleFiring)).scalars().all()
        }
    out: list[dict] = []
    local_today = demo_clock.business_date_of(now)
    for delta in range(-1, days):
        day = local_today + timedelta(days=delta)
        for name, at_local in (
            (JOB_NIGHTLY_CLOSE, config.statements_at),
            (JOB_PAYOUT_RELEASE, config.payout_release_at),
        ):
            instant = demo_clock.local_time_to_utc(day, at_local.hour, at_local.minute)
            firing = rows.get((name, instant))
            out.append(
                {
                    "job": name,
                    "scheduled_for": instant.isoformat(),
                    "scheduled_for_local": instant.astimezone(demo_clock.FESTIVAL_TZ).isoformat(),
                    "business_date": (day - timedelta(days=1)).isoformat(),
                    "due": instant <= now,
                    "firing_status": firing.status if firing else None,
                }
            )
    return sorted(out, key=lambda e: e["scheduled_for"])
