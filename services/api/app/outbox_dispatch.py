"""The outbox dispatcher: the process that actually delivers the intents
`UnitOfWork.enqueue_outbox` commits next to the domain change.

It is the ONLY process that contacts the payment rail. The nightly close
(app/orchestration/vendor_graph.py `pay`) releases a payout and stops; this
module submits it inside the 10 AM window, and the `payout.confirm` intent
it queues behind an accepted submission is what later confirms the payment
and re-issues the vendor statement as FINAL. A workflow payout therefore
reaches FAILED or UNKNOWN exactly as a hand-released one does.

Until this module existed every outbox intent stayed PENDING until a human
made an HTTP call, which is why a development database could sit on 541
undelivered intents (525 `payout.dispatch`, 16 `source.imported.notify`).

Claiming follows app/orchestration/coordinator.py's pattern rather than a
new one: `UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)`
plus a time-limited lease, so several worker processes share the backlog and
a worker that dies releases its work when the lease expires.

What makes a repeated delivery harmless -- and the lease is NOT it (C06:
"Outbox dispatcher may deliver repeatedly ... Simulator maintains its own
provider result keyed by idempotency identity; lookup resolves interrupted
calls"):

  * `payout.dispatch` -> app/payout_dispatch.py `dispatch_payout`, which
    looks the rail record up by the payout's stable `idempotency_key`
    FIRST. A dispatcher killed between the rail's own commit and the domain
    update leaves exactly that state; the next claim finds the record,
    reports `newly_submitted=False` and converges the payout and the intent
    to the outcome the rail already decided. It never submits twice and
    never invents a different outcome.
  * `dispatch_payout` is also a no-op for a payout that is no longer
    DISPATCH_PENDING/UNKNOWN, so a redelivery after the domain update
    committed writes nothing at all -- not even a duplicate audit event.
  * `payout.confirm` -> `handle_payout_confirm`, the rail reporting back on
    a submission it accepted. Both A07 confirmations are no-ops once their
    timestamp is set (not even a duplicate audit event), and the statement
    it re-issues is idempotent by content digest.
  * `source.imported.notify` is an internal notification with no external
    effect; delivering it twice is only a repeated log line, and the
    intent's own status stops that.

Bounded retry and the dead letter: `outbox_dispatch_state` carries
`attempts` and `next_attempt_at`. A handler that raises, or a rail outcome
that cannot progress on its own (a DELAYED record stays DELAYED until an
explicit reconciliation -- app/rail_simulator.py `resolve_outcome`), is
retried with exponential back-off up to OUTBOX_MAX_ATTEMPTS and then goes
DEAD_LETTER and is never claimed again. The intent itself stays PENDING on
purpose, so A10's OUTBOX_BACKLOG / stale-intent alert keeps pointing at it
until a human resolves it.

Intent types with no handler here are never claimed and never
dead-lettered; they stay PENDING and keep being reported by
OPS_OUTBOX_UNCONSUMED_INTENT_TYPES.

Release window: AGENT_HANDOFF.md's approved schedule is "statements by
9 AM, simulated payout release 10 AM Pacific". A `payout.dispatch` intent
whose night's release window has not opened yet is DEFERRED (no attempt
consumed) until it does. See `release_window_opens_at`.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

import app.accounting as accounting
import app.finance_reports as finance_reports
import app.rail_simulator as rail_simulator
from app import demo_clock, payout_dispatch
from app.models_ledger import OutboxIntent, Payout
from app.orchestration.recorder import crash_point, session
from app.uow import UnitOfWork

logger = logging.getLogger("outbox.dispatch")

DISPATCHER_CAPABILITY = "outbox_dispatcher"
# The rail confirming its own submission is not an actor with authority; it
# is the simulated counterparty reporting back. A07's confirmations take the
# capability, never a user id.
SIMULATED_RAIL = "simulated_rail"
CONFIRM_INTENT_TYPE = "payout.confirm"

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF_SECONDS = 15.0
DEFAULT_BACKOFF_CAP_SECONDS = 900.0
DEFAULT_BATCH = 20
DEFAULT_LEASE_SECONDS = 60.0
# Festival-local time of day the simulated payout release opens.
DEFAULT_RELEASE_WINDOW_LOCAL = "10:00"


def worker_id() -> str:
    return os.environ.get("WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def max_attempts() -> int:
    return max(1, _int_env("OUTBOX_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS))


def backoff_seconds(attempts: int) -> float:
    """Exponential, capped. `attempts` is the count AFTER this failure."""
    base = _float_env("OUTBOX_BACKOFF_SECONDS", DEFAULT_BACKOFF_SECONDS)
    cap = _float_env("OUTBOX_BACKOFF_CAP_SECONDS", DEFAULT_BACKOFF_CAP_SECONDS)
    return min(cap, base * (2 ** max(0, attempts - 1)))


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

RESOLVED = "RESOLVED"
RETRY = "RETRY"
DEFER = "DEFER"
DEAD_LETTER = "DEAD_LETTER"


@dataclass
class Delivery:
    """A handler's verdict. RESOLVED: nothing more to deliver (whatever the
    business outcome was). RETRY: try again later, spending one attempt.
    DEFER: not due yet -- try again at `not_before`, spending NO attempt.
    DEAD_LETTER: unusable now and retrying cannot help."""

    disposition: str
    detail: dict[str, Any] = field(default_factory=dict)
    not_before: datetime | None = None


Handler = Callable[[Session, OutboxIntent], Delivery]


def release_window_opens_at(business_date: date) -> datetime:
    """The night of `business_date` is settled and paid the FOLLOWING
    morning (statements 9 AM, simulated release 10 AM Pacific), so the
    window for business_date D opens at D+1 10:00 festival-local."""
    raw = os.environ.get("SCHEDULE_PAYOUT_RELEASE_AT", DEFAULT_RELEASE_WINDOW_LOCAL)
    try:
        hour_s, _, minute_s = raw.partition(":")
        hour, minute = int(hour_s), int(minute_s or 0)
    except ValueError:
        hour, minute = 10, 0
    return demo_clock.local_time_to_utc(business_date + timedelta(days=1), hour, minute)


def _release_window_enabled() -> bool:
    return os.environ.get("OUTBOX_RELEASE_WINDOW_ENABLED", "1").strip().lower() not in (
        "0",
        "false",
        "off",
    )


def handle_payout_dispatch(db: Session, intent: OutboxIntent) -> Delivery:
    """Deliver one released payout to the simulated rail.

    `simulate_outcome` comes from the intent payload when the enqueuer put
    one there, otherwise from OUTBOX_DEFAULT_SIMULATED_OUTCOME (ACCEPTED).
    It is never random: a demo that wants a FAILED or UNKNOWN payout says so
    in the data. The rail ignores it entirely once a record exists for the
    key, so a retry can never rewrite an outcome."""
    payout_id = (intent.payload or {}).get("payout_id")
    if not payout_id:
        return Delivery(DEAD_LETTER, {"reason": "payload has no payout_id"})

    payout = db.get(Payout, payout_id)
    if payout is None:
        return Delivery(DEAD_LETTER, {"reason": "payout no longer exists", "payout_id": payout_id})

    if _release_window_enabled():
        opens_at = release_window_opens_at(payout.business_date)
        clock = demo_clock.read_state(db)
        scheduling_now = clock.instant()
        if scheduling_now < opens_at:
            # `next_attempt_at` is compared against the database's own
            # now(), so it must be a WALL instant even when the window was
            # judged on a demo clock. Under a demo clock the wait is also
            # capped: a frozen clock would otherwise never reach the window,
            # and the operator advancing it must take effect promptly.
            remaining = (opens_at - scheduling_now).total_seconds()
            if clock.enabled and clock.mode != demo_clock.MODE_WALL:
                remaining = min(remaining, _float_env("OUTBOX_DEFER_RECHECK_SECONDS", 60.0))
            return Delivery(
                DEFER,
                {
                    "reason": "payout_release_window_closed",
                    "business_date": payout.business_date.isoformat(),
                    "opens_at": opens_at.isoformat(),
                    "clock_mode": clock.mode if clock.enabled else demo_clock.MODE_WALL,
                },
                not_before=datetime.now(UTC) + timedelta(seconds=max(1.0, remaining)),
            )

    requested = (intent.payload or {}).get("simulate_outcome") or os.environ.get(
        "OUTBOX_DEFAULT_SIMULATED_OUTCOME", "ACCEPTED"
    )
    if requested not in rail_simulator.VALID_OUTCOMES:
        return Delivery(DEAD_LETTER, {"reason": f"unknown simulate_outcome {requested!r}"})

    outcome = payout_dispatch.dispatch_payout(
        db,
        payout_id=payout_id,
        actor_id=None,
        actor_capability=DISPATCHER_CAPABILITY,
        simulate_outcome=requested,
    )
    # Same transaction as the domain update the rail's answer caused, for
    # the same reason release_payout enqueues this intent's own predecessor
    # that way: a submission whose confirmation intent did not commit with
    # it would be a payment nothing is waiting on.
    confirm_intent_id = None
    if outcome.payout.state == "SUBMITTED":
        confirm_intent_id = _enqueue_confirmation(db, outcome.payout)
    db.commit()

    # The rail committed on its own before the domain update above (see
    # app/rail_simulator.py). Dying here is the interesting crash: the next
    # claim must converge, not resubmit.
    crash_point("outbox:after_dispatch", payout_id)

    detail = {
        "payout_id": payout_id,
        "outcome": outcome.outcome,
        "payout_state": outcome.payout.state,
        "newly_submitted": outcome.newly_submitted,
        "changed": outcome.changed,
        "confirmation_intent_id": confirm_intent_id,
    }
    if outcome.outcome is None:
        # Not dispatchable: already SUBMITTED/CONFIRMED, CANCELLED or never
        # released. Nothing this dispatcher can deliver, now or later.
        detail["reason"] = "payout_not_dispatchable"
        return Delivery(RESOLVED, detail)
    if outcome.outcome == "DELAYED":
        # The rail record keeps outcome DELAYED until an explicit
        # reconciliation resolves it, so a retry only re-reads it. Retry a
        # bounded number of times, then hand it to a human.
        return Delivery(RETRY, detail)
    # ACCEPTED / FAILED / UNKNOWN are all final as far as delivery goes.
    # UNKNOWN deliberately keeps the cash reserved (C06) and is resolved by
    # payout_dispatch.reconcile_unknown_payout, never by redelivering.
    return Delivery(RESOLVED, detail)


def _enqueue_confirmation(db: Session, payout: Payout) -> str:
    """The rail's callback, in a simulator that has no callbacks. Keyed off
    the payout's own stable idempotency key, so a redelivered dispatch can
    never queue a second confirmation."""
    intent = UnitOfWork(db).enqueue_outbox(
        idempotency_key=f"{payout.idempotency_key}:confirm",
        intent_type=CONFIRM_INTENT_TYPE,
        target="rail_simulator",
        payload={
            "payout_id": payout.id,
            "vendor_id": payout.vendor_id,
            "business_date": payout.business_date.isoformat(),
        },
        event_id=None,
    )
    return intent.id


def handle_payout_confirm(db: Session, intent: OutboxIntent) -> Delivery:
    """Bank debit, then the payout itself (C06 tracks them as separate
    events), then the statement that can now say FINAL.

    Harmless to deliver repeatedly: both confirmations are no-ops once
    their timestamp is set, and `issue_vendor_statement` is idempotent by
    content digest, so a second delivery issues no new version.

    This is also where a payment that did NOT arrive stops: FAILED,
    CANCELLED and UNKNOWN are not confirmable, now or later, and UNKNOWN
    resolves only through `reconcile_unknown_payout` (C06 -- never through
    a confirmation side door).
    """
    payout_id = (intent.payload or {}).get("payout_id")
    if not payout_id:
        return Delivery(DEAD_LETTER, {"reason": "payload has no payout_id"})
    payout = db.get(Payout, payout_id)
    if payout is None:
        return Delivery(DEAD_LETTER, {"reason": "payout no longer exists", "payout_id": payout_id})
    if payout.state not in ("SUBMITTED", "CONFIRMED"):
        return Delivery(
            RESOLVED,
            {
                "payout_id": payout_id,
                "reason": "payout_not_confirmable",
                "payout_state": payout.state,
            },
        )

    vendor_id, business_date = payout.vendor_id, payout.business_date
    try:
        _payout, debit_newly = payout_dispatch.confirm_bank_debit(
            db, payout_id=payout_id, actor_id=None, actor_capability=SIMULATED_RAIL
        )
        db.commit()
        crash_point("outbox:after_bank_debit", payout_id)
        payout, rail_newly = payout_dispatch.confirm_payout_rail(
            db, payout_id=payout_id, actor_id=None, actor_capability=SIMULATED_RAIL
        )
        db.commit()
    except payout_dispatch.PayoutBlockedError as exc:
        # Lost a race with a cancellation or a reconciliation between the
        # state check above and the lock.
        db.rollback()
        return Delivery(
            RESOLVED,
            {"payout_id": payout_id, "reason": exc.reason_code, "details": exc.details},
        )
    except accounting.BusinessDateClosedError:
        # The books closed on a night whose payment was still in flight.
        # Retrying cannot reopen them; a human has to (and the intent stays
        # PENDING, so A10 keeps pointing at it).
        db.rollback()
        return Delivery(
            DEAD_LETTER,
            {
                "payout_id": payout_id,
                "reason": "business_date_closed_before_confirmation",
                "business_date": business_date.isoformat(),
            },
        )
    final_state = payout.state
    crash_point("outbox:after_payout_confirmed", payout_id)

    # "Statement can be provisional/held; payout confirmation updates it
    # afterward" (AGENT_HANDOFF). The close issued a PROVISIONAL statement
    # without waiting for the money; this is the part that makes it FINAL.
    statement_id = statement_status = None
    if final_state == "CONFIRMED":
        stmt, _new = finance_reports.issue_vendor_statement(
            db,
            vendor_id=vendor_id,
            business_date=business_date,
            demo_effective_at=None,
            actor_id=None,
            actor_capability="close_books",
        )
        db.commit()
        statement_id, statement_status = stmt.id, stmt.status

    # "Log intent before external effects and confirmed outcome after"
    # (AGENT_HANDOFF): this intent, and the dispatch intent it followed,
    # now both carry the outcome the rail reported.
    now = datetime.now(UTC)
    intent.status = "DISPATCHED"
    intent.dispatched_at = intent.dispatched_at or now
    intent.confirmed_at = now
    db.execute(
        text(
            "UPDATE outbox_intents SET confirmed_at = :now"
            " WHERE idempotency_key = :key AND confirmed_at IS NULL"
        ),
        {"now": now, "key": (intent.idempotency_key or "").removesuffix(":confirm")},
    )
    db.commit()
    return Delivery(
        RESOLVED,
        {
            "payout_id": payout_id,
            "payout_state": final_state,
            "bank_debit_newly_confirmed": debit_newly,
            "payout_newly_confirmed": rail_newly,
            "statement_id": statement_id,
            "statement_status": statement_status,
        },
    )


def handle_source_imported_notify(db: Session, intent: OutboxIntent) -> Delivery:
    """A02's import notification. `target` is "internal": there is no
    external system in v1, so delivery is a structured log line plus the
    intent's own DISPATCHED status, which is what stops it being redelivered
    and what takes it out of the ops backlog."""
    logger.info(
        json.dumps(
            {
                "event": "source_import_notified",
                "intent_id": intent.id,
                "source_document_id": (intent.payload or {}).get("source_document_id"),
                "version": (intent.payload or {}).get("version"),
                "status": (intent.payload or {}).get("status"),
            }
        )
    )
    intent.status = "DISPATCHED"
    intent.dispatched_at = datetime.now(UTC)
    intent.confirmed_at = intent.dispatched_at
    db.commit()
    return Delivery(RESOLVED, {"delivered_to": "internal_log"})


HANDLERS: dict[str, Handler] = {
    "payout.dispatch": handle_payout_dispatch,
    CONFIRM_INTENT_TYPE: handle_payout_confirm,
    "source.imported.notify": handle_source_imported_notify,
}


def handled_intent_types() -> list[str]:
    return sorted(HANDLERS)


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------

# One seed row per claimable intent. Separate from the claim so the claim
# itself stays the single short UPDATE the coordinator uses.
_SEED_SQL = text(
    """
    INSERT INTO outbox_dispatch_state (intent_id, status, attempts, claim_count, detail)
    SELECT i.id, 'PENDING', 0, 0, '{}'::jsonb
    FROM outbox_intents i
    WHERE i.status = 'PENDING' AND i.intent_type = ANY(:types)
    ORDER BY i.created_at
    LIMIT :limit
    ON CONFLICT (intent_id) DO NOTHING
    """
)

# `FOR UPDATE OF s` locks only the dispatcher's own row: the restricted
# encore_app role needs UPDATE on the locked table, and outbox_intents is
# an audit-adjacent table whose UPDATE rights should not have to widen for
# a worker claim.
_CLAIM_SQL = text(
    """
    UPDATE outbox_dispatch_state SET
        lease_owner = :worker,
        lease_expires_at = now() + make_interval(secs => :lease),
        claim_count = claim_count + 1,
        updated_at = now()
    WHERE intent_id = (
        SELECT s.intent_id
        FROM outbox_dispatch_state s
        JOIN outbox_intents i ON i.id = s.intent_id
        WHERE s.status = 'PENDING'
          AND i.status = 'PENDING'
          AND i.intent_type = ANY(:types)
          AND s.attempts < :max_attempts
          AND (s.lease_expires_at IS NULL OR s.lease_expires_at < now())
          AND (s.next_attempt_at IS NULL OR s.next_attempt_at <= now())
        ORDER BY i.created_at
        FOR UPDATE OF s SKIP LOCKED
        LIMIT 1
    )
    RETURNING intent_id, attempts
    """
)


# A worker killed between the handler's commit and its own bookkeeping
# leaves a PENDING claim row for an intent that is already delivered. The
# intent's status is authoritative, so converge the claim row instead of
# leaving it to look like outstanding work forever.
_REAP_SQL = text(
    """
    UPDATE outbox_dispatch_state s SET
        status = 'DONE',
        last_disposition = 'RESOLVED',
        lease_owner = NULL,
        lease_expires_at = NULL,
        resolved_at = now(),
        updated_at = now()
    WHERE s.status = 'PENDING'
      AND (s.lease_expires_at IS NULL OR s.lease_expires_at < now())
      AND EXISTS (
          SELECT 1 FROM outbox_intents i
          WHERE i.id = s.intent_id AND i.status <> 'PENDING'
      )
    """
)


def reap_delivered(db: Session) -> int:
    result = db.execute(_REAP_SQL)
    db.commit()
    return result.rowcount or 0


def seed_claimable(db: Session, *, limit: int = 500) -> int:
    result = db.execute(_SEED_SQL, {"types": handled_intent_types(), "limit": limit})
    db.commit()
    return result.rowcount or 0


def claim_one(db: Session, *, worker: str, lease_seconds: float) -> tuple[str, int] | None:
    row = db.execute(
        _CLAIM_SQL,
        {
            "worker": worker,
            "lease": lease_seconds,
            "types": handled_intent_types(),
            "max_attempts": max_attempts(),
        },
    ).first()
    db.commit()
    return (row[0], int(row[1])) if row else None


def _finish(
    db: Session,
    intent_id: str,
    *,
    status: str,
    attempts: int,
    disposition: str,
    detail: dict,
    next_attempt_at: datetime | None = None,
    last_error: str | None = None,
) -> None:
    db.execute(
        text(
            """
            UPDATE outbox_dispatch_state SET
                status = :status,
                attempts = :attempts,
                next_attempt_at = :next_attempt_at,
                last_error = :last_error,
                last_disposition = :disposition,
                detail = CAST(:detail AS jsonb),
                lease_owner = NULL,
                lease_expires_at = NULL,
                resolved_at = :resolved_at,
                updated_at = now()
            WHERE intent_id = :intent_id
            """
        ),
        {
            "intent_id": intent_id,
            "status": status,
            "attempts": attempts,
            "next_attempt_at": next_attempt_at,
            "last_error": None if last_error is None else last_error[:1000],
            "disposition": disposition,
            "detail": json.dumps(detail, default=str),
            "resolved_at": (datetime.now(UTC) if status in ("DONE", "DEAD_LETTER") else None),
        },
    )
    db.commit()


def _record_dead_letter(db: Session, intent: OutboxIntent, detail: dict) -> None:
    """A dead letter is an operational fact worth having in the flight
    recorder: the intent is still undelivered and now nobody is retrying
    it."""
    uow = UnitOfWork(db)
    uow.append_event(
        stream_id=f"outbox:{intent.id}",
        event_type="outbox.intent_dead_lettered",
        aggregate_id=intent.id,
        aggregate_version=1,
        actor_id=None,
        actor_type="system",
        actor_capability=DISPATCHER_CAPABILITY,
        occurred_at=datetime.now(UTC),
        payload={
            "intent_type": intent.intent_type,
            "target": intent.target,
            "idempotency_key": intent.idempotency_key,
            "detail": detail,
        },
        lineage_refs=[{"ref_kind": "outbox_intent", "ref_id": intent.id}],
    )
    db.commit()


@dataclass
class DispatchStats:
    claimed: int = 0
    resolved: int = 0
    retried: int = 0
    deferred: int = 0
    dead_lettered: int = 0

    def as_dict(self) -> dict:
        return {
            "claimed": self.claimed,
            "resolved": self.resolved,
            "retried": self.retried,
            "deferred": self.deferred,
            "dead_lettered": self.dead_lettered,
        }


def dispatch_claimed(db: Session, intent_id: str, attempts_before: int) -> str:
    """Runs one claimed intent's handler and records the result. Returns the
    disposition. Never raises for a handler failure: the attempt is spent
    and the intent is retried or dead-lettered."""
    intent = db.get(OutboxIntent, intent_id)
    if intent is None:  # pragma: no cover - FK ON DELETE CASCADE makes this unreachable
        _finish(
            db,
            intent_id,
            status="DONE",
            attempts=attempts_before,
            disposition=RESOLVED,
            detail={"reason": "intent row vanished"},
        )
        return RESOLVED

    handler = HANDLERS.get(intent.intent_type)
    if handler is None:  # pragma: no cover - the claim filters by type
        _finish(
            db,
            intent_id,
            status="DONE",
            attempts=attempts_before,
            disposition=RESOLVED,
            detail={"reason": f"no handler for {intent.intent_type}"},
        )
        return RESOLVED

    try:
        verdict = handler(db, intent)
    except Exception as exc:  # noqa: BLE001 -- a bad intent must not stop the loop
        db.rollback()
        attempts = attempts_before + 1
        error = f"{type(exc).__name__}: {exc}"
        exhausted = attempts >= max_attempts()
        verdict = Delivery(
            DEAD_LETTER if exhausted else RETRY,
            {"error": error[:500], "attempts": attempts},
        )
        logger.warning(
            json.dumps(
                {
                    "event": "outbox_handler_error",
                    "intent_id": intent_id,
                    "intent_type": intent.intent_type,
                    "attempts": attempts,
                    "error": error[:300],
                }
            )
        )
        _apply_verdict(db, intent, verdict, attempts=attempts, error=error)
        return verdict.disposition

    if verdict.disposition == DEFER:
        attempts = attempts_before
    else:
        attempts = attempts_before + 1
        if verdict.disposition == RETRY and attempts >= max_attempts():
            verdict = Delivery(DEAD_LETTER, {**verdict.detail, "reason": "retry budget exhausted"})
    _apply_verdict(db, intent, verdict, attempts=attempts, error=None)
    return verdict.disposition


def _apply_verdict(
    db: Session, intent: OutboxIntent, verdict: Delivery, *, attempts: int, error: str | None
) -> None:
    if verdict.disposition == RESOLVED:
        _finish(
            db,
            intent.id,
            status="DONE",
            attempts=attempts,
            disposition=RESOLVED,
            detail=verdict.detail,
        )
        return
    if verdict.disposition == DEFER:
        _finish(
            db,
            intent.id,
            status="PENDING",
            attempts=attempts,
            disposition=DEFER,
            detail=verdict.detail,
            next_attempt_at=verdict.not_before,
        )
        return
    if verdict.disposition == RETRY:
        _finish(
            db,
            intent.id,
            status="PENDING",
            attempts=attempts,
            disposition=RETRY,
            detail=verdict.detail,
            next_attempt_at=datetime.now(UTC) + timedelta(seconds=backoff_seconds(attempts)),
            last_error=error,
        )
        return
    _finish(
        db,
        intent.id,
        status="DEAD_LETTER",
        attempts=attempts,
        disposition=DEAD_LETTER,
        detail=verdict.detail,
        last_error=error,
    )
    _record_dead_letter(db, intent, verdict.detail)
    logger.error(
        json.dumps(
            {
                "event": "outbox_intent_dead_lettered",
                "intent_id": intent.id,
                "intent_type": intent.intent_type,
                "idempotency_key": intent.idempotency_key,
                "attempts": attempts,
                "detail": verdict.detail,
            },
            default=str,
        )
    )


def dispatch_once(
    worker: str | None = None,
    *,
    lease_seconds: float | None = None,
    batch: int | None = None,
) -> DispatchStats:
    """One pass: seed, then claim and deliver up to `batch` intents. Each
    intent gets its own session and its own commit, so a failure or a crash
    affects one intent, never the batch."""
    worker = worker or worker_id()
    # `is None`, not `or`: a caller that asks for a zero-length lease (a test
    # that wants its own claim to be immediately re-claimable) must not
    # silently get the 60-second default back.
    lease = (
        _float_env("OUTBOX_LEASE_SECONDS", DEFAULT_LEASE_SECONDS)
        if lease_seconds is None
        else lease_seconds
    )
    limit = _int_env("OUTBOX_BATCH", DEFAULT_BATCH) if batch is None else batch
    stats = DispatchStats()

    with session() as db:
        reap_delivered(db)
        seed_claimable(db, limit=max(limit * 10, 100))

    # One intent per pass at most: a retry or defer sets `next_attempt_at`
    # in the future so a re-claim should be impossible anyway, but a pass
    # that silently spent its whole batch on one intent would be a very
    # quiet bug.
    seen: set[str] = set()
    for _ in range(limit):
        with session() as db:
            claim = claim_one(db, worker=worker, lease_seconds=lease)
            if claim is None:
                break
            intent_id, attempts_before = claim
            if intent_id in seen:
                logger.warning(
                    json.dumps({"event": "outbox_reclaimed_in_same_pass", "intent_id": intent_id})
                )
                break
            seen.add(intent_id)
            stats.claimed += 1
            crash_point("outbox:after_claim", intent_id)
            disposition = dispatch_claimed(db, intent_id, attempts_before)
            if disposition == RESOLVED:
                stats.resolved += 1
            elif disposition == RETRY:
                stats.retried += 1
            elif disposition == DEFER:
                stats.deferred += 1
            else:
                stats.dead_lettered += 1
    return stats


def backlog(db: Session) -> dict:
    """Operator view: what the dispatcher thinks is left to do."""
    rows = db.execute(
        text(
            """
            SELECT i.intent_type, i.status AS intent_status,
                   COALESCE(s.status, 'UNSEEN') AS dispatch_status, count(*)
            FROM outbox_intents i
            LEFT JOIN outbox_dispatch_state s ON s.intent_id = i.id
            GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
            """
        )
    ).all()
    return {
        "by_type": [
            {
                "intent_type": r[0],
                "intent_status": r[1],
                "dispatch_status": r[2],
                "count": int(r[3]),
            }
            for r in rows
        ],
        "handled_intent_types": handled_intent_types(),
    }
