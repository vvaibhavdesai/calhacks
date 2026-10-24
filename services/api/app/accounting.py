"""A08 accounting: DB-touching journal posting.

Every posting goes through `post_cumulative`: a business fact has a
deterministic base `entry_key` (e.g. "SALES:<vendor>:<date>:1100") and a
desired cumulative set of lines. The first post inserts the base entry.
A repeat with the same lines is a no-op. A repeat with DIFFERENT lines
(late orders, a settlement correction, a credit note) never edits the
posted entry -- `journal_entries`/`journal_lines` are append-only for
encore_app (migration 0003) -- it appends a linked adjustment entry
"<key>#adj<n>" (`correction_of` = the base entry) carrying exactly the
delta. Posting is therefore idempotent and history-preserving.

Concurrency: the base key is serialized with a transaction-scoped Postgres
advisory lock (`pg_advisory_xact_lock(hashtext(key))`), so two concurrent
posts of the same fact compute their delta one after another instead of
both appending the same adjustment.

Atomicity (C06): functions here add rows and audit events on the caller's
session and never commit; the router commits once per request.

Closed days: after `daily_close` for a festival/business_date, a NEW base
entry for that date is refused (`BusinessDateClosedError`). Adjustments to
an already-posted fact are still allowed and are marked
`posted_after_close` in their note, because refusing them would hide a
real late fact; the close's snapshot stays as it was.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from domain import accounting as acct
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import _new_id
from app.models_finance import Account, DailyClose, PostingRuleVersion
from app.models_ledger import JournalEntry, JournalLine, Order, Payment, Vendor
from app.models_reconciliation import BankEntry
from app.models_settlement import Settlement
from app.uow import UnitOfWork


class BusinessDateClosedError(ValueError):
    def __init__(self, festival_id: str, business_date: date) -> None:
        super().__init__(f"business date {business_date} is closed for {festival_id}")
        self.festival_id = festival_id
        self.business_date = business_date


@dataclass
class PostResult:
    entry: JournalEntry | None  # None when nothing new was posted
    created: bool
    base_key: str


def ensure_chart(db: Session) -> None:
    """Idempotently seeds the DRAFT chart and posting-rule version."""
    for code, (role, name, category) in acct.ACCOUNTS.items():
        db.execute(
            pg_insert(Account)
            .values(
                id=_new_id(),
                chart_version=acct.CHART_VERSION,
                code=code,
                role=role,
                name=name,
                category=category,
            )
            .on_conflict_do_nothing(index_elements=["chart_version", "code"])
        )
    db.execute(
        pg_insert(PostingRuleVersion)
        .values(
            id=_new_id(),
            version=acct.POSTING_RULE_VERSION,
            chart_version=acct.CHART_VERSION,
            status="DRAFT",
            description=(
                "DRAFT posting map (C02): organizer is collection agent for vendor sales and "
                "recognizes only commission as revenue; tax, tips and customer refunds are "
                "protected liabilities. Not user-approved, not legal/tax advice."
            ),
        )
        .on_conflict_do_nothing(index_elements=["version"])
    )


def _is_closed(db: Session, festival_id: str, business_date: date) -> bool:
    return (
        db.execute(
            select(DailyClose.id).where(
                DailyClose.festival_id == festival_id, DailyClose.business_date == business_date
            )
        ).first()
        is not None
    )


def _family(db: Session, base_key: str) -> list[JournalEntry]:
    return list(
        db.execute(
            select(JournalEntry)
            .where(
                (JournalEntry.entry_key == base_key)
                | JournalEntry.entry_key.like(base_key.replace("%", r"\%") + "#adj%")
            )
            .order_by(JournalEntry.created_at, JournalEntry.entry_key)
        )
        .scalars()
        .all()
    )


def _net_by_account(entries: list[JournalEntry]) -> dict[str, int]:
    net: dict[str, int] = defaultdict(int)
    for e in entries:
        for ln in e.lines:
            net[ln.account_code] += ln.debit_cents - ln.credit_cents
    return net


def post_cumulative(
    db: Session,
    *,
    festival_id: str,
    base_key: str,
    business_date: date,
    kind: str,
    lines: list[acct.Line],
    source_refs: list | None = None,
    source_record_ids: list[str] | None = None,
    correction_of_key: str | None = None,
    note: str | None = None,
    actor_id: str | None,
    actor_capability: str | None,
) -> PostResult:
    """Make the journal family for `base_key` sum to `lines`. See module
    docstring. `lines` may be empty only when correcting an existing family
    back to zero (e.g. a fully credited invoice)."""
    ensure_chart(db)
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": base_key})
    family = _family(db, base_key)

    desired: dict[str, int] = defaultdict(int)
    for ln in lines:
        desired[ln.account_code] += ln.debit_cents - ln.credit_cents
    current = _net_by_account(family)
    delta = [
        (code, desired.get(code, 0) - current.get(code, 0), 0)
        for code in sorted(set(desired) | set(current))
    ]
    delta_lines = acct.normalize(delta)
    if not delta_lines:
        return PostResult(entry=None, created=False, base_key=base_key)
    acct.assert_balanced(delta_lines)

    if not family:
        if _is_closed(db, festival_id, business_date):
            raise BusinessDateClosedError(festival_id, business_date)
        entry_key = base_key
        correction_of = None
        if correction_of_key is not None:
            original = db.execute(
                select(JournalEntry).where(JournalEntry.entry_key == correction_of_key)
            ).scalar_one_or_none()
            correction_of = original.id if original is not None else None
        entry_note = note
    else:
        entry_key = f"{base_key}#adj{len(family)}"
        correction_of = family[0].id
        entry_note = "Linked adjustment to an already-posted fact; original entry untouched."
        if _is_closed(db, festival_id, business_date):
            entry_note += " posted_after_close."
        if note:
            entry_note = f"{note} {entry_note}"

    entry = JournalEntry(
        festival_id=festival_id,
        entry_key=entry_key,
        business_date=business_date,
        kind=kind,
        posting_rule_version=acct.POSTING_RULE_VERSION,
        source_refs=source_refs or [],
        source_record_ids=source_record_ids or [],
        correction_of=correction_of,
        note=entry_note,
        posted_at=datetime.now(UTC),
        posted_by=actor_id,
    )
    entry.lines = [
        JournalLine(
            account_code=ln.account_code,
            account_role=ln.role,
            debit_cents=ln.debit_cents,
            credit_cents=ln.credit_cents,
        )
        for ln in delta_lines
    ]
    db.add(entry)
    db.flush()
    UnitOfWork(db).append_event(
        stream_id=f"journal:{festival_id}",
        event_type="journal.posted",
        aggregate_id=entry.id,
        aggregate_version=1,
        actor_id=actor_id,
        actor_type="human" if actor_id else "system",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        payload={
            "entry_key": entry_key,
            "kind": kind,
            "business_date": business_date.isoformat(),
            "correction_of": correction_of,
            "lines": [
                {"account": ln.account_code, "dr": ln.debit_cents, "cr": ln.credit_cents}
                for ln in delta_lines
            ],
        },
        lineage_refs=[{"ref_kind": "source_record", "ref_id": r} for r in (source_record_ids or [])]
        or None,
    )
    return PostResult(entry=entry, created=True, base_key=base_key)


# ---------------------------------------------------------------------------
# Operational postings: sales/refunds, processor deposits, settlements,
# confirmed vendor payouts
# ---------------------------------------------------------------------------

_CLEARING_METHODS = ("card", "wristband")


def _asset_for(method: str) -> str:
    return "1100" if method.lower() in _CLEARING_METHODS else "1050"


def _alloc(part: int, numerator: int, denominator: int) -> int:
    if denominator == 0:
        return 0
    q = Decimal(part) * Decimal(numerator) / Decimal(denominator)
    return int(q.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def post_sales_for_day(
    db: Session,
    *,
    festival_id: str,
    business_date: date,
    actor_id: str | None,
    actor_capability: str | None,
) -> list[PostResult]:
    """Centrally collected sales net of refunds and chargebacks for one
    business date, one cumulative family per vendor/asset
    ("SALES:<vendor>:<date>:<asset>"). Vendor-retained payments post
    nothing: the organizer never held that money (C02 fixture 4).

    Refunds/chargebacks belong to the original order's business date (the
    same attribution A04's settlement uses). One known at first posting is
    netted into the summary; one recorded later -- even after the day is
    closed -- appends a linked "#adj" entry with exactly its delta, so the
    original posting is never rewritten (C02 fixture 5). Refund tax and
    tips are allocated proportionally from the order (ROUND_HALF_UP)."""
    rows = db.execute(
        select(Payment, Order)
        .join(Order, Payment.order_id == Order.id)
        .join(Vendor, Vendor.id == Payment.vendor_id)
        .where(
            Vendor.festival_id == festival_id,
            Payment.custody == "centrally_collected",
            Order.business_date == business_date,
        )
        .order_by(Payment.created_at, Payment.id)
    ).all()
    by_order: dict[str, list] = defaultdict(list)
    orders: dict[str, Order] = {}
    for payment, order in rows:
        by_order[order.id].append(payment)
        orders[order.id] = order

    summary: dict[tuple[str, str], list] = defaultdict(lambda: [0, 0, 0, 0, [], set(), 0])
    for order_id, payments in by_order.items():
        order = orders[order_id]
        captures = sum(abs(p.amount_cents) for p in payments if p.kind == "capture")
        reversals = sum(abs(p.amount_cents) for p in payments if p.kind != "capture")
        m = acct.split_order_money(
            order_gross_cents=order.gross_cents, captures_cents=captures, reversals_cents=reversals
        )
        g_abs = abs(order.gross_cents)
        tax = _alloc(abs(order.tax_cents), abs(m.goods_cents), g_abs)
        tips = _alloc(abs(order.tips_cents), abs(m.goods_cents), g_abs)
        sign = -1 if m.goods_cents < 0 else 1
        s = summary[(order.vendor_id, _asset_for(payments[0].method))]
        s[0] += m.gross_net_cents
        s[1] += sign * tax
        s[2] += sign * tips
        s[3] += m.customer_money_cents
        s[4].extend(p.id for p in payments)
        s[5].add(order.id)
        s[6] += sum(1 for p in payments if p.kind != "capture")

    results = []
    for (vendor_id, asset), (gross, tax, tips, cust, payment_ids, order_ids, n_adj) in sorted(
        summary.items()
    ):
        results.append(
            post_cumulative(
                db,
                festival_id=festival_id,
                base_key=f"SALES:{vendor_id}:{business_date.isoformat()}:{asset}",
                business_date=business_date,
                kind="SALES_CAPTURE_SUMMARY",
                lines=acct.sales_capture(
                    asset_code=asset,
                    gross_cents=gross,
                    tax_cents=tax,
                    tips_cents=tips,
                    customer_money_cents=cust,
                ),
                source_record_ids=sorted(payment_ids) + sorted(order_ids),
                note=f"Net of {n_adj} refund/chargeback payment(s)." if n_adj else None,
                actor_id=actor_id,
                actor_capability=actor_capability,
            )
        )
    return results


def post_processor_deposits(
    db: Session,
    *,
    festival_id: str,
    business_date: date,
    actor_id: str | None,
    actor_capability: str | None,
) -> list[PostResult]:
    """Processor batch deposits (bank credits carrying batch gross/fee):
    Dr bank net, Dr processor fee expense, Cr processor clearing gross."""
    entries = (
        db.execute(
            select(BankEntry).where(
                BankEntry.festival_id == festival_id,
                BankEntry.business_date == business_date,
                BankEntry.batch_gross_cents.is_not(None),
            )
        )
        .scalars()
        .all()
    )
    results = []
    for be in entries:
        fee = be.batch_fee_cents or 0
        # A net-negative batch (refunds/chargebacks exceeding sales) is a
        # bank DEBIT: the processor pulls money, clearing is debited back.
        bank = be.amount_cents if be.direction == "credit" else -be.amount_cents
        results.append(
            post_cumulative(
                db,
                festival_id=festival_id,
                base_key=f"DEPOSIT:{be.id}",
                business_date=business_date,
                kind="PROCESSOR_SETTLEMENT",
                lines=acct.processor_settlement(
                    bank_cents=bank, fee_cents=fee, gross_cents=be.batch_gross_cents
                ),
                source_refs=[_bank_ref(be)],
                source_record_ids=[be.id],
                actor_id=actor_id,
                actor_capability=actor_capability,
            )
        )
    return results


def _bank_ref(be: BankEntry) -> dict:
    return {
        "source_document_id": be.source_document_id,
        "source_version": be.source_version,
        "content_hash": be.content_hash,
        "locator_kind": be.locator_kind,
        "locator": be.locator,
    }


def post_settlement(
    db: Session, *, settlement: Settlement, actor_id: str | None, actor_capability: str | None
) -> list[PostResult]:
    """Commission (and any contract-permitted processor-fee recharge) for
    one settlement. A correction settlement (`correction_of`) is posted to
    the SAME base key as the settlement it corrects, so only the delta is
    appended as a linked adjustment -- the original posting is untouched."""
    root = settlement
    while root.correction_of is not None:
        parent = db.get(Settlement, root.correction_of)
        if parent is None:
            break
        root = parent
    vendor = db.get(Vendor, settlement.vendor_id)
    festival_id = vendor.festival_id if vendor else settlement.festival_id
    tag = f"{root.vendor_id}:{root.business_date.isoformat()}:{root.id}"
    uncollected = settlement.unpaid_balance_cents + settlement.vendor_fee_receivable_cents
    results = [
        post_cumulative(
            db,
            festival_id=festival_id,
            base_key=f"COMMISSION:{tag}",
            business_date=root.business_date,
            kind="COMMISSION_SETTLEMENT",
            lines=(
                acct.commission_settlement(
                    assessed_cents=settlement.assessed_fee_cents,
                    collected_from_payable_cents=settlement.collected_fee_cents,
                    receivable_cents=uncollected,
                )
                if settlement.assessed_fee_cents
                else []
            ),
            source_record_ids=[settlement.id],
            note=(
                f"From settlement {settlement.id}"
                + (" (correction)" if root is not settlement else "")
            ),
            actor_id=actor_id,
            actor_capability=actor_capability,
        )
    ]
    if (
        settlement.processor_fee_collected_cents
        or db.execute(
            select(JournalEntry.id).where(JournalEntry.entry_key == f"FEE_RECHARGE:{tag}")
        ).first()
    ):
        results.append(
            post_cumulative(
                db,
                festival_id=festival_id,
                base_key=f"FEE_RECHARGE:{tag}",
                business_date=root.business_date,
                kind="PROCESSOR_FEE_RECHARGE",
                lines=(
                    acct.processor_fee_recharge(settlement.processor_fee_collected_cents)
                    if settlement.processor_fee_collected_cents
                    else []
                ),
                source_record_ids=[settlement.id],
                actor_id=actor_id,
                actor_capability=actor_capability,
            )
        )
    return results


def post_settlements_for_day(
    db: Session,
    *,
    festival_id: str,
    business_date: date,
    actor_id: str | None,
    actor_capability: str | None,
) -> list[PostResult]:
    """Posts the latest version of each settlement chain for the date
    (a settlement that has been corrected is posted via its correction)."""
    settlements = (
        db.execute(
            select(Settlement)
            .join(Vendor, Vendor.id == Settlement.vendor_id)
            .where(Vendor.festival_id == festival_id, Settlement.business_date == business_date)
            .order_by(Settlement.created_at)
        )
        .scalars()
        .all()
    )
    superseded = {s.correction_of for s in settlements if s.correction_of}
    results = []
    for s in settlements:
        if s.id in superseded:
            continue
        results.extend(
            post_settlement(db, settlement=s, actor_id=actor_id, actor_capability=actor_capability)
        )
    return results


def post_vendor_payout(
    db: Session, *, payout, actor_id: str | None, actor_capability: str | None
) -> PostResult:
    """Called when a simulated payout is CONFIRMED (A07). Posted on the
    festival-local date of confirmation, not the settlement's business date:
    the 10:00 payout for night N lands on day N+1, after night N may already
    be closed."""
    from app.finance_records import get_or_default_config  # circular at import time

    vendor = db.get(Vendor, payout.vendor_id)
    cfg = get_or_default_config(db, vendor.festival_id)
    confirmed_at = payout.payout_confirmed_at or datetime.now(UTC)
    local_date = confirmed_at.astimezone(ZoneInfo(cfg.timezone)).date()
    return post_cumulative(
        db,
        festival_id=vendor.festival_id,
        base_key=f"PAYOUT:{payout.id}",
        business_date=local_date,
        kind="VENDOR_PAYOUT_SIMULATED",
        lines=acct.vendor_payout(payout.amount_cents),
        source_record_ids=[payout.id] + ([payout.settlement_id] if payout.settlement_id else []),
        note=(
            f"Simulated payout for business date {payout.business_date.isoformat()}, "
            "CONFIRMED. No real money moved."
        ),
        actor_id=actor_id,
        actor_capability=actor_capability,
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def festival_lines(
    db: Session, festival_id: str, *, through: date | None = None
) -> list[acct.Line]:
    q = (
        select(JournalLine.account_code, JournalLine.debit_cents, JournalLine.credit_cents)
        .join(JournalEntry, JournalEntry.id == JournalLine.journal_entry_id)
        .where(JournalEntry.festival_id == festival_id)
    )
    if through is not None:
        q = q.where(JournalEntry.business_date <= through)
    return [acct.Line(c, d, cr) for c, d, cr in db.execute(q).all()]


def festival_balances(db: Session, festival_id: str, *, through: date | None = None) -> dict:
    return acct.balances(festival_lines(db, festival_id, through=through))


def balances_by_role(bal: dict[str, int]) -> dict[str, int]:
    return {acct.ACCOUNTS[code][0]: v for code, v in bal.items()}


def unbalanced_entry_count(db: Session, festival_id: str) -> int:
    sub = (
        select(
            JournalLine.journal_entry_id,
            (func.sum(JournalLine.debit_cents) - func.sum(JournalLine.credit_cents)).label("d"),
        )
        .join(JournalEntry, JournalEntry.id == JournalLine.journal_entry_id)
        .where(JournalEntry.festival_id == festival_id)
        .group_by(JournalLine.journal_entry_id)
        .subquery()
    )
    return db.execute(select(func.count()).select_from(sub).where(sub.c.d != 0)).scalar_one()
