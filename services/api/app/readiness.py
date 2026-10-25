"""R1 (prevention extension): readiness records and checks.

DB-touching orchestration on top of packages/domain/domain/readiness.py's
pure check logic -- same split as every other domain-adjacent module in
this codebase (app/term_provenance.py wraps domain.term_precedence,
app/contract_obligations.py wraps domain.contract_obligations). Read that
module's docstring first for what each of the eight checks actually means.

Extraction reuse (TICKET_SPEC.md R1: "Extraction reuses `propose_terms`
with citations. Agents never set a check result"): there is no literal
`propose_terms` function anywhere in this codebase (confirmed before
writing this module, per the task's own read-first instructions) -- the
real, citation-bearing agent-proposal pattern CONTEXT.md names as R1's
dependency is `app/agents/extraction.py`'s `extract_document` (A06). This
module's `SIGNED_AGREEMENT` check reads the real output of that function
(`DocumentClaim` rows, each already code-verified against its source
document by `domain.extraction.verify_claim` before this module ever sees
it) -- it never calls the model itself, and never asks the model "is this
ready." The other seven checks read `VendorContractTerm`/`Vendor`/`Case`
rows exactly the way A05/X0.2 already do; this ticket does NOT build the
"claims -> VendorContractTerm" conversion pipeline `models_agents.py`'s own
docstring names as future work (`app/terms_resolution.py`, which does not
exist) -- that is a genuinely separate, larger piece of work than R1's own
"Done when" asks for. See docs/progress/R1-handoff.md for the full
reasoning.

Vendor-only for now (TICKET_SPEC.md R1: "counterparty here can be vendor OR
supplier ... for R1, focus on vendor counterparties ... don't hardcode the
schema to vendor-only if it's trivial not to"): the SCHEMA
(`counterparty_type`/`counterparty_id` on `ReadinessCheck`) is generic: see
app/models_readiness.py. The LOGIC in this module is not -- there is no
`SupplierContractTerm` model anywhere in this codebase (confirmed by
reading the repo), so `compute_readiness` raises
`UnsupportedCounterpartyTypeError` (a clear, loud ValueError subclass, never
a silent no-op) for `counterparty_type="supplier"`. A future supplier-
readiness ticket adds its own data source and its own call into
`domain.readiness.evaluate_readiness` (which is already generic over
`counterparty_type`) -- it does not need this ticket's schema to change.

R1 explicitly does NOT open any Case (TICKET_SPEC.md: "R2 Readiness cases
and decisions [must] (after R1, X0.1)" -- case-opening is R2's job, not
R1's). `compute_readiness` reuses `app.policy.find_candidate_conflicting_
terms` directly (the pure detection function) for COMMISSION_SOURCES_AGREE,
never `app.policy.detect_contract_term_conflicts` (which also OPENS a real
Case as a side effect) -- this keeps R1's own compute path free of any
case-opening side effect.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta

from domain.policy import CaseFacts
from domain.readiness import (
    COUNTERPARTY_TYPES,
    DailyResolution,
    ReadinessInputs,
    ReadinessTerm,
    evaluate_readiness,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models_agents import DocumentClaim, DocumentExtraction
from app.models_ledger import PolicyVersion, SourceVersion, Vendor, VendorContractTerm
from app.models_readiness import ReadinessCheck, ReadinessInput
from app.policy import find_active_matching_policy, find_candidate_conflicting_terms
from app.term_provenance import resolve_vendor_term_precedence
from app.uow import UnitOfWork

READINESS_SUBJECT_TYPE_VENDOR = "vendor"


class UnsupportedCounterpartyTypeError(ValueError):
    pass


def _festival_days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _to_readiness_term(term: VendorContractTerm) -> ReadinessTerm:
    return ReadinessTerm(
        term_id=term.id,
        source_kind=term.source_kind,
        status=term.status,
        version=term.version,
        commission_bps=term.commission_bps,
        minimum_fee_cents=term.minimum_fee_cents,
        effective_from=term.effective_from,
        effective_to=term.effective_to,
        has_source_citation=bool(term.source_refs),
    )


def _gather_vendor_terms(db: Session, vendor_id: str) -> list[VendorContractTerm]:
    return (
        db.execute(
            select(VendorContractTerm)
            .where(VendorContractTerm.vendor_id == vendor_id)
            .order_by(VendorContractTerm.version.asc())
        )
        .scalars()
        .all()
    )


def _signed_agreement_source_versions(db: Session, vendor_id: str) -> list[SourceVersion]:
    """Real SourceVersion rows backing every code-VERIFIED claim whose
    speaker_role is "SIGNED_AGREEMENT" for this vendor -- the exact
    (claim_kind-agnostic) signal scripts/eval_a06_extraction.py already
    uses to mean "this came from the text of a signed agreement," reused
    here, not reinvented."""
    rows = (
        db.execute(
            select(SourceVersion)
            .join(DocumentExtraction, DocumentExtraction.source_version_id == SourceVersion.id)
            .join(DocumentClaim, DocumentClaim.extraction_id == DocumentExtraction.id)
            .where(
                DocumentClaim.vendor_id == vendor_id,
                DocumentClaim.speaker_role == "SIGNED_AGREEMENT",
                DocumentClaim.verified.is_(True),
            )
            .distinct()
        )
        .scalars()
        .all()
    )
    return rows


def _daily_resolutions(
    db: Session,
    *,
    vendor: Vendor,
    festival_start: date,
    festival_end: date,
    case_facts: CaseFacts | None,
) -> list[DailyResolution]:
    out = []
    for day in _festival_days(festival_start, festival_end):
        res = resolve_vendor_term_precedence(
            db, vendor=vendor, target_date=day, case_facts=case_facts
        )
        out.append(DailyResolution(day=day, term_id=res.term_id, source_kind=res.source_kind))
    return out


def _has_unresolved_conflict(
    db: Session, *, vendor_id: str, festival_start: date, festival_end: date
) -> bool:
    for day in _festival_days(festival_start, festival_end):
        if find_candidate_conflicting_terms(db, vendor_id, day) is not None:
            return True
    return False


def _payout_details_verified(vendor: Vendor) -> bool:
    details = vendor.beneficiary_details or {}
    return bool(details) and details.get("verified") is True


def _input_fingerprint(
    *,
    festival_id: str,
    counterparty_type: str,
    counterparty_id: str,
    festival_start: date,
    festival_end: date,
    term_inputs: list[tuple[str, int]],
    policy_inputs: list[tuple[str, int]],
    source_version_inputs: list[tuple[str, int]],
) -> str:
    payload = {
        "festival_id": festival_id,
        "counterparty_type": counterparty_type,
        "counterparty_id": counterparty_id,
        "festival_start": festival_start.isoformat(),
        "festival_end": festival_end.isoformat(),
        "vendor_contract_term": sorted(term_inputs),
        "policy_version": sorted(policy_inputs),
        "source_version": sorted(source_version_inputs),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _latest_readiness_check(
    db: Session, *, festival_id: str, counterparty_type: str, counterparty_id: str
) -> ReadinessCheck | None:
    return db.execute(
        select(ReadinessCheck)
        .where(
            ReadinessCheck.festival_id == festival_id,
            ReadinessCheck.counterparty_type == counterparty_type,
            ReadinessCheck.counterparty_id == counterparty_id,
        )
        .order_by(ReadinessCheck.version.desc())
        .limit(1)
    ).scalar_one_or_none()


def compute_readiness(
    db: Session,
    *,
    festival_id: str,
    counterparty_type: str,
    counterparty_id: str,
    vendor: Vendor,
    festival_start: date,
    festival_end: date,
    case_facts: CaseFacts | None = None,
    actor_id: str | None,
    actor_capability: str,
) -> tuple[ReadinessCheck, bool]:
    """Computes (or reuses) one readiness_check row for
    (festival_id, counterparty_type, counterparty_id). Returns
    (the row, created) -- `created=False` when the freshly-computed
    input_fingerprint matches the latest existing row's: that existing row
    is returned UNCHANGED, no new version, no new readiness_input rows, no
    new flight-recorder event (TICKET_SPEC.md R1 "Done when": "the same
    inputs give the same fingerprint and no new version")."""
    if counterparty_type not in COUNTERPARTY_TYPES:
        raise UnsupportedCounterpartyTypeError(
            f"unknown counterparty_type {counterparty_type!r}; expected one of {COUNTERPARTY_TYPES}"
        )
    if counterparty_type != READINESS_SUBJECT_TYPE_VENDOR:
        raise UnsupportedCounterpartyTypeError(
            f"R1's readiness checks only implement counterparty_type="
            f"{READINESS_SUBJECT_TYPE_VENDOR!r} -- no SupplierContractTerm (or equivalent) "
            f"data model exists yet for counterparty_type={counterparty_type!r}. "
            "A future supplier-readiness ticket adds its own data source and calls "
            "domain.readiness.evaluate_readiness (already generic over counterparty_type) "
            "directly; the readiness_checks/readiness_inputs TABLES do not need to change."
        )
    if vendor.id != counterparty_id:
        raise ValueError(
            f"vendor.id {vendor.id!r} does not match counterparty_id {counterparty_id!r}"
        )

    term_rows = _gather_vendor_terms(db, vendor.id)
    readiness_terms = tuple(_to_readiness_term(t) for t in term_rows)

    matched_policy: PolicyVersion | None = None
    if case_facts is not None:
        matched_policy = find_active_matching_policy(db, case_facts)

    daily = _daily_resolutions(
        db,
        vendor=vendor,
        festival_start=festival_start,
        festival_end=festival_end,
        case_facts=case_facts,
    )
    has_conflict = _has_unresolved_conflict(
        db, vendor_id=vendor.id, festival_start=festival_start, festival_end=festival_end
    )
    signed_source_versions = _signed_agreement_source_versions(db, vendor.id)
    payout_verified = _payout_details_verified(vendor)

    inputs = ReadinessInputs(
        counterparty_type=counterparty_type,
        counterparty_id=counterparty_id,
        festival_start=festival_start,
        festival_end=festival_end,
        terms=readiness_terms,
        daily_resolutions=tuple(daily),
        has_unresolved_conflict=has_conflict,
        has_verified_signed_agreement_claim=bool(signed_source_versions),
        payout_details_verified=payout_verified,
    )
    result = evaluate_readiness(inputs)

    term_inputs = [(t.id, t.version) for t in term_rows]
    policy_inputs = [(matched_policy.id, matched_policy.version)] if matched_policy else []
    source_version_inputs = [(sv.id, sv.version) for sv in signed_source_versions]

    fingerprint = _input_fingerprint(
        festival_id=festival_id,
        counterparty_type=counterparty_type,
        counterparty_id=counterparty_id,
        festival_start=festival_start,
        festival_end=festival_end,
        term_inputs=term_inputs,
        policy_inputs=policy_inputs,
        source_version_inputs=source_version_inputs,
    )

    latest = _latest_readiness_check(
        db,
        festival_id=festival_id,
        counterparty_type=counterparty_type,
        counterparty_id=counterparty_id,
    )
    if latest is not None and latest.input_fingerprint == fingerprint:
        return latest, False

    checks_payload = [
        {"check": c.check, "passed": c.passed, "reason": c.reason, "details": c.details}
        for c in result.checks
    ]
    row = ReadinessCheck(
        festival_id=festival_id,
        counterparty_type=counterparty_type,
        counterparty_id=counterparty_id,
        version=(latest.version + 1 if latest is not None else 1),
        status=result.status,
        checks=checks_payload,
        input_fingerprint=fingerprint,
        computed_at=datetime.now(UTC),
        computed_by=actor_id,
    )
    db.add(row)
    db.flush()

    for input_id, input_version in term_inputs:
        db.add(
            ReadinessInput(
                readiness_check_id=row.id,
                input_kind="vendor_contract_term",
                input_id=input_id,
                input_version=input_version,
            )
        )
    for input_id, input_version in policy_inputs:
        db.add(
            ReadinessInput(
                readiness_check_id=row.id,
                input_kind="policy_version",
                input_id=input_id,
                input_version=input_version,
            )
        )
    for input_id, input_version in source_version_inputs:
        db.add(
            ReadinessInput(
                readiness_check_id=row.id,
                input_kind="source_version",
                input_id=input_id,
                input_version=input_version,
            )
        )
    db.flush()

    uow = UnitOfWork(db)
    lineage = [
        {"ref_kind": "readiness_check", "ref_id": row.id},
        {"ref_kind": "vendor", "ref_id": vendor.id},
    ]
    if matched_policy is not None:
        lineage.append({"ref_kind": "policy_version", "ref_id": matched_policy.id})
    uow.append_event(
        stream_id=f"readiness_check:{festival_id}:{counterparty_type}:{counterparty_id}",
        event_type="readiness_check.computed",
        aggregate_id=row.id,
        aggregate_version=row.version,
        actor_id=actor_id,
        actor_type="human" if actor_id else "system",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        vendor_id=vendor.id if counterparty_type == "vendor" else None,
        payload={
            "festival_id": festival_id,
            "counterparty_type": counterparty_type,
            "counterparty_id": counterparty_id,
            "version": row.version,
            "status": row.status,
            "failing_checks": list(result.failing_checks),
            "input_fingerprint": fingerprint,
            "previous_version": latest.version if latest is not None else None,
        },
        lineage_refs=lineage,
    )
    return row, True


def readiness_view(row: ReadinessCheck) -> dict:
    failing = [c["check"] for c in row.checks if not c["passed"]]
    return {
        "id": row.id,
        "festival_id": row.festival_id,
        "counterparty_type": row.counterparty_type,
        "counterparty_id": row.counterparty_id,
        "version": row.version,
        "status": row.status,
        "failing_checks": failing,
        "checks": row.checks,
        "input_fingerprint": row.input_fingerprint,
        "computed_at": row.computed_at.isoformat(),
        # R2's own job (exposure_cents lives on the Case it opens for a
        # failing counterparty, not on this row) -- always null from R1.
        "exposure_cents": None,
        # R2's own job (decision_deadline lives on the Case it opens) --
        # always null from R1.
        "decision_deadline": None,
    }


def latest_readiness_for_festival(db: Session, *, festival_id: str) -> list[ReadinessCheck]:
    """Every counterparty's LATEST readiness_check row for `festival_id`
    (one row per distinct (counterparty_type, counterparty_id), the
    highest `version` of each) -- what GET /readiness lists."""
    all_rows = (
        db.execute(
            select(ReadinessCheck)
            .where(ReadinessCheck.festival_id == festival_id)
            .order_by(ReadinessCheck.version.asc())
        )
        .scalars()
        .all()
    )
    latest_by_counterparty: dict[tuple[str, str], ReadinessCheck] = {}
    for row in all_rows:
        latest_by_counterparty[(row.counterparty_type, row.counterparty_id)] = row
    return list(latest_by_counterparty.values())


__all__ = [
    "UnsupportedCounterpartyTypeError",
    "compute_readiness",
    "latest_readiness_for_festival",
    "readiness_view",
]
