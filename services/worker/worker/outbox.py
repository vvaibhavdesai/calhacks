"""Outbox dispatcher loop.

The claim/dispatch/retry mechanics live in app/outbox_dispatch.py (next to
the domain code the handlers call); this is only the process loop, shaped
exactly like worker/orchestration.py so the two read the same way.

Several of these may run: claims take a lease under `FOR UPDATE SKIP
LOCKED`, and a repeated delivery after a lease expiry cannot repeat an
external effect (app/outbox_dispatch.py explains why).

Run standalone:
    uv run python -m worker.outbox            # loop
    uv run python -m worker.outbox --once     # one pass, exit
    uv run python -m worker.outbox --backlog  # print what is left, exit
Environment: DATABASE_URL (the encore_app role), AUTH_JWT_SECRET (settings
validation), OUTBOX_LEASE_SECONDS, OUTBOX_POLL_SECONDS, OUTBOX_BATCH,
OUTBOX_MAX_ATTEMPTS, OUTBOX_BACKOFF_SECONDS, OUTBOX_BACKOFF_CAP_SECONDS,
OUTBOX_RELEASE_WINDOW_ENABLED, OUTBOX_DEFAULT_SIMULATED_OUTCOME, WORKER_ID.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
from collections.abc import Callable

logger = logging.getLogger("worker.outbox")

DEFAULT_POLL_SECONDS = 2.0
# Longest a single pass should take; the health window has to cover it or a
# busy dispatcher reads as stale (see worker/main.py).
EXPECTED_PASS_SECONDS = 60.0


def run_loop(
    stop: threading.Event,
    *,
    once: bool = False,
    on_beat: Callable[[], None] | None = None,
) -> int:
    from app.outbox_dispatch import dispatch_once, worker_id

    poll = float(os.environ.get("OUTBOX_POLL_SECONDS", DEFAULT_POLL_SECONDS))
    me = worker_id()
    logger.info(json.dumps({"event": "outbox_dispatcher_started", "worker_id": me}))
    while not stop.is_set():
        if on_beat is not None:
            on_beat()
        try:
            stats = dispatch_once(me)
        except Exception as exc:  # noqa: BLE001 -- keep the loop alive; state is durable
            logger.error(
                json.dumps({"event": "outbox_pass_error", "error": f"{type(exc).__name__}: {exc}"})
            )
            stats = None
        if stats is not None and stats.claimed:
            logger.info(json.dumps({"event": "outbox_pass_done", **stats.as_dict()}))
        if once:
            return 0
        # A pass that found nothing waits; a full pass goes straight round
        # again so a large backlog drains without an artificial delay.
        if stats is None or stats.claimed == 0:
            stop.wait(poll)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--backlog", action="store_true", help="print the backlog and exit")
    args = parser.parse_args(argv)
    if args.backlog:
        from app.orchestration.recorder import session
        from app.outbox_dispatch import backlog

        with session() as db:
            print(json.dumps(backlog(db), indent=2))
        return 0
    return run_loop(threading.Event(), once=args.once)


if __name__ == "__main__":
    sys.exit(main())
