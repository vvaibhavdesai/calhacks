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

# Reused rather than redeclared: the cash position embeds the cash-office
# summary and its suspense rows, and the festival report embeds cash-office
# cases and chargeback fees. Those shapes are already modelled where they
# are produced, and a second copy here would mangle the generated client.
from app.routers.cash_office import (
    CashCaseOut,
    ChargebackFeeStatusOut,
    SlipSummaryOut,
    SuspenseItemOut,
)

router = APIRouter(prefix="/api/v1", tags=["finance"])


# Instants and dates are `str`: the handlers emit `.isoformat()`, and a
# `datetime` field would make pydantic rewrite the `+00:00` offset as `Z`.


# -- Suppliers / POs / invoices ---------------------------------------------


class CreateSupplierOut(BaseModel):
    id: str
    name: str
    festival_id: str
    # False when the supplier already existed under this name.
    created: bool


class SupplierOut(BaseModel):
    id: str
    name: str
    category: str | None


class SupplierListOut(BaseModel):
    items: list[SupplierOut]
    # Always null throughout this router: one festival's finance records are
    # a page, not a stream.
    next_cursor: str | None


class CreatePurchaseOrderOut(BaseModel):
    id: str
    po_number: str
    amount_cents: int
    created: bool


class PurchaseOrderOut(BaseModel):
    id: str
    supplier_id: str
    po_number: str
    po_date: str
    amount_cents: int
    # Computed live from the invoices citing this PO, not stored.
    invoiced_cents: int
    receipt_evidence_refs: list
    source_refs: list


class PurchaseOrderListOut(BaseModel):
    items: list[PurchaseOrderOut]
    next_cursor: str | None


class InvoiceOut(BaseModel):
    """One supplier invoice or credit note (`_invoice_out`)."""

    id: str
    festival_id: str
    supplier_id: str
    kind: str  # INVOICE | CREDIT_NOTE
    po_refs: list
    invoice_number: str
    invoice_date: str
    due_date: str | None
    net_cents: int
    tax_cents: int
    gross_cents: int
    credited_cents: int
    paid_cents: int
    outstanding_cents: int
    po_excess_cents: int
    # Null until a finance lead approves the PO variance.
    variance_approved_by: str | None
    # Derived as of the request date (so it can read OVERDUE), not stored.
    status: str
    applies_to_invoice_id: str | None
    journal_entry_id: str | None
    source_refs: list
    version: int


class RecordInvoiceOut(BaseModel):
    invoice: InvoiceOut
    created: bool
    # Cases the recording opened (a suspected duplicate, a PO excess).
    case_ids: list[str]


class RecordCreditNoteOut(BaseModel):
    credit_note: InvoiceOut
    # The invoice the note was applied to, re-read after the application.
    invoice: InvoiceOut
    created: bool


class PaymentSimulationOut(BaseModel):
    id: str
    amount_cents: int
    state: str
    payment_reference: str
    journal_entry_id: str | None
    # Constant: no real money moves in this system.
    label: str


class PayInvoiceOut(BaseModel):
    payment: PaymentSimulationOut
    created: bool
    invoice: InvoiceOut


class PayableInvoiceOut(BaseModel):
    """An invoice as the payables summary lists it. Note this is NOT
    `InvoiceOut`: the summary keys the id as `invoice_id`, adds the supplier
    name and the cases blocking payment, and reports `variance_approved` as
    a boolean instead of naming the approver."""

    invoice_id: str
    supplier_id: str
    supplier_name: str | None
    kind: str
    invoice_number: str
    invoice_date: str
    due_date: str | None
    gross_cents: int
    credited_cents: int
    paid_cents: int
    outstanding_cents: int
    po_excess_cents: int
    variance_approved: bool
    status: str
    applies_to_invoice_id: str | None
    journal_entry_id: str | None
    blocking_case_ids: list[str]


class PayablesSummaryOut(BaseModel):
    """The payables summary as `payables.payables_summary` builds it.

    This is the shape the FESTIVAL REPORT embeds. It has no `items` /
    `next_cursor`: those are added by GET /invoices below, not by the
    summary, so the two are deliberately different models. Declaring one
    model for both made the festival report fail response validation --
    which is how this difference was found.
    """

    as_of: str
    invoices: list[PayableInvoiceOut]
    total_outstanding_cents: int


class PayablesOut(PayablesSummaryOut):
    """GET /invoices: the summary, plus the collection envelope. `items` is
    the same list as `invoices`, under the name every other collection uses;
    `invoices` stays where the summary's existing callers look for it."""

    items: list[PayableInvoiceOut]
    next_cursor: str | None


# -- Other organizer records + config ---------------------------------------


class RecordCreatedOut(BaseModel):
    """The common reply for the one-shot organizer records (`_record`)."""

    id: str
    created: bool


class RecognizeTicketsOut(BaseModel):
    # Null when there was nothing deferred left to recognise.
    entry_key: str | None
    created: bool


class FinanceConfigOut(BaseModel):
    id: str
    festival_id: str
    version: int
    status: str
    timezone: str
    statement_local_time: str
    payout_local_time: str
    forecast_horizon_days: int
    # The stored tax configuration: rates by category (as decimal STRINGS),
    # exempt categories and who owns the liability per custody. Left open
    # because it is operator-supplied configuration, not a fixed schema.
    tax: dict
    # Constant: this is demo configuration, not an approved tax fact.
    label: str


class DefaultTermsOut(BaseModel):
    """What a vendor falls back to when its own terms leave a field null.
    Read from the domain constants the engines apply, not restated here."""

    commission_bps: int
    festival_minimum_cents: int
    basis: str


class PayoutControlsOut(BaseModel):
    auto_limit_cents_per_vendor_day: int
    # Constant SIMULATED: no real rail is reachable from this system.
    payment_mode: str
    statement_local_time: str | None
    payout_local_time: str | None


class BusinessConfigOut(FinanceConfigOut):
    # C01's fixed USD for v1.
    currency: str
    # The config's own label, repeated so nobody reads these as approved facts.
    draft_notice: str | None
    default_terms: DefaultTermsOut
    payout_controls: PayoutControlsOut


# -- Journal ----------------------------------------------------------------


class PostDayOut(BaseModel):
    # Entry keys this run created, and the ones it found already posted.
    posted: list[str]
    unchanged: list[str]


class JournalLineOut(BaseModel):
    account_code: str
    account_role: str
    debit_cents: int
    credit_cents: int


class JournalEntryOut(BaseModel):
    """A journal entry. `sources` and `corrections` are added only by the
    drill-down (GET /journal/{id}), so they are defaulted and that route
    serialises with `response_model_exclude_unset`."""

    id: str
    entry_key: str
    festival_id: str
    business_date: str | None
    kind: str
    posting_rule_version: str | None
    # The entry this one corrects, when it is a correction.
    correction_of: str | None
    note: str | None
    posted_at: str | None
    posted_by: str | None
    source_refs: list
    source_record_ids: list[str]
    lines: list[JournalLineOut]
    # Drill-down only: the records behind `source_record_ids`, resolved.
    sources: list[dict] = []
    # Drill-down only: entry keys of entries that correct THIS one.
    corrections: list[str] = []


class JournalListOut(BaseModel):
    items: list[JournalEntryOut]
    # This is the one paged collection here, so it can be non-null.
    next_cursor: str | None


class TrialBalanceRowOut(BaseModel):
    account_code: str
    name: str
    category: str
    account_role: str
    debit_cents: int
    credit_cents: int


class TrialBalanceOut(BaseModel):
    rows: list[TrialBalanceRowOut]
    total_debit_cents: int
    total_credit_cents: int
    balanced: bool
    # Null when the balance runs through every posted date.
    through: str | None


# -- Reports ----------------------------------------------------------------


class TaxTotalsOut(BaseModel):
    central_supplied_tax_cents: int
    central_recalculated_tax_cents: int
    # A memo only: the vendor owns this liability, not the organizer.
    vendor_retained_supplied_tax_memo_cents: int


class TaxReportOut(BaseModel):
    """Draft sales tax. `by_business_date` and `mismatch_lines` stay open
    objects: a per-date row only carries the columns that apply to it (a
    night with no vendor-retained sales has no vendor-retained keys at all),
    so a model would either invent them as null or drop them."""

    status: str
    config_label: str | None
    finance_config_id: str
    finance_config_version: int
    totals: TaxTotalsOut
    by_business_date: list[dict]
    # Order lines where the supplied tax disagrees with the recalculation.
    mismatch_lines: list[dict]
    journal_sales_tax_liability_cents: int
    source_totals_match_journal: bool


class ProtectedCashOut(BaseModel):
    """Cash that is in the bank but is not the organizer's to spend."""

    sales_tax_cents: int
    tips_cents: int
    customer_refunds_cents: int


class ReleaseCashPoolOut(BaseModel):
    available_cents: int
    reserved_cents: int
    # True is a warning: the pool claims more than bank minus liabilities.
    exceeds_book_spendable: bool
    note: str


class CashOfficeBlockOut(BaseModel):
    cash_office_cents: int
    deposit_slips: list[SlipSummaryOut]
    deposited_cents: int
    awaiting_bank_credit_cents: int
    open_cases: list[CashCaseOut]
    note: str


class SuspenseBlockOut(BaseModel):
    suspense_cents: int
    items: list[SuspenseItemOut]
    note: str


class UpcomingObligationOut(BaseModel):
    kind: str
    ref_id: str
    due_date: str | None
    amount_cents: int
    within_horizon: bool


class UpcomingPayoutOut(BaseModel):
    payout_id: str
    vendor_id: str
    business_date: str
    state: str
    amount_cents: int
    included_in_vendor_funds_payable: bool


class CashReportOut(BaseModel):
    as_of: str
    bank_cents: int
    pending_cents: int
    protected: ProtectedCashOut
    protected_cents: int
    reserved_payouts_cents: int
    vendor_funds_payable_cents: int
    freely_available_cents: int
    obligations_due_within_horizon_cents: int
    obligations_after_horizon_cents: int
    horizon_end: str
    forecast_horizon_days: int
    upcoming_obligations: list[UpcomingObligationOut]
    upcoming_vendor_payouts: list[UpcomingPayoutOut]
    # Null when this festival has no release cash pool at all.
    release_cash_pool: ReleaseCashPoolOut | None
    cash_office: CashOfficeBlockOut
    suspense: SuspenseBlockOut
    finance_config_version: int


class CashOut(CashReportOut):
    """A cash position is a single as-of snapshot, served ALSO as a one-entry
    collection so a reader of a list gets the shape it expects. `items[0]`
    is this same object; the flat keys stay for the callers that read them."""

    items: list[CashReportOut]
    next_cursor: str | None


class PnlLineOut(BaseModel):
    account_code: str
    name: str
    account_role: str
    amount_cents: int


class PnlReportOut(BaseModel):
    revenue: list[PnlLineOut]
    expenses: list[PnlLineOut]
    total_revenue_cents: int
    total_expenses_cents: int
    net_income_cents: int
    note: str
    through: str | None


class SponsorshipMemoOut(BaseModel):
    sponsor_invoice_id: str
    sponsor_name: str
    invoice_number: str
    amount_cents: int
    received_cents: int
    # A memo: sponsorship receivables are not booked as an asset.
    outstanding_memo_cents: int
    status: str


class FestivalReportOut(BaseModel):
    """Everything the organizer's one-page view needs, composed from the
    reports above rather than recomputed."""

    festival_id: str
    as_of: str
    cash: CashReportOut
    tax: TaxReportOut
    profit_and_loss: PnlReportOut
    payables: PayablesSummaryOut
    # Entries per kind, keyed by whichever kinds were posted, so an open map.
    journal_entry_counts_by_kind: dict[str, int]
    closed_business_dates: list[str]
    cash_office_and_bank_cases: list[CashCaseOut]
    chargeback_fees: list[ChargebackFeeStatusOut]
    sponsorship_receivables_memo: list[SponsorshipMemoOut]
    # The DRAFT/not-filed notices that apply to the figures above.
    labels: list[str]


# -- Vendor statements ------------------------------------------------------


class StatementOut(BaseModel):
    id: str
    vendor_id: str
    business_date: str
    statement_version: int
    status: str
    settlement_id: str | None
    payout_id: str | None
    payout_state: str | None
    finance_config_id: str | None
    due_by: str
    issued_on_time: bool
    issued_at: str | None
    # Set when this statement replaces an earlier version.
    supersedes_statement_id: str | None
    # The rendered statement body (its `_digest` is stripped). Its sections
    # are the statement's own content, so it stays an open object.
    content: dict


class StatementListOut(BaseModel):
    items: list[StatementOut]
    next_cursor: str | None


class IssueStatementOut(BaseModel):
    statement: StatementOut
    # False when an identical statement was already issued.
    created: bool


class IssueDayStatementOut(BaseModel):
    vendor_id: str
    statement_id: str
    status: str
    statement_version: int
    created: bool
    issued_on_time: bool


class IssueDayOut(BaseModel):
    statements: list[IssueDayStatementOut]


# -- Daily close ------------------------------------------------------------


class CloseHardChecksOut(BaseModel):
    """Checks that MUST pass before the day can close."""

    trial_balance_balanced: bool
    every_entry_balanced: bool
    supplier_ap_matches_open_invoices: bool
    tax_source_totals_match_journal: bool
    all_settlements_posted: bool
    bank_entries_accounted_for: bool
    cash_deposits_posted: bool


class CloseWarningsOut(BaseModel):
    """Checks that are reported but do not block the close."""

    statements_issued_for_all_settled_vendors: bool
    cash_office_and_bank_cases_resolved: bool
    suspense_zero: bool
    processor_clearing_zero: bool


class CloseChecksOut(BaseModel):
    hard: CloseHardChecksOut
    warnings: CloseWarningsOut
    trial_balance: TrialBalanceOut
    # The ids behind the failing checks, so a human can go and look.
    unposted_settlement_ids: list[str]
    unaccounted_bank_entry_ids: list[str]
    unposted_cash_deposit_slip_ids: list[str]
    vendors_without_statement: list[str]
    open_cash_and_bank_case_ids: list[str]
    open_case_count: int
    suspense_cents: int
    cash_office_cents: int
    processor_clearing_cents: int


class CloseDayOut(BaseModel):
    id: str
    business_date: str
    created: bool
    # The stored check results, as `close_checks` produced them at the time.
    checks: dict
    journal_entry_count: int
    trial_balance_balanced: bool


class DailyCloseOut(BaseModel):
    id: str
    business_date: str
    closed_by: str | None
    checks: dict
    journal_entry_count: int
    closed_at: str | None


class DailyCloseListOut(BaseModel):
    items: list[DailyCloseOut]
    next_cursor: str | None


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


@router.post("/suppliers", status_code=201, response_model=CreateSupplierOut)
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


@router.get("/suppliers", response_model=SupplierListOut)
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
    # S3: True (default, every PO before this) holds only a disputed
    # remainder's payment; False holds the whole invoice until fully
    # delivered.
    partial_payment_allowed: bool = True


@router.post("/purchase-orders", status_code=201, response_model=CreatePurchaseOrderOut)
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
        "partial_payment_allowed": po.partial_payment_allowed,
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


@router.get("/purchase-orders", response_model=PurchaseOrderListOut)
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
                "partial_payment_allowed": p.partial_payment_allowed,
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


@router.post("/invoices", status_code=201, response_model=RecordInvoiceOut)
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


@router.post(
    "/invoices/{invoice_id}/credit-notes", status_code=201, response_model=RecordCreditNoteOut
)
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


@router.post("/invoices/{invoice_id}/approve-variance", response_model=InvoiceOut)
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


@router.post("/invoices/{invoice_id}/pay", response_model=PayInvoiceOut)
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
    # S1: who physically accepted the delivery, when known/distinct from
    # the authenticated API caller (`user.id` below). None (unset) if not
    # given -- never guessed.
    accepted_by: str | None = None


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
            accepted_by=body.accepted_by,
        )
    except _DOMAIN_ERRORS as exc:
        raise _err(exc) from exc
    db.commit()
    return {
        "id": receipt.id,
        "po_line_id": receipt.po_line_id,
        "quantity": receipt.quantity,
        "receipt_key": receipt.receipt_key,
        "accepted_by": receipt.accepted_by,
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
_FIXTURE_DATA_DIR = _FIXTURE_DIR / "data"


def _load_fixture_json(name: str):
    """Reads ONLY fixtures/encore-procurement-synthetic-v1/data/<name>.json
    -- never evaluation/ (TICKET: supplier comparison "must be unable to
    open evaluation/"). Resolves the final path and verifies it is still
    inside `_FIXTURE_DATA_DIR` before opening, so a `name` containing
    path-traversal segments (e.g. "../evaluation/answer_key") is refused
    rather than silently escaping the data/ directory."""
    path = (_FIXTURE_DATA_DIR / f"{name}.json").resolve()
    if _FIXTURE_DATA_DIR.resolve() not in path.parents:
        raise ValueError(f"fixture name {name!r} escapes the data/ directory")
    with open(path) as f:
        return json.load(f)


@router.get("/supplier-comparison")
def supplier_comparison(
    request_key: str,
    as_of: datetime,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """TICKET section 7. Quote amounts come from the imported `supplier_quotes`
    rows (demo-clock-gated by `available_at <= as_of`); the cash-impact
    baseline and supplier history are read directly from the fixture's
    cash_inputs.json/historical_contracts.json -- a labelled synthetic
    comparison, never written to cash_pools/opening_balances/the journal.
    Selection stays undecided here: no winner field, no aggregate score.

    `as_of` is a full timezone-aware instant (not a bare date): a quote
    available at 09:00 on its release day must not leak into a comparison
    made at, say, 08:00 that same day."""
    import domain.procurement_cash as cash_rules

    from app.models_finance import SupplierQuote

    cutoff = as_of
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
        # historical_contracts.json/historical_deliveries.json key by the
        # FIXTURE's own supplier id ("sup_a"), but an imported SupplierQuote
        # row's supplier_id is the real DB-generated Supplier.id -- there is
        # no column linking the two back. Name is the only stable bridge the
        # importer already establishes 1:1 (import_procurement_fixture.py
        # creates exactly one Supplier per counterparties.json entry, named
        # verbatim from there), so resolve fixture id by name, not by id.
        fixture_id_by_name = {c["name"]: c["id"] for c in _load_fixture_json("counterparties")}
    except FileNotFoundError:
        cash, hist_contracts, hist_deliveries, fixture_id_by_name = None, {}, {}, {}

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

        supplier = db.get(Supplier, q.supplier_id)
        fixture_supplier_id = fixture_id_by_name.get(supplier.name) if supplier else None
        h = hist_contracts.get(fixture_supplier_id)
        history_label = "no_history"
        variance_bps = None
        otif = None
        approved_scope_changes_cents = None
        variance_vs_revised_budget_cents = None
        if h is not None:
            history_label = "has_history"
            variance_cents = h["final_net_cost_cents"] - h["original_budget_cents"]
            variance_bps = round(variance_cents / h["original_budget_cents"] * 10000)
            # TICKET section 7: approved scope changes are not supplier
            # fault -- break the variance into the approved portion and
            # whatever is left over against a budget already adjusted for
            # those approved changes, so a reviewer never reads a change
            # order as a cost overrun.
            approved_scope_changes_cents = h.get("approved_scope_changes_cents", 0)
            revised_budget_cents = h["original_budget_cents"] + approved_scope_changes_cents
            variance_vs_revised_budget_cents = h["final_net_cost_cents"] - revised_budget_cents
            d = hist_deliveries.get(fixture_supplier_id)
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
                "history_approved_scope_changes_cents": approved_scope_changes_cents,
                "history_variance_vs_revised_budget_cents": variance_vs_revised_budget_cents,
                "history_otif": otif,
                # A single prior contract is too small a sample to rank on,
                # and an approved scope change is not the supplier's fault --
                # TICKET section 7's own caveat, stated here so the UI always
                # carries it rather than needing to reconstruct it.
                "history_caveat": (
                    "Based on a single prior contract, not a reliable sample. Approved scope "
                    "changes are not the supplier's fault."
                )
                if history_label == "has_history"
                else None,
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


@router.get("/invoices", response_model=PayablesOut)
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


@router.get("/invoices/{invoice_id}", response_model=InvoiceOut)
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


@router.post("/finance/opening-balances", status_code=201, response_model=RecordCreatedOut)
def opening_balance(
    body: OpeningIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CLOSE_BOOKS)),
) -> dict:
    return _record(db, user, records.record_opening_balance, body)


@router.post("/finance/ticket-settlements", status_code=201, response_model=RecordCreatedOut)
def ticket_settlement(
    body: TicketIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CLOSE_BOOKS)),
) -> dict:
    return _record(db, user, records.record_ticket_settlement, body)


@router.post("/finance/tickets/recognize", response_model=RecognizeTicketsOut)
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


@router.post("/finance/sponsor-invoices", status_code=201, response_model=RecordCreatedOut)
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


@router.post("/finance/sponsorship-receipts", status_code=201, response_model=RecordCreatedOut)
def sponsorship_receipt(
    body: SponsorReceiptIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CLOSE_BOOKS)),
) -> dict:
    return _record(db, user, records.record_sponsorship_receipt, body)


@router.post("/finance/expenses", status_code=201, response_model=RecordCreatedOut)
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


@router.get("/business-config", response_model=BusinessConfigOut)
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


@router.get("/finance/config", response_model=FinanceConfigOut)
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


@router.put("/finance/config", response_model=FinanceConfigOut)
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


@router.post("/journal/post-day", response_model=PostDayOut)
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


@router.get("/journal", response_model=JournalListOut, response_model_exclude_unset=True)
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


@router.get("/journal/trial-balance", response_model=TrialBalanceOut)
def get_trial_balance(
    festival_id: str,
    through: date | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    return reports.trial_balance(db, festival_id=festival_id, through=through)


@router.get(
    "/journal/{entry_id}", response_model=JournalEntryOut, response_model_exclude_unset=True
)
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


@router.get("/reports/tax", response_model=TaxReportOut)
def get_tax(
    festival_id: str,
    through: date | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    out = reports.tax_summary(db, festival_id=festival_id, through=through)
    db.commit()  # first use may create the default config version
    return out


@router.get("/cash", response_model=CashOut)
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


@router.get("/reports/pnl", response_model=PnlReportOut)
def get_pnl(
    festival_id: str,
    through: date | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    return reports.profit_and_loss(db, festival_id=festival_id, through=through)


@router.get("/reports/festival", response_model=FestivalReportOut)
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


@router.post("/statements", response_model=IssueStatementOut)
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


@router.post("/statements/issue-day", response_model=IssueDayOut)
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


@router.get("/statements", response_model=StatementListOut)
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


@router.get("/close/checks", response_model=CloseChecksOut)
def get_close_checks(
    festival_id: str,
    business_date: date,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    out = reports.close_checks(db, festival_id=festival_id, business_date=business_date)
    db.commit()
    return out


@router.post("/close", response_model=CloseDayOut)
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


@router.get("/close", response_model=DailyCloseListOut)
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
