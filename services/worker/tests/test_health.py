import json
import urllib.error
import urllib.request

from worker.main import _run_health_server


def test_health_server_responds(tmp_path):
    server = _run_health_server(0)  # port 0 -> OS picks a free port
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/live", timeout=5) as resp:
            assert resp.status == 200
            body = json.loads(resp.read())
            assert body["status"] == "ok"
    finally:
        server.shutdown()


def test_liveness_turns_stale_when_heartbeat_stops(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from worker import main as wm

    now = datetime.now(UTC)
    monkeypatch.setattr(wm, "_stale_after_seconds", 90.0)
    monkeypatch.setattr(wm, "_last_heartbeat_at", now - timedelta(seconds=10))
    assert wm.health_status(now)["status"] == "ok"
    monkeypatch.setattr(wm, "_last_heartbeat_at", now - timedelta(seconds=300))
    assert wm.health_status(now)["status"] == "stale"

    server = _run_health_server(0)
    try:
        port = server.server_address[1]
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health/live", timeout=5)
            raise AssertionError("expected 503 for a stale worker")
        except urllib.error.HTTPError as err:
            assert err.code == 503
            assert json.loads(err.read())["status"] == "stale"
    finally:
        server.shutdown()
