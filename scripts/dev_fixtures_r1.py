#!/usr/bin/env python3
"""THROWAWAY, HAND-CHECKED dev fixtures for R1 (readiness records and
checks) verification.

*** THIS IS NOT B01. *** Same standing disclaimer as every other
scripts/dev_fixtures_*.py in this repo (see dev_fixtures_a05.py's own
docstring for the full explanation of why). There is no real B01/X0.4
answer key for readiness scenarios in this repo yet (X0.4 is deliberately
deferred -- see docs/prevention-extension/PROGRESS.md) -- these are R1's
OWN hand-checked substitute, not the genuine future answer key.

Four independent vendors, each proving one TICKET_SPEC.md R1 "Done when"
scenario against REAL rows / REAL production code (app.readiness.
compute_readiness, domain.readiness.evaluate_readiness, app.term_provenance.
resolve_vendor_term_precedence, app.policy.find_candidate_conflicting_terms
-- nothing mocked, nothing synthetic-only):

  (a) "R1 Ready Vendor" -- one ACTIVE, fully-cited term covering the whole
      festival with commission stated once, plus a verified
      SIGNED_AGREEMENT document claim and verified payout details.
      Expect: ready, all eight checks pass.

  (b) "R1 Commission Conflict Vendor" -- the classic taco-truck-style
      conflict: a signed contract (verified SIGNED_AGREEMENT claim) states
      one commission rate, a separate unresolved (status=proposed) claim
      states another, covering the same window, with NEITHER promoted to
      "active" yet (no human has resolved it -- that is R2's job, not
      R1's). Expect: blocked, with COMMISSION_SOURCES_AGREE specifically
      failing (other checks may also fail, honestly, since no rate is
      confirmed yet -- see docs/progress/R1-handoff.md for why that is
      realistic, not a design flaw).

  (c) "R1 Unsigned Agreement Vendor" -- a candidate term exists
      (status=proposed, never activated) but NO document claim anywhere
      carries speaker_role="SIGNED_AGREEMENT" (only a plain correspondence
      claim exists, proving extraction ran and found something, just not a
      signed agreement). Expect: blocked, with SIGNED_AGREEMENT specifically
      failing.

  (d) "R1 Partial Amendment Vendor" -- everything present and CONSISTENT
      (a real signed contract covering DAY1-DAY2, a real signed amendment
      covering DAY2 only, non-overlapping, no disagreement where they
      abut) but DAY3 of the festival is left with no covering active term
      at all. Expect: blocked, with DATES_COVER_FESTIVAL specifically
      failing while AMENDMENT_PRECEDENCE specifically PASSES (exercising
      both checks meaningfully, per the ticket's own wording, rather than
      both failing).

No live Anthropic call is made anywhere in this script. `ANTHROPIC_API_KEY`
is unset in this sandbox's `.env` (confirmed before writing this script --
see docs/progress/R1-handoff.md's "Deviations" for the full explanation);
every `DocumentClaim`/`DocumentExtraction` row below is constructed directly
via SQLAlchemy, the SAME precedent dev_fixtures_x02.py already established
for not re-exercising another ticket's own live/HTTP flow a second time
(there, A05's `approve_case_policy` endpoint; here, A06's real `extract_
document`/Claude call). `app/readiness.py`'s SIGNED_AGREEMENT check still
reads these rows through the EXACT same query A06's real extraction path
would have populated -- nothing in the production code path is bypassed or
mocked, only the live model call itself.

Usage (from repo root, real Postgres required, no SQLite):
    DATABASE_URL=... uv run python scripts/dev_fixtures_r1.py
"""

from __future__ import annotations

import hashlib
import os
import sys
from datetime import UTC, date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app.db import get_session_factory, make_engine  # noqa: E402
from app.ingestion import import_source  # noqa: E402
from app.models_agents import DocumentClaim, DocumentExtraction  # noqa: E402
from app.models_ledger import Vendor, VendorContractTerm  # noqa: E402
from app.storage.local_fs import get_document_storage  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

FESTIVAL_ID = "gcmf-2026-r1dev"
SOURCE_SYSTEM = "r1_devfixtures"

DAY1 = date(2026, 10, 16)
DAY2 = date(2026, 10, 17)
DAY3 = date(2026, 10, 18)
FESTIVAL_START = DAY1
FESTIVAL_END = DAY3


def get_or_create_vendor(db: Session, *, name: str, beneficiary_verified: bool) -> Vendor:
    existing = db.execute(
        select(Vendor).where(Vendor.festival_id == FESTIVAL_ID, Vendor.name == name)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    vendor = Vendor(
        festival_id=FESTIVAL_ID,
        name=name,
        beneficiary_details=(
            {"account_last4": "4242", "routing_last4": "1111", "verified": True}
            if beneficiary_verified
            else {}
        ),
    )
    db.add(vendor)
    db.flush()
    return vendor


def _next_version(db: Session, vendor_id: str) -> int:
    max_version = db.execute(
        select(VendorContractTerm.version)
        .where(VendorContractTerm.vendor_id == vendor_id)
        .order_by(VendorContractTerm.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    return (max_version or 0) + 1


def _get_or_create_term(
    db: Session,
    *,
    vendor_id: str,
    commission_bps: int,
    status: str,
    effective_from: date,
    effective_to: date | None,
    source_refs: list,
    minimum_fee_cents: int | None = 50_000,
) -> VendorContractTerm:
    existing = db.execute(
        select(VendorContractTerm).where(
            VendorContractTerm.vendor_id == vendor_id,
            VendorContractTerm.commission_bps == commission_bps,
            VendorContractTerm.effective_from == effective_from,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    term = VendorContractTerm(
        vendor_id=vendor_id,
        version=_next_version(db, vendor_id),
        commission_bps=commission_bps,
        minimum_fee_cents=minimum_fee_cents,
        processor_fee_deductible=False,
        effective_from=effective_from,
        effective_to=effective_to,
        status=status,
        source_refs=source_refs,
    )
    db.add(term)
    db.flush()
    db.commit()
    return term


def _ingest_document(db: Session, *, native_id: str, label: str) -> dict:
    storage = get_document_storage()
    content = (
        f'{{"label": "{label}", "note": "throwaway hand-checked R1 dev fixture, '
        'not a real signed document"}}'
    ).encode()
    result = import_source(
        db,
        storage,
        source_system=SOURCE_SYSTEM,
        source_native_id=native_id,
        content_bytes=content,
        mime_type="application/json",
        actor_id="dev-fixture-script",
        actor_capability="import_source",
    )
    db.commit()
    return {
        "source_document_id": result.source_document_id,
        "source_version_id": result.source_version_id,
        "source_version": result.version,
        "content_hash": result.content_hash,
    }


def _add_document_claim(
    db: Session,
    *,
    vendor: Vendor,
    doc_ref: dict,
    speaker_role: str,
    commission_bps: int | None,
    quoted_excerpt: str,
    native_id_suffix: str,
) -> DocumentClaim:
    """Constructs a DocumentExtraction + DocumentClaim pair DIRECTLY (no
    live Anthropic call -- see module docstring for why), in the exact
    shape A06's real `extract_document` would have produced, so
    app/readiness.py's SIGNED_AGREEMENT check (which only ever reads these
    two tables) exercises its real, unmocked query path."""
    existing = db.execute(
        select(DocumentClaim).where(
            DocumentClaim.vendor_id == vendor.id,
            DocumentClaim.speaker_role == speaker_role,
            DocumentClaim.quoted_excerpt == quoted_excerpt,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    text_sha = hashlib.sha256(quoted_excerpt.encode()).hexdigest()
    extraction = DocumentExtraction(
        source_document_id=doc_ref["source_document_id"],
        source_version_id=doc_ref["source_version_id"],
        content_hash=doc_ref["content_hash"],
        parser="r1-dev-fixture (no live model call)",
        text_sha256=text_sha,
        model_requested="r1-dev-fixture",
        model_served=None,
        prompt_version="r1-dev-fixture-v1",
        attempt=1,
        status="SUCCEEDED",
        retryable=False,
        document_kind="CONTRACT",
        summary="R1 dev fixture extraction (constructed directly, not a live model call).",
        embedded_instructions_detected=False,
    )
    db.add(extraction)
    db.flush()

    claim = DocumentClaim(
        extraction_id=extraction.id,
        source_document_id=doc_ref["source_document_id"],
        vendor_id=vendor.id,
        vendor_name_claimed=vendor.name,
        claim_kind="COMMISSION_RATE" if commission_bps is not None else "BENEFICIARY_CHANGE",
        value_raw=str(commission_bps) if commission_bps is not None else "n/a",
        commission_bps=commission_bps,
        speaker_role=speaker_role,
        restates_existing_terms=False,
        available_from=datetime.now(UTC),
        quoted_excerpt=quoted_excerpt,
        page=1,
        verified=True,
        verification_notes=[],
        source_ref={
            "source_document_id": doc_ref["source_document_id"],
            "source_version": doc_ref["source_version"],
            "content_hash": doc_ref["content_hash"],
            "locator_kind": "page",
            "locator": f"page=1:{native_id_suffix}",
            "quoted_excerpt": quoted_excerpt,
        },
    )
    db.add(claim)
    db.flush()
    db.commit()
    return claim


def seed(db: Session) -> dict:
    ids: dict = {
        "festival_id": FESTIVAL_ID,
        "festival_start": FESTIVAL_START.isoformat(),
        "festival_end": FESTIVAL_END.isoformat(),
    }

    # =======================================================================
    # (a) R1 Ready Vendor -- clean signed contract covering the full
    # festival, commission stated once, verified payout details.
    # =======================================================================
    vendor_a = get_or_create_vendor(db, name="R1 Ready Vendor", beneficiary_verified=True)
    ids["vendor_a_id"] = vendor_a.id
    doc_a = _ingest_document(db, native_id="r1-dev-vendor-a-contract", label="vendor-a-contract")
    term_a = _get_or_create_term(
        db,
        vendor_id=vendor_a.id,
        commission_bps=1500,
        status="active",
        effective_from=DAY1,
        effective_to=DAY3,
        source_refs=[
            {
                "ref_kind": "document",
                "source_document_id": doc_a["source_document_id"],
                "source_version": doc_a["source_version"],
            }
        ],
    )
    ids["vendor_a_term_id"] = term_a.id
    _add_document_claim(
        db,
        vendor=vendor_a,
        doc_ref=doc_a,
        speaker_role="SIGNED_AGREEMENT",
        commission_bps=1500,
        quoted_excerpt="This signed agreement sets commission at 15% of eligible sales.",
        native_id_suffix="vendor-a",
    )

    # =======================================================================
    # (b) R1 Commission Conflict Vendor -- a real signed contract (15%)
    # whose rate a later, unresolved email claims is 10% -- the taco-truck
    # shape. Neither candidate is promoted to "active" (unresolved).
    # =======================================================================
    vendor_b = get_or_create_vendor(
        db, name="R1 Commission Conflict Vendor", beneficiary_verified=True
    )
    ids["vendor_b_id"] = vendor_b.id
    doc_b = _ingest_document(db, native_id="r1-dev-vendor-b-contract", label="vendor-b-contract")
    contract_claim_b = _get_or_create_term(
        db,
        vendor_id=vendor_b.id,
        commission_bps=1500,
        status="proposed",
        effective_from=DAY1,
        effective_to=None,
        source_refs=[
            {
                "ref_kind": "document",
                "source_document_id": doc_b["source_document_id"],
                "source_version": doc_b["source_version"],
            }
        ],
    )
    email_claim_b = _get_or_create_term(
        db,
        vendor_id=vendor_b.id,
        commission_bps=1000,
        status="proposed",
        effective_from=DAY1,
        effective_to=None,
        source_refs=[{"locator_kind": "row", "locator": f"r1-dev-{vendor_b.id}-email-10pct"}],
    )
    ids["vendor_b_contract_claim_id"] = contract_claim_b.id
    ids["vendor_b_email_claim_id"] = email_claim_b.id
    _add_document_claim(
        db,
        vendor=vendor_b,
        doc_ref=doc_b,
        speaker_role="SIGNED_AGREEMENT",
        commission_bps=1500,
        quoted_excerpt="This signed agreement sets commission at 15% of eligible sales.",
        native_id_suffix="vendor-b",
    )

    # =======================================================================
    # (c) R1 Unsigned Agreement Vendor -- a candidate term exists but was
    # never activated, and no document claim anywhere is sourced from a
    # signed agreement (only ordinary correspondence).
    # =======================================================================
    vendor_c = get_or_create_vendor(
        db, name="R1 Unsigned Agreement Vendor", beneficiary_verified=True
    )
    ids["vendor_c_id"] = vendor_c.id
    doc_c = _ingest_document(db, native_id="r1-dev-vendor-c-draft", label="vendor-c-draft")
    draft_term_c = _get_or_create_term(
        db,
        vendor_id=vendor_c.id,
        commission_bps=1500,
        status="proposed",
        effective_from=DAY1,
        effective_to=DAY3,
        source_refs=[
            {
                "ref_kind": "document",
                "source_document_id": doc_c["source_document_id"],
                "source_version": doc_c["source_version"],
            }
        ],
    )
    ids["vendor_c_draft_term_id"] = draft_term_c.id
    _add_document_claim(
        db,
        vendor=vendor_c,
        doc_ref=doc_c,
        speaker_role="VENDOR",
        commission_bps=1500,
        quoted_excerpt="We are still reviewing the draft agreement you sent over.",
        native_id_suffix="vendor-c",
    )

    # =======================================================================
    # (d) R1 Partial Amendment Vendor -- a signed contract (DAY1-DAY2, 15%)
    # and a signed amendment (DAY2 only, 12%) are both real and CONSISTENT
    # where they abut (no overlap, no disagreement) but DAY3 is left with
    # no covering active term at all.
    # =======================================================================
    vendor_d = get_or_create_vendor(
        db, name="R1 Partial Amendment Vendor", beneficiary_verified=True
    )
    ids["vendor_d_id"] = vendor_d.id
    doc_d_contract = _ingest_document(
        db, native_id="r1-dev-vendor-d-contract", label="vendor-d-contract"
    )
    doc_d_amendment = _ingest_document(
        db, native_id="r1-dev-vendor-d-amendment", label="vendor-d-amendment"
    )
    contract_term_d = _get_or_create_term(
        db,
        vendor_id=vendor_d.id,
        commission_bps=1500,
        status="active",
        effective_from=DAY1,
        effective_to=DAY2,
        source_refs=[
            {
                "ref_kind": "document",
                "source_document_id": doc_d_contract["source_document_id"],
                "source_version": doc_d_contract["source_version"],
            }
        ],
    )
    amendment_term_d = _get_or_create_term(
        db,
        vendor_id=vendor_d.id,
        commission_bps=1200,
        status="active",
        effective_from=DAY2,
        effective_to=DAY2,
        source_refs=[
            {
                "ref_kind": "document",
                "source_document_id": doc_d_amendment["source_document_id"],
                "source_version": doc_d_amendment["source_version"],
            }
        ],
    )
    # record_signed_amendment's source_kind is only set by that specific
    # constructor (app/term_provenance.py); this fixture builds the row
    # directly (same precedent as dev_fixtures_x02.py's own plain-contract
    # term), so set source_kind explicitly to prove AMENDMENT_PRECEDENCE
    # reads it, not just commission_bps.
    amendment_term_d.source_kind = "signed_amendment"
    db.add(amendment_term_d)
    db.flush()
    db.commit()
    ids["vendor_d_contract_term_id"] = contract_term_d.id
    ids["vendor_d_amendment_term_id"] = amendment_term_d.id
    _add_document_claim(
        db,
        vendor=vendor_d,
        doc_ref=doc_d_contract,
        speaker_role="SIGNED_AGREEMENT",
        commission_bps=1500,
        quoted_excerpt="This signed agreement sets commission at 15% of eligible sales, DAY1-DAY2.",
        native_id_suffix="vendor-d",
    )

    # =======================================================================
    # (e) R1 Fingerprint Test Vendor -- a plain clean vendor DEDICATED to
    # verify_r1_readiness.py's own fingerprint-stability proof (re-run with
    # unchanged inputs -> no new version; add a real new conflicting claim
    # -> new version, different fingerprint, status flips to blocked). Kept
    # entirely separate from (a) "R1 Ready Vendor" so that script's own
    # DB-mutating fingerprint-change step never contaminates (a)'s
    # always-clean "ready, zero failing checks" scenario across repeated
    # runs of verify_r1_readiness.py against the same long-lived dev DB.
    # =======================================================================
    vendor_e = get_or_create_vendor(
        db, name="R1 Fingerprint Test Vendor", beneficiary_verified=True
    )
    ids["vendor_e_id"] = vendor_e.id
    doc_e = _ingest_document(db, native_id="r1-dev-vendor-e-contract", label="vendor-e-contract")
    term_e = _get_or_create_term(
        db,
        vendor_id=vendor_e.id,
        commission_bps=1500,
        status="active",
        effective_from=DAY1,
        effective_to=DAY3,
        source_refs=[
            {
                "ref_kind": "document",
                "source_document_id": doc_e["source_document_id"],
                "source_version": doc_e["source_version"],
            }
        ],
    )
    ids["vendor_e_term_id"] = term_e.id
    _add_document_claim(
        db,
        vendor=vendor_e,
        doc_ref=doc_e,
        speaker_role="SIGNED_AGREEMENT",
        commission_bps=1500,
        quoted_excerpt="This signed agreement sets commission at 15% for vendor E.",
        native_id_suffix="vendor-e",
    )

    return ids


def add_conflicting_claims_if_absent(db: Session, *, vendor_id: str, effective_from: date) -> None:
    """Idempotently adds two new, mutually-conflicting status=proposed
    candidate terms to `vendor_id` -- used ONLY by
    verify_r1_readiness.py's own fingerprint-change proof (adding a real
    new input and showing the fingerprint/version/status all react). Safe
    to call repeatedly: looks up by (vendor_id, commission_bps,
    effective_from) first, same convention as `_get_or_create_term`."""
    _get_or_create_term(
        db,
        vendor_id=vendor_id,
        commission_bps=900,
        status="proposed",
        effective_from=effective_from,
        effective_to=None,
        source_refs=[{"locator_kind": "row", "locator": "r1-verify-new-conflicting-claim-1"}],
    )
    _get_or_create_term(
        db,
        vendor_id=vendor_id,
        commission_bps=1100,
        status="proposed",
        effective_from=effective_from,
        effective_to=None,
        source_refs=[{"locator_kind": "row", "locator": "r1-verify-new-conflicting-claim-2"}],
    )


if __name__ == "__main__":
    make_engine()
    session_factory = get_session_factory()
    db = session_factory()
    try:
        result = seed(db)
        import json

        print(json.dumps(result, indent=2))
    finally:
        db.close()
