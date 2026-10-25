#!/usr/bin/env python3
"""THROWAWAY, HAND-CHECKED dev fixtures for X0.2 (term provenance and
precedence) verification.

*** THIS IS NOT B01. *** Same standing disclaimer as every other
scripts/dev_fixtures_*.py in this repo -- see dev_fixtures_a05.py's own
docstring for the full explanation of why (no change here).

Three independent scenarios, each proving one of TICKET_SPEC.md's X0.2
"Done when" criteria against REAL rows / REAL production code paths, not
synthetic stand-ins:

  1. "X0.2 Precedence Vendor" -- a plain `source_kind="contract"` term
     covering the whole festival window (DAY1-DAY3, 18% commission) and a
     `source_kind="signed_amendment"` term covering only DAY2-DAY3 (10%
     commission), the latter recorded through
     app.term_provenance.record_signed_amendment with a REAL document
     citation (a real SourceDocument/SourceVersion row, via the same
     app.ingestion.import_source A02/A05/A06 already use). Proves "an
     amendment overrides the contract only inside its effective dates."

  2. "X0.2 Interpretation Vendor" -- a REAL contract-term conflict
     detected through app.policy.detect_contract_term_conflicts (the exact
     production code A05's own case-opening uses, not a bare Case(...)
     construction), an approved-interpretation term recorded through
     app.term_provenance.record_approved_interpretation citing that real
     case, and one ACTIVE PolicyVersion row whose scope/predicates match
     the case's own recorded facts (standing in for what R2's future
     "approve interpretation as reusable" decision will eventually create
     through its own endpoint -- X0.2 does not build that endpoint, only
     the data model/precedence function it will call). Proves "an
     approved interpretation applies only through a matching rule when
     sources conflict" -- the positive case.

  3. "X0.2 Unmatched Interpretation Vendor" -- the exact same conflict +
     interpretation-term setup as (2), but deliberately WITHOUT a matching
     PolicyVersion. Proves the negative case of the same rule: an
     interpretation term that exists, with dates covering the target date,
     still never applies on its own -- no matching rule, no effect.

Usage (from repo root, real Postgres required, no SQLite):
    DATABASE_URL=... uv run python scripts/dev_fixtures_x02.py

Safe to re-run: every entity is looked up by a natural key before being
created, same convention as dev_fixtures_a03/a04/a05/x01.
"""

from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app.db import get_session_factory, make_engine  # noqa: E402
from app.ingestion import import_source  # noqa: E402
from app.models import _new_id  # noqa: E402
from app.models_ledger import PolicyVersion, Vendor, VendorContractTerm  # noqa: E402
from app.policy import CONTRACT_CONFLICT_REASON_CODE, detect_contract_term_conflicts  # noqa: E402
from app.storage.local_fs import get_document_storage  # noqa: E402
from app.term_provenance import (  # noqa: E402
    record_approved_interpretation,
    record_signed_amendment,
)
from app.uow import UnitOfWork  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

FESTIVAL_ID = "gcmf-2026-x02dev"
SOURCE_SYSTEM = "x02_devfixtures"
_AMENDMENT_DOC_NATIVE_ID = "x02-dev-fixture-signed-amendment-v1"

DAY1 = date(2026, 10, 16)
DAY2 = date(2026, 10, 17)
DAY3 = date(2026, 10, 18)
DAY4 = date(2026, 10, 19)  # after both the contract's and the amendment's effective_to

VENDOR_ELIGIBLE_SALES_CENTS = 1_000_000  # $10,000.00 -- any value works; not asserted on


def _ingest_amendment_document(db: Session) -> dict:
    storage = get_document_storage()
    content = (
        b'{"name": "x02-dev-fixture-signed-amendment", '
        b'"note": "throwaway hand-checked X0.2 dev fixture, not a real signed document"}'
    )
    result = import_source(
        db,
        storage,
        source_system=SOURCE_SYSTEM,
        source_native_id=_AMENDMENT_DOC_NATIVE_ID,
        content_bytes=content,
        mime_type="application/json",
        actor_id="dev-fixture-script",
        actor_capability="import_source",
    )
    db.commit()
    return {
        "ref_kind": "document",
        "source_document_id": result.source_document_id,
        "source_version": result.version,
        "content_hash": result.content_hash,
        "locator_kind": "row",
        "locator": "x02-dev-amendment-signature-page",
    }


def get_or_create_vendor(db: Session, *, name: str) -> Vendor:
    existing = db.execute(
        select(Vendor).where(Vendor.festival_id == FESTIVAL_ID, Vendor.name == name)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    vendor = Vendor(festival_id=FESTIVAL_ID, name=name)
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
    db: Session, *, vendor_id: str, commission_bps: int
) -> VendorContractTerm | None:
    return db.execute(
        select(VendorContractTerm).where(
            VendorContractTerm.vendor_id == vendor_id,
            VendorContractTerm.commission_bps == commission_bps,
        )
    ).scalar_one_or_none()


def seed(db: Session, *, admin_id: str) -> dict:
    ids: dict = {
        "festival_id": FESTIVAL_ID,
        "day1": DAY1.isoformat(),
        "day2": DAY2.isoformat(),
        "day3": DAY3.isoformat(),
        "day4": DAY4.isoformat(),
    }

    # =======================================================================
    # Scenario 1: contract (whole festival, 18%) vs. signed amendment
    # (DAY2-DAY3 only, 10%). 18% is deliberately NOT the same as the
    # festival default (15%, domain.settlement.DEFAULT_COMMISSION_BPS) so
    # "the contract term won" and "the default applied" are never
    # numerically ambiguous in the verify script's assertions.
    # =======================================================================
    vendor_a = get_or_create_vendor(db, name="X0.2 Precedence Vendor")
    ids["vendor_a_id"] = vendor_a.id

    contract_term = _get_or_create_term(db, vendor_id=vendor_a.id, commission_bps=1800)
    if contract_term is None:
        contract_term = VendorContractTerm(
            vendor_id=vendor_a.id,
            version=_next_version(db, vendor_a.id),
            commission_bps=1800,
            minimum_fee_cents=None,
            processor_fee_deductible=False,
            effective_from=DAY1,
            effective_to=DAY3,
            status="active",
            # source_kind deliberately NOT passed -- proves the model/
            # migration default ("contract") applies, exactly like every
            # VendorContractTerm row that existed before X0.2.
            source_refs=[{"locator_kind": "row", "locator": "x02-dev-contract-pdf-clause-4"}],
            approved_by=admin_id,
        )
        db.add(contract_term)
        db.flush()
        db.commit()
    ids["vendor_a_contract_term_id"] = contract_term.id
    assert (
        contract_term.source_kind == "contract"
    ), f"expected default source_kind='contract', got {contract_term.source_kind!r}"

    amendment_term = _get_or_create_term(db, vendor_id=vendor_a.id, commission_bps=1000)
    if amendment_term is None:
        document_ref = _ingest_amendment_document(db)
        uow = UnitOfWork(db)
        amendment_term = record_signed_amendment(
            db,
            uow,
            vendor=vendor_a,
            commission_bps=1000,
            minimum_fee_cents=None,
            processor_fee_deductible=False,
            effective_from=DAY2,
            effective_to=DAY3,
            document_source_ref=document_ref,
            actor_id=admin_id,
            actor_capability="record_amendment",
        )
        db.commit()
    ids["vendor_a_amendment_term_id"] = amendment_term.id

    # =======================================================================
    # Scenarios 2 and 3: a REAL contract-term conflict (contract 1500bps
    # vs. producer email 1000bps, same shape as dev_fixtures_a05's
    # canonical taco-vendor scenario), detected through the real
    # app.policy.detect_contract_term_conflicts production code path, then
    # resolved by recording an approved interpretation (NOT an amendment --
    # structurally distinct, see app/term_provenance.py). Two vendors,
    # built by the same helper, differing only in whether a matching
    # ACTIVE PolicyVersion is ALSO created:
    #   - vendor_b ("X0.2 Interpretation Vendor"): case + interpretation
    #     term + a matching ACTIVE PolicyVersion -- the interpretation
    #     SHOULD apply.
    #   - vendor_c ("X0.2 Unmatched Interpretation Vendor"): case +
    #     interpretation term, deliberately NO PolicyVersion -- the
    #     interpretation must NOT apply, proving it never silently
    #     overrides anything on facts/dates alone, only through an actual
    #     approved matching rule.
    # =======================================================================
    vendor_b_ids = _build_conflict_and_interpretation(
        db, name="X0.2 Interpretation Vendor", admin_id=admin_id, create_matching_policy=True
    )
    ids.update({f"vendor_b_{k}": v for k, v in vendor_b_ids.items()})

    vendor_c_ids = _build_conflict_and_interpretation(
        db,
        name="X0.2 Unmatched Interpretation Vendor",
        admin_id=admin_id,
        create_matching_policy=False,
    )
    ids.update({f"vendor_c_{k}": v for k, v in vendor_c_ids.items()})

    return ids


def _build_conflict_and_interpretation(
    db: Session, *, name: str, admin_id: str, create_matching_policy: bool
) -> dict:
    vendor = get_or_create_vendor(db, name=name)
    result: dict = {"id": vendor.id}

    contract_claim = db.execute(
        select(VendorContractTerm).where(
            VendorContractTerm.vendor_id == vendor.id,
            VendorContractTerm.status == "proposed",
            VendorContractTerm.commission_bps == 1500,
        )
    ).scalar_one_or_none()
    if contract_claim is None:
        contract_claim = VendorContractTerm(
            vendor_id=vendor.id,
            version=_next_version(db, vendor.id),
            commission_bps=1500,
            minimum_fee_cents=None,
            processor_fee_deductible=False,
            effective_from=DAY1,
            effective_to=None,
            status="proposed",
            source_refs=[{"locator_kind": "row", "locator": f"x02-dev-{vendor.id}-contract-15pct"}],
        )
        db.add(contract_claim)
        db.flush()

    email_claim = db.execute(
        select(VendorContractTerm).where(
            VendorContractTerm.vendor_id == vendor.id,
            VendorContractTerm.status == "proposed",
            VendorContractTerm.commission_bps == 1000,
        )
    ).scalar_one_or_none()
    if email_claim is None:
        email_claim = VendorContractTerm(
            vendor_id=vendor.id,
            version=_next_version(db, vendor.id),
            commission_bps=1000,
            minimum_fee_cents=None,
            processor_fee_deductible=False,
            effective_from=DAY1,
            effective_to=None,
            status="proposed",
            source_refs=[{"locator_kind": "row", "locator": f"x02-dev-{vendor.id}-email-10pct"}],
        )
        db.add(email_claim)
        db.flush()
    db.commit()
    result["contract_claim_id"] = contract_claim.id
    result["email_claim_id"] = email_claim.id

    uow = UnitOfWork(db)
    case = detect_contract_term_conflicts(
        db,
        uow,
        vendor=vendor,
        business_date_=DAY1,
        eligible_sales_cents_today=VENDOR_ELIGIBLE_SALES_CENTS,
        run_id=None,
        actor_id=admin_id,
        actor_capability="resolve_case",
    )
    db.commit()
    assert case is not None, f"expected a real conflict case to be opened for {name}"
    assert case.reason_code == CONTRACT_CONFLICT_REASON_CODE
    result["case_id"] = case.id

    interpretation_term = db.execute(
        select(VendorContractTerm).where(
            VendorContractTerm.vendor_id == vendor.id,
            VendorContractTerm.source_kind == "approved_interpretation",
        )
    ).scalar_one_or_none()
    if interpretation_term is None:
        uow = UnitOfWork(db)
        interpretation_term = record_approved_interpretation(
            db,
            uow,
            vendor=vendor,
            commission_bps=1000,
            minimum_fee_cents=None,
            processor_fee_deductible=False,
            effective_from=DAY1,
            effective_to=None,
            case_id=case.id,
            actor_id=admin_id,
            actor_capability="approve_interpretation",
        )
        db.commit()
    result["interpretation_term_id"] = interpretation_term.id

    if create_matching_policy:
        # The scoped rule R2's future "approve interpretation as reusable"
        # decision will create through app/policy.py's `approve_case_policy`.
        # Built directly here (not through that endpoint) because X0.2 is
        # verifying app.term_provenance's own "matching rule" integration,
        # not re-exercising A05's resolve/approve-policy HTTP flow a
        # second time (verify_a05_policy.py already covers that in full).
        policy = db.execute(
            select(PolicyVersion).where(PolicyVersion.source_case_id == case.id)
        ).scalar_one_or_none()
        if policy is None:
            policy = PolicyVersion(
                policy_id=_new_id(),
                version=1,
                scope={"vendor_id": vendor.id, "festival_id": vendor.festival_id},
                predicates=case.predicate_values,
                effect={"commission_bps": 1000},
                evidence_refs=case.evidence_refs,
                approver_ref=admin_id,
                valid_from=DAY1,
                valid_to=None,
                status="ACTIVE",
                supersedes_version=None,
                source_case_id=case.id,
            )
            db.add(policy)
            db.flush()
            db.commit()
        result["policy_version_id"] = policy.id

    return result


if __name__ == "__main__":
    make_engine()
    session_factory = get_session_factory()
    db = session_factory()
    try:
        from app.models import User

        admin = db.execute(select(User).where(User.email == "admin@example.com")).scalar_one()
        result = seed(db, admin_id=admin.id)
        import json

        print(json.dumps(result, indent=2))
    finally:
        db.close()
