"""A07 controlled payouts: pure, side-effect-free decision helpers.

Everything in this module takes plain values and returns plain values --
no database session, no HTTP, no datetime.now(), no randomness -- same
convention as domain.settlement (A04) and domain.policy (A05). The
DB-touching orchestration (locking the right rows, reading current state,
writing the result) lives in services/api/app/payout_dispatch.py.

Money rule source: AGENT_HANDOFF.md "Financial rules already approved" --
"Automatic simulated release limit: $5,000 cumulatively per vendor per
festival day. Above limit needs finance-lead approval. Split requests
cannot bypass limit." and CONTRACTS.md C04's "automatic gate" list.

The limit is a CUMULATIVE one across however many separate release
requests land on the same vendor/business_date -- this module's only job
is the one comparison that makes "splitting into smaller requests cannot
evade the limit" true: compare the PROJECTED total (already-committed
amount for this vendor/day, plus this new request) against the limit, not
just this request's own amount in isolation. The caller is responsible for
reading "already-committed" from a row locked with `SELECT ... FOR UPDATE`
in the same transaction as the reservation it is about to make (see
services/api/app/payout_dispatch.py's `release_payout`) -- that atomicity
is what actually prevents a race between two concurrent requests, not
anything in this pure module.
"""

from __future__ import annotations

from dataclasses import dataclass

# $5,000/day per vendor, AGENT_HANDOFF.md "Financial rules already
# approved". Cents, per C01.
AUTOMATIC_RELEASE_LIMIT_CENTS = 500_000

# The four simulated-rail outcomes this task's acceptance criteria name
# explicitly. "UNKNOWN" is not a transient glitch -- it is a first-class
# terminal-until-reconciled outcome (C04/C06: "unknown means reconciliation
# required, not permission to create another payment").
RAIL_OUTCOMES = ("ACCEPTED", "DELAYED", "FAILED", "UNKNOWN")


@dataclass(frozen=True)
class CumulativeLimitCheck:
    committed_prior_cents: int
    amount_cents: int
    limit_cents: int
    projected_total_cents: int
    exceeds_limit: bool


def check_cumulative_limit(
    committed_prior_cents: int,
    amount_cents: int,
    limit_cents: int = AUTOMATIC_RELEASE_LIMIT_CENTS,
) -> CumulativeLimitCheck:
    """The one comparison AGENT_HANDOFF.md's $5,000/day rule reduces to.
    `committed_prior_cents` is whatever this vendor/business_date has
    already committed toward release BEFORE this request (read by the
    caller from a row-locked aggregate, so concurrent callers see a
    consistent, serialized view -- this function itself has no concept of
    concurrency, it is pure arithmetic)."""
    if committed_prior_cents < 0 or amount_cents < 0:
        raise ValueError("committed_prior_cents and amount_cents must be non-negative")
    projected = committed_prior_cents + amount_cents
    return CumulativeLimitCheck(
        committed_prior_cents=committed_prior_cents,
        amount_cents=amount_cents,
        limit_cents=limit_cents,
        projected_total_cents=projected,
        exceeds_limit=projected > limit_cents,
    )
