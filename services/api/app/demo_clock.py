"""The demo clock: one shared, deterministic "what time does the system
think it is" for SCHEDULING decisions.

Why it exists: the festival is 2026-10-16..18 plus a post-festival final
period, and the nightly close fires at a festival-local time of day. Driving
that from the wall clock means a demo can only ever show whatever today
happens to be. The clock lets an operator say "it is now 2026-10-17 08:59
Pacific", advance a minute, and watch the 9 AM close fire for real.

What it is NOT, deliberately (C01/C06, CONTRACTS.md: "Demo clock never
overwrites audit-recording wall time"):

  - it does not change `recorded_at`, `occurred_at`, event hashes, journal
    dates, lease expiry or any other audit/timing value. Those stay on the
    database's own `now()` and `datetime.now(UTC)` exactly as before.
  - nothing imports it except the scheduler (app/scheduler.py) and the
    operator CLI (`python -m worker.clock`). A reader who wants to know
    whether a demo clock could have distorted the books only has to check
    those two call sites.

Modes:
  wall    -- `now()` is the wall clock. The default; nothing is stored.
  frozen  -- `now()` is exactly `frozen_at`; time does not pass until
             somebody advances it. The deterministic mode for a scripted
             demo.
  offset  -- `now()` is wall + `offset_seconds`; time passes normally but
             shifted onto the festival timeline.

Inert in production, the same way app/orchestration/recorder.py gates its
crash points: with `APP_ENV=production` (or `DEMO_CLOCK_MODE=off`) `now()`
returns the wall clock whatever the row says, and every mutator raises.
A production deployment that somehow has a stored demo clock therefore
behaves as if it did not.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models_worker import DemoClock

# The festival's business timezone (C01: business_date is YYYY-MM-DD in
# America/Los_Angeles). app/history.py has the same constant for its own
# knowledge-time folds; this module does not import from it, to keep the
# dependency direction one-way.
FESTIVAL_TZ = ZoneInfo("America/Los_Angeles")

SINGLETON_ID = "singleton"
MODE_WALL = "wall"
MODE_FROZEN = "frozen"
MODE_OFFSET = "offset"
VALID_MODES = (MODE_WALL, MODE_FROZEN, MODE_OFFSET)


class DemoClockDisabledError(RuntimeError):
    """Raised by a mutator when the demo clock is inert (production, or
    DEMO_CLOCK_MODE=off). Deliberately not a silent no-op: an operator who
    tries to move the clock in production should be told it did nothing."""


@dataclass(frozen=True)
class ClockState:
    mode: str
    frozen_at: datetime | None
    offset_seconds: int
    note: str | None
    updated_by: str | None
    enabled: bool

    def instant(self, *, wall: datetime | None = None) -> datetime:
        wall = wall or datetime.now(UTC)
        if not self.enabled or self.mode == MODE_WALL:
            return wall
        if self.mode == MODE_FROZEN and self.frozen_at is not None:
            return self.frozen_at.astimezone(UTC)
        if self.mode == MODE_OFFSET:
            return wall + timedelta(seconds=self.offset_seconds)
        return wall

    def as_dict(self, *, wall: datetime | None = None) -> dict:
        wall = wall or datetime.now(UTC)
        now_ = self.instant(wall=wall)
        return {
            "enabled": self.enabled,
            "mode": self.mode if self.enabled else MODE_WALL,
            "stored_mode": self.mode,
            "frozen_at": self.frozen_at.isoformat() if self.frozen_at else None,
            "offset_seconds": self.offset_seconds,
            "note": self.note,
            "updated_by": self.updated_by,
            "wall_now": wall.isoformat(),
            "now": now_.isoformat(),
            "now_local": now_.astimezone(FESTIVAL_TZ).isoformat(),
            "business_date": business_date_of(now_).isoformat(),
            "skew_seconds": round((now_ - wall).total_seconds()),
        }


def enabled() -> bool:
    """False -> the stored row is ignored entirely and mutators refuse."""
    if os.environ.get("APP_ENV") == "production":
        return False
    return os.environ.get("DEMO_CLOCK_MODE", "enabled").strip().lower() not in ("off", "0", "false")


def business_date_of(instant: datetime) -> date:
    """The festival-local calendar date of an instant (C01)."""
    return instant.astimezone(FESTIVAL_TZ).date()


def local_time_to_utc(day: date, hour: int, minute: int) -> datetime:
    """The UTC instant of a festival-local wall time. ZoneInfo resolves a
    DST-ambiguous local time with fold=0 (the first of the two); the demo
    schedule's 9/10 AM times are never ambiguous."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=FESTIVAL_TZ).astimezone(UTC)


def read_state(db: Session) -> ClockState:
    """The stored clock is read even when the clock is inert, so `status()`
    can say "a frozen clock is stored here and being ignored" rather than
    quietly reporting a wall clock. `ClockState.instant()` is what refuses
    to apply it."""
    is_enabled = enabled()
    row = db.execute(select(DemoClock).where(DemoClock.id == SINGLETON_ID)).scalar_one_or_none()
    if row is None:
        return ClockState(
            mode=MODE_WALL,
            frozen_at=None,
            offset_seconds=0,
            note=None,
            updated_by=None,
            enabled=is_enabled,
        )
    return ClockState(
        mode=row.mode,
        frozen_at=row.frozen_at,
        offset_seconds=int(row.offset_seconds),
        note=row.note,
        updated_by=row.updated_by,
        enabled=is_enabled,
    )


def now(db: Session) -> datetime:
    """Scheduling "now", in UTC. Never use this for an audit timestamp."""
    return read_state(db).instant()


def status(db: Session) -> dict:
    return read_state(db).as_dict()


# ---------------------------------------------------------------------------
# Mutators (operator-driven; see `python -m worker.clock`)
# ---------------------------------------------------------------------------


def _require_enabled() -> None:
    if not enabled():
        raise DemoClockDisabledError(
            "the demo clock is inert here (APP_ENV=production or DEMO_CLOCK_MODE=off); "
            "refusing to pretend the clock moved"
        )


def _upsert(db: Session, **values) -> ClockState:
    row = db.execute(select(DemoClock).where(DemoClock.id == SINGLETON_ID)).scalar_one_or_none()
    if row is None:
        row = DemoClock(id=SINGLETON_ID)
        db.add(row)
    for key, value in values.items():
        setattr(row, key, value)
    db.flush()
    db.commit()
    return read_state(db)


def freeze_at(db: Session, instant: datetime, *, actor: str | None = None, note: str | None = None):
    """Stop the clock at `instant`. The deterministic demo mode."""
    _require_enabled()
    if instant.tzinfo is None:
        raise ValueError("instant must be timezone-aware")
    return _upsert(
        db,
        mode=MODE_FROZEN,
        frozen_at=instant.astimezone(UTC),
        offset_seconds=0,
        note=note,
        updated_by=actor,
    )


def advance(db: Session, seconds: float, *, actor: str | None = None) -> ClockState:
    """Move the clock forward (negative moves it back). A frozen clock stays
    frozen at the new instant; a wall clock becomes an offset clock, so
    advancing never silently stops time."""
    _require_enabled()
    state = read_state(db)
    if state.mode == MODE_FROZEN and state.frozen_at is not None:
        return _upsert(
            db,
            mode=MODE_FROZEN,
            frozen_at=state.frozen_at.astimezone(UTC) + timedelta(seconds=seconds),
            updated_by=actor,
        )
    return _upsert(
        db,
        mode=MODE_OFFSET,
        frozen_at=None,
        offset_seconds=int(state.offset_seconds + seconds),
        updated_by=actor,
    )


def set_offset(db: Session, seconds: float, *, actor: str | None = None) -> ClockState:
    _require_enabled()
    return _upsert(
        db, mode=MODE_OFFSET, frozen_at=None, offset_seconds=int(seconds), updated_by=actor
    )


def resume_wall_clock(db: Session, *, actor: str | None = None) -> ClockState:
    _require_enabled()
    return _upsert(
        db, mode=MODE_WALL, frozen_at=None, offset_seconds=0, note=None, updated_by=actor
    )
