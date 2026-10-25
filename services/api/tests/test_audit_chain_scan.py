"""Locks the shape of `verify_chain`'s reads.

It used to `SELECT DISTINCT stream_id` and then run one
`WHERE stream_id = ?` query per id, and hand that same list of ids to
`head_consistency` as `stream_id IN (...)`. On the demo festival -- 202,478
streams, 202,377 of them holding exactly one event -- that was 202,478
round-trips followed by an outright failure: Postgres takes at most 65,535
bind parameters, so the endpoint answered HTTP 500.

Neither symptom shows up in a small fixture, because 20 streams is 20 cheap
queries and 20 bind parameters. So these tests assert the SHAPE of the work
-- how many statements, how many parameters -- rather than how long it
takes, and they fail deterministically on the old implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.audit_chain import verify_chain
from app.uow import UnitOfWork
from sqlalchemy import event


def _append(uow: UnitOfWork, stream_id: str, aggregate_id: str, version: int) -> None:
    uow.append_event(
        stream_id=stream_id,
        event_type="case.opened",
        aggregate_id=aggregate_id,
        aggregate_version=version,
        actor_id=None,
        actor_type="SYSTEM",
        occurred_at=datetime.now(UTC),
        payload={"reason_code": "SCAN_TEST"},
    )


def _seed(db_session, streams: int, per_stream: int = 1, first: int = 0) -> list[str]:
    """One event in each of `streams` streams -- the demo data's shape, where
    nearly every stream is a single `match_group:*` event."""
    uow = UnitOfWork(db_session)
    ids = []
    for s in range(first, first + streams):
        stream_id = f"scantest_group:{s:05d}"
        ids.append(stream_id)
        for v in range(1, per_stream + 1):
            _append(uow, stream_id, f"agg-{s:05d}", v)
    db_session.commit()
    return ids


class _Counter:
    """Counts statements and the largest parameter set sent on a connection."""

    def __init__(self) -> None:
        self.statements = 0
        self.max_params = 0

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        self.statements += 1
        if isinstance(parameters, dict):
            self.max_params = max(self.max_params, len(parameters))
        elif isinstance(parameters, list | tuple):
            self.max_params = max(self.max_params, len(parameters))


def _count(db_session, fn):
    counter = _Counter()
    conn = db_session.connection()
    event.listen(conn.engine, "before_cursor_execute", counter)
    try:
        result = fn()
    finally:
        event.remove(conn.engine, "before_cursor_execute", counter)
    return result, counter


def test_verify_chain_does_not_query_per_stream(db_session):
    """The number of statements must not grow with the number of streams.

    40 single-event streams: the old implementation issued one SELECT per
    stream (40+) plus the id lookup. The scan issues a small fixed number
    regardless, so verifying 40 streams and verifying 10 cost the same.
    """
    _seed(db_session, streams=40)

    report, counter = _count(db_session, lambda: verify_chain(db_session))

    assert report.streams_checked >= 40
    assert report.events_checked >= 40
    assert report.ok, [d.detail for d in report.streams if not d.ok]
    # Generous, so this fails on O(streams) rather than on an extra query
    # someone legitimately adds: 40 streams must not cost ~40 statements.
    assert counter.statements < 15, (
        f"{counter.statements} statements for 40 streams -- verify_chain is "
        "querying per stream again"
    )


def test_verify_chain_statement_count_is_flat_in_stream_count(db_session):
    """Doubling the streams must not change the statement count at all."""
    _seed(db_session, streams=10)
    _, small = _count(db_session, lambda: verify_chain(db_session))

    # `first=10` so these are 30 NEW streams, not the same ten again.
    _seed(db_session, streams=30, first=10)
    report, larger = _count(db_session, lambda: verify_chain(db_session))

    assert report.streams_checked >= 40
    assert larger.statements == small.statements, (
        f"{small.statements} statements for 10 streams but {larger.statements} "
        "for 40 -- the read is scaling with the number of streams"
    )


def test_verify_chain_does_not_bind_a_parameter_per_stream(db_session):
    """No statement may carry one bind parameter per stream in scope.

    This is the failure that made the endpoint answer 500 rather than just
    run slowly: the scope was passed as `stream_id IN (...)`, one parameter
    per stream, against a ceiling of 65,535.
    """
    _seed(db_session, streams=60)

    _, counter = _count(db_session, lambda: verify_chain(db_session))

    assert counter.max_params < 20, (
        f"a statement carried {counter.max_params} parameters for 60 streams -- "
        "the scope is being enumerated as bind parameters again"
    )


def test_explicit_stream_ids_are_chunked(db_session):
    """A caller's own id list is bounded per statement.

    app/replay.py passes the streams a replay touched, which can exceed the
    bind-parameter ceiling on a large run, so the list is chunked rather
    than sent whole.
    """
    ids = _seed(db_session, streams=30)

    report, counter = _count(db_session, lambda: verify_chain(db_session, stream_ids=ids))

    assert report.streams_checked == 30
    assert counter.max_params <= 10_000 + 50


def test_a_requested_stream_with_no_events_still_gets_a_report(db_session):
    """Unchanged from the per-stream loop, which queried a named stream and
    reported the empty result. The scan finds no rows for it, so the report
    has to be filled in rather than silently dropped."""
    ids = _seed(db_session, streams=3)
    missing = "scantest_group:nonexistent"

    report = verify_chain(db_session, stream_ids=sorted([*ids, missing]))

    assert report.streams_checked == 4
    by_id = {s.stream_id: s for s in report.streams}
    assert missing in by_id
    assert by_id[missing].events == 0
    assert by_id[missing].first_sequence is None
    assert by_id[missing].last_sequence is None


def test_streams_are_reported_in_python_sort_order(db_session):
    """`report.streams` is a published array, and its order used to come
    from Python's `sorted()`. The scan orders by `stream_id COLLATE "C"` so
    the order stays byte order rather than following the database's
    collation."""
    _seed(db_session, streams=12)

    report = verify_chain(db_session)

    reported = [s.stream_id for s in report.streams]
    assert reported == sorted(reported)


def test_prefix_scope_only_reports_matching_streams(db_session):
    """Prefix scoping moved from a Python id list into the WHERE clause."""
    _seed(db_session, streams=5)
    uow = UnitOfWork(db_session)
    _append(uow, "scantest_other:aaa", "agg-other", 1)
    db_session.commit()

    report = verify_chain(db_session, stream_prefix="scantest_group:")

    assert report.streams_checked == 5
    assert all(s.stream_id.startswith("scantest_group:") for s in report.streams)
