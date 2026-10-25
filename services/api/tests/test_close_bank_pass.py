"""The nightly close's festival-level bank pass (`reconcile_bank_deposits_day`).

Two defects found running the close on the generated festival:

1. Night D's processor batch is always dated D+1, and the pass only read bank
   entries dated on or before D. Every card sale stayed `pending` on its own
   night, the night settled on its refunds alone, and a refund-only total
   crashed the terms step. The pass now reads entries up to the day the close
   actually runs (the demo clock here).
2. Every bank line was classified as a vendor deposit, so a sponsor wire or a
   payroll debit -- no batch, no card payments -- opened an "unexplained
   variance" case for its whole amount.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app import demo_clock
from app.ingestion import import_source
from app.models_ledger import Case, Order, Payment, Run, Vendor
from app.models_reconciliation import BankEntry, MatchGroup, MatchGroupMember
from app.orchestration.reconcile_day import reconcile_bank_deposits_day
from app.storage.local_fs import get_document_storage
from sqlalchemy import select

FESTIVAL = "fest-bank-pass"
NIGHT = date(2026, 10, 16)


def _seed(db) -> dict:
    src = import_source(
        db,
        get_document_storage(),
        source_system="test_bank_pass",
        source_native_id="manifest",
        content_bytes=b'{"fixture": "bank pass"}',
        mime_type="application/json",
        actor_id="test",
        actor_capability="import_source",
    )
    ref = {
        "source_document_id": src.source_document_id,
        "source_version": src.version,
        "content_hash": src.content_hash,
        "locator_kind": "row",
    }
    vendor = Vendor(festival_id=FESTIVAL, name="Taco Libre")
    db.add(vendor)
    db.flush()
    order = Order(
        vendor_id=vendor.id,
        source_system="test_bank_pass",
        source_native_id="o1",
        business_date=NIGHT,
        ordered_at=datetime(2026, 10, 17, 3, 0, tzinfo=UTC),
        gross_cents=10_000,
        locator="order:o1",
        **ref,
    )
    db.add(order)
    db.flush()
    # The card processor pays night 1 out the next day, net of a $3 fee.
    batch = BankEntry(
        festival_id=FESTIVAL,
        account_id="operating",
        provider_reference="CARDCO-PO-0001",
        direction="credit",
        amount_cents=9_700,
        batch_gross_cents=10_000,
        batch_fee_cents=300,
        settlement_status="settled",
        business_date=date(2026, 10, 17),
        occurred_at=datetime(2026, 10, 17, 18, 0, tzinfo=UTC),
    )
    # A sponsor wire: real money, not a vendor deposit.
    wire = BankEntry(
        festival_id=FESTIVAL,
        account_id="operating",
        provider_reference="WIRE-SPONSOR-1",
        direction="credit",
        amount_cents=1_500_000,
        settlement_status="settled",
        business_date=date(2026, 10, 14),
        occurred_at=datetime(2026, 10, 14, 18, 0, tzinfo=UTC),
    )
    db.add_all([batch, wire])
    db.flush()
    db.add(
        Payment(
            vendor_id=vendor.id,
            order_id=order.id,
            source_system="test_bank_pass",
            source_native_id="p1",
            method="card",
            custody="central",
            amount_cents=10_000,
            kind="capture",
            bank_entry_id=batch.id,
        )
    )
    run = Run(festival_id=FESTIVAL, status="RUNNING")
    db.add(run)
    db.flush()
    return {"run": run, "batch": batch, "wire": wire}


def _close_night_1(db, seeded: dict, clock_at: datetime) -> None:
    demo_clock.freeze_at(db, clock_at, actor="test")
    reconcile_bank_deposits_day(
        db,
        run=seeded["run"],
        festival_id=FESTIVAL,
        business_date=NIGHT,
        actor_id="test",
        actor_capability="run_reconciliation",
    )
    db.flush()


def _groups_for(db, entry: BankEntry) -> list[MatchGroup]:
    return (
        db.execute(
            select(MatchGroup)
            .join(MatchGroupMember, MatchGroupMember.match_group_id == MatchGroup.id)
            .where(
                MatchGroup.match_type == "bank_deposit",
                MatchGroupMember.member_kind == "bank_entry",
                MatchGroupMember.member_id == entry.id,
            )
        )
        .scalars()
        .all()
    )


def test_close_next_morning_counts_the_batch_that_has_landed(db_session):
    seeded = _seed(db_session)
    _close_night_1(db_session, seeded, datetime(2026, 10, 17, 16, 0, tzinfo=UTC))  # 9 AM PT

    groups = _groups_for(db_session, seeded["batch"])
    assert [g.status for g in groups] == ["matched"]


def test_close_before_the_batch_lands_leaves_it_out(db_session):
    seeded = _seed(db_session)
    _close_night_1(db_session, seeded, datetime(2026, 10, 17, 6, 30, tzinfo=UTC))  # 11:30 PM PT

    assert _groups_for(db_session, seeded["batch"]) == []


def test_non_processor_bank_line_is_not_a_vendor_deposit(db_session):
    seeded = _seed(db_session)
    _close_night_1(db_session, seeded, datetime(2026, 10, 17, 16, 0, tzinfo=UTC))

    assert _groups_for(db_session, seeded["wire"]) == []
    cases = db_session.execute(
        select(Case).where(Case.reason_code == "bank_deposit_variance_unexplained")
    ).scalars()
    assert list(cases) == []
