"""Unit-of-work helper: the one reusable mechanism for "domain update +
audit event + outbox intent commit atomically in one Postgres transaction"
(C06, and AGENT_HANDOFF.md: "Commit domain changes, audit event and outbox
intent atomically").

A04/A07/A09 all depend on this same atomicity guarantee later, per the A02
task description -- so this is written once here as a reusable class, not
ad hoc per-endpoint code.

How atomicity is actually achieved: every method below takes the *same*
SQLAlchemy `Session` the caller is already using for its domain writes, and
only ever calls `db.add(...)` / `db.execute(...)` / `db.flush()` -- never
`db.commit()`. The caller (an endpoint or service function) does all of:
  1. domain row writes (via plain SQLAlchemy as usual)
  2. `uow.append_event(...)`
  3. `uow.enqueue_outbox(...)`
  4. exactly one `db.commit()` at the end.
If anything raises before that commit, the whole transaction rolls back --
domain rows, the event, and the outbox intent all disappear together. This
is proven for real in
services/api/tests/test_uow_atomicity.py (forces a rollback mid-transaction
and asserts nothing partial landed).

Concurrency-safe hash chain: `append_event` takes `SELECT ... FOR UPDATE` on
the stream's `StreamHead` row before computing the next sequence/hash. Two
concurrent transactions appending to the SAME stream_id serialize on that
row lock: the second transaction's SELECT FOR UPDATE blocks until the first
commits, then sees the updated last_sequence/last_hash and continues from
there. This is proven for real in
services/api/tests/test_concurrent_append.py using real separate DB
connections/threads against the live Postgres instance, not a mock.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models import _new_id
from app.models_ledger import BusinessEvent, EventLineage, OutboxIntent, StreamHead

# Seed hash for a brand-new stream's "previous_hash" slot. Not a secret --
# just a stable, documented genesis marker so hash(genesis + first_event) is
# deterministic and distinguishable from "previous_hash was accidentally
# empty string".
GENESIS_HASH = hashlib.sha256(b"encore-stream-genesis-v1").hexdigest()


def _canonical_json(data: dict[str, Any]) -> str:
    """Deterministic JSON encoding used as hash input: sorted keys, no
    whitespace ambiguity, ISO datetimes. Must stay stable -- changing this
    function changes future hashes, not past ones (past hashes were already
    computed and stored)."""

    def _default(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        raise TypeError(f"not JSON-serializable for hashing: {type(value)!r}")

    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=_default)


class UnitOfWork:
    """Wraps one SQLAlchemy Session. Does not manage transactions itself --
    the caller commits (or lets an exception roll back) exactly once."""

    def __init__(self, db: Session) -> None:
        self.db = db

    def append_event(
        self,
        *,
        stream_id: str,
        event_type: str,
        aggregate_id: str,
        aggregate_version: int,
        actor_id: str | None,
        actor_type: str,
        occurred_at: datetime,
        payload: dict[str, Any],
        actor_capability: str | None = None,
        run_id: str | None = None,
        vendor_id: str | None = None,
        branch_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        command_id: str | None = None,
        schema_version: int = 1,
        versions: dict[str, Any] | None = None,
        lineage_refs: list[dict[str, Any]] | None = None,
    ) -> BusinessEvent:
        """Append one event to `stream_id`, assigning the next sequence
        number and chaining its hash onto the stream's previous hash.

        Concurrency: `INSERT ... ON CONFLICT DO NOTHING` creates the
        StreamHead row if this is the stream's first event (safe even if two
        transactions race to create it -- exactly one INSERT wins, the
        other is a no-op, neither raises). Then `SELECT ... FOR UPDATE`
        takes a row lock on the (now-guaranteed-to-exist) head row, which is
        what actually serializes concurrent appenders to this stream.
        """
        self.db.execute(
            pg_insert(StreamHead)
            .values(stream_id=stream_id, last_sequence=0, last_hash=GENESIS_HASH)
            .on_conflict_do_nothing(index_elements=["stream_id"])
        )
        head = self.db.execute(
            select(StreamHead).where(StreamHead.stream_id == stream_id).with_for_update()
        ).scalar_one()

        next_sequence = head.last_sequence + 1
        event_id = _new_id()
        hashable = {
            "event_id": event_id,
            "stream_id": stream_id,
            "sequence": next_sequence,
            "schema_version": schema_version,
            "event_type": event_type,
            "actor_id": actor_id,
            "actor_type": actor_type,
            "actor_capability": actor_capability,
            "occurred_at": occurred_at,
            "aggregate_id": aggregate_id,
            "aggregate_version": aggregate_version,
            "run_id": run_id,
            "vendor_id": vendor_id,
            "branch_id": branch_id,
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "command_id": command_id,
            "payload": payload,
            "versions": versions,
            "previous_hash": head.last_hash,
        }
        new_hash = hashlib.sha256(_canonical_json(hashable).encode("utf-8")).hexdigest()

        event = BusinessEvent(
            event_id=event_id,
            stream_id=stream_id,
            sequence=next_sequence,
            schema_version=schema_version,
            event_type=event_type,
            actor_id=actor_id,
            actor_type=actor_type,
            actor_capability=actor_capability,
            occurred_at=occurred_at,
            aggregate_id=aggregate_id,
            aggregate_version=aggregate_version,
            run_id=run_id,
            vendor_id=vendor_id,
            branch_id=branch_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            command_id=command_id,
            payload=payload,
            versions=versions,
            previous_hash=head.last_hash,
            hash=new_hash,
        )
        self.db.add(event)

        head.last_sequence = next_sequence
        head.last_hash = new_hash

        # Flush now so a UNIQUE(stream_id, sequence) violation (which should
        # be impossible given the row lock above, but is the real backstop
        # if it ever isn't) surfaces inside this unit of work rather than
        # silently at a later, unrelated flush.
        self.db.flush()

        for ref in lineage_refs or []:
            self.db.add(
                EventLineage(
                    event_id=event_id,
                    ref_kind=ref["ref_kind"],
                    ref_id=ref["ref_id"],
                    ref_version=ref.get("ref_version"),
                )
            )
        if lineage_refs:
            self.db.flush()

        return event

    def enqueue_outbox(
        self,
        *,
        idempotency_key: str,
        intent_type: str,
        target: str,
        payload: dict[str, Any],
        event_id: str | None = None,
    ) -> OutboxIntent:
        """Idempotent insert: same idempotency_key returns the existing
        intent instead of creating a second one (retries of the same
        logical command must not duplicate outbox work)."""
        existing = self.db.execute(
            select(OutboxIntent).where(OutboxIntent.idempotency_key == idempotency_key)
        ).scalar_one_or_none()
        if existing is not None:
            return existing

        intent = OutboxIntent(
            event_id=event_id,
            idempotency_key=idempotency_key,
            intent_type=intent_type,
            target=target,
            payload=payload,
            status="PENDING",
        )
        self.db.add(intent)
        self.db.flush()
        return intent
