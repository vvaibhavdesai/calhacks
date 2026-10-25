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


# ---------------------------------------------------------------------------
# Intake metadata (A06): a document nobody classified is never extracted
#
# `app/agents/runner.py` only reads documents whose `document_kind` is one of
# EXTRACTABLE_KINDS, so a contract imported without a kind produces no claims
# and leaves its vendor with no evidenced terms. Until this existed the import
# path could not state a kind at all -- only the fixture scripts set it, which
# is why documents imported through the API never produced terms.
# ---------------------------------------------------------------------------


def _contract_payload(native_id="contract-1", kind="CONTRACT", **extra):
    body = {
        "source_system": "drive",
        "source_native_id": native_id,
        "mime_type": "application/pdf",
        "content_base64": base64.b64encode(b"%PDF-1.4 signed agreement").decode(),
        **extra,
    }
    if kind is not None:
        body["document_kind"] = kind
    return body


def test_an_imported_contract_can_state_its_kind_and_vendor(client, db_session):
    from app.agents.runner import festival_documents
    from app.models_ledger import SourceDocument, Vendor

    vendor = Vendor(festival_id="gcmf-2026-intake", name="Intake Vendor")
    db_session.add(vendor)
    db_session.commit()
    _create_user(db_session, capabilities=[CAPABILITY_IMPORT_SOURCE])
    headers = {"Authorization": f"Bearer {_login(client, 'importer@example.com')}"}

    resp = client.post(
        "/api/v1/imports",
        json=_contract_payload(vendor_id=vendor.id),
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    doc = db_session.get(SourceDocument, resp.json()["source_document_id"])
    db_session.refresh(doc)
    assert (doc.document_kind, doc.vendor_id) == ("CONTRACT", vendor.id)
    # The consequence that matters: extraction will now read it.
    assert doc.id in festival_documents(db_session, "gcmf-2026-intake")


def test_an_import_with_no_kind_is_stored_and_never_extracted(client, db_session):
    """Not an error -- most imports are POS and bank files. But the document
    is invisible to extraction, and this test says so out loud rather than
    leaving it to be discovered when a vendor has no terms."""
    from app.agents.runner import festival_documents
    from app.models_ledger import SourceDocument, Vendor

    vendor = Vendor(festival_id="gcmf-2026-intake-nokind", name="Unclassified Vendor")
    db_session.add(vendor)
    db_session.commit()
    _create_user(db_session, capabilities=[CAPABILITY_IMPORT_SOURCE])
    headers = {"Authorization": f"Bearer {_login(client, 'importer@example.com')}"}

    resp = client.post(
        "/api/v1/imports",
        json=_contract_payload(native_id="unclassified-1", kind=None, vendor_id=vendor.id),
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    doc = db_session.get(SourceDocument, resp.json()["source_document_id"])
    db_session.refresh(doc)
    assert doc.document_kind is None
    assert festival_documents(db_session, "gcmf-2026-intake-nokind") == []


def test_an_unknown_document_kind_is_refused_not_stored_as_unextractable(client, db_session):
    _create_user(db_session, capabilities=[CAPABILITY_IMPORT_SOURCE])
    headers = {"Authorization": f"Bearer {_login(client, 'importer@example.com')}"}

    resp = client.post(
        "/api/v1/imports",
        json=_contract_payload(native_id="typo-1", kind="CONTRCT"),
        headers=headers,
    )
    assert resp.status_code == 422, resp.text
    assert "CONTRCT" in resp.text


def test_a_later_import_fills_in_a_missing_kind_but_never_reclassifies(client, db_session):
    """Additive is fine; contradicting is a 409. The kind chose the
    extraction prompt that produced this document's existing claims."""
    from app.models_ledger import SourceDocument

    _create_user(db_session, capabilities=[CAPABILITY_IMPORT_SOURCE])
    headers = {"Authorization": f"Bearer {_login(client, 'importer@example.com')}"}

    first = client.post(
        "/api/v1/imports",
        json=_contract_payload(native_id="late-kind-1", kind=None),
        headers=headers,
    )
    assert first.status_code == 201, first.text
    doc_id = first.json()["source_document_id"]

    filled = client.post(
        "/api/v1/imports",
        json=_contract_payload(
            native_id="late-kind-1",
            kind="CONTRACT",
            content_base64=base64.b64encode(b"%PDF-1.4 signed agreement v2").decode(),
        ),
        headers=headers,
    )
    assert filled.status_code == 201, filled.text
    doc = db_session.get(SourceDocument, doc_id)
    db_session.refresh(doc)
    assert doc.document_kind == "CONTRACT"

    conflicting = client.post(
        "/api/v1/imports",
        json=_contract_payload(
            native_id="late-kind-1",
            kind="EMAIL",
            content_base64=base64.b64encode(b"%PDF-1.4 signed agreement v3").decode(),
        ),
        headers=headers,
    )
    assert conflicting.status_code == 409, conflicting.text
    detail = conflicting.json()["detail"]
    assert (detail["code"], detail["current"], detail["stated"]) == (
        "document_kind_conflict",
        "CONTRACT",
        "EMAIL",
    )
    db_session.refresh(doc)
    assert doc.document_kind == "CONTRACT"
