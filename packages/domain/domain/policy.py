"""A05 deterministic policy-applicability engine: pure, side-effect-free.

Same package convention as domain.settlement (A04): plain dataclasses in,
plain values out, no database session, no HTTP, no datetime.now(), no
randomness, no fuzzy/similarity scoring of any kind.

AGENT_HANDOFF.md is explicit that this must be the ONLY mechanism that can
let a case auto-resolve without a fresh human approval:

    "Similar-case retrieval suggests candidates; only an authorized active
    policy with matching conditions permits autonomous handling."

    "Deterministic applicability evaluation only -- if a new case's facts
    match an ACTIVE policy's scope+predicates+validity, it auto-resolves
    citing that policy version; precedent/similar-case retrieval may
    *suggest* candidates but is never itself authority to auto-resolve."

So `policy_applies` below does exact equality checks only (scope keys,
predicate keys, status, validity window) -- never a distance/similarity
threshold. A case whose facts don't match on every dimension the policy
actually constrains MUST re-escalate, not auto-resolve on a "close enough"
basis. `policy_applies` returns a reason string for every non-match so a
caller (and a human reading the case) can see WHY a policy did not apply
-- "new facts, contradictions, policy expiry, or scope mismatch must
re-escalate a case" needs to be visibly true, not just silently true.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class CaseFacts:
    """The concrete facts of one case, as recorded at the moment it was
    detected -- used both to decide whether an existing ACTIVE policy
    applies, and (verbatim, not re-derived) as the `predicates` of any NEW
    policy a human later approves from this case's resolution."""

    vendor_id: str
    festival_id: str
    reason_code: str
    business_date: date
    predicate_values: dict


@dataclass(frozen=True)
class PolicyCandidate:
    """The fields of one PolicyVersion row that matter for applicability.
    Deliberately a plain dataclass (not the ORM row itself) so this module
    stays DB-free and independently testable."""

    policy_id: str
    version: int
    scope: dict
    predicates: dict
    effect: dict
    status: str
    valid_from: date | None
    valid_to: date | None


def policy_applies(policy: PolicyCandidate, facts: CaseFacts) -> tuple[bool, str]:
    """Returns (applies, reason). `reason` is "" when applies is True, and
    a stable machine-readable string naming the first failing check
    otherwise -- never a blank/silent False. Checks, in order:

      1. status must be ACTIVE (DRAFT/SUSPENDED/EXPIRED/SUPERSEDED/REVOKED
         can never auto-resolve anything, regardless of how well the facts
         otherwise match -- "suspending a policy must not delete its
         history" implies a suspended policy's PAST applications stay
         visible, but it must not gain any NEW ones).
      2. validity window: business_date must fall within
         [valid_from, valid_to] (either bound may be open/None).
      3. scope: every key the policy's scope dict actually sets (e.g.
         vendor_id, festival_id, reason_code) must equal the case's
         corresponding fact. A scope key the policy does not set is not
         constrained (e.g. a policy with no "reason_code" in scope applies
         across reason codes, by design -- the caller decides how narrowly
         to scope a policy when approving it).
      4. predicates: every key/value the policy's predicates dict sets
         must equal `facts.predicate_values`'s value for that key EXACTLY.
         A changed fact (e.g. a third source now disagreeing with the
         recorded conflict amounts) fails here, not silently passes.
    """
    if policy.status != "ACTIVE":
        return False, f"policy_status_not_active:{policy.status}"

    if policy.valid_from is not None and facts.business_date < policy.valid_from:
        return False, "policy_not_yet_valid"
    if policy.valid_to is not None and facts.business_date > policy.valid_to:
        return False, "policy_expired"

    scope_vendor = policy.scope.get("vendor_id")
    if scope_vendor is not None and scope_vendor != facts.vendor_id:
        return False, "scope_vendor_mismatch"
    scope_festival = policy.scope.get("festival_id")
    if scope_festival is not None and scope_festival != facts.festival_id:
        return False, "scope_festival_mismatch"
    scope_reason = policy.scope.get("reason_code")
    if scope_reason is not None and scope_reason != facts.reason_code:
        return False, "scope_reason_code_mismatch"

    for key, expected in policy.predicates.items():
        if facts.predicate_values.get(key) != expected:
            return False, f"predicate_mismatch:{key}"

    return True, ""
