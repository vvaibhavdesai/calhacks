#!/usr/bin/env python3
"""THROWAWAY, HAND-CHECKED dev fixtures for A05 exceptions/approvals/
scoped-policies verification.

*** THIS IS NOT B01. *** B01 ("compact synthetic fixture and independent
oracle") does not exist in this repository -- see docs/progress/A02 through
A04-handoff.md. Per the user's explicit decision, carried forward unchanged
into this task: this script builds a small, clearly-labeled, hand-checkable
substitute for the canonical "taco vendor 15% contract vs 10% producer
email" three-night scenario from AGENT_HANDOFF.md, with every expected
number worked out by hand in the comments below and re-checked mechanically
by scripts/verify_a05_policy.py. The same person who wrote app/policy.py
and packages/domain/domain/policy.py also wrote the expected answers here --
a development/demo aid, not evidence of independent verification.

This script only builds the RAW DATA a human would already have imported
before any case/policy workflow runs: vendors, "proposed" (conflicting)
VendorContractTerm rows standing in for a signed contract PDF vs a producer
email, and (for the two vendors whose actual dollar settlement this script
checks) matched orders/payments/bank entries exactly like
scripts/dev_fixtures_a04.py already does. It deliberately does NOT call
POST /api/v1/cases/detect-contract-conflicts, /resolve, /approve-policy, or
/settlements/calculate itself -- those are real HTTP commands against a
real running API, driven by scripts/verify_a05_policy.py, because they are
exactly the workflow this task is responsible for proving actually works.

Four vendors, one shared festival_id, two business dates for the main
scenario (DAY1/DAY2) plus a third (DAY3) for the expiry fixture:

  - "A05 Taco Vendor"       (A): the canonical Night 1 / Night 2 scenario.
  - "A05 Churro Vendor"     (B): a normal, unrelated vendor -- proves
                                  resolving A's case never touches B.
  - "A05 Lemonade Vendor"   (C): policy EXPIRY re-escalation fixture.
  - "A05 Kettle Corn Vendor"(D): CONTRADICTING NEW FACT re-escalation
                                  fixture, and (on its own Day 1) a scope-
                                  exclusivity check -- D's conflict has the
                                  SAME predicate shape as Taco Vendor's, but
                                  Taco Vendor's (vendor-scoped) policy must
                                  NOT auto-resolve it.

Usage (from repo root, real Postgres required, no SQLite):
    DATABASE_URL=... uv run python scripts/dev_fixtures_a05.py

Safe to re-run: every entity is looked up by a natural key before being
created, same convention as dev_fixtures_a03/a04.
"""

from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app.db import get_session_factory, make_engine  # noqa: E402
from app.ingestion import import_source  # noqa: E402
from app.models_ledger import Vendor, VendorContractTerm  # noqa: E402
from app.storage.local_fs import get_document_storage  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import dev_fixtures_a04 as a04f  # noqa: E402 -- reuse order/payment/bank_entry helpers

FESTIVAL_ID = "gcmf-2026-a05dev"
SOURCE_SYSTEM = "a05_devfixtures"
_MANIFEST_NATIVE_ID = "a05-dev-fixture-manifest-v1"

DAY1 = date(2026, 10, 16)
DAY2 = date(2026, 10, 17)
DAY3 = date(2026, 10, 18)


def _ingest_manifest(db: Session) -> tuple[str, int, str]:
    storage = get_document_storage()
    content = (
        b'{"name": "a05-dev-fixture-manifest", "version": 1, '
        b'"note": "throwaway hand-checked A05 dev fixtures, not B01"}'
    )
    result = import_source(
        db,
        storage,
        source_system=SOURCE_SYSTEM,
        source_native_id=_MANIFEST_NATIVE_ID,
        content_bytes=content,
        mime_type="application/json",
        actor_id="dev-fixture-script",
        actor_capability="import_source",
    )
    db.commit()
    return result.source_document_id, result.version, result.content_hash


def _source_ref(manifest: tuple[str, int, str], locator: str) -> dict:
    doc_id, version, content_hash = manifest
    return {
        "source_document_id": doc_id,
        "source_version": version,
        "content_hash": content_hash,
        "locator_kind": "row",
        "locator": locator,
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


def get_or_create_proposed_term(
    db: Session,
    *,
    vendor: Vendor,
    commission_bps: int,
    role: str,
    manifest: tuple[str, int, str],
    effective_from: date = DAY1,
) -> VendorContractTerm:
    """A candidate "proposed" (not yet active) VendorContractTerm, standing
    in for one source's claimed commission rate -- `role` is "contract" or
    "producer_email", used only to build a readable locator string."""
    existing = db.execute(
        select(VendorContractTerm).where(
            VendorContractTerm.vendor_id == vendor.id,
            VendorContractTerm.status == "proposed",
            VendorContractTerm.commission_bps == commission_bps,
            VendorContractTerm.effective_from == effective_from,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    term = VendorContractTerm(
        vendor_id=vendor.id,
        version=_next_version(db, vendor.id),
        commission_bps=commission_bps,
        minimum_fee_cents=None,
        processor_fee_deductible=False,
        effective_from=effective_from,
        effective_to=None,
        status="proposed",
        source_refs=[_source_ref(manifest, f"{role}:{vendor.name}:{commission_bps}bps")],
    )
    db.add(term)
    db.flush()
    return term


def _next_version(db: Session, vendor_id: str) -> int:
    max_version = db.execute(
        select(VendorContractTerm.version)
        .where(VendorContractTerm.vendor_id == vendor_id)
        .order_by(VendorContractTerm.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    return (max_version or 0) + 1


def retire_term(db: Session, term: VendorContractTerm) -> None:
    """Simulates "a later producer email supersedes an earlier one": the
    old candidate term is removed from `find_candidate_conflicting_terms`'
    consideration (status flips away from "proposed") without deleting the
    row -- same "never silently drop, just transition status" convention
    used throughout this schema."""
    term.status = "proposed_superseded"
    db.add(term)
    db.flush()


def seed(db: Session) -> dict:
    manifest = _ingest_manifest(db)
    ids: dict[str, str] = {}

    # =======================================================================
    # Vendor A: "A05 Taco Vendor" -- the canonical Night 1 / Night 2 scenario.
    # Contract says 15% (1500 bps); producer email says 10% (1000 bps).
    #   DAY1 eligible = 1,000,000 cents ($10,000.00)
    #   DAY2 eligible = 1,200,000 cents ($12,000.00)
    #   financial_impact_cents (informative, non-cumulative, per-night):
    #     DAY1: 15%*1,000,000=150,000 vs 10%*1,000,000=100,000 -> 50,000 ($500)
    #     DAY2: 15%*1,200,000=180,000 vs 10%*1,200,000=120,000 -> 60,000 ($600)
    #   Once resolved at 10% (commission_bps=1000) and made ACTIVE for the
    #   rest of the festival:
    #     DAY1 settlement (not final): fee=10%*1,000,000=100,000 ($1,000);
    #       payout=1,000,000-100,000=900,000 ($9,000)
    #     DAY2 settlement (not final): cumulative=2,200,000; pct_to_date=
    #       10%*2,200,000=220,000; increment=220,000-100,000=120,000
    #       ($1,200, == 10% of DAY2's own $12,000); payout=1,200,000-120,000
    #       =1,080,000 ($10,800)
    # =======================================================================
    va = get_or_create_vendor(db, name="A05 Taco Vendor")
    hi_term_a = get_or_create_proposed_term(
        db, vendor=va, commission_bps=1500, role="contract", manifest=manifest
    )
    lo_term_a = get_or_create_proposed_term(
        db, vendor=va, commission_bps=1000, role="producer_email", manifest=manifest
    )
    for day, suffix, gross in ((DAY1, "d1", 1_000_000), (DAY2, "d2", 1_200_000)):
        order = a04f.get_or_create_order(
            db,
            vendor=va,
            native_id=f"a05-taco-order-{suffix}",
            business_date_=day,
            gross_cents=gross,
            tax_cents=0,
            tips_cents=0,
            manifest=manifest,
        )
        bank = a04f.get_or_create_bank_entry(
            db,
            festival_id=FESTIVAL_ID,
            provider_reference=f"a05-taco-bank-{suffix}",
            amount_cents=gross,
            batch_fee_cents=None,
            business_date_=day,
            manifest=manifest,
        )
        payment = a04f.get_or_create_payment(
            db,
            vendor=va,
            order=order,
            native_id=f"a05-taco-payment-{suffix}",
            custody="centrally_collected",
            amount_cents=gross,
            manifest=manifest,
            bank_entry_id=bank.id,
        )
        a04f.create_matched_order_group(
            db,
            festival_id=FESTIVAL_ID,
            order=order,
            payment=payment,
            matched_amount_cents=gross,
            manifest=manifest,
        )
    ids["festival_id"] = FESTIVAL_ID
    ids["taco_vendor_id"] = va.id
    ids["taco_contract_term_id"] = hi_term_a.id
    ids["taco_email_term_id"] = lo_term_a.id
    ids["day1"] = DAY1.isoformat()
    ids["day2"] = DAY2.isoformat()
    ids["day3"] = DAY3.isoformat()
    ids["taco_day1_eligible_cents"] = 1_000_000
    ids["taco_day2_eligible_cents"] = 1_200_000

    # =======================================================================
    # Vendor B: "A05 Churro Vendor" -- normal, unrelated vendor. No contract
    # conflict, no case ever opens for this vendor. $5,000 eligible both
    # nights, festival-default 15%/$500 terms (no VendorContractTerm row at
    # all -- NULL means "use the festival default", per models_ledger.py).
    #   DAY1: fee=15%*500,000=75,000 ($750); payout=425,000 ($4,250)
    #   DAY2: cumulative=1,000,000; pct_to_date=150,000; increment=
    #     150,000-75,000=75,000 ($750); payout=500,000-75,000=425,000 ($4,250)
    # =======================================================================
    vb = get_or_create_vendor(db, name="A05 Churro Vendor")
    for day, suffix in ((DAY1, "d1"), (DAY2, "d2")):
        order = a04f.get_or_create_order(
            db,
            vendor=vb,
            native_id=f"a05-churro-order-{suffix}",
            business_date_=day,
            gross_cents=500_000,
            tax_cents=0,
            tips_cents=0,
            manifest=manifest,
        )
        bank = a04f.get_or_create_bank_entry(
            db,
            festival_id=FESTIVAL_ID,
            provider_reference=f"a05-churro-bank-{suffix}",
            amount_cents=500_000,
            batch_fee_cents=None,
            business_date_=day,
            manifest=manifest,
        )
        payment = a04f.get_or_create_payment(
            db,
            vendor=vb,
            order=order,
            native_id=f"a05-churro-payment-{suffix}",
            custody="centrally_collected",
            amount_cents=500_000,
            manifest=manifest,
            bank_entry_id=bank.id,
        )
        a04f.create_matched_order_group(
            db,
            festival_id=FESTIVAL_ID,
            order=order,
            payment=payment,
            matched_amount_cents=500_000,
            manifest=manifest,
        )
    ids["churro_vendor_id"] = vb.id

    # =======================================================================
    # Vendor C: "A05 Lemonade Vendor" -- policy EXPIRY re-escalation fixture.
    # Contract says 15%; producer email says 12% (1200 bps) -- deliberately
    # different magnitude than Taco Vendor's, so the two conflicts are
    # distinguishable by their own numbers. No order/payment fixtures needed
    # -- this vendor's case/policy behavior is what's under test, not its
    # settlement math (detect_contract_term_conflicts takes eligible sales
    # as a plain caller-supplied figure for the financial-impact
    # computation, not derived from match_groups).
    #   financial_impact_cents (eligible=600,000 / $6,000, hand-picked):
    #     15%*600,000=90,000 vs 12%*600,000=72,000 -> 18,000 ($180)
    # =======================================================================
    vc = get_or_create_vendor(db, name="A05 Lemonade Vendor")
    get_or_create_proposed_term(
        db, vendor=vc, commission_bps=1500, role="contract", manifest=manifest
    )
    get_or_create_proposed_term(
        db, vendor=vc, commission_bps=1200, role="producer_email", manifest=manifest
    )
    ids["lemonade_vendor_id"] = vc.id
    ids["lemonade_eligible_cents"] = 600_000

    # =======================================================================
    # Vendor D: "A05 Kettle Corn Vendor" -- CONTRADICTING NEW FACT
    # re-escalation fixture. Day 1 conflict has the SAME shape as Taco
    # Vendor's (1500 vs 1000) -- deliberately, to additionally prove scope
    # exclusivity (Taco Vendor's policy, scoped to Taco Vendor's vendor_id,
    # must NOT auto-resolve Kettle Corn Vendor's Day 1 case even though the
    # predicate values match byte-for-byte).
    #   financial_impact_cents (eligible=700,000 / $7,000, hand-picked):
    #     15%*700,000=105,000 vs 10%*700,000=70,000 -> 35,000 ($350)
    # The Day 2 contradiction (superseding the 1000bps email with a NEW
    # 1200bps one) is applied by apply_day2_contradiction() below, called
    # explicitly by scripts/verify_a05_policy.py BETWEEN its Day 1 and Day 2
    # steps for this vendor -- not from seed() itself, same sequencing
    # convention as dev_fixtures_a04.py's apply_fixture5_late_refund().
    # =======================================================================
    vd = get_or_create_vendor(db, name="A05 Kettle Corn Vendor")
    get_or_create_proposed_term(
        db, vendor=vd, commission_bps=1500, role="contract", manifest=manifest
    )
    get_or_create_proposed_term(
        db, vendor=vd, commission_bps=1000, role="producer_email_v1", manifest=manifest
    )
    ids["kettle_vendor_id"] = vd.id
    ids["kettle_eligible_cents"] = 700_000

    db.commit()
    return ids


def apply_day2_contradiction(
    db: Session, *, vendor_id: str, manifest: tuple[str, int, str]
) -> None:
    """Retires Kettle Corn Vendor's original "producer_email_v1" (1000bps)
    proposed term and adds a NEW, contradicting "producer_email_v2"
    proposed term at 1200bps -- simulating a later email that changes the
    claimed rate. After this runs, `find_candidate_conflicting_terms` sees
    {1500 (still proposed, contract), 1200 (newly proposed, email v2)},
    not {1500, 1000} -- so the ACTIVE policy approved from Day 1 (whose
    recorded predicates still say proposed_bps=1000) no longer matches."""
    old_term = db.execute(
        select(VendorContractTerm).where(
            VendorContractTerm.vendor_id == vendor_id,
            VendorContractTerm.status == "proposed",
            VendorContractTerm.commission_bps == 1000,
        )
    ).scalar_one()
    retire_term(db, old_term)
    get_or_create_proposed_term(
        db,
        vendor=db.get(Vendor, vendor_id),
        commission_bps=1200,
        role="producer_email_v2",
        manifest=manifest,
    )
    db.commit()


def main() -> int:
    make_engine()
    session_factory = get_session_factory()
    db = session_factory()
    try:
        ids = seed(db)
        print(f"dev_fixtures_a05: seeded/verified {len(ids)} tracked fixture ids")
        for key, value in sorted(ids.items()):
            print(f"  {key} = {value}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
