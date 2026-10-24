"""A05 exceptions, approvals and scoped policies: DB-touching orchestration.

The deterministic "does this policy apply" check itself lives in
`domain.policy` (packages/domain/domain/policy.py) -- a pure, DB-free
module, same convention as domain.settlement (A04). Everything in THIS
module is the glue: detect a vendor contract-term conflict from real
`VendorContractTerm` rows, compute its real financial impact using A04's
own settlement math, decide (via the pure `policy_applies` check) whether
an existing ACTIVE policy already covers it, and -- if not -- open a real
`Case` for a human; if so, auto-resolve that case citing the policy version
and the case that originally established it. Also: the explicit human
resolve/approve-reuse/challenge/suspend/revise commands, each one reusing
A02's `UnitOfWork` for the same "domain write + audit event" atomicity A02-
A04 already established, and each one recording the REAL authenticated
actor/capability (never a fabricated approver).

Conflict-detection scope note: this module's own `detect_contract_term_
conflicts` is a stand-in for what will eventually be a nightly/continuous
detection step (A06's scheduler territory, or B02's ingestion pipeline) --
it is triggered here by an explicit, capability-gated API call
(POST /api/v1/cases/detect-contract-conflicts), not by a background worker
that doesn't exist yet. The DETECTION LOGIC itself (comparing two
"proposed" VendorContractTerm rows, computing the real dollar impact, and
checking for a matching ACTIVE policy) is what this task is actually
responsible for; how/when it gets invoked in production is explicitly out
of scope here, same spirit as A04's `is_final_period` being caller-supplied
rather than festival-calendar-derived.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from domain.policy import CaseFacts, PolicyCandidate, policy_applies
from domain.settlement import (
    DEFAULT_MINIMUM_FEE_CENTS,
    EligibleSalesInput,
    calculate_vendor_settlement,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CAPABILITY_RESOLVE_CASE, _new_id
from app.models_ledger import Case, PolicyVersion, Vendor, VendorContractTerm
from app.uow import UnitOfWork

# Reason code this module's detector opens/auto-resolves. A stable,
# dedicated constant (not a bare string literal scattered across the
# module) so a future second conflict-detector can add its own distinct
# reason_code without this one's matching logic accidentally picking it up.
CONTRACT_CONFLICT_REASON_CODE = "vendor_contract_term_conflict"


# ---------------------------------------------------------------------------
# Conflict detection (the Night 1 / Night 2 taco-vendor scenario)
# ---------------------------------------------------------------------------


def _conflict_fingerprint(bps_a: int, bps_b: int) -> str:
    lo, hi = sorted((bps_a, bps_b))
    return f"commission_bps_conflict:{lo}:{hi}"


def find_candidate_conflicting_terms(
    db: Session, vendor_id: str, business_date_: date
) -> tuple[VendorContractTerm, VendorContractTerm] | None:
    """Two `status="proposed"` VendorContractTerm rows for this vendor,
    both covering `business_date_`, whose `commission_bps` disagree --
    e.g. one sourced from a signed contract PDF, one from a later producer
    email (see scripts/dev_fixtures_a05.py for how these are seeded).
    Returns (higher_bps_term, lower_bps_term), or None if there is no such
    disagreement right now (e.g. only one candidate exists, or the
    conflict has already been resolved into a single `status="active"`
    term and the proposed rows are just historical leftovers)."""
    terms = (
        db.execute(
            select(VendorContractTerm)
            .where(
                VendorContractTerm.vendor_id == vendor_id,
                VendorContractTerm.status == "proposed",
                VendorContractTerm.effective_from <= business_date_,
            )
            .where(
                (VendorContractTerm.effective_to.is_(None))
                | (VendorContractTerm.effective_to >= business_date_)
            )
        )
        .scalars()
        .all()
    )
    distinct_bps = {t.commission_bps for t in terms if t.commission_bps is not None}
    if len(distinct_bps) < 2:
        return None
    hi_bps, lo_bps = max(distinct_bps), min(distinct_bps)
    hi_term = next(t for t in terms if t.commission_bps == hi_bps)
    lo_term = next(t for t in terms if t.commission_bps == lo_bps)
    return hi_term, lo_term


def find_active_matching_policy(db: Session, facts: CaseFacts) -> PolicyVersion | None:
    """The ONLY path that can let a case auto-resolve: every `status=
    "ACTIVE"` PolicyVersion row is checked against `facts` with the pure
    `domain.policy.policy_applies` function. No similarity scoring, no
    "closest match" fallback -- an exact, deterministic match or nothing.
    Returns the first match (in practice exactly one policy is expected to
    match a given vendor/reason_code/predicate fingerprint, since policies
    are scoped specifically at approval time)."""
    candidates = (
        db.execute(select(PolicyVersion).where(PolicyVersion.status == "ACTIVE")).scalars().all()
    )
    for p in candidates:
        candidate = PolicyCandidate(
            policy_id=p.policy_id,
            version=p.version,
            scope=p.scope,
            predicates=p.predicates,
            effect=p.effect,
            status=p.status,
            valid_from=p.valid_from,
            valid_to=p.valid_to,
        )
        applies, _reason = policy_applies(candidate, facts)
        if applies:
            return p
    return None


def _compute_conflict_financial_impact_cents(
    *, eligible_sales_cents: int, hi_bps: int, lo_bps: int
) -> int:
    """Real A04 settlement math, not hand-reimplemented arithmetic: the
    actual fee the organizer would assess at each candidate rate against
    the SAME eligible sales, for a single (non-cumulative, non-final)
    period -- the dollar amount genuinely in dispute between "contract
    says 15%" and "producer email says 10%". Both calls use
    `is_final_period=False`/`cumulative_commission_assessed_prior_cents=0`
    deliberately: a case's financial_impact is informative evidence about
    the SIZE of the disagreement, not itself a settlement calculation (A04
    owns settlements; this never writes a Settlement row)."""
    hi_result = calculate_vendor_settlement(
        eligible=EligibleSalesInput(
            centrally_held_cents=eligible_sales_cents,
            vendor_retained_cash_cents=0,
        ),
        cumulative_eligible_sales_cents=eligible_sales_cents,
        cumulative_commission_assessed_prior_cents=0,
        commission_bps=hi_bps,
        minimum_fee_cents=DEFAULT_MINIMUM_FEE_CENTS,
        is_final_period=False,
    )
    lo_result = calculate_vendor_settlement(
        eligible=EligibleSalesInput(
            centrally_held_cents=eligible_sales_cents,
            vendor_retained_cash_cents=0,
        ),
        cumulative_eligible_sales_cents=eligible_sales_cents,
        cumulative_commission_assessed_prior_cents=0,
        commission_bps=lo_bps,
        minimum_fee_cents=DEFAULT_MINIMUM_FEE_CENTS,
        is_final_period=False,
    )
    return hi_result.fee_assessed_cents - lo_result.fee_assessed_cents


def detect_contract_term_conflicts(
    db: Session,
    uow: UnitOfWork,
    *,
    vendor: Vendor,
    business_date_: date,
    eligible_sales_cents_today: int,
    run_id: str | None,
    actor_id: str,
    actor_capability: str,
) -> Case | None:
    """Idempotent per (vendor_id, business_date): re-running detection for
    a night that already has a case for this vendor/reason_code returns
    that same case, never a duplicate row. Returns None if this vendor has
    no conflicting proposed contract terms for this business_date at all
    (the ordinary case -- most vendors, most nights)."""
    existing = db.execute(
        select(Case).where(
            Case.vendor_id == vendor.id,
            Case.business_date == business_date_,
            Case.reason_code == CONTRACT_CONFLICT_REASON_CODE,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    found = find_candidate_conflicting_terms(db, vendor.id, business_date_)
    if found is None:
        return None
    hi_term, lo_term = found

    facts = CaseFacts(
        vendor_id=vendor.id,
        festival_id=vendor.festival_id,
        reason_code=CONTRACT_CONFLICT_REASON_CODE,
        business_date=business_date_,
        predicate_values={
            "conflict_fingerprint": _conflict_fingerprint(
                hi_term.commission_bps, lo_term.commission_bps
            ),
            "contract_bps": hi_term.commission_bps,
            "proposed_bps": lo_term.commission_bps,
        },
    )
    financial_impact_cents = _compute_conflict_financial_impact_cents(
        eligible_sales_cents=eligible_sales_cents_today,
        hi_bps=hi_term.commission_bps,
        lo_bps=lo_term.commission_bps,
    )
    evidence_refs = [
        {
            "ref_kind": "vendor_contract_term",
            "ref_id": hi_term.id,
            "role": "contract_rate",
            "commission_bps": hi_term.commission_bps,
            "source_refs": hi_term.source_refs,
        },
        {
            "ref_kind": "vendor_contract_term",
            "ref_id": lo_term.id,
            "role": "producer_email_rate",
            "commission_bps": lo_term.commission_bps,
            "source_refs": lo_term.source_refs,
        },
    ]
    proposed_resolution = (
        f"Signed contract states {hi_term.commission_bps / 100:.2f}% commission; a later "
        f"producer email states {lo_term.commission_bps / 100:.2f}%. These conflict for the "
        f"same vendor/event -- a human must choose one rate for this vendor (one-time), and "
        f"may separately approve it as a reusable policy for future nights."
    )

    active_policy = find_active_matching_policy(db, facts)

    if active_policy is not None:
        # AUTO-RESOLVE. No approval call made here -- the policy IS the
        # approval, already granted once by a human for exactly this
        # scope+predicates+validity window.
        case = Case(
            run_id=run_id,
            vendor_id=vendor.id,
            business_date=business_date_,
            reason_code=CONTRACT_CONFLICT_REASON_CODE,
            evidence_refs=evidence_refs,
            financial_impact_cents=financial_impact_cents,
            proposed_resolution=proposed_resolution,
            required_capability=CAPABILITY_RESOLVE_CASE,
            status="RESOLVED",
            predicate_values=facts.predicate_values,
            auto_resolved=True,
            applied_policy_version_id=active_policy.id,
            origin_case_id=active_policy.source_case_id,
            resolution_decision="auto_applied_by_policy",
            resolution_note=(
                f"auto-applied policy {active_policy.policy_id} v{active_policy.version}: "
                f"effect={active_policy.effect}"
            ),
            resolution_effect=active_policy.effect,
            resolved_by=None,
            resolved_capability=None,
            resolved_at=datetime.now(UTC),
        )
        db.add(case)
        db.flush()
        lineage = [
            {"ref_kind": "case", "ref_id": case.id},
            {"ref_kind": "policy_version", "ref_id": active_policy.id},
        ]
        if active_policy.source_case_id:
            lineage.append({"ref_kind": "case", "ref_id": active_policy.source_case_id})
        uow.append_event(
            stream_id=f"case:{case.id}",
            event_type="case.auto_resolved",
            aggregate_id=case.id,
            aggregate_version=1,
            actor_id=actor_id,
            actor_type="system",
            actor_capability=actor_capability,
            occurred_at=datetime.now(UTC),
            run_id=run_id,
            vendor_id=vendor.id,
            payload={
                "reason_code": CONTRACT_CONFLICT_REASON_CODE,
                "business_date": business_date_.isoformat(),
                "financial_impact_cents": financial_impact_cents,
                "applied_policy_version_id": active_policy.id,
                "applied_policy_id": active_policy.policy_id,
                "applied_policy_version": active_policy.version,
                "origin_case_id": active_policy.source_case_id,
                "effect": active_policy.effect,
            },
            lineage_refs=lineage,
        )
        return case

    # ESCALATE: no matching ACTIVE policy. Open a real case for a human.
    case = Case(
        run_id=run_id,
        vendor_id=vendor.id,
        business_date=business_date_,
        reason_code=CONTRACT_CONFLICT_REASON_CODE,
        evidence_refs=evidence_refs,
        financial_impact_cents=financial_impact_cents,
        proposed_resolution=proposed_resolution,
        required_capability=CAPABILITY_RESOLVE_CASE,
        status="OPEN",
        predicate_values=facts.predicate_values,
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
        payload={
            "reason_code": CONTRACT_CONFLICT_REASON_CODE,
            "business_date": business_date_.isoformat(),
            "financial_impact_cents": financial_impact_cents,
            "evidence_refs": evidence_refs,
        },
    )
    return case


def detect_contract_term_conflicts_for_festival(
    db: Session,
    *,
    festival_id: str,
    business_date_: date,
    eligible_sales_cents_by_vendor: dict[str, int],
    run_id: str | None,
    actor_id: str,
    actor_capability: str,
) -> list[Case]:
    """Runs detection for every vendor in `festival_id` that has an entry
    in `eligible_sales_cents_by_vendor`, one vendor at a time -- mirrors
    A03's `reconcile_festival`/A04's `calculate_festival_settlements` fault-
    isolation pattern: a conflict (or lack of one) for ANY single vendor
    never affects any other vendor's detection in the same call. Returns
    only the cases actually opened/auto-resolved this call (vendors with no
    conflict contribute nothing to the returned list -- which is itself the
    concrete evidence of "only the specific affected vendor is touched").
    """
    uow = UnitOfWork(db)
    vendors = db.execute(select(Vendor).where(Vendor.festival_id == festival_id)).scalars().all()
    results: list[Case] = []
    for vendor in vendors:
        eligible = eligible_sales_cents_by_vendor.get(vendor.id)
        if eligible is None:
            continue
        case = detect_contract_term_conflicts(
            db,
            uow,
            vendor=vendor,
            business_date_=business_date_,
            eligible_sales_cents_today=eligible,
            run_id=run_id,
            actor_id=actor_id,
            actor_capability=actor_capability,
        )
        if case is not None:
            results.append(case)
    return results


# ---------------------------------------------------------------------------
# Explicit human decisions: resolve (approve/edit/reject), approve-reuse,
# challenge (hook), suspend, revise (supersession)
# ---------------------------------------------------------------------------


def _next_term_version(db: Session, vendor_id: str) -> int:
    max_version = db.execute(
        select(VendorContractTerm.version)
        .where(VendorContractTerm.vendor_id == vendor_id)
        .order_by(VendorContractTerm.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    return (max_version or 0) + 1


def _contract_processor_fee_deductible(db: Session, case: Case, commission_bps: int) -> bool:
    """The processor-fee clause of the contract candidate a decision
    actually chose.

    These cases are a disagreement about `commission_bps` ALONE -- nobody
    disputed whether the vendor's contract lets the organizer recharge the
    processor fee. Hard-coding that flag on the term a resolution writes
    would quietly rewrite a clause no human decided, in either direction
    (a vendor stops being charged a fee its contract permits, or starts
    being charged one it does not).

    Preference order: the candidate term whose rate is the one chosen,
    then the signed-contract candidate, then any candidate the case cites.
    False only when no candidate term is readable at all -- the same
    conservative fallback app/corrections.py uses when it cannot find the
    term it is superseding."""
    refs = [
        r
        for r in (case.evidence_refs or [])
        if isinstance(r, dict) and r.get("ref_kind") == "vendor_contract_term" and r.get("ref_id")
    ]
    terms = [t for t in (db.get(VendorContractTerm, r["ref_id"]) for r in refs) if t is not None]
    if not terms:
        return False
    chosen = next((t for t in terms if t.commission_bps == commission_bps), None)
    if chosen is None:
        role_by_id = {r["ref_id"]: r.get("role") for r in refs}
        chosen = next((t for t in terms if role_by_id.get(t.id) == "contract_rate"), terms[0])
    return chosen.processor_fee_deductible


def _activate_resolved_term(
    db: Session,
    *,
    case: Case,
    commission_bps: int,
    minimum_fee_cents: int | None,
    source_refs: list,
    approved_by: str | None,
) -> VendorContractTerm:
    """Activate a chosen commission rate for the case's OWN business_date
    and not one night longer.

    `effective_from == effective_to == case.business_date`. An open-ended
    term here would let every remaining night of the festival bill at a
    rate no human decided for it and no policy covers -- silently, because
    A04's `resolve_vendor_terms` just takes the highest-version active
    term covering the night. Each later night earns its own single-day
    term the same way: its own case, resolved by a human or auto-resolved
    against an ACTIVE policy."""
    term = VendorContractTerm(
        vendor_id=case.vendor_id,
        version=_next_term_version(db, case.vendor_id),
        commission_bps=commission_bps,
        minimum_fee_cents=minimum_fee_cents,
        processor_fee_deductible=_contract_processor_fee_deductible(db, case, commission_bps),
        effective_from=case.business_date,
        effective_to=case.business_date,
        status="active",
        source_refs=source_refs,
        approved_by=approved_by,
    )
    db.add(term)
    db.flush()
    return term


def resolve_case(
    db: Session,
    uow: UnitOfWork,
    *,
    case: Case,
    decision: str,
    resolution_note: str | None,
    commission_bps: int | None,
    minimum_fee_cents: int | None,
    effective_to: date | None,
    actor_id: str,
    actor_capability: str,
) -> Case:
    """One-time resolution of `case` by a real human decision -- approve,
    edit, or reject. This ALONE never creates a reusable policy (AGENT_
    HANDOFF.md: "a one-time resolution is never auto-promoted into a
    reusable rule"); that requires a separate, explicit call to
    `approve_case_policy` below.

    For a `vendor_contract_term_conflict` case, "approve"/"edit" activates
    exactly one new VendorContractTerm for this vendor (status="active"),
    covering the case's own business_date and NOTHING beyond it -- a
    one-time decision is policy for exactly one night. Later nights pick
    up the rate only through the separate, explicit `approve_case_policy`
    call below: their own detection then auto-resolves against that ACTIVE
    policy and activates their own single-day term. `effective_to`, if
    passed at all, must equal that business_date; a wider window is
    refused rather than silently written, so no caller walks away
    believing a one-time resolution bought a festival-long rate.
    "approve" is expected to pass the LOWER (producer-email) rate the case
    proposed; "edit" lets the human choose any rate at all, including
    neither conflicting candidate -- both are the same code path here,
    since both are an explicit human decision, not a default.
    """
    if case.status != "OPEN":
        raise ValueError(f"case {case.id} is not OPEN (status={case.status}); cannot resolve it")
    if decision not in ("approve", "edit", "reject"):
        raise ValueError(f"invalid decision: {decision!r} (expected approve/edit/reject)")

    new_term_id = None
    resolution_effect: dict | None = None
    if decision in ("approve", "edit"):
        if case.reason_code == CONTRACT_CONFLICT_REASON_CODE:
            if commission_bps is None:
                raise ValueError(
                    "resolving a vendor_contract_term_conflict case requires commission_bps"
                )
            if case.business_date is None:
                raise ValueError(
                    f"case {case.id} has no business_date; a contract-term resolution has to be "
                    "scoped to the night it decides"
                )
            if effective_to is not None and effective_to != case.business_date:
                raise ValueError(
                    f"a one-time resolution of case {case.id} covers only its own business_date "
                    f"({case.business_date.isoformat()}); approve it as a reusable policy "
                    f"(POST /cases/{{id}}/approve-policy) to cover later nights instead of "
                    f"widening effective_to to {effective_to.isoformat()}"
                )
            new_term = _activate_resolved_term(
                db,
                case=case,
                commission_bps=commission_bps,
                minimum_fee_cents=minimum_fee_cents,
                source_refs=case.evidence_refs,
                approved_by=actor_id,
            )
            new_term_id = new_term.id
            resolution_effect = {
                "commission_bps": commission_bps,
                "minimum_fee_cents": minimum_fee_cents,
            }

    case.status = "RESOLVED" if decision in ("approve", "edit") else "REJECTED"
    case.resolution_decision = decision
    case.resolution_note = resolution_note
    case.resolution_effect = resolution_effect
    case.resolved_by = actor_id
    case.resolved_capability = actor_capability
    case.resolved_at = datetime.now(UTC)
    db.add(case)
    db.flush()

    lineage = [{"ref_kind": "case", "ref_id": case.id}]
    if new_term_id is not None:
        lineage.append({"ref_kind": "vendor_contract_term", "ref_id": new_term_id})
    uow.append_event(
        stream_id=f"case:{case.id}",
        event_type="case.resolved",
        aggregate_id=case.id,
        aggregate_version=2,
        actor_id=actor_id,
        actor_type="human",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        run_id=case.run_id,
        vendor_id=case.vendor_id,
        payload={
            "decision": decision,
            "resolution_note": resolution_note,
            "resolution_effect": resolution_effect,
            "new_contract_term_id": new_term_id,
        },
        lineage_refs=lineage,
    )
    return case


def approve_case_policy(
    db: Session,
    uow: UnitOfWork,
    *,
    case: Case,
    valid_from: date,
    valid_to: date | None,
    actor_id: str,
    actor_capability: str,
) -> PolicyVersion:
    """The SEPARATE, explicit "approve reuse" command. Only callable on a
    case a human has already RESOLVED (approve/edit) -- never on a
    rejected case, and never on a case that was ITSELF auto-resolved by an
    existing policy (that would let a policy silently approve a broader
    version of itself with no new human decision -- a real one-time-vs-
    reusable human choice must exist at the root of every policy)."""
    if case.status != "RESOLVED":
        raise ValueError(f"case {case.id} is not RESOLVED (status={case.status})")
    if case.resolution_decision not in ("approve", "edit"):
        raise ValueError(
            f"case {case.id} was not approved/edited (decision={case.resolution_decision})"
        )
    if case.auto_resolved:
        raise ValueError(
            f"case {case.id} was itself auto-resolved by policy "
            f"{case.applied_policy_version_id}; approve reuse from the ORIGINAL case instead"
        )
    if case.resolution_effect is None:
        raise ValueError(f"case {case.id} has no recorded resolution_effect to make reusable")

    vendor = db.get(Vendor, case.vendor_id)
    scope = {"vendor_id": case.vendor_id, "festival_id": vendor.festival_id if vendor else None}
    new_policy_id = _new_id()
    policy = PolicyVersion(
        policy_id=new_policy_id,
        version=1,
        scope=scope,
        predicates=case.predicate_values,
        effect=case.resolution_effect,
        evidence_refs=case.evidence_refs,
        approver_ref=actor_id,
        valid_from=valid_from,
        valid_to=valid_to,
        status="ACTIVE",
        supersedes_version=None,
        source_case_id=case.id,
        approved_at=datetime.now(UTC),
    )
    db.add(policy)
    db.flush()

    uow.append_event(
        stream_id=f"policy:{policy.policy_id}",
        event_type="policy.approved",
        aggregate_id=policy.id,
        aggregate_version=1,
        actor_id=actor_id,
        actor_type="human",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        vendor_id=case.vendor_id,
        payload={
            "policy_id": policy.policy_id,
            "version": policy.version,
            "scope": scope,
            "predicates": case.predicate_values,
            "effect": case.resolution_effect,
            "valid_from": valid_from.isoformat(),
            "valid_to": valid_to.isoformat() if valid_to else None,
            "source_case_id": case.id,
        },
        lineage_refs=[
            {"ref_kind": "case", "ref_id": case.id},
            {"ref_kind": "policy_version", "ref_id": policy.id},
        ],
    )
    return policy


def challenge_case(
    db: Session,
    uow: UnitOfWork,
    *,
    case: Case,
    note: str,
    actor_id: str,
    actor_capability: str,
) -> Case:
    """DATA MODEL / ACTION HOOK ONLY. The actual challenge workflow --
    preview of affected settlements/payouts/journal entries, pausing
    dependent work, obtaining a correction approval -- is A09's job
    (TASK_IMPLEMENTATION.md A09). This function only records that a
    RESOLVED case (including an auto-resolved one) has been flagged for
    challenge, so A09 has a real column/event to build its workflow on top
    of, instead of needing to also invent the case-side data model."""
    if case.status != "RESOLVED":
        raise ValueError(
            f"case {case.id} is not RESOLVED (status={case.status}); nothing to challenge"
        )
    case.challenge_status = "challenged"
    case.challenged_by = actor_id
    case.challenged_at = datetime.now(UTC)
    case.challenge_note = note
    db.add(case)
    db.flush()
    uow.append_event(
        stream_id=f"case:{case.id}",
        event_type="case.challenged",
        aggregate_id=case.id,
        aggregate_version=3,
        actor_id=actor_id,
        actor_type="human",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        vendor_id=case.vendor_id,
        payload={"note": note},
        lineage_refs=[{"ref_kind": "case", "ref_id": case.id}],
    )
    return case


def suspend_policy(
    db: Session,
    uow: UnitOfWork,
    *,
    policy: PolicyVersion,
    reason: str,
    actor_id: str,
    actor_capability: str,
) -> PolicyVersion:
    """Suspends an ACTIVE policy so it can no longer auto-resolve ANY new
    case (find_active_matching_policy only ever considers status=="ACTIVE"
    rows) -- without deleting the row or touching any Case that already
    cites it. Past `applied_policy_version_id` references on existing
    cases are untouched: they still point at this exact row/id, which
    still exists with its original scope/predicates/effect, just a
    different `status`."""
    if policy.status != "ACTIVE":
        raise ValueError(f"only an ACTIVE policy can be suspended (status={policy.status})")
    policy.status = "SUSPENDED"
    db.add(policy)
    db.flush()
    uow.append_event(
        stream_id=f"policy:{policy.policy_id}",
        event_type="policy.suspended",
        aggregate_id=policy.id,
        aggregate_version=2,
        actor_id=actor_id,
        actor_type="human",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        payload={"policy_id": policy.policy_id, "version": policy.version, "reason": reason},
        lineage_refs=[{"ref_kind": "policy_version", "ref_id": policy.id}],
    )
    return policy


def revise_policy(
    db: Session,
    uow: UnitOfWork,
    *,
    policy: PolicyVersion,
    new_effect: dict,
    new_valid_from: date,
    new_valid_to: date | None,
    evidence_refs: list | None,
    actor_id: str,
    actor_capability: str,
) -> PolicyVersion:
    """Supersession: creates a NEW PolicyVersion row (same `policy_id`,
    version+1, `supersedes_version` set) and marks the OLD row SUPERSEDED
    -- never edits the old row's scope/predicates/effect/evidence in
    place. Any Case whose `applied_policy_version_id` points at the OLD
    version keeps citing that exact version/id forever (supersession
    cannot retroactively conceal what a prior version actually did)."""
    if policy.status != "ACTIVE":
        raise ValueError(f"only an ACTIVE policy can be revised (status={policy.status})")

    new_version = PolicyVersion(
        policy_id=policy.policy_id,
        version=policy.version + 1,
        scope=policy.scope,
        predicates=policy.predicates,
        effect=new_effect,
        evidence_refs=evidence_refs if evidence_refs is not None else policy.evidence_refs,
        approver_ref=actor_id,
        valid_from=new_valid_from,
        valid_to=new_valid_to,
        status="ACTIVE",
        supersedes_version=policy.version,
        source_case_id=policy.source_case_id,
        approved_at=datetime.now(UTC),
    )
    db.add(new_version)
    db.flush()

    policy.status = "SUPERSEDED"
    db.add(policy)
    db.flush()

    uow.append_event(
        stream_id=f"policy:{policy.policy_id}",
        event_type="policy.superseded",
        aggregate_id=policy.id,
        aggregate_version=2,
        actor_id=actor_id,
        actor_type="human",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        payload={
            "policy_id": policy.policy_id,
            "superseded_version": policy.version,
            "superseding_version_row_id": new_version.id,
        },
        lineage_refs=[
            {"ref_kind": "policy_version", "ref_id": policy.id},
            {"ref_kind": "policy_version", "ref_id": new_version.id},
        ],
    )
    uow.append_event(
        stream_id=f"policy:{new_version.policy_id}",
        event_type="policy.approved",
        aggregate_id=new_version.id,
        aggregate_version=1,
        actor_id=actor_id,
        actor_type="human",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        payload={
            "policy_id": new_version.policy_id,
            "version": new_version.version,
            "supersedes_version": new_version.supersedes_version,
            "effect": new_effect,
        },
        lineage_refs=[{"ref_kind": "policy_version", "ref_id": new_version.id}],
    )
    return new_version
