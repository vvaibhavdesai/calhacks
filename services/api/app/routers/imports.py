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

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel, ValidationError
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
    """Accepts the flat body and the C03 command envelope alike.

    The envelope ({command_id, expected_version, payload}) is what the UI's
    command client sends for every effectful call; this route predates it and
    took the fields at the top level. Both work: `payload` is merged over the
    top level before validation, so existing scripts are unaffected.

    Idempotency still comes from the content hash, as it always has -- the same
    bytes for the same document are one version however many times they are
    posted -- so command_id is recorded rather than used as the key.
    """

    model_config = {"extra": "ignore"}

    command_id: str | None = None

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
    # The document record itself, flattened from the document row and its
    # CURRENT version. Served alongside `versions` so the existing drill-down
    # consumers are unaffected, and so an evidence drawer can show what a
    # document is without walking the version list first.
    id: str
    vendor_id: str | None = None
    kind: str | None = None
    # The document's name in the system it came from. Not a separate stored
    # title -- nothing invents one; `source_native_id` IS how that system
    # identifies this document.
    title: str
    version: int | None = None
    status: str | None = None
    media_type: str | None = None
    content_hash: str | None = None
    byte_length: int | None = None
    parser_version: str | None = None
    received_at: str | None = None


class SourceDocumentListOut(BaseModel):
    items: list[SourceDocumentOut]
    next_cursor: str | None = None


def _source_document_out(
    document: SourceDocument, versions: list[SourceVersion]
) -> SourceDocumentOut:
    """One document plus its versions, newest version's facts flattened on top.

    `versions` must be ordered ascending, as both callers order it, so the last
    entry is current. A document with no versions is still returned: ingestion
    creates the row and the version together, but a reader should see the
    document rather than a 404 if that ever changes.
    """
    current = versions[-1] if versions else None
    return SourceDocumentOut(
        source_document_id=document.id,
        source_system=document.source_system,
        source_native_id=document.source_native_id,
        id=document.id,
        vendor_id=document.vendor_id,
        kind=document.document_kind,
        title=document.source_native_id,
        version=current.version if current else None,
        status=current.status if current else None,
        media_type=current.mime_type if current else None,
        content_hash=current.content_hash if current else None,
        byte_length=current.byte_size if current else None,
        parser_version=current.parser_version if current else None,
        received_at=current.received_at.isoformat() if current else None,
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


def _versions_for(db: Session, document_ids: list[str]) -> dict[str, list[SourceVersion]]:
    """Every version of the given documents, ascending, in ONE query.

    A per-document query would be a round trip per row; the evidence picker
    asks for a whole page at a time.
    """
    if not document_ids:
        return {}
    rows = (
        db.execute(
            select(SourceVersion)
            .where(SourceVersion.source_document_id.in_(document_ids))
            .order_by(SourceVersion.source_document_id.asc(), SourceVersion.version.asc())
        )
        .scalars()
        .all()
    )
    out: dict[str, list[SourceVersion]] = {}
    for v in rows:
        out.setdefault(v.source_document_id, []).append(v)
    return out


@router.get("/source-documents", response_model=SourceDocumentListOut)
def list_source_documents(
    kind: str | None = None,
    vendor_id: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_IMPORT_SOURCE)),
) -> SourceDocumentListOut:
    """The imported documents, newest first.

    Same offset-cursor convention as `GET /vendors` and `GET /journal`: the
    cursor is an opaque decimal offset over a stable order and `next_cursor` is
    null on the last page. Until this existed only fetch-by-id did, so nothing
    could populate an evidence picker or show what had been ingested.
    """
    offset = int(cursor) if cursor and cursor.isdigit() else 0
    query = select(SourceDocument)
    if kind is not None:
        query = query.where(SourceDocument.document_kind == kind)
    if vendor_id is not None:
        query = query.where(SourceDocument.vendor_id == vendor_id)
    rows = (
        db.execute(
            query.order_by(SourceDocument.created_at.desc(), SourceDocument.id.asc())
            .offset(offset)
            .limit(limit + 1)
        )
        .scalars()
        .all()
    )
    page = list(rows[:limit])
    by_doc = _versions_for(db, [d.id for d in page])
    return SourceDocumentListOut(
        items=[_source_document_out(d, by_doc.get(d.id, [])) for d in page],
        next_cursor=str(offset + limit) if len(rows) > limit else None,
    )


def _unwrap_envelope(body: dict) -> dict:
    """Flatten a C03 command envelope onto the top level.

    A reader sending {command_id, expected_version, payload: {...}} and a
    script sending the fields directly should reach the same handler.
    """
    payload = body.get("payload")
    if isinstance(payload, dict):
        merged = {k: v for k, v in body.items() if k != "payload"}
        merged.update(payload)
        return merged
    return body


@router.post("/imports", response_model=ImportResponse, status_code=201)
def create_import(
    raw: dict = Body(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_IMPORT_SOURCE)),
) -> ImportResponse:
    # Taken raw so a command envelope can be flattened before validation;
    # FastAPI would otherwise reject the envelope's shape first.
    try:
        body = ImportRequest(**_unwrap_envelope(raw))
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.errors()
        ) from exc
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
    return _source_document_out(document, list(versions))


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
