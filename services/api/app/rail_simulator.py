"""Simulated payment-rail provider.

PAYMENT_MODE is simulated for v1 (C00): this module is explicitly NOT a
real payment processor integration. `require_simulated_payment_mode()` is
called at the one entry point that would stand in for a real provider call
(`submit_payment`) so a misconfigured `PAYMENT_MODE` fails loudly instead of
silently pretending to move real money.

Why this is modeled as a SEPARATE table with its OWN commit, not folded
into `UnitOfWork`'s single-transaction pattern: C06 says "never claim
atomicity across PostgreSQL and external provider." A real payment rail is
a genuinely separate system -- its "yes, I accepted this" cannot be rolled
back by our database transaction failing afterward. Modeling that
separation for real (a distinct commit boundary) is what makes the
worker-crash-mid-dispatch scenario a real test instead of a comment: if a
crash happens between this module's commit and the caller's own
domain-state update, the caller MUST re-query this table on resume, never
blindly resubmit.

The core safety property, and the literal mechanism behind "a resumed or
retried dispatch is a safe no-op or safely resolves to the same single
outcome": `submit_payment` always looks up `idempotency_key` FIRST. An
existing record is returned unchanged -- the `simulate_outcome` argument is
IGNORED on a lookup hit, because the rail has already decided and a retry
cannot rewrite history.
"""

from __future__ import annotations

from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models_payout_dispatch import RailSimulatorRecord

Outcome = Literal["ACCEPTED", "DELAYED", "FAILED", "UNKNOWN"]
VALID_OUTCOMES = ("ACCEPTED", "DELAYED", "FAILED", "UNKNOWN")


class PaymentModeError(RuntimeError):
    """Raised when PAYMENT_MODE is not 'simulated'. There is no real-payment
    code path in this repository -- this is a fail-closed guard, not a
    feature flag with two real implementations."""


def require_simulated_payment_mode() -> None:
    mode = get_settings().payment_mode
    if mode != "simulated":
        raise PaymentModeError(
            f"PAYMENT_MODE={mode!r} is not supported -- this repository only "
            "implements a simulated rail (C00). Refusing to proceed rather than "
            "silently behaving as if a real payment processor were configured."
        )


def lookup(db: Session, idempotency_key: str) -> RailSimulatorRecord | None:
    return db.execute(
        select(RailSimulatorRecord).where(RailSimulatorRecord.idempotency_key == idempotency_key)
    ).scalar_one_or_none()


def submit_payment(
    db: Session,
    *,
    idempotency_key: str,
    vendor_id: str,
    amount_cents: int,
    simulate_outcome: Outcome = "ACCEPTED",
) -> tuple[RailSimulatorRecord, bool]:
    """Idempotent "submit to the rail". Returns (record, was_newly_submitted).

    Call this with a Session that has NO unrelated uncommitted work pending
    -- it commits immediately on a genuine new submission (see module
    docstring), which would also flush/commit anything else pending on the
    same Session. app/payout_dispatch.py's `dispatch_payout` is written so
    that nothing is mutated on `db` before this call.
    """
    require_simulated_payment_mode()

    existing = lookup(db, idempotency_key)
    if existing is not None:
        return existing, False

    if simulate_outcome not in VALID_OUTCOMES:
        raise ValueError(f"unknown simulated outcome: {simulate_outcome!r}")

    record = RailSimulatorRecord(
        idempotency_key=idempotency_key,
        vendor_id=vendor_id,
        amount_cents=amount_cents,
        outcome=simulate_outcome,
        provider_reference=f"SIM-{idempotency_key[:40]}",
    )
    db.add(record)
    # Deliberately its own commit boundary -- see module docstring. This is
    # the "external effect" C06 says cannot be rolled back by our own
    # transaction failing afterward.
    db.commit()
    return record, True


def resolve_outcome(
    db: Session, idempotency_key: str, *, new_outcome: Outcome
) -> RailSimulatorRecord | None:
    """Explicit, human/reconciliation-triggered transition for a
    DELAYED/UNKNOWN record -- simulates the provider eventually reporting a
    final outcome on its own schedule. Never called implicitly by a retry;
    only by app/payout_dispatch.py's `reconcile_unknown_payout`, itself an
    explicit capability-gated command. "Unknown outcomes stay unknown until
    an explicit reconciliation step resolves them" (C06) -- this function
    IS that step, nothing else mutates `outcome`."""
    if new_outcome not in VALID_OUTCOMES:
        raise ValueError(f"unknown simulated outcome: {new_outcome!r}")
    record = lookup(db, idempotency_key)
    if record is None:
        return None
    record.outcome = new_outcome
    db.commit()
    return record
