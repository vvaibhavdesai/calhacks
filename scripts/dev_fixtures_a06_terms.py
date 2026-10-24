#!/usr/bin/env python3
"""THROWAWAY, HAND-CHECKED dev fixture for A06 stage 2 (terms resolution).
*** NOT B01. *** Same convention as dev_fixtures_a03..a09: written by the
author of the code under test; a development aid, not independent
verification.

Raw operational inputs plus VERIFIED document claims only. The claims are
written directly, standing in for what A06 stage 1 already records in
`document_extractions`/`document_claims` after a real Claude call (that
stage is done and its output lives in the database; re-calling the model
here would spend credit to re-derive rows this fixture states by hand).
Everything downstream -- contract terms, proposed conflict rows, cases,
settlements, payouts -- is produced by the real code under test.

Nothing seeds a `proposed` VendorContractTerm or a case: if the Night 1
conflict case exists after a run, terms resolution opened it by itself.

Night 1 (D1 = 2026-10-16), non-final, $500 festival minimum (not applied
on a non-final night), processor fees not deductible. Five vendors, each
with one card order of 100.00 + 9.25 tax:

  Taco     contract 15% (recv 10-01) + organizer email 10% (recv 10-16)
           -> two `proposed` rows for 10-16, A05 opens
              vendor_contract_term_conflict (impact 1500-1000 = 500)
           -> HELD at TERMS_RESOLVED, nothing settled
  Burrito  contract 12% (recv 10-01) + organizer email 12% (recv 10-16)
           -> the email RESTATES the rate in force: corroboration, no
              conflict, no case -> RESOLVED, fee 1200, payout 8800
  Noodle   contract 15% + vendor email "minimum is per night" (recv 10-16)
           -> NON-blocking minimum_period_claim case; the contract stands
           -> RESOLVED, fee 1500, payout 8500
  Bank     contract 15% + email requesting a new payout account (10-16)
           -> blocking beneficiary_change_unverified case; no payment
              detail is read or applied -> HELD
  Amend    contract 15% (recv 10-01) + SIGNED amendment 10% (recv 10-17)
           -> Night 1 does not see the amendment: v1 15% from 10-01,
              fee 1500, payout 8500. Night 2 does: v2 10% from 10-17,
              and v1 is untouched.

  Deposit DEP-1 (settled): gross 5 x 10,925 = 54,625, fee 164,
  bank 54,461. Cash pool funded $10,000.00.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))
sys.path.insert(0, os.path.dirname(__file__))

from app.ingestion import import_source  # noqa: E402
from app.models_agents import DocumentClaim, DocumentExtraction  # noqa: E402
from app.models_ledger import SourceDocument  # noqa: E402
from app.payout_dispatch import fund_cash_pool  # noqa: E402
from app.storage.local_fs import get_document_storage  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import dev_fixtures_a08 as a08f  # noqa: E402

D1 = date(2026, 10, 16)
D2 = date(2026, 10, 17)

ELIGIBLE = 10_000
TAX = 925
VENDORS = ("Taco", "Burrito", "Noodle", "Bank", "Amend")
CASH_POOL_CENTS = 1_000_000
DEPOSIT = {"gross": 5 * (ELIGIBLE + TAX), "fee": 164}

# Hand-worked expectations for the five vendors on Night 1.
EXPECTED = {
    "Taco": {"status": "HELD", "reason_code": "vendor_contract_term_conflict", "impact": 500},
    "Burrito": {"status": "RESOLVED", "bps": 1200, "fee": 1200, "payout": 8800},
    "Noodle": {"status": "RESOLVED", "bps": 1500, "fee": 1500, "payout": 8500},
    "Bank": {"status": "HELD", "reason_code": "beneficiary_change_unverified"},
    "Amend": {"status": "RESOLVED", "bps": 1500, "fee": 1500, "payout": 8500},
}


def _at(d: date, hour: int) -> datetime:
    """A received_at in UTC that is unambiguously inside the Pacific day
    `d` (Pacific is UTC-7 in October), so the business date a claim becomes
    usable on is the one this fixture means."""
    return datetime(d.year, d.month, d.day, hour + 7, 0, tzinfo=UTC)


def _document(
    db: Session,
    *,
    native: str,
    vendor_id: str,
    document_kind: str,
    received_at: datetime,
    body: dict,
) -> tuple[SourceDocument, str, int, str]:
    """Import one document's bytes through A02's real `import_source`
    (hash-preserving) and tag it with the intake metadata A06 stage 1
    reads: which vendor it concerns and what kind of document it is."""
    result = import_source(
        db,
        get_document_storage(),
        source_system="a06-terms-dev-fixture",
        source_native_id=native,
        content_bytes=json.dumps(body, sort_keys=True).encode(),
        mime_type="application/json",
        actor_id="dev-fixture-script",
        actor_capability="import_source",
        received_at=received_at,
    )
    doc = db.get(SourceDocument, result.source_document_id)
    doc.vendor_id = vendor_id
    doc.document_kind = document_kind
    db.flush()
    return doc, result.source_version_id, result.version, result.content_hash


def _extraction(db: Session, doc: SourceDocument, version_id: str, content_hash: str) -> str:
    ext = DocumentExtraction(
        source_document_id=doc.id,
        source_version_id=version_id,
        content_hash=content_hash,
        parser="dev-fixture",
        text_sha256=content_hash,
        model_requested="dev-fixture",
        model_served="dev-fixture",
        prompt_version="dev-fixture",
        status="SUCCEEDED",
        retryable=False,
        document_kind=doc.document_kind,
        summary="hand-written stand-in for a recorded A06 stage 1 extraction",
        embedded_instructions_detected=False,
    )
    db.add(ext)
    db.flush()
    return ext.id


def _claims(
    db: Session,
    *,
    vendor_id: str,
    vendor_name: str,
    native: str,
    document_kind: str,
    received_at: datetime,
    speaker_role: str,
    claims: list[dict],
) -> None:
    """One document and the verified claims it yielded. `claims` entries:
    {kind, raw, excerpt, restates?, and the normalized value field}."""
    doc, version_id, version, content_hash = _document(
        db,
        native=native,
        vendor_id=vendor_id,
        document_kind=document_kind,
        received_at=received_at,
        body={"document": native, "vendor": vendor_name, "claims": claims},
    )
    extraction_id = _extraction(db, doc, version_id, content_hash)
    for c in claims:
        db.add(
            DocumentClaim(
                extraction_id=extraction_id,
                source_document_id=doc.id,
                vendor_id=vendor_id,
                vendor_name_claimed=vendor_name,
                claim_kind=c["kind"],
                value_raw=c["raw"],
                commission_bps=c.get("commission_bps"),
                minimum_fee_cents=c.get("minimum_fee_cents"),
                minimum_period=c.get("minimum_period"),
                processing_fee_deductible=c.get("processing_fee_deductible"),
                effective_from=c.get("effective_from"),
                speaker_role=speaker_role,
                restates_existing_terms=bool(c.get("restates")),
                available_from=received_at,
                quoted_excerpt=c["excerpt"],
                page=1,
                verified=True,
                verification_notes=["dev fixture: citation and value hand-checked"],
                source_ref={
                    "source_document_id": doc.id,
                    "source_version": version,
                    "content_hash": content_hash,
                    "locator_kind": "page",
                    "locator": "page=1",
                    "quoted_excerpt": c["excerpt"],
                },
            )
        )
    db.flush()


def _contract(db: Session, vendor, native: str, bps: int) -> None:
    _claims(
        db,
        vendor_id=vendor.id,
        vendor_name=vendor.name,
        native=native,
        document_kind="CONTRACT",
        received_at=_at(date(2026, 10, 1), 9),
        speaker_role="SIGNED_AGREEMENT",
        claims=[
            {
                "kind": "COMMISSION_RATE",
                "raw": f"{bps / 100:.2f}%",
                "commission_bps": bps,
                "excerpt": f"Organizer commission: {bps / 100:.2f}% of eligible sales.",
            },
            {
                "kind": "MINIMUM_FEE",
                "raw": "$500.00",
                "minimum_fee_cents": 50_000,
                "excerpt": "Minimum organizer fee: $500.00 for the engagement.",
            },
            {
                "kind": "MINIMUM_PERIOD",
                "raw": "for the entire festival",
                "minimum_period": "FESTIVAL",
                "excerpt": "The minimum fee applies once for the entire festival.",
            },
            {
                "kind": "PROCESSING_FEE_DEDUCTION",
                "raw": "not deductible",
                "processing_fee_deductible": False,
                "excerpt": "Card processing fees are borne by the Organizer.",
            },
        ],
    )


def seed(db: Session, festival_id: str, run_tag: str) -> dict:
    m = a08f.manifest(db, f"a06terms-{run_tag}")
    vendors = {n: a08f.vendor(db, festival_id, n) for n in VENDORS}
    ids = {n: v.id for n, v in vendors.items()}
    p = festival_id
    card = []

    def card_order(vname: str, native: str, d: date) -> None:
        o = a08f.order(
            db, m, vendors[vname], f"{p}-{native}", d, [("L1", "prepared_food", ELIGIBLE, TAX, 0)]
        )
        card.append(
            a08f.payment(
                db,
                m,
                o,
                f"{p}-{native}-P",
                method="card",
                custody="centrally_collected",
                amount=ELIGIBLE + TAX,
            )
        )

    for i, name in enumerate(VENDORS, start=1):
        card_order(name, f"O{i}", D1)

    assert sum(pm.amount_cents for pm in card) == DEPOSIT["gross"]
    be = a08f.deposit(db, m, festival_id, f"{p}-DEP1", D1, DEPOSIT["gross"], DEPOSIT["fee"])
    for pm in card:
        pm.bank_entry_id = be.id

    # --- the documents, and the verified claims A06 stage 1 got from them

    for name, bps in (
        ("Taco", 1500),
        ("Burrito", 1200),
        ("Noodle", 1500),
        ("Bank", 1500),
        ("Amend", 1500),
    ):
        _contract(db, vendors[name], f"{p}-{name}-CONTRACT", bps)

    # Taco: organizer staff state a DIFFERENT rate -> conflict for a human.
    _claims(
        db,
        vendor_id=ids["Taco"],
        vendor_name="Taco",
        native=f"{p}-Taco-EMAIL",
        document_kind="EMAIL",
        received_at=_at(D1, 11),
        speaker_role="ORGANIZER_STAFF",
        claims=[
            {
                "kind": "COMMISSION_RATE",
                "raw": "10%",
                "commission_bps": 1000,
                "excerpt": "Just confirming we agreed 10% with this vendor for the weekend.",
            }
        ],
    )

    # Burrito: organizer staff RESTATE the rate in force -> corroboration.
    _claims(
        db,
        vendor_id=ids["Burrito"],
        vendor_name="Burrito",
        native=f"{p}-Burrito-EMAIL",
        document_kind="EMAIL",
        received_at=_at(D1, 11),
        speaker_role="ORGANIZER_STAFF",
        claims=[
            {
                "kind": "COMMISSION_RATE",
                "raw": "12%",
                "commission_bps": 1200,
                "restates": True,
                "excerpt": "As per the signed agreement, their commission is 12%.",
            }
        ],
    )

    # Noodle: the vendor says the minimum is per night -> non-blocking.
    _claims(
        db,
        vendor_id=ids["Noodle"],
        vendor_name="Noodle",
        native=f"{p}-Noodle-EMAIL",
        document_kind="EMAIL",
        received_at=_at(D1, 11),
        speaker_role="VENDOR",
        claims=[
            {
                "kind": "MINIMUM_PERIOD",
                "raw": "per night",
                "minimum_period": "NIGHTLY",
                "excerpt": "Our understanding is the $500 minimum is charged per night.",
            }
        ],
    )

    # Bank: a payout-account change by email -> blocking, never applied.
    _claims(
        db,
        vendor_id=ids["Bank"],
        vendor_name="Bank",
        native=f"{p}-Bank-EMAIL",
        document_kind="EMAIL",
        received_at=_at(D1, 11),
        speaker_role="VENDOR",
        claims=[
            {
                "kind": "BENEFICIARY_CHANGE",
                "raw": "new payout account requested",
                "excerpt": "Please send this week's payment to our new account instead.",
            }
        ],
    )

    # Amend: a SIGNED amendment received on Night 2, not Night 1 (DA18/DA19).
    _claims(
        db,
        vendor_id=ids["Amend"],
        vendor_name="Amend",
        native=f"{p}-Amend-AMENDMENT",
        document_kind="AMENDMENT",
        received_at=_at(D2, 10),
        speaker_role="SIGNED_AGREEMENT",
        claims=[
            {
                "kind": "COMMISSION_RATE",
                "raw": "10.00%",
                "commission_bps": 1000,
                "excerpt": "Section 3 is amended to read: Organizer commission 10.00%.",
            }
        ],
    )

    fund_cash_pool(
        db,
        festival_id=festival_id,
        additional_cents=CASH_POOL_CENTS,
        actor_id="dev-fixture-script",
        actor_capability="admin",
    )
    db.commit()
    return ids
