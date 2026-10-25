"""Which contract term bills a night, when several of them cover it.

A04 picked the highest-version active term covering the date. Two kinds of
row compete there and they are not interchangeable:

  * a STANDING rate, written by A06 from the signed documents, open-ended
    (`effective_to IS NULL`) -- "this is what the contract says";
  * a DECIDED night, written by A05 when a human resolved a conflict case
    or a policy auto-resolved one, deliberately bounded to the night(s)
    that were actually decided (app/policy.py `_activate_resolved_term`:
    "an open-ended term here would let every remaining night of the
    festival bill at a rate no human decided for it").

Ordering by version alone means whichever was written LAST wins, so a
standing rate written after a decision -- a re-extraction that reads the
contract slightly differently, a newly signed amendment -- silently
rebills every night a human already decided, at the rate the human decided
against. The human is not asked again and nothing opens a case.

These tests pin the precedence: for a given night, a term written FOR that
night beats a standing rate that merely covers it, whatever order they
were written in.
"""

from __future__ import annotations

from datetime import date

import pytest
from app.models_ledger import Vendor, VendorContractTerm
from app.settlement import (
    DEFAULT_COMMISSION_BPS,
    DEFAULT_MINIMUM_FEE_CENTS,
    resolve_vendor_terms,
)
from app.terms_resolution import (
    BLOCKING_REASON_CODES,
    VENDOR_TERMS_UNEVIDENCED_REASON,
    unevidenced_reasons,
    unevidenced_unresolved,
)
from domain.terms import TermsResolution

NIGHT = date(2026, 10, 16)
CONTRACT_START = date(2026, 8, 7)

# The demo's real shape (vendor ven_5aa2250fb593d2e8, Taco Libre): a 15%
# standing rate from the signed contract, and a human decision covering
# nights 16-17 after the producer-email conflict.
STANDING_BPS = 1500
DECIDED_BPS = 1000


@pytest.fixture()
def vendor(db_session) -> Vendor:
    v = Vendor(festival_id="gcmf-2026-term-precedence", name="Term Precedence Vendor")
    db_session.add(v)
    db_session.flush()
    return v


def _term(
    db,
    vendor: Vendor,
    *,
    version: int,
    bps: int,
    frm: date,
    to: date | None,
    status: str = "active",
    approved_by: str | None = None,
) -> VendorContractTerm:
    term = VendorContractTerm(
        vendor_id=vendor.id,
        version=version,
        commission_bps=bps,
        minimum_fee_cents=None,
        processor_fee_deductible=False,
        effective_from=frm,
        effective_to=to,
        status=status,
        source_refs=[],
        approved_by=approved_by,
    )
    db.add(term)
    db.flush()
    return term


def test_a_decided_night_beats_a_standing_rate_written_after_it(db_session, vendor):
    """The regression. v1 is the contract's standing rate, v2 is the night
    a human decided at 10%, v3 is the standing rate written again later --
    a new version, a higher number, and open-ended, so it covers the
    decided night too."""
    _term(db_session, vendor, version=1, bps=STANDING_BPS, frm=CONTRACT_START, to=None)
    decided = _term(
        db_session, vendor, version=2, bps=DECIDED_BPS, frm=NIGHT, to=NIGHT, approved_by=None
    )
    _term(db_session, vendor, version=3, bps=STANDING_BPS, frm=CONTRACT_START, to=None)

    bps, _minimum, _deductible, term = resolve_vendor_terms(db_session, vendor.id, NIGHT)
    assert (bps, term.id) == (DECIDED_BPS, decided.id)


def test_a_decided_night_beats_a_standing_rate_written_before_it(db_session, vendor):
    """The order the demo happens to be in. This already worked; it must
    keep working, because the fix must not simply invert the comparison."""
    _term(db_session, vendor, version=1, bps=STANDING_BPS, frm=CONTRACT_START, to=None)
    decided = _term(db_session, vendor, version=2, bps=DECIDED_BPS, frm=NIGHT, to=NIGHT)

    bps, _minimum, _deductible, term = resolve_vendor_terms(db_session, vendor.id, NIGHT)
    assert (bps, term.id) == (DECIDED_BPS, decided.id)


def test_a_standing_rate_still_bills_every_night_nobody_decided(db_session, vendor):
    """The decision covers night 16 only (A05 bounds it on purpose), so
    night 17 bills at the contract's standing rate -- not at the rate a
    human chose for a different night."""
    _term(db_session, vendor, version=1, bps=STANDING_BPS, frm=CONTRACT_START, to=None)
    _term(db_session, vendor, version=2, bps=DECIDED_BPS, frm=NIGHT, to=NIGHT)

    bps, _minimum, _deductible, _term_row = resolve_vendor_terms(
        db_session, vendor.id, date(2026, 10, 17)
    )
    assert bps == STANDING_BPS


def test_the_newest_standing_rate_wins_among_standing_rates(db_session, vendor):
    """A signed amendment is a new standing rate from its own date. Between
    two open-ended terms there is nothing to prefer but recency."""
    _term(db_session, vendor, version=1, bps=STANDING_BPS, frm=CONTRACT_START, to=None)
    amended = _term(db_session, vendor, version=2, bps=1200, frm=date(2026, 10, 15), to=None)

    bps, _minimum, _deductible, term = resolve_vendor_terms(db_session, vendor.id, NIGHT)
    assert (bps, term.id) == (1200, amended.id)


def test_the_newest_decision_wins_among_decisions_for_the_same_night(db_session, vendor):
    """Two bounded terms covering the same night: a correction of a
    decision is itself a decision, and the later one stands."""
    _term(db_session, vendor, version=1, bps=DECIDED_BPS, frm=NIGHT, to=NIGHT)
    corrected = _term(db_session, vendor, version=2, bps=1250, frm=NIGHT, to=NIGHT)

    bps, _minimum, _deductible, term = resolve_vendor_terms(db_session, vendor.id, NIGHT)
    assert (bps, term.id) == (1250, corrected.id)


def test_the_narrower_window_wins_between_two_bounded_terms(db_session, vendor):
    """`_cover_night_from_policy` widens a decision's term to span the
    nights a policy has been checked against, so bounded windows are not
    all one night long. A term covering this night ALONE was decided for
    this night; a wider one was decided for a range that includes it."""
    _term(db_session, vendor, version=1, bps=DECIDED_BPS, frm=NIGHT, to=date(2026, 10, 18))
    exact = _term(db_session, vendor, version=2, bps=1250, frm=NIGHT, to=NIGHT)
    # ... and written in the other order, so the rule is not accidental.
    bps, _minimum, _deductible, term = resolve_vendor_terms(db_session, vendor.id, NIGHT)
    assert (bps, term.id) == (1250, exact.id)

    _term(db_session, vendor, version=3, bps=DECIDED_BPS, frm=NIGHT, to=date(2026, 10, 18))
    bps, _minimum, _deductible, term = resolve_vendor_terms(db_session, vendor.id, NIGHT)
    assert (bps, term.id) == (1250, exact.id)


def test_proposed_and_out_of_window_terms_are_never_billed(db_session, vendor):
    """Precedence must not reach past `status`/window filtering: the two
    `proposed` rows A06 writes for a conflict are evidence for A05, never
    a rate, and they are bounded to the night -- i.e. exactly the shape
    the new ordering prefers."""
    _term(db_session, vendor, version=1, bps=STANDING_BPS, frm=CONTRACT_START, to=None)
    _term(db_session, vendor, version=2, bps=DECIDED_BPS, frm=NIGHT, to=NIGHT, status="proposed")
    _term(db_session, vendor, version=3, bps=800, frm=date(2026, 10, 18), to=date(2026, 10, 18))

    bps, _minimum, _deductible, _term_row = resolve_vendor_terms(db_session, vendor.id, NIGHT)
    assert bps == STANDING_BPS


def test_no_term_at_all_falls_back_to_the_festival_default(db_session, vendor):
    bps, minimum, deductible, term = resolve_vendor_terms(db_session, vendor.id, NIGHT)
    assert (bps, minimum, deductible, term) == (
        DEFAULT_COMMISSION_BPS,
        DEFAULT_MINIMUM_FEE_CENTS,
        False,
        None,
    )


# ---------------------------------------------------------------------------
# A vendor with no evidenced rate is held, not quietly billed at the default
# ---------------------------------------------------------------------------


def test_a_vendor_with_no_term_in_force_is_unevidenced(db_session, vendor):
    """The question is asked of A04's own selector: with no term covering
    the night, A04 bills the festival default, and that is the silent
    fallback this case exists to stop."""
    assert unevidenced_reasons(
        db_session, vendor_id=vendor.id, business_date=NIGHT, res=TermsResolution()
    ) == ["no_terms_in_force"]


def test_a_term_in_force_is_evidence_whoever_wrote_it(db_session, vendor):
    """A rate a human decided or a policy covered is evidence even when
    today's document claims yield nothing: A04 bills from it, so nothing
    is silent."""
    _term(db_session, vendor, version=1, bps=DECIDED_BPS, frm=NIGHT, to=NIGHT)
    assert (
        unevidenced_reasons(
            db_session, vendor_id=vendor.id, business_date=NIGHT, res=TermsResolution()
        )
        == []
    )


def test_a_term_that_does_not_cover_the_night_is_not_evidence_for_it(db_session, vendor):
    _term(db_session, vendor, version=1, bps=DECIDED_BPS, frm=NIGHT, to=NIGHT)
    assert unevidenced_reasons(
        db_session,
        vendor_id=vendor.id,
        business_date=date(2026, 10, 17),
        res=TermsResolution(),
    ) == ["no_terms_in_force"]


def test_a_self_contradicting_contract_is_unevidenced_even_with_a_rate(db_session, vendor):
    _term(db_session, vendor, version=1, bps=STANDING_BPS, frm=CONTRACT_START, to=None)
    res = TermsResolution(commission_bps=STANDING_BPS)
    res.unresolved = [
        "contract_states_conflicting_minimum_fee_cents",
        "contract_minimum_period_unsupported:NIGHTLY",
    ]
    assert unevidenced_reasons(db_session, vendor_id=vendor.id, business_date=NIGHT, res=res) == [
        "contract_minimum_period_unsupported:NIGHTLY",
        "contract_states_conflicting_minimum_fee_cents",
    ]


def test_unsigned_statements_alone_never_hold_a_vendor():
    """Holding on these would hold a vendor for somebody else's email: the
    signed contract stands, which is the stance `minimum_period_claim`
    already takes."""
    res = TermsResolution(commission_bps=STANDING_BPS)
    res.unresolved = [
        "minimum_period_statement:vendor:NIGHTLY",
        "unhandled_statement:producer:commission_bps:1000",
    ]
    assert unevidenced_unresolved(res) == []


def test_the_unevidenced_reason_code_blocks_payouts():
    """The reason code has to be in the blocking set, or the case opens and
    withholds nothing -- which was the old behaviour with extra steps."""
    assert VENDOR_TERMS_UNEVIDENCED_REASON in BLOCKING_REASON_CODES
