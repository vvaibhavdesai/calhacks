"""Idempotent source ingestion (A02).

This is the one concrete, end-to-end workflow A02 implements to prove the
canonical-records/flight-recorder foundation actually works:

  1. immutable raw storage with hash + locator (SourceDocument/SourceVersion)
  2. idempotent import (same bytes imported twice -> no duplicate rows)
  3. quarantine instead of silently dropping malformed input
  4. domain write + audit event + outbox intent committed atomically
     (via app.uow.UnitOfWork, inside the SAME db.Session/transaction)
  5. a real, explicitly rebuildable projection (app.projections)

Later tasks (A03 reconciliation, etc.) are expected to ingest orders/
payments through a similar shape, reusing SourceDocument/SourceVersion and
UnitOfWork -- not reinventing idempotency or atomicity per endpoint.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models_ledger import SourceDocument, SourceVersion
from app.projections import upsert_projection_after_event
from app.storage.base import DocumentStorage
from app.uow import UnitOfWork

# Mime types this service knows how to structurally validate before
# accepting. Anything else is accepted as opaque bytes (e.g. application/pdf)
# -- "quarantine invalid data" applies to data we claim to understand the
# shape of and it turns out we don't, not to formats we never parse at all.
_VALIDATABLE_MIME_TYPES = {"application/json", "text/csv"}


@dataclass
class ImportResult:
    source_document_id: str
    source_version_id: str
    version: int
    content_hash: str
    status: str  # "active" | "quarantined"
    duplicate: bool  # True if this import matched existing content (idempotent no-op)
    quarantine_reason: str | None = None


def _validate_structure(mime_type: str, content_bytes: bytes) -> str | None:
    """Returns a quarantine reason string if `content_bytes` is malformed
    for the claimed `mime_type`, else None. Deliberately conservative: only
    mime types we actually know how to check are validated."""
    if mime_type == "application/json":
        try:
            json.loads(content_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return f"invalid JSON for declared mime_type application/json: {exc}"
    elif mime_type == "text/csv":
        try:
            text = content_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            return f"content is not valid utf-8 text for declared mime_type text/csv: {exc}"
        if text.strip() == "":
            return "text/csv content is empty"
    return None


# What a document IS, as stated by whoever handed it over. Nothing here
# guesses: a filename containing "contract" is not evidence, and a kind
# decides which extraction prompt reads the document and whether A06 treats
# what it says as signed terms or as somebody's assertion.
#
# CONTRACT / AMENDMENT / EMAIL are app/agents/runner.py's EXTRACTABLE_KINDS.
# The rest are storable and auditable but nothing extracts them, which is
# the honest state for a document whose kind nobody has established.
DOCUMENT_KINDS = ("CONTRACT", "AMENDMENT", "EMAIL", "STATEMENT", "INVOICE", "OTHER")


class DocumentKindConflictError(ValueError):
    """An import stated a different kind than the document already has.

    Reclassifying is not an import-time side effect: the kind chose the
    extraction prompt that produced this document's existing claims, and
    those claims are what A06 resolved terms from. Changing it quietly
    would leave claims attributed to a reading of the document that nobody
    would make again.
    """

    def __init__(self, current: str, stated: str) -> None:
        super().__init__(
            f"document is recorded as {current}; this import states {stated}. "
            "Correct the document record explicitly rather than at import."
        )
        self.current = current
        self.stated = stated


def import_source(
    db: Session,
    storage: DocumentStorage,
    *,
    source_system: str,
    source_native_id: str,
    content_bytes: bytes,
    mime_type: str,
    actor_id: str,
    actor_capability: str,
    received_at: datetime | None = None,
    parser_version: str | None = None,
    document_kind: str | None = None,
    vendor_id: str | None = None,
) -> ImportResult:
    """Import one source payload. Does NOT call db.commit() -- the caller
    (e.g. the /api/v1/imports router) commits exactly once after this
    returns, so that the domain write (SourceVersion row), the audit event,
    and the outbox intent are one atomic Postgres transaction per C06.

    `document_kind` and `vendor_id` are the intake metadata A06 needs and
    could not previously be given here at all: extraction only reads
    documents whose kind is one of EXTRACTABLE_KINDS, so a document
    imported without one is never extracted, produces no claims, and
    leaves its vendor with no evidenced terms. Only the fixture scripts set
    it, which is why documents imported through this path never produced
    terms. Both are stated by the caller or left unset -- never inferred
    from a filename -- and both are recorded in the audit event.
    """
    if received_at is None:
        received_at = datetime.now(UTC)
    if document_kind is not None and document_kind not in DOCUMENT_KINDS:
        # Loud, not NULL: a typo'd kind that silently stored as "unknown"
        # would make the document quietly unextractable.
        raise ValueError(
            f"unknown document_kind {document_kind!r}; expected one of {DOCUMENT_KINDS}"
        )

    content_hash = hashlib.sha256(content_bytes).hexdigest()

    document = db.execute(
        select(SourceDocument).where(
            SourceDocument.source_system == source_system,
            SourceDocument.source_native_id == source_native_id,
        )
    ).scalar_one_or_none()
    if document is None:
        document = SourceDocument(
            source_system=source_system,
            source_native_id=source_native_id,
            document_kind=document_kind,
            vendor_id=vendor_id,
        )
        db.add(document)
        db.flush()
    else:
        if (
            document_kind is not None
            and document.document_kind is not None
            and document.document_kind != document_kind
        ):
            raise DocumentKindConflictError(document.document_kind, document_kind)
        # Filling in metadata a previous import did not carry is additive;
        # contradicting it is the error above.
        if document_kind is not None and document.document_kind is None:
            document.document_kind = document_kind
        if vendor_id is not None and document.vendor_id is None:
            document.vendor_id = vendor_id
        db.flush()

    existing_version = db.execute(
        select(SourceVersion).where(
            SourceVersion.source_document_id == document.id,
            SourceVersion.content_hash == content_hash,
        )
    ).scalar_one_or_none()
    if existing_version is not None:
        # Idempotent no-op: byte-identical content already imported under
        # this source document. No new row, no new event, no new outbox
        # intent -- "importing the same source twice does not duplicate
        # rows" (acceptance criterion), proven in
        # services/api/tests/test_ingestion.py.
        return ImportResult(
            source_document_id=document.id,
            source_version_id=existing_version.id,
            version=existing_version.version,
            content_hash=content_hash,
            status=existing_version.status,
            duplicate=True,
            quarantine_reason=existing_version.quarantine_reason,
        )

    max_version = db.execute(
        select(SourceVersion.version)
        .where(SourceVersion.source_document_id == document.id)
        .order_by(SourceVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    next_version = (max_version or 0) + 1

    quarantine_reason = _validate_structure(mime_type, content_bytes)
    status = "quarantined" if quarantine_reason else "active"

    # Store raw bytes keyed by content hash regardless of validity -- per
    # the task description, invalid input is quarantined, never dropped.
    storage_key = f"sources/{document.id}/{content_hash}.bin"
    storage_locator = storage.save(storage_key, content_bytes)

    version_row = SourceVersion(
        source_document_id=document.id,
        version=next_version,
        content_hash=content_hash,
        mime_type=mime_type,
        byte_size=len(content_bytes),
        storage_locator=storage_locator,
        parser_version=parser_version,
        status=status,
        quarantine_reason=quarantine_reason,
        received_at=received_at,
    )
    db.add(version_row)
    db.flush()

    stream_id = f"source_document:{document.id}"
    uow = UnitOfWork(db)
    event = uow.append_event(
        stream_id=stream_id,
        event_type="source.quarantined" if quarantine_reason else "source.imported",
        aggregate_id=document.id,
        aggregate_version=next_version,
        actor_id=actor_id,
        actor_type="human",
        actor_capability=actor_capability,
        occurred_at=received_at,
        payload={
            "source_system": source_system,
            "source_native_id": source_native_id,
            "version": next_version,
            "content_hash": content_hash,
            "mime_type": mime_type,
            "byte_size": len(content_bytes),
            "storage_locator": storage_locator,
            "quarantine_reason": quarantine_reason,
            # What this document is and whose it is, as stated at intake.
            # Null means nobody said -- which is also why nothing extracted
            # it (app/agents/runner.py EXTRACTABLE_KINDS).
            "document_kind": document.document_kind,
            "vendor_id": document.vendor_id,
        },
        lineage_refs=[
            {
                "ref_kind": "source_version",
                "ref_id": version_row.id,
                "ref_version": next_version,
            }
        ],
    )
    uow.enqueue_outbox(
        idempotency_key=f"import:{document.id}:{next_version}",
        intent_type="source.imported.notify",
        target="internal",
        payload={"source_document_id": document.id, "version": next_version, "status": status},
        event_id=event.event_id,
    )

    upsert_projection_after_event(db, event)

    return ImportResult(
        source_document_id=document.id,
        source_version_id=version_row.id,
        version=next_version,
        content_hash=content_hash,
        status=status,
        duplicate=False,
        quarantine_reason=quarantine_reason,
    )
