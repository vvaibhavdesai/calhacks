"""A08 other organizer cash/income/expense records and finance configuration.

Opening balances, ticket settlements, sponsor invoices/receipts and
operating expenses. Each record is idempotent on its natural reference
(same reference + same amount = the original row; same reference with a
different amount = 409, never a silent overwrite) and posts its journal
entry in the same transaction.

Ticket revenue recognition: ticket money received before the event is
deferred (DEFERRED_TICKET_REVENUE). `recognize_ticket_revenue` is the
explicit command that recognizes everything deferred through a business
date once the event has happened; a ticket settlement recorded after that
recognition posts straight to TICKET_REVENUE. The recognition date is a
human/config decision, not inferred from the calendar.
"""

from __future__ import annotations

from datetime import UTC, datetime

from domain import accounting as acct
from domain.tax import DEFAULT_TAX_CONFIG
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.accounting import post_cumulative
from app.models import _new_id
from app.models_finance import (
    Expense,
    FinanceConfig,
    OpeningBalance,
    SponsorInvoice,
    SponsorshipReceipt,
    TicketSettlement,
)
from app.models_ledger import JournalEntry
from app.uow import UnitOfWork


class RecordConflictError(ValueError):
    def __init__(self, code: str, details: dict | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = details or {}


class RecordNotFoundError(LookupError):
    pass


def _same_or_conflict(existing, amount_field: str, amount: int, code: str):
    if getattr(existing, amount_field) != amount:
        raise RecordConflictError(
            code, {"existing_id": existing.id, amount_field: getattr(existing, amount_field)}
        )
    return existing, False


# ---------------------------------------------------------------------------
# Finance configuration
# ---------------------------------------------------------------------------

DEFAULT_SCHEDULE = {
    "timezone": "America/Los_Angeles",
    "statement_local_time": "09:00",
    "payout_local_time": "10:00",
    "forecast_horizon_days": 14,
}


def active_config(db: Session, festival_id: str) -> FinanceConfig | None:
    return db.execute(
        select(FinanceConfig)
        .where(FinanceConfig.festival_id == festival_id, FinanceConfig.status == "ACTIVE")
        .order_by(FinanceConfig.version.desc())
    ).scalar_one_or_none()


def get_or_default_config(db: Session, festival_id: str) -> FinanceConfig:
    """The active config, or (first use) version 1 created from the
    documented DEMO defaults -- recorded as a real row so every report can
    pin the exact version it used."""
    cfg = active_config(db, festival_id)
    if cfg is not None:
        return cfg
    # ON CONFLICT: two concurrent first reads must not both create v1.
    db.execute(
        pg_insert(FinanceConfig)
        .values(
            id=_new_id(),
            festival_id=festival_id,
            version=1,
            status="ACTIVE",
            tax=DEFAULT_TAX_CONFIG,
            **DEFAULT_SCHEDULE,
        )
        .on_conflict_do_nothing(index_elements=["festival_id", "version"])
    )
    return active_config(db, festival_id)


def set_config(
    db: Session,
    *,
    festival_id: str,
    tax: dict,
    timezone: str,
    statement_local_time: str,
    payout_local_time: str,
    forecast_horizon_days: int,
    actor_id: str,
    actor_capability: str,
) -> FinanceConfig:
    for key in ("rates_by_category", "liability_owner_by_custody"):
        if key not in tax:
            raise RecordConflictError("tax_config_missing_field", {"field": key})
    if forecast_horizon_days < 1 or forecast_horizon_days > 366:
        raise RecordConflictError("forecast_horizon_out_of_range")
    current = active_config(db, festival_id)
    version = 1
    if current is not None:
        current.status = "SUPERSEDED"
        version = current.version + 1
    cfg = FinanceConfig(
        festival_id=festival_id,
        version=version,
        status="ACTIVE",
        tax=tax,
        timezone=timezone,
        statement_local_time=statement_local_time,
        payout_local_time=payout_local_time,
        forecast_horizon_days=forecast_horizon_days,
        created_by=actor_id,
    )
    db.add(cfg)
    db.flush()
    UnitOfWork(db).append_event(
        stream_id=f"finance_config:{festival_id}",
        event_type="finance_config.versioned",
        aggregate_id=cfg.id,
        aggregate_version=version,
        actor_id=actor_id,
        actor_type="human",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        payload={"version": version, "supersedes": current.id if current else None},
    )
    return cfg


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


def record_opening_balance(
    db, *, festival_id, as_of, amount_cents, source_refs, actor_id, actor_capability
):
    existing = db.execute(
        select(OpeningBalance).where(
            OpeningBalance.festival_id == festival_id,
            OpeningBalance.account_role == "BANK_OPERATING",
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _same_or_conflict(
            existing, "amount_cents", amount_cents, "opening_balance_already_recorded"
        )
    ob = OpeningBalance(
        festival_id=festival_id,
        account_role="BANK_OPERATING",
        as_of=as_of,
        amount_cents=amount_cents,
        source_refs=source_refs,
    )
    db.add(ob)
    db.flush()
    post_cumulative(
        db,
        festival_id=festival_id,
        base_key=f"OPENING:{festival_id}",
        business_date=as_of,
        kind="OPENING_BALANCE",
        lines=acct.opening_balance(amount_cents),
        source_refs=source_refs,
        source_record_ids=[ob.id],
        actor_id=actor_id,
        actor_capability=actor_capability,
    )
    return ob, True


def _recognized(db: Session, festival_id: str) -> bool:
    return (
        db.execute(
            select(JournalEntry.id).where(
                JournalEntry.festival_id == festival_id,
                JournalEntry.kind == "TICKET_REVENUE_RECOGNITION",
            )
        ).first()
        is not None
    )


def record_ticket_settlement(
    db,
    *,
    festival_id,
    platform,
    settlement_reference,
    gross_cents,
    platform_fees_cents,
    net_cents,
    business_date,
    bank_entry_id,
    source_refs,
    actor_id,
    actor_capability,
):
    if gross_cents - platform_fees_cents != net_cents:
        raise RecordConflictError("ticket_amounts_inconsistent")
    existing = db.execute(
        select(TicketSettlement).where(
            TicketSettlement.festival_id == festival_id,
            TicketSettlement.settlement_reference == settlement_reference,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _same_or_conflict(
            existing, "gross_cents", gross_cents, "ticket_settlement_reference_exists"
        )
    t = TicketSettlement(
        festival_id=festival_id,
        platform=platform,
        settlement_reference=settlement_reference,
        gross_cents=gross_cents,
        platform_fees_cents=platform_fees_cents,
        net_cents=net_cents,
        business_date=business_date,
        bank_entry_id=bank_entry_id,
        source_refs=source_refs,
    )
    db.add(t)
    db.flush()
    deferred = not _recognized(db, festival_id)
    post_cumulative(
        db,
        festival_id=festival_id,
        base_key=f"TICKETS:{t.id}",
        business_date=business_date,
        kind="TICKET_SETTLEMENT",
        lines=acct.ticket_settlement(
            gross_cents=gross_cents,
            fee_cents=platform_fees_cents,
            net_cents=net_cents,
            deferred=deferred,
        ),
        source_refs=source_refs,
        source_record_ids=[t.id] + ([bank_entry_id] if bank_entry_id else []),
        note="Deferred until the event is recognized." if deferred else "Post-event: recognized.",
        actor_id=actor_id,
        actor_capability=actor_capability,
    )
    return t, True


def recognize_ticket_revenue(db, *, festival_id, business_date, actor_id, actor_capability):
    deferred_balance = -sum(
        ln.debit_cents - ln.credit_cents
        for e in db.execute(
            select(JournalEntry).where(JournalEntry.festival_id == festival_id)
        ).scalars()
        for ln in e.lines
        if ln.account_code == "2400"
    )
    if deferred_balance <= 0:
        raise RecordConflictError("no_deferred_ticket_revenue")
    ids = [
        t
        for (t,) in db.execute(
            select(TicketSettlement.id).where(
                TicketSettlement.festival_id == festival_id,
                TicketSettlement.business_date <= business_date,
            )
        ).all()
    ]
    return post_cumulative(
        db,
        festival_id=festival_id,
        base_key=f"TICKET_RECOGNITION:{festival_id}:{business_date.isoformat()}",
        business_date=business_date,
        kind="TICKET_REVENUE_RECOGNITION",
        lines=acct.ticket_recognition(deferred_balance),
        source_record_ids=ids,
        actor_id=actor_id,
        actor_capability=actor_capability,
    )


def record_sponsor_invoice(
    db,
    *,
    festival_id,
    sponsor_name,
    invoice_number,
    invoice_date,
    due_date,
    amount_cents,
    source_refs,
):
    existing = db.execute(
        select(SponsorInvoice).where(
            SponsorInvoice.festival_id == festival_id,
            SponsorInvoice.invoice_number == invoice_number,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _same_or_conflict(
            existing, "amount_cents", amount_cents, "sponsor_invoice_number_exists"
        )
    si = SponsorInvoice(
        festival_id=festival_id,
        sponsor_name=sponsor_name,
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        due_date=due_date,
        amount_cents=amount_cents,
        source_refs=source_refs,
    )
    db.add(si)
    db.flush()
    # Memo only: sponsorship is recognized on receipt (DRAFT recognition
    # rule, same as the oracle's), so an unpaid sponsor invoice is a memo
    # receivable, not a journal entry.
    return si, True


def sponsor_received_cents(db: Session, sponsor_invoice_id: str) -> int:
    return int(
        db.execute(
            select(func.coalesce(func.sum(SponsorshipReceipt.amount_cents), 0)).where(
                SponsorshipReceipt.sponsor_invoice_id == sponsor_invoice_id
            )
        ).scalar_one()
    )


def record_sponsorship_receipt(
    db,
    *,
    sponsor_invoice_id,
    receipt_reference,
    amount_cents,
    business_date,
    bank_entry_id,
    source_refs,
    actor_id,
    actor_capability,
):
    si = db.execute(
        select(SponsorInvoice).where(SponsorInvoice.id == sponsor_invoice_id).with_for_update()
    ).scalar_one_or_none()
    if si is None:
        raise RecordNotFoundError(f"sponsor invoice {sponsor_invoice_id} not found")
    existing = db.execute(
        select(SponsorshipReceipt).where(
            SponsorshipReceipt.festival_id == si.festival_id,
            SponsorshipReceipt.receipt_reference == receipt_reference,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _same_or_conflict(
            existing, "amount_cents", amount_cents, "sponsorship_receipt_reference_exists"
        )
    if sponsor_received_cents(db, si.id) + amount_cents > si.amount_cents:
        raise RecordConflictError(
            "receipt_exceeds_sponsor_invoice", {"invoice_amount_cents": si.amount_cents}
        )
    r = SponsorshipReceipt(
        festival_id=si.festival_id,
        sponsor_invoice_id=si.id,
        receipt_reference=receipt_reference,
        amount_cents=amount_cents,
        business_date=business_date,
        bank_entry_id=bank_entry_id,
        source_refs=source_refs,
    )
    db.add(r)
    db.flush()
    post_cumulative(
        db,
        festival_id=si.festival_id,
        base_key=f"SPONSOR:{r.id}",
        business_date=business_date,
        kind="SPONSORSHIP_RECEIPT",
        lines=acct.sponsorship_receipt(amount_cents),
        source_refs=source_refs,
        source_record_ids=[r.id, si.id] + ([bank_entry_id] if bank_entry_id else []),
        actor_id=actor_id,
        actor_capability=actor_capability,
    )
    return r, True


def record_expense(
    db,
    *,
    festival_id,
    expense_reference,
    category,
    description,
    amount_cents,
    business_date,
    bank_entry_id,
    source_refs,
    actor_id,
    actor_capability,
):
    if amount_cents <= 0:
        raise RecordConflictError("expense_amount_must_be_positive")
    existing = db.execute(
        select(Expense).where(
            Expense.festival_id == festival_id, Expense.expense_reference == expense_reference
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _same_or_conflict(existing, "amount_cents", amount_cents, "expense_reference_exists")
    e = Expense(
        festival_id=festival_id,
        expense_reference=expense_reference,
        category=category,
        description=description,
        amount_cents=amount_cents,
        business_date=business_date,
        bank_entry_id=bank_entry_id,
        source_refs=source_refs,
    )
    db.add(e)
    db.flush()
    post_cumulative(
        db,
        festival_id=festival_id,
        base_key=f"EXPENSE:{e.id}",
        business_date=business_date,
        kind="OPERATING_EXPENSE",
        lines=acct.operating_expense(amount_cents),
        source_refs=source_refs,
        source_record_ids=[e.id] + ([bank_entry_id] if bank_entry_id else []),
        actor_id=actor_id,
        actor_capability=actor_capability,
    )
    return e, True
