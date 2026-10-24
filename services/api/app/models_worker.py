"""Autonomy-layer records: outbox dispatcher claims, schedule firings and
the demo clock.

Three small tables, all of them operational state rather than history. The
history of what the dispatcher and the scheduler actually did stays in
`business_events` (the flight recorder) exactly as the orchestration
coordinator already does it -- these rows only answer "what should a worker
pick up next, and who holds it right now".

`outbox_dispatch_state` is the dispatcher's claim/retry row, one per
`outbox_intents` row it has ever looked at. It is deliberately SEPARATE
from `outbox_intents`:

  - `outbox_intents` is the domain record written inside the same
    transaction as the domain change and the audit event (app/uow.py). Its
    `status` means what the *delivery* ended up being (DISPATCHED / FAILED /
    UNKNOWN), set by the handler that owns the effect -- not "a worker is
    holding this right now", which is what a lease means.
  - a dispatcher-owned table can be mutable without widening what the
    restricted `encore_app` role may UPDATE on a table the audit trail
    depends on, and the claim's `SELECT ... FOR UPDATE OF s SKIP LOCKED`
    then only needs UPDATE on this table.

A dead letter stays PENDING in `outbox_intents` on purpose: the intent
genuinely has not been delivered, so A10's OUTBOX_BACKLOG / stale-intent
alert (app/ops.py) must keep firing until a human deals with it. Only
`outbox_dispatch_state.status` goes DEAD_LETTER, which is what stops the
retry loop.

`schedule_firings` is the scheduler's exactly-once claim: UNIQUE(job_name,
scheduled_for) means the INSERT itself is the mutual exclusion, so N worker
processes fire one occurrence once between them. The row then carries a
lease so an occurrence whose worker died is retried instead of being lost.

`demo_clock` is a single row shared by every process, so "advance the demo
to 9 AM on 2026-10-17" is one write rather than a restart of each process
with a different environment variable. It feeds scheduling decisions ONLY.
Audit timestamps (`recorded_at`, `occurred_at`, journal dates) stay on the
wall clock, per C01: "Demo clock never overwrites audit-recording wall
time."
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models import _new_id


class OutboxDispatchState(Base):
    __tablename__ = "outbox_dispatch_state"

    intent_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("outbox_intents.id", ondelete="CASCADE"), primary_key=True
    )
    # PENDING (claimable) | DONE (the handler resolved it) | DEAD_LETTER
    # (retry budget spent or an unusable payload; a human must act). The
    # intent's own status is the delivery outcome and is set by the handler.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claim_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # A lease, not a safety mechanism: it keeps two healthy workers off the
    # same intent. What actually prevents a repeated external effect is the
    # handler's idempotency key (the rail simulator's lookup-by-key).
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Earliest instant this intent may be claimed again: a retry back-off,
    # or the business date's payout-release window when it is still shut.
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_disposition: Mapped[str | None] = mapped_column(String(20), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScheduleFiring(Base):
    __tablename__ = "schedule_firings"
    __table_args__ = (
        UniqueConstraint("job_name", "scheduled_for", name="uq_schedule_firing_occurrence"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    job_name: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    # The occurrence instant in UTC, derived from the job's festival-local
    # time of day. Unique with job_name: this is the exactly-once key.
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The festival-local business date the occurrence acts on (the night
    # being closed), when the job has one.
    business_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # CLAIMED (in flight, retryable once the lease expires) | DONE |
    # SKIPPED (nothing to do, e.g. outside the festival's dates) | FAILED
    # (retry budget spent).
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="CLAIMED")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Which clock decided this occurrence was due: "wall" or a demo-clock mode.
    clock_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="wall")
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DemoClock(Base):
    """One row, id = 'singleton'. See app/demo_clock.py."""

    __tablename__ = "demo_clock"

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default="singleton")
    # wall | frozen | offset
    mode: Mapped[str] = mapped_column(String(10), nullable=False, default="wall")
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    offset_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
