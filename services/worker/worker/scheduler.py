"""Scheduler loop: ticks the schedule in app/scheduler.py.

The loop itself is deliberately dumb -- it wakes up, asks which occurrences
are due on the current (possibly demo) clock, and tries to claim them.
Everything that matters (exactly-once claiming across processes, leases,
bounded retry, what each job does) is in app/scheduler.py.

Run standalone:
    uv run python -m worker.scheduler             # loop
    uv run python -m worker.scheduler --once      # one tick, exit
    uv run python -m worker.scheduler --upcoming  # show the schedule, exit
Environment: DATABASE_URL, AUTH_JWT_SECRET, SCHEDULE_FESTIVAL_ID (unset =
the scheduler stays inert), SCHEDULE_ACTOR_EMAIL, SCHEDULE_STATEMENTS_AT,
SCHEDULE_PAYOUT_RELEASE_AT, SCHEDULE_FIRST_BUSINESS_DATE,
SCHEDULE_LAST_BUSINESS_DATE, SCHEDULE_POLL_SECONDS, SCHEDULE_CATCHUP_HOURS,
SCHEDULE_MAX_ATTEMPTS, SCHEDULE_LEASE_SECONDS, SCHEDULE_MAX_CONCURRENCY,
SCHEDULE_BUDGET_USD, SCHEDULE_EXPLAIN_CASES, WORKER_ID.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from collections.abc import Callable

logger = logging.getLogger("worker.scheduler")

DEFAULT_POLL_SECONDS = 15.0
# A tick may start a nightly close, which returns as soon as the run row is
# committed -- the worker that executes the graph is a different loop.
EXPECTED_TICK_SECONDS = 60.0


def run_loop(
    stop: threading.Event,
    *,
    once: bool = False,
    on_beat: Callable[[], None] | None = None,
) -> int:
    from app.outbox_dispatch import worker_id
    from app.scheduler import load_config, tick

    poll = float(os.environ.get("SCHEDULE_POLL_SECONDS", DEFAULT_POLL_SECONDS))
    me = worker_id()
    config = load_config()
    logger.info(
        json.dumps(
            {
                "event": "scheduler_started",
                "worker_id": me,
                "enabled": config.enabled,
                "festival_id": config.festival_id,
                "statements_at": config.statements_at.isoformat(),
                "payout_release_at": config.payout_release_at.isoformat(),
            }
        )
    )
    if not config.enabled:
        logger.warning(
            json.dumps(
                {
                    "event": "scheduler_inert",
                    "reason": "SCHEDULE_FESTIVAL_ID is not set -- nothing will be scheduled",
                }
            )
        )
    while not stop.is_set():
        if on_beat is not None:
            on_beat()
        try:
            # Re-read the configuration every tick: an operator changing the
            # schedule should not have to restart the worker.
            tick(me, config=load_config())
        except Exception as exc:  # noqa: BLE001 -- keep the loop alive; state is durable
            logger.error(
                json.dumps(
                    {"event": "scheduler_tick_error", "error": f"{type(exc).__name__}: {exc}"}
                )
            )
        if once:
            return 0
        stop.wait(poll)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--upcoming", action="store_true", help="print the schedule and exit")
    args = parser.parse_args(argv)
    if args.upcoming:
        from app.scheduler import upcoming

        print(json.dumps(upcoming(), indent=2))
        return 0
    return run_loop(threading.Event(), once=args.once)


if __name__ == "__main__":
    sys.exit(main())
