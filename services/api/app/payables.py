"""A08 payables: purchase orders, supplier invoices/credit notes, duplicate
detection, PO-variance approval and approved simulated supplier payments.

Every command takes the caller's session, row-locks what it decides on,
appends an audit event, posts its journal entry through
app/accounting.py's `post_cumulative`, and never commits (the router
commits once per request), same convention as app/payout_dispatch.py.

Paid invoices cannot be paid twice, by three independent guards:
  1. the invoice row is locked FOR UPDATE and its outstanding balance
     re-checked (domain.payables.check_payment) before any payment;
  2. the idempotency key is checked under that same lock and is UNIQUE in
     `supplier_payments`, so a retried request returns the original payment;
  3. the payment draws from the same row-locked `cash_pools` row A07's
     vendor releases reserve from, so supplier payments and vendor payouts
     cannot jointly spend more than the cash a finance lead made available.

Duplicate detection:
  - same supplier + invoice number + same source document = an API retry:
    the original invoice is returned, nothing new is recorded;
  - same supplier + invoice number from a DIFFERENT source document = a
    duplicate: a DUPLICATE_SUPPLIER_INVOICE case is opened, nothing is
    posted, nothing can be paid (`DuplicateInvoiceError`, 409);
  - different number but same supplier, gross and invoice date = a possible
    duplicate: the invoice is recorded and its liability posted, but a
    blocking POSSIBLE_DUPLICATE_SUPPLIER_INVOICE case stops payment until a
    human resolves it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from domain import accounting as acct
from domain import payables as rules
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounting import post_cumulative
from app.models import (
    CAPABILITY_FINANCE_LEAD,
    CAPABILITY_MANAGE_PAYABLES,
    CAPABILITY_RESOLVE_CASE,
    _new_id,
)
from app.models_finance import (
    DeliveryReceipt,
    PurchaseOrder,
    PurchaseOrderLine,
    SupplierDeposit,
    SupplierDepositApplication,
    SupplierPayment,
)
from app.models_ledger import Case, Supplier, SupplierInvoice
from app.models_payout_dispatch import CashPool
from app.uow import UnitOfWork

# Non-blocking: DELIVERY_SHORTFALL and SLA_POTENTIAL_CREDIT are visible but
# never stop payment of the delivery-supported/undisputed amount -- only
# the existing duplicate/PO-variance reasons do that.
BLOCKING_CASE_REASONS = ("POSSIBLE_DUPLICATE_SUPPLIER_INVOICE", "INVOICE_EXCEEDS_PO")
NON_BLOCKING_INVOICE_CASE_REASONS = ("DELIVERY_SHORTFALL", "SLA_POTENTIAL_CREDIT")


class PayablesNotFoundError(LookupError):
    pass


class PayablesConflictError(ValueError):
    def __init__(self, code: str, details: dict | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = details or {}


class DuplicateInvoiceError(PayablesConflictError):
    pass


class PaymentRefusedError(ValueError):
    def __init__(self, reason_code: str, details: dict | None = None) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.details = details or {}


def _event(db, *, stream, event_type, aggregate_id, version, actor_id, actor_capability, payload):
    UnitOfWork(db).append_event(
        stream_id=stream,
        event_type=event_type,
        aggregate_id=aggregate_id,
        aggregate_version=version,
        actor_id=actor_id,
        actor_type="human" if actor_id else "system",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        payload=payload,
        lineage_refs=(
            [{"ref_kind": "supplier_invoice", "ref_id": aggregate_id}]
            if stream.startswith("supplier_invoice:")
            else None
        ),
    )


def _invoice_ref(invoice_id: str, role: str = "related") -> dict:
    return {"ref_kind": "supplier_invoice", "ref_id": invoice_id, "role": role}


def _open_case(
    db,
    *,
    reason_code,
    invoice: SupplierInvoice,
    evidence,
    impact,
    proposed,
    cap,
    blocking: bool = True,
):
    case = Case(
        vendor_id=None,
        reason_code=reason_code,
        evidence_refs=[_invoice_ref(invoice.id, "subject"), *evidence],
        financial_impact_cents=impact,
        proposed_resolution=proposed,
        required_capability=cap,
        # X0.1: generalized subject -- this case is about one supplier
        # invoice (see packages/domain/domain/case_routing.py). Pure
        # additive tagging; the A08 duplicate/variance workflow below is
        # unchanged.
        subject_type="supplier_invoice",
        subject_id=invoice.id,
        status="OPEN",
        blocking=blocking,
    )
    db.add(case)
    db.flush()
    return case


def _blocking_cases(db: Session, invoice_id: str) -> list[Case]:
    return list(
        db.execute(
            select(Case).where(
                Case.status == "OPEN",
                Case.reason_code.in_(BLOCKING_CASE_REASONS),
                # Only cases ABOUT this invoice block it; an invoice merely
                # cited as evidence (e.g. the original of a suspected
                # duplicate) stays payable.
                Case.evidence_refs.contains([_invoice_ref(invoice_id, "subject")]),
            )
        )
        .scalars()
        .all()
    )


def _balance(inv: SupplierInvoice) -> rules.InvoiceBalance:
    return rules.InvoiceBalance(
        inv.gross_cents, inv.credited_cents, inv.paid_cents, inv.deposit_applied_cents
    )


def _refresh_status(inv: SupplierInvoice, *, as_of: date | None = None) -> None:
    if inv.kind == "CREDIT_NOTE":
        inv.status = "CREDIT_NOTE"
        return
    inv.outstanding_cents = _balance(inv).outstanding_cents
    status = rules.invoice_status(
        _balance(inv),
        po_excess_cents=inv.po_excess_cents,
        variance_approved=inv.variance_approved_at is not None,
        due_date=None,  # OVERDUE is an as-of view, computed at read time
        as_of=as_of or date.min,
    )
    inv.status = status


def display_status(inv: SupplierInvoice, as_of: date) -> str:
    if inv.kind == "CREDIT_NOTE":
        return "CREDIT_NOTE"
    return rules.invoice_status(
        _balance(inv),
        po_excess_cents=inv.po_excess_cents,
        variance_approved=inv.variance_approved_at is not None,
        due_date=inv.due_date,
        as_of=as_of,
    )


# ---------------------------------------------------------------------------
# Suppliers / purchase orders
# ---------------------------------------------------------------------------


def get_or_create_supplier(
    db: Session, *, festival_id: str, name: str, category: str | None
) -> tuple[Supplier, bool]:
    existing = db.execute(
        select(Supplier).where(Supplier.festival_id == festival_id, Supplier.name == name)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    sup = Supplier(festival_id=festival_id, name=name, category=category, status="active")
    db.add(sup)
    db.flush()
    return sup, True


def create_purchase_order(
    db: Session,
    *,
    supplier_id: str,
    po_number: str,
    po_date: date,
    amount_cents: int,
    description: str | None,
    receipt_evidence_refs: list,
    source_refs: list,
    actor_id: str,
    actor_capability: str,
    lines: list[dict] | None = None,
) -> tuple[PurchaseOrder, bool]:
    """`lines` is optional: a PO created without it (every PO before this
    feature) behaves exactly as before -- no delivery-supported payment
    limit applies to its invoices (domain.payables.check_payment's
    `delivery_supported_cents` stays None). Each line dict:
    {"line_no", "item_code", "quantity", "unit_price_cents"}; their
    `quantity * unit_price_cents` must sum to `amount_cents`."""
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise PayablesNotFoundError(f"supplier {supplier_id} not found")
    if amount_cents <= 0:
        raise PayablesConflictError("po_amount_must_be_positive")
    if lines is not None:
        total = sum(line["quantity"] * line["unit_price_cents"] for line in lines)
        if total != amount_cents:
            raise PayablesConflictError(
                "po_lines_do_not_sum_to_amount",
                {"lines_total_cents": total, "amount_cents": amount_cents},
            )
    existing = db.execute(
        select(PurchaseOrder).where(
            PurchaseOrder.supplier_id == supplier_id, PurchaseOrder.po_number == po_number
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.amount_cents != amount_cents or existing.po_date != po_date:
            raise PayablesConflictError(
                "po_number_exists_with_different_terms",
                {"po_id": existing.id, "amount_cents": existing.amount_cents},
            )
        return existing, False
    po = PurchaseOrder(
        festival_id=supplier.festival_id,
        supplier_id=supplier_id,
        po_number=po_number,
        po_date=po_date,
        amount_cents=amount_cents,
        description=description,
        receipt_evidence_refs=receipt_evidence_refs,
        source_refs=source_refs,
    )
    db.add(po)
    db.flush()
    if lines is not None:
        for line in lines:
            db.add(
                PurchaseOrderLine(
                    po_id=po.id,
                    line_no=line["line_no"],
                    item_code=line["item_code"],
                    quantity=line["quantity"],
                    unit_price_cents=line["unit_price_cents"],
                    line_cents=line["quantity"] * line["unit_price_cents"],
                )
            )
        db.flush()
    _event(
        db,
        stream=f"purchase_order:{po.id}",
        event_type="purchase_order.created",
        aggregate_id=po.id,
        version=1,
        actor_id=actor_id,
        actor_capability=actor_capability,
        payload={
            "po_number": po_number,
            "amount_cents": amount_cents,
            "has_lines": lines is not None,
        },
    )
    return po, True


def _po_lines(db: Session, po_id: str) -> list[PurchaseOrderLine]:
    return list(
        db.execute(
            select(PurchaseOrderLine)
            .where(PurchaseOrderLine.po_id == po_id)
            .order_by(PurchaseOrderLine.line_no)
        )
        .scalars()
        .all()
    )


def _delivered_qty(db: Session, po_line_id: str) -> int:
    return int(
        db.execute(
            select(func.coalesce(func.sum(DeliveryReceipt.quantity), 0)).where(
                DeliveryReceipt.po_line_id == po_line_id
            )
        ).scalar_one()
    )


def delivery_supported_cents_for_po(db: Session, po_id: str) -> int | None:
    """None when the PO has no lines (no delivery-based limit); otherwise
    the total value of everything accepted so far across all its lines.
    Reads `delivery_receipts` WITHOUT locking the PO -- the table is
    append-only, so reading it unlocked can only UNDERSTATE the current
    total (a concurrent insert not yet visible), never overstate it, which
    is the safe direction for a payment limit."""
    lines = _po_lines(db, po_id)
    if not lines:
        return None
    total = 0
    for line in lines:
        delivered = min(_delivered_qty(db, line.id), line.quantity)
        total += delivered * line.unit_price_cents
    return total


def _po_invoiced_cents(db: Session, po_id: str, *, excluding: str | None = None) -> int:
    q = select(
        func.coalesce(func.sum(SupplierInvoice.gross_cents - SupplierInvoice.credited_cents), 0)
    ).where(
        SupplierInvoice.kind == "INVOICE",
        SupplierInvoice.po_refs.contains([po_id]),
    )
    if excluding is not None:
        q = q.where(SupplierInvoice.id != excluding)
    return int(db.execute(q).scalar_one())


def _recompute_po(db: Session, po: PurchaseOrder, *, actor_id: str | None) -> None:
    """Re-attributes PO excess across ALL invoices on the PO, in invoice
    date order (net of credit notes). Each invoice's excess is the part of
    it that lies beyond the PO amount. Called with the PO row locked. An
    invoice whose excess falls to zero (e.g. after a credit note on an
    earlier invoice) has its INVOICE_EXCEEDS_PO case resolved as no longer
    applicable -- never silently: the resolution is recorded on the case."""
    invoices = (
        db.execute(
            select(SupplierInvoice)
            .where(SupplierInvoice.kind == "INVOICE", SupplierInvoice.po_refs.contains([po.id]))
            .order_by(SupplierInvoice.invoice_date, SupplierInvoice.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )
    running = 0
    for inv in invoices:
        own = inv.gross_cents - inv.credited_cents
        running += own
        excess = min(rules.po_excess(po.amount_cents, running), max(own, 0))
        if excess != inv.po_excess_cents:
            inv.po_excess_cents = excess
            inv.version += 1
            if excess == 0:
                for case in _blocking_cases(db, inv.id):
                    if case.reason_code == "INVOICE_EXCEEDS_PO":
                        case.status = "RESOLVED"
                        case.auto_resolved = True
                        case.resolution_decision = "NO_LONGER_EXCEEDS_PO"
                        case.resolution_note = "PO total back within amount after a credit note."
                        case.resolved_at = datetime.now(UTC)
        _refresh_status(inv)


def _non_blocking_invoice_cases(db: Session, invoice_id: str, reason_code: str) -> list[Case]:
    return list(
        db.execute(
            select(Case).where(
                Case.status == "OPEN",
                Case.reason_code == reason_code,
                Case.evidence_refs.contains([_invoice_ref(invoice_id, "subject")]),
            )
        )
        .scalars()
        .all()
    )


def _recompute_delivery_shortfall(db: Session, po: PurchaseOrder, *, actor_id: str | None) -> None:
    """DELIVERY_SHORTFALL is non-blocking (NON_BLOCKING_INVOICE_CASE_REASONS):
    it is visible on the invoice but never stops `pay_invoice` -- only the
    domain-level delivery-supported check in `check_payment` limits the
    amount. One OPEN case per invoice; when the shortfall reaches 0 it is
    auto-resolved with a recorded note, same pattern as INVOICE_EXCEEDS_PO
    above."""
    supported = delivery_supported_cents_for_po(db, po.id)
    if supported is None:
        return
    invoices = (
        db.execute(
            select(SupplierInvoice).where(
                SupplierInvoice.kind == "INVOICE", SupplierInvoice.po_refs.contains([po.id])
            )
        )
        .scalars()
        .all()
    )
    for inv in invoices:
        shortfall = max(inv.gross_cents - supported, 0)
        open_cases = _non_blocking_invoice_cases(db, inv.id, "DELIVERY_SHORTFALL")
        if shortfall > 0:
            if not open_cases:
                _open_case(
                    db,
                    reason_code="DELIVERY_SHORTFALL",
                    invoice=inv,
                    evidence=[],
                    impact=shortfall,
                    proposed=(
                        "Accepted delivery evidence does not yet support the full invoiced "
                        "value. Non-blocking: the undisputed, delivery-supported portion stays "
                        "payable. Visible until resolved by more evidence."
                    ),
                    cap=CAPABILITY_MANAGE_PAYABLES,
                    blocking=False,
                )
            else:
                for case in open_cases:
                    if case.financial_impact_cents != shortfall:
                        case.financial_impact_cents = shortfall
        else:
            for case in open_cases:
                case.status = "RESOLVED"
                case.auto_resolved = True
                case.resolution_decision = "DELIVERY_NOW_SUPPORTS_FULL_INVOICE"
                case.resolution_note = "Accepted delivery evidence now covers the full invoice."
                case.resolved_at = datetime.now(UTC)


def record_delivery_receipt(
    db: Session,
    *,
    po_line_id: str,
    quantity: int,
    accepted_at: datetime,
    available_at: datetime,
    receipt_key: str,
    source_refs: list,
    actor_id: str,
    actor_capability: str,
) -> tuple[DeliveryReceipt, bool]:
    """Idempotent on `receipt_key` (unique). Refuses (`over_receipt`) if
    recording this receipt would make the line's total received exceed its
    ordered quantity. Locks the PO row (via its line's `po_id`) for the
    duration of the over-receipt check and the shortfall recompute that
    follows -- not held by `pay_invoice`, which never locks the PO."""
    existing = db.execute(
        select(DeliveryReceipt).where(DeliveryReceipt.receipt_key == receipt_key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    if quantity <= 0:
        raise PayablesConflictError("quantity_must_be_positive", {"quantity": quantity})

    line = db.get(PurchaseOrderLine, po_line_id)
    if line is None:
        raise PayablesNotFoundError(f"purchase order line {po_line_id} not found")
    po = db.execute(
        select(PurchaseOrder).where(PurchaseOrder.id == line.po_id).with_for_update()
    ).scalar_one()

    already = _delivered_qty(db, po_line_id)
    if already + quantity > line.quantity:
        raise PayablesConflictError(
            "over_receipt",
            {
                "line_id": line.id,
                "ordered_quantity": line.quantity,
                "already_received": already,
                "attempted": quantity,
            },
        )

    receipt = DeliveryReceipt(
        po_line_id=po_line_id,
        quantity=quantity,
        accepted_at=accepted_at,
        available_at=available_at,
        receipt_key=receipt_key,
        source_refs=source_refs,
    )
    db.add(receipt)
    db.flush()
    _event(
        db,
        stream=f"purchase_order:{po.id}",
        event_type="delivery_receipt.recorded",
        aggregate_id=receipt.id,
        version=1,
        actor_id=actor_id,
        actor_capability=actor_capability,
        payload={"po_line_id": po_line_id, "quantity": quantity, "receipt_key": receipt_key},
    )
    _recompute_delivery_shortfall(db, po, actor_id=actor_id)
    return receipt, True


# ---------------------------------------------------------------------------
# Supplier deposits
# ---------------------------------------------------------------------------


def pay_supplier_deposit(
    db: Session,
    *,
    supplier_id: str,
    po_id: str | None,
    amount_cents: int,
    paid_business_date: date,
    paid_at: datetime,
    idempotency_key: str,
    source_refs: list,
    actor_id: str,
    actor_capability: str,
) -> tuple[SupplierDeposit, bool]:
    """Draws `amount_cents` from the cash pool exactly once -- the unique
    `idempotency_key` means a retried request returns the original deposit
    untouched, same three-guard discipline `pay_invoice` already uses."""
    prior = db.execute(
        select(SupplierDeposit).where(SupplierDeposit.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if prior is not None:
        return prior, False
    if amount_cents <= 0:
        raise PayablesConflictError("amount_must_be_positive", {"amount_cents": amount_cents})

    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise PayablesNotFoundError(f"supplier {supplier_id} not found")

    pool = db.execute(
        select(CashPool).where(CashPool.festival_id == supplier.festival_id).with_for_update()
    ).scalar_one_or_none()
    if pool is None or pool.available_cents < amount_cents:
        raise PaymentRefusedError(
            "insufficient_available_cash",
            {"available_cents": None if pool is None else pool.available_cents},
        )
    pool.available_cents -= amount_cents
    pool.version += 1

    deposit_id = _new_id()
    result = post_cumulative(
        db,
        festival_id=supplier.festival_id,
        base_key=f"SUPPLIER_DEPOSIT:{deposit_id}",
        business_date=paid_business_date,
        kind="SUPPLIER_DEPOSIT",
        lines=acct.supplier_deposit_paid(amount_cents),
        source_refs=source_refs,
        source_record_ids=[deposit_id, supplier_id] + ([po_id] if po_id else []),
        note="Simulated supplier deposit. No real money moved.",
        actor_id=actor_id,
        actor_capability=actor_capability,
    )
    deposit = SupplierDeposit(
        id=deposit_id,
        festival_id=supplier.festival_id,
        supplier_id=supplier_id,
        po_id=po_id,
        amount_cents=amount_cents,
        paid_business_date=paid_business_date,
        paid_at=paid_at,
        idempotency_key=idempotency_key,
        source_refs=source_refs,
        journal_entry_id=result.entry.id if result.entry else None,
    )
    db.add(deposit)
    db.flush()
    _event(
        db,
        stream=f"supplier_deposit:{deposit.id}",
        event_type="supplier_deposit.paid",
        aggregate_id=deposit.id,
        version=1,
        actor_id=actor_id,
        actor_capability=actor_capability,
        payload={"amount_cents": amount_cents, "supplier_id": supplier_id},
    )
    return deposit, True


def apply_deposit(
    db: Session,
    *,
    deposit_id: str,
    invoice_id: str,
    amount_cents: int,
    actor_id: str,
    actor_capability: str,
) -> tuple[SupplierDepositApplication, bool]:
    """Locks the invoice, then the deposit (same lock order as every other
    two-row command here: invoice first). No cash-pool access -- applying a
    deposit never moves cash, only reclassifies it (domain.accounting.
    supplier_deposit_applied: Dr 2300 / Cr 1300). UNIQUE(deposit_id,
    invoice_id) makes a retried application a no-op at the database level."""
    existing = db.execute(
        select(SupplierDepositApplication).where(
            SupplierDepositApplication.deposit_id == deposit_id,
            SupplierDepositApplication.invoice_id == invoice_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    if amount_cents <= 0:
        raise PayablesConflictError("amount_must_be_positive", {"amount_cents": amount_cents})

    inv = _lock_invoice(db, invoice_id)
    deposit = db.execute(
        select(SupplierDeposit).where(SupplierDeposit.id == deposit_id).with_for_update()
    ).scalar_one_or_none()
    if deposit is None:
        raise PayablesNotFoundError(f"supplier deposit {deposit_id} not found")

    already_applied = int(
        db.execute(
            select(func.coalesce(func.sum(SupplierDepositApplication.amount_cents), 0)).where(
                SupplierDepositApplication.deposit_id == deposit_id
            )
        ).scalar_one()
    )
    unapplied = deposit.amount_cents - already_applied
    if amount_cents > unapplied:
        raise PayablesConflictError(
            "amount_exceeds_unapplied_deposit",
            {"amount_cents": amount_cents, "unapplied_cents": unapplied},
        )
    if amount_cents > _balance(inv).outstanding_cents:
        raise PayablesConflictError(
            "amount_exceeds_outstanding",
            {"amount_cents": amount_cents, "outstanding_cents": _balance(inv).outstanding_cents},
        )

    result = post_cumulative(
        db,
        festival_id=inv.festival_id,
        base_key=f"DEPOSIT_APPLICATION:{deposit_id}:{invoice_id}",
        business_date=inv.invoice_date,
        kind="SUPPLIER_DEPOSIT_APPLICATION",
        lines=acct.supplier_deposit_applied(amount_cents),
        source_record_ids=[deposit_id, invoice_id],
        note="Deposit applied against invoice; no cash movement.",
        actor_id=actor_id,
        actor_capability=actor_capability,
    )
    application = SupplierDepositApplication(
        deposit_id=deposit_id,
        invoice_id=invoice_id,
        amount_cents=amount_cents,
        journal_entry_id=result.entry.id if result.entry else None,
    )
    db.add(application)
    db.flush()
    inv.deposit_applied_cents += amount_cents
    inv.version += 1
    _refresh_status(inv)
    _event(
        db,
        stream=f"supplier_invoice:{inv.id}",
        event_type="supplier_invoice.deposit_applied",
        aggregate_id=inv.id,
        version=inv.version,
        actor_id=actor_id,
        actor_capability=actor_capability,
        payload={"deposit_id": deposit_id, "amount_cents": amount_cents},
    )
    return application, True


# ---------------------------------------------------------------------------
# SLA potential credit (opened through the A08 _open_case path, not the A05
# contract-term detector, so no approved policy can auto-resolve it)
# ---------------------------------------------------------------------------


def open_sla_potential_credit_case(
    db: Session,
    *,
    invoice_id: str,
    potential_credit_cents: int,
    clause: str,
    evidence_refs: list,
    note: str,
    actor_id: str,
    actor_capability: str,
) -> Case | None:
    """Non-blocking, financial_impact_cents 0 -- the potential amount is
    stated in `note` (proposed_resolution), not as the case's financial
    impact, because a potential credit is not yet an approved monetary
    effect (AGENT_HANDOFF.md: no automatic deduction). Idempotent: returns
    None if an OPEN SLA_POTENTIAL_CREDIT case already exists for this
    invoice, rather than opening a second one."""
    inv = db.get(SupplierInvoice, invoice_id)
    if inv is None:
        raise PayablesNotFoundError(f"supplier invoice {invoice_id} not found")
    if _non_blocking_invoice_cases(db, invoice_id, "SLA_POTENTIAL_CREDIT"):
        return None
    case = _open_case(
        db,
        reason_code="SLA_POTENTIAL_CREDIT",
        invoice=inv,
        evidence=evidence_refs,
        impact=0,
        proposed=note,
        cap=CAPABILITY_MANAGE_PAYABLES,
        blocking=False,
    )
    _event(
        db,
        stream=f"supplier_invoice:{inv.id}",
        event_type="supplier_invoice.sla_potential_credit_opened",
        aggregate_id=inv.id,
        version=inv.version,
        actor_id=actor_id,
        actor_capability=actor_capability,
        payload={
            "case_id": case.id,
            "potential_credit_cents": potential_credit_cents,
            "clause": clause,
        },
    )
    return case


# ---------------------------------------------------------------------------
# Invoices / credit notes
# ---------------------------------------------------------------------------


def record_invoice(
    db: Session,
    *,
    supplier_id: str,
    po_id: str | None,
    invoice_number: str,
    invoice_date: date,
    due_date: date | None,
    net_cents: int,
    tax_cents: int,
    gross_cents: int,
    received_at: datetime | None,
    source_refs: list,
    actor_id: str,
    actor_capability: str,
) -> tuple[SupplierInvoice, bool, list[Case]]:
    """Returns (invoice, created, cases_opened). See module docstring for
    the duplicate rules."""
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise PayablesNotFoundError(f"supplier {supplier_id} not found")
    if gross_cents != net_cents + tax_cents or gross_cents <= 0:
        raise PayablesConflictError(
            "invoice_amounts_inconsistent",
            {"net_cents": net_cents, "tax_cents": tax_cents, "gross_cents": gross_cents},
        )
    po = None
    if po_id is not None:
        po = db.execute(
            select(PurchaseOrder).where(PurchaseOrder.id == po_id).with_for_update()
        ).scalar_one_or_none()
        if po is None or po.supplier_id != supplier_id:
            raise PayablesNotFoundError(f"purchase order {po_id} not found for this supplier")

    # Normalized (case/whitespace-insensitive) match, per domain.payables.
    # normalize_invoice_number -- " b-001 " and "B-001" must collide here.
    # A normalized match that ISN'T also an exact raw/source_refs/gross
    # match is a real duplicate (below), not a harmless resend: without
    # this, a resubmission under different casing/whitespace would land in
    # the harmless-resend branch and open no case.
    normalized = rules.normalize_invoice_number(invoice_number)
    existing = db.execute(
        select(SupplierInvoice).where(
            SupplierInvoice.supplier_id == supplier_id,
            func.lower(func.btrim(SupplierInvoice.invoice_number, " \t\r\n")) == normalized,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.invoice_number == invoice_number
            and existing.source_refs == source_refs
            and existing.gross_cents == gross_cents
        ):
            return existing, False, []
        case = _open_case(
            db,
            reason_code="DUPLICATE_SUPPLIER_INVOICE",
            invoice=existing,
            evidence=list(source_refs) + list(existing.source_refs),
            impact=gross_cents,
            proposed=(
                "Same supplier and invoice number already received from a different source "
                "document: not posted and not payable. A human confirms rejection."
            ),
            cap=CAPABILITY_RESOLVE_CASE,
        )
        _event(
            db,
            stream=f"supplier_invoice:{existing.id}",
            event_type="supplier_invoice.duplicate_rejected",
            aggregate_id=existing.id,
            version=existing.version,
            actor_id=actor_id,
            actor_capability=actor_capability,
            payload={"invoice_number": invoice_number, "case_id": case.id},
        )
        db.commit()  # the case and its audit event are kept; nothing else changed
        raise DuplicateInvoiceError(
            "duplicate_supplier_invoice", {"existing_invoice_id": existing.id, "case_id": case.id}
        )

    inv = SupplierInvoice(
        festival_id=supplier.festival_id,
        supplier_id=supplier_id,
        po_refs=[po.id] if po else [],
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        due_date=due_date,
        net_cents=net_cents,
        tax_cents=tax_cents,
        gross_cents=gross_cents,
        outstanding_cents=gross_cents,
        received_at=received_at,
        source_refs=source_refs,
        kind="INVOICE",
        status="OPEN",
    )
    db.add(inv)
    db.flush()

    cases: list[Case] = []
    near = db.execute(
        select(SupplierInvoice.id).where(
            SupplierInvoice.supplier_id == supplier_id,
            SupplierInvoice.kind == "INVOICE",
            SupplierInvoice.id != inv.id,
            SupplierInvoice.gross_cents == gross_cents,
            SupplierInvoice.invoice_date == invoice_date,
        )
    ).first()
    if near is not None:
        cases.append(
            _open_case(
                db,
                reason_code="POSSIBLE_DUPLICATE_SUPPLIER_INVOICE",
                invoice=inv,
                evidence=[_invoice_ref(near[0])],
                impact=gross_cents,
                proposed=(
                    "Different invoice number but same supplier, amount and date as another "
                    "invoice: liability posted, payment blocked until a human confirms."
                ),
                cap=CAPABILITY_RESOLVE_CASE,
            )
        )

    if po is not None:
        _recompute_po(db, po, actor_id=actor_id)
        if inv.po_excess_cents > 0:
            cases.append(
                _open_case(
                    db,
                    reason_code="INVOICE_EXCEEDS_PO",
                    invoice=inv,
                    evidence=list(po.source_refs),
                    impact=inv.po_excess_cents,
                    proposed=(
                        "Invoice total exceeds the remaining PO amount: liability posted, "
                        "payment blocked until a finance lead approves the variance."
                    ),
                    cap=CAPABILITY_FINANCE_LEAD,
                )
            )
    _refresh_status(inv)

    result = post_cumulative(
        db,
        festival_id=supplier.festival_id,
        base_key=f"SUPPLIER_INVOICE:{inv.id}",
        business_date=invoice_date,
        kind="SUPPLIER_INVOICE",
        lines=acct.supplier_invoice(gross_cents),
        source_refs=source_refs,
        source_record_ids=[inv.id] + ([po.id] if po else []),
        actor_id=actor_id,
        actor_capability=actor_capability,
    )
    inv.journal_entry_id = result.entry.id if result.entry else None
    _event(
        db,
        stream=f"supplier_invoice:{inv.id}",
        event_type="supplier_invoice.recorded",
        aggregate_id=inv.id,
        version=inv.version,
        actor_id=actor_id,
        actor_capability=actor_capability,
        payload={
            "invoice_number": invoice_number,
            "gross_cents": gross_cents,
            "po_excess_cents": inv.po_excess_cents,
            "cases": [c.id for c in cases],
        },
    )
    return inv, True, cases


def _lock_invoice(db: Session, invoice_id: str) -> SupplierInvoice:
    inv = db.execute(
        select(SupplierInvoice)
        .where(SupplierInvoice.id == invoice_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if inv is None:
        raise PayablesNotFoundError(f"supplier invoice {invoice_id} not found")
    return inv


def record_credit_note(
    db: Session,
    *,
    applies_to_invoice_id: str,
    credit_note_number: str,
    credit_note_date: date,
    net_cents: int,
    tax_cents: int,
    gross_cents: int,
    received_at: datetime | None,
    source_refs: list,
    actor_id: str,
    actor_capability: str,
    resolves_case_id: str | None = None,
) -> tuple[SupplierInvoice, bool]:
    """`resolves_case_id`, when given, resolves that case as ISSUED_CREDIT_NOTE
    in this SAME transaction (no separate commit) -- the only route from a
    potential SLA credit to actual money. Called only by a human via
    POST /invoices/{id}/credit-notes (no import/scheduler/agent module may
    call this function with `resolves_case_id` set -- see
    scripts/verify_payables_v2.py's static check)."""
    original = _lock_invoice(db, applies_to_invoice_id)
    if original.kind != "INVOICE":
        raise PayablesConflictError("credit_note_must_apply_to_an_invoice")
    if gross_cents != net_cents + tax_cents or gross_cents <= 0:
        raise PayablesConflictError("credit_note_amounts_inconsistent")
    existing = db.execute(
        select(SupplierInvoice).where(
            SupplierInvoice.supplier_id == original.supplier_id,
            SupplierInvoice.invoice_number == credit_note_number,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.applies_to_invoice_id == original.id and existing.gross_cents == gross_cents:
            return existing, False
        raise PayablesConflictError("credit_note_number_exists", {"existing_id": existing.id})
    if gross_cents > _balance(original).outstanding_cents:
        raise PayablesConflictError(
            "credit_exceeds_outstanding",
            {"gross_cents": gross_cents, "outstanding_cents": _balance(original).outstanding_cents},
        )
    cn = SupplierInvoice(
        festival_id=original.festival_id,
        supplier_id=original.supplier_id,
        po_refs=list(original.po_refs),
        invoice_number=credit_note_number,
        invoice_date=credit_note_date,
        net_cents=net_cents,
        tax_cents=tax_cents,
        gross_cents=gross_cents,
        outstanding_cents=0,
        received_at=received_at,
        source_refs=source_refs,
        kind="CREDIT_NOTE",
        applies_to_invoice_id=original.id,
        status="CREDIT_NOTE",
    )
    db.add(cn)
    db.flush()
    original.credited_cents += gross_cents
    original.version += 1
    _refresh_status(original)
    if original.po_refs:
        po = db.execute(
            select(PurchaseOrder).where(PurchaseOrder.id == original.po_refs[0]).with_for_update()
        ).scalar_one_or_none()
        if po is not None:
            _recompute_po(db, po, actor_id=actor_id)
    result = post_cumulative(
        db,
        festival_id=original.festival_id,
        base_key=f"SUPPLIER_CREDIT_NOTE:{cn.id}",
        business_date=credit_note_date,
        kind="SUPPLIER_CREDIT_NOTE",
        lines=acct.supplier_credit_note(gross_cents),
        source_refs=source_refs,
        source_record_ids=[cn.id, original.id],
        correction_of_key=f"SUPPLIER_INVOICE:{original.id}",
        note="Credit note linked to the original invoice; original posting untouched.",
        actor_id=actor_id,
        actor_capability=actor_capability,
    )
    cn.journal_entry_id = result.entry.id if result.entry else None
    _event(
        db,
        stream=f"supplier_invoice:{original.id}",
        event_type="supplier_invoice.credited",
        aggregate_id=original.id,
        version=original.version,
        actor_id=actor_id,
        actor_capability=actor_capability,
        payload={"credit_note_id": cn.id, "gross_cents": gross_cents},
    )
    if resolves_case_id is not None:
        case = db.get(Case, resolves_case_id)
        if case is None:
            raise PayablesNotFoundError(f"case {resolves_case_id} not found")
        if case.reason_code != "SLA_POTENTIAL_CREDIT":
            raise PayablesConflictError(
                "resolves_case_id_wrong_reason_code",
                {"case_id": case.id, "reason_code": case.reason_code},
            )
        if case.status != "OPEN":
            raise PayablesConflictError(
                "case_not_open", {"case_id": case.id, "status": case.status}
            )
        case.status = "RESOLVED"
        case.resolution_decision = "ISSUED_CREDIT_NOTE"
        case.resolution_note = f"Credit note {cn.id} ({gross_cents} cents) issued."
        case.resolved_by = actor_id
        case.resolved_capability = actor_capability
        case.resolved_at = datetime.now(UTC)
    return cn, True


def approve_variance(
    db: Session, *, invoice_id: str, actor_id: str, note: str | None
) -> SupplierInvoice:
    """Finance-lead approval of an invoice that exceeds its PO. Resolves the
    INVOICE_EXCEEDS_PO case; does NOT resolve any other blocking case."""
    inv = _lock_invoice(db, invoice_id)
    if inv.po_excess_cents <= 0:
        raise PayablesConflictError("invoice_does_not_exceed_po")
    if inv.variance_approved_at is not None:
        return inv
    inv.variance_approved_by = actor_id
    inv.variance_approved_at = datetime.now(UTC)
    inv.version += 1
    for case in _blocking_cases(db, inv.id):
        if case.reason_code == "INVOICE_EXCEEDS_PO":
            case.status = "RESOLVED"
            case.resolution_decision = "APPROVE_VARIANCE"
            case.resolution_note = note
            case.resolved_by = actor_id
            case.resolved_capability = CAPABILITY_FINANCE_LEAD
            case.resolved_at = datetime.now(UTC)
    _refresh_status(inv)
    _event(
        db,
        stream=f"supplier_invoice:{inv.id}",
        event_type="supplier_invoice.variance_approved",
        aggregate_id=inv.id,
        version=inv.version,
        actor_id=actor_id,
        actor_capability=CAPABILITY_FINANCE_LEAD,
        payload={"po_excess_cents": inv.po_excess_cents, "note": note},
    )
    return inv


# ---------------------------------------------------------------------------
# Payment
# ---------------------------------------------------------------------------


def pay_invoice(
    db: Session,
    *,
    invoice_id: str,
    amount_cents: int,
    idempotency_key: str,
    business_date: date,
    actor_id: str,
    actor_capability: str,
    bank_entry_id: str | None = None,
) -> tuple[SupplierPayment, bool]:
    inv = _lock_invoice(db, invoice_id)

    prior = db.execute(
        select(SupplierPayment).where(SupplierPayment.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if prior is not None:
        if prior.invoice_id != invoice_id or prior.amount_cents != amount_cents:
            raise PayablesConflictError(
                "idempotency_key_reused_with_different_payload", {"payment_id": prior.id}
            )
        return prior, False

    # Computed under the invoice lock already held above, AFTER the
    # idempotency lookup (so a retry still returns the original payment
    # rather than re-deriving the limit) -- never locks the PO here; the
    # only PO lock in this module is record_delivery_receipt's, and the
    # lock order everywhere else is PO-then-invoice, so pay_invoice must
    # not also take the PO lock or the two could deadlock against each
    # other under concurrency.
    delivery_supported = None
    if inv.po_refs:
        delivery_supported = delivery_supported_cents_for_po(db, inv.po_refs[0])
    try:
        rules.check_payment(
            _balance(inv),
            amount_cents,
            po_excess_cents=inv.po_excess_cents,
            variance_approved=inv.variance_approved_at is not None,
            is_credit_note=inv.kind == "CREDIT_NOTE",
            delivery_supported_cents=delivery_supported,
        )
    except rules.PaymentRefused as exc:
        details = dict(exc.details)
        if exc.reason_code == "amount_exceeds_delivery_supported" and inv.po_refs:
            lines = _po_lines(db, inv.po_refs[0])
            if len(lines) == 1:
                details["accepted_qty"] = min(_delivered_qty(db, lines[0].id), lines[0].quantity)
                details["unit_price_cents"] = lines[0].unit_price_cents
        raise PaymentRefusedError(exc.reason_code, details) from exc
    blocking = _blocking_cases(db, inv.id)
    if blocking:
        raise PaymentRefusedError("open_blocking_case", {"case_ids": [c.id for c in blocking]})

    pool = db.execute(
        select(CashPool).where(CashPool.festival_id == inv.festival_id).with_for_update()
    ).scalar_one_or_none()
    if pool is None or pool.available_cents < amount_cents:
        raise PaymentRefusedError(
            "insufficient_available_cash",
            {"available_cents": None if pool is None else pool.available_cents},
        )
    pool.available_cents -= amount_cents
    pool.version += 1

    # `supplier_payments` is append-only for encore_app (no UPDATE), so the
    # row is inserted once, complete: the id is generated first, the journal
    # entry posted citing it, then the payment row written with its entry id.
    payment_id = _new_id()
    result = post_cumulative(
        db,
        festival_id=inv.festival_id,
        base_key=f"SUPPLIER_PAYMENT:{payment_id}",
        business_date=business_date,
        kind="SUPPLIER_PAYMENT",
        lines=acct.supplier_payment(amount_cents),
        source_record_ids=[payment_id, inv.id] + ([bank_entry_id] if bank_entry_id else []),
        note="Simulated supplier payment. No real money moved.",
        actor_id=actor_id,
        actor_capability=actor_capability,
    )
    payment = SupplierPayment(
        id=payment_id,
        festival_id=inv.festival_id,
        supplier_id=inv.supplier_id,
        invoice_id=inv.id,
        idempotency_key=idempotency_key,
        amount_cents=amount_cents,
        business_date=business_date,
        payment_reference=f"SIM-AP-{idempotency_key[:40]}",
        approved_by=actor_id,
        approved_capability=actor_capability,
        journal_entry_id=result.entry.id,
    )
    db.add(payment)
    db.flush()
    inv.paid_cents += amount_cents
    inv.version += 1
    _refresh_status(inv)
    _event(
        db,
        stream=f"supplier_invoice:{inv.id}",
        event_type="supplier_invoice.paid",
        aggregate_id=inv.id,
        version=inv.version,
        actor_id=actor_id,
        actor_capability=actor_capability,
        payload={
            "payment_id": payment.id,
            "amount_cents": amount_cents,
            "outstanding_cents": inv.outstanding_cents,
        },
    )
    return payment, True


def payables_summary(db: Session, *, festival_id: str, as_of: date) -> dict:
    invoices = (
        db.execute(
            select(SupplierInvoice)
            .where(SupplierInvoice.festival_id == festival_id)
            .order_by(SupplierInvoice.invoice_date, SupplierInvoice.invoice_number)
        )
        .scalars()
        .all()
    )
    suppliers = {
        s.id: s.name
        for s in db.execute(select(Supplier).where(Supplier.festival_id == festival_id)).scalars()
    }
    rows = []
    for inv in invoices:
        rows.append(
            {
                "invoice_id": inv.id,
                "supplier_id": inv.supplier_id,
                "supplier_name": suppliers.get(inv.supplier_id),
                "kind": inv.kind,
                "invoice_number": inv.invoice_number,
                "invoice_date": inv.invoice_date.isoformat(),
                "due_date": inv.due_date.isoformat() if inv.due_date else None,
                "gross_cents": inv.gross_cents,
                "credited_cents": inv.credited_cents,
                "paid_cents": inv.paid_cents,
                "outstanding_cents": inv.outstanding_cents if inv.kind == "INVOICE" else 0,
                "po_excess_cents": inv.po_excess_cents,
                "variance_approved": inv.variance_approved_at is not None,
                "status": display_status(inv, as_of),
                "applies_to_invoice_id": inv.applies_to_invoice_id,
                "journal_entry_id": inv.journal_entry_id,
                "blocking_case_ids": [c.id for c in _blocking_cases(db, inv.id)],
            }
        )
    total = sum(r["outstanding_cents"] for r in rows)
    return {"as_of": as_of.isoformat(), "invoices": rows, "total_outstanding_cents": total}
