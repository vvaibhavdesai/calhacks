"""Proves the A02 acceptance criterion: "a domain update and its event/
outbox are atomic" -- against the real Postgres test database (db_session
fixture), not a mock. One test forces a rollback mid-transaction and shows
nothing partial lands; the paired test shows everything lands together on
success, so the first test isn't just "nothing ever gets written"."""

from __future__ import annotations

from datetime import UTC, datetime

from app.models_ledger import BusinessEvent, OutboxIntent, StreamHead, Vendor
from app.uow import UnitOfWork


def test_failed_transaction_leaves_nothing_committed(db_session):
    stream_id = "vendor:atomicity-rollback-test"
    vendor = Vendor(festival_id="gcmf-2026", name="Atomicity Rollback Vendor")
    db_session.add(vendor)
    db_session.flush()  # id assigned, but nothing committed yet

    uow = UnitOfWork(db_session)
    uow.append_event(
        stream_id=stream_id,
        event_type="vendor.test",
        aggregate_id=vendor.id,
        aggregate_version=1,
        actor_id=None,
        actor_type="system",
        occurred_at=datetime.now(UTC),
        payload={"note": "about to fail before commit"},
    )
    uow.enqueue_outbox(
        idempotency_key=f"test-rollback:{vendor.id}",
        intent_type="vendor.test.notify",
        target="internal",
        payload={},
    )

    # Simulate the failure the acceptance criterion asks for: something goes
    # wrong before the single commit point, so the whole transaction rolls
    # back, exactly as it would if an exception had propagated out of a real
    # endpoint before its one `db.commit()` call.
    db_session.rollback()

    assert db_session.get(Vendor, vendor.id) is None
    assert db_session.query(BusinessEvent).filter_by(stream_id=stream_id).count() == 0
    assert db_session.get(StreamHead, stream_id) is None
    assert (
        db_session.query(OutboxIntent)
        .filter_by(idempotency_key=f"test-rollback:{vendor.id}")
        .count()
        == 0
    )


def test_successful_transaction_commits_everything_together(db_session):
    stream_id = "vendor:atomicity-success-test"
    vendor = Vendor(festival_id="gcmf-2026", name="Atomicity Success Vendor")
    db_session.add(vendor)
    db_session.flush()

    uow = UnitOfWork(db_session)
    event = uow.append_event(
        stream_id=stream_id,
        event_type="vendor.test",
        aggregate_id=vendor.id,
        aggregate_version=1,
        actor_id=None,
        actor_type="system",
        occurred_at=datetime.now(UTC),
        payload={"note": "should land"},
    )
    uow.enqueue_outbox(
        idempotency_key=f"test-success:{vendor.id}",
        intent_type="vendor.test.notify",
        target="internal",
        payload={},
        event_id=event.event_id,
    )
    db_session.commit()

    assert db_session.get(Vendor, vendor.id) is not None
    assert db_session.query(BusinessEvent).filter_by(stream_id=stream_id).count() == 1
    assert db_session.get(StreamHead, stream_id) is not None
    assert (
        db_session.query(OutboxIntent)
        .filter_by(idempotency_key=f"test-success:{vendor.id}")
        .count()
        == 1
    )
