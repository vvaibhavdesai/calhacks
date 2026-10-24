import json
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
