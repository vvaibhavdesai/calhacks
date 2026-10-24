"""Minimal worker process entrypoint.

The worker runs a tiny stdlib HTTP health server on WORKER_HEALTH_PORT plus
whichever work loops are enabled, each in its own thread:

    ORCHESTRATION_WORKER_ENABLED=1  worker/orchestration.py -- the A06
                                    nightly-close loop (lease-claimed
                                    LangGraph runs)
    OUTBOX_DISPATCHER_ENABLED=1     worker/outbox.py -- delivers PENDING
                                    outbox intents
    SCHEDULER_ENABLED=1             worker/scheduler.py -- fires the 9 AM /
                                    10 AM nightly close

With none of them enabled it only logs a heartbeat, as in A01. The
health-check contract is unchanged: /health/live and /health/ready report
`stale` (503) when the work loop stops making progress. Each enabled loop
beats separately now, and the worker is stale as soon as ANY of them stops
beating -- a wedged dispatcher must not hide behind a healthy scheduler --
while `last_heartbeat_at` keeps its old meaning of "the oldest progress any
loop has made".
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("worker")

_shutdown = threading.Event()
_started_at = datetime.now(UTC)
_last_heartbeat_at: datetime | None = None
_stale_after_seconds: float = 90.0
# Per-loop progress: name -> (last beat, how long that loop may legitimately
# go between beats). Empty when only the A01 heartbeat is running.
_loop_beats: dict[str, datetime] = {}
_loop_windows: dict[str, float] = {}


class _HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass  # silence default access logging; structured logs go through `logger`

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler naming
        if self.path in ("/health/live", "/health/ready"):
            body = health_status()
            self.send_response(200 if body["status"] == "ok" else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(body).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()


def health_status(now: datetime | None = None) -> dict:
    """A10 worker liveness: "the process answers HTTP" is not enough -- the
    work loop itself must be making progress. Live means the loop recorded a
    heartbeat within `_stale_after_seconds` (3x the heartbeat interval).
    Before the loop's first beat (startup) the process counts as live."""
    now = now or datetime.now(UTC)
    last = _last_heartbeat_at
    age = (now - last).total_seconds() if last else None
    stale = age is not None and age > _stale_after_seconds
    loops = {}
    for name, beat in _loop_beats.items():
        window = _loop_windows.get(name, _stale_after_seconds)
        loop_age = (now - beat).total_seconds()
        loop_stale = loop_age > window
        stale = stale or loop_stale
        loops[name] = {
            "last_heartbeat_at": beat.isoformat(),
            "heartbeat_age_seconds": loop_age,
            "stale_after_seconds": window,
            "status": "stale" if loop_stale else "ok",
        }
    return {
        "status": "stale" if stale else "ok",
        "started_at": _started_at.isoformat(),
        "uptime_seconds": (now - _started_at).total_seconds(),
        "last_heartbeat_at": last.isoformat() if last else None,
        "heartbeat_age_seconds": age,
        "stale_after_seconds": _stale_after_seconds,
        "loops": loops,
    }


def _run_health_server(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(json.dumps({"event": "worker_health_server_started", "port": port}))
    return server


def _beat(loop: str | None = None) -> None:
    global _last_heartbeat_at
    now = datetime.now(UTC)
    if loop is not None:
        _loop_beats[loop] = now
        # "Oldest progress any loop has made", so the pre-existing field
        # still means what A10's alert expects it to mean.
        _last_heartbeat_at = min(_loop_beats.values())
    else:
        _last_heartbeat_at = now
    logger.info(
        json.dumps(
            {
                "event": "worker_heartbeat",
                "ts": now.isoformat(),
                **({"loop": loop} if loop else {}),
            }
        )
    )


def _heartbeat_loop(interval_seconds: float) -> None:
    while not _shutdown.is_set():
        _beat()
        _shutdown.wait(interval_seconds)


def _handle_signal(signum, frame) -> None:  # noqa: ANN001
    logger.info(json.dumps({"event": "worker_shutdown_signal", "signum": signum}))
    _shutdown.set()


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _enabled_loops() -> list[tuple[str, Callable[[threading.Event], None], float]]:
    """(name, target, stale window) for every loop this process should run."""
    loops: list[tuple[str, Callable[[threading.Event], None], float]] = []
    if _enabled("ORCHESTRATION_WORKER_ENABLED"):
        from worker.orchestration import run_loop as orchestration_loop

        # The orchestration loop beats once per pass, and one pass may
        # legitimately spend the whole LLM timeout inside a graph node, so
        # the window has to cover a slow pass or a busy worker reads stale.
        llm_timeout = float(os.environ.get("ORCHESTRATION_LLM_TIMEOUT_SECONDS", "60"))
        loops.append(("orchestration", orchestration_loop, 3 * llm_timeout))
    if _enabled("OUTBOX_DISPATCHER_ENABLED"):
        from worker.outbox import EXPECTED_PASS_SECONDS
        from worker.outbox import run_loop as outbox_loop

        loops.append(("outbox", outbox_loop, 3 * EXPECTED_PASS_SECONDS))
    if _enabled("SCHEDULER_ENABLED"):
        from worker.scheduler import DEFAULT_POLL_SECONDS, EXPECTED_TICK_SECONDS
        from worker.scheduler import run_loop as scheduler_loop

        poll = float(os.environ.get("SCHEDULE_POLL_SECONDS", DEFAULT_POLL_SECONDS))
        loops.append(("scheduler", scheduler_loop, 3 * max(poll, EXPECTED_TICK_SECONDS)))
    return loops


def main() -> int:
    port = int(os.environ.get("WORKER_HEALTH_PORT", "8100"))
    interval = float(os.environ.get("WORKER_HEARTBEAT_SECONDS", "30"))
    global _stale_after_seconds
    _stale_after_seconds = 3 * interval

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    server = _run_health_server(port)
    try:
        loops = _enabled_loops()
        if not loops:
            _heartbeat_loop(interval)
            return 0
        # Seed every loop's first beat before it starts, so a loop that is
        # slow to import cannot be reported as already stale.
        now = datetime.now(UTC)
        threads = []
        for name, target, window in loops:
            _loop_beats[name] = now
            _loop_windows[name] = window
            _stale_after_seconds = max(_stale_after_seconds, window)
            threads.append(
                threading.Thread(
                    target=_loop_thread, args=(name, target), name=f"loop-{name}", daemon=True
                )
            )
        _beat()
        logger.info(
            json.dumps({"event": "worker_loops_started", "loops": [n for n, _, _ in loops]})
        )
        for thread in threads:
            thread.start()
        # The main thread owns the signal handlers and does nothing else.
        while not _shutdown.wait(1.0):
            if not any(t.is_alive() for t in threads):
                logger.error(json.dumps({"event": "worker_all_loops_exited"}))
                return 1
        for thread in threads:
            thread.join(timeout=10)
    finally:
        server.shutdown()
    return 0


def _loop_thread(name: str, target: Callable[..., object]) -> None:
    try:
        target(_shutdown, on_beat=lambda: _beat(name))
    except BaseException as exc:  # noqa: BLE001 -- log before the thread dies
        logger.error(
            json.dumps(
                {
                    "event": "worker_loop_crashed",
                    "loop": name,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        )
        raise


if __name__ == "__main__":
    sys.exit(main())
