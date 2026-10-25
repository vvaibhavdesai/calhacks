#!/usr/bin/env python3
"""THROWAWAY, HAND-CHECKED dev fixtures for X0.3 (contract obligations and
links) verification.

*** NOT B01. *** Same standing disclaimer as every other
scripts/dev_fixtures_*.py in this repo.

Builds, all through REAL production code paths (app.ingestion.import_source
for the contract document, app.payables for suppliers/POs/invoices/
payments, app.contract_obligations for the new X0.3 tables themselves --
nothing here constructs an ORM row by hand except where a scenario is
specifically about rejecting a bad input):

  1. "X0.3 Flagship Supplier" -- the EXACT TICKET_SPEC.md scenario: a
     $20,000 contract split into a $5,000 deposit obligation (invoiced then
     paid in full) and a $15,000 final obligation (invoiced, not yet paid).
     Proves: aggregate commitment across the contract is $20,000, aggregate
     remaining is $15,000, and it is never $40,000.
  2. "X0.3 Property Supplier" -- one $10,000 milestone obligation. A PO is
     linked (subject_type="po"), then THREE invoices totaling $12,000 are
     linked (subject_type="invoice", deliberately exceeding the obligation
     amount, to prove over-INVOICING alone never changes `amount_cents` and
     never trips overpayment), then three payments totaling $11,000 are
     linked (subject_type="payment") -- the second payment pushes
     paid_cents past amount_cents, which SHOULD open exactly one
     overpayment Case (the third, small payment link afterward must NOT
     open a second one).
  3. "X0.3 Vendor Milestone" -- counterparty_type="vendor" (not just
     "supplier"), proposed -> approved -> committed -> cancelled, proving
     the vocabulary and the "cancelled is reachable before invoiced" state-
     machine judgment call both work for the vendor side too.
  4. An illegal-transition fixture (proposed -> paid directly) -- asserted
     directly in this script (not just by the verify script), so a broken
     fixture fails loudly at seed time.

Usage (from repo root, real Postgres required, no SQLite):
    DATABASE_URL=... uv run python scripts/dev_fixtures_x03.py

Safe to re-run: every entity is looked up by a natural key (supplier name,
invoice number, obligation absence-check via (contract_id, kind)) before
being created, same convention as dev_fixtures_a03/a04/a05/x01/x02.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app.contract_obligations import (  # noqa: E402
    ContractObligationConflictError,
    link_obligation,
    propose_obligation,
    transition_obligation,
)
from app.db import get_session_factory, make_engine  # noqa: E402
from app.ingestion import import_source  # noqa: E402
from app.models import User  # noqa: E402
from app.models_finance import ContractObligation, ObligationLink  # noqa: E402
from app.models_ledger import Vendor  # noqa: E402
from app.payables import (  # noqa: E402
    approve_variance,
    create_purchase_order,
    get_or_create_supplier,
    pay_invoice,
    record_invoice,
)
from app.payout_dispatch import fund_cash_pool  # noqa: E402
from app.storage.local_fs import get_document_storage  # noqa: E402
from domain.contract_obligations import IllegalTransitionError  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

FESTIVAL_ID = "gcmf-2026-x03dev"
SOURCE_SYSTEM = "x03_devfixtures"


def _ingest_contract_document(db: Session, native_id: str, label: str) -> tuple[str, int, str]:
    storage = get_document_storage()
    content = (
        f'{{"name": "{label}", '
        '"note": "throwaway hand-checked X0.3 dev fixture, not a real contract"}}'
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
    return result.source_document_id, result.version, result.content_hash


def get_or_create_vendor(db: Session, *, name: str) -> Vendor:
    existing = db.execute(
        select(Vendor).where(Vendor.festival_id == FESTIVAL_ID, Vendor.name == name)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    vendor = Vendor(festival_id=FESTIVAL_ID, name=name)
    db.add(vendor)
    db.flush()
    db.commit()
    return vendor


def _get_obligation_by_kind(
    db: Session, *, contract_id: str, kind: str
) -> ContractObligation | None:
    return db.execute(
        select(ContractObligation).where(
            ContractObligation.contract_id == contract_id, ContractObligation.kind == kind
        )
    ).scalar_one_or_none()


def _source_ref(doc_id: str, version: int, content_hash: str, locator: str, excerpt: str) -> dict:
    return {
        "source_document_id": doc_id,
        "source_version": version,
        "content_hash": content_hash,
        "locator_kind": "clause",
        "locator": locator,
        "quoted_excerpt": excerpt,
    }


def _link_if_absent(
    db: Session,
    *,
    obligation_id: str,
    subject_type: str,
    subject_id: str,
    amount_cents: int,
    admin_id: str,
):
    existing = db.execute(
        select(ObligationLink).where(
            ObligationLink.obligation_id == obligation_id,
            ObligationLink.subject_type == subject_type,
            ObligationLink.subject_id == subject_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None
    _link, _ob, totals, case = link_obligation(
        db,
        obligation_id=obligation_id,
        subject_type=subject_type,
        subject_id=subject_id,
        amount_cents=amount_cents,
        actor_id=admin_id,
        actor_capability="manage_payables",
    )
    db.commit()
    return totals, case


def _transition_if_current(
    db: Session, *, obligation_id: str, from_status: str, to_status: str, admin_id: str
) -> None:
    ob = db.get(ContractObligation, obligation_id)
    if ob.status == from_status:
        transition_obligation(
            db,
            obligation_id=obligation_id,
            new_status=to_status,
            actor_id=admin_id,
            actor_capability="finance_lead",
        )
        db.commit()


def _build_flagship(db: Session, admin_id: str, ids: dict) -> None:
    doc_id, doc_version, doc_hash = _ingest_contract_document(
        db, "x03-dev-flagship-contract-v1", "X0.3 Flagship Supplier master agreement"
    )
    ids["flagship_contract_id"] = doc_id

    supplier, _ = get_or_create_supplier(
        db, festival_id=FESTIVAL_ID, name="X0.3 Flagship Supplier", category="staging"
    )
    db.commit()
    ids["flagship_supplier_id"] = supplier.id

    deposit_ob = _get_obligation_by_kind(db, contract_id=doc_id, kind="deposit")
    if deposit_ob is None:
        deposit_ob = propose_obligation(
            db,
            festival_id=FESTIVAL_ID,
            contract_id=doc_id,
            counterparty_type="supplier",
            counterparty_id=supplier.id,
            kind="deposit",
            amount_cents=500_000,
            due_date=date(2026, 11, 1),
            condition="Due on contract signature.",
            actor_id=admin_id,
            actor_capability="finance_lead",
            **_source_ref(
                doc_id, doc_version, doc_hash, "clause-2.1", "A $5,000 deposit is due on signature."
            ),
        )
        db.commit()
    ids["flagship_deposit_obligation_id"] = deposit_ob.id

    final_ob = _get_obligation_by_kind(db, contract_id=doc_id, kind="final")
    if final_ob is None:
        final_ob = propose_obligation(
            db,
            festival_id=FESTIVAL_ID,
            contract_id=doc_id,
            counterparty_type="supplier",
            counterparty_id=supplier.id,
            kind="final",
            amount_cents=1_500_000,
            due_date=date(2026, 12, 1),
            condition="Due on final invoice, net 30.",
            actor_id=admin_id,
            actor_capability="finance_lead",
            **_source_ref(
                doc_id,
                doc_version,
                doc_hash,
                "clause-2.2",
                "The remaining $15,000 is due as a final invoice.",
            ),
        )
        db.commit()
    ids["flagship_final_obligation_id"] = final_ob.id

    _transition_if_current(
        db,
        obligation_id=deposit_ob.id,
        from_status="proposed",
        to_status="approved",
        admin_id=admin_id,
    )
    _transition_if_current(
        db,
        obligation_id=deposit_ob.id,
        from_status="approved",
        to_status="committed",
        admin_id=admin_id,
    )

    deposit_invoice, _created, _cases = record_invoice(
        db,
        supplier_id=supplier.id,
        po_id=None,
        invoice_number="X03-DEV-DEPOSIT-1",
        invoice_date=date(2026, 11, 1),
        due_date=date(2026, 11, 1),
        net_cents=500_000,
        tax_cents=0,
        gross_cents=500_000,
        received_at=datetime(2026, 11, 1, tzinfo=UTC),
        source_refs=[{"locator_kind": "row", "locator": "x03-dev-deposit-invoice"}],
        actor_id=admin_id,
        actor_capability="manage_payables",
    )
    db.commit()
    ids["flagship_deposit_invoice_id"] = deposit_invoice.id

    _transition_if_current(
        db,
        obligation_id=deposit_ob.id,
        from_status="committed",
        to_status="invoiced",
        admin_id=admin_id,
    )

    deposit_payment, _paid_created = pay_invoice(
        db,
        invoice_id=deposit_invoice.id,
        amount_cents=500_000,
        idempotency_key=f"x03-dev-deposit-payment:{deposit_invoice.id}",
        business_date=date(2026, 11, 2),
        actor_id=admin_id,
        actor_capability="manage_payables",
    )
    db.commit()
    ids["flagship_deposit_payment_id"] = deposit_payment.id

    _link_if_absent(
        db,
        obligation_id=deposit_ob.id,
        subject_type="payment",
        subject_id=deposit_payment.id,
        amount_cents=500_000,
        admin_id=admin_id,
    )

    _transition_if_current(
        db, obligation_id=deposit_ob.id, from_status="invoiced", to_status="paid", admin_id=admin_id
    )

    _transition_if_current(
        db,
        obligation_id=final_ob.id,
        from_status="proposed",
        to_status="approved",
        admin_id=admin_id,
    )
    _transition_if_current(
        db,
        obligation_id=final_ob.id,
        from_status="approved",
        to_status="committed",
        admin_id=admin_id,
    )

    final_invoice, _created2, _cases2 = record_invoice(
        db,
        supplier_id=supplier.id,
        po_id=None,
        invoice_number="X03-DEV-FINAL-1",
        invoice_date=date(2026, 12, 1),
        due_date=date(2026, 12, 1),
        net_cents=1_500_000,
        tax_cents=0,
        gross_cents=1_500_000,
        received_at=None,
        source_refs=[{"locator_kind": "row", "locator": "x03-dev-final-invoice"}],
        actor_id=admin_id,
        actor_capability="manage_payables",
    )
    db.commit()
    ids["flagship_final_invoice_id"] = final_invoice.id

    _link_if_absent(
        db,
        obligation_id=final_ob.id,
        subject_type="invoice",
        subject_id=final_invoice.id,
        amount_cents=1_500_000,
        admin_id=admin_id,
    )

    _transition_if_current(
        db,
        obligation_id=final_ob.id,
        from_status="committed",
        to_status="invoiced",
        admin_id=admin_id,
    )


def _build_property(db: Session, admin_id: str, ids: dict) -> None:
    doc2_id, doc2_version, doc2_hash = _ingest_contract_document(
        db, "x03-dev-property-contract-v1", "X0.3 Property Supplier agreement"
    )
    ids["property_contract_id"] = doc2_id

    supplier2, _ = get_or_create_supplier(
        db, festival_id=FESTIVAL_ID, name="X0.3 Property Supplier", category="generators"
    )
    db.commit()
    ids["property_supplier_id"] = supplier2.id

    milestone_ob = _get_obligation_by_kind(db, contract_id=doc2_id, kind="milestone")
    if milestone_ob is None:
        milestone_ob = propose_obligation(
            db,
            festival_id=FESTIVAL_ID,
            contract_id=doc2_id,
            counterparty_type="supplier",
            counterparty_id=supplier2.id,
            kind="milestone",
            amount_cents=1_000_000,
            due_date=date(2026, 11, 15),
            condition="Due on generator delivery milestone.",
            actor_id=admin_id,
            actor_capability="finance_lead",
            **_source_ref(
                doc2_id, doc2_version, doc2_hash, "clause-3.1", "A $10,000 milestone on delivery."
            ),
        )
        db.commit()
    ids["property_milestone_obligation_id"] = milestone_ob.id

    po, _po_created = create_purchase_order(
        db,
        supplier_id=supplier2.id,
        po_number="X03-DEV-PROP-PO-1",
        po_date=date(2026, 11, 1),
        amount_cents=1_000_000,
        description="Generators milestone PO",
        receipt_evidence_refs=[],
        source_refs=[{"locator_kind": "row", "locator": "x03-dev-property-po"}],
        actor_id=admin_id,
        actor_capability="manage_payables",
    )
    db.commit()
    ids["property_po_id"] = po.id

    _link_if_absent(
        db,
        obligation_id=milestone_ob.id,
        subject_type="po",
        subject_id=po.id,
        amount_cents=1_000_000,
        admin_id=admin_id,
    )

    invoice_amounts = [400_000, 700_000, 100_000]  # $4k+$7k+$1k=$12k, deliberately over-invoiced
    property_invoice_ids = []
    for idx, amt in enumerate(invoice_amounts, start=1):
        inv, _c, _cs = record_invoice(
            db,
            supplier_id=supplier2.id,
            po_id=po.id,
            invoice_number=f"X03-DEV-PROP-INV-{idx}",
            invoice_date=date(2026, 11, 10 + idx),
            due_date=date(2026, 12, 10 + idx),
            net_cents=amt,
            tax_cents=0,
            gross_cents=amt,
            received_at=None,
            source_refs=[{"locator_kind": "row", "locator": f"x03-dev-property-invoice-{idx}"}],
            actor_id=admin_id,
            actor_capability="manage_payables",
        )
        db.commit()
        property_invoice_ids.append(inv.id)
        _link_if_absent(
            db,
            obligation_id=milestone_ob.id,
            subject_type="invoice",
            subject_id=inv.id,
            amount_cents=amt,
            admin_id=admin_id,
        )
    ids["property_invoice_ids"] = property_invoice_ids

    # Three real SupplierPayment rows, each linked at its OWN real, full
    # amount (no invented portion exceeding what was actually paid -- an
    # obligation_link's amount_cents represents a portion of a REAL
    # payment, never more than that payment's own amount). Invoices 2 and 3
    # individually push the PO's running invoiced total past its $10,000
    # amount (app/payables.py's PO-excess attribution, the same mechanism
    # verify_a08_finance.py exercises), so each needs a finance-lead
    # variance approval before it is payable at all -- a completely
    # separate, invoice/PO-level concern from X0.3's own obligation-level
    # "overpaid" condition below, which is keyed off payment/credit links
    # only.
    payment1, _ = pay_invoice(
        db,
        invoice_id=property_invoice_ids[0],
        amount_cents=400_000,
        idempotency_key=f"x03-dev-property-payment-1:{property_invoice_ids[0]}",
        business_date=date(2026, 11, 20),
        actor_id=admin_id,
        actor_capability="manage_payables",
    )
    db.commit()
    ids["property_payment1_id"] = payment1.id
    # $400,000 paid and linked, amount_cents=1,000,000: remaining=$600,000,
    # not yet overpaid.
    result1 = _link_if_absent(
        db,
        obligation_id=milestone_ob.id,
        subject_type="payment",
        subject_id=payment1.id,
        amount_cents=400_000,
        admin_id=admin_id,
    )

    approve_variance(
        db, invoice_id=property_invoice_ids[1], actor_id=admin_id, note="X0.3 dev fixture"
    )
    db.commit()
    payment2, _ = pay_invoice(
        db,
        invoice_id=property_invoice_ids[1],
        amount_cents=700_000,
        idempotency_key=f"x03-dev-property-payment-2:{property_invoice_ids[1]}",
        business_date=date(2026, 11, 21),
        actor_id=admin_id,
        actor_capability="manage_payables",
    )
    db.commit()
    ids["property_payment2_id"] = payment2.id
    # Cumulative linked payments now $400,000 + $700,000 = $1,100,000 >
    # amount_cents ($1,000,000): THIS link is the one that tips it over
    # into overpaid -- expect a real new Case here.
    result2 = _link_if_absent(
        db,
        obligation_id=milestone_ob.id,
        subject_type="payment",
        subject_id=payment2.id,
        amount_cents=700_000,
        admin_id=admin_id,
    )

    approve_variance(
        db, invoice_id=property_invoice_ids[2], actor_id=admin_id, note="X0.3 dev fixture"
    )
    db.commit()
    payment3, _ = pay_invoice(
        db,
        invoice_id=property_invoice_ids[2],
        amount_cents=50_000,
        idempotency_key=f"x03-dev-property-payment-3:{property_invoice_ids[2]}",
        business_date=date(2026, 11, 22),
        actor_id=admin_id,
        actor_capability="manage_payables",
    )
    db.commit()
    ids["property_payment3_id"] = payment3.id
    # Still overpaid afterward ($1,150,000 paid), but the case already
    # opened by the second link above is still OPEN -- this link must NOT
    # open a second, duplicate overpayment case.
    result3 = _link_if_absent(
        db,
        obligation_id=milestone_ob.id,
        subject_type="payment",
        subject_id=payment3.id,
        amount_cents=50_000,
        admin_id=admin_id,
    )

    ids["property_after_payment1_link"] = (
        None
        if result1 is None
        else {
            "is_overpaid": result1[0].is_overpaid,
            "case_id": None if result1[1] is None else result1[1].id,
        }
    )
    ids["property_after_payment2_link"] = (
        None
        if result2 is None
        else {
            "is_overpaid": result2[0].is_overpaid,
            "case_id": None if result2[1] is None else result2[1].id,
        }
    )
    ids["property_after_payment3_link"] = (
        None
        if result3 is None
        else {
            "is_overpaid": result3[0].is_overpaid,
            "case_id": None if result3[1] is None else result3[1].id,
        }
    )


def _build_vendor_cancel(db: Session, admin_id: str, ids: dict) -> None:
    doc3_id, doc3_version, doc3_hash = _ingest_contract_document(
        db, "x03-dev-vendor-contract-v1", "X0.3 Vendor Milestone agreement"
    )
    ids["vendor_contract_id"] = doc3_id

    vendor = get_or_create_vendor(db, name="X0.3 Vendor Milestone Vendor")
    ids["vendor_id"] = vendor.id

    vendor_ob = _get_obligation_by_kind(db, contract_id=doc3_id, kind="milestone")
    if vendor_ob is None:
        vendor_ob = propose_obligation(
            db,
            festival_id=FESTIVAL_ID,
            contract_id=doc3_id,
            counterparty_type="vendor",
            counterparty_id=vendor.id,
            kind="milestone",
            amount_cents=200_000,
            due_date=None,
            condition="Mid-festival bonus milestone, cancelled before invoicing.",
            actor_id=admin_id,
            actor_capability="finance_lead",
            **_source_ref(
                doc3_id,
                doc3_version,
                doc3_hash,
                "clause-9.1",
                "A $2,000 mid-festival bonus milestone.",
            ),
        )
        db.commit()
    ids["vendor_obligation_id"] = vendor_ob.id

    _transition_if_current(
        db,
        obligation_id=vendor_ob.id,
        from_status="proposed",
        to_status="approved",
        admin_id=admin_id,
    )
    _transition_if_current(
        db,
        obligation_id=vendor_ob.id,
        from_status="approved",
        to_status="committed",
        admin_id=admin_id,
    )
    _transition_if_current(
        db,
        obligation_id=vendor_ob.id,
        from_status="committed",
        to_status="cancelled",
        admin_id=admin_id,
    )


def _build_illegal_transition_fixture(db: Session, admin_id: str, ids: dict) -> None:
    fresh_doc_id, fresh_ver, fresh_hash = _ingest_contract_document(
        db, "x03-dev-illegal-transition-contract-v1", "X0.3 Illegal Transition Fixture"
    )
    ids["illegal_transition_contract_id"] = fresh_doc_id

    illegal_ob = _get_obligation_by_kind(db, contract_id=fresh_doc_id, kind="deposit")
    if illegal_ob is None:
        supplier2_id = ids["property_supplier_id"]
        illegal_ob = propose_obligation(
            db,
            festival_id=FESTIVAL_ID,
            contract_id=fresh_doc_id,
            counterparty_type="supplier",
            counterparty_id=supplier2_id,
            kind="deposit",
            amount_cents=10_000,
            due_date=None,
            condition="Fixture only, for illegal-transition checks.",
            actor_id=admin_id,
            actor_capability="finance_lead",
            **_source_ref(fresh_doc_id, fresh_ver, fresh_hash, "clause-x", "fixture-only clause"),
        )
        db.commit()
    ids["illegal_transition_obligation_id"] = illegal_ob.id

    raised_skip = False
    try:
        transition_obligation(
            db,
            obligation_id=illegal_ob.id,
            new_status="paid",
            actor_id=admin_id,
            actor_capability="finance_lead",
        )
    except (IllegalTransitionError, ContractObligationConflictError):
        raised_skip = True
        db.rollback()
    assert raised_skip, "expected proposed -> paid to be rejected"
    ids["illegal_skip_rejected"] = raised_skip


def _ensure_cash_pool_funded(db: Session, *, admin_id: str) -> None:
    """pay_invoice (app.payables) draws from the same row-locked CashPool
    A07 vendor releases do -- fund it generously once so this fixture's
    several real SupplierPayment rows (deposit + three property-supplier
    payments) never fail on `insufficient_available_cash`. Idempotent in
    effect (re-running just funds it again, harmlessly over-funded) --
    acceptable for a throwaway dev fixture, same as every other
    dev_fixtures_*.py script's "safe to re-run" discipline applying to the
    SCENARIOS, not to every incidental side table."""
    fund_cash_pool(
        db,
        festival_id=FESTIVAL_ID,
        additional_cents=10_000_000,
        actor_id=admin_id,
        actor_capability="finance_lead",
    )
    db.commit()


def seed(db: Session, *, admin_id: str) -> dict:
    ids: dict = {"festival_id": FESTIVAL_ID}
    _ensure_cash_pool_funded(db, admin_id=admin_id)
    _build_flagship(db, admin_id, ids)
    _build_property(db, admin_id, ids)
    _build_vendor_cancel(db, admin_id, ids)
    _build_illegal_transition_fixture(db, admin_id, ids)
    return ids


if __name__ == "__main__":
    make_engine()
    session_factory = get_session_factory()
    db = session_factory()
    try:
        admin = db.execute(select(User).where(User.email == "admin@example.com")).scalar_one()
        result = seed(db, admin_id=admin.id)
        import json

        print(json.dumps(result, indent=2, default=str))
    finally:
        db.close()
