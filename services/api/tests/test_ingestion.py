"""A02 ingestion acceptance tests, against the real API + real Postgres
(via the `client`/`db_session` fixtures in conftest.py -- no mocked DB).

These are throwaway dev fixtures written by the A02 implementer to prove
idempotency/quarantine/drill-down work; they are NOT the independent B01
oracle (B01 was not done in this repo -- see docs/progress/A02-handoff.md).
"""

from __future__ import annotations

import base64
import hashlib

from app.auth import hash_password
from app.models import CAPABILITY_IMPORT_SOURCE, Capability, User, UserCapability
from app.models_ledger import SourceVersion


def _create_user(
    db_session,
    email="importer@example.com",
    password="correct-password",
    capabilities=(),
):
    user = User(email=email, hashed_password=hash_password(password), display_name="Test Importer")
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
    return user


def _login(client, email, password="correct-password"):
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def test_import_requires_capability(client, db_session):
    """New write endpoints reuse A01's require_capability, not a parallel
    auth mechanism."""
    _create_user(db_session, capabilities=())
    token = _login(client, "importer@example.com")
    resp = client.post(
        "/api/v1/imports",
        json={
            "source_system": "pos",
            "source_native_id": "order-1",
            "mime_type": "application/json",
            "content_base64": base64.b64encode(b'{"a":1}').decode(),
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_import_twice_is_idempotent(client, db_session):
    """Acceptance criterion: importing twice does not duplicate rows."""
    _create_user(db_session, capabilities=[CAPABILITY_IMPORT_SOURCE])
    token = _login(client, "importer@example.com")
    payload = {
        "source_system": "pos",
        "source_native_id": "order-1",
        "mime_type": "application/json",
        "content_base64": base64.b64encode(b'{"order":"abc","amount_cents":1000}').decode(),
    }
    headers = {"Authorization": f"Bearer {token}"}

    first = client.post("/api/v1/imports", json=payload, headers=headers)
    assert first.status_code == 201, first.text
    first_body = first.json()
    assert first_body["duplicate"] is False
    assert first_body["source_version"] == 1

    second = client.post("/api/v1/imports", json=payload, headers=headers)
    assert second.status_code == 201, second.text
    second_body = second.json()
    assert second_body["duplicate"] is True
    assert second_body["source_version"] == 1
    assert second_body["source_document_id"] == first_body["source_document_id"]

    count = (
        db_session.query(SourceVersion)
        .filter_by(source_document_id=first_body["source_document_id"])
        .count()
    )
    assert count == 1


def test_reimporting_different_content_creates_a_new_version_not_a_conflict(client, db_session):
    """Re-sending genuinely different bytes under the same (source_system,
    source_native_id) -- e.g. a corrected feed -- is a new version, not
    silently merged with or rejected in favor of the old one."""
    _create_user(db_session, capabilities=[CAPABILITY_IMPORT_SOURCE])
    token = _login(client, "importer@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    first = client.post(
        "/api/v1/imports",
        json={
            "source_system": "pos",
            "source_native_id": "order-2",
            "mime_type": "application/json",
            "content_base64": base64.b64encode(b'{"amount_cents":1000}').decode(),
        },
        headers=headers,
    )
    second = client.post(
        "/api/v1/imports",
        json={
            "source_system": "pos",
            "source_native_id": "order-2",
            "mime_type": "application/json",
            "content_base64": base64.b64encode(b'{"amount_cents":1200}').decode(),
        },
        headers=headers,
    )
    assert first.json()["source_version"] == 1
    assert second.json()["source_version"] == 2
    assert second.json()["duplicate"] is False
    assert first.json()["source_document_id"] == second.json()["source_document_id"]


def test_malformed_input_is_quarantined_not_dropped(client, db_session):
    """Acceptance criterion: quarantine invalid/malformed input rather than
    silently dropping it."""
    _create_user(db_session, capabilities=[CAPABILITY_IMPORT_SOURCE])
    token = _login(client, "importer@example.com")
    resp = client.post(
        "/api/v1/imports",
        json={
            "source_system": "pos",
            "source_native_id": "order-bad",
            "mime_type": "application/json",
            "content_base64": base64.b64encode(b"{not valid json").decode(),
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "quarantined"
    assert body["quarantine_reason"] is not None

    drill = client.get(
        f"/api/v1/sources/{body['source_document_id']}/versions/1",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert drill.status_code == 200
    assert drill.json()["status"] == "quarantined"
    assert drill.json()["quarantine_reason"] == body["quarantine_reason"]


def test_source_drill_down_from_version(client, db_session):
    """Acceptance criterion: source drill-down works -- from a stored
    import result back to the exact source document/version/hash."""
    _create_user(db_session, capabilities=[CAPABILITY_IMPORT_SOURCE])
    token = _login(client, "importer@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    content = b'{"order":"xyz","amount_cents":4200}'
    resp = client.post(
        "/api/v1/imports",
        json={
            "source_system": "pos",
            "source_native_id": "order-9",
            "mime_type": "application/json",
            "content_base64": base64.b64encode(content).decode(),
        },
        headers=headers,
    )
    assert resp.status_code == 201
    body = resp.json()
    expected_hash = hashlib.sha256(content).hexdigest()
    assert body["content_hash"] == expected_hash

    doc = client.get(f"/api/v1/sources/{body['source_document_id']}", headers=headers)
    assert doc.status_code == 200
    doc_body = doc.json()
    assert doc_body["source_system"] == "pos"
    assert doc_body["source_native_id"] == "order-9"
    assert len(doc_body["versions"]) == 1
    assert doc_body["versions"][0]["content_hash"] == expected_hash
    assert doc_body["versions"][0]["storage_locator"].startswith("local://")
