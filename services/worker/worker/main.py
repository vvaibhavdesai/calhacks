"""Minimal worker process entrypoint.

No LangGraph/Claude logic yet -- that lands in A06. A01's job is just to
prove the worker is a real, independently deployable process with its own
health check, so infra/CI can exercise "start worker, confirm liveness"
before any workflow logic exists.

The worker runs a tiny stdlib HTTP health server on WORKER_HEALTH_PORT and a
background loop that, for now, only logs a heartbeat. Later tasks replace the
heartbeat loop with durable LangGraph dispatch (A06) without changing the
health-check contract other processes depend on.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("worker")

_shutdown = threading.Event()
_started_at = datetime.now(UTC)
_last_heartbeat_at: datetime | None = None
_stale_after_seconds: float = 90.0


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
    return {
        "status": "stale" if stale else "ok",
        "started_at": _started_at.isoformat(),
        "uptime_seconds": (now - _started_at).total_seconds(),
        "last_heartbeat_at": last.isoformat() if last else None,
        "heartbeat_age_seconds": age,
        "stale_after_seconds": _stale_after_seconds,
    }


def _run_health_server(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(json.dumps({"event": "worker_health_server_started", "port": port}))
    return server


def _beat() -> None:
    global _last_heartbeat_at
    _last_heartbeat_at = datetime.now(UTC)
    logger.info(json.dumps({"event": "worker_heartbeat", "ts": _last_heartbeat_at.isoformat()}))


def _heartbeat_loop(interval_seconds: float) -> None:
    while not _shutdown.is_set():
        _beat()
        _shutdown.wait(interval_seconds)


def _handle_signal(signum, frame) -> None:  # noqa: ANN001
    logger.info(json.dumps({"event": "worker_shutdown_signal", "signum": signum}))
    _shutdown.set()


def main() -> int:
    port = int(os.environ.get("WORKER_HEALTH_PORT", "8100"))
    interval = float(os.environ.get("WORKER_HEARTBEAT_SECONDS", "30"))
    global _stale_after_seconds
    _stale_after_seconds = 3 * interval

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    server = _run_health_server(port)
    try:
        _heartbeat_loop(interval)
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
