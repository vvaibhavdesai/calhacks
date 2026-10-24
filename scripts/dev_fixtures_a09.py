#!/usr/bin/env python3
"""THROWAWAY, HAND-CHECKED dev fixtures for A09 verification.

*** THIS IS NOT B01. *** These are small, clearly labeled substitutes built
by the same author who wrote app/corrections.py, in the same spirit as
scripts/dev_fixtures_a04/a05/a07/a08.py. Every expected number is worked
out by hand in scripts/verify_a09_corrections.py's comments; nothing here
reads lane B's oracle (tests/oracle/) or the generator (data/generator/).

This module only builds RAW INPUTS a human would already have imported:
vendors, two conflicting "proposed" contract terms per vendor (standing in
for a signed contract and a producer email), and matched orders/payments/
bank deposits per night (A04's fixture helpers). Every workflow step --
conflict detection, the human resolution, reuse approval, settlement,
posting, statements, payouts, challenge, preview, approval, recovery,
branch, replay -- is driven over real HTTP by the verification script.

The one deliberate direct-DB mutation is `apply_late_refund`, copied from
dev_fixtures_a04.apply_fixture5_late_refund: it lowers an order's
matched_amount to simulate a late refund A03 would have netted, so A04's
own correction path (and A09's recovery for it) can be exercised.
"""

from __future__ import annotations

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app.ingestion import import_source  # noqa: E402
from app.models_ledger import Order, Payment, Vendor, VendorContractTerm  # noqa: E402
from app.models_reconciliation import MatchGroup, MatchGroupMember  # noqa: E402
from app.storage.local_fs import get_document_storage  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

import dev_fixtures_a04 as a04f  # noqa: E402

SOURCE_SYSTEM = "a09_devfixtures"
D1 = date(2026, 10, 16)
D2 = date(2026, 10, 17)
D3 = date(2026, 10, 18)


def ingest_manifest(db: Session, run: str) -> tuple[str, int, str]:
    content = (
        b'{"name": "a09-dev-fixture-manifest", "run": "' + run.encode() + b'", '
        b'"note": "throwaway hand-checked A09 dev fixtures, NOT B01"}'
    )
    r = import_source(
        db,
        get_document_storage(),
        source_system=SOURCE_SYSTEM,
        source_native_id=f"a09-manifest-{run}",
        content_bytes=content,
        mime_type="application/json",
        actor_id="dev-fixture-script",
        actor_capability="import_source",
    )
    db.commit()
    return r.source_document_id, r.version, r.content_hash


def _ref(manifest, locator: str) -> dict:
    doc_id, version, h = manifest
    return {
        "source_document_id": doc_id,
        "source_version": version,
        "content_hash": h,
        "locator_kind": "row",
        "locator": locator,
    }


def seed_vendor(
    db: Session,
    *,
    festival_id: str,
    name: str,
    run: str,
    manifest,
    contract_bps: int | None,
    email_bps: int | None,
    nights: list[tuple[date, int, int]],
) -> Vendor:
    """nights: (business_date, gross_cents, tax_cents); eligible = gross - tax.
    All money centrally collected through a settled, fee-free deposit."""
    vendor = Vendor(festival_id=festival_id, name=name)
    db.add(vendor)
    db.flush()
    version = 0
    for bps, role in ((contract_bps, "contract"), (email_bps, "producer_email")):
        if bps is None:
            continue
        version += 1
        db.add(
            VendorContractTerm(
                vendor_id=vendor.id,
                version=version,
                commission_bps=bps,
                minimum_fee_cents=None,
                processor_fee_deductible=False,
                effective_from=D1,
                effective_to=None,
                status="proposed",
                source_refs=[_ref(manifest, f"{role}:{name}:{bps}bps")],
            )
        )
    db.flush()
    for i, (day, gross, tax) in enumerate(nights):
        tag = f"{run}-{name.replace(' ', '')}-{i}"
        order = a04f.get_or_create_order(
            db,
            vendor=vendor,
            native_id=f"a09-order-{tag}",
            business_date_=day,
            gross_cents=gross,
            tax_cents=tax,
            tips_cents=0,
            manifest=manifest,
        )
        bank = a04f.get_or_create_bank_entry(
            db,
            festival_id=festival_id,
            provider_reference=f"a09-bank-{tag}",
            amount_cents=gross,
            batch_fee_cents=None,
            business_date_=day,
            manifest=manifest,
        )
        payment = a04f.get_or_create_payment(
            db,
            vendor=vendor,
            order=order,
            native_id=f"a09-payment-{tag}",
            custody="centrally_collected",
            amount_cents=gross,
            manifest=manifest,
            bank_entry_id=bank.id,
        )
        a04f.create_matched_order_group(
            db,
            festival_id=festival_id,
            order=order,
            payment=payment,
            matched_amount_cents=gross,
            manifest=manifest,
        )
    db.commit()
    return vendor


def apply_late_refund(
    db: Session, *, vendor_id: str, business_date_: date, new_matched: int
) -> None:
    group = db.execute(
        select(MatchGroup).where(
            MatchGroup.vendor_id == vendor_id,
            MatchGroup.match_type == "order_capture",
            MatchGroup.business_date == business_date_,
        )
    ).scalar_one()
    refunded = group.matched_amount_cents - new_matched
    group.matched_amount_cents = new_matched
    db.add(group)
    db.flush()
    # The refund itself, as the processor would report it, so the books
    # (A08 sales posting) see the money going back to the customer too.
    order_member = db.execute(
        select(MatchGroupMember).where(
            MatchGroupMember.match_group_id == group.id, MatchGroupMember.member_kind == "order"
        )
    ).scalar_one()
    order = db.get(Order, order_member.member_id)
    db.add(
        Payment(
            vendor_id=vendor_id,
            order_id=order.id,
            source_system=SOURCE_SYSTEM,
            source_native_id=f"a09-late-refund-{order.id}",
            method="card",
            custody="centrally_collected",
            amount_cents=refunded,
            kind="refund",
            source_document_id=order.source_document_id,
            source_version=order.source_version,
            content_hash=order.content_hash,
            locator_kind="row",
            locator=f"refund:{order.source_native_id}",
        )
    )
    db.flush()
    db.commit()


AMENDMENT_TEXT = (
    "SIGNED CONTRACT AMENDMENT (fictional, A09 dev fixture, NOT B01)\n"
    "Vendor: {vendor}\nFestival: Golden Coast Music Festival 2026\n"
    "Section 4 (commission) is amended: the organizer commission is {pct}% of eligible "
    "sales for the whole festival, effective 2026-10-16, superseding any earlier rate "
    "stated in correspondence.\nSigned by both parties.\n"
)
