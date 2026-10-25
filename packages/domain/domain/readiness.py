"""R1 (prevention extension): pre-event contract readiness checks.

Pure, side-effect-free -- same convention as domain.term_precedence/
domain.contract_obligations/domain.case_routing: plain dataclasses in,
plain values out, no database session, no HTTP, no datetime.now(), no
randomness, no LLM call. The DB-touching orchestration (gathering real
VendorContractTerm/DocumentClaim/Vendor rows, calling the real X0.2
precedence resolver per day, writing the result) lives in
services/api/app/readiness.py.

TICKET_SPEC.md R1 (verbatim): "Deterministic checks over extracted terms...
Extraction reuses `propose_terms` with citations. Agents never set a check
result." There is no literal `propose_terms` function anywhere in this
codebase (confirmed by reading the repo before writing this module) -- the
closest, and the one CONTEXT.md explicitly names as what R1 should reuse,
is `services/api/app/agents/extraction.py`'s `extract_document` (A06's
real, citation-bearing Claude extraction into `DocumentClaim` rows, each
code-verified against the source document before being trusted at all).
See docs/progress/R1-handoff.md for the full reasoning on how this module's
checks consume that extraction output without ever asking the model "is
this ready" -- every check below is plain code over already-stored,
already-verified structured data.

The eight checks (TICKET_SPEC.md R1, verbatim names):

  SIGNED_AGREEMENT        -- at least one CODE-VERIFIED DocumentClaim for
                             this counterparty has speaker_role=
                             "SIGNED_AGREEMENT" (A06's own vocabulary for
                             "this excerpt is the text of a signed
                             agreement or amendment itself", not organizer/
                             vendor correspondence). This is deliberately
                             the ONE check that reads DocumentClaim rather
                             than VendorContractTerm: it is the real
                             evidence-of-signature signal this codebase has
                             (see scripts/eval_a06_extraction.py's own
                             `kind == "CONTRACT" and c.speaker_role ==
                             "SIGNED_AGREEMENT"` convention, reused here,
                             not reinvented).
  DATES_COVER_FESTIVAL    -- the union of ACTIVE terms' effective-date
                             windows covers every day of
                             [festival_start, festival_end] with no gap.
  COMMISSION_PRESENT      -- the ACTIVE term that actually covers each
                             festival day states its own commission_bps
                             explicitly (not silently relying on the
                             engine's global festival-default rate).
  COMMISSION_SOURCES_AGREE-- no unresolved (status="proposed") candidate
                             terms disagree on commission_bps for the same
                             period (the taco-truck conflict scenario).
  PAYMENT_TERMS_PRESENT   -- same pattern as COMMISSION_PRESENT, for
                             minimum_fee_cents.
  AMENDMENT_PRECEDENCE    -- no two ACTIVE terms of the SAME source_kind
                             have overlapping effective windows with
                             disagreeing commission/minimum-fee figures
                             (which would make "highest version wins" an
                             arbitrary, silently-hidden tie-break instead
                             of a real, resolvable precedence order).
  PAYOUT_DETAILS_VERIFIED -- the counterparty's on-file payout/beneficiary
                             details are both present and explicitly
                             marked verified.
  EVIDENCE_COMPLETE       -- every ACTIVE term that actually covers at
                             least one festival day carries a non-empty
                             source citation (`source_refs`).

Overall status (`ready | ready_with_interpretation | blocked`; "stale" is
never produced here -- see module docstring in app/readiness.py for why
that is R3's job, not this function's):
  - any failing check -> "blocked"
  - all checks pass, and at least one festival day's winning term is
    source_kind="approved_interpretation" -> "ready_with_interpretation"
  - all checks pass, no interpretation involved -> "ready"
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# Kept in sync BY HAND with domain.contract_obligations.COUNTERPARTY_TYPES
# (same "duplicated literal, cross-checked by a verify script" convention
# domain.case_routing already uses for app.models capability codes --
# domain/* modules deliberately do not import one another, same as they
# deliberately do not import services/api/app).
COUNTERPARTY_TYPES: tuple[str, ...] = ("vendor", "supplier")

STATUS_READY = "ready"
STATUS_READY_WITH_INTERPRETATION = "ready_with_interpretation"
STATUS_BLOCKED = "blocked"
STATUS_STALE = "stale"  # never returned by evaluate_readiness(); R3's job to set.

STATUSES: tuple[str, ...] = (
    STATUS_READY,
    STATUS_READY_WITH_INTERPRETATION,
    STATUS_BLOCKED,
    STATUS_STALE,
)

CHECK_SIGNED_AGREEMENT = "SIGNED_AGREEMENT"
CHECK_DATES_COVER_FESTIVAL = "DATES_COVER_FESTIVAL"
CHECK_COMMISSION_PRESENT = "COMMISSION_PRESENT"
CHECK_COMMISSION_SOURCES_AGREE = "COMMISSION_SOURCES_AGREE"
CHECK_PAYMENT_TERMS_PRESENT = "PAYMENT_TERMS_PRESENT"
CHECK_AMENDMENT_PRECEDENCE = "AMENDMENT_PRECEDENCE"
CHECK_PAYOUT_DETAILS_VERIFIED = "PAYOUT_DETAILS_VERIFIED"
CHECK_EVIDENCE_COMPLETE = "EVIDENCE_COMPLETE"

CHECK_NAMES: tuple[str, ...] = (
    CHECK_SIGNED_AGREEMENT,
    CHECK_DATES_COVER_FESTIVAL,
    CHECK_COMMISSION_PRESENT,
    CHECK_COMMISSION_SOURCES_AGREE,
    CHECK_PAYMENT_TERMS_PRESENT,
    CHECK_AMENDMENT_PRECEDENCE,
    CHECK_PAYOUT_DETAILS_VERIFIED,
    CHECK_EVIDENCE_COMPLETE,
)

# source_kind values that count as "a real, signed-or-equivalent contractual
# source" for DATES_COVER_FESTIVAL/COMMISSION_PRESENT/PAYMENT_TERMS_PRESENT/
# AMENDMENT_PRECEDENCE/EVIDENCE_COMPLETE purposes -- i.e. everything except
# the synthesized "default" fallback domain.term_precedence invents when no
# real row covers a date. Kept in sync by hand with
# domain.term_precedence.SOURCE_KINDS (same no-cross-import convention).
_CONTRACTUAL_SOURCE_KINDS = frozenset({"contract", "signed_amendment", "approved_interpretation"})
_AMENDMENT_PRECEDENCE_SOURCE_KINDS = frozenset({"contract", "signed_amendment"})


@dataclass(frozen=True)
class ReadinessTerm:
    """The fields of one VendorContractTerm (or future SupplierContractTerm)
    row that matter for readiness checking. A plain dataclass, not the ORM
    row itself -- same convention as domain.term_precedence.TermCandidate."""

    term_id: str
    source_kind: str
    status: str  # "draft" | "proposed" | "active"
    version: int
    commission_bps: int | None
    minimum_fee_cents: int | None
    effective_from: date
    effective_to: date | None
    has_source_citation: bool  # bool(term.source_refs)

    def covers(self, day: date) -> bool:
        if day < self.effective_from:
            return False
        if self.effective_to is not None and day > self.effective_to:
            return False
        return True


@dataclass(frozen=True)
class DailyResolution:
    """One day's precedence-resolved winner, mirroring
    domain.term_precedence.TermResolution's shape (term_id=None means "no
    real row covers this day; the synthesized festival default applied")."""

    day: date
    term_id: str | None
    source_kind: str


@dataclass(frozen=True)
class ReadinessInputs:
    counterparty_type: str
    counterparty_id: str
    festival_start: date
    festival_end: date
    # ALL terms for this counterparty, any status -- both "active" (the
    # currently-governing rate) and "proposed" (unresolved candidate claims,
    # needed for COMMISSION_SOURCES_AGREE).
    terms: tuple[ReadinessTerm, ...]
    daily_resolutions: tuple[DailyResolution, ...]
    has_unresolved_conflict: bool
    has_verified_signed_agreement_claim: bool
    payout_details_verified: bool

    def __post_init__(self) -> None:
        if self.counterparty_type not in COUNTERPARTY_TYPES:
            raise ValueError(
                f"unknown counterparty_type {self.counterparty_type!r}; "
                f"expected one of {COUNTERPARTY_TYPES}"
            )
        if self.festival_start > self.festival_end:
            raise ValueError(
                f"festival_start {self.festival_start} is after festival_end "
                f"{self.festival_end}"
            )


@dataclass(frozen=True)
class CheckResult:
    check: str
    passed: bool
    reason: str
    details: dict


@dataclass(frozen=True)
class ReadinessResult:
    status: str
    checks: tuple[CheckResult, ...]

    @property
    def failing_checks(self) -> tuple[str, ...]:
        return tuple(c.check for c in self.checks if not c.passed)


def _festival_days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _terms_by_id(inputs: ReadinessInputs) -> dict[str, ReadinessTerm]:
    return {t.term_id: t for t in inputs.terms}


def _check_signed_agreement(inputs: ReadinessInputs) -> CheckResult:
    passed = inputs.has_verified_signed_agreement_claim
    return CheckResult(
        check=CHECK_SIGNED_AGREEMENT,
        passed=passed,
        reason=(
            "a code-verified claim citing the text of a signed agreement is on file"
            if passed
            else "no code-verified claim citing a signed agreement/amendment was found"
        ),
        details={},
    )


def _check_dates_cover_festival(inputs: ReadinessInputs) -> CheckResult:
    days = _festival_days(inputs.festival_start, inputs.festival_end)
    active_contractual = [
        t
        for t in inputs.terms
        if t.status == "active" and t.source_kind in _CONTRACTUAL_SOURCE_KINDS
    ]
    uncovered = [d for d in days if not any(t.covers(d) for t in active_contractual)]
    passed = not uncovered
    return CheckResult(
        check=CHECK_DATES_COVER_FESTIVAL,
        passed=passed,
        reason=(
            "every festival day is covered by an active contract/amendment/interpretation term"
            if passed
            else f"{len(uncovered)} festival day(s) have no covering active term, "
            f"first uncovered day {uncovered[0].isoformat()}"
        ),
        details={"uncovered_days": [d.isoformat() for d in uncovered]},
    )


def _check_commission_present(inputs: ReadinessInputs) -> CheckResult:
    by_id = _terms_by_id(inputs)
    missing_days = []
    for res in inputs.daily_resolutions:
        term = by_id.get(res.term_id) if res.term_id else None
        if term is None or term.commission_bps is None:
            missing_days.append(res.day)
    passed = not missing_days
    return CheckResult(
        check=CHECK_COMMISSION_PRESENT,
        passed=passed,
        reason=(
            "every festival day resolves to a term with an explicit commission_bps"
            if passed
            else f"{len(missing_days)} festival day(s) have no explicit commission rate on file, "
            f"first such day {missing_days[0].isoformat()}"
        ),
        details={"missing_days": [d.isoformat() for d in missing_days]},
    )


def _check_commission_sources_agree(inputs: ReadinessInputs) -> CheckResult:
    passed = not inputs.has_unresolved_conflict
    return CheckResult(
        check=CHECK_COMMISSION_SOURCES_AGREE,
        passed=passed,
        reason=(
            "no unresolved conflicting commission-rate claims for this festival window"
            if passed
            else "two or more unresolved (status=proposed) candidate terms disagree on "
            "commission_bps for an overlapping period"
        ),
        details={},
    )


def _check_payment_terms_present(inputs: ReadinessInputs) -> CheckResult:
    by_id = _terms_by_id(inputs)
    missing_days = []
    for res in inputs.daily_resolutions:
        term = by_id.get(res.term_id) if res.term_id else None
        if term is None or term.minimum_fee_cents is None:
            missing_days.append(res.day)
    passed = not missing_days
    return CheckResult(
        check=CHECK_PAYMENT_TERMS_PRESENT,
        passed=passed,
        reason=(
            "every festival day resolves to a term with an explicit minimum_fee_cents"
            if passed
            else f"{len(missing_days)} festival day(s) have no explicit payment terms on file, "
            f"first such day {missing_days[0].isoformat()}"
        ),
        details={"missing_days": [d.isoformat() for d in missing_days]},
    )


def _windows_overlap(a: ReadinessTerm, b: ReadinessTerm) -> bool:
    a_to = a.effective_to or date.max
    b_to = b.effective_to or date.max
    return a.effective_from <= b_to and b.effective_from <= a_to


def _check_amendment_precedence(inputs: ReadinessInputs) -> CheckResult:
    active = [
        t
        for t in inputs.terms
        if t.status == "active" and t.source_kind in _AMENDMENT_PRECEDENCE_SOURCE_KINDS
    ]
    ambiguous_pairs = []
    for kind in _AMENDMENT_PRECEDENCE_SOURCE_KINDS:
        group = [t for t in active if t.source_kind == kind]
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                if _windows_overlap(a, b) and (
                    a.commission_bps != b.commission_bps
                    or a.minimum_fee_cents != b.minimum_fee_cents
                ):
                    ambiguous_pairs.append((a.term_id, b.term_id))
    passed = not ambiguous_pairs
    return CheckResult(
        check=CHECK_AMENDMENT_PRECEDENCE,
        passed=passed,
        reason=(
            "no ambiguous overlapping active terms of the same source_kind"
            if passed
            else f"{len(ambiguous_pairs)} pair(s) of active same-source_kind terms overlap "
            "with disagreeing figures"
        ),
        details={"ambiguous_pairs": ambiguous_pairs},
    )


def _check_payout_details_verified(inputs: ReadinessInputs) -> CheckResult:
    passed = inputs.payout_details_verified
    return CheckResult(
        check=CHECK_PAYOUT_DETAILS_VERIFIED,
        passed=passed,
        reason=(
            "payout/beneficiary details are on file and marked verified"
            if passed
            else "no verified payout/beneficiary details on file"
        ),
        details={},
    )


def _check_evidence_complete(inputs: ReadinessInputs) -> CheckResult:
    by_id = _terms_by_id(inputs)
    winning_term_ids = {r.term_id for r in inputs.daily_resolutions if r.term_id is not None}
    missing = [tid for tid in winning_term_ids if not by_id[tid].has_source_citation]
    passed = not missing
    return CheckResult(
        check=CHECK_EVIDENCE_COMPLETE,
        passed=passed,
        reason=(
            "every term governing a festival day carries a source citation"
            if passed
            else f"{len(missing)} governing term(s) have no source citation on file"
        ),
        details={"terms_missing_citation": missing},
    )


_CHECK_FUNCS = (
    _check_signed_agreement,
    _check_dates_cover_festival,
    _check_commission_present,
    _check_commission_sources_agree,
    _check_payment_terms_present,
    _check_amendment_precedence,
    _check_payout_details_verified,
    _check_evidence_complete,
)


def evaluate_readiness(inputs: ReadinessInputs) -> ReadinessResult:
    """Runs all eight deterministic checks and combines them into one
    overall status. Raises ValueError (via ReadinessInputs.__post_init__)
    for an unrecognized counterparty_type or an inverted festival window --
    never guesses, same "never silently default" discipline as
    domain.case_routing.route_case / domain.term_precedence.
    resolve_term_precedence."""
    results = tuple(fn(inputs) for fn in _CHECK_FUNCS)

    if any(not r.passed for r in results):
        status = STATUS_BLOCKED
    elif any(r.source_kind == "approved_interpretation" for r in inputs.daily_resolutions):
        status = STATUS_READY_WITH_INTERPRETATION
    else:
        status = STATUS_READY

    return ReadinessResult(status=status, checks=results)
