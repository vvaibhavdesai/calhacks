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
from app.models import CAPABILITY_FINANCE_LEAD, CAPABILITY_RESOLVE_CASE, _new_id
from app.models_finance import PurchaseOrder, SupplierPayment
from app.models_ledger import Case, Supplier, SupplierInvoice
from app.models_payout_dispatch import CashPool
from app.uow import UnitOfWork

BLOCKING_CASE_REASONS = ("POSSIBLE_DUPLICATE_SUPPLIER_INVOICE", "INVOICE_EXCEEDS_PO")


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


def _open_case(db, *, reason_code, invoice: SupplierInvoice, evidence, impact, proposed, cap):
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
    return rules.InvoiceBalance(inv.gross_cents, inv.credited_cents, inv.paid_cents)


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
) -> tuple[PurchaseOrder, bool]:
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise PayablesNotFoundError(f"supplier {supplier_id} not found")
    if amount_cents <= 0:
        raise PayablesConflictError("po_amount_must_be_positive")
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
    _event(
        db,
        stream=f"purchase_order:{po.id}",
        event_type="purchase_order.created",
        aggregate_id=po.id,
        version=1,
        actor_id=actor_id,
        actor_capability=actor_capability,
        payload={"po_number": po_number, "amount_cents": amount_cents},
    )
    return po, True


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

    existing = db.execute(
        select(SupplierInvoice).where(
            SupplierInvoice.supplier_id == supplier_id,
            SupplierInvoice.invoice_number == invoice_number,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.source_refs == source_refs and existing.gross_cents == gross_cents:
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
) -> tuple[SupplierInvoice, bool]:
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

    try:
        rules.check_payment(
            _balance(inv),
            amount_cents,
            po_excess_cents=inv.po_excess_cents,
            variance_approved=inv.variance_approved_at is not None,
            is_credit_note=inv.kind == "CREDIT_NOTE",
        )
    except rules.PaymentRefused as exc:
        raise PaymentRefusedError(exc.reason_code, exc.details) from exc
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
        source_record_ids=[payment_id, inv.id],
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
