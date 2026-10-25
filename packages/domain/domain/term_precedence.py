"""X0.2 (prevention extension): term provenance and precedence.

Pure, side-effect-free -- same convention as domain.settlement (A04) and
domain.policy (A05): plain dataclasses in, plain values out, no database
session, no HTTP, no datetime.now(), no randomness.

This module is new modeling ALONGSIDE, not instead of, A05's existing
`Case`+`PolicyVersion` contract-conflict resolution path
(services/api/app/policy.py's `detect_contract_term_conflicts`/
`resolve_case`/`approve_case_policy`, and domain.policy's `policy_applies`).
That mechanism answers "does an already-approved reusable policy cover this
new case." This module answers a different, complementary question: "of
the several VendorContractTerm rows that might apply to a vendor on a given
date, which one actually wins, and how should it be labeled." See
docs/progress/X0.2-handoff.md and docs/prevention-extension/TICKET_SPEC.md's
X0.2 section for the full rationale.

Vocabulary (`source_kind`, TICKET_SPEC.md X0.2, verbatim):
    contract | signed_amendment | approved_interpretation | default

Precedence (TICKET_SPEC.md X0.2, verbatim): "a signed amendment covering
the date beats the contract, which beats the default." `approved_
interpretation` is deliberately NOT a plain fourth rung on that same
ladder: "An approved interpretation applies only through a matching rule
when sources conflict" -- it never silently outranks a signed amendment or
a contract on date-coverage alone. `resolve_term_precedence` below only
even considers an `approved_interpretation` candidate when the caller
passes `interpretation_applies=True` -- which services/api/app/
term_provenance.py's DB-touching wrapper sets only when a real ACTIVE
PolicyVersion's predicates match the case's facts (domain.policy.
policy_applies, the SAME matching-rule mechanism A05 already uses to let a
case auto-resolve -- reused here, not reinvented).

Hard rule (TICKET_SPEC.md/CONTEXT.md, binding): "Signed amendment != approved
interpretation: these are different things. Never store or display one as
the other." This module enforces the "display" half: `_resolution_from`
below is the ONLY place a `TermResolution.label` is computed, and an
`approved_interpretation` candidate ALWAYS gets `INTERPRETATION_LABEL`
("internal interpretation, not contractual") -- there is no code path here
that can produce a `TermResolution` with `source_kind="approved_
interpretation"` and any other label, and no code path that can produce
`source_kind="signed_amendment"` while carrying that label. The "storage"
half (never letting one be CREATED labeled as the other) is enforced in
services/api/app/term_provenance.py's two constructor functions, which are
the only two sanctioned ways to create a non-"contract" term.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

SOURCE_KIND_CONTRACT = "contract"
SOURCE_KIND_SIGNED_AMENDMENT = "signed_amendment"
SOURCE_KIND_APPROVED_INTERPRETATION = "approved_interpretation"
SOURCE_KIND_DEFAULT = "default"

SOURCE_KINDS: tuple[str, ...] = (
    SOURCE_KIND_CONTRACT,
    SOURCE_KIND_SIGNED_AMENDMENT,
    SOURCE_KIND_APPROVED_INTERPRETATION,
    SOURCE_KIND_DEFAULT,
)

# Lower rank wins. `approved_interpretation` sits between `contract` and
# `default` in RANK terms only -- it is still gated by `interpretation_
# applies` below regardless of rank, so a covering interpretation candidate
# never beats a covering contract/amendment candidate just because its rank
# number happens to be lower than `default`'s.
_PRECEDENCE_RANK: dict[str, int] = {
    SOURCE_KIND_SIGNED_AMENDMENT: 0,
    SOURCE_KIND_CONTRACT: 1,
    SOURCE_KIND_APPROVED_INTERPRETATION: 2,
    SOURCE_KIND_DEFAULT: 3,
}

# The ONE literal label an approved_interpretation resolution ever carries.
# TICKET_SPEC.md: "always labeled 'internal interpretation, not
# contractual'" -- this is that exact string, defined once, never
# re-typed/paraphrased anywhere this module or its callers display a
# resolution.
INTERPRETATION_LABEL = "internal interpretation, not contractual"

_REASON_BY_KIND: dict[str, str] = {
    SOURCE_KIND_SIGNED_AMENDMENT: "signed_amendment_covers_date",
    SOURCE_KIND_CONTRACT: "contract_covers_date",
    SOURCE_KIND_APPROVED_INTERPRETATION: "approved_interpretation_matched_rule",
    SOURCE_KIND_DEFAULT: "default_term_row_covers_date",
}


@dataclass(frozen=True)
class TermCandidate:
    """The fields of one VendorContractTerm row that matter for precedence.
    A plain dataclass (not the ORM row itself), same convention as domain.
    policy.PolicyCandidate, so this module stays DB-free and independently
    testable. `commission_bps`/`minimum_fee_cents` may be None, meaning
    "use the festival default" for THIS term's own figures (A02/A04's
    documented convention -- null is never treated as zero)."""

    term_id: str
    source_kind: str
    version: int
    commission_bps: int | None
    minimum_fee_cents: int | None
    processor_fee_deductible: bool
    effective_from: date
    effective_to: date | None

    def covers(self, target_date: date) -> bool:
        if target_date < self.effective_from:
            return False
        if self.effective_to is not None and target_date > self.effective_to:
            return False
        return True


@dataclass(frozen=True)
class TermResolution:
    """The result of resolving precedence for one vendor on one date.
    `term_id` is None only for the synthesized "no real row applies, fall
    back to the festival default" result. `label` is the human-displayable
    provenance string -- always `INTERPRETATION_LABEL` for an approved_
    interpretation result, never anything else, by construction (see
    `_resolution_from`)."""

    term_id: str | None
    source_kind: str
    commission_bps: int
    minimum_fee_cents: int
    processor_fee_deductible: bool
    label: str
    reason: str


def _resolution_from(
    candidate: TermCandidate,
    *,
    reason: str,
    default_commission_bps: int,
    default_minimum_fee_cents: int,
) -> TermResolution:
    commission_bps = (
        candidate.commission_bps if candidate.commission_bps is not None else default_commission_bps
    )
    minimum_fee_cents = (
        candidate.minimum_fee_cents
        if candidate.minimum_fee_cents is not None
        else default_minimum_fee_cents
    )
    label = (
        INTERPRETATION_LABEL
        if candidate.source_kind == SOURCE_KIND_APPROVED_INTERPRETATION
        else candidate.source_kind
    )
    return TermResolution(
        term_id=candidate.term_id,
        source_kind=candidate.source_kind,
        commission_bps=commission_bps,
        minimum_fee_cents=minimum_fee_cents,
        processor_fee_deductible=candidate.processor_fee_deductible,
        label=label,
        reason=reason,
    )


def _default_resolution(
    default_commission_bps: int, default_minimum_fee_cents: int
) -> TermResolution:
    return TermResolution(
        term_id=None,
        source_kind=SOURCE_KIND_DEFAULT,
        commission_bps=default_commission_bps,
        minimum_fee_cents=default_minimum_fee_cents,
        processor_fee_deductible=False,
        label="festival default (no contract, amendment, or matched interpretation applies)",
        reason="no_candidate_covers_date",
    )


def resolve_term_precedence(
    candidates: list[TermCandidate],
    target_date: date,
    *,
    interpretation_applies: bool,
    default_commission_bps: int,
    default_minimum_fee_cents: int,
) -> TermResolution:
    """Deterministic, exact: no similarity/fuzzy matching, no "closest"
    fallback (same discipline as domain.policy.policy_applies).

    1. Every candidate's `source_kind` must be one of SOURCE_KINDS -- an
       unrecognized value raises ValueError rather than being silently
       ranked last.
    2. Only candidates whose [effective_from, effective_to] window actually
       covers `target_date` are considered at all.
    3. An `approved_interpretation` candidate is excluded entirely unless
       `interpretation_applies` is True -- it is the caller's job (see
       services/api/app/term_provenance.py) to have already confirmed a
       real matching rule (an ACTIVE PolicyVersion) applies to this
       specific vendor/date/case before passing True. This function never
       decides that for itself from dates alone.
    4. Among whatever remains, the lowest-rank `source_kind` wins
       (signed_amendment < contract < approved_interpretation < default);
       ties within the same source_kind break to the highest `version`
       (the most recently recorded row of that same kind).
    5. If nothing remains, the festival default applies -- `term_id=None`,
       `source_kind="default"`.
    """
    for c in candidates:
        if c.source_kind not in SOURCE_KINDS:
            raise ValueError(
                f"unknown source_kind {c.source_kind!r} on term {c.term_id!r}; "
                f"expected one of {SOURCE_KINDS}"
            )

    covering = [c for c in candidates if c.covers(target_date)]
    eligible = [
        c
        for c in covering
        if c.source_kind != SOURCE_KIND_APPROVED_INTERPRETATION or interpretation_applies
    ]

    if not eligible:
        return _default_resolution(default_commission_bps, default_minimum_fee_cents)

    best_rank = min(_PRECEDENCE_RANK[c.source_kind] for c in eligible)
    same_rank = [c for c in eligible if _PRECEDENCE_RANK[c.source_kind] == best_rank]
    winner = max(same_rank, key=lambda c: c.version)

    return _resolution_from(
        winner,
        reason=_REASON_BY_KIND[winner.source_kind],
        default_commission_bps=default_commission_bps,
        default_minimum_fee_cents=default_minimum_fee_cents,
    )
