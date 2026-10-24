"""Proves the A02 acceptance criterion: "concurrent appends preserve stream
ordering" -- real concurrency (multiple threads, each with its own
Postgres connection out of a shared connection pool) against the real test
database, not a mock or single-threaded simulation.

Mechanism under test: app.uow.UnitOfWork.append_event takes
`SELECT ... FOR UPDATE` on the stream's StreamHead row before computing the
next sequence/hash, so Postgres's row lock serializes concurrent appenders
to the SAME stream_id. This test fires N concurrent appends at one stream
and checks: no two events got the same sequence, no sequence is skipped,
and the hash chain is valid along the resulting order.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

from app.models_ledger import BusinessEvent
from app.uow import GENESIS_HASH, UnitOfWork
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

N_CONCURRENT_APPENDS = 25


def _append_one(session_factory, stream_id: str, i: int) -> str | None:
    db = session_factory()
    try:
        uow = UnitOfWork(db)
        uow.append_event(
            stream_id=stream_id,
            event_type="test.concurrent_append",
            aggregate_id="concurrency-test-aggregate",
            aggregate_version=i,
            actor_id=None,
            actor_type="system",
            occurred_at=datetime.now(UTC),
            payload={"worker_index": i},
        )
        db.commit()
        return None
    except Exception as exc:  # noqa: BLE001 - surfaced to the test, not swallowed
        db.rollback()
        return f"worker {i} failed: {exc!r}"
    finally:
        db.close()


def test_concurrent_appends_preserve_stream_ordering_and_hash_chain(engine, db_session):
    # `db_session` is requested (but not used directly) purely so this
    # test's rows are truncated by conftest's cleanup like every other
    # test -- the actual concurrency exercise below uses its own sessions.
    stream_id = f"test-stream-{uuid.uuid4().hex}"
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=N_CONCURRENT_APPENDS) as pool:
        futures = [
            pool.submit(_append_one, session_factory, stream_id, i)
            for i in range(N_CONCURRENT_APPENDS)
        ]
        for future in as_completed(futures):
            result = future.result()
            if result:
                errors.append(result)

    assert errors == [], f"concurrent appenders hit errors: {errors}"

    check_db = session_factory()
    try:
        events = (
            check_db.execute(
                select(BusinessEvent)
                .where(BusinessEvent.stream_id == stream_id)
                .order_by(BusinessEvent.sequence.asc())
            )
            .scalars()
            .all()
        )

        assert len(events) == N_CONCURRENT_APPENDS, (
            "expected exactly one event per concurrent appender -- a lost or "
            "duplicated sequence number means the row lock failed to "
            "serialize appenders"
        )

        sequences = [e.sequence for e in events]
        assert sequences == list(
            range(1, N_CONCURRENT_APPENDS + 1)
        ), f"sequence numbers are not a contiguous 1..N run: {sequences}"

        expected_previous = GENESIS_HASH
        for event in events:
            assert event.previous_hash == expected_previous, (
                f"hash chain broken at sequence {event.sequence}: "
                f"expected previous_hash={expected_previous!r}, got {event.previous_hash!r}"
            )
            expected_previous = event.hash
    finally:
        check_db.close()
