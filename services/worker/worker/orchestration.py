"""Nightly-close worker loop (A06 stage 3).

Polls for orchestration runs that need work (queued, resume/retry command
pending, or a lease abandoned by a dead worker), claims one with a lease
and executes a pass. Several worker processes may run: claims use
`FOR UPDATE SKIP LOCKED` + leases, and every effect is protected by the
effect ledger and A07/A08 idempotency keys, so a duplicate execution after
a lease expiry cannot repeat a domain effect.

Run standalone:
    uv run python -m worker.orchestration            # loop
    uv run python -m worker.orchestration --once     # one claim/pass, exit
Environment: DATABASE_URL (the encore_app role), AUTH_JWT_SECRET (settings
validation), ORCHESTRATION_LEASE_SECONDS, ORCHESTRATION_POLL_SECONDS,
WORKER_ID; optional ANTHROPIC_API_KEY for the explanation node.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
from collections.abc import Callable

logger = logging.getLogger("worker.orchestration")


def worker_id() -> str:
    return os.environ.get("WORKER_ID") or f"{socket.gethostname()}:{os.getpid()}"


def run_loop(
    stop: threading.Event,
    *,
    once: bool = False,
    on_beat: Callable[[], None] | None = None,
) -> int:
    from app.orchestration.coordinator import work_once

    lease = float(os.environ.get("ORCHESTRATION_LEASE_SECONDS", "30"))
    poll = float(os.environ.get("ORCHESTRATION_POLL_SECONDS", "2"))
    me = worker_id()
    logger.info(json.dumps({"event": "orchestration_worker_started", "worker_id": me}))
    while not stop.is_set():
        if on_beat is not None:
            on_beat()
        try:
            run_id = work_once(me, lease_seconds=lease)
        except Exception as exc:  # noqa: BLE001 -- keep the worker alive; state is durable
            logger.error(
                json.dumps(
                    {"event": "orchestration_pass_error", "error": f"{type(exc).__name__}: {exc}"}
                )
            )
            run_id = None
        if run_id:
            logger.info(json.dumps({"event": "orchestration_pass_done", "run_id": run_id}))
        if once:
            return 0
        if run_id is None:
            stop.wait(poll)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    return run_loop(threading.Event(), once=args.once)


if __name__ == "__main__":
    sys.exit(main())
