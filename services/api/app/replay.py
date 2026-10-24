"""A09 safe replay: re-walk the recorded history consuming recorded
results, with every effect disabled.

What a replay does, per recorded production event of the scope (festival,
optionally one vendor/run), in recorded order:
  - settlement.calculated / settlement.corrected: re-run A04's pure
    `calculate_vendor_settlement` on the frozen inputs pinned on the
    immutable settlement row and compare fee/payout with BOTH the row and
    the event payload.
  - journal.posted: compare the event's recorded lines with the stored
    journal lines and check the entry balances.
  - payout.*: follow the recorded state transitions and check each one is
    legal under C04's payout state machine.
  - agent extraction events: consume the RECORDED model result (the
    `document_extractions` row and its claims). No model is called.
  - every stream touched: recompute the per-stream hash chain.

What a replay can never do: it reads through a session whose transaction
is `SET TRANSACTION READ ONLY` (Postgres refuses writes), inside
`effects_guard.effects_disabled` (model client, rail simulator and journal
posting refuse and count the attempt). Production row counts are measured
before and after; the replay's own record (`replays` row + a
`replay.completed` event on the replay's stream) is written afterwards in
the caller's normal transaction and is not a financial mutation.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from domain import history as dh
from domain.settlement import EligibleSalesInput, calculate_vendor_settlement
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db import get_session_factory
from app.effects_guard import effects_disabled
from app.history import festival_event_clause
from app.models_agents import DocumentClaim, DocumentExtraction
from app.models_corrections import Replay
from app.models_ledger import BusinessEvent, JournalEntry, SourceDocument, Vendor
from app.models_settlement import Settlement
from app.uow import GENESIS_HASH, UnitOfWork, _canonical_json
from app.whatif import production_counts


def _recompute_hash(e: BusinessEvent) -> str:
    hashable = {
        "event_id": e.event_id,
        "stream_id": e.stream_id,
        "sequence": e.sequence,
        "schema_version": e.schema_version,
        "event_type": e.event_type,
        "actor_id": e.actor_id,
        "actor_type": e.actor_type,
        "actor_capability": e.actor_capability,
        "occurred_at": e.occurred_at.astimezone(UTC),
        "aggregate_id": e.aggregate_id,
        "aggregate_version": e.aggregate_version,
        "run_id": e.run_id,
        "vendor_id": e.vendor_id,
        "branch_id": e.branch_id,
        "correlation_id": e.correlation_id,
        "causation_id": e.causation_id,
        "command_id": e.command_id,
        "payload": e.payload,
        "versions": e.versions,
        "previous_hash": e.previous_hash,
    }
    return hashlib.sha256(_canonical_json(hashable).encode("utf-8")).hexdigest()


def _scope_events(ro: Session, *, festival_id: str, vendor_id: str | None, run_id: str | None):
    agent_ids = (
        select(DocumentExtraction.id)
        .join(SourceDocument, SourceDocument.id == DocumentExtraction.source_document_id)
        .where(
            SourceDocument.vendor_id.in_(select(Vendor.id).where(Vendor.festival_id == festival_id))
        )
    )
    q = (
        select(BusinessEvent)
        .where(BusinessEvent.branch_id.is_(None))
        .where(
            festival_event_clause(ro, festival_id)
            | (
                BusinessEvent.stream_id.like("agent_extraction:%")
                & BusinessEvent.aggregate_id.in_(agent_ids)
            )
        )
        .order_by(BusinessEvent.global_position)
    )
    if vendor_id:
        q = q.where(BusinessEvent.vendor_id == vendor_id)
    if run_id:
        q = q.where(BusinessEvent.run_id == run_id)
    return list(ro.execute(q).scalars().all())


def _replay_events(ro: Session, events: list[BusinessEvent]) -> tuple[int, list[dict], dict]:
    divergences: list[dict] = []
    checks = {
        "settlements_recomputed": 0,
        "journal_entries_compared": 0,
        "payout_transitions_checked": 0,
        "recorded_model_results_consumed": 0,
        "streams_hash_checked": 0,
        "events_hash_checked": 0,
    }
    payout_state: dict[str, str | None] = {}
    for e in events:
        p = e.payload or {}
        t = e.event_type
        if t in ("settlement.calculated", "settlement.corrected"):
            s = ro.get(Settlement, e.aggregate_id)
            if s is None:
                divergences.append({"event_id": e.event_id, "detail": "settlement row missing"})
                continue
            r = calculate_vendor_settlement(
                eligible=EligibleSalesInput(
                    centrally_held_cents=s.centrally_held_cents,
                    vendor_retained_cash_cents=s.vendor_retained_cash_cents,
                    tax_cents=s.tax_cents,
                    tips_cents=s.tips_cents,
                ),
                cumulative_eligible_sales_cents=s.cumulative_eligible_sales_cents,
                cumulative_commission_assessed_prior_cents=s.cumulative_commission_assessed_prior_cents,
                commission_bps=s.commission_bps_used,
                minimum_fee_cents=s.minimum_fee_cents_used,
                is_final_period=s.is_final_period,
                processor_fee_assessed_cents=s.processor_fee_assessed_cents,
                processor_fee_deductible=s.processor_fee_deductible_used,
            )
            checks["settlements_recomputed"] += 1
            for field, recomputed, stored, recorded in (
                ("fee", r.fee_assessed_cents, s.assessed_fee_cents, p.get("assessed_fee_cents")),
                ("payout", r.vendor_payout_cents, s.payout_cents, p.get("payout_cents")),
            ):
                if not (recomputed == stored == recorded):
                    divergences.append(
                        {
                            "event_id": e.event_id,
                            "detail": (
                                f"settlement {s.id} {field}: recomputed {recomputed}, stored "
                                f"{stored}, recorded {recorded}"
                            ),
                        }
                    )
        elif t == "journal.posted":
            je = ro.get(JournalEntry, e.aggregate_id)
            checks["journal_entries_compared"] += 1
            if je is None:
                divergences.append({"event_id": e.event_id, "detail": "journal entry missing"})
                continue
            stored = sorted((ln.account_code, ln.debit_cents, ln.credit_cents) for ln in je.lines)
            recorded = sorted(
                (ln["account"], int(ln["dr"]), int(ln["cr"])) for ln in p.get("lines") or []
            )
            if stored != recorded:
                divergences.append(
                    {"event_id": e.event_id, "detail": f"journal {je.entry_key} lines differ"}
                )
            if sum(x[1] for x in stored) != sum(x[2] for x in stored):
                divergences.append(
                    {"event_id": e.event_id, "detail": f"journal {je.entry_key} unbalanced"}
                )
        elif t.startswith("payout."):
            prior = payout_state.get(e.aggregate_id)
            new = dh.payout_state_after(t, p, prior)
            checks["payout_transitions_checked"] += 1
            if new != prior and new not in dh.PAYOUT_TRANSITIONS.get(prior, set()):
                divergences.append(
                    {
                        "event_id": e.event_id,
                        "detail": f"payout {e.aggregate_id}: illegal transition {prior} -> {new}",
                    }
                )
            payout_state[e.aggregate_id] = new
        elif e.stream_id.startswith("agent_extraction:"):
            ext = ro.get(DocumentExtraction, e.aggregate_id)
            if ext is None:
                divergences.append(
                    {"event_id": e.event_id, "detail": "recorded extraction missing"}
                )
                continue
            ro.execute(select(DocumentClaim.id).where(DocumentClaim.extraction_id == ext.id)).all()
            checks["recorded_model_results_consumed"] += 1

    # Hash chain for every stream touched (whole stream, not just the scope).
    for stream_id in sorted({e.stream_id for e in events}):
        prev = GENESIS_HASH
        expected_seq = 1
        for se in ro.execute(
            select(BusinessEvent)
            .where(BusinessEvent.stream_id == stream_id)
            .order_by(BusinessEvent.sequence)
        ).scalars():
            checks["events_hash_checked"] += 1
            if se.sequence != expected_seq or se.previous_hash != prev:
                divergences.append(
                    {"event_id": se.event_id, "detail": f"stream {stream_id}: chain link broken"}
                )
            if _recompute_hash(se) != se.hash:
                divergences.append(
                    {"event_id": se.event_id, "detail": f"stream {stream_id}: hash mismatch"}
                )
            prev = se.hash
            expected_seq = se.sequence + 1
        checks["streams_hash_checked"] += 1
    return len(events), divergences, checks


def run_replay(
    db: Session,
    *,
    festival_id: str,
    vendor_id: str | None,
    run_id: str | None,
    actor_id: str,
    capability: str,
) -> Replay:
    before = production_counts(db)
    db.commit()  # release the snapshot so the read-only session sees the same data
    ro = get_session_factory()()
    write_refused = False
    try:
        ro.execute(text("SET TRANSACTION READ ONLY"))
        with effects_disabled("replay") as attempts:
            events = _scope_events(ro, festival_id=festival_id, vendor_id=vendor_id, run_id=run_id)
            processed, divergences, checks = _replay_events(ro, events)
            first_pos = events[0].global_position if events else None
            last_pos = events[-1].global_position if events else None
            n_events = len(events)
            # Prove the read-only boundary is real, not assumed: a write in
            # this transaction must be refused by Postgres.
            # (encore_app is otherwise allowed to UPDATE vendors, so only the
            # READ ONLY transaction can be what refuses it.)
            try:
                ro.execute(text("UPDATE vendors SET status = status WHERE false"))
            except Exception as exc:  # noqa: BLE001
                write_refused = "read-only" in str(exc)
        ro.rollback()
    finally:
        ro.close()
    after = production_counts(db)
    deltas = {k: after[k] - before[k] for k in before}
    effects = {
        "live_model_calls": attempts["live_model_call"] + deltas["document_extractions"],
        "payment_effects": attempts["payment_effect"]
        + deltas["rail_simulator_records"]
        + deltas["outbox_intents"],
        "production_postings": attempts["production_posting"]
        + deltas["journal_entries"]
        + deltas["settlements"]
        + deltas["payouts"]
        + deltas["vendor_statements"],
    }
    replay = Replay(
        status="COMPLETED",
        mode="RECORDED_RESULTS_ONLY",
        scope={"festival_id": festival_id, "vendor_id": vendor_id, "run_id": run_id},
        from_position=first_pos,
        to_position=last_pos,
        processed=processed,
        total=n_events,
        matching=processed - len({d["event_id"] for d in divergences}),
        divergences=divergences[:500],
        effects=effects,
        checks={
            **checks,
            "read_only_write_probe_refused": write_refused,
            "production_counts_before": before,
            "production_counts_after": after,
            "guard_attempts": attempts,
        },
        created_by=actor_id,
        completed_at=datetime.now(UTC),
    )
    db.add(replay)
    db.flush()
    UnitOfWork(db).append_event(
        stream_id=f"replay:{replay.id}",
        event_type="replay.completed",
        aggregate_id=replay.id,
        aggregate_version=replay.version,
        actor_id=actor_id,
        actor_type="human",
        actor_capability=capability,
        occurred_at=datetime.now(UTC),
        payload={
            "status": replay.status,
            "scope": replay.scope,
            "processed": processed,
            "divergences": len(divergences),
            "effects": effects,
        },
        lineage_refs=[{"ref_kind": "replay", "ref_id": replay.id}],
    )
    return replay


def replay_out(r: Replay) -> dict:
    return {
        "id": r.id,
        "version": r.version,
        "status": r.status,
        "mode": r.mode,
        "run_id": (r.scope or {}).get("run_id"),
        "scope": r.scope,
        "from_sequence": r.from_position,
        "to_sequence": r.to_position,
        "effects": r.effects,
        "processed": r.processed,
        "total": r.total,
        "matching": r.matching,
        "divergences": r.divergences,
        "checks": r.checks,
        "label": "SIDE-EFFECT-FREE replay of recorded results",
        "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
    }
