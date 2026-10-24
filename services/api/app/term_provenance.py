"""X0.2 (prevention extension): term provenance and precedence -- DB-touching
orchestration.

The pure precedence math lives in `domain.term_precedence` (packages/
domain/domain/term_precedence.py) -- read that module's docstring first.
Everything in THIS module is the glue: construct real `VendorContractTerm`
rows with a specific, structurally-enforced `source_kind`, and resolve
precedence for a real vendor across real rows.

Relationship to app/policy.py (A05): this module is NEW and ADDITIVE,
alongside (not instead of) A05's existing Case+PolicyVersion conflict
detection/resolution (`detect_contract_term_conflicts`/`resolve_case`/
`approve_case_policy`/`find_active_matching_policy`). None of those
functions are touched by this ticket. R1/R2 will be the ones to actually
wire a readiness-case decision to `record_signed_amendment`/
`record_approved_interpretation` below -- this ticket only builds the two
constructors and the precedence resolver so R-track has something real to
call (see docs/progress/X0.1-handoff.md's "Next ticket" note and
docs/progress/X0.2-handoff.md).

Structural enforcement (TICKET_SPEC.md/CONTEXT.md hard rule -- "Signed
amendment != approved interpretation: these are different things. Never
store or display one as the other"):

  - `record_signed_amendment` REQUIRES a real document citation
    (`document_source_ref` with `ref_kind="document"` and a real
    `source_document_id`) -- "a document upload is required" per
    TICKET_SPEC.md's R2 "Done when." It is structurally impossible to call
    this function with only a case citation and no document citation: the
    function does not even accept a `case_id` parameter.
  - `record_approved_interpretation` REQUIRES a real Case citation
    (`case_id`, an existing `Case` row) -- an interpretation resolves a
    specific case's conflict and is never backed by only a document the
    way an amendment is. It is structurally impossible to call this
    function with only a document citation and no case: the function does
    not even accept a `document_source_ref` parameter.
  - Neither function accepts `source_kind` as a parameter at all -- each
    one sets it internally to its own fixed value. There is no code path
    through this module where evidence gathered for one kind can be passed
    through and labeled as the other.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from domain.policy import CaseFacts
from domain.settlement import DEFAULT_COMMISSION_BPS, DEFAULT_MINIMUM_FEE_CENTS
from domain.term_precedence import (
    INTERPRETATION_LABEL,
    SOURCE_KIND_APPROVED_INTERPRETATION,
    SOURCE_KIND_SIGNED_AMENDMENT,
    TermCandidate,
    TermResolution,
    resolve_term_precedence,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models_ledger import Case, Vendor, VendorContractTerm
from app.policy import find_active_matching_policy
from app.uow import UnitOfWork


def _next_term_version(db: Session, vendor_id: str) -> int:
    max_version = db.execute(
        select(VendorContractTerm.version)
        .where(VendorContractTerm.vendor_id == vendor_id)
        .order_by(VendorContractTerm.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    return (max_version or 0) + 1


def record_signed_amendment(
    db: Session,
    uow: UnitOfWork,
    *,
    vendor: Vendor,
    commission_bps: int | None,
    minimum_fee_cents: int | None,
    processor_fee_deductible: bool,
    effective_from: date,
    effective_to: date | None,
    document_source_ref: dict,
    actor_id: str,
    actor_capability: str,
) -> VendorContractTerm:
    """Creates one new `source_kind="signed_amendment"` VendorContractTerm,
    `status="active"`. `document_source_ref` must carry `ref_kind=
    "document"` plus a real, non-empty `source_document_id` (the same
    SourceRef shape A02/A06 already use elsewhere in this codebase --
    see app/models_agents.py's DocumentClaim / scripts/dev_fixtures_a05.py's
    `_source_ref` helper for the established shape) -- this is the "a
    document upload is required" guardrail (TICKET_SPEC.md R2), enforced
    structurally here rather than left to each future caller's discipline.
    Raises ValueError, never silently substitutes a weaker citation, if the
    shape doesn't look like a real document citation.

    Deliberately does not touch `app/policy.py`'s existing Case/PolicyVersion
    flow at all -- this is a new, independent way to get an "active" term
    onto the vendor's row, alongside `resolve_case`'s existing one (see this
    module's docstring)."""
    if document_source_ref.get("ref_kind") != "document" or not document_source_ref.get(
        "source_document_id"
    ):
        raise ValueError(
            "record_signed_amendment requires document_source_ref with "
            "ref_kind='document' and a real source_document_id -- a signed "
            "amendment is never recorded from a case citation alone"
        )

    term = VendorContractTerm(
        vendor_id=vendor.id,
        version=_next_term_version(db, vendor.id),
        commission_bps=commission_bps,
        minimum_fee_cents=minimum_fee_cents,
        processor_fee_deductible=processor_fee_deductible,
        effective_from=effective_from,
        effective_to=effective_to,
        status="active",
        source_kind=SOURCE_KIND_SIGNED_AMENDMENT,
        source_refs=[document_source_ref],
        approved_by=actor_id,
    )
    db.add(term)
    db.flush()

    uow.append_event(
        stream_id=f"vendor_contract_term:{term.id}",
        event_type="vendor_contract_term.signed_amendment_recorded",
        aggregate_id=term.id,
        aggregate_version=1,
        actor_id=actor_id,
        actor_type="human",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        vendor_id=vendor.id,
        payload={
            "source_kind": SOURCE_KIND_SIGNED_AMENDMENT,
            "commission_bps": commission_bps,
            "minimum_fee_cents": minimum_fee_cents,
            "effective_from": effective_from.isoformat(),
            "effective_to": effective_to.isoformat() if effective_to else None,
            "document_source_ref": document_source_ref,
        },
        lineage_refs=[{"ref_kind": "vendor_contract_term", "ref_id": term.id}],
    )
    return term


def record_approved_interpretation(
    db: Session,
    uow: UnitOfWork,
    *,
    vendor: Vendor,
    commission_bps: int | None,
    minimum_fee_cents: int | None,
    processor_fee_deductible: bool,
    effective_from: date,
    effective_to: date | None,
    case_id: str,
    actor_id: str,
    actor_capability: str,
) -> VendorContractTerm:
    """Creates one new `source_kind="approved_interpretation"`
    VendorContractTerm, `status="active"`. `case_id` must reference a real,
    existing `Case` -- an approved interpretation is always "internal
    interpretation, not contractual" (TICKET_SPEC.md X0.2), resolving a
    specific case's conflict, never a free-floating claim recorded the way
    a signed amendment is. Raises ValueError if the case does not exist.

    R2 owns the actual decision-endpoint wiring (which open case, which
    scoped rule it creates, capability-gating the HTTP call with
    CAPABILITY_APPROVE_INTERPRETATION) -- this function only enforces that
    SOME real case is cited, structurally, the same way
    `record_signed_amendment` enforces a real document."""
    case = db.get(Case, case_id)
    if case is None:
        raise ValueError(
            f"record_approved_interpretation requires a real case_id; no Case "
            f"{case_id!r} found -- an approved interpretation is never recorded "
            "from a document citation alone"
        )

    term = VendorContractTerm(
        vendor_id=vendor.id,
        version=_next_term_version(db, vendor.id),
        commission_bps=commission_bps,
        minimum_fee_cents=minimum_fee_cents,
        processor_fee_deductible=processor_fee_deductible,
        effective_from=effective_from,
        effective_to=effective_to,
        status="active",
        source_kind=SOURCE_KIND_APPROVED_INTERPRETATION,
        source_refs=[
            {
                "ref_kind": "case",
                "ref_id": case.id,
                "label": INTERPRETATION_LABEL,
            }
        ],
        approved_by=actor_id,
    )
    db.add(term)
    db.flush()

    uow.append_event(
        stream_id=f"vendor_contract_term:{term.id}",
        event_type="vendor_contract_term.interpretation_approved",
        aggregate_id=term.id,
        aggregate_version=1,
        actor_id=actor_id,
        actor_type="human",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        vendor_id=vendor.id,
        payload={
            "source_kind": SOURCE_KIND_APPROVED_INTERPRETATION,
            "label": INTERPRETATION_LABEL,
            "commission_bps": commission_bps,
            "minimum_fee_cents": minimum_fee_cents,
            "effective_from": effective_from.isoformat(),
            "effective_to": effective_to.isoformat() if effective_to else None,
            "case_id": case.id,
        },
        lineage_refs=[
            {"ref_kind": "vendor_contract_term", "ref_id": term.id},
            {"ref_kind": "case", "ref_id": case.id},
        ],
    )
    return term


def _to_candidate(term: VendorContractTerm) -> TermCandidate:
    return TermCandidate(
        term_id=term.id,
        source_kind=term.source_kind,
        version=term.version,
        commission_bps=term.commission_bps,
        minimum_fee_cents=term.minimum_fee_cents,
        processor_fee_deductible=term.processor_fee_deductible,
        effective_from=term.effective_from,
        effective_to=term.effective_to,
    )


def resolve_vendor_term_precedence(
    db: Session,
    *,
    vendor: Vendor,
    target_date: date,
    case_facts: CaseFacts | None = None,
) -> TermResolution:
    """Gathers every real, `status="active"` VendorContractTerm row for
    `vendor` and resolves which one applies on `target_date`, per
    `domain.term_precedence`'s precedence ladder.

    Deliberately does NOT replace `app/settlement.py`'s
    `resolve_vendor_terms` (A04, used for real settlement calculation) or
    `app/policy.py`'s own `status="proposed"` candidate lookup (A05, used
    for conflict DETECTION) -- both of those stay exactly as they were.
    This is a new, parallel read, for R4's future "the terms resolver
    reuses the pre-event rule" gate-integration and for any readiness/
    statement display that wants an honestly-labeled provenance result.

    An `approved_interpretation` candidate is only even considered if
    `case_facts` is given AND an ACTIVE PolicyVersion's predicates match it
    -- the exact same `domain.policy.policy_applies` mechanism A05 already
    uses to decide whether a case can auto-resolve (via `app.policy.
    find_active_matching_policy`), reused here rather than reinvented. With
    no `case_facts` (the common case -- most vendors have no interpretation
    candidate at all), an interpretation-sourced row is never considered,
    even if its effective dates cover `target_date`."""
    rows = (
        db.execute(
            select(VendorContractTerm).where(
                VendorContractTerm.vendor_id == vendor.id,
                VendorContractTerm.status == "active",
            )
        )
        .scalars()
        .all()
    )
    candidates = [_to_candidate(t) for t in rows]

    interpretation_applies = False
    if case_facts is not None and any(
        c.source_kind == SOURCE_KIND_APPROVED_INTERPRETATION for c in candidates
    ):
        interpretation_applies = find_active_matching_policy(db, case_facts) is not None

    return resolve_term_precedence(
        candidates,
        target_date,
        interpretation_applies=interpretation_applies,
        default_commission_bps=DEFAULT_COMMISSION_BPS,
        default_minimum_fee_cents=DEFAULT_MINIMUM_FEE_CENTS,
    )
