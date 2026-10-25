"""Day-scoped, incremental driver for A03's reconciliation engine.

`app.reconciliation.reconcile_vendor` matches EVERY order the vendor has
ever had and records a fresh match group (and, for discrepancies, a fresh
OPEN case) each time it runs. That is correct for its one-shot endpoint,
but a nightly close that runs on night 2 (or re-runs after a crash) would
re-open night 1's already-handled cases and re-state every night's orders
whether or not anything about them had changed.

This module calls the SAME deterministic pieces -- `classify_order_capture`,
`classify_cash_count`, `classify_bank_deposit` and `_record_match_group`
(which opens the case and appends the audit events) -- but only for records
of the run's business date, and only when their current classification is
not already the one the current inputs produce. It never decides money
itself and never commits; the caller's `run_once` does.

Re-classification rule
----------------------
"Already has a match group" is not a safe reason to skip a record. A group
is a statement about the inputs as they stood when it was written, so
skipping on existence alone froze the first statement for ever: a refund
booked after the night's first close never reached A04's eligible sales
(the stale group stayed the order's current one), and an order whose
discrepancy had since been fixed in the data stayed held by its OPEN case
for ever.

So every record of the day is classified from its CURRENT inputs on every
pass, and the outcome is compared with the record's current group -- the
newest group of that match type containing it, which is exactly the row
A04 settles from (`app/settlement.py`'s `_latest_order_capture_groups`,
and its bank-entry read, both take the newest). A new group is written
only when something actually differs: the classification (status, reason
code, variance, matched amount) or the member set it was computed from (a
late refund payment, a corrected amount, a bank entry that has since
settled). Inputs unchanged -> nothing written, so re-running a night any
number of times still leaves exactly one group per record and never a
second case for the same discrepancy.

Match groups are append-only history, as everywhere else in this codebase:
a re-classification SUPERSEDES by appending a NEWER group and never edits
the one it replaces, which is precisely what A04's newest-group-per-order
read then picks up, with no change needed on its side. When the superseded
group had opened a case that is still OPEN, that case is auto-resolved as
no longer applicable, citing the superseding group (see
`_supersede_case`) -- otherwise the vendor stays held by a discrepancy the
data no longer shows, and a human is asked to decide an already-dead case.
Cases a human (or a policy) has already resolved are never touched.

This also closes the limitation the earlier handoff recorded here: a bank
deposit classified `pending` IS re-evaluated now, and becomes `matched`
(or, past the timing window, an overdue case) through a superseding group.

Writers for one vendor/day -- festival/day for the bank pass -- serialize
on a transaction-scoped advisory lock, the house pattern for these tables
(`SELECT ... FOR UPDATE` needs an UPDATE privilege the `encore_app` role
does not have on append-only history), so two concurrent runs cannot both
decide to supersede the same group.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import reconciliation as a03
from app.models_ledger import Case, Order, Payment, Run, Vendor
from app.models_reconciliation import BankEntry, CashCount, MatchGroup, MatchGroupMember
from app.uow import UnitOfWork


@dataclass
class DayReconciliation:
    order_groups: int = 0
    cash_groups: int = 0
    # Records whose current group already says what the current inputs say,
    # so this pass left it standing (the name is kept: it is what the
    # orchestration_steps ledger and the verification scripts already read).
    skipped_already_matched: int = 0
    # Of `order_groups` + `cash_groups`, how many superseded an earlier
    # group rather than being a record's first classification.
    reclassified: int = 0
    # Cases this pass auto-resolved because the superseded group's
    # discrepancy is no longer in the data (see `_supersede_case`).
    resolved_case_ids: list[str] = field(default_factory=list)
    case_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "order_groups": self.order_groups,
            "cash_groups": self.cash_groups,
            "skipped_already_matched": self.skipped_already_matched,
            "reclassified": self.reclassified,
            "resolved_case_ids": self.resolved_case_ids,
            "case_ids": self.case_ids,
        }


def _lock(db: Session, key: str) -> None:
    # `match_groups`/`match_group_members` are append-only history and
    # SELECT ... FOR UPDATE needs UPDATE privilege, so the read-decide-append
    # sequence below serializes on a transaction-scoped advisory lock
    # instead -- the same pattern app/accounting.py and app/cash_office.py
    # use. Released when the caller's `run_once` commits or rolls back.
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": key})


def _current_group(
    db: Session, match_type: str, member_kind: str, member_id: str
) -> MatchGroup | None:
    """This record's CURRENT classification: the newest group of
    `match_type` that contains it, or None if it has never been classified.

    Newest by (`created_at`, `id`) -- deliberately the same ordering
    app/settlement.py uses for the group it settles from, so "current" here
    and "current" there are always the same row. `run_id` is not involved:
    it is nullable (hand-built dev fixtures, ON DELETE SET NULL), and a
    record's latest classification is its latest one whichever run wrote
    it."""
    return (
        db.execute(
            select(MatchGroup)
            .join(MatchGroupMember, MatchGroupMember.match_group_id == MatchGroup.id)
            .where(
                MatchGroup.match_type == match_type,
                MatchGroupMember.member_kind == member_kind,
                MatchGroupMember.member_id == member_id,
            )
            .order_by(MatchGroup.created_at.desc(), MatchGroup.id.desc())
        )
        .scalars()
        .first()
    )


def _members_of(db: Session, group: MatchGroup) -> list[tuple[str, str, str, int]]:
    return [
        (m.member_kind, m.member_id, m.role, m.amount_cents)
        for m in db.execute(
            select(MatchGroupMember).where(MatchGroupMember.match_group_id == group.id)
        )
        .scalars()
        .all()
    ]


# Postgres accepts at most 65535 bind parameters per statement, so an `IN`
# over a whole festival night's orders is chunked -- same constant and reason
# as app/settlement.py's `_IN_CHUNK`.
_IN_CHUNK = 5_000


def _chunks(values: list[str], size: int = _IN_CHUNK):
    for i in range(0, len(values), size):
        yield values[i : i + size]


@dataclass
class _OrderRows:
    """Everything the per-order loop below reads, loaded in bulk.

    Keyed exactly the way the loop looks things up, so the loop's logic is
    unchanged -- it reads from dictionaries instead of issuing three queries
    per order (the order's payments, its current classification, and that
    group's members). At 40,000 orders that was ~120,000 round trips a run.
    """

    payments_by_order: dict[str, list[Payment]] = field(default_factory=dict)
    current_group_by_order: dict[str, MatchGroup] = field(default_factory=dict)
    members_by_group: dict[str, list[tuple[str, str, str, int]]] = field(default_factory=dict)


def _load_order_rows(db: Session, orders: list[Order]) -> _OrderRows:
    rows = _OrderRows()
    order_ids = [o.id for o in orders]
    if not order_ids:
        return rows

    for chunk in _chunks(order_ids):
        for p in (
            db.execute(select(Payment).where(Payment.order_id.in_(chunk)).order_by(Payment.id))
            .scalars()
            .all()
        ):
            rows.payments_by_order.setdefault(p.order_id, []).append(p)

        # Newest order_capture group per order: ordered so the FIRST row seen
        # for an order is its newest, matching the per-order `_current_group`
        # query this replaces (created_at desc, then id desc).
        for member_id, group in db.execute(
            select(MatchGroupMember.member_id, MatchGroup)
            .join(MatchGroup, MatchGroup.id == MatchGroupMember.match_group_id)
            .where(
                MatchGroup.match_type == "order_capture",
                MatchGroupMember.member_kind == "order",
                MatchGroupMember.member_id.in_(chunk),
            )
            .order_by(
                MatchGroupMember.member_id.asc(),
                MatchGroup.created_at.desc(),
                MatchGroup.id.desc(),
            )
        ):
            rows.current_group_by_order.setdefault(member_id, group)

    current_ids = sorted({g.id for g in rows.current_group_by_order.values()})
    for chunk in _chunks(current_ids):
        for m in (
            db.execute(select(MatchGroupMember).where(MatchGroupMember.match_group_id.in_(chunk)))
            .scalars()
            .all()
        ):
            rows.members_by_group.setdefault(m.match_group_id, []).append(
                (m.member_kind, m.member_id, m.role, m.amount_cents)
            )
    return rows


def _is_still_current(
    db: Session,
    group: MatchGroup,
    result: a03.ClassificationResult,
    members: list[tuple[str, str, str, int]],
    existing_members: list[tuple[str, str, str, int]] | None = None,
) -> bool:
    """True when `group` already records exactly what the current inputs
    classify to, so there is nothing to supersede.

    Both halves matter. The classification is the part A04 settles and the
    part that holds a vendor, so a changed status/reason/variance/matched
    amount must produce a new group. The member set is the part a human (or
    A09's lineage) drills into: a group whose members no longer are the
    records that exist cannot be left standing as the current evidence even
    when the four amounts happen to come out the same -- e.g. one capture
    replaced by two summing to it. Member order is not significant (the
    payment queries behind these lists are not all ordered), so compare
    sorted.

    `existing_members` lets a caller that has already loaded this group's
    members in bulk pass them in rather than have one query issued per
    record here."""
    if (
        group.status != result.status
        or group.reason_code != result.reason_code
        or group.variance_cents != result.variance_cents
        or group.matched_amount_cents != result.matched_amount_cents
    ):
        return False
    have = _members_of(db, group) if existing_members is None else existing_members
    return sorted(have) == sorted(members)


def _supersede_case(
    db: Session,
    uow: UnitOfWork,
    *,
    run: Run,
    superseded: MatchGroup,
    superseding: MatchGroup,
    actor_id: str,
    actor_capability: str,
) -> str | None:
    """Auto-resolve the case the superseded group opened, when it is still
    OPEN and a fresh classification of the same records has replaced it.
    Returns the case id, or None when there was nothing to resolve.

    Without this, a discrepancy that has since been fixed in the data holds
    the vendor for ever (`open_cases_for_vendor` -> the graph's
    check_controls node) and a human is eventually asked to decide a case
    whose evidence no longer reconstructs. The resolution is never silent:
    `auto_resolved` + the decision code + a `case.auto_resolved` event whose
    lineage cites both groups, exactly as app/payables.py resolves an
    INVOICE_EXCEEDS_PO case that no longer exceeds and app/policy.py
    resolves a conflict a policy already answers.

    Only the case THIS detection opened for THIS group is touched, and only
    while it is OPEN -- a case a human or a policy already decided keeps its
    decision, and a case that belongs to another detector is none of this
    pass's business. `resolved_by` stays NULL: no person acted here, the
    data changed (models_ledger.Case: never fabricate an approver)."""
    if superseded.case_id is None:
        return None
    case = db.get(Case, superseded.case_id)
    if case is None or case.status != "OPEN":
        return None

    case.status = "RESOLVED"
    case.auto_resolved = True
    case.resolution_decision = "NO_LONGER_APPLICABLE"
    case.resolution_note = (
        f"re-classification of the same records superseded match group "
        f"{superseded.id} ({superseded.reason_code}) with {superseding.id} "
        f"({superseding.reason_code}, status={superseding.status}); the discrepancy "
        f"this case was opened for is no longer in the data"
    )
    # No money effect: this resolution applies nothing, it records that the
    # finding went away. Any new finding is its own case on the new group.
    case.resolution_effect = None
    case.resolved_by = None
    case.resolved_capability = None
    case.resolved_at = datetime.now(UTC)
    case.version += 1
    db.flush()

    uow.append_event(
        stream_id=f"case:{case.id}",
        event_type="case.auto_resolved",
        aggregate_id=case.id,
        aggregate_version=case.version,
        actor_id=actor_id,
        actor_type="system",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        run_id=run.id,
        vendor_id=case.vendor_id,
        payload={
            "reason_code": case.reason_code,
            "resolution_decision": case.resolution_decision,
            "resolution_note": case.resolution_note,
            "superseded_match_group_id": superseded.id,
            "superseding_match_group_id": superseding.id,
            "superseding_status": superseding.status,
            "superseding_reason_code": superseding.reason_code,
            "superseding_case_id": superseding.case_id,
        },
        # Both groups, so the chain reads case -> the finding that opened it
        # -> the finding that replaced it. Which is which is in the payload:
        # EventLineage stores only (ref_kind, ref_id, ref_version).
        lineage_refs=[
            {"ref_kind": "case", "ref_id": case.id},
            {"ref_kind": "match_group", "ref_id": superseded.id},
            {"ref_kind": "match_group", "ref_id": superseding.id},
        ],
    )
    return case.id


def _resolve_stranded_cases(
    db: Session,
    uow: UnitOfWork,
    *,
    run: Run,
    match_type: str,
    member_kind: str,
    current_group_by_member: dict[str, MatchGroup],
    actor_id: str,
    actor_capability: str,
) -> list[str]:
    """Resolve OPEN cases left behind by classifications that are no longer
    current -- not just the one generation `_supersede_case` replaced.

    `_supersede_case` resolves the case of the group it directly replaces,
    which is correct and sufficient when every re-classification goes
    through this pass. It is not sufficient in general: a record can be
    re-classified by a path that did not pair the two groups (a different
    code path, a pass interrupted between recording the new group and
    resolving the old case), and `_is_still_current` deliberately skips a
    record whose classification has not changed, so no pairing happens then
    either. Each such hop can leave a case attached to a group that is no
    longer the record's classification.

    That is not a harmless orphan. `release_payout` refuses to pay a vendor
    with ANY open blocking case, and the nightly graph's check_controls
    holds the vendor, so a case whose finding is gone from the data
    withholds the vendor's money for ever -- the exact outcome
    `_supersede_case`'s docstring says this must not produce. On the scored
    festival it stranded 693 such cases across 59 of the 80 vendors; the
    current classification of the records behind 681 of them is `matched`.

    This sweeps per record, from the current classification backwards: any
    group for the same record other than its current one is superseded by
    definition, so a still-OPEN case it opened is stale. A case a human or
    a policy decided is untouched (status is no longer OPEN), and a case on
    the CURRENT group is untouched -- that finding is live.
    """
    if not current_group_by_member:
        return []
    resolved: list[str] = []
    member_ids = list(current_group_by_member)
    for chunk in _chunks(member_ids):
        rows = db.execute(
            select(MatchGroupMember.member_id, MatchGroup)
            .join(MatchGroup, MatchGroup.id == MatchGroupMember.match_group_id)
            .where(
                MatchGroup.match_type == match_type,
                MatchGroup.case_id.is_not(None),
                MatchGroupMember.member_kind == member_kind,
                MatchGroupMember.member_id.in_(chunk),
            )
        ).all()
        for member_id, group in rows:
            current = current_group_by_member.get(member_id)
            if current is None or group.id == current.id:
                continue
            case_id = _supersede_case(
                db,
                uow,
                run=run,
                superseded=group,
                superseding=current,
                actor_id=actor_id,
                actor_capability=actor_capability,
            )
            if case_id:
                resolved.append(case_id)
    return resolved


def reconcile_vendor_day(
    db: Session,
    *,
    run: Run,
    vendor: Vendor,
    business_date: date,
    actor_id: str,
    actor_capability: str,
) -> DayReconciliation:
    uow = UnitOfWork(db)
    out = DayReconciliation()
    _lock(db, f"reconcile_day:{vendor.id}:{business_date.isoformat()}")
    orders = (
        db.execute(
            select(Order)
            .where(
                Order.vendor_id == vendor.id,
                Order.status == "active",
                Order.business_date == business_date,
            )
            .order_by(Order.source_native_id)
        )
        .scalars()
        .all()
    )
    # Everything the loop below reads, fetched up front in a handful of
    # queries instead of three per order. The arithmetic is unchanged --
    # only where the rows come from.
    batch = _load_order_rows(db, list(orders))
    # The classification each order ends this pass with: whatever was
    # current coming in, replaced by anything recorded below. Fed to
    # `_resolve_stranded_cases` after the loop.
    current_after: dict[str, MatchGroup] = dict(batch.current_group_by_order)

    for order in orders:
        payments = batch.payments_by_order.get(order.id, [])
        facts = [
            a03.PaymentFact(id=p.id, amount_cents=p.amount_cents, kind=p.kind) for p in payments
        ]
        # `matched_amount_cents` here is the order's captures NET of every
        # refund/chargeback booked against it, so a refund that lands after
        # the night's first close changes this result and therefore lands a
        # superseding group -- which is how it reaches A04's eligible sales.
        result = a03.classify_order_capture(order.gross_cents, facts)
        members = [("order", order.id, "expected", order.gross_cents)] + [
            ("payment", p.id, p.kind, p.amount_cents) for p in payments
        ]
        current = batch.current_group_by_order.get(order.id)
        if current is not None and _is_still_current(
            db,
            current,
            result,
            members,
            existing_members=batch.members_by_group.get(current.id, []),
        ):
            out.skipped_already_matched += 1
            continue
        group = a03._record_match_group(
            db,
            uow,
            run=run,
            festival_id=vendor.festival_id,
            vendor_id=vendor.id,
            match_type="order_capture",
            business_date_=order.business_date,
            result=result,
            members=members,
            actor_id=actor_id,
            actor_capability=actor_capability,
        )
        out.order_groups += 1
        current_after[order.id] = group
        if group.case_id:
            out.case_ids.append(group.case_id)
        if current is not None:
            out.reclassified += 1
            resolved = _supersede_case(
                db,
                uow,
                run=run,
                superseded=current,
                superseding=group,
                actor_id=actor_id,
                actor_capability=actor_capability,
            )
            if resolved:
                out.resolved_case_ids.append(resolved)

    # Cases left open by any EARLIER classification of these same orders,
    # not just the one generation replaced above. See
    # `_resolve_stranded_cases`: one of these withholds the vendor's money
    # for ever.
    out.resolved_case_ids.extend(
        _resolve_stranded_cases(
            db,
            uow,
            run=run,
            match_type="order_capture",
            member_kind="order",
            current_group_by_member=current_after,
            actor_id=actor_id,
            actor_capability=actor_capability,
        )
    )

    counts = (
        db.execute(
            select(CashCount).where(
                CashCount.vendor_id == vendor.id, CashCount.business_date == business_date
            )
        )
        .scalars()
        .all()
    )
    for count in counts:
        vendor_payments = (
            db.execute(
                select(Payment)
                .join(Order, Order.id == Payment.order_id)
                .where(
                    Payment.vendor_id == vendor.id,
                    Payment.custody == "vendor_retained",
                    Order.business_date == count.business_date,
                )
            )
            .scalars()
            .all()
        )
        captured = sum(p.amount_cents for p in vendor_payments if p.kind == "capture")
        refunded = sum(
            p.amount_cents for p in vendor_payments if p.kind in ("refund", "chargeback")
        )
        result = a03.classify_cash_count(captured - refunded, count.counted_cents)
        members = [("cash_count", count.id, "count", count.counted_cents)] + [
            ("payment", p.id, p.kind, p.amount_cents) for p in vendor_payments
        ]
        current = _current_group(db, "cash_count", "cash_count", count.id)
        if current is not None and _is_still_current(db, current, result, members):
            out.skipped_already_matched += 1
            continue
        group = a03._record_match_group(
            db,
            uow,
            run=run,
            festival_id=vendor.festival_id,
            vendor_id=vendor.id,
            match_type="cash_count",
            business_date_=count.business_date,
            result=result,
            members=members,
            actor_id=actor_id,
            actor_capability=actor_capability,
        )
        out.cash_groups += 1
        if group.case_id:
            out.case_ids.append(group.case_id)
        if current is not None:
            out.reclassified += 1
            resolved = _supersede_case(
                db,
                uow,
                run=run,
                superseded=current,
                superseding=group,
                actor_id=actor_id,
                actor_capability=actor_capability,
            )
            if resolved:
                out.resolved_case_ids.append(resolved)
    return out


def reconcile_bank_deposits_day(
    db: Session,
    *,
    run: Run,
    festival_id: str,
    business_date: date,
    actor_id: str,
    actor_capability: str,
) -> dict:
    """Festival-level pass (not vendor-scoped, same reason as A03): bank
    entries dated on or before the business date whose current
    `bank_deposit` classification is not the one they classify to now.

    Entries from earlier days are deliberately in range: a deposit that was
    `pending` on night 1 and has settled since re-classifies to `matched`
    here on night 2 (and past the timing window to an overdue case), which
    is what releases the orders A04 held on it."""
    uow = UnitOfWork(db)
    _lock(db, f"reconcile_day:bank:{festival_id}:{business_date.isoformat()}")
    entries = (
        db.execute(
            select(BankEntry)
            .where(BankEntry.festival_id == festival_id, BankEntry.business_date <= business_date)
            .order_by(BankEntry.provider_reference)
        )
        .scalars()
        .all()
    )
    groups, case_ids, resolved_case_ids, skipped, reclassified = 0, [], [], 0, 0
    for entry in entries:
        payments = (
            db.execute(select(Payment).where(Payment.bank_entry_id == entry.id)).scalars().all()
        )
        order_dates = (
            db.execute(
                select(Order.business_date)
                .join(Payment, Payment.order_id == Order.id)
                .where(Payment.bank_entry_id == entry.id)
            )
            .scalars()
            .all()
        )
        result = a03.classify_bank_deposit(
            captures_total_cents=sum(p.amount_cents for p in payments if p.kind == "capture"),
            # Refunds and chargebacks the processor took back out of this
            # same batch: the deposit lands net of them, so A03 has to
            # expect it net of them too.
            adjustments_total_cents=sum(
                p.amount_cents for p in payments if p.kind in ("refund", "chargeback")
            ),
            entry_amount_cents=entry.amount_cents,
            direction=entry.direction,
            batch_fee_cents=entry.batch_fee_cents,
            settlement_status=entry.settlement_status,
            earliest_payment_business_date=min(order_dates) if order_dates else entry.business_date,
            as_of=business_date,
        )
        members = [("bank_entry", entry.id, "deposit", entry.amount_cents)] + [
            ("payment", p.id, p.kind, p.amount_cents) for p in payments
        ]
        current = _current_group(db, "bank_deposit", "bank_entry", entry.id)
        if current is not None and _is_still_current(db, current, result, members):
            skipped += 1
            continue
        group = a03._record_match_group(
            db,
            uow,
            run=run,
            festival_id=festival_id,
            vendor_id=None,
            match_type="bank_deposit",
            business_date_=entry.business_date,
            result=result,
            members=members,
            actor_id=actor_id,
            actor_capability=actor_capability,
        )
        groups += 1
        if group.case_id:
            case_ids.append(group.case_id)
        if current is not None:
            reclassified += 1
            resolved = _supersede_case(
                db,
                uow,
                run=run,
                superseded=current,
                superseding=group,
                actor_id=actor_id,
                actor_capability=actor_capability,
            )
            if resolved:
                resolved_case_ids.append(resolved)
    return {
        "bank_deposit_groups": groups,
        "case_ids": case_ids,
        "resolved_case_ids": resolved_case_ids,
        "skipped_already_matched": skipped,
        "reclassified": reclassified,
    }


def open_cases_for_vendor(db: Session, vendor_id: str) -> list[Case]:
    """The OPEN cases that hold this vendor's money at CONTROLS_CHECKED.
    `Case.blocking` defaults to true, so this is every case kind that has
    ever held a vendor; A06 stage 2's `minimum_period_claim` is the one
    deliberately non-blocking kind -- the signed contract already decides
    the night, so the open question is visible without holding the money
    (the same distinction A07's `release_payout` makes)."""
    return list(
        db.execute(
            select(Case)
            .where(
                Case.vendor_id == vendor_id,
                Case.status == "OPEN",
                Case.blocking.is_(True),
            )
            .order_by(Case.created_at)
        )
        .scalars()
        .all()
    )
