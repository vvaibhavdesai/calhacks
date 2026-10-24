"""Minimal worker process entrypoint.

The worker runs a tiny stdlib HTTP health server on WORKER_HEALTH_PORT and,
when ORCHESTRATION_WORKER_ENABLED=1, the A06 nightly-close loop
(worker/orchestration.py: lease-claimed LangGraph runs). Otherwise it only
logs a heartbeat, as in A01. The health-check contract is unchanged.
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


class _HealthHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass  # silence default access logging; structured logs go through `logger`

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler naming
        if self.path in ("/health/live", "/health/ready"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            body = {
                "status": "ok",
                "started_at": _started_at.isoformat(),
                "uptime_seconds": (datetime.now(UTC) - _started_at).total_seconds(),
            }
            self.wfile.write(json.dumps(body).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()


def _run_health_server(port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(json.dumps({"event": "worker_health_server_started", "port": port}))
    return server


def _heartbeat_loop(interval_seconds: float) -> None:
    while not _shutdown.is_set():
        logger.info(
            json.dumps(
                {
                    "event": "worker_heartbeat",
                    "ts": datetime.now(UTC).isoformat(),
                }
            )
        )
        _shutdown.wait(interval_seconds)


def _handle_signal(signum, frame) -> None:  # noqa: ANN001
    logger.info(json.dumps({"event": "worker_shutdown_signal", "signum": signum}))
    _shutdown.set()


def main() -> int:
    port = int(os.environ.get("WORKER_HEALTH_PORT", "8100"))
    interval = float(os.environ.get("WORKER_HEARTBEAT_SECONDS", "30"))

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    server = _run_health_server(port)
    try:
        if os.environ.get("ORCHESTRATION_WORKER_ENABLED") == "1":
            from worker.orchestration import run_loop

            run_loop(_shutdown)
        else:
            _heartbeat_loop(interval)
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
