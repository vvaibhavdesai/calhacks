#!/usr/bin/env python3
"""THROWAWAY, HAND-CHECKED dev fixtures for X0.1 (generalize cases and
approvals) verification.

*** NOT B01. *** Same convention as scripts/dev_fixtures_a05.py and every
dev_fixtures_a0N.py before it: a small, hand-checkable substitute, not an
independent oracle.

Builds:
  - A fresh Vendor and, via the REAL app.policy call site, a real
    `vendor_contract_term_conflict` Case -- proving the existing A05 call
    site still tags `subject_type="vendor_run"`/`subject_id=<vendor_id>`
    on every NEW case it opens (not just backfilled old rows).
  - A fresh Supplier/PurchaseOrder/SupplierInvoice, then a deliberately
    duplicated invoice submission via the REAL app.payables call site --
    proving it still opens a real `DUPLICATE_SUPPLIER_INVOICE` case,
    unchanged, now tagged `subject_type="supplier_invoice"`/
    `subject_id=<invoice_id>`.
  - One fixture-only Case row with `subject_type="forecast_snapshot"` and
    an arbitrary, non-existent `subject_id` string -- proving the column
    is a loose reference usable before F1's `forecast_snapshot` table
    exists at all (X0.1's "Done when": "A case can reference an invoice
    and a forecast snapshot").
  - A second, deliberately LIMITED user holding `resolve_case` and
    `approve_invoice_exception` but NOT `release_supplier_payment` -- used
    by scripts/verify_x01_cases_approvals.py to prove the new /approvals
    endpoint's capability check is genuinely per-action, not all-or-
    nothing (the same user is granted for one action and rejected for
    another).

Usage (from repo root, real Postgres required, no SQLite):
    DATABASE_URL=... uv run python scripts/dev_fixtures_x01.py

Safe to re-run: every entity is looked up by a natural key before being
created, same convention as dev_fixtures_a03/a04/a05. The run id suffix on
the festival id keeps each invocation's duplicate-invoice scenario
independent (duplicate detection is stateful -- a second run against a
fixed festival id would be asserting against the first run's leftovers,
same reasoning as dev_fixtures_a07.py's RUN suffix).
"""

from __future__ import annotations

import os
import sys
import time
from datetime import UTC, date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

from app.auth import hash_password  # noqa: E402
from app.db import get_session_factory, make_engine  # noqa: E402
from app.models import (  # noqa: E402
    CAPABILITY_APPROVE_INVOICE_EXCEPTION,
    CAPABILITY_RESOLVE_CASE,
    Capability,
    User,
    UserCapability,
)
from app.models_ledger import Case, Vendor, VendorContractTerm  # noqa: E402
from app.payables import (  # noqa: E402
    DuplicateInvoiceError,
    create_purchase_order,
    get_or_create_supplier,
    record_invoice,
)
from app.policy import detect_contract_term_conflicts  # noqa: E402
from app.uow import UnitOfWork  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

# Stable across runs (idempotent fixtures).
FESTIVAL_ID = "gcmf-2026-x01dev"
DAY1 = date(2026, 10, 16)

# Fresh per process invocation (the duplicate-invoice scenario is
# stateful; a fixed invoice_number would only be "duplicate" on run 1).
RUN = os.environ.get("X01_DEV_RUN") or str(int(time.time() * 1000))

LIMITED_USER_EMAIL = f"x01-limited-{RUN}@example.com"
LIMITED_USER_PASSWORD = "X01LimitedDevPassw0rd!"

FORECAST_SNAPSHOT_SUBJECT_ID = f"fcst-not-yet-real-{RUN}"


def _next_term_version(db: Session, vendor_id: str) -> int:
    max_version = db.execute(
        select(VendorContractTerm.version)
        .where(VendorContractTerm.vendor_id == vendor_id)
        .order_by(VendorContractTerm.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    return (max_version or 0) + 1


def _get_or_create_vendor(db: Session, *, name: str) -> Vendor:
    existing = db.execute(
        select(Vendor).where(Vendor.festival_id == FESTIVAL_ID, Vendor.name == name)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    vendor = Vendor(festival_id=FESTIVAL_ID, name=name)
    db.add(vendor)
    db.flush()
    return vendor


def _ensure_proposed_term(
    db: Session, *, vendor: Vendor, commission_bps: int, role: str
) -> VendorContractTerm:
    existing = db.execute(
        select(VendorContractTerm).where(
            VendorContractTerm.vendor_id == vendor.id,
            VendorContractTerm.status == "proposed",
            VendorContractTerm.commission_bps == commission_bps,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    term = VendorContractTerm(
        vendor_id=vendor.id,
        version=_next_term_version(db, vendor.id),
        commission_bps=commission_bps,
        minimum_fee_cents=None,
        processor_fee_deductible=False,
        effective_from=DAY1,
        effective_to=None,
        status="proposed",
        source_refs=[
            {
                "source_document_id": "x01-dev-fixture-doc",
                "source_version": 1,
                "content_hash": "x01-dev-fixture-hash",
                "locator_kind": "row",
                "locator": f"{role}:{vendor.name}:{commission_bps}bps",
            }
        ],
    )
    db.add(term)
    db.flush()
    return term


def _ensure_limited_user(db: Session) -> str:
    """Holds resolve_case (so it can read cases/approvals) and
    approve_invoice_exception, but deliberately NOT
    release_supplier_payment -- the verify script exercises BOTH an
    allowed and a rejected action against this SAME user, proving the
    /approvals capability check is genuinely per-action."""
    existing = db.execute(select(User).where(User.email == LIMITED_USER_EMAIL)).scalar_one_or_none()
    if existing is not None:
        return existing.id
    user = User(email=LIMITED_USER_EMAIL, hashed_password=hash_password(LIMITED_USER_PASSWORD))
    db.add(user)
    db.flush()
    for code in (CAPABILITY_RESOLVE_CASE, CAPABILITY_APPROVE_INVOICE_EXCEPTION):
        cap = db.execute(select(Capability).where(Capability.code == code)).scalar_one()
        db.add(UserCapability(user_id=user.id, capability_id=cap.id, granted_by=None))
    db.flush()
    return user.id


def seed(db: Session, *, admin_id: str) -> dict:
    ids: dict = {}

    # --- 1. vendor_run: a real NEW contract-conflict case via app.policy --
    vendor = _get_or_create_vendor(db, name=f"X01 Taco Vendor {RUN}")
    _ensure_proposed_term(db, vendor=vendor, commission_bps=1500, role="contract")
    _ensure_proposed_term(db, vendor=vendor, commission_bps=1000, role="producer_email")
    uow = UnitOfWork(db)
    case = detect_contract_term_conflicts(
        db,
        uow,
        vendor=vendor,
        business_date_=DAY1,
        eligible_sales_cents_today=1_000_000,
        run_id=None,
        actor_id=admin_id,
        actor_capability=CAPABILITY_RESOLVE_CASE,
    )
    db.commit()
    ids["vendor_id"] = vendor.id
    ids["vendor_run_case_id"] = case.id if case is not None else None

    # --- 2. supplier_invoice: a real NEW duplicate-invoice case ------------
    supplier, _ = get_or_create_supplier(
        db, festival_id=FESTIVAL_ID, name=f"X01 Ice Supplier {RUN}", category="ice"
    )
    db.commit()
    po, _ = create_purchase_order(
        db,
        supplier_id=supplier.id,
        po_number=f"PO-X01-{RUN}",
        po_date=DAY1,
        amount_cents=500_000,
        description="X01 dev fixture PO",
        receipt_evidence_refs=[],
        source_refs=[],
        actor_id=admin_id,
        actor_capability=CAPABILITY_RESOLVE_CASE,
    )
    db.commit()
    invoice, _created, _cases = record_invoice(
        db,
        supplier_id=supplier.id,
        po_id=po.id,
        invoice_number=f"INV-X01-{RUN}",
        invoice_date=DAY1,
        due_date=None,
        net_cents=100_000,
        tax_cents=0,
        gross_cents=100_000,
        received_at=datetime.now(UTC),
        source_refs=[{"locator": "x01-dev-fixture-first-submission"}],
        actor_id=admin_id,
        actor_capability=CAPABILITY_RESOLVE_CASE,
    )
    db.commit()
    ids["supplier_invoice_id"] = invoice.id

    duplicate_case_id = None
    try:
        record_invoice(
            db,
            supplier_id=supplier.id,
            po_id=po.id,
            invoice_number=f"INV-X01-{RUN}",  # same number
            invoice_date=DAY1,
            due_date=None,
            net_cents=100_000,
            tax_cents=0,
            gross_cents=100_000,
            received_at=datetime.now(UTC),
            source_refs=[{"locator": "x01-dev-fixture-DIFFERENT-resubmission"}],
            actor_id=admin_id,
            actor_capability=CAPABILITY_RESOLVE_CASE,
        )
        raise AssertionError("expected DuplicateInvoiceError, got none")
    except DuplicateInvoiceError as exc:
        # record_invoice already committed the case + audit event before
        # raising -- see app/payables.py's docstring.
        duplicate_case_id = exc.details["case_id"]
    ids["supplier_invoice_case_id"] = duplicate_case_id

    # --- 3. forecast_snapshot: fixture-only, loose string subject_id -------
    existing_forecast_case = db.execute(
        select(Case).where(
            Case.subject_type == "forecast_snapshot",
            Case.subject_id == FORECAST_SNAPSHOT_SUBJECT_ID,
        )
    ).scalar_one_or_none()
    if existing_forecast_case is None:
        forecast_case = Case(
            reason_code="x01_dev_fixture_shortfall",
            evidence_refs=[],
            required_capability="acknowledge_forecast",
            status="OPEN",
            subject_type="forecast_snapshot",
            subject_id=FORECAST_SNAPSHOT_SUBJECT_ID,
        )
        db.add(forecast_case)
        db.commit()
        existing_forecast_case = forecast_case
    ids["forecast_snapshot_case_id"] = existing_forecast_case.id
    ids["forecast_snapshot_subject_id"] = FORECAST_SNAPSHOT_SUBJECT_ID

    # --- 4. limited user for the dynamic-capability-check proof ------------
    ids["limited_user_id"] = _ensure_limited_user(db)
    db.commit()
    ids["limited_user_email"] = LIMITED_USER_EMAIL
    ids["limited_user_password"] = LIMITED_USER_PASSWORD
    ids["run"] = RUN
    return ids


if __name__ == "__main__":
    make_engine()
    session_factory = get_session_factory()
    database = session_factory()
    try:
        # Needs an actor id for the real call sites above -- use the
        # already-seeded admin (scripts/seed_admin.py must have run first).
        admin = database.execute(
            select(User).where(User.email == os.environ.get("ADMIN_EMAIL", "admin@example.com"))
        ).scalar_one()
        result = seed(database, admin_id=admin.id)
    finally:
        database.close()
    print(f"seeded/confirmed X0.1 dev fixtures: {result}")
