"""A10 operations: alert signals, /metrics auth, Claude caps and log redaction.

No Claude calls: every extraction-endpoint case here is refused by a cap, or
reaches `make_client()` with no ANTHROPIC_API_KEY (503) -- which proves the
caps were evaluated first and passed.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta

import pytest
from app.agents import limits
from app.auth import hash_password
from app.logging_config import RedactingJSONFormatter
from app.models import CAPABILITY_RUN_AGENTS, Capability, User, UserCapability
from app.models_agents import DocumentExtraction
from app.models_ledger import OutboxIntent, Payout, Run, SourceDocument, SourceVersion, Vendor


def _login(client, db_session, capabilities=()) -> dict:
    user = User(email="ops@example.com", hashed_password=hash_password("pw-ops-1"))
    db_session.add(user)
    db_session.flush()
    for code in capabilities:
        cap = db_session.query(Capability).filter_by(code=code).one_or_none()
        if cap is None:
            cap = Capability(code=code, description=code)
            db_session.add(cap)
            db_session.flush()
        db_session.add(UserCapability(user_id=user.id, capability_id=cap.id, granted_by=None))
    db_session.commit()
    resp = client.post(
        "/api/v1/auth/login", json={"email": "ops@example.com", "password": "pw-ops-1"}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _seed_problems(db_session) -> None:
    old = datetime.now(UTC) - timedelta(hours=3)
    db_session.add(Run(festival_id="f-ops", status="RUNNING", started_at=old, updated_at=old))
    db_session.add(Run(festival_id="f-ops", status="RUNNING"))  # fresh: not stalled
    db_session.add(Run(festival_id="f-ops", status="COMPLETED", updated_at=old))
    db_session.add(
        OutboxIntent(idempotency_key="ops-1", intent_type="payout", target="rail", created_at=old)
    )
    db_session.add(OutboxIntent(idempotency_key="ops-2", intent_type="payout", target="rail"))
    db_session.add(
        OutboxIntent(idempotency_key="ops-3", intent_type="payout", target="rail", status="UNKNOWN")
    )
    v = Vendor(festival_id="f-ops", name="Ops Vendor")
    db_session.add(v)
    db_session.flush()
    db_session.add(
        Payout(
            logical_obligation_id="obl-1",
            idempotency_key="pay-ops-1",
            vendor_id=v.id,
            business_date=date(2026, 10, 16),
            amount_cents=1000,
            state="UNKNOWN",
        )
    )
    db_session.commit()


def test_alerts_require_authentication(client):
    assert client.get("/api/v1/ops/alerts").status_code == 401


def test_alerts_ok_on_empty_database(client, db_session):
    headers = _login(client, db_session)
    body = client.get("/api/v1/ops/alerts", headers=headers).json()
    assert body["status"] == "ok"
    assert body["alerts"] == []
    assert body["signals"]["worker_up"] is None  # not configured -> not probed


def test_alerts_report_stalled_runs_outbox_backlog_and_unknown_payouts(client, db_session):
    _seed_problems(db_session)
    headers = _login(client, db_session)
    body = client.get("/api/v1/ops/alerts", headers=headers).json()
    codes = {a["code"]: a for a in body["alerts"]}
    assert body["status"] == "alerting"
    assert codes["STALLED_RUNS"]["count"] == 1
    assert codes["OUTBOX_BACKLOG"]["count"] == 2  # both PENDING; one is stale
    assert body["signals"]["outbox_pending_stale"] == 1
    assert codes["OUTBOX_UNKNOWN"]["count"] == 1
    assert codes["UNKNOWN_PAYOUTS"]["count"] == 1
    assert codes["UNKNOWN_PAYOUTS"]["severity"] == "critical"
    # Read-only: the probe must not have touched the payout.
    assert db_session.query(Payout).one().state == "UNKNOWN"


def test_unconsumed_intent_types_do_not_trip_backlog_alert(client, db_session):
    old = datetime.now(UTC) - timedelta(hours=3)
    db_session.add(
        OutboxIntent(
            idempotency_key="imp-1",
            intent_type="source.imported.notify",
            target="internal",
            created_at=old,
        )
    )
    db_session.commit()
    headers = _login(client, db_session)
    body = client.get("/api/v1/ops/alerts", headers=headers).json()
    assert body["alerts"] == []
    assert body["signals"]["outbox_unconsumed_pending"] == 1
    assert body["signals"]["outbox_pending"] == 0


def test_worker_probe_failure_raises_worker_down(client, db_session, monkeypatch):
    monkeypatch.setenv("OPS_WORKER_HEALTH_URL", "http://127.0.0.1:9/health/live")
    headers = _login(client, db_session)
    body = client.get("/api/v1/ops/alerts", headers=headers).json()
    assert body["signals"]["worker_up"] is False
    assert "WORKER_DOWN" in {a["code"] for a in body["alerts"]}


def test_metrics_disabled_without_token(client, monkeypatch):
    monkeypatch.delenv("OPS_METRICS_TOKEN", raising=False)
    assert client.get("/metrics").status_code == 404


def test_metrics_requires_matching_token(client, db_session, monkeypatch):
    monkeypatch.setenv("OPS_METRICS_TOKEN", "metrics-test-token")
    _seed_problems(db_session)
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code == 401
    resp = client.get("/metrics", headers={"Authorization": "Bearer metrics-test-token"})
    assert resp.status_code == 200
    assert "encore_payouts_unknown 1" in resp.text
    assert "encore_runs_stalled 1" in resp.text
    assert "encore_outbox_pending 2" in resp.text


# ---------------------------------------------------------------------------
# Claude rate/cost limits
# ---------------------------------------------------------------------------


@pytest.fixture()
def agent_headers(client, db_session, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    limits.gate.reset_for_tests()
    yield _login(client, db_session, capabilities=(CAPABILITY_RUN_AGENTS,))
    limits.gate.reset_for_tests()


def _extract(client, headers, **body):
    payload = {"festival_id": "f-ops", "source_document_ids": ["none"], **body}
    return client.post("/api/v1/agents/extract-documents", json=payload, headers=headers)


def test_request_budget_above_cap_is_rejected(client, agent_headers, monkeypatch):
    monkeypatch.setenv("CLAUDE_MAX_BUDGET_USD_PER_REQUEST", "1.0")
    resp = _extract(client, agent_headers, budget_usd=2.0)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "budget_exceeds_cap"


def test_document_count_above_cap_is_rejected(client, agent_headers, monkeypatch):
    monkeypatch.setenv("CLAUDE_MAX_DOCUMENTS_PER_REQUEST", "5")
    resp = _extract(client, agent_headers, max_documents=6)
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "documents_exceed_cap"


def _record_spend(db_session, input_tokens: int, output_tokens: int) -> None:
    doc = SourceDocument(source_system="ops-test", source_native_id="doc-1")
    db_session.add(doc)
    db_session.flush()
    sv = SourceVersion(
        source_document_id=doc.id,
        version=1,
        content_hash="0" * 64,
        mime_type="application/pdf",
        byte_size=1,
        storage_locator="ops/doc-1",
        received_at=datetime.now(UTC),
    )
    db_session.add(sv)
    db_session.flush()
    db_session.add(
        DocumentExtraction(
            source_document_id=doc.id,
            source_version_id=sv.id,
            content_hash="0" * 64,
            parser="test",
            text_sha256="0" * 64,
            model_requested="test-model",
            prompt_version="test",
            status="SUCCEEDED",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    )
    db_session.commit()


def test_daily_budget_exhausted_is_refused_before_any_call(
    client, db_session, agent_headers, monkeypatch
):
    monkeypatch.setenv("CLAUDE_DAILY_BUDGET_USD", "1.0")
    _record_spend(db_session, input_tokens=0, output_tokens=200_000)  # $2.00 estimated
    resp = _extract(client, agent_headers, budget_usd=0.5)
    assert resp.status_code == 429
    assert resp.json()["detail"]["code"] == "claude_daily_budget_exhausted"
    body = client.get("/api/v1/ops/alerts", headers=agent_headers).json()
    budget = [a for a in body["alerts"] if a["code"] == "CLAUDE_BUDGET"]
    assert budget and budget[0]["severity"] == "critical"


def test_within_caps_reaches_client_construction(client, agent_headers):
    # No API key configured: passing every cap ends at 503, never a call.
    resp = _extract(client, agent_headers, budget_usd=0.5)
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "model_not_configured"


def test_batch_rate_and_single_batch_gate():
    limits.gate.reset_for_tests()
    try:
        limits.gate.try_start(max_per_hour=2)
        with pytest.raises(limits.LimitError) as busy:
            limits.gate.try_start(max_per_hour=2)
        assert busy.value.status_code == 409
        limits.gate.finish()
        limits.gate.try_start(max_per_hour=2)
        limits.gate.finish()
        with pytest.raises(limits.LimitError) as rate:
            limits.gate.try_start(max_per_hour=2)
        assert rate.value.status_code == 429
    finally:
        limits.gate.reset_for_tests()


# ---------------------------------------------------------------------------
# Redacted structured logs
# ---------------------------------------------------------------------------


def _format(message: str, **fields) -> dict:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, message, None, None)
    record.extra_fields = fields
    return json.loads(RedactingJSONFormatter().format(record))


# Fake secret-shaped values are assembled at runtime so this file itself
# never contains a literal that a secret scanner (scripts/check_secrets.sh,
# gitleaks) would rightly flag.
_FAKE_PW = "hunter2" + "pw"
_FAKE_KEY = "sk-ant-" + "api03-" + "abcdefghijklmnop"


def test_log_redaction_covers_keys_urls_and_api_keys():
    out = _format(
        f"connect postgresql+psycopg://encore_app:{_FAKE_PW}@db:5432/encore failed; "
        f"key {_FAKE_KEY}; header Bearer abc.def.ghi",
        refresh_token="tok-one",
        ops_metrics_token="tok-two",
        db_password="p",
        nested={"api_key": "k", "ok": "fine"},
        path="/api/v1/ops/alerts",
    )
    text = json.dumps(out)
    for leaked in (_FAKE_PW, _FAKE_KEY, "abc.def.ghi", "tok-one", "tok-two"):
        assert leaked not in text
    assert "encore_app:[REDACTED]@db" in out["message"]
    assert out["db_password"] == "[REDACTED]"
    assert out["nested"] == {"api_key": "[REDACTED]", "ok": "fine"}
    assert out["path"] == "/api/v1/ops/alerts"


def test_readiness_does_not_leak_connection_details(client, monkeypatch):
    from app.db import get_db
    from app.main import app

    class _Broken:
        def execute(self, *_a, **_k):
            raise RuntimeError(f"could not connect to encore_app:{_FAKE_PW}@db-host")

    def _broken_db():
        yield _Broken()

    app.dependency_overrides[get_db] = _broken_db
    resp = client.get("/health/ready")
    assert resp.status_code == 503
    assert _FAKE_PW not in resp.text and "db-host" not in resp.text
    assert resp.json()["error"] == "RuntimeError"


def test_requests_get_a_request_id(client):
    resp = client.get("/health/live", headers={"X-Request-ID": "abc123"})
    assert resp.headers["X-Request-ID"] == "abc123"
    assert client.get("/health/live").headers["X-Request-ID"]
