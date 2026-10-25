"""Reads for the canonical input records.

These rows could all be written and never read back: orders and payments
arrive through ingestion, bank entries through the scan, cash counts through
the cash office, supplier payments through invoice payment -- and nothing
served them, so no screen could show what the system had actually taken in.

Every route here is a read. Paging follows the same offset-cursor convention
as `GET /vendors` and `GET /journal`: the cursor is an opaque decimal offset
over a stable order and `next_cursor` is null on the last page.

Each record carries `source_refs`, the provenance the evidence drawer opens:
which document, which version of it, its content hash and where in it this row
came from. Those columns are on the rows already; this just shapes them.
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.db import get_db
from app.models import User
from app.models_finance import SupplierPayment
from app.models_ledger import Order, Payment
from app.models_reconciliation import BankEntry, CashCount

router = APIRouter(prefix="/api/v1", tags=["records"])


class SourceRefOut(BaseModel):
    source_document_id: str
    source_version: int | None = None
    content_hash: str | None = None
    locator_kind: str | None = None
    locator: str | None = None


def _source_refs(row: object) -> list[SourceRefOut]:
    """The row's provenance, as a list.

    A list of zero or one: these records each come from one place in one
    document, but every reader of a record expects the plural `source_refs`
    shape, and a row whose document is unknown returns [] rather than a
    half-filled ref.
    """
    doc_id = getattr(row, "source_document_id", None)
    if not doc_id:
        return []
    return [
        SourceRefOut(
            source_document_id=doc_id,
            source_version=getattr(row, "source_version", None),
            content_hash=getattr(row, "content_hash", None),
            locator_kind=getattr(row, "locator_kind", None),
            locator=getattr(row, "locator", None),
        )
    ]


def _offset(cursor: str | None) -> int:
    return int(cursor) if cursor and cursor.isdigit() else 0


def _next_cursor(rows: list, offset: int, limit: int) -> str | None:
    return str(offset + limit) if len(rows) > limit else None


# ---------------------------------------------------------------------------
# Orders and payments
# ---------------------------------------------------------------------------


class OrderOut(BaseModel):
    id: str
    vendor_id: str
    business_date: date
    ordered_at: datetime
    source_system: str
    source_native_id: str
    gross_cents: int
    tax_cents: int
    tips_cents: int
    # Gross less the amounts held for someone else. This is the quantity a fee
    # is computed on, so it is stated here rather than re-derived per reader.
    eligible_net_cents: int
    currency: str
    status: str
    source_refs: list[SourceRefOut]


class OrderListOut(BaseModel):
    items: list[OrderOut]
    next_cursor: str | None = None


@router.get("/orders", response_model=OrderListOut)
def list_orders(
    vendor_id: str | None = None,
    business_date: date | None = None,
    cursor: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> OrderListOut:
    offset = _offset(cursor)
    q = select(Order)
    if vendor_id is not None:
        q = q.where(Order.vendor_id == vendor_id)
    if business_date is not None:
        q = q.where(Order.business_date == business_date)
    rows = (
        db.execute(
            q.order_by(Order.ordered_at.desc(), Order.id.asc()).offset(offset).limit(limit + 1)
        )
        .scalars()
        .all()
    )
    page = list(rows[:limit])
    return OrderListOut(
        items=[
            OrderOut(
                id=o.id,
                vendor_id=o.vendor_id,
                business_date=o.business_date,
                ordered_at=o.ordered_at,
                source_system=o.source_system,
                source_native_id=o.source_native_id,
                gross_cents=o.gross_cents,
                tax_cents=o.tax_cents,
                tips_cents=o.tips_cents,
                eligible_net_cents=o.gross_cents - o.tax_cents - o.tips_cents,
                currency=o.currency,
                status=o.status,
                source_refs=_source_refs(o),
            )
            for o in page
        ],
        next_cursor=_next_cursor(rows, offset, limit),
    )


class PaymentOut(BaseModel):
    id: str
    vendor_id: str
    order_id: str | None = None
    # Taken from the order this payment settles; a payment row carries no
    # business date of its own, and one is not invented when there is no order.
    business_date: date | None = None
    occurred_at: datetime
    method: str
    custody: str
    kind: str
    amount_cents: int
    currency: str
    status: str
    provider_reference: str | None = None
    bank_entry_id: str | None = None
    source_refs: list[SourceRefOut]


class PaymentListOut(BaseModel):
    items: list[PaymentOut]
    next_cursor: str | None = None


@router.get("/payments", response_model=PaymentListOut)
def list_payments(
    vendor_id: str | None = None,
    order_id: str | None = None,
    kind: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PaymentListOut:
    offset = _offset(cursor)
    q = select(Payment)
    if vendor_id is not None:
        q = q.where(Payment.vendor_id == vendor_id)
    if order_id is not None:
        q = q.where(Payment.order_id == order_id)
    if kind is not None:
        q = q.where(Payment.kind == kind)
    rows = (
        db.execute(
            q.order_by(Payment.created_at.desc(), Payment.id.asc()).offset(offset).limit(limit + 1)
        )
        .scalars()
        .all()
    )
    page = list(rows[:limit])
    # One query for the whole page's orders rather than one per payment.
    order_ids = [p.order_id for p in page if p.order_id]
    dates: dict[str, date] = {}
    if order_ids:
        dates = {
            o.id: o.business_date
            for o in db.execute(select(Order).where(Order.id.in_(order_ids))).scalars()
        }
    return PaymentListOut(
        items=[
            PaymentOut(
                id=p.id,
                vendor_id=p.vendor_id,
                order_id=p.order_id,
                business_date=dates.get(p.order_id) if p.order_id else None,
                occurred_at=p.created_at,
                method=p.method,
                custody=p.custody,
                kind=p.kind,
                amount_cents=p.amount_cents,
                currency=p.currency,
                status=p.status,
                provider_reference=p.provider_reference,
                bank_entry_id=p.bank_entry_id,
                source_refs=_source_refs(p),
            )
            for p in page
        ],
        next_cursor=_next_cursor(rows, offset, limit),
    )


# ---------------------------------------------------------------------------
# Bank entries and cash counts
# ---------------------------------------------------------------------------


class BankEntryOut(BaseModel):
    id: str
    festival_id: str
    account_id: str
    direction: str
    amount_cents: int
    # Present only on a grouped processor deposit, which is the one case where
    # the gross and the fee are stated separately from the net.
    batch_gross_cents: int | None = None
    batch_fee_cents: int | None = None
    business_date: date
    occurred_at: datetime
    settled_at: datetime | None = None
    # The bank's own settlement state, under the name every reader uses.
    status: str
    provider_reference: str | None = None
    # The line as it reads in the statement, when one was quoted.
    description: str | None = None
    source_refs: list[SourceRefOut]


class BankEntryListOut(BaseModel):
    items: list[BankEntryOut]
    next_cursor: str | None = None


@router.get("/bank-entries", response_model=BankEntryListOut)
def list_bank_entries(
    festival_id: str | None = None,
    direction: str | None = None,
    business_date: date | None = None,
    cursor: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BankEntryListOut:
    offset = _offset(cursor)
    q = select(BankEntry)
    if festival_id is not None:
        q = q.where(BankEntry.festival_id == festival_id)
    if direction is not None:
        q = q.where(BankEntry.direction == direction)
    if business_date is not None:
        q = q.where(BankEntry.business_date == business_date)
    rows = (
        db.execute(
            q.order_by(BankEntry.occurred_at.desc(), BankEntry.id.asc())
            .offset(offset)
            .limit(limit + 1)
        )
        .scalars()
        .all()
    )
    page = list(rows[:limit])
    return BankEntryListOut(
        items=[
            BankEntryOut(
                id=b.id,
                festival_id=b.festival_id,
                account_id=b.account_id,
                direction=b.direction,
                amount_cents=b.amount_cents,
                batch_gross_cents=b.batch_gross_cents,
                batch_fee_cents=b.batch_fee_cents,
                business_date=b.business_date,
                occurred_at=b.occurred_at,
                settled_at=b.settled_at,
                status=b.settlement_status,
                provider_reference=b.provider_reference,
                description=b.quoted_excerpt,
                source_refs=_source_refs(b),
            )
            for b in page
        ],
        next_cursor=_next_cursor(rows, offset, limit),
    )


class CashCountOut(BaseModel):
    id: str
    vendor_id: str
    business_date: date
    amount_cents: int
    counted_at: datetime
    counted_by: str | None = None
    # A counted till is cash the vendor held; that is what makes it a count
    # rather than a deposit, so the custody is stated rather than left blank.
    custody: str = "VENDOR_RETAINED"
    description: str | None = None
    source_refs: list[SourceRefOut]


class CashCountListOut(BaseModel):
    items: list[CashCountOut]
    next_cursor: str | None = None


@router.get("/cash-counts", response_model=CashCountListOut)
def list_cash_counts(
    vendor_id: str | None = None,
    business_date: date | None = None,
    cursor: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CashCountListOut:
    offset = _offset(cursor)
    q = select(CashCount)
    if vendor_id is not None:
        q = q.where(CashCount.vendor_id == vendor_id)
    if business_date is not None:
        q = q.where(CashCount.business_date == business_date)
    rows = (
        db.execute(
            q.order_by(CashCount.counted_at.desc(), CashCount.id.asc())
            .offset(offset)
            .limit(limit + 1)
        )
        .scalars()
        .all()
    )
    page = list(rows[:limit])
    return CashCountListOut(
        items=[
            CashCountOut(
                id=c.id,
                vendor_id=c.vendor_id,
                business_date=c.business_date,
                amount_cents=c.counted_cents,
                counted_at=c.counted_at,
                counted_by=c.counted_by,
                description=c.quoted_excerpt,
                source_refs=_source_refs(c),
            )
            for c in page
        ],
        next_cursor=_next_cursor(rows, offset, limit),
    )


# ---------------------------------------------------------------------------
# Supplier payments
# ---------------------------------------------------------------------------


class SupplierPaymentOut(BaseModel):
    id: str
    festival_id: str
    supplier_id: str
    invoice_id: str
    amount_cents: int
    business_date: date
    payment_reference: str
    state: str
    approved_by: str | None = None
    approved_capability: str
    journal_entry_id: str | None = None
    created_at: datetime


class SupplierPaymentListOut(BaseModel):
    items: list[SupplierPaymentOut]
    next_cursor: str | None = None


@router.get("/supplier-payments", response_model=SupplierPaymentListOut)
def list_supplier_payments(
    festival_id: str | None = None,
    supplier_id: str | None = None,
    invoice_id: str | None = None,
    cursor: str | None = None,
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SupplierPaymentListOut:
    offset = _offset(cursor)
    q = select(SupplierPayment)
    if festival_id is not None:
        q = q.where(SupplierPayment.festival_id == festival_id)
    if supplier_id is not None:
        q = q.where(SupplierPayment.supplier_id == supplier_id)
    if invoice_id is not None:
        q = q.where(SupplierPayment.invoice_id == invoice_id)
    rows = (
        db.execute(
            q.order_by(SupplierPayment.created_at.desc(), SupplierPayment.id.asc())
            .offset(offset)
            .limit(limit + 1)
        )
        .scalars()
        .all()
    )
    page = list(rows[:limit])
    return SupplierPaymentListOut(
        items=[
            SupplierPaymentOut(
                id=sp.id,
                festival_id=sp.festival_id,
                supplier_id=sp.supplier_id,
                invoice_id=sp.invoice_id,
                amount_cents=sp.amount_cents,
                business_date=sp.business_date,
                payment_reference=sp.payment_reference,
                state=sp.state,
                approved_by=sp.approved_by,
                approved_capability=sp.approved_capability,
                journal_entry_id=sp.journal_entry_id,
                created_at=sp.created_at,
            )
            for sp in page
        ],
        next_cursor=_next_cursor(rows, offset, limit),
    )
