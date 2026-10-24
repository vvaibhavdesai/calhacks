"""A08 reports: draft tax summary, cash position, trial balance, P&L,
journal drill-down, vendor statements and daily close.

Every number here is computed live from posted journal lines or the source
tables -- no stored report totals are served back as truth. The only
stored report artifacts are issued vendor statement versions and daily
close snapshots, both append-only, both pinning the inputs they used.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from domain import accounting as acct
from domain import cash as cash_rules
from domain import tax as tax_rules
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.accounting import (
    balances_by_role,
    festival_balances,
    unbalanced_entry_count,
)
from app.finance_records import get_or_default_config, sponsor_received_cents
from app.models_finance import (
    DailyClose,
    Expense,
    OpeningBalance,
    PurchaseOrder,
    SponsorInvoice,
    SponsorshipReceipt,
    SupplierPayment,
    TicketSettlement,
    VendorStatement,
)
from app.models_ledger import (
    Case,
    JournalEntry,
    Order,
    OrderLine,
    Payment,
    Payout,
    SupplierInvoice,
    Vendor,
)
from app.models_payout_dispatch import CashPool
from app.models_reconciliation import BankEntry
from app.models_settlement import Settlement
from app.payables import payables_summary
from app.uow import UnitOfWork

IN_FLIGHT_PAYOUT_STATES = ("DISPATCH_PENDING", "SUBMITTED", "UNKNOWN", "CANCEL_REQUESTED")


class CloseRefusedError(ValueError):
    def __init__(self, failing: list[str], checks: dict) -> None:
        super().__init__("daily_close_checks_failed")
        self.failing = failing
        self.checks = checks


# ---------------------------------------------------------------------------
# Tax
# ---------------------------------------------------------------------------


def _round(q: Decimal) -> int:
    return int(q.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _largest_remainder(total: int, weights: list[tuple[str, int]]) -> dict[str, int]:
    """Split `total` (any sign) across keys proportionally to non-negative
    weights; leftover cents go to the largest remainders, ties by key."""
    wsum = sum(w for _k, w in weights)
    if wsum == 0 or total == 0:
        return {k: 0 for k, _w in weights}
    sign = -1 if total < 0 else 1
    t = abs(total)
    shares = {}
    rems = []
    for k, w in weights:
        exact = Decimal(t) * Decimal(w) / Decimal(wsum)
        base = int(exact)
        shares[k] = base
        rems.append((exact - base, k))
    left = t - sum(shares.values())
    for _r, k in sorted(rems, key=lambda x: (-x[0], x[1]))[:left]:
        shares[k] += 1
    return {k: sign * v for k, v in shares.items()}


def _tax_lines(db: Session, festival_id: str, through: date | None) -> list[tax_rules.TaxLine]:
    """Supports both refund representations:
      - a refund/chargeback PAYMENT against the original sale order (A's
        fixtures): its tax/eligible are allocated from the order and split
        across lines deterministically by line id;
      - a separate refund ORDER with its own negative lines and business
        date (lane B's generator): its lines are taken as recorded, and its
        refund payment is not allocated a second time.
    Custody comes from the order's first payment of any kind. Payments and
    lines are loaded in bulk (one query each), not per order."""
    q = (
        select(Order)
        .join(Vendor, Vendor.id == Order.vendor_id)
        .where(Vendor.festival_id == festival_id)
    )
    if through is not None:
        q = q.where(Order.business_date <= through)
    orders = db.execute(q).scalars().all()
    order_ids = [o.id for o in orders]
    payments_by_order: dict[str, list[Payment]] = defaultdict(list)
    lines_by_order: dict[str, list[OrderLine]] = defaultdict(list)
    for chunk_start in range(0, len(order_ids), 5000):
        chunk = order_ids[chunk_start : chunk_start + 5000]
        for p in db.execute(
            select(Payment).where(Payment.order_id.in_(chunk)).order_by(Payment.created_at)
        ).scalars():
            payments_by_order[p.order_id].append(p)
        for ln in db.execute(select(OrderLine).where(OrderLine.order_id.in_(chunk))).scalars():
            lines_by_order[ln.order_id].append(ln)

    out: list[tax_rules.TaxLine] = []
    for order in orders:
        payments = payments_by_order.get(order.id, [])
        if not payments:
            continue
        first = next((p for p in payments if p.kind == "capture"), payments[0])
        custody = "central" if first.custody == "centrally_collected" else "vendor_retained"
        lines = sorted(lines_by_order.get(order.id, []), key=lambda ln: ln.line_id)
        is_refund_order = order.gross_cents < 0
        for ln in lines:
            out.append(
                tax_rules.TaxLine(
                    business_date=order.business_date,
                    order_line_ref=ln.id,
                    custody=custody,
                    tax_category=ln.tax_category,
                    eligible_net_cents=ln.eligible_net_cents,
                    supplied_tax_cents=ln.tax_cents,
                    inherits_supplied=is_refund_order,
                )
            )
        if is_refund_order:
            continue
        captures = sum(abs(p.amount_cents) for p in payments if p.kind == "capture")
        reversals = sum(abs(p.amount_cents) for p in payments if p.kind != "capture")
        if not reversals:
            continue
        # Same split app/accounting.py's sales posting uses: reversals first
        # return any duplicate-charge excess (customer money, no tax effect);
        # only the remainder reverses goods. Refund tax/tips are the order's
        # totals minus what the remaining goods carry, so the summary ties to
        # the sales-tax liability in the journal to the cent.
        m = acct.split_order_money(
            order_gross_cents=order.gross_cents, captures_cents=captures, reversals_cents=reversals
        )
        if m.goods_refunded_cents == 0:
            continue
        g = Decimal(order.gross_cents or 1)
        kept_tax = _round(Decimal(order.tax_cents) * m.goods_cents / g)
        kept_tips = _round(Decimal(order.tips_cents) * m.goods_cents / g)
        refund_tax = order.tax_cents - kept_tax
        refund_tips = order.tips_cents - kept_tips
        refund_eligible = m.goods_refunded_cents - refund_tax - refund_tips
        tax_split = _largest_remainder(-refund_tax, [(ln.id, ln.tax_cents) for ln in lines])
        elig_split = _largest_remainder(
            -refund_eligible, [(ln.id, ln.eligible_net_cents) for ln in lines]
        )
        for ln in lines:
            out.append(
                tax_rules.TaxLine(
                    business_date=order.business_date,
                    order_line_ref=f"{ln.id}:refund",
                    custody=custody,
                    tax_category=ln.tax_category,
                    eligible_net_cents=elig_split[ln.id],
                    supplied_tax_cents=tax_split[ln.id],
                    inherits_supplied=True,
                )
            )
    return out


def tax_summary(db: Session, *, festival_id: str, through: date | None = None) -> dict:
    cfg = get_or_default_config(db, festival_id)
    summary = tax_rules.summarize(_tax_lines(db, festival_id, through), cfg.tax)
    bal = festival_balances(db, festival_id, through=through)
    journal_tax = -bal["2100"]
    summary["finance_config_id"] = cfg.id
    summary["finance_config_version"] = cfg.version
    summary["journal_sales_tax_liability_cents"] = journal_tax
    summary["source_totals_match_journal"] = (
        journal_tax == summary["totals"]["central_supplied_tax_cents"]
    )
    return summary


# ---------------------------------------------------------------------------
# Cash, trial balance, P&L
# ---------------------------------------------------------------------------


def _reserved_payouts(db: Session, festival_id: str) -> int:
    return int(
        db.execute(
            select(func.coalesce(func.sum(Payout.amount_cents), 0))
            .join(Vendor, Vendor.id == Payout.vendor_id)
            .where(
                Vendor.festival_id == festival_id,
                Payout.state.in_(IN_FLIGHT_PAYOUT_STATES),
                Payout.reservation_released_at.is_(None),
            )
        ).scalar_one()
    )


def cash_report(db: Session, *, festival_id: str, as_of: date) -> dict:
    cfg = get_or_default_config(db, festival_id)
    bal = festival_balances(db, festival_id)
    invoices = (
        db.execute(
            select(SupplierInvoice).where(
                SupplierInvoice.festival_id == festival_id,
                SupplierInvoice.kind == "INVOICE",
                SupplierInvoice.outstanding_cents > 0,
            )
        )
        .scalars()
        .all()
    )
    obligations = [
        cash_rules.Obligation(
            kind="SUPPLIER_INVOICE",
            ref_id=i.id,
            due_date=i.due_date,
            amount_cents=i.outstanding_cents,
        )
        for i in invoices
    ]
    position = cash_rules.cash_position(
        balances_by_role=balances_by_role(bal),
        reserved_payouts_cents=_reserved_payouts(db, festival_id),
        obligations=obligations,
        as_of=as_of,
        horizon_days=cfg.forecast_horizon_days,
    )
    upcoming_payouts = (
        db.execute(
            select(Payout)
            .join(Vendor, Vendor.id == Payout.vendor_id)
            .where(
                Vendor.festival_id == festival_id,
                Payout.state.in_(("DRAFT", "APPROVED", *IN_FLIGHT_PAYOUT_STATES)),
            )
        )
        .scalars()
        .all()
    )
    position["upcoming_vendor_payouts"] = [
        {
            "payout_id": p.id,
            "vendor_id": p.vendor_id,
            "business_date": p.business_date.isoformat(),
            "state": p.state,
            "amount_cents": p.amount_cents,
            "included_in_vendor_funds_payable": True,
        }
        for p in upcoming_payouts
    ]
    pool = db.execute(
        select(CashPool).where(CashPool.festival_id == festival_id)
    ).scalar_one_or_none()
    book_spendable = position["bank_cents"] - position["protected_cents"]
    position["release_cash_pool"] = (
        None
        if pool is None
        else {
            "available_cents": pool.available_cents,
            "reserved_cents": pool.reserved_cents,
            "exceeds_book_spendable": pool.available_cents > book_spendable,
            "note": (
                "Cash a finance lead made available for releases (A07). Shown beside the books; "
                "if it exceeds bank minus protected liabilities, that is a warning."
            ),
        }
    )
    position["finance_config_version"] = cfg.version
    return position


def trial_balance(db: Session, *, festival_id: str, through: date | None = None) -> dict:
    tb = acct.trial_balance(festival_balances(db, festival_id, through=through))
    tb["through"] = through.isoformat() if through else None
    return tb


def profit_and_loss(db: Session, *, festival_id: str, through: date | None = None) -> dict:
    pnl = acct.profit_and_loss(festival_balances(db, festival_id, through=through))
    pnl["through"] = through.isoformat() if through else None
    pnl["note"] = (
        "Organizer P&L under the DRAFT posting map: vendor gross sales are custody "
        "liabilities, not organizer revenue."
    )
    return pnl


# ---------------------------------------------------------------------------
# Journal drill-down
# ---------------------------------------------------------------------------

_SOURCE_TABLES = (
    ("order", Order),
    ("payment", Payment),
    ("settlement", Settlement),
    ("bank_entry", BankEntry),
    ("supplier_invoice", SupplierInvoice),
    ("supplier_payment", SupplierPayment),
    ("purchase_order", PurchaseOrder),
    ("ticket_settlement", TicketSettlement),
    ("sponsor_invoice", SponsorInvoice),
    ("sponsorship_receipt", SponsorshipReceipt),
    ("expense", Expense),
    ("opening_balance", OpeningBalance),
    ("payout", Payout),
)


def _resolve(db: Session, record_id: str) -> dict:
    for kind, model in _SOURCE_TABLES:
        row = db.get(model, record_id)
        if row is None:
            continue
        refs = getattr(row, "source_refs", None)
        if refs is None and getattr(row, "source_document_id", None):
            refs = [
                {
                    "source_document_id": row.source_document_id,
                    "source_version": getattr(row, "source_version", None),
                    "content_hash": getattr(row, "content_hash", None),
                    "locator_kind": getattr(row, "locator_kind", None),
                    "locator": getattr(row, "locator", None),
                }
            ]
        return {"record_kind": kind, "record_id": record_id, "source_refs": refs or []}
    return {"record_kind": "unknown", "record_id": record_id, "source_refs": []}


def entry_out(e: JournalEntry, *, drill: bool = False, db: Session | None = None) -> dict:
    out = {
        "id": e.id,
        "entry_key": e.entry_key,
        "festival_id": e.festival_id,
        "business_date": e.business_date.isoformat() if e.business_date else None,
        "kind": e.kind,
        "posting_rule_version": e.posting_rule_version,
        "correction_of": e.correction_of,
        "note": e.note,
        "posted_at": e.posted_at.isoformat() if e.posted_at else None,
        "posted_by": e.posted_by,
        "source_refs": e.source_refs,
        "source_record_ids": e.source_record_ids,
        "lines": [
            {
                "account_code": ln.account_code,
                "account_role": ln.account_role,
                "debit_cents": ln.debit_cents,
                "credit_cents": ln.credit_cents,
            }
            for ln in e.lines
        ],
    }
    if drill and db is not None:
        out["sources"] = [_resolve(db, rid) for rid in e.source_record_ids]
        out["corrections"] = [
            c.entry_key
            for c in db.execute(select(JournalEntry).where(JournalEntry.correction_of == e.id))
            .scalars()
            .all()
        ]
    return out


# ---------------------------------------------------------------------------
# Vendor statements
# ---------------------------------------------------------------------------


def _due_by(cfg, business_date: date) -> datetime:
    hh, mm = (int(x) for x in cfg.statement_local_time.split(":"))
    local = datetime.combine(
        business_date + timedelta(days=1), time(hh, mm), ZoneInfo(cfg.timezone)
    )
    return local.astimezone(UTC)


def _latest_settlement(db: Session, vendor_id: str, business_date: date) -> Settlement | None:
    rows = (
        db.execute(
            select(Settlement)
            .where(Settlement.vendor_id == vendor_id, Settlement.business_date == business_date)
            .order_by(Settlement.created_at)
        )
        .scalars()
        .all()
    )
    superseded = {s.correction_of for s in rows if s.correction_of}
    live = [s for s in rows if s.id not in superseded]
    return live[-1] if live else None


def build_statement_content(db: Session, vendor: Vendor, business_date: date) -> tuple[str, dict]:
    settlement = _latest_settlement(db, vendor.id, business_date)
    payout = None
    if settlement is not None:
        payout = db.execute(
            select(Payout).where(Payout.settlement_id == settlement.id)
        ).scalar_one_or_none()
    open_cases = (
        db.execute(select(Case).where(Case.vendor_id == vendor.id, Case.status == "OPEN"))
        .scalars()
        .all()
    )
    holds = []
    if settlement is None:
        holds.append({"code": "settlement_not_calculated"})
    else:
        holds.extend({"code": "settlement_hold", "detail": h} for h in settlement.holds or [])
    holds.extend(
        {"code": "open_case", "case_id": c.id, "reason_code": c.reason_code} for c in open_cases
    )
    if payout is not None and payout.recovery_required:
        holds.append({"code": "payout_recovery_required", "payout_id": payout.id})

    entries = (
        db.execute(
            select(JournalEntry).where(
                JournalEntry.festival_id == vendor.festival_id,
                JournalEntry.business_date == business_date,
                JournalEntry.entry_key.like(f"%:{vendor.id}:{business_date.isoformat()}%"),
            )
        )
        .scalars()
        .all()
    )
    if payout is not None:
        entries += (
            db.execute(
                select(JournalEntry).where(JournalEntry.entry_key.like(f"PAYOUT:{payout.id}%"))
            )
            .scalars()
            .all()
        )

    if holds:
        status = "HELD"
    elif payout is not None and payout.state == "CONFIRMED":
        status = "FINAL"
    elif settlement is not None and settlement.payout_cents == 0:
        status = "FINAL"
    else:
        status = "PROVISIONAL"

    amounts = None
    line_items = []
    if settlement is not None:
        amounts = {
            "eligible_sales_cents": settlement.eligible_sales_cents,
            "centrally_held_cents": settlement.centrally_held_cents,
            "vendor_retained_cash_cents": settlement.vendor_retained_cash_cents,
            "tax_cents": settlement.tax_cents,
            "tips_cents": settlement.tips_cents,
            "assessed_fee_cents": settlement.assessed_fee_cents,
            "collected_fee_cents": settlement.collected_fee_cents,
            "vendor_fee_receivable_cents": settlement.vendor_fee_receivable_cents,
            "unpaid_balance_cents": settlement.unpaid_balance_cents,
            "processor_fee_collected_cents": settlement.processor_fee_collected_cents,
            "payout_cents": settlement.payout_cents,
        }
        line_items = [
            {
                "code": li.line_type,
                "amount_cents": li.amount_cents,
                "source_refs": li.source_refs,
            }
            for li in settlement.line_items
        ]
    content = {
        "vendor_id": vendor.id,
        "vendor_name": vendor.name,
        "business_date": business_date.isoformat(),
        "settlement_id": settlement.id if settlement else None,
        "calculation_version": settlement.calculation_version if settlement else None,
        "amounts": amounts,
        "line_items": line_items,
        "holds": holds,
        "payout": (
            None
            if payout is None
            else {
                "payout_id": payout.id,
                "state": payout.state,
                "amount_cents": payout.amount_cents,
                "label": "SIMULATED payout - no real money movement",
            }
        ),
        "journal_entries": sorted(
            ({"id": e.id, "entry_key": e.entry_key, "kind": e.kind} for e in entries),
            key=lambda x: x["entry_key"] or "",
        ),
    }
    return status, content


def issue_vendor_statement(
    db: Session,
    *,
    vendor_id: str,
    business_date: date,
    demo_effective_at: datetime | None,
    actor_id: str | None,
    actor_capability: str | None,
) -> tuple[VendorStatement, bool]:
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise LookupError(f"vendor {vendor_id} not found")
    cfg = get_or_default_config(db, vendor.festival_id)
    status, content = build_statement_content(db, vendor, business_date)
    digest = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
    # vendor_statements is append-only for encore_app, and SELECT ... FOR
    # UPDATE needs UPDATE privilege, so concurrent issuers for the same
    # vendor/date serialize on a transaction-scoped advisory lock instead.
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
        {"k": f"vendor_statement:{vendor_id}:{business_date.isoformat()}"},
    )
    latest = (
        db.execute(
            select(VendorStatement)
            .where(
                VendorStatement.vendor_id == vendor_id,
                VendorStatement.business_date == business_date,
            )
            .order_by(VendorStatement.statement_version.desc())
        )
        .scalars()
        .first()
    )
    if latest is not None and latest.status == status and latest.content.get("_digest") == digest:
        return latest, False

    due_by = _due_by(cfg, business_date)
    evaluated_at = demo_effective_at or datetime.now(UTC)
    content["_digest"] = digest
    content["demo_effective_at"] = demo_effective_at.isoformat() if demo_effective_at else None
    stmt = VendorStatement(
        festival_id=vendor.festival_id,
        vendor_id=vendor_id,
        business_date=business_date,
        statement_version=(latest.statement_version + 1) if latest else 1,
        status=status,
        settlement_id=content["settlement_id"],
        payout_id=content["payout"]["payout_id"] if content["payout"] else None,
        payout_state=content["payout"]["state"] if content["payout"] else None,
        finance_config_id=cfg.id,
        due_by=due_by,
        issued_on_time=evaluated_at <= due_by,
        content=content,
        supersedes_statement_id=latest.id if latest else None,
        issued_by=actor_id,
    )
    db.add(stmt)
    db.flush()
    UnitOfWork(db).append_event(
        stream_id=f"vendor_statement:{vendor_id}:{business_date.isoformat()}",
        event_type="vendor_statement.issued",
        aggregate_id=stmt.id,
        aggregate_version=stmt.statement_version,
        actor_id=actor_id,
        actor_type="human" if actor_id else "system",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        vendor_id=vendor_id,
        payload={
            "status": status,
            "statement_version": stmt.statement_version,
            "issued_on_time": stmt.issued_on_time,
            "due_by": due_by.isoformat(),
        },
    )
    return stmt, True


def statement_out(s: VendorStatement) -> dict:
    return {
        "id": s.id,
        "vendor_id": s.vendor_id,
        "business_date": s.business_date.isoformat(),
        "statement_version": s.statement_version,
        "status": s.status,
        "settlement_id": s.settlement_id,
        "payout_id": s.payout_id,
        "payout_state": s.payout_state,
        "finance_config_id": s.finance_config_id,
        "due_by": s.due_by.astimezone(UTC).isoformat(),
        "issued_on_time": s.issued_on_time,
        "issued_at": s.created_at.isoformat() if s.created_at else None,
        "supersedes_statement_id": s.supersedes_statement_id,
        "content": {k: v for k, v in s.content.items() if k != "_digest"},
    }


# ---------------------------------------------------------------------------
# Daily close
# ---------------------------------------------------------------------------


def close_checks(db: Session, *, festival_id: str, business_date: date) -> dict:
    tb = trial_balance(db, festival_id=festival_id, through=business_date)
    bal = festival_balances(db, festival_id)
    ap = payables_summary(db, festival_id=festival_id, as_of=business_date)
    tax = tax_summary(db, festival_id=festival_id, through=business_date)
    settlements = (
        db.execute(
            select(Settlement)
            .join(Vendor, Vendor.id == Settlement.vendor_id)
            .where(Vendor.festival_id == festival_id, Settlement.business_date == business_date)
        )
        .scalars()
        .all()
    )
    unposted = [
        s.id
        for s in settlements
        if s.assessed_fee_cents
        and not db.execute(
            select(JournalEntry.id).where(JournalEntry.source_record_ids.contains([s.id]))
        ).first()
    ]
    vendor_ids = {s.vendor_id for s in settlements}
    unstated = [
        v
        for v in sorted(vendor_ids)
        if not db.execute(
            select(VendorStatement.id).where(
                VendorStatement.vendor_id == v, VendorStatement.business_date == business_date
            )
        ).first()
    ]
    hard = {
        "trial_balance_balanced": tb["balanced"],
        "every_entry_balanced": unbalanced_entry_count(db, festival_id) == 0,
        "supplier_ap_matches_open_invoices": -bal["2300"] == ap["total_outstanding_cents"],
        "tax_source_totals_match_journal": tax["source_totals_match_journal"],
        "all_settlements_posted": not unposted,
    }
    warnings = {
        "suspense_zero": bal["1999"] == 0,
        "statements_issued_for_all_settled_vendors": not unstated,
        "processor_clearing_zero": bal["1100"] == 0,
    }
    return {
        "hard": hard,
        "warnings": warnings,
        "unposted_settlement_ids": unposted,
        "vendors_without_statement": unstated,
        "processor_clearing_cents": bal["1100"],
        "open_case_count": db.execute(
            select(func.count()).select_from(Case).where(Case.status == "OPEN")
        ).scalar_one(),
        "trial_balance": tb,
    }


def run_daily_close(
    db: Session,
    *,
    festival_id: str,
    business_date: date,
    note: str | None,
    actor_id: str,
    actor_capability: str,
) -> tuple[DailyClose, bool]:
    existing = db.execute(
        select(DailyClose).where(
            DailyClose.festival_id == festival_id, DailyClose.business_date == business_date
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    checks = close_checks(db, festival_id=festival_id, business_date=business_date)
    failing = [k for k, ok in checks["hard"].items() if not ok]
    if failing:
        raise CloseRefusedError(failing, checks)
    tb = checks.pop("trial_balance")
    count = db.execute(
        select(func.count())
        .select_from(JournalEntry)
        .where(JournalEntry.festival_id == festival_id, JournalEntry.business_date <= business_date)
    ).scalar_one()
    close = DailyClose(
        festival_id=festival_id,
        business_date=business_date,
        trial_balance=tb,
        checks=checks,
        journal_entry_count=count,
        closed_by=actor_id,
        note=note,
    )
    db.add(close)
    db.flush()
    UnitOfWork(db).append_event(
        stream_id=f"daily_close:{festival_id}",
        event_type="daily_close.closed",
        aggregate_id=close.id,
        aggregate_version=1,
        actor_id=actor_id,
        actor_type="human",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        payload={"business_date": business_date.isoformat(), "warnings": checks["warnings"]},
    )
    return close, True


def sponsorship_memo(db: Session, festival_id: str) -> list[dict]:
    rows = []
    for si in db.execute(
        select(SponsorInvoice).where(SponsorInvoice.festival_id == festival_id)
    ).scalars():
        got = sponsor_received_cents(db, si.id)
        rows.append(
            {
                "sponsor_invoice_id": si.id,
                "sponsor_name": si.sponsor_name,
                "invoice_number": si.invoice_number,
                "amount_cents": si.amount_cents,
                "received_cents": got,
                "outstanding_memo_cents": si.amount_cents - got,
                "status": (
                    "PAID" if got == si.amount_cents else ("PARTIALLY_PAID" if got else "OPEN")
                ),
            }
        )
    return rows


def festival_report(db: Session, *, festival_id: str, as_of: date) -> dict:
    """One page of festival-level books: P&L, cash, payables, sponsorship
    memo receivables, tax and the closes performed so far."""
    by_kind = defaultdict(int)
    for kind, n in db.execute(
        select(JournalEntry.kind, func.count())
        .where(JournalEntry.festival_id == festival_id)
        .group_by(JournalEntry.kind)
    ).all():
        by_kind[kind or "UNSPECIFIED"] = n
    closes = (
        db.execute(
            select(DailyClose.business_date)
            .where(DailyClose.festival_id == festival_id)
            .order_by(DailyClose.business_date)
        )
        .scalars()
        .all()
    )
    return {
        "festival_id": festival_id,
        "as_of": as_of.isoformat(),
        "profit_and_loss": profit_and_loss(db, festival_id=festival_id),
        "cash": cash_report(db, festival_id=festival_id, as_of=as_of),
        "payables": payables_summary(db, festival_id=festival_id, as_of=as_of),
        "sponsorship_receivables_memo": sponsorship_memo(db, festival_id),
        "tax": tax_summary(db, festival_id=festival_id),
        "journal_entry_counts_by_kind": dict(by_kind),
        "closed_business_dates": [d.isoformat() for d in closes],
        "labels": [
            "Simulated payouts and supplier payments - no real money movement.",
            tax_rules.STATUS_LABEL,
        ],
    }
