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

Verifying the chain: `app/audit_chain.py` recomputes it from the stored
rows using `event_hash_input`/`compute_event_hash` below -- the same two
functions this module hashes with, so the write path and the verifier
cannot drift apart. It is reachable as `GET /api/v1/ops/audit-chain`, as
gauges on `/metrics`, and as `scripts/verify_audit_chain.py`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
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
    whitespace ambiguity, ISO datetimes normalized to UTC. Must stay stable
    -- changing this function changes future hashes, not past ones (past
    hashes were already computed and stored).

    Why the `.astimezone(UTC)`: `occurred_at` is a `timestamptz`, so the
    value Postgres hands back is rendered in the *reading session's*
    TimeZone. The same instant therefore reaches a verifier as
    `...+00:00` under a UTC session and `...-07:00` under
    America/Los_Angeles -- two different `isoformat()` strings, two
    different SHA-256s, for one unchanged row. Normalizing here makes the
    hash input a function of the instant only, so a recomputation agrees
    with the original write whatever timezone the verifying session is in.

    Does this invalidate hashes already stored? No, for one checkable
    reason: every writer passes an aware UTC datetime (`datetime.now(UTC)`
    at every `append_event` call site; `app/ingestion.py` passes
    `received_at`, which defaults to `datetime.now(UTC)` and which no
    caller overrides; the dev fixtures build `datetime(..., tzinfo=UTC)`).
    For an aware UTC datetime `d.astimezone(UTC).isoformat() ==
    d.isoformat()` byte for byte, so this function produces exactly the
    bytes it produced before for every input the writers actually produce.
    It changes the bytes only for an input that was never written: a
    datetime carrying a non-UTC offset.

    A naive datetime is refused rather than guessed at. Postgres would
    interpret one on the way in using the writing session's TimeZone, so
    the stored instant -- and hence any later recomputation -- would depend
    on a setting the hash input does not record. There is no call site that
    passes one today; refusing keeps it that way instead of minting an
    event whose hash can never be reproduced."""

    def _default(value: Any) -> Any:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise ValueError(
                    "refusing to hash a naive datetime: the instant it denotes would depend "
                    "on the session timezone, so the hash could not be reproduced"
                )
            return value.astimezone(UTC).isoformat()
        raise TypeError(f"not JSON-serializable for hashing: {type(value)!r}")

    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=_default)


def event_hash_input(
    *,
    event_id: str,
    stream_id: str,
    sequence: int,
    schema_version: int,
    event_type: str,
    actor_id: str | None,
    actor_type: str,
    actor_capability: str | None,
    occurred_at: datetime,
    aggregate_id: str,
    aggregate_version: int,
    run_id: str | None,
    vendor_id: str | None,
    branch_id: str | None,
    correlation_id: str | None,
    causation_id: str | None,
    command_id: str | None,
    payload: dict[str, Any],
    versions: dict[str, Any] | None,
    previous_hash: str | None,
) -> dict[str, Any]:
    """The exact, ordered set of fields that go into an event's hash.

    Defined once, here, because there are now two callers that must agree
    byte for byte forever: `append_event` below (which computes the hash at
    write time) and `app/audit_chain.recompute_event_hash` (which
    recomputes it from the stored row). Every argument is keyword-only and
    required so a caller cannot silently omit a field and still get a hash.
    Adding, removing or renaming a key here changes every future hash --
    don't, without a schema_version bump and a documented migration."""
    return {
        "event_id": event_id,
        "stream_id": stream_id,
        "sequence": sequence,
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
        "previous_hash": previous_hash,
    }


def compute_event_hash(hashable: dict[str, Any]) -> str:
    """SHA-256 of the canonical encoding of `event_hash_input(...)`."""
    return hashlib.sha256(_canonical_json(hashable).encode("utf-8")).hexdigest()


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
        new_hash = compute_event_hash(
            event_hash_input(
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
            )
        )

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
