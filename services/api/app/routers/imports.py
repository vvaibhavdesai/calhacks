"""POST /api/v1/imports -- idempotent source ingestion endpoint (A02).

Reuses `require_capability` from A01 (app.auth) exactly as instructed by the
A02 task description: no parallel authorization mechanism is invented here.

Also exposes the read side of "source drill-down": given a stored canonical
record you can always walk back to source_document -> source_version ->
content_hash/locator. GET /api/v1/sources/{id} and
GET /api/v1/sources/{id}/versions/{version} are the concrete drill-down
routes; services/api/tests/test_ingestion.py exercises them end to end.
"""

from __future__ import annotations

import base64

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_capability
from app.db import get_db
from app.ingestion import import_source
from app.models import CAPABILITY_IMPORT_SOURCE, User
from app.models_ledger import SourceDocument, SourceVersion
from app.storage.local_fs import get_document_storage

router = APIRouter(prefix="/api/v1", tags=["imports"])


class ImportRequest(BaseModel):
    source_system: str
    source_native_id: str
    mime_type: str
    # Base64-encoded raw bytes. Keeps this a plain JSON body (no multipart
    # plumbing needed for A02's scope) while still carrying arbitrary binary
    # content safely.
    content_base64: str
    parser_version: str | None = None


class ImportResponse(BaseModel):
    source_document_id: str
    source_version: int
    content_hash: str
    status: str
    duplicate: bool
    quarantine_reason: str | None = None


class SourceVersionOut(BaseModel):
    source_document_id: str
    version: int
    content_hash: str
    mime_type: str
    byte_size: int
    storage_locator: str
    status: str
    quarantine_reason: str | None
    received_at: str


class SourceDocumentOut(BaseModel):
    source_document_id: str
    source_system: str
    source_native_id: str
    versions: list[SourceVersionOut]


@router.post("/imports", response_model=ImportResponse, status_code=201)
def create_import(
    body: ImportRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_IMPORT_SOURCE)),
) -> ImportResponse:
    try:
        content_bytes = base64.b64decode(body.content_base64, validate=True)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"content_base64 is not valid base64: {exc}",
        ) from exc

    storage = get_document_storage()
    result = import_source(
        db,
        storage,
        source_system=body.source_system,
        source_native_id=body.source_native_id,
        content_bytes=content_bytes,
        mime_type=body.mime_type,
        actor_id=user.id,
        actor_capability=CAPABILITY_IMPORT_SOURCE,
        parser_version=body.parser_version,
    )
    # Exactly one commit point: domain row(s), audit event and outbox intent
    # inserted inside import_source() all land -- or none do.
    db.commit()

    return ImportResponse(
        source_document_id=result.source_document_id,
        source_version=result.version,
        content_hash=result.content_hash,
        status=result.status,
        duplicate=result.duplicate,
        quarantine_reason=result.quarantine_reason,
    )


@router.get("/sources/{source_document_id}", response_model=SourceDocumentOut)
def get_source_document(
    source_document_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_IMPORT_SOURCE)),
) -> SourceDocumentOut:
    document = db.get(SourceDocument, source_document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="source document not found")
    versions = (
        db.execute(
            select(SourceVersion)
            .where(SourceVersion.source_document_id == document.id)
            .order_by(SourceVersion.version.asc())
        )
        .scalars()
        .all()
    )
    return SourceDocumentOut(
        source_document_id=document.id,
        source_system=document.source_system,
        source_native_id=document.source_native_id,
        versions=[
            SourceVersionOut(
                source_document_id=document.id,
                version=v.version,
                content_hash=v.content_hash,
                mime_type=v.mime_type,
                byte_size=v.byte_size,
                storage_locator=v.storage_locator,
                status=v.status,
                quarantine_reason=v.quarantine_reason,
                received_at=v.received_at.isoformat(),
            )
            for v in versions
        ],
    )


@router.get("/sources/{source_document_id}/versions/{version}", response_model=SourceVersionOut)
def get_source_version(
    source_document_id: str,
    version: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_IMPORT_SOURCE)),
) -> SourceVersionOut:
    v = db.execute(
        select(SourceVersion).where(
            SourceVersion.source_document_id == source_document_id,
            SourceVersion.version == version,
        )
    ).scalar_one_or_none()
    if v is None:
        raise HTTPException(status_code=404, detail="source version not found")
    return SourceVersionOut(
        source_document_id=source_document_id,
        version=v.version,
        content_hash=v.content_hash,
        mime_type=v.mime_type,
        byte_size=v.byte_size,
        storage_locator=v.storage_locator,
        status=v.status,
        quarantine_reason=v.quarantine_reason,
        received_at=v.received_at.isoformat(),
    )
