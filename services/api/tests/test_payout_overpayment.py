"""Every way the system could pay a vendor more than it owes.

Round-3 review found four. They share one root cause: "already paid" was
read as state == "CONFIRMED", so a payout that had entered the payment flow
but not yet been confirmed counted as nothing, and a fresh obligation for the
full settlement figure could be issued beside it. UNKNOWN is the sharp edge --
there the rail never answered, so the transfer may well have happened.

These tests pin the rule itself (domain.corrections.COMMITTED_STATES covers
every state that is neither a draft nor reported never-paid) and then each
path that consumes it, against the real Postgres test database.
"""

from __future__ import annotations

from datetime import date

import pytest
from app.models_ledger import Payout, Vendor
from app.models_settlement import Settlement
from app.payout_dispatch import (
    PayoutBlockedError,
    committed_cents_for_entitlement,
    create_payout_obligation,
    settlement_chain_ids,
)
from domain.corrections import (
    COMMITTED_STATES,
    COMPLETED_STATES,
    DRAFT_STATES,
    NEVER_PAID_STATES,
    PENDING_STATES,
)

ALL_PAYOUT_STATES = {
    "DRAFT",
    "APPROVED",
    "DISPATCH_PENDING",
    "SUBMITTED",
    "UNKNOWN",
    "CANCEL_REQUESTED",
    "CONFIRMED",
    "CANCELLED",
    "FAILED",
    "REJECTED",
}

# Stated literally, NOT derived from COMMITTED_STATES: parametrising over the
# constant under test means narrowing that constant shrinks the test matrix
# instead of failing it, which is how this bug survived in the first place.
MONEY_IS_OUT_STATES = (
    "DISPATCH_PENDING",
    "SUBMITTED",
    "UNKNOWN",
    "CANCEL_REQUESTED",
    "CONFIRMED",
)
MONEY_NEVER_LEFT_STATES = ("DRAFT", "APPROVED", "CANCELLED", "FAILED", "REJECTED")

ACTOR = "test-actor"
CAP = "approve_payout"


def _vendor(db) -> Vendor:
    v = Vendor(festival_id="gcmf-2026-overpay", name="Overpayment Test Vendor")
    db.add(v)
    db.flush()
    return v


def _settlement(db, vendor: Vendor, *, payout_cents: int, correction_of: str | None = None):
    s = Settlement(
        festival_id=vendor.festival_id,
        vendor_id=vendor.id,
        business_date=date(2026, 10, 16),
        calculation_version="a04-settlement-v1",
        commission_bps_used=1500,
        minimum_fee_cents_used=50000,
        processor_fee_deductible_used=False,
        eligible_sales_cents=payout_cents,
        centrally_held_cents=payout_cents,
        vendor_retained_cash_cents=0,
        cumulative_eligible_sales_cents=payout_cents,
        cumulative_commission_assessed_prior_cents=0,
        assessed_fee_cents=0,
        collected_fee_cents=0,
        payout_cents=payout_cents,
        status="CORRECTION" if correction_of else "FINAL",
        correction_of=correction_of,
    )
    db.add(s)
    db.flush()
    return s


def _payout(db, vendor: Vendor, settlement: Settlement, *, state: str, cents: int, key: str):
    p = Payout(
        logical_obligation_id=key,
        idempotency_key=key,
        vendor_id=vendor.id,
        business_date=settlement.business_date,
        amount_cents=cents,
        state=state,
        settlement_id=settlement.id,
    )
    db.add(p)
    db.flush()
    return p


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_every_payout_state_is_classified_exactly_once():
    """If a new state is added and not classified, money silently stops being
    counted as paid. That must fail here rather than in a payout run."""
    groups = [set(DRAFT_STATES), set(NEVER_PAID_STATES), set(COMMITTED_STATES)]
    union: set[str] = set()
    for g in groups:
        assert not (union & g), f"state classified twice: {union & g}"
        union |= g
    assert union == ALL_PAYOUT_STATES, f"unclassified: {ALL_PAYOUT_STATES - union}"


def test_committed_states_include_in_flight_not_just_confirmed():
    """The specific regression: CONFIRMED alone is not "already paid"."""
    assert set(COMMITTED_STATES) == set(MONEY_IS_OUT_STATES)
    assert set(DRAFT_STATES) | set(NEVER_PAID_STATES) == set(MONEY_NEVER_LEFT_STATES)
    assert set(COMPLETED_STATES) <= set(COMMITTED_STATES)
    assert set(PENDING_STATES) <= set(COMMITTED_STATES)


# ---------------------------------------------------------------------------
# Counting what is already committed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", MONEY_IS_OUT_STATES)
def test_a_payout_in_any_committed_state_counts_as_paid(db_session, state):
    v = _vendor(db_session)
    s = _settlement(db_session, v, payout_cents=100_000)
    _payout(db_session, v, s, state=state, cents=40_000, key=f"k-{state}")
    committed, rows = committed_cents_for_entitlement(db_session, s)
    assert committed == 40_000, f"{state} was not counted as committed money"
    assert [r.state for r in rows] == [state]


@pytest.mark.parametrize("state", MONEY_NEVER_LEFT_STATES)
def test_a_draft_or_never_paid_payout_does_not_count_as_paid(db_session, state):
    """Money that was never sent, or that the rail said did not move, must not
    reduce what the vendor is owed -- that would underpay."""
    v = _vendor(db_session)
    s = _settlement(db_session, v, payout_cents=100_000)
    _payout(db_session, v, s, state=state, cents=40_000, key=f"k-{state}")
    committed, rows = committed_cents_for_entitlement(db_session, s)
    assert committed == 0
    assert rows == []


def test_committed_money_is_summed_across_the_correction_history(db_session):
    """The amount paid belongs to the entitlement, not to one row of it."""
    v = _vendor(db_session)
    original = _settlement(db_session, v, payout_cents=100_000)
    correction = _settlement(db_session, v, payout_cents=120_000, correction_of=original.id)
    _payout(db_session, v, original, state="CONFIRMED", cents=100_000, key="orig")
    _payout(db_session, v, correction, state="SUBMITTED", cents=15_000, key="add")

    assert settlement_chain_ids(db_session, correction) == [correction.id, original.id]
    committed, rows = committed_cents_for_entitlement(db_session, correction)
    assert committed == 115_000
    assert len(rows) == 2


def test_a_correction_cycle_does_not_hang(db_session):
    v = _vendor(db_session)
    a = _settlement(db_session, v, payout_cents=100_000)
    b = _settlement(db_session, v, payout_cents=100_000, correction_of=a.id)
    a.correction_of = b.id  # pathological, but must terminate
    db_session.flush()
    assert set(settlement_chain_ids(db_session, b)) == {a.id, b.id}


# ---------------------------------------------------------------------------
# The obligation itself
# ---------------------------------------------------------------------------


def test_a_first_obligation_covers_the_whole_payout(db_session):
    """Unchanged behaviour for the ordinary case."""
    v = _vendor(db_session)
    s = _settlement(db_session, v, payout_cents=340_000)
    payout, created = create_payout_obligation(
        db_session, settlement_id=s.id, actor_id=ACTOR, actor_capability=CAP
    )
    assert created is True
    assert payout.amount_cents == 340_000


@pytest.mark.parametrize("state", MONEY_IS_OUT_STATES)
def test_an_obligation_covers_only_what_is_still_owed(db_session, state):
    """The core fix: a payout already in the payment flow is subtracted, so a
    recalculation cannot mint a second full payout beside it."""
    v = _vendor(db_session)
    original = _settlement(db_session, v, payout_cents=100_000)
    _payout(db_session, v, original, state=state, cents=100_000, key=f"paid-{state}")
    correction = _settlement(db_session, v, payout_cents=130_000, correction_of=original.id)

    payout, created = create_payout_obligation(
        db_session, settlement_id=correction.id, actor_id=ACTOR, actor_capability=CAP
    )
    assert created is True
    assert payout.amount_cents == 30_000, (
        f"with {state} money already out, the obligation must cover the 30,000 "
        f"difference, not the full 130,000"
    )


def test_nothing_further_owed_is_refused_rather_than_paid_again(db_session):
    v = _vendor(db_session)
    original = _settlement(db_session, v, payout_cents=100_000)
    _payout(db_session, v, original, state="CONFIRMED", cents=100_000, key="paid")
    # A recalculation that lands on the SAME figure owes nothing more.
    correction = _settlement(db_session, v, payout_cents=100_000, correction_of=original.id)
    with pytest.raises(PayoutBlockedError) as exc:
        create_payout_obligation(
            db_session, settlement_id=correction.id, actor_id=ACTOR, actor_capability=CAP
        )
    assert exc.value.reason_code == "nothing_further_owed"
    assert exc.value.details["already_committed_cents"] == 100_000


def test_an_overpaid_entitlement_is_refused_not_given_a_negative_payout(db_session):
    """A correction DOWNWARDS after payment is a recovery, never a payout."""
    v = _vendor(db_session)
    original = _settlement(db_session, v, payout_cents=425_000)
    _payout(db_session, v, original, state="CONFIRMED", cents=425_000, key="paid")
    correction = _settlement(db_session, v, payout_cents=340_000, correction_of=original.id)
    with pytest.raises(PayoutBlockedError) as exc:
        create_payout_obligation(
            db_session, settlement_id=correction.id, actor_id=ACTOR, actor_capability=CAP
        )
    assert exc.value.reason_code == "nothing_further_owed"


def test_a_second_obligation_is_refused_while_a_draft_one_is_outstanding(db_session):
    """A09 creates a ":additional" draft for the difference. POST /payouts on
    the same settlement must not create another draft for the same money."""
    v = _vendor(db_session)
    s = _settlement(db_session, v, payout_cents=100_000)
    _payout(
        db_session, v, s, state="DRAFT", cents=20_000, key=f"payout-obligation:{s.id}:additional"
    )
    with pytest.raises(PayoutBlockedError) as exc:
        create_payout_obligation(
            db_session, settlement_id=s.id, actor_id=ACTOR, actor_capability=CAP
        )
    assert exc.value.reason_code == "obligation_already_outstanding"


def test_the_same_settlement_returns_the_same_obligation(db_session):
    """Idempotency is unchanged by the arithmetic above."""
    v = _vendor(db_session)
    s = _settlement(db_session, v, payout_cents=50_000)
    first, created_first = create_payout_obligation(
        db_session, settlement_id=s.id, actor_id=ACTOR, actor_capability=CAP
    )
    second, created_second = create_payout_obligation(
        db_session, settlement_id=s.id, actor_id=ACTOR, actor_capability=CAP
    )
    assert created_first is True and created_second is False
    assert first.id == second.id
    assert second.amount_cents == 50_000


@pytest.mark.parametrize("state", DRAFT_STATES)
def test_a_superseded_settlements_unreleased_obligation_does_not_block_its_correction(
    db_session, state
):
    """One outstanding obligation per vendor-night stops a duplicated night
    getting one obligation per copy. It must not stop a CORRECTION: the
    original's unreleased obligation can never be released once a correction
    supersedes it (`release_payout` refuses with
    settlement_superseded_by_correction), so counting it as outstanding
    money left the correction unable to get an obligation at all -- a
    deadlock, found by verify_a07 scenario M.
    """
    v = _vendor(db_session)
    original = _settlement(db_session, v, payout_cents=200_000)
    _payout(db_session, v, original, state=state, cents=200_000, key=f"orig-{state}")
    correction = _settlement(db_session, v, payout_cents=180_000, correction_of=original.id)

    payout, created = create_payout_obligation(
        db_session, settlement_id=correction.id, actor_id=ACTOR, actor_capability=CAP
    )
    assert created is True
    # Nothing is committed (a draft/approved payout is not money out), so the
    # correction's obligation covers its whole figure.
    assert payout.amount_cents == 180_000
    assert payout.settlement_id == correction.id


def test_a_live_draft_on_the_same_night_still_blocks_a_second_obligation(db_session):
    """The guard the test above must not have weakened: with no correction
    superseding it, an outstanding draft still blocks."""
    v = _vendor(db_session)
    a = _settlement(db_session, v, payout_cents=200_000)
    b = _settlement(db_session, v, payout_cents=200_000)  # duplicate night, not a correction
    _payout(db_session, v, a, state="APPROVED", cents=200_000, key="live-draft")

    with pytest.raises(PayoutBlockedError) as exc:
        create_payout_obligation(
            db_session, settlement_id=b.id, actor_id=ACTOR, actor_capability=CAP
        )
    assert exc.value.reason_code == "obligation_already_outstanding"
