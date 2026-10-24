"""A07 controlled payouts: DB-touching orchestration.

Reads settlements.py/models_settlement.py's `Settlement.payout_cents` (A04)
as the authoritative "how much to pay this vendor this period" -- this
module never recomputes money, it only decides WHETHER and HOW a release
of an already-computed amount is allowed to happen, and talks to the
simulated rail (app/rail_simulator.py) to actually "move" it.

Seam for A06 (per the task brief: A06 -- LangGraph/live Claude execution --
is explicitly deferred, not a dependency of this task's actual behavior):
every function below takes plain parameters (db session, ids, an
actor_id/actor_capability pair) and is reachable from an ordinary
authenticated HTTP request (see app/routers/payouts.py). Nothing here
assumes a human is the caller versus a future scheduler/LangGraph node --
the ONLY thing that matters is that the caller has already passed
`require_capability`, exactly like every other write endpoint in this
codebase. When A06 is eventually built, it can call these same functions
(or the same HTTP endpoints) without any change here.

State machine actually implemented (C04): DRAFT -> APPROVED ->
DISPATCH_PENDING -> SUBMITTED -> CONFIRMED, with REJECTED/FAILED/CANCELLED/
CANCEL_REQUESTED/UNKNOWN as explicit alternate/terminal-ish states. See each
function's docstring for its exact transitions.

Atomicity (C06): every state-changing function below does ALL of its
checks, row locks and mutations on the SAME `db.Session` the caller passed
in, calls `db.flush()` as needed, and never calls `db.commit()` itself
(same convention as app/settlement.py, app/policy.py) -- the router commits
exactly once per request. The one deliberate exception is
app/rail_simulator.py's `submit_payment`/`resolve_outcome`, which commit
their OWN row in their OWN transaction, modeling the real external-system
boundary (see that module's docstring) -- `dispatch_payout` below is
written so nothing on `db` is mutated before that call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from domain.payout_dispatch import AUTOMATIC_RELEASE_LIMIT_CENTS, check_cumulative_limit
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

import app.accounting as accounting
import app.rail_simulator as rail_simulator
from app.holds import active_hold_for_payout
from app.models import CAPABILITY_APPROVE_PAYOUT, CAPABILITY_FINANCE_LEAD
from app.models_ledger import Case, OutboxIntent, Payout, Vendor
from app.models_payout_dispatch import CashPool, PayoutApproval, VendorDayPayoutAggregate
from app.models_settlement import Settlement
from app.uow import UnitOfWork

STREAM_PREFIX = "payout"


class PayoutNotFoundError(LookupError):
    pass


class PayoutVersionConflictError(ValueError):
    def __init__(self, expected: int, current: int) -> None:
        super().__init__(f"expected_version={expected} does not match current version={current}")
        self.expected = expected
        self.current = current


class PayoutCapabilityError(PermissionError):
    def __init__(self, required_capability: str) -> None:
        super().__init__(f"missing required capability: {required_capability}")
        self.required_capability = required_capability


class PayoutBlockedError(ValueError):
    """A release/approve attempt was safely refused. `reason_code` is a
    stable machine-readable string; `details` carries whatever numbers made
    the decision (never fabricated, always the real row values read during
    this check)."""

    def __init__(self, reason_code: str, details: dict | None = None) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.details = details or {}


@dataclass
class ReleaseOutcome:
    payout: Payout
    changed: bool
    already_released: bool = False


@dataclass
class DispatchOutcome:
    payout: Payout
    outcome: str | None
    changed: bool
    newly_submitted: bool = False


@dataclass
class CancelOutcome:
    payout: Payout
    result: str
    # "cancelled" | "accepted_with_recovery_flagged" | "cancel_requested_pending"
    # | "no_op_terminal_state"


# ---------------------------------------------------------------------------
# Row-locked aggregate helpers (same "INSERT ... ON CONFLICT DO NOTHING,
# then SELECT ... FOR UPDATE" pattern app/uow.py's StreamHead already uses)
# ---------------------------------------------------------------------------


def _lock_vendor_day_aggregate(
    db: Session, vendor_id: str, business_date_: date
) -> VendorDayPayoutAggregate:
    db.execute(
        pg_insert(VendorDayPayoutAggregate)
        .values(vendor_id=vendor_id, business_date=business_date_, committed_cents=0)
        .on_conflict_do_nothing(index_elements=["vendor_id", "business_date"])
    )
    return db.execute(
        select(VendorDayPayoutAggregate)
        .where(
            VendorDayPayoutAggregate.vendor_id == vendor_id,
            VendorDayPayoutAggregate.business_date == business_date_,
        )
        .with_for_update()
    ).scalar_one()


def _lock_cash_pool(db: Session, festival_id: str) -> CashPool | None:
    return db.execute(
        select(CashPool).where(CashPool.festival_id == festival_id).with_for_update()
    ).scalar_one_or_none()


def _lock_payout(db: Session, payout_id: str) -> Payout | None:
    # populate_existing: a row already in the identity map (e.g. re-locked
    # after rail_simulator committed this session) must be overwritten with
    # what the lock actually read, never served from a stale cached copy.
    return db.execute(
        select(Payout)
        .where(Payout.id == payout_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _append_payout_event(
    db: Session,
    payout: Payout,
    *,
    event_type: str,
    actor_id: str | None,
    actor_capability: str | None,
    payload: dict,
) -> None:
    uow = UnitOfWork(db)
    uow.append_event(
        stream_id=f"{STREAM_PREFIX}:{payout.id}",
        event_type=event_type,
        aggregate_id=payout.id,
        aggregate_version=payout.version,
        actor_id=actor_id,
        actor_type="human" if actor_id else "system",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        vendor_id=payout.vendor_id,
        # A09: every payout event records the state and amount it left the
        # payout in, so the flight recorder can rebuild payout history from
        # events alone (app/history.py) instead of from the mutable row.
        payload={
            **payload,
            "state_after": payout.state,
            "amount_cents_after": payout.amount_cents,
            "business_date": payout.business_date.isoformat(),
            "settlement_id": payout.settlement_id,
        },
        lineage_refs=[{"ref_kind": "payout", "ref_id": payout.id}]
        + (
            [{"ref_kind": "settlement", "ref_id": payout.settlement_id}]
            if payout.settlement_id
            else []
        ),
    )


def _block(
    db: Session,
    payout: Payout,
    *,
    reason_code: str,
    details: dict,
    actor_id: str | None,
    actor_capability: str | None,
) -> None:
    """Logs the blocked attempt as a real audit event (C06: 'attempted
    effects ... outcomes' are part of the flight recorder, not only
    successful ones) and commits that log entry, THEN raises. The release
    itself performed no mutation -- only the fact that it was attempted and
    blocked is recorded."""
    _append_payout_event(
        db,
        payout,
        event_type="payout.release_blocked",
        actor_id=actor_id,
        actor_capability=actor_capability,
        payload={"reason_code": reason_code, **details},
    )
    db.commit()
    raise PayoutBlockedError(reason_code, details)


# ---------------------------------------------------------------------------
# Cash pool funding (A08 owns the real cash model -- see
# docs/progress/A07-handoff.md's "conservative defaults")
# ---------------------------------------------------------------------------


def fund_cash_pool(
    db: Session, *, festival_id: str, additional_cents: int, actor_id: str, actor_capability: str
) -> CashPool:
    if additional_cents < 0:
        raise ValueError("additional_cents must be non-negative")
    pool = _lock_cash_pool(db, festival_id)
    if pool is None:
        pool = CashPool(festival_id=festival_id, available_cents=0, reserved_cents=0)
        db.add(pool)
        db.flush()
    pool.available_cents += additional_cents
    pool.version += 1
    uow = UnitOfWork(db)
    uow.append_event(
        stream_id=f"cash_pool:{festival_id}",
        event_type="cash_pool.funded",
        aggregate_id=pool.id,
        aggregate_version=pool.version,
        actor_id=actor_id,
        actor_type="human",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        payload={
            "festival_id": festival_id,
            "additional_cents": additional_cents,
            "available_cents": pool.available_cents,
        },
    )
    return pool


def update_vendor_beneficiary(
    db: Session,
    *,
    vendor_id: str,
    beneficiary_details: dict,
    actor_id: str,
    actor_capability: str,
) -> Vendor:
    """Bumps Vendor.version -- the exact signal release's atomic re-check
    reads (Payout.beneficiary_version pinned at approval/creation time vs.
    Vendor.version read fresh at release time). A minimal hook, not a full
    beneficiary-management workflow -- see module docstring."""
    vendor = db.get(Vendor, vendor_id)
    if vendor is None:
        raise PayoutNotFoundError(f"vendor {vendor_id} not found")
    vendor.beneficiary_details = beneficiary_details
    vendor.version += 1
    uow = UnitOfWork(db)
    uow.append_event(
        stream_id=f"vendor:{vendor_id}",
        event_type="vendor.beneficiary_updated",
        aggregate_id=vendor_id,
        aggregate_version=vendor.version,
        actor_id=actor_id,
        actor_type="human",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        vendor_id=vendor_id,
        payload={"new_version": vendor.version},
    )
    return vendor


# ---------------------------------------------------------------------------
# Obligation creation (idempotent on settlement_id -- one logical payout
# obligation per settlement row, a DISTINCT settlement correction gets its
# OWN distinct obligation/key)
# ---------------------------------------------------------------------------


def create_payout_obligation(
    db: Session, *, settlement_id: str, actor_id: str, actor_capability: str
) -> tuple[Payout, bool]:
    """Returns (payout, created). `created=False` means a payout already
    existed for this settlement -- the SAME row is returned, never a second
    one, because logical_obligation_id/idempotency_key are both
    deterministic functions of settlement_id. A later, explicitly-approved
    correction settlement (A04's `Settlement.correction_of`) has its OWN
    `id`, so it deterministically produces its OWN distinct
    logical_obligation_id/idempotency_key here -- never a reused key for a
    different obligation."""
    settlement = db.get(Settlement, settlement_id)
    if settlement is None:
        raise PayoutNotFoundError(f"settlement {settlement_id} not found")

    superseding = db.execute(
        select(Settlement).where(Settlement.correction_of == settlement.id)
    ).first()
    if superseding is not None:
        raise PayoutBlockedError(
            "settlement_superseded_by_correction",
            {"settlement_id": settlement.id, "correction_settlement_id": superseding[0].id},
        )

    idempotency_key = f"payout-obligation:{settlement.id}"
    existing = db.execute(
        select(Payout).where(Payout.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    vendor = db.get(Vendor, settlement.vendor_id)
    if vendor is None:
        raise PayoutNotFoundError(f"vendor {settlement.vendor_id} not found")

    payout = Payout(
        logical_obligation_id=idempotency_key,
        idempotency_key=idempotency_key,
        vendor_id=settlement.vendor_id,
        business_date=settlement.business_date,
        beneficiary_version=vendor.version,
        amount_cents=settlement.payout_cents,
        state="DRAFT",
        settlement_id=settlement.id,
    )
    db.add(payout)
    db.flush()
    _append_payout_event(
        db,
        payout,
        event_type="payout.created",
        actor_id=actor_id,
        actor_capability=actor_capability,
        payload={
            "settlement_id": settlement.id,
            "vendor_id": settlement.vendor_id,
            "business_date": settlement.business_date.isoformat(),
            "amount_cents": payout.amount_cents,
            "beneficiary_version": payout.beneficiary_version,
        },
    )
    return payout, True


# ---------------------------------------------------------------------------
# Approval (the finance-lead gate)
# ---------------------------------------------------------------------------


def approve_payout(
    db: Session,
    *,
    payout_id: str,
    actor_id: str,
    user_capability_codes: set[str],
    expected_version: int | None,
    note: str | None = None,
) -> Payout:
    """DRAFT -> APPROVED. Requires CAPABILITY_APPROVE_PAYOUT always (the
    router's `require_capability` dependency already guarantees this --
    `user_capability_codes` here is the FULL set re-read from the database
    for THIS actor, used only to decide whether the ADDITIONAL finance_lead
    capability is also required and held).

    This check is a best-effort snapshot (it locks and reads the vendor/day
    aggregate's CURRENT `committed_cents`, but does not itself reserve
    anything) -- the authoritative, race-proof enforcement is
    `release_payout`'s re-check at execution time, inside the same
    transaction as the reservation. A payout approved here as "under the
    limit" can still be blocked at release if concurrent releases pushed
    the vendor/day total over $5,000 in the meantime -- that is intended,
    not a bug (see app/payout_dispatch.py module docstring / A07 handoff).
    """
    payout = _lock_payout(db, payout_id)
    if payout is None:
        raise PayoutNotFoundError(f"payout {payout_id} not found")
    if expected_version is not None and payout.version != expected_version:
        raise PayoutVersionConflictError(expected_version, payout.version)

    if payout.state == "APPROVED" and CAPABILITY_FINANCE_LEAD in user_capability_codes:
        # Escalation: an ordinary approval that release later blocked with
        # exceeds_daily_automatic_limit_requires_finance_lead (because other
        # releases pushed the vendor/day total over the limit in between)
        # must be able to receive the finance-lead approval release looks
        # for -- otherwise the payout is stuck in APPROVED forever.
        has_finance_lead = db.execute(
            select(PayoutApproval.id).where(
                PayoutApproval.payout_id == payout.id,
                PayoutApproval.capability == CAPABILITY_FINANCE_LEAD,
                PayoutApproval.decision == "APPROVED",
            )
        ).first()
        if has_finance_lead is not None:
            return payout
        agg = _lock_vendor_day_aggregate(db, payout.vendor_id, payout.business_date)
        check = check_cumulative_limit(agg.committed_cents, payout.amount_cents)
        if not check.exceeds_limit:
            return payout  # nothing to escalate; ordinary approval suffices
        db.add(
            PayoutApproval(
                payout_id=payout.id,
                actor_id=actor_id,
                capability=CAPABILITY_FINANCE_LEAD,
                decision="APPROVED",
                amount_cents=payout.amount_cents,
                note=note,
            )
        )
        payout.version += 1
        _append_payout_event(
            db,
            payout,
            event_type="payout.finance_lead_escalation_approved",
            actor_id=actor_id,
            actor_capability=CAPABILITY_FINANCE_LEAD,
            payload={
                "amount_cents": payout.amount_cents,
                "committed_prior_cents": check.committed_prior_cents,
                "projected_total_cents": check.projected_total_cents,
            },
        )
        return payout

    if payout.state != "DRAFT":
        # Idempotent: already approved (or further along) -- a repeat
        # approval call is a safe no-op, not a second approval record.
        return payout

    agg = _lock_vendor_day_aggregate(db, payout.vendor_id, payout.business_date)
    check = check_cumulative_limit(agg.committed_cents, payout.amount_cents)
    required_capability = (
        CAPABILITY_FINANCE_LEAD if check.exceeds_limit else CAPABILITY_APPROVE_PAYOUT
    )
    if required_capability not in user_capability_codes:
        db.rollback()
        raise PayoutCapabilityError(required_capability)

    db.add(
        PayoutApproval(
            payout_id=payout.id,
            actor_id=actor_id,
            capability=required_capability,
            decision="APPROVED",
            amount_cents=payout.amount_cents,
            note=note,
        )
    )
    payout.state = "APPROVED"
    payout.version += 1
    _append_payout_event(
        db,
        payout,
        event_type="payout.approved",
        actor_id=actor_id,
        actor_capability=required_capability,
        payload={
            "amount_cents": payout.amount_cents,
            "committed_prior_cents": check.committed_prior_cents,
            "required_capability": required_capability,
            "exceeded_automatic_limit": check.exceeds_limit,
        },
    )
    return payout


# ---------------------------------------------------------------------------
# Release: the atomic, re-checked-at-execution-time core
# ---------------------------------------------------------------------------


def release_payout(
    db: Session,
    *,
    payout_id: str,
    actor_id: str,
    actor_capability: str,
    expected_version: int | None,
) -> ReleaseOutcome:
    """APPROVED -> DISPATCH_PENDING, with an outbox intent enqueued for
    `dispatch_payout` to process.

    Idempotent: if `payout.state` is already past APPROVED (DISPATCH_PENDING
    or later), this is a safe no-op returning the current row unchanged --
    this is exactly the "worker crash mid-release, restart/resume" case:
    a resumed caller that doesn't know whether its own prior attempt
    committed re-calls this function and gets the SAME single outcome, not
    a second reservation.

    Every check below re-runs using data read FRESH inside this
    transaction, after acquiring the relevant row locks -- never trusting
    anything decided at approval time as still true:
      1. approval validity (state == APPROVED)
      2. record version match (expected_version)
      3. unresolved holds: an OPEN case on this vendor blocks release
         regardless of amount or finance-lead approval (A05's Case.status)
      4. beneficiary/vendor-terms version unchanged since approval
      5. the settlement this payout pays is not itself superseded by a
         later correction
      6. the cumulative $5,000/day limit, atomically, against a row-locked
         per-vendor/day aggregate -- the SAME transaction as the
         reservation below, which is what makes concurrent split requests
         unable to evade the limit
      7. available cash, reserved atomically against a row-locked cash pool
    """
    payout = _lock_payout(db, payout_id)
    if payout is None:
        raise PayoutNotFoundError(f"payout {payout_id} not found")
    if expected_version is not None and payout.version != expected_version:
        raise PayoutVersionConflictError(expected_version, payout.version)

    if payout.state != "APPROVED":
        if payout.state in (
            "DISPATCH_PENDING",
            "SUBMITTED",
            "CONFIRMED",
            "FAILED",
            "CANCELLED",
            "CANCEL_REQUESTED",
            "UNKNOWN",
        ):
            return ReleaseOutcome(payout=payout, changed=False, already_released=True)
        _block(
            db,
            payout,
            reason_code="not_approved",
            details={"current_state": payout.state},
            actor_id=actor_id,
            actor_capability=actor_capability,
        )

    # 3. Unresolved holds: an OPEN case on this vendor is a hard blocker,
    # independent of amount/approval (AGENT_HANDOFF.md: "An amount approval
    # does not waive unrelated controls"). `blocking` defaults to true, so
    # every case kind that ever blocked still blocks; the one exception is
    # A06 stage 2's `minimum_period_claim`, where the signed contract
    # already decides the night and the open question withholds nothing.
    open_case = db.execute(
        select(Case.id).where(
            Case.vendor_id == payout.vendor_id,
            Case.status == "OPEN",
            Case.blocking.is_(True),
        )
    ).first()
    if open_case is not None:
        _block(
            db,
            payout,
            reason_code="open_unresolved_case",
            details={"case_id": open_case[0]},
            actor_id=actor_id,
            actor_capability=actor_capability,
        )

    # 3b. A09 work holds: an open challenge of the rule/result this vendor's
    # money depends on, or an undecided vendor recovery, blocks release
    # regardless of amount or approval.
    hold = active_hold_for_payout(
        db, vendor_id=payout.vendor_id, payout_id=payout.id, settlement_id=payout.settlement_id
    )
    if hold is not None:
        _block(
            db,
            payout,
            reason_code=hold.reason_code,
            details={
                "hold_id": hold.id,
                "source_kind": hold.source_kind,
                "source_id": hold.source_id,
            },
            actor_id=actor_id,
            actor_capability=actor_capability,
        )

    # 4. Beneficiary/vendor-terms version unchanged since approval.
    vendor = db.get(Vendor, payout.vendor_id)
    if vendor is None or vendor.version != payout.beneficiary_version:
        _block(
            db,
            payout,
            reason_code="beneficiary_version_changed",
            details={
                "payout_beneficiary_version": payout.beneficiary_version,
                "current_vendor_version": None if vendor is None else vendor.version,
            },
            actor_id=actor_id,
            actor_capability=actor_capability,
        )

    # 5. The settlement this pays must not be superseded by a later
    # correction -- a stale obligation must not be released.
    if payout.settlement_id is not None:
        superseding = db.execute(
            select(Settlement.id).where(Settlement.correction_of == payout.settlement_id)
        ).first()
        if superseding is not None:
            _block(
                db,
                payout,
                reason_code="settlement_superseded_by_correction",
                details={"correction_settlement_id": superseding[0]},
                actor_id=actor_id,
                actor_capability=actor_capability,
            )

    # 6. Atomic cumulative $5,000/day check, under the vendor/day
    # aggregate's row lock.
    agg = _lock_vendor_day_aggregate(db, payout.vendor_id, payout.business_date)
    check = check_cumulative_limit(agg.committed_cents, payout.amount_cents)
    if check.exceeds_limit:
        finance_lead_approval = db.execute(
            select(PayoutApproval.id).where(
                PayoutApproval.payout_id == payout.id,
                PayoutApproval.capability == CAPABILITY_FINANCE_LEAD,
                PayoutApproval.decision == "APPROVED",
            )
        ).first()
        if finance_lead_approval is None:
            _block(
                db,
                payout,
                reason_code="exceeds_daily_automatic_limit_requires_finance_lead",
                details={
                    "committed_prior_cents": check.committed_prior_cents,
                    "amount_cents": check.amount_cents,
                    "projected_total_cents": check.projected_total_cents,
                    "limit_cents": AUTOMATIC_RELEASE_LIMIT_CENTS,
                },
                actor_id=actor_id,
                actor_capability=actor_capability,
            )

    # 7. Available cash, reserved atomically under the cash pool's row lock.
    pool = _lock_cash_pool(db, vendor.festival_id)
    if pool is None or pool.available_cents < payout.amount_cents:
        _block(
            db,
            payout,
            reason_code="insufficient_available_cash",
            details={
                "available_cents": None if pool is None else pool.available_cents,
                "amount_cents": payout.amount_cents,
            },
            actor_id=actor_id,
            actor_capability=actor_capability,
        )

    # All checks passed -- mutate, atomically, in this one transaction.
    pool.available_cents -= payout.amount_cents
    pool.reserved_cents += payout.amount_cents
    pool.version += 1

    agg.committed_cents = check.projected_total_cents
    agg.version += 1

    payout.state = "DISPATCH_PENDING"
    payout.version += 1
    db.flush()

    uow = UnitOfWork(db)
    uow.append_event(
        stream_id=f"{STREAM_PREFIX}:{payout.id}",
        event_type="payout.released",
        aggregate_id=payout.id,
        aggregate_version=payout.version,
        actor_id=actor_id,
        actor_type="human",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        vendor_id=payout.vendor_id,
        payload={
            "amount_cents": payout.amount_cents,
            "committed_prior_cents": check.committed_prior_cents,
            "projected_total_cents": check.projected_total_cents,
            "state_after": payout.state,
            "amount_cents_after": payout.amount_cents,
            "business_date": payout.business_date.isoformat(),
            "settlement_id": payout.settlement_id,
        },
        lineage_refs=[{"ref_kind": "payout", "ref_id": payout.id}],
    )
    uow.enqueue_outbox(
        idempotency_key=payout.idempotency_key,
        intent_type="payout.dispatch",
        target="rail_simulator",
        payload={
            "payout_id": payout.id,
            "vendor_id": payout.vendor_id,
            "amount_cents": payout.amount_cents,
        },
        event_id=None,
    )
    return ReleaseOutcome(payout=payout, changed=True, already_released=False)


# ---------------------------------------------------------------------------
# Reservation release (genuine pre-acceptance cancel, or a FAILED outcome)
# ---------------------------------------------------------------------------


def _release_reservation(db: Session, payout: Payout) -> None:
    """Only call for a payout that release_payout actually reserved (state
    was DISPATCH_PENDING or later). Locks in the SAME order release_payout
    does -- vendor/day aggregate, then cash pool -- so a concurrent release
    and cancel/fail on the same vendor cannot deadlock."""
    if payout.reservation_released_at is not None:
        return  # already released -- never double-release (idempotency guard)
    agg = _lock_vendor_day_aggregate(db, payout.vendor_id, payout.business_date)
    vendor = db.get(Vendor, payout.vendor_id)
    pool = _lock_cash_pool(db, vendor.festival_id) if vendor is not None else None
    agg.committed_cents = max(0, agg.committed_cents - payout.amount_cents)
    agg.version += 1
    if pool is not None:
        pool.available_cents += payout.amount_cents
        pool.reserved_cents -= payout.amount_cents
        pool.version += 1
    payout.reservation_released_at = datetime.now(UTC)


# ---------------------------------------------------------------------------
# Dispatch: outbox -> simulated rail
# ---------------------------------------------------------------------------


def dispatch_payout(
    db: Session,
    *,
    payout_id: str,
    actor_id: str | None,
    actor_capability: str | None,
    simulate_outcome: rail_simulator.Outcome = "ACCEPTED",
) -> DispatchOutcome:
    """DISPATCH_PENDING -> SUBMITTED (ACCEPTED), stays DISPATCH_PENDING
    (DELAYED, retry later), -> FAILED (reservation released), or -> UNKNOWN
    (reservation stays held until explicit reconciliation).

    Safe to call repeatedly, including after a simulated crash: if
    `payout.state` is no longer DISPATCH_PENDING/UNKNOWN (i.e. a previous
    call already resolved it, or it was never released), this is a no-op.
    If the rail was already contacted (a RailSimulatorRecord already exists
    for this idempotency_key -- e.g. because a prior call crashed AFTER
    `rail_simulator.submit_payment` committed but BEFORE this function
    could update the Payout row), the SAME record is returned
    (`newly_submitted=False`) and this call simply converges our domain
    state to match it -- it never calls `submit_payment` with a different
    outcome or creates a second record.
    """
    payout = _lock_payout(db, payout_id)
    if payout is None:
        raise PayoutNotFoundError(f"payout {payout_id} not found")

    if payout.state not in ("DISPATCH_PENDING", "UNKNOWN"):
        return DispatchOutcome(payout=payout, outcome=None, changed=False)

    intent = db.execute(
        select(OutboxIntent).where(OutboxIntent.idempotency_key == payout.idempotency_key)
    ).scalar_one_or_none()
    if intent is None:
        raise RuntimeError(
            f"no outbox intent for payout {payout.id} -- release_payout must run first"
        )

    # A09 recheck at execution: a challenge that landed after release holds
    # the dispatch too, as long as the rail has not been contacted yet (once
    # it has, only cancellation/reconciliation can follow -- never a pretend
    # "unsent").
    hold = active_hold_for_payout(
        db, vendor_id=payout.vendor_id, payout_id=payout.id, settlement_id=payout.settlement_id
    )
    if hold is not None and rail_simulator.lookup(db, payout.idempotency_key) is None:
        _block(
            db,
            payout,
            reason_code=hold.reason_code,
            details={
                "hold_id": hold.id,
                "source_kind": hold.source_kind,
                "source_id": hold.source_id,
                "stage": "dispatch",
            },
            actor_id=actor_id,
            actor_capability=actor_capability,
        )

    # Nothing on `db` is mutated above this line (only SELECTs/locks) --
    # see app/rail_simulator.py's docstring for why that matters.
    record, newly_submitted = rail_simulator.submit_payment(
        db,
        idempotency_key=payout.idempotency_key,
        vendor_id=payout.vendor_id,
        amount_cents=payout.amount_cents,
        simulate_outcome=simulate_outcome,
    )

    # submit_payment may have committed this session (a genuinely new
    # submission), which released the row lock taken above. Re-lock and
    # re-check before converging: a concurrent dispatcher/cancel may have
    # acted on the payout in the gap.
    payout = _lock_payout(db, payout_id)
    if payout.state not in ("DISPATCH_PENDING", "UNKNOWN"):
        # Another dispatcher converged the payout in the gap; this call may
        # still be the one that actually submitted to the rail.
        return DispatchOutcome(
            payout=payout,
            outcome=record.outcome,
            changed=False,
            newly_submitted=newly_submitted,
        )
    intent = db.execute(
        select(OutboxIntent).where(OutboxIntent.idempotency_key == payout.idempotency_key)
    ).scalar_one()

    payout.dispatch_attempts += 1
    outcome = record.outcome

    if outcome == "ACCEPTED":
        payout.state = "SUBMITTED"
        payout.provider_reference = record.provider_reference
        intent.status = "DISPATCHED"
        intent.dispatched_at = datetime.now(UTC)
    elif outcome == "DELAYED":
        intent.status = "PENDING"
    elif outcome == "FAILED":
        payout.state = "FAILED"
        intent.status = "FAILED"
        _release_reservation(db, payout)
    elif outcome == "UNKNOWN":
        payout.state = "UNKNOWN"
        intent.status = "UNKNOWN"
        # Reservation deliberately stays held -- C06: "Unknown outcomes
        # consume reserved cash until resolved appropriately, not
        # immediately released for another payout."
    else:  # pragma: no cover - rail_simulator already validates this
        raise ValueError(f"unexpected rail outcome: {outcome!r}")

    payout.version += 1
    _append_payout_event(
        db,
        payout,
        event_type=f"payout.dispatch_{outcome.lower()}",
        actor_id=actor_id,
        actor_capability=actor_capability,
        payload={
            "outcome": outcome,
            "newly_submitted": newly_submitted,
            "dispatch_attempts": payout.dispatch_attempts,
            "provider_reference": record.provider_reference,
        },
    )
    return DispatchOutcome(
        payout=payout, outcome=outcome, changed=True, newly_submitted=newly_submitted
    )


# ---------------------------------------------------------------------------
# Separate, idempotent confirmation events
# ---------------------------------------------------------------------------


# A confirmation (bank debit or payout) is only meaningful once the rail has
# accepted the payment. UNKNOWN is deliberately excluded: it resolves only
# through reconcile_unknown_payout, never through a confirmation side door.
_RAIL_ACCEPTED_STATES = ("SUBMITTED", "CONFIRMED")


def confirm_bank_debit(
    db: Session, *, payout_id: str, actor_id: str | None, actor_capability: str | None
) -> tuple[Payout, bool]:
    """Idempotent: a second call makes NO additional write at all -- not
    even a duplicate audit event -- if `bank_debit_confirmed_at` is already
    set. Returns (payout, newly_confirmed)."""
    payout = _lock_payout(db, payout_id)
    if payout is None:
        raise PayoutNotFoundError(f"payout {payout_id} not found")
    if payout.bank_debit_confirmed_at is not None:
        db.rollback()
        return payout, False
    if payout.state not in _RAIL_ACCEPTED_STATES:
        db.rollback()
        raise PayoutBlockedError("rail_has_not_accepted", {"current_state": payout.state})
    payout.bank_debit_confirmed_at = datetime.now(UTC)
    _append_payout_event(
        db,
        payout,
        event_type="payout.bank_debit_confirmed",
        actor_id=actor_id,
        actor_capability=actor_capability,
        payload={"bank_debit_confirmed_at": payout.bank_debit_confirmed_at.isoformat()},
    )
    return payout, True


def confirm_payout_rail(
    db: Session, *, payout_id: str, actor_id: str | None, actor_capability: str | None
) -> tuple[Payout, bool]:
    """Idempotent, separate from confirm_bank_debit (C06: "Bank-debit
    confirmation and payout confirmation are tracked as separate events").
    SUBMITTED -> CONFIRMED on first confirmation; a second call is a pure
    no-op."""
    payout = _lock_payout(db, payout_id)
    if payout is None:
        raise PayoutNotFoundError(f"payout {payout_id} not found")
    if payout.payout_confirmed_at is not None:
        db.rollback()
        return payout, False
    if payout.state not in _RAIL_ACCEPTED_STATES:
        db.rollback()
        raise PayoutBlockedError("rail_has_not_accepted", {"current_state": payout.state})
    payout.payout_confirmed_at = datetime.now(UTC)
    if payout.state == "SUBMITTED":
        payout.state = "CONFIRMED"
    payout.version += 1
    # A08: the books record the payout once, when it is confirmed -- never
    # at release or submission, which may still fail or stay unknown.
    accounting.post_vendor_payout(
        db, payout=payout, actor_id=actor_id, actor_capability=actor_capability
    )
    _append_payout_event(
        db,
        payout,
        event_type="payout.confirmed",
        actor_id=actor_id,
        actor_capability=actor_capability,
        payload={"payout_confirmed_at": payout.payout_confirmed_at.isoformat()},
    )
    return payout, True


# ---------------------------------------------------------------------------
# Reconciliation (the only way an UNKNOWN outcome ever resolves)
# ---------------------------------------------------------------------------


def reconcile_unknown_payout(
    db: Session,
    *,
    payout_id: str,
    resolved_outcome: rail_simulator.Outcome,
    actor_id: str,
    actor_capability: str,
) -> Payout:
    """The ONLY path that moves a payout out of an unresolved rail outcome:
    UNKNOWN, DISPATCH_PENDING whose rail record is DELAYED (re-dispatch
    only ever reads back the same DELAYED record, by design), or
    CANCEL_REQUESTED (a cancel that landed while the rail outcome was
    DELAYED/UNKNOWN). Never invents a new idempotency key or calls
    `submit_payment` again -- it transitions the EXISTING
    RailSimulatorRecord via `resolve_outcome` (simulating the provider
    eventually reporting what really happened) and then applies that
    outcome to our domain state.

    For CANCEL_REQUESTED: FAILED means the money never moved, so the
    cancel completes (CANCELLED, reservation released); ACCEPTED means the
    payment won the race -- the payout becomes SUBMITTED with
    recovery_required set, never CANCELLED."""
    payout = _lock_payout(db, payout_id)
    if payout is None:
        raise PayoutNotFoundError(f"payout {payout_id} not found")

    existing = rail_simulator.lookup(db, payout.idempotency_key)
    reconcilable = payout.state in ("UNKNOWN", "CANCEL_REQUESTED") or (
        payout.state == "DISPATCH_PENDING"
        and existing is not None
        and existing.outcome == "DELAYED"
    )
    if not reconcilable:
        raise PayoutBlockedError("nothing_to_reconcile", {"current_state": payout.state})
    if existing is None:
        raise RuntimeError(f"no rail_simulator_record for payout {payout.id} -- cannot reconcile")

    prior_state = payout.state
    record = rail_simulator.resolve_outcome(
        db, payout.idempotency_key, new_outcome=resolved_outcome
    )
    # resolve_outcome committed this session (its own external-system
    # boundary), releasing the row lock -- re-lock before mutating.
    payout = _lock_payout(db, payout_id)
    if payout.state != prior_state:
        # A concurrent reconcile/cancel already moved it; the rail record is
        # the source of truth and the other call applied it.
        return payout
    intent = db.execute(
        select(OutboxIntent).where(OutboxIntent.idempotency_key == payout.idempotency_key)
    ).scalar_one_or_none()

    cancel_pending = prior_state == "CANCEL_REQUESTED"
    if resolved_outcome == "ACCEPTED":
        payout.state = "SUBMITTED"
        payout.provider_reference = record.provider_reference
        if intent is not None:
            intent.status = "DISPATCHED"
            intent.dispatched_at = datetime.now(UTC)
        if cancel_pending:
            payout.recovery_required = True
            payout.recovery_reason = (
                "cancellation was pending when the simulated rail reported the payout "
                "accepted -- funds moved and require a recovery/offset workflow, not a "
                "pretended cancellation"
            )
    elif resolved_outcome == "FAILED":
        payout.state = "CANCELLED" if cancel_pending else "FAILED"
        if intent is not None:
            intent.status = "FAILED"
        _release_reservation(db, payout)
    elif resolved_outcome == "DELAYED":
        if not cancel_pending:
            payout.state = "DISPATCH_PENDING"
            if intent is not None:
                intent.status = "PENDING"
    elif resolved_outcome == "UNKNOWN":
        if not cancel_pending:
            payout.state = "UNKNOWN"
            if intent is not None:
                intent.status = "UNKNOWN"
    payout.version += 1
    _append_payout_event(
        db,
        payout,
        event_type="payout.reconciled",
        actor_id=actor_id,
        actor_capability=actor_capability,
        payload={
            "resolved_outcome": resolved_outcome,
            "prior_state": prior_state,
            "new_state": payout.state,
            "recovery_required": payout.recovery_required,
        },
    )
    return payout


# ---------------------------------------------------------------------------
# Cancel (the explicit cancel-race model)
# ---------------------------------------------------------------------------


_TERMINAL_STATES = ("CONFIRMED", "CANCELLED", "FAILED", "REJECTED")


def cancel_payout(
    db: Session,
    *,
    payout_id: str,
    actor_id: str,
    actor_capability: str,
    expected_version: int | None,
) -> CancelOutcome:
    """Locks the Payout row first, then looks up the rail simulator record
    for its idempotency_key -- the SAME row lock `dispatch_payout` takes,
    so Postgres serializes a concurrent cancel/dispatch pair instead of
    letting them interleave. Whichever call's transaction commits first
    determines the outcome the other then sees:

      - no rail record yet (never dispatched, or dispatch hasn't run) ->
        genuine cancel: state -> CANCELLED, reservation released.
      - rail record outcome == ACCEPTED (the payment won the race) ->
        state is left exactly as the rail reported it (SUBMITTED/CONFIRMED
        from whatever dispatch/confirm already set) and
        recovery_required=True is set instead -- money moved, so this is
        NEVER marked CANCELLED (AGENT_HANDOFF.md: "do not pretend money was
        unsent").
      - rail record outcome == FAILED -> safe to cancel (nothing to
        recover); reservation released.
      - rail record outcome in (DELAYED, UNKNOWN) -> CANCEL_REQUESTED:
        neither cancelled nor accepted yet; an explicit reconciliation
        step resolves it later, same as C04 describes ("Pending cancellation
        is CANCEL_REQUESTED, not CANCELLED until confirmed").
    """
    payout = _lock_payout(db, payout_id)
    if payout is None:
        raise PayoutNotFoundError(f"payout {payout_id} not found")
    if expected_version is not None and payout.version != expected_version:
        raise PayoutVersionConflictError(expected_version, payout.version)

    if payout.state in _TERMINAL_STATES:
        db.rollback()
        return CancelOutcome(payout=payout, result="no_op_terminal_state")
    if payout.state == "CANCEL_REQUESTED":
        db.rollback()  # repeat cancel: already pending, no second event
        return CancelOutcome(payout=payout, result="cancel_requested_pending")

    record = rail_simulator.lookup(db, payout.idempotency_key)

    if record is not None and record.outcome == "ACCEPTED":
        payout.recovery_required = True
        payout.recovery_reason = (
            "cancellation requested after the simulated rail already accepted this "
            "payout -- the payment won the race; funds already moved and require a "
            "recovery/offset workflow, not a pretended cancellation"
        )
        payout.cancel_requested_at = datetime.now(UTC)
        payout.cancel_requested_by = actor_id
        payout.version += 1
        _append_payout_event(
            db,
            payout,
            event_type="payout.cancel_race_lost_recovery_flagged",
            actor_id=actor_id,
            actor_capability=actor_capability,
            payload={"provider_reference": record.provider_reference},
        )
        return CancelOutcome(payout=payout, result="accepted_with_recovery_flagged")

    if record is not None and record.outcome == "FAILED":
        payout.state = "CANCELLED"
        _release_reservation(db, payout)
        payout.cancel_requested_at = datetime.now(UTC)
        payout.cancel_requested_by = actor_id
        payout.version += 1
        _append_payout_event(
            db,
            payout,
            event_type="payout.cancelled",
            actor_id=actor_id,
            actor_capability=actor_capability,
            payload={"rail_outcome_at_cancel": "FAILED"},
        )
        return CancelOutcome(payout=payout, result="cancelled")

    if record is not None and record.outcome in ("DELAYED", "UNKNOWN"):
        payout.state = "CANCEL_REQUESTED"
        payout.cancel_requested_at = datetime.now(UTC)
        payout.cancel_requested_by = actor_id
        payout.version += 1
        _append_payout_event(
            db,
            payout,
            event_type="payout.cancel_requested_pending_reconciliation",
            actor_id=actor_id,
            actor_capability=actor_capability,
            payload={"rail_outcome_at_cancel": record.outcome},
        )
        return CancelOutcome(payout=payout, result="cancel_requested_pending")

    # No rail record at all -- never dispatched (or dispatch hasn't run
    # yet): a genuinely clean cancel, nothing to recover. Only a payout that
    # release_payout actually reserved (DISPATCH_PENDING) gives cash and
    # daily-limit headroom back; DRAFT/APPROVED never reserved anything, and
    # "releasing" for them would mint cash that was never set aside.
    was_reserved = payout.state not in ("DRAFT", "APPROVED")
    payout.state = "CANCELLED"
    if was_reserved:
        _release_reservation(db, payout)
    payout.cancel_requested_at = datetime.now(UTC)
    payout.cancel_requested_by = actor_id
    payout.version += 1
    _append_payout_event(
        db,
        payout,
        event_type="payout.cancelled",
        actor_id=actor_id,
        actor_capability=actor_capability,
        payload={"rail_outcome_at_cancel": None},
    )
    return CancelOutcome(payout=payout, result="cancelled")
