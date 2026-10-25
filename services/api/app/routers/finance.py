"""A08 endpoints: payables, other income/expense records, finance config,
journal posting/drill-down, draft tax, cash, P&L, vendor statements and the
daily close.

Capabilities (A01's `require_capability`, unchanged): `manage_payables` for
POs/invoices/credit notes/supplier payments, `finance_lead` for PO-variance
approval, `close_books` for journal posting, configuration, other records,
statements and the close. Every read requires an authenticated user. Every
view below is computed live from the database.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import domain.payout_dispatch as domain_payout_dispatch
import domain.settlement as domain_settlement
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.accounting as accounting
import app.cash_office as cash_office
import app.finance_records as records
import app.finance_reports as reports
import app.payables as payables
from app.auth import get_current_user, require_capability
from app.db import get_db
from app.models import (
    CAPABILITY_CLOSE_BOOKS,
    CAPABILITY_FINANCE_LEAD,
    CAPABILITY_MANAGE_PAYABLES,
    User,
)
from app.models_finance import DailyClose, PurchaseOrder, VendorStatement
from app.models_ledger import JournalEntry, Supplier, SupplierInvoice, Vendor
from app.models_reconciliation import BankEntry
from app.models_settlement import Settlement

router = APIRouter(prefix="/api/v1", tags=["finance"])


def _err(exc: Exception) -> HTTPException:
    if isinstance(exc, payables.PayablesNotFoundError | records.RecordNotFoundError | LookupError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, payables.DuplicateInvoiceError):
        return HTTPException(status_code=409, detail={"code": exc.code, "details": exc.details})
    if isinstance(exc, payables.PayablesConflictError | records.RecordConflictError):
        return HTTPException(status_code=409, detail={"code": exc.code, "details": exc.details})
    if isinstance(exc, payables.PaymentRefusedError):
        return HTTPException(
            status_code=422, detail={"code": exc.reason_code, "details": exc.details}
        )
    if isinstance(exc, accounting.BusinessDateClosedError):
        return HTTPException(
            status_code=409,
            detail={"code": "business_date_closed", "business_date": str(exc.business_date)},
        )
    if isinstance(exc, reports.CloseRefusedError):
        return HTTPException(
            status_code=422,
            detail={
                "code": "daily_close_checks_failed",
                "failing": exc.failing,
                "checks": {k: v for k, v in exc.checks.items() if k != "trial_balance"},
            },
        )
    raise exc


_DOMAIN_ERRORS = (
    payables.PayablesNotFoundError,
    payables.PayablesConflictError,
    payables.PaymentRefusedError,
    records.RecordConflictError,
    records.RecordNotFoundError,
    accounting.BusinessDateClosedError,
    reports.CloseRefusedError,
    LookupError,
)


def _invoice_out(inv: SupplierInvoice, as_of: date | None = None) -> dict:
    return {
        "id": inv.id,
        "festival_id": inv.festival_id,
        "supplier_id": inv.supplier_id,
        "kind": inv.kind,
        "po_refs": inv.po_refs,
        "invoice_number": inv.invoice_number,
        "invoice_date": inv.invoice_date.isoformat(),
        "due_date": inv.due_date.isoformat() if inv.due_date else None,
        "net_cents": inv.net_cents,
        "tax_cents": inv.tax_cents,
        "gross_cents": inv.gross_cents,
        "credited_cents": inv.credited_cents,
        "paid_cents": inv.paid_cents,
        "outstanding_cents": inv.outstanding_cents,
        "po_excess_cents": inv.po_excess_cents,
        "variance_approved_by": inv.variance_approved_by,
        "status": payables.display_status(inv, as_of or date.today()),
        "applies_to_invoice_id": inv.applies_to_invoice_id,
        "journal_entry_id": inv.journal_entry_id,
        "source_refs": inv.source_refs,
        "version": inv.version,
        "deposit_applied_cents": inv.deposit_applied_cents,
    }


# ---------------------------------------------------------------------------
# Suppliers / POs / invoices
# ---------------------------------------------------------------------------


class SupplierIn(BaseModel):
    festival_id: str
    name: str
    category: str | None = None


@router.post("/suppliers", status_code=201)
def create_supplier(
    body: SupplierIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_MANAGE_PAYABLES)),
) -> dict:
    sup, created = payables.get_or_create_supplier(
        db, festival_id=body.festival_id, name=body.name, category=body.category
    )
    db.commit()
    return {"id": sup.id, "name": sup.name, "festival_id": sup.festival_id, "created": created}


@router.get("/suppliers")
def list_suppliers(
    festival_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    rows = db.execute(select(Supplier).where(Supplier.festival_id == festival_id)).scalars()
    return {
        "items": [{"id": s.id, "name": s.name, "category": s.category} for s in rows],
        "next_cursor": None,
    }


class PurchaseOrderLineIn(BaseModel):
    line_no: int
    item_code: str
    quantity: int
    unit_price_cents: int


class PurchaseOrderIn(BaseModel):
    supplier_id: str
    po_number: str
    po_date: date
    amount_cents: int
    description: str | None = None
    receipt_evidence_refs: list = Field(default_factory=list)
    source_refs: list = Field(default_factory=list)
    lines: list[PurchaseOrderLineIn] | None = None


@router.post("/purchase-orders", status_code=201)
def create_purchase_order(
    body: PurchaseOrderIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_MANAGE_PAYABLES)),
) -> dict:
    payload = body.model_dump()
    lines = payload.pop("lines")
    try:
        po, created = payables.create_purchase_order(
            db,
            **payload,
            lines=lines,
            actor_id=user.id,
            actor_capability=CAPABILITY_MANAGE_PAYABLES,
        )
    except _DOMAIN_ERRORS as exc:
        raise _err(exc) from exc
    db.commit()
    po_lines = payables._po_lines(db, po.id)
    return {
        "id": po.id,
        "po_number": po.po_number,
        "amount_cents": po.amount_cents,
        "created": created,
        "lines": [
            {
                "id": line_.id,
                "line_no": line_.line_no,
                "item_code": line_.item_code,
                "quantity": line_.quantity,
                "unit_price_cents": line_.unit_price_cents,
            }
            for line_ in po_lines
        ],
    }


@router.get("/purchase-orders")
def list_purchase_orders(
    festival_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    rows = db.execute(
        select(PurchaseOrder).where(PurchaseOrder.festival_id == festival_id)
    ).scalars()
    return {
        "items": [
            {
                "id": p.id,
                "supplier_id": p.supplier_id,
                "po_number": p.po_number,
                "po_date": p.po_date.isoformat(),
                "amount_cents": p.amount_cents,
                "invoiced_cents": payables._po_invoiced_cents(db, p.id),
                "receipt_evidence_refs": p.receipt_evidence_refs,
                "source_refs": p.source_refs,
            }
            for p in rows
        ],
        "next_cursor": None,
    }


class InvoiceIn(BaseModel):
    supplier_id: str
    po_id: str | None = None
    invoice_number: str
    invoice_date: date
    due_date: date | None = None
    net_cents: int
    tax_cents: int = 0
    gross_cents: int
    received_at: datetime | None = None
    source_refs: list = Field(default_factory=list)


@router.post("/invoices", status_code=201)
def record_invoice(
    body: InvoiceIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_MANAGE_PAYABLES)),
) -> dict:
    try:
        inv, created, cases = payables.record_invoice(
            db, **body.model_dump(), actor_id=user.id, actor_capability=CAPABILITY_MANAGE_PAYABLES
        )
    except _DOMAIN_ERRORS as exc:
        raise _err(exc) from exc
    db.commit()
    return {"invoice": _invoice_out(inv), "created": created, "case_ids": [c.id for c in cases]}


class CreditNoteIn(BaseModel):
    credit_note_number: str
    credit_note_date: date
    net_cents: int
    tax_cents: int = 0
    gross_cents: int
    received_at: datetime | None = None
    source_refs: list = Field(default_factory=list)
    resolves_case_id: str | None = None


@router.post("/invoices/{invoice_id}/credit-notes", status_code=201)
def record_credit_note(
    invoice_id: str,
    body: CreditNoteIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_MANAGE_PAYABLES)),
) -> dict:
    try:
        cn, created = payables.record_credit_note(
            db,
            applies_to_invoice_id=invoice_id,
            **body.model_dump(),
            actor_id=user.id,
            actor_capability=CAPABILITY_MANAGE_PAYABLES,
        )
    except _DOMAIN_ERRORS as exc:
        raise _err(exc) from exc
    db.commit()
    original = db.get(SupplierInvoice, invoice_id)
    return {"credit_note": _invoice_out(cn), "invoice": _invoice_out(original), "created": created}


class VarianceIn(BaseModel):
    note: str | None = None


@router.post("/invoices/{invoice_id}/approve-variance")
def approve_variance(
    invoice_id: str,
    body: VarianceIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_FINANCE_LEAD)),
) -> dict:
    try:
        inv = payables.approve_variance(db, invoice_id=invoice_id, actor_id=user.id, note=body.note)
    except _DOMAIN_ERRORS as exc:
        raise _err(exc) from exc
    db.commit()
    return _invoice_out(inv)


class PayIn(BaseModel):
    amount_cents: int
    business_date: date
    # The bank debit that carried this payment, when known: it is cited in
    # the journal so the unmatched-bank scan treats that debit as explained.
    bank_entry_id: str | None = None


@router.post("/invoices/{invoice_id}/pay")
def pay_invoice(
    invoice_id: str,
    body: PayIn,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=100),
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_MANAGE_PAYABLES)),
) -> dict:
    if body.bank_entry_id is not None and db.get(BankEntry, body.bank_entry_id) is None:
        raise HTTPException(status_code=404, detail="bank entry not found")
    try:
        payment, created = payables.pay_invoice(
            db,
            invoice_id=invoice_id,
            amount_cents=body.amount_cents,
            idempotency_key=f"ap:{user.id}:{idempotency_key}",
            business_date=body.business_date,
            bank_entry_id=body.bank_entry_id,
            actor_id=user.id,
            actor_capability=CAPABILITY_MANAGE_PAYABLES,
        )
    except _DOMAIN_ERRORS as exc:
        db.rollback()
        raise _err(exc) from exc
    db.commit()
    inv = db.get(SupplierInvoice, invoice_id)
    return {
        "payment": {
            "id": payment.id,
            "amount_cents": payment.amount_cents,
            "state": payment.state,
            "payment_reference": payment.payment_reference,
            "journal_entry_id": payment.journal_entry_id,
            "label": "SIMULATED supplier payment - no real money movement",
        },
        "created": created,
        "invoice": _invoice_out(inv),
    }


@router.get("/invoices/{invoice_id}/payability")
def invoice_payability(
    invoice_id: str,
    as_of: datetime | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """As-of view (TICKET section 5): the agreed/invoiced/deposit/accepted-
    units/max-payable/pending picture for one invoice. `as_of` filters
    which delivery receipts count -- defaults to now if omitted, but a
    caller building a reproducible demo screen should always pass it
    explicitly."""
    inv = db.get(SupplierInvoice, invoice_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    cutoff = as_of or datetime.now(UTC)
    accepted_qty = 0
    total_units: int | None = None
    unit_price_cents: int | None = None
    delivery_supported = None
    if inv.po_refs:
        po_id = inv.po_refs[0]
        lines = payables._po_lines(db, po_id)
        if lines:
            total_units = sum(line_.quantity for line_ in lines)
            delivery_supported = 0
            for line_ in lines:
                receipts = (
                    db.execute(
                        select(payables.DeliveryReceipt).where(
                            payables.DeliveryReceipt.po_line_id == line_.id,
                            payables.DeliveryReceipt.available_at <= cutoff,
                        )
                    )
                    .scalars()
                    .all()
                )
                qty_as_of = sum(r.quantity for r in receipts)
                accepted_qty += qty_as_of
                delivery_supported += qty_as_of * line_.unit_price_cents
            if len(lines) == 1:
                unit_price_cents = lines[0].unit_price_cents
    balance = payables._balance(inv)
    max_payable = (
        payables.rules.max_payable_now(balance, delivery_supported) if inv.kind == "INVOICE" else 0
    )
    pending_evidence_cents = (
        max(inv.gross_cents - (delivery_supported or 0), 0) if delivery_supported is not None else 0
    )
    return {
        "invoice_id": inv.id,
        "as_of": cutoff.isoformat(),
        "agreed_cents": inv.gross_cents,
        "invoiced_cents": inv.gross_cents,
        "deposit_applied_cents": inv.deposit_applied_cents,
        "paid_cents": inv.paid_cents,
        "accepted_units": accepted_qty,
        "total_units": total_units,
        "unit_price_cents": unit_price_cents,
        "delivery_supported_cents": delivery_supported,
        "max_payable_now_cents": max_payable,
        "pending_evidence_cents": pending_evidence_cents,
        "outstanding_cents": balance.outstanding_cents,
        "has_delivery_limit": delivery_supported is not None,
    }


class SupplierDepositIn(BaseModel):
    po_id: str | None = None
    amount_cents: int
    paid_business_date: date
    paid_at: datetime
    idempotency_key: str
    source_refs: list = Field(default_factory=list)


@router.post("/suppliers/{supplier_id}/deposits", status_code=201)
def pay_supplier_deposit(
    supplier_id: str,
    body: SupplierDepositIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_MANAGE_PAYABLES)),
) -> dict:
    try:
        deposit, created = payables.pay_supplier_deposit(
            db,
            supplier_id=supplier_id,
            **body.model_dump(),
            actor_id=user.id,
            actor_capability=CAPABILITY_MANAGE_PAYABLES,
        )
    except _DOMAIN_ERRORS as exc:
        raise _err(exc) from exc
    db.commit()
    return {
        "id": deposit.id,
        "supplier_id": deposit.supplier_id,
        "amount_cents": deposit.amount_cents,
        "journal_entry_id": deposit.journal_entry_id,
        "created": created,
    }


class ApplyDepositIn(BaseModel):
    deposit_id: str
    amount_cents: int


@router.post("/invoices/{invoice_id}/apply-deposit", status_code=201)
def apply_deposit(
    invoice_id: str,
    body: ApplyDepositIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_MANAGE_PAYABLES)),
) -> dict:
    try:
        application, created = payables.apply_deposit(
            db,
            invoice_id=invoice_id,
            **body.model_dump(),
            actor_id=user.id,
            actor_capability=CAPABILITY_MANAGE_PAYABLES,
        )
    except _DOMAIN_ERRORS as exc:
        raise _err(exc) from exc
    db.commit()
    inv = db.get(SupplierInvoice, invoice_id)
    return {
        "application_id": application.id,
        "amount_cents": application.amount_cents,
        "created": created,
        "invoice": _invoice_out(inv),
    }


class DeliveryReceiptIn(BaseModel):
    po_id: str
    line_no: int
    quantity: int
    accepted_at: datetime
    available_at: datetime
    receipt_key: str
    source_refs: list = Field(default_factory=list)


@router.post("/deliveries", status_code=201)
def record_delivery(
    body: DeliveryReceiptIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_MANAGE_PAYABLES)),
) -> dict:
    line = db.execute(
        select(payables.PurchaseOrderLine).where(
            payables.PurchaseOrderLine.po_id == body.po_id,
            payables.PurchaseOrderLine.line_no == body.line_no,
        )
    ).scalar_one_or_none()
    if line is None:
        raise HTTPException(status_code=404, detail="purchase order line not found")
    try:
        receipt, created = payables.record_delivery_receipt(
            db,
            po_line_id=line.id,
            quantity=body.quantity,
            accepted_at=body.accepted_at,
            available_at=body.available_at,
            receipt_key=body.receipt_key,
            source_refs=body.source_refs,
            actor_id=user.id,
            actor_capability=CAPABILITY_MANAGE_PAYABLES,
        )
    except _DOMAIN_ERRORS as exc:
        raise _err(exc) from exc
    db.commit()
    return {
        "id": receipt.id,
        "po_line_id": receipt.po_line_id,
        "quantity": receipt.quantity,
        "receipt_key": receipt.receipt_key,
        "created": created,
    }


class SlaPotentialCreditIn(BaseModel):
    potential_credit_cents: int
    clause: str
    evidence_refs: list = Field(default_factory=list)
    note: str


@router.post("/invoices/{invoice_id}/sla-potential-credit", status_code=201)
def post_sla_potential_credit(
    invoice_id: str,
    body: SlaPotentialCreditIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_MANAGE_PAYABLES)),
) -> dict:
    try:
        case = payables.open_sla_potential_credit_case(
            db,
            invoice_id=invoice_id,
            **body.model_dump(),
            actor_id=user.id,
            actor_capability=CAPABILITY_MANAGE_PAYABLES,
        )
    except _DOMAIN_ERRORS as exc:
        raise _err(exc) from exc
    db.commit()
    return {"case_id": case.id if case else None, "created": case is not None}


class SupplierQuoteIn(BaseModel):
    request_key: str
    version: int
    supersedes_id: str | None = None
    available_at: datetime
    base_cents: int | None = None
    delivery_cents: int | None = None
    tax_cents: int | None = None
    deposit_cents: int | None = None
    deposit_due: date | None = None
    balance_due: date | None = None
    insurance_evidence: bool | None = None
    citations: dict = Field(default_factory=dict)
    source_refs: list = Field(default_factory=list)


@router.post("/suppliers/{supplier_id}/quotes", status_code=201)
def post_supplier_quote(
    supplier_id: str,
    body: SupplierQuoteIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_MANAGE_PAYABLES)),
) -> dict:
    from app.models_finance import SupplierQuote

    existing = db.execute(
        select(SupplierQuote).where(
            SupplierQuote.supplier_id == supplier_id,
            SupplierQuote.request_key == body.request_key,
            SupplierQuote.version == body.version,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return {"id": existing.id, "created": False}
    quote = SupplierQuote(supplier_id=supplier_id, **body.model_dump())
    db.add(quote)
    db.commit()
    return {"id": quote.id, "created": True}


@router.get("/supplier-quotes")
def list_supplier_quotes(
    supplier_id: str | None = None,
    as_of: datetime | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    from app.models_finance import SupplierQuote

    q = select(SupplierQuote)
    if supplier_id is not None:
        q = q.where(SupplierQuote.supplier_id == supplier_id)
    cutoff = as_of or datetime.now(UTC)
    q = q.where(SupplierQuote.available_at <= cutoff)
    rows = db.execute(q.order_by(SupplierQuote.request_key, SupplierQuote.version)).scalars().all()
    return {
        "as_of": cutoff.isoformat(),
        "items": [
            {
                "id": r.id,
                "supplier_id": r.supplier_id,
                "request_key": r.request_key,
                "version": r.version,
                "supersedes_id": r.supersedes_id,
                "available_at": r.available_at.isoformat(),
                "base_cents": r.base_cents,
                "delivery_cents": r.delivery_cents,
                "tax_cents": r.tax_cents,
                "deposit_cents": r.deposit_cents,
                "deposit_due": r.deposit_due.isoformat() if r.deposit_due else None,
                "balance_due": r.balance_due.isoformat() if r.balance_due else None,
                "insurance_evidence": r.insurance_evidence,
                "citations": r.citations,
            }
            for r in rows
        ],
        "next_cursor": None,
    }


_FIXTURE_DIR = Path(__file__).resolve().parents[4] / "fixtures" / "encore-procurement-synthetic-v1"


def _load_fixture_json(name: str):
    with open(_FIXTURE_DIR / "data" / f"{name}.json") as f:
        return json.load(f)


@router.get("/supplier-comparison")
def supplier_comparison(
    request_key: str,
    as_of: date,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """TICKET section 7. Quote amounts come from the imported `supplier_quotes`
    rows (demo-clock-gated by `available_at <= as_of`); the cash-impact
    baseline and supplier history are read directly from the fixture's
    cash_inputs.json/historical_contracts.json -- a labelled synthetic
    comparison, never written to cash_pools/opening_balances/the journal.
    Selection stays undecided here: no winner field, no aggregate score."""
    import domain.procurement_cash as cash_rules

    from app.models_finance import SupplierQuote

    cutoff = datetime.combine(as_of, datetime.min.time())
    quotes = (
        db.execute(
            select(SupplierQuote).where(
                SupplierQuote.request_key == request_key, SupplierQuote.available_at <= cutoff
            )
        )
        .scalars()
        .all()
    )

    try:
        cash = _load_fixture_json("cash_inputs")
        hist_contracts = {h["supplier_id"]: h for h in _load_fixture_json("historical_contracts")}
        hist_deliveries = {d["supplier_id"]: d for d in _load_fixture_json("historical_deliveries")}
    except FileNotFoundError:
        cash, hist_contracts, hist_deliveries = None, {}, {}

    rows = []
    for q in quotes:
        missing = []
        if q.delivery_cents is None:
            missing.append("delivery pricing")
        if not q.insurance_evidence:
            missing.append("insurance evidence")
        complete = not missing
        complete_total = (
            (q.base_cents or 0) + (q.delivery_cents or 0) + (q.tax_cents or 0) if complete else None
        )

        breach = None
        if cash is not None and q.deposit_cents is not None and q.deposit_due is not None:
            result = cash_rules.synthetic_cash_breach(
                as_of=date.fromisoformat(cash["as_of"]),
                opening_posted_cents=cash["opening_posted_cents"],
                buffer_cents=cash["buffer_cents"],
                scheduled_outflows=[
                    (date.fromisoformat(o["date"]), o["amount_cents"])
                    for o in cash["scheduled_outflows"]
                ],
                deposit_due=q.deposit_due,
                deposit_cents=q.deposit_cents,
                horizon_end=date.fromisoformat(cash["as_of"]) + timedelta(days=10),
            )
            breach = {
                "minimum_closing_cents": result.minimum_closing_cents,
                "first_breach_date": (
                    result.first_breach_date.isoformat() if result.first_breach_date else None
                ),
                "buffer_gap_cents": result.buffer_gap_cents,
            }

        h = hist_contracts.get(q.supplier_id)
        history_label = "no_history"
        variance_bps = None
        otif = None
        if h is not None:
            history_label = "has_history"
            variance_bps = round(
                (h["final_net_cost_cents"] - h["original_budget_cents"])
                / h["original_budget_cents"]
                * 10000
            )
            d = hist_deliveries.get(q.supplier_id)
            if d is not None:
                on_time = d["completed_at"] <= d["deadline"]
                otif = f"{1 if on_time else 0}/{d.get('scheduled_delivery_count', 1)}"

        rows.append(
            {
                "supplier_id": q.supplier_id,
                "quote_id": q.id,
                "version": q.version,
                "supersedes_id": q.supersedes_id,
                "complete": complete,
                "missing_requirements": missing,
                "complete_quote_cents": complete_total,
                "deposit_cents": q.deposit_cents,
                "deposit_due": q.deposit_due.isoformat() if q.deposit_due else None,
                "balance_due": q.balance_due.isoformat() if q.balance_due else None,
                "cash_impact_synthetic": breach,
                "history_label": history_label,
                "history_variance_bps": variance_bps,
                "history_otif": otif,
            }
        )

    return {
        "request_key": request_key,
        "as_of": as_of.isoformat(),
        "cash_baseline_note": (
            "Synthetic demo baseline (fixture cash_inputs.json): proposed, not approved "
            "business policy. Not the real books."
        ),
        "selection": "undecided",
        "no_automatic_winner": True,
        "quotes": rows,
    }


@router.get("/invoices")
def list_invoices(
    festival_id: str,
    as_of: date | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    out = payables.payables_summary(db, festival_id=festival_id, as_of=as_of or date.today())
    # The summary nests its collection under `invoices`; readers of a list
    # expect {items, next_cursor}. Both are served: the summary's own totals
    # stay where its existing callers look for them. Not paged -- the payables
    # of one festival are a page, not a stream -- so next_cursor is null.
    return {**out, "items": out.get("invoices") or [], "next_cursor": None}


@router.get("/invoices/{invoice_id}")
def get_invoice(
    invoice_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    inv = db.get(SupplierInvoice, invoice_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="invoice not found")
    return _invoice_out(inv)


# ---------------------------------------------------------------------------
# Other organizer records + config
# ---------------------------------------------------------------------------


class OpeningIn(BaseModel):
    festival_id: str
    as_of: date
    amount_cents: int
    source_refs: list = Field(default_factory=list)


class TicketIn(BaseModel):
    festival_id: str
    platform: str
    settlement_reference: str
    gross_cents: int
    platform_fees_cents: int
    net_cents: int
    business_date: date
    bank_entry_id: str | None = None
    source_refs: list = Field(default_factory=list)


class RecognizeIn(BaseModel):
    festival_id: str
    business_date: date


class SponsorInvoiceIn(BaseModel):
    festival_id: str
    sponsor_name: str
    invoice_number: str
    invoice_date: date
    due_date: date | None = None
    amount_cents: int
    source_refs: list = Field(default_factory=list)


class SponsorReceiptIn(BaseModel):
    sponsor_invoice_id: str
    receipt_reference: str
    amount_cents: int
    business_date: date
    bank_entry_id: str | None = None
    source_refs: list = Field(default_factory=list)


class ExpenseIn(BaseModel):
    festival_id: str
    expense_reference: str
    category: str
    description: str | None = None
    amount_cents: int
    business_date: date
    bank_entry_id: str | None = None
    source_refs: list = Field(default_factory=list)


def _record(db, user, fn, body: BaseModel, **extra) -> dict:
    try:
        row, created = fn(
            db,
            **body.model_dump(),
            **extra,
            actor_id=user.id,
            actor_capability=CAPABILITY_CLOSE_BOOKS,
        )
    except _DOMAIN_ERRORS as exc:
        raise _err(exc) from exc
    db.commit()
    return {"id": row.id, "created": created}


@router.post("/finance/opening-balances", status_code=201)
def opening_balance(
    body: OpeningIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CLOSE_BOOKS)),
) -> dict:
    return _record(db, user, records.record_opening_balance, body)


@router.post("/finance/ticket-settlements", status_code=201)
def ticket_settlement(
    body: TicketIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CLOSE_BOOKS)),
) -> dict:
    return _record(db, user, records.record_ticket_settlement, body)


@router.post("/finance/tickets/recognize")
def recognize_tickets(
    body: RecognizeIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CLOSE_BOOKS)),
) -> dict:
    try:
        result = records.recognize_ticket_revenue(
            db, **body.model_dump(), actor_id=user.id, actor_capability=CAPABILITY_CLOSE_BOOKS
        )
    except _DOMAIN_ERRORS as exc:
        raise _err(exc) from exc
    db.commit()
    return {
        "entry_key": result.entry.entry_key if result.entry else None,
        "created": result.created,
    }


@router.post("/finance/sponsor-invoices", status_code=201)
def sponsor_invoice(
    body: SponsorInvoiceIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CLOSE_BOOKS)),
) -> dict:
    try:
        si, created = records.record_sponsor_invoice(db, **body.model_dump())
    except _DOMAIN_ERRORS as exc:
        raise _err(exc) from exc
    db.commit()
    return {"id": si.id, "created": created}


@router.post("/finance/sponsorship-receipts", status_code=201)
def sponsorship_receipt(
    body: SponsorReceiptIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CLOSE_BOOKS)),
) -> dict:
    return _record(db, user, records.record_sponsorship_receipt, body)


@router.post("/finance/expenses", status_code=201)
def expense(
    body: ExpenseIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CLOSE_BOOKS)),
) -> dict:
    return _record(db, user, records.record_expense, body)


def _config_out(cfg) -> dict:
    return {
        "id": cfg.id,
        "festival_id": cfg.festival_id,
        "version": cfg.version,
        "status": cfg.status,
        "timezone": cfg.timezone,
        "statement_local_time": cfg.statement_local_time,
        "payout_local_time": cfg.payout_local_time,
        "forecast_horizon_days": cfg.forecast_horizon_days,
        "tax": cfg.tax,
        "label": "DEMO configuration - not an approved business or tax fact",
    }


@router.get("/business-config")
def get_business_config(
    festival_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    """The C05 business configuration: how this festival charges and pays.

    The finance posting config carries the versioned, stored part (timezone,
    the 9 AM / 10 AM times, the forecast horizon, the tax configuration). The
    rest -- the default terms a vendor falls back to and the payout controls --
    lives in the domain package as the constants the engines actually apply, so
    it is read from there rather than restated. If a value here disagreed with
    what settlement and release use, this endpoint would be the thing that is
    wrong.

    `currency` is C01's fixed USD for v1. `draft_notice` carries the config's
    own label, so nobody reads these as approved business facts.
    """
    cfg = records.get_or_default_config(db, festival_id)
    db.commit()
    out = _config_out(cfg)
    return {
        **out,
        "currency": "USD",
        "draft_notice": out.get("label"),
        "default_terms": {
            # Applied when a vendor's active term leaves these null -- "null
            # means use the festival default, never zero".
            "commission_bps": domain_settlement.DEFAULT_COMMISSION_BPS,
            "festival_minimum_cents": domain_settlement.DEFAULT_MINIMUM_FEE_CENTS,
            "basis": "Applies only where a vendor's own contract terms do not state a rate.",
        },
        "payout_controls": {
            "auto_limit_cents_per_vendor_day": domain_payout_dispatch.AUTOMATIC_RELEASE_LIMIT_CENTS,
            # No real rail is reachable from this system; every payout is
            # executed against the simulator, which is stated rather than
            # implied so a reader never takes a CONFIRMED payout for a
            # real transfer.
            "payment_mode": "SIMULATED",
            "statement_local_time": out.get("statement_local_time"),
            "payout_local_time": out.get("payout_local_time"),
        },
    }


@router.get("/finance/config")
def get_config(
    festival_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    cfg = records.get_or_default_config(db, festival_id)
    db.commit()
    return _config_out(cfg)


class ConfigIn(BaseModel):
    festival_id: str
    tax: dict
    timezone: str = "America/Los_Angeles"
    statement_local_time: str = Field("09:00", pattern=r"^\d{2}:\d{2}$")
    payout_local_time: str = Field("10:00", pattern=r"^\d{2}:\d{2}$")
    forecast_horizon_days: int = 14


@router.put("/finance/config")
def put_config(
    body: ConfigIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CLOSE_BOOKS)),
) -> dict:
    try:
        cfg = records.set_config(
            db, **body.model_dump(), actor_id=user.id, actor_capability=CAPABILITY_CLOSE_BOOKS
        )
    except _DOMAIN_ERRORS as exc:
        raise _err(exc) from exc
    db.commit()
    return _config_out(cfg)


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


class PostDayIn(BaseModel):
    festival_id: str
    business_date: date


@router.post("/journal/post-day")
def post_day(
    body: PostDayIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CLOSE_BOOKS)),
) -> dict:
    """Posts (idempotently) the day's centrally collected sales net of
    refunds/chargebacks, processor deposits, settlement commissions,
    cash-office deposits whose bank credit landed that day, and chargeback
    fee recharges dated that day (re-evaluated against current terms)."""
    kw = dict(
        festival_id=body.festival_id,
        business_date=body.business_date,
        actor_id=user.id,
        actor_capability=CAPABILITY_CLOSE_BOOKS,
    )
    try:
        results = (
            accounting.post_sales_for_day(db, **kw)
            + accounting.post_processor_deposits(db, **kw)
            + accounting.post_settlements_for_day(db, **kw)
            + cash_office.post_cash_deposits(db, **kw)
            + cash_office.post_chargeback_fees_for_day(db, **kw)
        )
    except _DOMAIN_ERRORS as exc:
        db.rollback()
        raise _err(exc) from exc
    db.commit()
    return {
        "posted": [r.entry.entry_key for r in results if r.created],
        "unchanged": [r.base_key for r in results if not r.created],
    }


@router.get("/journal")
def list_journal(
    festival_id: str,
    business_date: date | None = None,
    kind: str | None = None,
    cursor: str | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    q = select(JournalEntry).where(JournalEntry.festival_id == festival_id)
    if business_date is not None:
        q = q.where(JournalEntry.business_date == business_date)
    if kind is not None:
        q = q.where(JournalEntry.kind == kind)
    offset = int(cursor) if cursor and cursor.isdigit() else 0
    limit = max(1, min(limit, 500))
    rows = (
        db.execute(
            q.order_by(JournalEntry.business_date, JournalEntry.created_at, JournalEntry.id)
            .offset(offset)
            .limit(limit + 1)
        )
        .scalars()
        .all()
    )
    return {
        "items": [reports.entry_out(e) for e in rows[:limit]],
        "next_cursor": str(offset + limit) if len(rows) > limit else None,
    }


@router.get("/journal/trial-balance")
def get_trial_balance(
    festival_id: str,
    through: date | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    return reports.trial_balance(db, festival_id=festival_id, through=through)


@router.get("/journal/{entry_id}")
def get_journal_entry(
    entry_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    e = db.get(JournalEntry, entry_id)
    if e is None:
        raise HTTPException(status_code=404, detail="journal entry not found")
    return reports.entry_out(e, drill=True, db=db)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Reports as addressable records
#
# These reports are computed from the books on request; none is stored. That
# would normally mean nothing can be linked to, opened by id or exported --
# which is what the reports screen needs.
#
# Rather than add a table and an "issue report" step, a report id states its
# own inputs: `<kind>:<festival_id>:<through>`. Because a report is a pure
# function of the books at a date, that id IS a durable reference -- resolving
# it recomputes exactly the same report. Nothing is invented and no schema
# changes; the trade is that a report reflects the books as they are NOW at
# that date, so a correction posted later changes what the id resolves to.
# Issuing immutable snapshots is a separate decision, recorded in
# docs/progress/V1-READINESS.md.
# ---------------------------------------------------------------------------

_REPORT_KINDS = {
    "tax": ("Sales tax", "DRAFT, NOT FILED"),
    "pnl": ("Profit and loss", "DRAFT"),
    "festival": ("Festival summary", "DRAFT"),
}


class ReportSectionOut(BaseModel):
    section_id: str
    title: str
    lines: list[dict]


class ReportOut(BaseModel):
    id: str
    kind: str
    title: str
    status: str
    festival_id: str
    period_to: date | None = None
    generated_at: datetime
    # What the report rests on, so a reader is not left guessing whether these
    # numbers are filed, final or illustrative.
    basis_note: str
    headline: list[dict]
    sections: list[ReportSectionOut]
    source_refs: list = []


class ReportListOut(BaseModel):
    items: list[ReportOut]
    next_cursor: str | None = None


def _report_id(kind: str, festival_id: str, through: date | None) -> str:
    return f"{kind}:{festival_id}:{through.isoformat() if through else 'latest'}"


def _parse_report_id(report_id: str) -> tuple[str, str, date | None]:
    kind, _, rest = report_id.partition(":")
    festival_id, _, through = rest.rpartition(":")
    if kind not in _REPORT_KINDS or not festival_id:
        raise HTTPException(status_code=404, detail="report not found")
    if through in ("", "latest"):
        return kind, festival_id, None
    try:
        return kind, festival_id, date.fromisoformat(through)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="report not found") from exc


def _money_lines(pairs: list[tuple[str, int]], prefix: str) -> list[dict]:
    return [
        {"line_id": f"{prefix}.{i}", "label": label, "amount_cents": amount}
        for i, (label, amount) in enumerate(pairs)
    ]


def _section_lines(value: object) -> list[dict]:
    """A report section's lines, always as objects.

    The computed reports mix shapes: a list of row dicts, a single summary
    dict, or a plain list of strings (the closed business dates). A reader
    should not have to branch on that, so a bare value becomes {"label": ...}
    rather than being dropped or stringified into one blob.
    """
    if isinstance(value, dict):
        return [{"label": k.replace("_", " "), "value": v} for k, v in value.items()]
    if isinstance(value, list):
        return [v if isinstance(v, dict) else {"label": str(v)} for v in value]
    return [{"label": str(value)}]


def _build_report(db: Session, kind: str, festival_id: str, through: date | None) -> ReportOut:
    """Compute one report and shape it as sections a reader can read.

    The three computed reports each have their own natural shape; this is the
    only place that knows how to present each one, so no screen has to.
    """
    title, status = _REPORT_KINDS[kind]
    headline: list[dict] = []
    sections: list[ReportSectionOut] = []
    basis = ""

    if kind == "tax":
        r = reports.tax_summary(db, festival_id=festival_id, through=through)
        totals = r.get("totals") or {}
        basis = f"{r.get('status', '')} {r.get('config_label', '')}".strip()
        headline = _money_lines([(k.replace("_", " "), v) for k, v in totals.items()], "total")
        sections = [
            ReportSectionOut(
                section_id="by_business_date",
                title="By business date",
                lines=list(r.get("by_business_date") or []),
            ),
            ReportSectionOut(
                section_id="mismatch_lines",
                title="Supplied vs recalculated differences",
                lines=list(r.get("mismatch_lines") or []),
            ),
        ]
    elif kind == "pnl":
        r = reports.profit_and_loss(db, festival_id=festival_id, through=through)
        basis = str(r.get("note") or "")
        headline = _money_lines(
            [
                ("Total revenue", int(r.get("total_revenue_cents") or 0)),
                ("Total expenses", int(r.get("total_expenses_cents") or 0)),
                ("Net income", int(r.get("net_income_cents") or 0)),
            ],
            "headline",
        )
        sections = [
            ReportSectionOut(
                section_id="revenue", title="Revenue", lines=list(r.get("revenue") or [])
            ),
            ReportSectionOut(
                section_id="expenses", title="Expenses", lines=list(r.get("expenses") or [])
            ),
        ]
    else:
        # festival_report is as-of a date, not through one; the id's date is
        # that as-of, and today when the id says "latest".
        r = reports.festival_report(db, festival_id=festival_id, as_of=through or date.today())
        basis = "; ".join(str(x) for x in (r.get("labels") or []))
        pnl = r.get("profit_and_loss") or {}
        headline = _money_lines(
            [
                ("Net income", int(pnl.get("net_income_cents") or 0)),
                ("Cash in bank", int((r.get("cash") or {}).get("bank_cents") or 0)),
                (
                    "Payables outstanding",
                    int((r.get("payables") or {}).get("outstanding_cents") or 0),
                ),
            ],
            "headline",
        )
        sections = [
            ReportSectionOut(
                section_id=name,
                title=name.replace("_", " ").capitalize(),
                lines=_section_lines(value),
            )
            for name, value in r.items()
            if isinstance(value, list | dict) and value
        ]

    return ReportOut(
        id=_report_id(kind, festival_id, through),
        kind=kind,
        title=title,
        status=status,
        festival_id=festival_id,
        period_to=through,
        generated_at=datetime.now(UTC),
        basis_note=basis,
        headline=headline,
        sections=sections,
    )


@router.get("/reports", response_model=ReportListOut)
def list_reports(
    festival_id: str,
    through: date | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ReportListOut:
    """Every report that can be produced for this festival, newest basis first.

    `next_cursor` is always null: there are three kinds, not a stream of
    reports, so the envelope keeps the shape every other list uses without
    pretending to page.
    """
    items = [_build_report(db, kind, festival_id, through) for kind in _REPORT_KINDS]
    db.commit()  # first use may create the default finance config version
    return ReportListOut(items=items, next_cursor=None)


@router.get("/reports/by-id/{report_id}", response_model=ReportOut)
def get_report(
    report_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ReportOut:
    """One report, resolved from an id that states its own inputs."""
    kind, festival_id, through = _parse_report_id(report_id)
    out = _build_report(db, kind, festival_id, through)
    db.commit()  # first use may create the default finance config version
    return out


@router.get("/reports/tax")
def get_tax(
    festival_id: str,
    through: date | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    out = reports.tax_summary(db, festival_id=festival_id, through=through)
    db.commit()  # first use may create the default config version
    return out


@router.get("/cash")
def get_cash(
    festival_id: str,
    as_of: date | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    out = reports.cash_report(db, festival_id=festival_id, as_of=as_of or date.today())
    db.commit()
    # A cash position is a single as-of snapshot, and the forecast points it
    # carries are inside it. Served ALSO as a one-entry collection so a reader
    # of a list gets the shape it expects rather than a bare object; the flat
    # keys stay for the callers that already read them.
    return {**out, "items": [out], "next_cursor": None}


@router.get("/reports/pnl")
def get_pnl(
    festival_id: str,
    through: date | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    return reports.profit_and_loss(db, festival_id=festival_id, through=through)


@router.get("/reports/festival")
def get_festival_report(
    festival_id: str,
    as_of: date | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    out = reports.festival_report(db, festival_id=festival_id, as_of=as_of or date.today())
    db.commit()
    return out


# ---------------------------------------------------------------------------
# Vendor statements
# ---------------------------------------------------------------------------


class StatementIn(BaseModel):
    vendor_id: str
    business_date: date
    demo_effective_at: datetime | None = None


@router.post("/statements")
def issue_statement(
    body: StatementIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CLOSE_BOOKS)),
) -> dict:
    try:
        stmt, created = reports.issue_vendor_statement(
            db, **body.model_dump(), actor_id=user.id, actor_capability=CAPABILITY_CLOSE_BOOKS
        )
    except _DOMAIN_ERRORS as exc:
        raise _err(exc) from exc
    db.commit()
    return {"statement": reports.statement_out(stmt), "created": created}


class IssueDayIn(BaseModel):
    festival_id: str
    business_date: date
    demo_effective_at: datetime | None = None


@router.post("/statements/issue-day")
def issue_day(
    body: IssueDayIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CLOSE_BOOKS)),
) -> dict:
    """Issues (or re-issues only if changed) a statement for every vendor
    with a settlement for the date. Meant for the configured 09:00 run."""
    vendor_ids = sorted(
        {
            v
            for (v,) in db.execute(
                select(Settlement.vendor_id)
                .join(Vendor, Vendor.id == Settlement.vendor_id)
                .where(
                    Vendor.festival_id == body.festival_id,
                    Settlement.business_date == body.business_date,
                )
            ).all()
        }
    )
    out = []
    for vid in vendor_ids:
        stmt, created = reports.issue_vendor_statement(
            db,
            vendor_id=vid,
            business_date=body.business_date,
            demo_effective_at=body.demo_effective_at,
            actor_id=user.id,
            actor_capability=CAPABILITY_CLOSE_BOOKS,
        )
        out.append(
            {
                "vendor_id": vid,
                "statement_id": stmt.id,
                "status": stmt.status,
                "statement_version": stmt.statement_version,
                "created": created,
                "issued_on_time": stmt.issued_on_time,
            }
        )
    db.commit()
    return {"statements": out}


@router.get("/statements")
def list_statements(
    vendor_id: str,
    business_date: date | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    q = select(VendorStatement).where(VendorStatement.vendor_id == vendor_id)
    if business_date is not None:
        q = q.where(VendorStatement.business_date == business_date)
    rows = db.execute(
        q.order_by(VendorStatement.business_date, VendorStatement.statement_version)
    ).scalars()
    return {"items": [reports.statement_out(s) for s in rows], "next_cursor": None}


# ---------------------------------------------------------------------------
# Daily close
# ---------------------------------------------------------------------------


class CloseIn(BaseModel):
    festival_id: str
    business_date: date
    note: str | None = None


@router.get("/close/checks")
def get_close_checks(
    festival_id: str,
    business_date: date,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    out = reports.close_checks(db, festival_id=festival_id, business_date=business_date)
    db.commit()
    return out


@router.post("/close")
def close_day(
    body: CloseIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CLOSE_BOOKS)),
) -> dict:
    try:
        close, created = reports.run_daily_close(
            db, **body.model_dump(), actor_id=user.id, actor_capability=CAPABILITY_CLOSE_BOOKS
        )
    except _DOMAIN_ERRORS as exc:
        db.rollback()
        raise _err(exc) from exc
    db.commit()
    return {
        "id": close.id,
        "business_date": close.business_date.isoformat(),
        "created": created,
        "checks": close.checks,
        "journal_entry_count": close.journal_entry_count,
        "trial_balance_balanced": close.trial_balance["balanced"],
    }


@router.get("/close")
def list_closes(
    festival_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    rows = db.execute(
        select(DailyClose)
        .where(DailyClose.festival_id == festival_id)
        .order_by(DailyClose.business_date)
    ).scalars()
    return {
        "items": [
            {
                "id": c.id,
                "business_date": c.business_date.isoformat(),
                "closed_by": c.closed_by,
                "checks": c.checks,
                "journal_entry_count": c.journal_entry_count,
                "closed_at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in rows
        ],
        "next_cursor": None,
    }
