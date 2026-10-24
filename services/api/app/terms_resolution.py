"""A06 stage 2: deterministic terms resolution (DB-touching orchestration).

Turns VERIFIED `document_claims` into versioned `vendor_contract_terms`
rows and review cases. The decisions themselves are made by the pure
`domain.terms.resolve_vendor_terms`; this module only loads claims,
compares against what is already recorded, and writes new rows through the
same UnitOfWork (domain write + audit event in one transaction) as A02-A09.
Claude proposes claims in stage 1; nothing here consults a model, and
nothing here approves, resolves or pays.

`resolve_vendor_day_terms` is the seam the nightly close's TERMS_RESOLVED
milestone calls (see `app/orchestration/terms.py` for the documented
contract). What it writes, per (vendor, business_date), idempotently:

  - an `active` term version built from signed documents (contract, then
    signed amendments), ONLY when it differs from the latest active
    document-derived version. Old versions are never edited. A
    document-derived version is marked by a `terms_basis` entry in its
    source_refs, so a human-decided version (A05 `resolve_case`) or a
    policy-covered night (A05 `_cover_night_from_policy`) is never
    mistaken for one, compared against one, or overwritten by one -- a
    later signed amendment simply adds a newer version, which A04 picks up.
  - for an unsuperseded organizer statement of a different rate: two
    `proposed` rows covering THIS business date (the signed rate and the
    stated rate), which is exactly the shape A05's
    `detect_contract_term_conflicts` looks for. They cover one business
    date each, so a conflict a later amendment supersedes stops being
    re-detected, and a one-time human answer does not silently become
    permanent.
  - then A05's own detector runs, so the `vendor_contract_term_conflict`
    case (lane B's CONFLICTING_TERMS) opens -- or auto-resolves against an
    ACTIVE human-approved policy -- through the one existing code path,
    with the real financial impact computed from the day's eligible sales.
  - a NON-blocking `minimum_period_claim` case for a vendor's claim that
    the minimum is nightly (the contract minimum stands, nothing is held);
  - a blocking `beneficiary_change_unverified` case for a payout-account
    change request (no payment details are ever read or applied).

Never commits: the caller (the graph node, or the router below) commits
once, so terms, proposed rows, cases and events land together.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from domain.terms import (
    TERMS_RESOLUTION_VERSION,
    ClaimFact,
    TermsResolution,
    resolve_vendor_terms,
    terms_differ,
)
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import CAPABILITY_RESOLVE_CASE
from app.models_agents import DocumentClaim
from app.models_ledger import Case, SourceDocument, Vendor, VendorContractTerm
from app.policy import CONTRACT_CONFLICT_REASON_CODE, detect_contract_term_conflicts
from app.settlement import gather_reconciled_day_facts
from app.uow import UnitOfWork

# Business timezone: a document's `available_from` and a business_date are
# both Pacific-local concepts (AGENT_HANDOFF.md), so the "as of" instant a
# night is resolved at is derived in this zone, never from a wall clock.
TZ = ZoneInfo("America/Los_Angeles")

MINIMUM_PERIOD_CLAIM_REASON = "minimum_period_claim"
BENEFICIARY_CHANGE_REASON = "beneficiary_change_unverified"

# Which case kinds this module can be holding a vendor with. A05's
# contract-conflict code is reused, not re-declared, so the two detectors
# can never drift apart on the reason string.
#
# Blocking class is a product decision, not a per-case judgement: only
# these withhold the vendor's money. `minimum_period_claim` is deliberately
# absent -- the signed contract stands and a human sees the question
# without the night being held.
BLOCKING_REASON_CODES = (CONTRACT_CONFLICT_REASON_CODE, BENEFICIARY_CHANGE_REASON)
NON_BLOCKING_REASON_CODES = (MINIMUM_PERIOD_CLAIM_REASON,)

DOCUMENT_BASIS_MARKER = {
    "ref_kind": "terms_basis",
    "basis": "SIGNED_DOCUMENTS",
    "resolution_version": TERMS_RESOLUTION_VERSION,
}


def is_document_derived(term: VendorContractTerm) -> bool:
    """Whether this term version was written by THIS module from signed
    documents, as opposed to by a human decision or a policy. Marked in
    `source_refs` rather than inferred, so the two kinds never blur."""
    return any(
        isinstance(r, dict) and r.get("ref_kind") == "terms_basis" for r in term.source_refs or []
    )


def business_day_end(business_date_: date) -> datetime:
    """The instant a business date closes, in the business timezone: the
    deterministic `as_of` for resolving that night. A document received
    after it does not exist for that night (DA19), and re-resolving the
    same night days later reaches the same answer -- which is what makes
    the nightly close replayable."""
    return datetime.combine(business_date_ + timedelta(days=1), time(0, 0), tzinfo=TZ)


@dataclass
class VendorTermsOutcome:
    """The nightly close's TERMS_RESOLVED result for one vendor.

    `status`/`case_ids`/`detail` are the three fields
    `app.orchestration.terms` reads; the term values are carried alongside
    them for A04, which applies the minimum against the day's sales (this
    module resolves what the minimum IS, never whether it bites).
    """

    status: str  # RESOLVED | HELD
    case_ids: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    vendor_id: str = ""
    action: str = "no_signed_terms"  # written | unchanged | no_signed_terms
    term_id: str | None = None
    term_version: int | None = None
    commission_bps: int | None = None
    minimum_fee_cents: int | None = None
    minimum_period: str | None = None
    processor_fee_deductible: bool | None = None
    conflicts_proposed: int = 0
    cases_opened: list[dict] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


@dataclass
class TermsResolutionSummary:
    festival_id: str
    business_date: date
    as_of: datetime
    vendors: list[VendorTermsOutcome] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for v in self.vendors:
            out[v.action] = out.get(v.action, 0) + 1
            out[v.status.lower()] = out.get(v.status.lower(), 0) + 1
        out["conflicts_proposed"] = sum(v.conflicts_proposed for v in self.vendors)
        out["cases_opened"] = sum(len(v.cases_opened) for v in self.vendors)
        return out


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _claim_facts(db: Session, vendor_id: str) -> list[ClaimFact]:
    """This vendor's VERIFIED claims only. An unverified claim stays
    visible in `document_claims` and is never used to decide money."""
    rows = db.execute(
        select(DocumentClaim, SourceDocument.document_kind)
        .join(SourceDocument, SourceDocument.id == DocumentClaim.source_document_id)
        .where(DocumentClaim.verified.is_(True), DocumentClaim.vendor_id == vendor_id)
        .order_by(DocumentClaim.available_from, DocumentClaim.id)
    ).all()
    return [
        ClaimFact(
            claim_id=c.id,
            source_document_id=c.source_document_id,
            document_kind=kind or "UNKNOWN",
            claim_kind=c.claim_kind,
            speaker_role=c.speaker_role,
            restates_existing_terms=c.restates_existing_terms,
            # business-local, so `.date()` is the Pacific calendar date
            available_from=c.available_from.astimezone(TZ),
            source_ref=c.source_ref,
            effective_from=c.effective_from,
            commission_bps=c.commission_bps,
            minimum_fee_cents=c.minimum_fee_cents,
            minimum_period=c.minimum_period,
            processor_fee_deductible=c.processing_fee_deductible,
        )
        for c, kind in rows
    ]


def latest_document_term(db: Session, vendor_id: str) -> VendorContractTerm | None:
    """The newest ACTIVE document-derived version for this vendor, which is
    what a fresh resolution is compared against (rule: write a new active
    version only when the terms actually changed)."""
    for t in db.execute(
        select(VendorContractTerm)
        .where(
            VendorContractTerm.vendor_id == vendor_id,
            VendorContractTerm.status == "active",
        )
        .order_by(VendorContractTerm.version.desc())
    ).scalars():
        if is_document_derived(t):
            return t
    return None


def _next_version(db: Session, vendor_id: str) -> int:
    v = db.execute(
        select(VendorContractTerm.version)
        .where(VendorContractTerm.vendor_id == vendor_id)
        .order_by(VendorContractTerm.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    return (v or 0) + 1


def _lock_vendor_terms(db: Session, vendor_id: str) -> None:
    """Serialize this vendor's term writes for the rest of the transaction.

    `vendor_contract_terms` has a UNIQUE(vendor_id, version), and two
    concurrent vendor threads resolving the same vendor would both read the
    same max version. A row lock is not available here: the restricted
    `encore_app` role needs UPDATE privilege for `SELECT ... FOR UPDATE`,
    and there is no natural row to lock before the first version exists
    anyway. Same mechanism `app.accounting`/`app.cash_office` use.
    """
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"vendor_terms:{vendor_id}"}
    )


# ---------------------------------------------------------------------------
# Cases (idempotent by a dedupe key stored in predicate_values)
# ---------------------------------------------------------------------------


def _open_case_once(
    db: Session,
    uow: UnitOfWork,
    *,
    vendor: Vendor,
    business_date_: date,
    reason_code: str,
    dedupe_key: str,
    blocking: bool,
    evidence_refs: list[dict],
    proposed_resolution: str,
    financial_impact_cents: int | None,
    run_id: str | None,
    actor_id: str,
    actor_capability: str,
) -> Case | None:
    """Open a case unless one with the same (vendor, reason, dedupe key)
    already exists in ANY status: the same document must never re-ask a
    question a human already answered, on this night or a later one.
    Returns None when nothing was opened."""
    existing = db.execute(
        select(Case.id).where(
            Case.vendor_id == vendor.id,
            Case.reason_code == reason_code,
            Case.predicate_values["dedupe_key"].astext == dedupe_key,
        )
    ).first()
    if existing is not None:
        return None
    case = Case(
        run_id=run_id,
        vendor_id=vendor.id,
        business_date=business_date_,
        reason_code=reason_code,
        evidence_refs=evidence_refs,
        financial_impact_cents=financial_impact_cents,
        proposed_resolution=proposed_resolution,
        required_capability=CAPABILITY_RESOLVE_CASE,
        status="OPEN",
        blocking=blocking,
        predicate_values={"dedupe_key": dedupe_key},
    )
    db.add(case)
    db.flush()
    uow.append_event(
        stream_id=f"case:{case.id}",
        event_type="case.opened",
        aggregate_id=case.id,
        aggregate_version=1,
        actor_id=actor_id,
        actor_type="system",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        run_id=run_id,
        vendor_id=vendor.id,
        versions={"terms_resolution": TERMS_RESOLUTION_VERSION},
        payload={
            "reason_code": reason_code,
            "business_date": business_date_.isoformat(),
            "blocking": blocking,
            "dedupe_key": dedupe_key,
            "financial_impact_cents": financial_impact_cents,
            "evidence_refs": evidence_refs,
        },
        lineage_refs=[{"ref_kind": "case", "ref_id": case.id}],
    )
    return case


def blocking_case_ids(db: Session, vendor_id: str) -> list[str]:
    """OPEN, blocking terms cases for this vendor -- re-read from the
    database on every run, never remembered from a previous one. This is
    what makes HELD/RESOLVED idempotent AND makes a resume a real re-check:
    once a human resolves the case, the next run returns RESOLVED without
    the resolution having to be reported back to this module.

    Not scoped to the business date on purpose: a night-1 conflict nobody
    answered still withholds night 2's money, which is what A07's
    `release_payout` already does with any OPEN case.
    """
    return [
        r[0]
        for r in db.execute(
            select(Case.id)
            .where(
                Case.vendor_id == vendor_id,
                Case.reason_code.in_(BLOCKING_REASON_CODES),
                Case.status == "OPEN",
                Case.blocking.is_(True),
            )
            .order_by(Case.created_at, Case.id)
        ).all()
    ]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def _write_document_terms(
    db: Session,
    uow: UnitOfWork,
    *,
    vendor: Vendor,
    res: TermsResolution,
    as_of: datetime,
    run_id: str | None,
    actor_id: str,
    actor_capability: str,
) -> tuple[VendorContractTerm, bool]:
    """(the document-derived active term now in force, newly written?)"""
    computed = {
        "commission_bps": res.commission_bps,
        "minimum_fee_cents": res.minimum_fee_cents,
        "processor_fee_deductible": bool(res.processor_fee_deductible),
        "effective_from": res.effective_from,
    }
    latest = latest_document_term(db, vendor.id)
    if latest is not None and not terms_differ(
        computed,
        {
            "commission_bps": latest.commission_bps,
            "minimum_fee_cents": latest.minimum_fee_cents,
            "processor_fee_deductible": latest.processor_fee_deductible,
            "effective_from": latest.effective_from,
        },
    ):
        return latest, False

    term = VendorContractTerm(
        vendor_id=vendor.id,
        version=_next_version(db, vendor.id),
        commission_bps=res.commission_bps,
        minimum_fee_cents=res.minimum_fee_cents,
        processor_fee_deductible=bool(res.processor_fee_deductible),
        effective_from=res.effective_from,
        effective_to=None,
        status="active",
        source_refs=[DOCUMENT_BASIS_MARKER, *res.source_refs()],
        # Nobody approved this: it is what the signed documents say,
        # code-verified. A human approval lives on the A05 path instead.
        approved_by=None,
    )
    db.add(term)
    db.flush()
    uow.append_event(
        stream_id=f"vendor_terms:{vendor.id}",
        event_type="terms.resolved",
        aggregate_id=term.id,
        aggregate_version=term.version,
        actor_id=actor_id,
        actor_type="system",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        run_id=run_id,
        vendor_id=vendor.id,
        versions={"terms_resolution": TERMS_RESOLUTION_VERSION},
        payload={
            "as_of": as_of.isoformat(),
            "term_version": term.version,
            "commission_bps": term.commission_bps,
            "minimum_fee_cents": term.minimum_fee_cents,
            "minimum_period": res.minimum_period,
            "processor_fee_deductible": term.processor_fee_deductible,
            "effective_from": term.effective_from.isoformat() if term.effective_from else None,
            "amended_fields": sorted(res.amended_fields),
            "superseded_version": latest.version if latest is not None else None,
            "corroborating_refs": res.corroborating_refs,
        },
        lineage_refs=[{"ref_kind": "vendor_contract_term", "ref_id": term.id}],
    )
    return term, True


def _propose_rate_conflict(
    db: Session,
    uow: UnitOfWork,
    *,
    vendor: Vendor,
    business_date_: date,
    res: TermsResolution,
    run_id: str | None,
    actor_id: str,
    actor_capability: str,
) -> list[str]:
    """Two `proposed` rows covering this business date per live conflict,
    unless rows for those rates already exist (idempotent re-runs of the
    same night). Returns the ids actually written."""
    have = {
        t.commission_bps
        for t in db.execute(
            select(VendorContractTerm).where(
                VendorContractTerm.vendor_id == vendor.id,
                VendorContractTerm.status == "proposed",
                VendorContractTerm.effective_from == business_date_,
                VendorContractTerm.effective_to == business_date_,
            )
        ).scalars()
    }
    written: list[str] = []
    for conflict in res.rate_conflicts:
        for bps, refs, role in (
            (conflict.signed_bps, list(conflict.signed_refs), "signed_agreement"),
            (conflict.stated_bps, [conflict.stated_claim.source_ref], "organizer_correspondence"),
        ):
            if bps in have:
                continue
            term = VendorContractTerm(
                vendor_id=vendor.id,
                version=_next_version(db, vendor.id),
                commission_bps=bps,
                minimum_fee_cents=res.minimum_fee_cents,
                processor_fee_deductible=bool(res.processor_fee_deductible),
                effective_from=business_date_,
                effective_to=business_date_,
                status="proposed",
                source_refs=refs,
                approved_by=None,
            )
            db.add(term)
            db.flush()
            have.add(bps)
            written.append(term.id)
            uow.append_event(
                stream_id=f"vendor_terms:{vendor.id}",
                event_type="terms.conflict_candidate_proposed",
                aggregate_id=term.id,
                aggregate_version=term.version,
                actor_id=actor_id,
                actor_type="system",
                actor_capability=actor_capability,
                occurred_at=datetime.now(UTC),
                run_id=run_id,
                vendor_id=vendor.id,
                versions={"terms_resolution": TERMS_RESOLUTION_VERSION},
                payload={
                    "business_date": business_date_.isoformat(),
                    "commission_bps": bps,
                    "role": role,
                    "source_refs": refs,
                },
                lineage_refs=[{"ref_kind": "vendor_contract_term", "ref_id": term.id}],
            )
    return written


# ---------------------------------------------------------------------------
# One vendor, one night -- the seam app/orchestration/terms.py calls
# ---------------------------------------------------------------------------


def resolve_vendor_day_terms(
    db: Session,
    *,
    vendor: Vendor,
    business_date: date,
    eligible_sales_cents: int,
    run_id: str | None,
    actor_id: str,
    actor_capability: str,
    as_of: datetime | None = None,
) -> VendorTermsOutcome:
    """Resolve one vendor's terms for one night. Never commits.

    Idempotent per (vendor, business_date): `as_of` is derived from the
    business date rather than the wall clock, a new active version is
    written only when the values actually change, proposed conflict rows
    and cases are deduped, and HELD/RESOLVED is re-derived from the
    database each time -- so re-running returns the same cases instead of
    opening new ones.

    `eligible_sales_cents` is the night's reconciled eligible sales. It is
    NOT an input to resolving the terms: resolution produces the rate and
    what the minimum IS, while applying a minimum against a day's sales is
    A04's job (`minimum_fee_cents` is handed back for it). It is used for
    the one thing it is actually evidence for -- the real dollar size of a
    rate disagreement on A05's case -- and recorded in `detail` either way.
    """
    as_of = as_of or business_day_end(business_date)
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    uow = UnitOfWork(db)
    _lock_vendor_terms(db, vendor.id)

    claims = _claim_facts(db, vendor.id)
    res = resolve_vendor_terms(claims, as_of)
    out = VendorTermsOutcome(status="RESOLVED", vendor_id=vendor.id)
    out.unresolved = list(res.unresolved)
    out.minimum_period = res.minimum_period
    proposed_term_ids: list[str] = []

    if res.has_signed_terms and res.effective_from is not None:
        term, written = _write_document_terms(
            db,
            uow,
            vendor=vendor,
            res=res,
            as_of=as_of,
            run_id=run_id,
            actor_id=actor_id,
            actor_capability=actor_capability,
        )
        out.action = "written" if written else "unchanged"
        out.term_id = term.id
        out.term_version = term.version
        out.commission_bps = res.commission_bps
        out.minimum_fee_cents = res.minimum_fee_cents
        out.processor_fee_deductible = bool(res.processor_fee_deductible)

        if res.rate_conflicts:
            proposed_term_ids = _propose_rate_conflict(
                db,
                uow,
                vendor=vendor,
                business_date_=business_date,
                res=res,
                run_id=run_id,
                actor_id=actor_id,
                actor_capability=actor_capability,
            )
            out.conflicts_proposed = len(proposed_term_ids)

    # The vendor's own minimum-period claim: a human sees it, the signed
    # contract stands, nothing is held.
    for c in res.minimum_period_claims:
        case = _open_case_once(
            db,
            uow,
            vendor=vendor,
            business_date_=business_date,
            reason_code=MINIMUM_PERIOD_CLAIM_REASON,
            dedupe_key=f"claim_document:{c.source_document_id}",
            blocking=False,
            evidence_refs=[c.source_ref, *res.field_refs.get("minimum_fee_cents", [])],
            proposed_resolution=(
                "The vendor states the minimum fee is charged per night; the signed agreement "
                "applies it once for the whole festival. The signed agreement stands: the "
                "minimum is assessed once at final settlement. Nothing is held."
            ),
            financial_impact_cents=res.minimum_fee_cents,
            run_id=run_id,
            actor_id=actor_id,
            actor_capability=actor_capability,
        )
        if case is not None:
            out.cases_opened.append(
                {"case_id": case.id, "reason_code": case.reason_code, "blocking": False}
            )

    # A payout-account change by correspondence: blocking, and the new
    # details are never read, let alone applied.
    for c in res.beneficiary_change_claims:
        case = _open_case_once(
            db,
            uow,
            vendor=vendor,
            business_date_=business_date,
            reason_code=BENEFICIARY_CHANGE_REASON,
            dedupe_key=f"claim_document:{c.source_document_id}",
            blocking=True,
            evidence_refs=[c.source_ref],
            proposed_resolution=(
                "A payout-account change was requested by correspondence. It is unverified: no "
                "payment details are applied, and this vendor's payouts are held until a human "
                "verifies the request out of band with a known contact."
            ),
            financial_impact_cents=None,
            run_id=run_id,
            actor_id=actor_id,
            actor_capability=actor_capability,
        )
        if case is not None:
            out.cases_opened.append(
                {"case_id": case.id, "reason_code": case.reason_code, "blocking": True}
            )

    # A05's existing detector turns the two proposed rows above (or rows
    # seeded by any other route) into the one contract-conflict case,
    # auto-resolving it only through an ACTIVE human-approved policy. One
    # code path, one reason code, one financial-impact calculation.
    conflict = detect_contract_term_conflicts(
        db,
        uow,
        vendor=vendor,
        business_date_=business_date,
        eligible_sales_cents_today=eligible_sales_cents,
        run_id=run_id,
        actor_id=actor_id,
        actor_capability=actor_capability,
    )

    out.case_ids = blocking_case_ids(db, vendor.id)
    out.status = "HELD" if out.case_ids else "RESOLVED"
    out.detail = {
        "resolution_version": TERMS_RESOLUTION_VERSION,
        "business_date": business_date.isoformat(),
        "as_of": as_of.isoformat(),
        # Recorded for the audit trail; see the docstring on why it is not
        # an input to resolving the rate.
        "eligible_sales_cents": eligible_sales_cents,
        "claims_considered": len(claims),
        "action": out.action,
        "term_id": out.term_id,
        "term_version": out.term_version,
        "commission_bps": out.commission_bps,
        "minimum_fee_cents": out.minimum_fee_cents,
        "minimum_period": out.minimum_period,
        "processor_fee_deductible": out.processor_fee_deductible,
        "corroborating_refs": len(res.corroborating_refs),
        "amended_fields": sorted(res.amended_fields),
        "proposed_term_ids": proposed_term_ids,
        "cases_opened": out.cases_opened,
        "blocking_case_ids": out.case_ids,
        "non_blocking_reason_codes": list(NON_BLOCKING_REASON_CODES),
        "unresolved": out.unresolved,
        "conflict_case": (
            None
            if conflict is None
            else {
                "case_id": conflict.id,
                "status": conflict.status,
                "auto_resolved": bool(conflict.auto_resolved),
                "applied_policy_version_id": conflict.applied_policy_version_id,
                "financial_impact_cents": conflict.financial_impact_cents,
            }
        ),
    }
    return out


# ---------------------------------------------------------------------------
# A whole festival (the endpoint; the nightly close drives one vendor at a
# time through its own graph thread)
# ---------------------------------------------------------------------------


def resolve_festival_terms(
    db: Session,
    *,
    festival_id: str,
    business_date_: date,
    as_of: datetime | None = None,
    run_id: str | None = None,
    actor_id: str,
    actor_capability: str,
) -> TermsResolutionSummary:
    """Resolve every vendor in the festival, one vendor at a time. Mirrors
    A03's `reconcile_festival`/A04's `calculate_festival_settlements`
    fault-isolation shape: one vendor's terms never depend on another's.
    Does not commit -- the caller commits once."""
    as_of = as_of or business_day_end(business_date_)
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    vendors = (
        db.execute(select(Vendor).where(Vendor.festival_id == festival_id).order_by(Vendor.id))
        .scalars()
        .all()
    )
    summary = TermsResolutionSummary(
        festival_id=festival_id, business_date=business_date_, as_of=as_of
    )
    for vendor in vendors:
        facts = gather_reconciled_day_facts(db, vendor, business_date_)
        summary.vendors.append(
            resolve_vendor_day_terms(
                db,
                vendor=vendor,
                business_date=business_date_,
                eligible_sales_cents=(
                    facts.centrally_held_cents + facts.vendor_retained_cash_cents
                ),
                run_id=run_id,
                actor_id=actor_id,
                actor_capability=actor_capability,
                as_of=as_of,
            )
        )
    return summary
