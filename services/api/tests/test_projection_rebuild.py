"""Proves the "mutable projections explicitly rebuildable" requirement for
real: delete the live-updated projection row, rebuild it purely by
replaying business_events, and show the recomputed state is identical to
what live incremental updates produced."""

from __future__ import annotations

from app.ingestion import import_source
from app.models_ledger import AggregateProjection
from app.projections import rebuild_aggregate_projection
from app.storage.local_fs import LocalFilesystemStorage


def test_rebuilt_projection_matches_live_projection(db_session, tmp_path):
    storage = LocalFilesystemStorage(str(tmp_path))

    result = import_source(
        db_session,
        storage,
        source_system="pos",
        source_native_id="rebuild-test-1",
        content_bytes=b'{"amount_cents": 500}',
        mime_type="application/json",
        actor_id="user-1",
        actor_capability="import_source",
    )
    db_session.commit()

    # A second version of the same document, so the projection has
    # something nontrivial to fold over two events, not just one.
    result2 = import_source(
        db_session,
        storage,
        source_system="pos",
        source_native_id="rebuild-test-1",
        content_bytes=b'{"amount_cents": 550}',
        mime_type="application/json",
        actor_id="user-1",
        actor_capability="import_source",
    )
    db_session.commit()
    assert result2.source_document_id == result.source_document_id
    assert result2.version == 2

    live = db_session.get(AggregateProjection, result.source_document_id)
    assert live is not None
    live_state = dict(live.state)
    assert live_state["latest_version"] == 2
    assert live.latest_sequence == 2

    stream_id = f"source_document:{result.source_document_id}"

    # Destroy the projection row entirely -- this simulates "the derived
    # table is wrong/missing and must be rebuilt from the event log alone".
    db_session.delete(live)
    db_session.commit()
    assert db_session.get(AggregateProjection, result.source_document_id) is None

    rebuilt = rebuild_aggregate_projection(
        db_session, stream_id=stream_id, aggregate_id=result.source_document_id
    )
    db_session.commit()

    assert rebuilt.state == live_state
    assert rebuilt.latest_sequence == 2
    assert rebuilt.aggregate_type == "source_document"
