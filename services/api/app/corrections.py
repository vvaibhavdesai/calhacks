"""A09 challenge -> impact preview -> explicit approval -> linked correction.

Flow (AGENT_HANDOFF.md "Challenges and reversal", C06):

1. `create_challenge`: a human challenges a decided result (a policy
   version, a settlement, a payout or a case) with superseding evidence --
   real `source_versions` rows, hash-checked. In ONE transaction it suspends
   the rule (policy ACTIVE -> SUSPENDED), places a vendor hold that
   app/payout_dispatch.py checks at release and at dispatch, and records
   what it paused. It changes no money. It takes the vendor's payout row
   locks before inserting the hold, so a concurrent release either commits
   first (and is then planned for as a pending payout) or is blocked.

2. `preview_correction`: recomputes the vendor's settlement chain with the
   corrected term (A04's own `calculate_vendor_settlement`, via
   domain.corrections.recompute_vendor_chain) and lists every affected
   record by its state now: draft payouts to revise, pending payouts to
   cancel-request, completed payouts to offset/recover, posted journals to
   reverse-and-correct, statements to reissue, and every other place the
   rule was used. The plan is stored with `preview_hash`; nothing financial
   changes.

3. `approve_correction`: the approver quotes the correction version,
   preview hash, evidence hash and item ids. The plan is recomputed under
   the payout locks; if the facts moved since the preview (e.g. a payout
   left DRAFT) the correction becomes STALE and nothing is applied. Else,
   in ONE transaction: the policy is superseded by a new version carrying
   the corrected effect, the contract term is superseded, a NEW linked
   settlement version is written per affected night (originals untouched),
   draft/pending payouts are cancelled through A07's own cancel path and
   replaced by distinct obligations, completed payouts get an additional
   payout or an OPEN vendor recovery (app/recovery.py; resolved only by a
   further human decision), posted commission journals get a linked
   reversing entry and a linked correcting entry (A08 `post_cumulative`,
   never a rewrite), and statements are reissued as new versions.

   A payout whose cancellation is still pending at the rail
   (CANCEL_REQUESTED) cannot be settled yet: its night stays contingent,
   the vendor hold stays, and `resolve_contingent` finishes it after the
   rail outcome is reconciled (A07 `/payouts/{id}/reconcile`).

Every state change appends an audit event through app/uow.py in the same
transaction, with `event_lineage` rows linking challenge, correction,
original and corrected records. The caller (router) commits once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from domain import corrections as dc
from domain.settlement import CALCULATION_VERSION
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.accounting as accounting
import app.payout_dispatch as payout_dispatch
from app.finance_reports import issue_vendor_statement
from app.holds import active_vendor_hold, place_hold, release_holds
from app.models import _new_id
from app.models_corrections import Challenge, Correction, CorrectionApproval, WorkHold
from app.models_finance import VendorStatement
from app.models_ledger import (
    BusinessEvent,
    Case,
    EventLineage,
    JournalEntry,
    Payout,
    PolicyVersion,
    SourceVersion,
    Vendor,
    VendorContractTerm,
)
from app.models_settlement import Settlement, SettlementLineItem
from app.recovery import open_recovery
from app.uow import UnitOfWork

TARGET_KINDS = ("POLICY_VERSION", "SETTLEMENT", "PAYOUT", "CASE")
OPEN_CHALLENGE_STATES = ("OPEN", "PREVIEWED", "CORRECTION_APPROVED")
HOLD_REASON = "challenge_open_terms_under_review"


class CorrectionError(ValueError):
    def __init__(
        self, code: str, message: str, *, status: int = 422, details: dict | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}


class NotFoundError(LookupError):
    pass


class VersionConflictError(ValueError):
    def __init__(self, expected: int | None, current: int) -> None:
        super().__init__(f"expected version {expected}, current {current}")
        self.expected = expected
        self.current = current


class StalePlanError(CorrectionError):
    """Raised AFTER the correction has been marked STALE on the session;
    the router commits that state change before answering 422."""


@dataclass
class TargetContext:
    festival_id: str
    vendor: Vendor
    policy: PolicyVersion | None
    case: Case | None
    term: VendorContractTerm | None
    current_version: int


# ---------------------------------------------------------------------------
# Lookups along explicit links
# ---------------------------------------------------------------------------


def _term_created_by_case(db: Session, case: Case) -> VendorContractTerm | None:
    term_ids = db.execute(
        select(EventLineage.ref_id)
        .join(BusinessEvent, BusinessEvent.event_id == EventLineage.event_id)
        .where(
            BusinessEvent.event_type == "case.resolved",
            BusinessEvent.aggregate_id == case.id,
            EventLineage.ref_kind == "vendor_contract_term",
        )
    ).scalars()
    terms = [t for t in (db.get(VendorContractTerm, i) for i in term_ids) if t is not None]
    return max(terms, key=lambda t: t.version) if terms else None


def _case_that_created_term(db: Session, term: VendorContractTerm) -> Case | None:
    case_id = db.execute(
        select(BusinessEvent.aggregate_id)
        .join(EventLineage, EventLineage.event_id == BusinessEvent.event_id)
        .where(
            BusinessEvent.event_type == "case.resolved",
            EventLineage.ref_kind == "vendor_contract_term",
            EventLineage.ref_id == term.id,
        )
    ).scalar()
    return db.get(Case, case_id) if case_id else None


def _policy_for_case(db: Session, case: Case) -> PolicyVersion | None:
    rows = (
        db.execute(
            select(PolicyVersion)
            .where(PolicyVersion.source_case_id == case.id)
            .order_by(PolicyVersion.version.desc())
        )
        .scalars()
        .all()
    )
    for status in ("ACTIVE", "SUSPENDED"):
        for p in rows:
            if p.status == status:
                return p
    return rows[0] if rows else None


def resolve_target(db: Session, kind: str, target_id: str) -> TargetContext:
    if kind not in TARGET_KINDS:
        raise CorrectionError("UNSUPPORTED_TARGET", f"target kind must be one of {TARGET_KINDS}")
    policy = case = term = None
    vendor_id = None
    if kind == "POLICY_VERSION":
        policy = db.get(PolicyVersion, target_id)
        if policy is None:
            raise NotFoundError("policy version not found")
        version = policy.version
        vendor_id = (policy.scope or {}).get("vendor_id")
        case = db.get(Case, policy.source_case_id) if policy.source_case_id else None
        term = _term_created_by_case(db, case) if case else None
    elif kind in ("SETTLEMENT", "PAYOUT"):
        if kind == "PAYOUT":
            payout = db.get(Payout, target_id)
            if payout is None:
                raise NotFoundError("payout not found")
            version = payout.version
            settlement = db.get(Settlement, payout.settlement_id) if payout.settlement_id else None
            if settlement is None:
                raise CorrectionError("NO_SETTLEMENT", "payout is not linked to a settlement")
        else:
            settlement = db.get(Settlement, target_id)
            if settlement is None:
                raise NotFoundError("settlement not found")
            version = settlement.version
        vendor_id = settlement.vendor_id
        term = (
            db.get(VendorContractTerm, settlement.vendor_contract_term_id)
            if settlement.vendor_contract_term_id
            else None
        )
        case = _case_that_created_term(db, term) if term else None
        policy = _policy_for_case(db, case) if case else None
    else:  # CASE
        case = db.get(Case, target_id)
        if case is None:
            raise NotFoundError("case not found")
        version = case.version
        vendor_id = case.vendor_id
        if case.auto_resolved and case.applied_policy_version_id:
            policy = db.get(PolicyVersion, case.applied_policy_version_id)
            origin = (
                db.get(Case, policy.source_case_id) if policy and policy.source_case_id else None
            )
            term = _term_created_by_case(db, origin) if origin else None
            case = origin or case
        else:
            term = _term_created_by_case(db, case)
            policy = _policy_for_case(db, case)
    vendor = db.get(Vendor, vendor_id) if vendor_id else None
    if vendor is None:
        raise CorrectionError("NO_VENDOR", "the challenged record is not scoped to a vendor")
    return TargetContext(
        festival_id=vendor.festival_id,
        vendor=vendor,
        policy=policy,
        case=case,
        term=term,
        current_version=version,
    )


def _lock_vendor_payouts(db: Session, vendor_id: str) -> list[Payout]:
    return list(
        db.execute(
            select(Payout)
            .where(Payout.vendor_id == vendor_id)
            .order_by(Payout.business_date, Payout.created_at, Payout.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        .scalars()
        .all()
    )


def _normalize_evidence(db: Session, evidence: list[dict]) -> list[dict]:
    if not evidence:
        raise CorrectionError(
            "EVIDENCE_REQUIRED", "superseding evidence is required", details={"field": "evidence"}
        )
    out = []
    for i, ref in enumerate(evidence):
        doc_id = ref.get("source_id") or ref.get("source_document_id")
        version = ref.get("source_version")
        sv = None
        if doc_id and version is not None:
            sv = db.execute(
                select(SourceVersion).where(
                    SourceVersion.source_document_id == doc_id,
                    SourceVersion.version == int(version),
                )
            ).scalar_one_or_none()
        if sv is None:
            raise CorrectionError(
                "UNKNOWN_EVIDENCE",
                f"superseding_evidence[{i}] does not resolve to a stored source version",
                details={"index": i},
            )
        claimed = dc.normalize_content_hash(ref.get("content_hash") or "")
        if claimed != sv.content_hash:
            raise CorrectionError(
                "EVIDENCE_HASH_MISMATCH",
                f"superseding_evidence[{i}] content_hash does not match the stored bytes",
                details={"index": i},
            )
        if sv.status != "active":
            raise CorrectionError(
                "EVIDENCE_QUARANTINED", f"superseding_evidence[{i}] is quarantined"
            )
        out.append(
            {
                "source_document_id": doc_id,
                "source_version": int(version),
                "content_hash": sv.content_hash,
                "locator_kind": ref.get("locator_kind") or ("span" if ref.get("locator") else None),
                "locator": ref.get("locator"),
                "quoted_excerpt": ref.get("quoted_excerpt"),
            }
        )
    return out


def _event(
    db: Session,
    *,
    stream_id: str,
    event_type: str,
    aggregate_id: str,
    aggregate_version: int,
    vendor_id: str | None,
    actor_id: str | None,
    capability: str | None,
    payload: dict,
    lineage: list[tuple[str, str | None]],
    correlation_id: str | None = None,
    command_id: str | None = None,
    occurred_at: datetime | None = None,
) -> BusinessEvent:
    return UnitOfWork(db).append_event(
        stream_id=stream_id,
        event_type=event_type,
        aggregate_id=aggregate_id,
        aggregate_version=aggregate_version,
        actor_id=actor_id,
        actor_type="human" if actor_id else "system",
        actor_capability=capability,
        occurred_at=occurred_at or datetime.now(UTC),
        vendor_id=vendor_id,
        correlation_id=correlation_id,
        command_id=command_id,
        payload=payload,
        lineage_refs=[{"ref_kind": k, "ref_id": v} for k, v in lineage if v],
    )


# ---------------------------------------------------------------------------
# 1. Challenge
# ---------------------------------------------------------------------------


def create_challenge(
    db: Session,
    *,
    target_kind: str,
    target_id: str,
    expected_version: int | None,
    evidence: list[dict],
    reason: str,
    proposed_effect: dict | None,
    effective_from: date | None,
    actor_id: str,
    capability: str,
    command_id: str | None,
) -> Challenge:
    if not (reason or "").strip():
        raise CorrectionError(
            "REASON_REQUIRED", "a reason is required", details={"field": "reason"}
        )
    if proposed_effect is not None:
        bps = proposed_effect.get("commission_bps")
        if not isinstance(bps, int) or isinstance(bps, bool) or not 0 <= bps <= 10_000:
            raise CorrectionError(
                "INVALID_PROPOSED_EFFECT",
                "proposed_effect.commission_bps must be an integer 0..10000",
                details={"field": "proposed_effect.commission_bps"},
            )
        proposed_effect = {"commission_bps": bps}
    refs = _normalize_evidence(db, evidence)
    ctx = resolve_target(db, target_kind, target_id)
    if expected_version is not None and expected_version != ctx.current_version:
        raise VersionConflictError(expected_version, ctx.current_version)

    # Serialize challenges per vendor on the vendor row, then take every
    # payout lock this vendor has (same order release_payout uses: payout
    # first, then holds are read), before the hold exists.
    db.execute(select(Vendor).where(Vendor.id == ctx.vendor.id).with_for_update()).scalar_one()
    if ctx.policy is not None:
        db.execute(
            select(PolicyVersion)
            .where(PolicyVersion.id == ctx.policy.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).scalar_one()
    existing = db.execute(
        select(Challenge.id).where(
            Challenge.vendor_id == ctx.vendor.id, Challenge.status.in_(OPEN_CHALLENGE_STATES)
        )
    ).first()
    if existing is not None:
        raise CorrectionError(
            "CHALLENGE_ALREADY_OPEN",
            "this vendor's terms already have an open challenge",
            details={"challenge_id": existing[0]},
        )
    payouts = _lock_vendor_payouts(db, ctx.vendor.id)

    if effective_from is None:
        if ctx.term is not None:
            effective_from = ctx.term.effective_from
        elif ctx.policy is not None and ctx.policy.valid_from is not None:
            effective_from = ctx.policy.valid_from

    now = datetime.now(UTC)
    challenge = Challenge(
        id=_new_id(),
        version=1,
        status="OPEN",
        festival_id=ctx.festival_id,
        vendor_id=ctx.vendor.id,
        target_kind=target_kind,
        target_id=target_id,
        target_version=ctx.current_version,
        policy_version_id=ctx.policy.id if ctx.policy else None,
        case_id=ctx.case.id if ctx.case else None,
        contract_term_id=ctx.term.id if ctx.term else None,
        reason=reason.strip(),
        superseding_evidence=refs,
        evidence_hash=dc.canonical_hash(sorted(refs, key=dc.canonical_json)),
        proposed_effect=proposed_effect,
        effective_from=effective_from,
        created_by=actor_id,
        capability=capability,
        correlation_id=_new_id(),
    )
    db.add(challenge)
    db.flush()

    policy_effect = None
    if ctx.policy is not None:
        before = ctx.policy.status
        if before == "ACTIVE":
            ctx.policy.status = "SUSPENDED"
            db.flush()
            _event(
                db,
                stream_id=f"policy:{ctx.policy.policy_id}",
                event_type="policy.suspended",
                aggregate_id=ctx.policy.id,
                aggregate_version=ctx.policy.version,
                vendor_id=ctx.vendor.id,
                actor_id=actor_id,
                capability=capability,
                payload={
                    "policy_id": ctx.policy.policy_id,
                    "version": ctx.policy.version,
                    "reason": f"challenged: {challenge.reason}"[:500],
                    "challenge_id": challenge.id,
                },
                lineage=[("policy_version", ctx.policy.id), ("challenge", challenge.id)],
                correlation_id=challenge.correlation_id,
                command_id=command_id,
            )
        policy_effect = {
            "policy_version_id": ctx.policy.id,
            "status_before": before,
            "status_now": ctx.policy.status,
            "paused_at": now.isoformat(),
        }

    hold = place_hold(
        db,
        festival_id=ctx.festival_id,
        vendor_id=ctx.vendor.id,
        record_kind="VENDOR",
        record_id=ctx.vendor.id,
        reason_code=HOLD_REASON,
        source_kind="challenge",
        source_id=challenge.id,
        actor_id=actor_id,
    )
    paused = [
        {
            "record": {
                "kind": "VENDOR",
                "id": ctx.vendor.id,
                "version": None,
                "label": ctx.vendor.name,
            },
            "detail": (
                "All releases and dispatches for this vendor are held until the challenge is "
                "corrected or withdrawn; new settlements are calculated but marked held."
            ),
            "hold_id": hold.id,
        }
    ]
    for p in payouts:
        if p.state in ("DRAFT", "APPROVED"):
            detail = "Release held (draft/approved; not reserved)."
        elif p.state == "DISPATCH_PENDING":
            detail = (
                "Dispatch held: released but not yet sent to the rail; correction will cancel it."
            )
        elif p.state in ("UNKNOWN", "CANCEL_REQUESTED"):
            detail = "Already with the rail, outcome unknown: correction will request cancellation."
        elif p.state in ("SUBMITTED", "CONFIRMED"):
            detail = "Already paid: any overpayment becomes an offset/recovery, never 'unsent'."
        else:
            continue
        paused.append(
            {
                "record": {
                    "kind": "PAYOUT",
                    "id": p.id,
                    "version": p.version,
                    "label": f"{p.business_date} {p.state}",
                },
                "detail": detail,
            }
        )
    if policy_effect is not None:
        policy_effect["paused_work"] = paused
    challenge.policy_effect = policy_effect
    challenge.paused_work = paused

    if ctx.case is not None:
        ctx.case.challenge_status = "challenged"
        ctx.case.challenged_by = actor_id
        ctx.case.challenged_at = now
        ctx.case.challenge_note = challenge.reason
        _event(
            db,
            stream_id=f"case:{ctx.case.id}",
            event_type="case.challenged",
            aggregate_id=ctx.case.id,
            aggregate_version=ctx.case.version,
            vendor_id=ctx.vendor.id,
            actor_id=actor_id,
            capability=capability,
            payload={"note": challenge.reason, "challenge_id": challenge.id},
            lineage=[("case", ctx.case.id), ("challenge", challenge.id)],
            correlation_id=challenge.correlation_id,
        )
    db.flush()
    _event(
        db,
        stream_id=f"challenge:{challenge.id}",
        event_type="challenge.created",
        aggregate_id=challenge.id,
        aggregate_version=challenge.version,
        vendor_id=ctx.vendor.id,
        actor_id=actor_id,
        capability=capability,
        payload={
            "status": challenge.status,
            "festival_id": ctx.festival_id,
            "target": {"kind": target_kind, "id": target_id, "version": ctx.current_version},
            "reason": challenge.reason,
            "superseding_evidence": refs,
            "evidence_hash": challenge.evidence_hash,
            "proposed_effect": proposed_effect,
            "effective_from": effective_from.isoformat() if effective_from else None,
            "policy_effect": policy_effect,
            "paused_work": paused,
        },
        lineage=[
            ("challenge", challenge.id),
            (target_kind.lower(), target_id),
            ("policy_version", ctx.policy.id if ctx.policy else None),
            ("vendor_contract_term", ctx.term.id if ctx.term else None),
        ]
        + [("source_document", r["source_document_id"]) for r in refs],
        correlation_id=challenge.correlation_id,
        command_id=command_id,
    )
    return challenge


def withdraw_challenge(
    db: Session, *, challenge_id: str, expected_version: int | None, actor_id: str, capability: str
) -> Challenge:
    ch = _lock_challenge(db, challenge_id)
    if expected_version is not None and expected_version != ch.version:
        raise VersionConflictError(expected_version, ch.version)
    if ch.status not in ("OPEN", "PREVIEWED"):
        raise CorrectionError("INVALID_TRANSITION", f"challenge is {ch.status}; cannot withdraw")
    policy = db.get(PolicyVersion, ch.policy_version_id) if ch.policy_version_id else None
    if (
        policy is not None
        and policy.status == "SUSPENDED"
        and (ch.policy_effect or {}).get("status_before") == "ACTIVE"
    ):
        policy.status = "ACTIVE"
        _event(
            db,
            stream_id=f"policy:{policy.policy_id}",
            event_type="policy.reactivated",
            aggregate_id=policy.id,
            aggregate_version=policy.version,
            vendor_id=ch.vendor_id,
            actor_id=actor_id,
            capability=capability,
            payload={"policy_id": policy.policy_id, "reason": "challenge withdrawn"},
            lineage=[("policy_version", policy.id), ("challenge", ch.id)],
            correlation_id=ch.correlation_id,
        )
    for corr in db.execute(
        select(Correction).where(
            Correction.challenge_id == ch.id, Correction.status == "PREVIEW_READY"
        )
    ).scalars():
        corr.status = "STALE"
        corr.version += 1
    release_holds(
        db, source_kind="challenge", source_id=ch.id, actor_id=actor_id, note="challenge withdrawn"
    )
    case = db.get(Case, ch.case_id) if ch.case_id else None
    if case is not None:
        case.challenge_status = "withdrawn"
    ch.status = "WITHDRAWN"
    ch.version += 1
    db.flush()
    _event(
        db,
        stream_id=f"challenge:{ch.id}",
        event_type="challenge.withdrawn",
        aggregate_id=ch.id,
        aggregate_version=ch.version,
        vendor_id=ch.vendor_id,
        actor_id=actor_id,
        capability=capability,
        payload={"status": ch.status, "reason": "withdrawn"},
        lineage=[("challenge", ch.id)],
        correlation_id=ch.correlation_id,
    )
    return ch


def _lock_challenge(db: Session, challenge_id: str) -> Challenge:
    ch = db.execute(
        select(Challenge)
        .where(Challenge.id == challenge_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if ch is None:
        raise NotFoundError("challenge not found")
    return ch


# ---------------------------------------------------------------------------
# 2. Plan (pure read)
# ---------------------------------------------------------------------------


def _live_settlements(db: Session, vendor_id: str, festival_id: str) -> list[Settlement]:
    rows = (
        db.execute(
            select(Settlement)
            .where(Settlement.vendor_id == vendor_id, Settlement.festival_id == festival_id)
            .order_by(Settlement.business_date, Settlement.created_at)
        )
        .scalars()
        .all()
    )
    superseded = {s.correction_of for s in rows if s.correction_of}
    live_by_date: dict[date, Settlement] = {}
    for s in rows:
        if s.id not in superseded:
            live_by_date[s.business_date] = s  # latest created wins
    return [live_by_date[d] for d in sorted(live_by_date)]


def _chain_ids(db: Session, s: Settlement) -> list[str]:
    ids = [s.id]
    node = s
    while node.correction_of:
        ids.append(node.correction_of)
        node = db.get(Settlement, node.correction_of)
        if node is None:
            break
    return ids


def _root(db: Session, s: Settlement) -> Settlement:
    node = s
    while node.correction_of:
        parent = db.get(Settlement, node.correction_of)
        if parent is None:
            break
        node = parent
    return node


def _commission_family(db: Session, s: Settlement) -> tuple[str, list[JournalEntry]]:
    root = _root(db, s)
    base = f"COMMISSION:{root.vendor_id}:{root.business_date.isoformat()}:{root.id}"
    fam = (
        db.execute(
            select(JournalEntry)
            .where((JournalEntry.entry_key == base) | JournalEntry.entry_key.like(base + "#adj%"))
            .order_by(JournalEntry.created_at, JournalEntry.entry_key)
        )
        .scalars()
        .all()
    )
    return base, list(fam)


def _ref(kind: str, rid: str, version: int | None = None, label: str | None = None) -> dict:
    return {"kind": kind, "id": rid, "version": version, "label": label}


def _cents(v: int | None) -> str | None:
    return None if v is None else str(v)


def build_plan(db: Session, challenge: Challenge) -> dict:
    """Deterministic for a given database state (so its hash can bind an
    approval). Reads only."""
    proposed = challenge.proposed_effect or {}
    if "commission_bps" not in proposed:
        raise CorrectionError(
            "PROPOSED_EFFECT_REQUIRED",
            "this challenge carries no proposed_effect.commission_bps; a correction plan needs "
            "the corrected term (the challenge itself already paused the rule)",
        )
    new_bps = int(proposed["commission_bps"])
    vendor = db.get(Vendor, challenge.vendor_id)
    effective_from = challenge.effective_from or date.min
    live = _live_settlements(db, vendor.id, challenge.festival_id)
    periods = [
        dc.PeriodInput(
            settlement_id=s.id,
            business_date=s.business_date,
            centrally_held_cents=s.centrally_held_cents,
            vendor_retained_cash_cents=s.vendor_retained_cash_cents,
            tax_cents=s.tax_cents,
            tips_cents=s.tips_cents,
            processor_fee_assessed_cents=s.processor_fee_assessed_cents,
            processor_fee_deductible=s.processor_fee_deductible_used,
            is_final_period=s.is_final_period,
            minimum_fee_cents=s.minimum_fee_cents_used,
            commission_bps=s.commission_bps_used,
            assessed_fee_cents=s.assessed_fee_cents,
            payout_cents=s.payout_cents,
        )
        for s in live
    ]
    recomputed = dc.recompute_vendor_chain(
        periods, new_commission_bps=new_bps, effective_from=effective_from
    )
    payouts = (
        db.execute(
            select(Payout)
            .where(Payout.vendor_id == vendor.id)
            .order_by(Payout.business_date, Payout.created_at, Payout.id)
        )
        .scalars()
        .all()
    )
    by_settlement: dict[str, list[Payout]] = {}
    for p in payouts:
        if p.settlement_id:
            by_settlement.setdefault(p.settlement_id, []).append(p)

    items: list[dict] = []
    nights: list[dict] = []
    fee_delta = overpaid = additional = 0
    offset_capacity = 0
    contingent_max = 0
    usage_settlements: list[dict] = []
    usage_payouts: list[dict] = []
    usage_journal: list[dict] = []
    usage_statements: list[dict] = []
    term_id = challenge.contract_term_id

    for s, r in zip(live, recomputed, strict=True):
        chain = _chain_ids(db, s)
        if term_id and db.get(Settlement, s.id).vendor_contract_term_id == term_id:
            usage_settlements.append(_ref("SETTLEMENT", s.id, s.version, str(s.business_date)))
        if not r.changed:
            continue
        new = r.result
        bd = s.business_date.isoformat()
        night = {
            "business_date": bd,
            "settlement_id": s.id,
            "settlement_version": s.version,
            "old_fee_cents": s.assessed_fee_cents,
            "new_fee_cents": new.fee_assessed_cents,
            "old_payout_cents": s.payout_cents,
            "new_payout_cents": new.vendor_payout_cents,
            "commission_bps": r.commission_bps,
        }
        fee_delta += r.fee_delta_cents
        items.append(
            {
                "item_id": f"SETTLEMENT_REVISION:{s.id}",
                "record": _ref("SETTLEMENT", s.id, s.version, f"Settlement {bd}"),
                "state_at_preview": s.status,
                "state_group": "SETTLEMENT",
                "treatment": "NEW_LINKED_VERSION",
                "description": (
                    f"Write settlement version {s.version + 1} for {bd} at {r.commission_bps} bps "
                    f"(fee {s.assessed_fee_cents} -> {new.fee_assessed_cents} cents); the original "
                    "version is kept unchanged and linked."
                ),
                "original_cents": _cents(s.assessed_fee_cents),
                "corrected_cents": _cents(new.fee_assessed_cents),
                "delta_cents": _cents(r.fee_delta_cents),
                "amount_label": "commission assessed",
                "contingent_on": None,
                "requires_human": False,
            }
        )
        chain_payouts = [p for sid in chain for p in by_settlement.get(sid, [])]
        chain_payouts.sort(key=lambda p: (p.created_at, p.id))
        paid = sum(p.amount_cents for p in chain_payouts if p.state in dc.COMPLETED_STATES)
        pending = [p for p in chain_payouts if p.state in dc.PENDING_STATES]
        drafts = [p for p in chain_payouts if p.state in dc.DRAFT_STATES]
        for p in chain_payouts:
            usage_payouts.append(_ref("PAYOUT", p.id, p.version, f"{bd} {p.state}"))
        night.update(
            paid_cents=paid,
            pending_payout_ids=[p.id for p in pending],
            draft_payout_ids=[p.id for p in drafts],
            had_payout=bool(chain_payouts),
        )
        for p in drafts:
            items.append(
                {
                    "item_id": f"DRAFT_REVISE:{p.id}",
                    "record": _ref("PAYOUT", p.id, p.version, f"Payout {bd}"),
                    "state_at_preview": p.state,
                    "state_group": "DRAFT",
                    "treatment": "DRAFT_REVISE",
                    "description": (
                        "Cancel this unreleased obligation and replace it with a distinct "
                        "obligation for the corrected amount (new idempotency key)."
                    ),
                    "original_cents": _cents(p.amount_cents),
                    "corrected_cents": _cents(max(new.vendor_payout_cents - paid, 0)),
                    "delta_cents": _cents(max(new.vendor_payout_cents - paid, 0) - p.amount_cents),
                    "amount_label": "SIMULATED payout",
                    "contingent_on": None,
                    "requires_human": False,
                }
            )
        for p in pending:
            pend_id = f"PENDING_CANCEL_REQUEST:{p.id}"
            items.append(
                {
                    "item_id": pend_id,
                    "record": _ref("PAYOUT", p.id, p.version, f"Payout {bd}"),
                    "state_at_preview": p.state,
                    "state_group": "PENDING",
                    "treatment": "PENDING_CANCEL_REQUEST",
                    "description": (
                        "Request cancellation through the payout's own cancel path and reconcile "
                        "its actual rail outcome; a payment that already won the race is "
                        "recovered, never treated as unsent."
                    ),
                    "original_cents": _cents(p.amount_cents),
                    "corrected_cents": _cents(new.vendor_payout_cents),
                    "delta_cents": _cents(new.vendor_payout_cents - p.amount_cents),
                    "amount_label": "SIMULATED payout",
                    "contingent_on": None,
                    "requires_human": False,
                }
            )
            worst = max(paid + p.amount_cents - new.vendor_payout_cents, 0)
            contingent_max += worst
            items.append(
                {
                    "item_id": f"COMPLETED_RECOVERY_OFFSET:{p.id}:if_paid",
                    "record": _ref("PAYOUT", p.id, p.version, f"Payout {bd}"),
                    "state_at_preview": p.state,
                    "state_group": "COMPLETED",
                    "treatment": "COMPLETED_RECOVERY_OFFSET",
                    "description": (
                        "Only if the rail reports this payout was paid: open a vendor recovery "
                        "for the overpayment (or an additional obligation if underpaid)."
                    ),
                    "original_cents": _cents(p.amount_cents),
                    "corrected_cents": _cents(new.vendor_payout_cents),
                    "delta_cents": _cents(new.vendor_payout_cents - paid - p.amount_cents),
                    "amount_label": "if paid",
                    "contingent_on": pend_id,
                    "requires_human": True,
                }
            )
        if not pending:
            add, rec = dc.paid_adjustment(
                paid_cents=paid, corrected_payout_cents=new.vendor_payout_cents
            )
            if paid > 0:
                completed = [p for p in chain_payouts if p.state in dc.COMPLETED_STATES]
                last = completed[-1]
                overpaid += rec
                additional += add
                items.append(
                    {
                        "item_id": f"COMPLETED_RECOVERY_OFFSET:{last.id}",
                        "record": _ref("PAYOUT", last.id, last.version, f"Payout {bd}"),
                        "state_at_preview": last.state,
                        "state_group": "COMPLETED",
                        "treatment": "COMPLETED_RECOVERY_OFFSET",
                        "description": (
                            f"Already paid {paid} cents; corrected entitlement "
                            f"{new.vendor_payout_cents} cents. "
                            + (
                                f"Open a vendor recovery for {rec} cents (offset against a later "
                                "payout or a recovery request, each needing approval)."
                                if rec
                                else (
                                    f"Create an additional obligation for {add} cents."
                                    if add
                                    else "No money moves."
                                )
                            )
                        ),
                        "original_cents": _cents(paid),
                        "corrected_cents": _cents(new.vendor_payout_cents),
                        "delta_cents": _cents(new.vendor_payout_cents - paid),
                        "amount_label": "SIMULATED payout already sent",
                        "contingent_on": None,
                        "requires_human": rec > 0,
                    }
                )
            elif chain_payouts:
                offset_capacity += new.vendor_payout_cents
        night["offset_capacity_cents"] = 0 if paid or pending else new.vendor_payout_cents
        base, fam = _commission_family(db, s)
        if fam:
            posted_fee = sum(
                ln.credit_cents - ln.debit_cents
                for e in fam
                for ln in e.lines
                if ln.account_code == "4000"
            )
            night["journal_base_key"] = base
            usage_journal.append(_ref("JOURNAL_ENTRY", fam[0].id, None, base))
            items.append(
                {
                    "item_id": f"POSTED_REVERSE_AND_CORRECT:{fam[0].id}",
                    "record": _ref("JOURNAL_ENTRY", fam[0].id, None, base),
                    "state_at_preview": f"POSTED ({len(fam)} entries)",
                    "state_group": "POSTED",
                    "treatment": "POSTED_REVERSE_AND_CORRECT",
                    "description": (
                        "Append a linked reversing entry and a linked correcting entry to this "
                        "commission posting family; posted entries are never edited."
                    ),
                    "original_cents": _cents(posted_fee),
                    "corrected_cents": _cents(new.fee_assessed_cents),
                    "delta_cents": _cents(new.fee_assessed_cents - posted_fee),
                    "amount_label": "commission revenue (4000)",
                    "contingent_on": None,
                    "requires_human": False,
                }
            )
        stmts = (
            db.execute(
                select(VendorStatement)
                .where(
                    VendorStatement.vendor_id == vendor.id,
                    VendorStatement.business_date == s.business_date,
                )
                .order_by(VendorStatement.statement_version.desc())
            )
            .scalars()
            .all()
        )
        if stmts:
            latest = stmts[0]
            usage_statements.append(
                _ref("REPORT", latest.id, latest.statement_version, f"Statement {bd}")
            )
            items.append(
                {
                    "item_id": f"STATEMENT_REISSUE:{latest.id}",
                    "record": _ref(
                        "REPORT", latest.id, latest.statement_version, f"Statement {bd}"
                    ),
                    "state_at_preview": latest.status,
                    "state_group": "STATEMENT",
                    "treatment": "STATEMENT_REISSUE",
                    "description": (
                        f"Issue statement version {latest.statement_version + 1} from the "
                        "corrected settlement; earlier versions remain."
                    ),
                    "original_cents": _cents(s.payout_cents),
                    "corrected_cents": _cents(new.vendor_payout_cents),
                    "delta_cents": _cents(new.vendor_payout_cents - s.payout_cents),
                    "amount_label": "statement payout",
                    "contingent_on": None,
                    "requires_human": False,
                }
            )
        nights.append(night)

    # Unaffected draft payouts of this vendor can also absorb an offset.
    affected_ids = {n["settlement_id"] for n in nights}
    for p in payouts:
        if p.state == "DRAFT" and p.settlement_id:
            s = db.get(Settlement, p.settlement_id)
            if (
                s is not None
                and s.id not in affected_ids
                and not db.execute(
                    select(Settlement.id).where(Settlement.correction_of == s.id)
                ).first()
            ):
                offset_capacity += p.amount_cents
    human_recovery = max(overpaid - offset_capacity, 0) + contingent_max
    if overpaid > offset_capacity:
        items.append(
            {
                "item_id": f"HUMAN_RECOVERY_OR_WAIVER:{vendor.id}",
                "record": _ref("VENDOR", vendor.id, None, vendor.name),
                "state_at_preview": "NO_REMAINING_PAYOUT",
                "state_group": "NO_REMAINING_PAYOUT",
                "treatment": "HUMAN_RECOVERY_OR_WAIVER",
                "description": (
                    f"{overpaid - offset_capacity} cents cannot be offset against a remaining "
                    "payout: a human must request it from the vendor or a finance lead must "
                    "waive it. Future funds are not invented."
                ),
                "original_cents": None,
                "corrected_cents": None,
                "delta_cents": _cents(-(overpaid - offset_capacity)),
                "amount_label": "to recover by request or waiver",
                "contingent_on": None,
                "requires_human": True,
            }
        )

    policy_changes = []
    other_vendor_cases: list[dict] = []
    usage_cases: list[dict] = []
    policy = (
        db.get(PolicyVersion, challenge.policy_version_id) if challenge.policy_version_id else None
    )
    if policy is not None:
        applied = (
            db.execute(
                select(Case)
                .where(Case.applied_policy_version_id == policy.id)
                .order_by(Case.created_at)
            )
            .scalars()
            .all()
        )
        for c in applied:
            ref = _ref("CASE", c.id, c.version, f"{c.business_date} auto-resolved")
            (usage_cases if c.vendor_id == vendor.id else other_vendor_cases).append(ref)
        if policy.source_case_id:
            src = db.get(Case, policy.source_case_id)
            usage_cases.insert(
                0, _ref("CASE", src.id, src.version, f"{src.business_date} origin (human decision)")
            )
        policy_changes.append(
            {
                "policy_version_id": policy.id,
                "title": f"Policy {policy.policy_id} v{policy.version} {policy.effect}",
                "status_now": policy.status,
                "planned_status": "SUPERSEDED",
                "application_count": len(applied),
                "new_version": policy.version + 1,
                "new_effect": {**(policy.effect or {}), "commission_bps": new_bps},
            }
        )
    term = (
        db.get(VendorContractTerm, challenge.contract_term_id)
        if challenge.contract_term_id
        else None
    )
    plan = {
        "challenge_id": challenge.id,
        "vendor_id": vendor.id,
        "festival_id": challenge.festival_id,
        "new_commission_bps": new_bps,
        "effective_from": (
            challenge.effective_from.isoformat() if challenge.effective_from else None
        ),
        "evidence_hash": challenge.evidence_hash,
        "policy_changes": policy_changes,
        "contract_term_change": (
            {
                "contract_term_id": term.id,
                "commission_bps_now": term.commission_bps,
                "planned_commission_bps": new_bps,
                "status_now": term.status,
            }
            if term
            else None
        ),
        "nights": nights,
        "items": items,
        "usages": {
            "cases": usage_cases,
            "other_vendor_cases": other_vendor_cases,
            "settlements": usage_settlements,
            "payouts": usage_payouts,
            "journal_families": usage_journal,
            "statements": usage_statements,
        },
        "totals": {
            "fee_delta_cents": str(fee_delta),
            "overpaid_cents": str(overpaid),
            "additional_payout_cents": str(additional),
            "offset_capacity_cents": str(offset_capacity),
            "human_recovery_max_cents": str(human_recovery),
        },
    }
    return plan


def plan_hash(plan: dict) -> str:
    return dc.canonical_hash(plan)


def preview_correction(
    db: Session,
    *,
    challenge_id: str,
    expected_version: int | None,
    actor_id: str,
    capability: str,
    command_id: str | None,
    required_capability: str,
) -> Correction:
    ch = _lock_challenge(db, challenge_id)
    if expected_version is not None and expected_version != ch.version:
        raise VersionConflictError(expected_version, ch.version)
    if ch.status not in ("OPEN", "PREVIEWED"):
        raise CorrectionError("INVALID_TRANSITION", f"challenge is {ch.status}; cannot preview")
    plan = build_plan(db, ch)
    for old in db.execute(
        select(Correction).where(
            Correction.challenge_id == ch.id, Correction.status == "PREVIEW_READY"
        )
    ).scalars():
        old.status = "STALE"
        old.version += 1
    n_nights = len(plan["nights"])
    summary = (
        f"Correct {n_nights} night(s) to {plan['new_commission_bps']} bps from "
        f"{plan['effective_from']}: fee delta {plan['totals']['fee_delta_cents']} cents, "
        f"overpaid {plan['totals']['overpaid_cents']} cents, additional payout "
        f"{plan['totals']['additional_payout_cents']} cents."
    )
    corr = Correction(
        id=_new_id(),
        version=1,
        status="PREVIEW_READY",
        challenge_id=ch.id,
        festival_id=ch.festival_id,
        vendor_id=ch.vendor_id,
        preview_hash=plan_hash(plan),
        evidence_hash=ch.evidence_hash,
        required_capability=required_capability,
        summary=summary,
        plan=plan,
        progress=[],
        results=[],
        created_by=actor_id,
        correlation_id=ch.correlation_id,
        generated_at=datetime.now(UTC),
    )
    db.add(corr)
    ch.status = "PREVIEWED"
    ch.version += 1
    ch.correction_id = corr.id
    db.flush()
    _event(
        db,
        stream_id=f"correction:{corr.id}",
        event_type="correction.previewed",
        aggregate_id=corr.id,
        aggregate_version=corr.version,
        vendor_id=ch.vendor_id,
        actor_id=actor_id,
        capability=capability,
        payload={
            "status": corr.status,
            "challenge_id": ch.id,
            "preview_hash": corr.preview_hash,
            "summary": summary,
            "item_count": len(plan["items"]),
            "totals": plan["totals"],
        },
        lineage=[("correction", corr.id), ("challenge", ch.id)],
        correlation_id=ch.correlation_id,
        command_id=command_id,
    )
    return corr


# ---------------------------------------------------------------------------
# 3. Approve + execute
# ---------------------------------------------------------------------------


def _lock_correction(db: Session, correction_id: str) -> Correction:
    corr = db.execute(
        select(Correction)
        .where(Correction.id == correction_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if corr is None:
        raise NotFoundError("correction not found")
    return corr


def approve_correction(
    db: Session,
    *,
    correction_id: str,
    expected_version: int | None,
    preview_hash: str,
    evidence_hash: str,
    item_ids: list[str],
    note: str | None,
    actor_id: str,
    user_capabilities: set[str],
    command_id: str | None,
) -> Correction:
    corr = _lock_correction(db, correction_id)
    if expected_version is not None and expected_version != corr.version:
        raise VersionConflictError(expected_version, corr.version)
    if corr.status != "PREVIEW_READY":
        raise CorrectionError(
            "INVALID_TRANSITION", f"correction is {corr.status}; only PREVIEW_READY can be approved"
        )
    if corr.required_capability not in user_capabilities:
        raise CorrectionError(
            "FORBIDDEN",
            f"missing required capability: {corr.required_capability}",
            status=403,
        )
    if preview_hash != corr.preview_hash:
        raise CorrectionError("PREVIEW_CHANGED", "preview_hash does not match the stored plan")
    if evidence_hash != corr.evidence_hash:
        raise CorrectionError("EVIDENCE_CHANGED", "evidence_hash does not match the challenge")
    plan_ids = sorted(i["item_id"] for i in corr.plan["items"])
    if sorted(item_ids) != plan_ids:
        raise CorrectionError(
            "ITEMS_MISMATCH",
            "item_ids must list exactly the items of the previewed plan",
            details={"expected": plan_ids},
        )
    ch = _lock_challenge(db, corr.challenge_id)
    if ch.evidence_hash != corr.evidence_hash:
        raise CorrectionError("EVIDENCE_CHANGED", "the challenge evidence changed")
    _lock_vendor_payouts(db, ch.vendor_id)
    fresh = build_plan(db, ch)
    if plan_hash(fresh) != corr.preview_hash:
        corr.status = "STALE"
        corr.version += 1
        db.flush()
        _event(
            db,
            stream_id=f"correction:{corr.id}",
            event_type="correction.stale",
            aggregate_id=corr.id,
            aggregate_version=corr.version,
            vendor_id=ch.vendor_id,
            actor_id=actor_id,
            capability=corr.required_capability,
            payload={
                "status": corr.status,
                "reason": "facts changed between preview and approval; nothing applied",
                "previewed_hash": corr.preview_hash,
                "current_hash": plan_hash(fresh),
            },
            lineage=[("correction", corr.id), ("challenge", ch.id)],
            correlation_id=ch.correlation_id,
        )
        raise StalePlanError(
            "PREVIEW_CHANGED",
            "the affected records changed since the preview; request a new preview",
            details={"current_version": corr.version},
        )

    approval = CorrectionApproval(
        correction_id=corr.id,
        actor_id=actor_id,
        capability=corr.required_capability,
        correction_version=corr.version,
        preview_hash=corr.preview_hash,
        evidence_hash=corr.evidence_hash,
        item_ids=plan_ids,
        note=note,
    )
    db.add(approval)
    db.flush()
    corr.approval_id = approval.id
    corr.status = "IN_PROGRESS"
    corr.version += 1
    ch.status = "CORRECTION_APPROVED"
    ch.version += 1
    db.flush()
    _event(
        db,
        stream_id=f"correction:{corr.id}",
        event_type="correction.approved",
        aggregate_id=corr.id,
        aggregate_version=corr.version,
        vendor_id=ch.vendor_id,
        actor_id=actor_id,
        capability=corr.required_capability,
        payload={
            "status": corr.status,
            "approval_id": approval.id,
            "preview_hash": corr.preview_hash,
            "evidence_hash": corr.evidence_hash,
            "item_ids": plan_ids,
            "note": note,
        },
        lineage=[("correction", corr.id), ("challenge", ch.id)],
        correlation_id=ch.correlation_id,
        command_id=command_id,
    )
    _execute(db, corr, ch, actor_id=actor_id)
    return corr


def _step(corr: Correction, step_id: str, label: str, status: str, detail: str | None) -> None:
    corr.progress = [
        *[s for s in (corr.progress or []) if s["step_id"] != step_id],
        {"step_id": step_id, "label": label, "status": status, "detail": detail},
    ]


def _execute(db: Session, corr: Correction, ch: Challenge, *, actor_id: str) -> None:
    plan = corr.plan
    cap = corr.required_capability
    vendor = db.get(Vendor, ch.vendor_id)
    new_bps = int(plan["new_commission_bps"])
    effective_from = date.fromisoformat(plan["effective_from"]) if plan["effective_from"] else None
    results: list[dict] = []
    lineage_base = [("correction", corr.id), ("challenge", ch.id)]

    # -- policy: supersede with a new version carrying the corrected effect --
    new_policy = None
    old_policy = db.get(PolicyVersion, ch.policy_version_id) if ch.policy_version_id else None
    if old_policy is not None:
        status_before = old_policy.status
        old_policy.status = "SUPERSEDED"
        new_policy = PolicyVersion(
            policy_id=old_policy.policy_id,
            version=old_policy.version + 1,
            scope=old_policy.scope,
            predicates=old_policy.predicates,
            effect={**(old_policy.effect or {}), "commission_bps": new_bps},
            evidence_refs=list(ch.superseding_evidence),
            approver_ref=actor_id,
            valid_from=old_policy.valid_from,
            valid_to=old_policy.valid_to,
            status="ACTIVE",
            supersedes_version=old_policy.version,
            source_case_id=old_policy.source_case_id,
            approved_at=datetime.now(UTC),
        )
        db.add(new_policy)
        db.flush()
        _event(
            db,
            stream_id=f"policy:{old_policy.policy_id}",
            event_type="policy.superseded",
            aggregate_id=old_policy.id,
            aggregate_version=old_policy.version,
            vendor_id=vendor.id,
            actor_id=actor_id,
            capability=cap,
            payload={
                "policy_id": old_policy.policy_id,
                "superseded_version": old_policy.version,
                "status_before": status_before,
                "superseding_version_row_id": new_policy.id,
                "reason": f"approved correction {corr.id}",
            },
            lineage=[("policy_version", old_policy.id), ("policy_version", new_policy.id)]
            + lineage_base,
            correlation_id=ch.correlation_id,
        )
        _event(
            db,
            stream_id=f"policy:{new_policy.policy_id}",
            event_type="policy.approved",
            aggregate_id=new_policy.id,
            aggregate_version=1,
            vendor_id=vendor.id,
            actor_id=actor_id,
            capability=cap,
            payload={
                "policy_id": new_policy.policy_id,
                "version": new_policy.version,
                "supersedes_version": new_policy.supersedes_version,
                "scope": new_policy.scope,
                "effect": new_policy.effect,
                "evidence": ch.superseding_evidence,
                "approved_via_correction": corr.id,
            },
            lineage=[("policy_version", new_policy.id)] + lineage_base,
            correlation_id=ch.correlation_id,
        )
        results.append(
            {
                "item_id": f"POLICY:{old_policy.id}",
                "original": _ref("POLICY_VERSION", old_policy.id, old_policy.version),
                "corrected": [_ref("POLICY_VERSION", new_policy.id, new_policy.version)],
                "original_cents": None,
                "corrected_cents": None,
                "outcome": f"policy v{old_policy.version} SUPERSEDED by v{new_policy.version}",
            }
        )
    _step(corr, "policy", "Supersede the challenged rule", "DONE", None)

    # -- contract term --------------------------------------------------------
    old_term = db.get(VendorContractTerm, ch.contract_term_id) if ch.contract_term_id else None
    max_version = db.execute(
        select(VendorContractTerm.version)
        .where(VendorContractTerm.vendor_id == vendor.id)
        .order_by(VendorContractTerm.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    term_from = effective_from or (old_term.effective_from if old_term else date.today())
    new_term = VendorContractTerm(
        vendor_id=vendor.id,
        version=(max_version or 0) + 1,
        commission_bps=new_bps,
        minimum_fee_cents=old_term.minimum_fee_cents if old_term else None,
        processor_fee_deductible=old_term.processor_fee_deductible if old_term else False,
        effective_from=term_from,
        effective_to=old_term.effective_to if old_term else None,
        status="active",
        source_refs=list(ch.superseding_evidence),
        approved_by=actor_id,
    )
    db.add(new_term)
    if old_term is not None and old_term.status == "active":
        if term_from <= old_term.effective_from:
            old_term.status = "superseded"
        else:
            old_term.effective_to = term_from - timedelta(days=1)
    db.flush()
    _event(
        db,
        stream_id=f"vendor:{vendor.id}",
        event_type="contract_term.superseded",
        aggregate_id=new_term.id,
        aggregate_version=new_term.version,
        vendor_id=vendor.id,
        actor_id=actor_id,
        capability=cap,
        payload={
            "old_contract_term_id": old_term.id if old_term else None,
            "new_contract_term_id": new_term.id,
            "commission_bps": new_bps,
            "effective_from": term_from.isoformat(),
        },
        lineage=[
            ("vendor_contract_term", new_term.id),
            ("vendor_contract_term", old_term.id if old_term else None),
        ]
        + lineage_base,
        correlation_id=ch.correlation_id,
    )

    # -- per night ------------------------------------------------------------
    contingent: list[dict] = []
    nights_out: list[dict] = []
    for night in plan["nights"]:
        s = db.get(Settlement, night["settlement_id"])
        new_s = _write_correction_settlement(
            db, s, corr=corr, ch=ch, new_term=new_term, new_policy=new_policy, actor_id=actor_id
        )
        results.append(
            {
                "item_id": f"SETTLEMENT_REVISION:{s.id}",
                "original": _ref("SETTLEMENT", s.id, s.version),
                "corrected": [_ref("SETTLEMENT", new_s.id, new_s.version)],
                "original_cents": str(s.assessed_fee_cents),
                "corrected_cents": str(new_s.assessed_fee_cents),
                "outcome": "new linked settlement version written; original unchanged",
            }
        )
        n_out = _settle_payouts_for_night(
            db, corr=corr, ch=ch, night=night, new_s=new_s, actor_id=actor_id, results=results
        )
        if n_out.get("contingent"):
            contingent.append(n_out)
        nights_out.append(n_out)

        # journals: linked reversal + linked correction (never a rewrite)
        if night.get("journal_base_key"):
            base = night["journal_base_key"]
            reversal = accounting.post_cumulative(
                db,
                festival_id=ch.festival_id,
                base_key=base,
                business_date=_root(db, s).business_date,
                kind="COMMISSION_SETTLEMENT",
                lines=[],
                source_record_ids=[s.id, new_s.id],
                note=f"A09 reversal for correction {corr.id} (challenge {ch.id}).",
                actor_id=actor_id,
                actor_capability=cap,
            )
            correcting = accounting.post_settlement(
                db, settlement=new_s, actor_id=actor_id, actor_capability=cap
            )
            corrected_refs = []
            if reversal.entry is not None:
                corrected_refs.append(
                    _ref("JOURNAL_ENTRY", reversal.entry.id, None, reversal.entry.entry_key)
                )
            for r in correcting:
                if r.entry is not None:
                    corrected_refs.append(
                        _ref("JOURNAL_ENTRY", r.entry.id, None, r.entry.entry_key)
                    )
            fam_root = db.execute(
                select(JournalEntry).where(JournalEntry.entry_key == base)
            ).scalar_one()
            results.append(
                {
                    "item_id": f"POSTED_REVERSE_AND_CORRECT:{fam_root.id}",
                    "original": _ref("JOURNAL_ENTRY", fam_root.id, None, base),
                    "corrected": corrected_refs,
                    "original_cents": str(night["old_fee_cents"]),
                    "corrected_cents": str(new_s.assessed_fee_cents),
                    "outcome": "linked reversing + correcting entries appended",
                }
            )
    _step(corr, "settlements", "Write linked settlement versions", "DONE", None)
    _step(corr, "payouts", "Revise / cancel / recover payouts", "DONE", None)
    _step(corr, "journals", "Reverse and correct posted journals", "DONE", None)

    corr.results = results
    corr.plan = {**plan}  # unchanged; reassigned so JSONB change tracking is explicit
    _set_execution(corr, nights_out)
    if contingent:
        _step(
            corr,
            "contingent",
            "Reconcile pending cancellations",
            "PENDING",
            f"{len(contingent)} night(s) wait for the rail outcome of a cancel request",
        )
        db.flush()
        _event(
            db,
            stream_id=f"correction:{corr.id}",
            event_type="correction.in_progress",
            aggregate_id=corr.id,
            aggregate_version=corr.version,
            vendor_id=vendor.id,
            actor_id=actor_id,
            capability=cap,
            payload={"status": corr.status, "summary": "waiting on pending cancellations"},
            lineage=lineage_base,
            correlation_id=ch.correlation_id,
        )
        return
    _finish(db, corr, ch, actor_id=actor_id)


def _set_execution(corr: Correction, nights_out: list[dict]) -> None:
    corr.progress = [
        *[s for s in (corr.progress or []) if s["step_id"] != "_execution"],
        {
            "step_id": "_execution",
            "label": "Execution record (per night)",
            "status": "DONE",
            "detail": None,
            "nights": nights_out,
        },
    ]


def _execution(corr: Correction) -> list[dict]:
    for s in corr.progress or []:
        if s["step_id"] == "_execution":
            return s.get("nights", [])
    return []


def _write_correction_settlement(
    db: Session,
    s: Settlement,
    *,
    corr: Correction,
    ch: Challenge,
    new_term: VendorContractTerm,
    new_policy: PolicyVersion | None,
    actor_id: str,
) -> Settlement:
    """The plan's numbers were bound by the approval; they are recomputed
    here from the same pure function over the same live chain, and the
    approval already proved the chain had not changed."""
    live = _live_settlements(db, s.vendor_id, s.festival_id)
    periods = [
        dc.PeriodInput(
            settlement_id=x.id,
            business_date=x.business_date,
            centrally_held_cents=x.centrally_held_cents,
            vendor_retained_cash_cents=x.vendor_retained_cash_cents,
            tax_cents=x.tax_cents,
            tips_cents=x.tips_cents,
            processor_fee_assessed_cents=x.processor_fee_assessed_cents,
            processor_fee_deductible=x.processor_fee_deductible_used,
            is_final_period=x.is_final_period,
            minimum_fee_cents=x.minimum_fee_cents_used,
            commission_bps=x.commission_bps_used,
            assessed_fee_cents=x.assessed_fee_cents,
            payout_cents=x.payout_cents,
        )
        for x in live
    ]
    effective_from = (
        date.fromisoformat(corr.plan["effective_from"]) if corr.plan["effective_from"] else date.min
    )
    rec = {
        r.period.settlement_id: r
        for r in dc.recompute_vendor_chain(
            periods,
            new_commission_bps=int(corr.plan["new_commission_bps"]),
            effective_from=effective_from,
        )
    }
    # After earlier nights were corrected in this same execution, `live`
    # already contains their new versions; recomputation is idempotent on
    # them, so this night's cumulative prior is the corrected one.
    r = rec[s.id]
    res = r.result
    new_s = Settlement(
        festival_id=s.festival_id,
        vendor_id=s.vendor_id,
        business_date=s.business_date,
        run_id=s.run_id,
        calculation_version=CALCULATION_VERSION,
        vendor_contract_term_id=new_term.id,
        vendor_contract_term_version=new_term.version,
        commission_bps_used=r.commission_bps,
        minimum_fee_cents_used=s.minimum_fee_cents_used,
        processor_fee_deductible_used=s.processor_fee_deductible_used,
        is_final_period=s.is_final_period,
        eligible_sales_cents=res.eligible_sales_cents,
        centrally_held_cents=res.centrally_held_cents,
        vendor_retained_cash_cents=res.vendor_retained_cash_cents,
        tax_cents=res.tax_cents,
        tips_cents=res.tips_cents,
        cumulative_eligible_sales_cents=res.cumulative_eligible_sales_cents,
        cumulative_commission_assessed_prior_cents=res.cumulative_commission_assessed_prior_cents,
        assessed_fee_cents=res.fee_assessed_cents,
        collected_fee_cents=res.fee_collected_from_central_cents,
        vendor_fee_receivable_cents=res.vendor_fee_receivable_cents,
        unpaid_balance_cents=res.unpaid_balance_cents,
        processor_fee_assessed_cents=res.processor_fee_assessed_cents,
        processor_fee_collected_cents=res.processor_fee_collected_cents,
        payout_cents=res.vendor_payout_cents,
        minimum_applied=res.minimum_applied,
        holds=[],
        policy_refs=(
            [
                {
                    "policy_version_id": new_policy.id,
                    "policy_id": new_policy.policy_id,
                    "version": new_policy.version,
                }
            ]
            if new_policy
            else []
        ),
        source_manifest_hash=s.source_manifest_hash,
        correction_of=s.id,
        correction_reason=f"A09 correction {corr.id} of challenge {ch.id}"[:500],
        status="CORRECTION",
        version=s.version + 1,
    )
    db.add(new_s)
    db.flush()
    refs_by_type: dict[str, list] = {}
    for li in db.execute(
        select(SettlementLineItem).where(SettlementLineItem.settlement_id == s.id)
    ).scalars():
        refs_by_type.setdefault(li.line_type, li.source_refs or [])
    link = [{"ref_kind": "settlement", "ref_id": s.id, "role": "corrects"}]
    for line_type, amount in (
        ("eligible_sales", res.eligible_sales_cents),
        ("centrally_held", res.centrally_held_cents),
        ("vendor_retained_cash", res.vendor_retained_cash_cents),
        ("tax", res.tax_cents),
        ("tips", res.tips_cents),
        ("processor_fee_assessed", res.processor_fee_assessed_cents),
        ("processor_fee_collected", res.processor_fee_collected_cents),
        ("fee_assessed", res.fee_assessed_cents),
        ("fee_collected_from_central", res.fee_collected_from_central_cents),
        ("vendor_fee_receivable", res.vendor_fee_receivable_cents),
        ("unpaid_balance", res.unpaid_balance_cents),
        ("vendor_payout", res.vendor_payout_cents),
    ):
        db.add(
            SettlementLineItem(
                settlement_id=new_s.id,
                line_type=line_type,
                amount_cents=amount,
                source_refs=list(refs_by_type.get(line_type, [])) + link,
            )
        )
    db.flush()
    _event(
        db,
        stream_id=f"settlement:{s.vendor_id}:{s.festival_id}",
        event_type="settlement.corrected",
        aggregate_id=new_s.id,
        aggregate_version=new_s.version,
        vendor_id=s.vendor_id,
        actor_id=actor_id,
        capability=corr.required_capability,
        payload={
            "festival_id": s.festival_id,
            "business_date": s.business_date.isoformat(),
            "is_final_period": s.is_final_period,
            "calculation_version": CALCULATION_VERSION,
            "commission_bps_used": r.commission_bps,
            "assessed_fee_cents": res.fee_assessed_cents,
            "payout_cents": res.vendor_payout_cents,
            "unpaid_balance_cents": res.unpaid_balance_cents,
            "vendor_fee_receivable_cents": res.vendor_fee_receivable_cents,
            "is_correction": True,
            "corrected_settlement_id": s.id,
            "correction_id": corr.id,
            "challenge_id": ch.id,
        },
        lineage=[
            ("settlement", new_s.id),
            ("settlement", s.id),
            ("vendor_contract_term", new_term.id),
            ("policy_version", new_policy.id if new_policy else None),
            ("correction", corr.id),
            ("challenge", ch.id),
        ],
        correlation_id=ch.correlation_id,
    )
    return new_s


def _settle_payouts_for_night(
    db: Session,
    *,
    corr: Correction,
    ch: Challenge,
    night: dict,
    new_s: Settlement,
    actor_id: str,
    results: list[dict],
) -> dict:
    cap = corr.required_capability
    out = {
        "business_date": night["business_date"],
        "original_settlement_id": night["settlement_id"],
        "corrected_settlement_id": new_s.id,
        "contingent": False,
        "pending_payout_ids": [],
    }
    for pid in night.get("draft_payout_ids", []):
        cancel = payout_dispatch.cancel_payout(
            db, payout_id=pid, actor_id=actor_id, actor_capability=cap, expected_version=None
        )
        results.append(
            {
                "item_id": f"DRAFT_REVISE:{pid}",
                "original": _ref("PAYOUT", pid, cancel.payout.version),
                "corrected": [],
                "original_cents": str(cancel.payout.amount_cents),
                "corrected_cents": None,
                "outcome": f"cancelled ({cancel.result})",
            }
        )
    for pid in night.get("pending_payout_ids", []):
        current = db.get(Payout, pid)
        if current.state == "CANCEL_REQUESTED":
            # Already waiting on the rail; a second cancel is a no-op in A07
            # (and must not be called inside this transaction: it rolls back).
            results.append(
                {
                    "item_id": f"PENDING_CANCEL_REQUEST:{pid}",
                    "original": _ref("PAYOUT", pid, current.version),
                    "corrected": [],
                    "original_cents": str(current.amount_cents),
                    "corrected_cents": None,
                    "outcome": "cancel already requested; waiting for the rail outcome",
                }
            )
            out["contingent"] = True
            out["pending_payout_ids"].append(pid)
            continue
        cancel = payout_dispatch.cancel_payout(
            db, payout_id=pid, actor_id=actor_id, actor_capability=cap, expected_version=None
        )
        results.append(
            {
                "item_id": f"PENDING_CANCEL_REQUEST:{pid}",
                "original": _ref("PAYOUT", pid, cancel.payout.version),
                "corrected": [],
                "original_cents": str(cancel.payout.amount_cents),
                "corrected_cents": None,
                "outcome": f"{cancel.result} (state {cancel.payout.state})",
            }
        )
        if cancel.payout.state == "CANCEL_REQUESTED":
            out["contingent"] = True
            out["pending_payout_ids"].append(pid)
    if out["contingent"]:
        return out
    _settle_amounts(
        db, corr=corr, ch=ch, night=night, new_s=new_s, actor_id=actor_id, results=results
    )
    return out


def _settle_amounts(
    db: Session,
    *,
    corr: Correction,
    ch: Challenge,
    night: dict,
    new_s: Settlement,
    actor_id: str,
    results: list[dict],
) -> None:
    """With no payout of this night still pending at the rail: compare what
    actually moved with the corrected entitlement."""
    cap = corr.required_capability
    chain = _chain_ids(db, new_s)
    payouts = (
        db.execute(
            select(Payout)
            .where(Payout.settlement_id.in_(chain))
            .order_by(Payout.created_at, Payout.id)
        )
        .scalars()
        .all()
    )
    completed = [p for p in payouts if p.state in dc.COMPLETED_STATES]
    paid = sum(p.amount_cents for p in completed)
    add, rec = dc.paid_adjustment(paid_cents=paid, corrected_payout_cents=new_s.payout_cents)
    if paid == 0:
        if night.get("had_payout") and new_s.payout_cents > 0:
            payout, _created = payout_dispatch.create_payout_obligation(
                db, settlement_id=new_s.id, actor_id=actor_id, actor_capability=cap
            )
            results.append(
                {
                    "item_id": f"REPLACEMENT:{new_s.id}",
                    "original": _ref("SETTLEMENT", night["settlement_id"]),
                    "corrected": [
                        _ref("PAYOUT", payout.id, payout.version, "replacement obligation")
                    ],
                    "original_cents": str(night["old_payout_cents"]),
                    "corrected_cents": str(payout.amount_cents),
                    "outcome": "replacement obligation created (DRAFT, needs normal approval)",
                }
            )
        return
    last = completed[-1]
    if add > 0:
        key = f"payout-obligation:{new_s.id}:additional"
        payout = Payout(
            logical_obligation_id=key,
            idempotency_key=key,
            vendor_id=new_s.vendor_id,
            business_date=new_s.business_date,
            beneficiary_version=db.get(Vendor, new_s.vendor_id).version,
            amount_cents=add,
            state="DRAFT",
            settlement_id=new_s.id,
        )
        db.add(payout)
        db.flush()
        payout_dispatch._append_payout_event(
            db,
            payout,
            event_type="payout.created",
            actor_id=actor_id,
            actor_capability=cap,
            payload={
                "settlement_id": new_s.id,
                "vendor_id": new_s.vendor_id,
                "business_date": new_s.business_date.isoformat(),
                "amount_cents": add,
                "beneficiary_version": payout.beneficiary_version,
                "additional_after_correction": corr.id,
            },
        )
        results.append(
            {
                "item_id": f"COMPLETED_RECOVERY_OFFSET:{last.id}",
                "original": _ref("PAYOUT", last.id, last.version),
                "corrected": [_ref("PAYOUT", payout.id, payout.version, "additional obligation")],
                "original_cents": str(paid),
                "corrected_cents": str(new_s.payout_cents),
                "outcome": f"additional obligation {add} cents created (DRAFT)",
            }
        )
    elif rec > 0:
        recovery = open_recovery(
            db,
            recovery_key=f"correction:{corr.id}:{new_s.id}",
            festival_id=new_s.festival_id,
            vendor_id=new_s.vendor_id,
            origin_kind="correction",
            origin_id=corr.id,
            settlement_id=new_s.id,
            payout_id=last.id,
            business_date=new_s.business_date,
            amount_cents=rec,
            reason=(
                f"Correction {corr.id}: paid {paid} cents, corrected entitlement "
                f"{new_s.payout_cents} cents."
            ),
            actor_id=actor_id,
            capability=cap,
            correlation_id=ch.correlation_id,
        )
        results.append(
            {
                "item_id": f"COMPLETED_RECOVERY_OFFSET:{last.id}",
                "original": _ref("PAYOUT", last.id, last.version),
                "corrected": [_ref("PAYOUT", recovery.id, recovery.version, "vendor recovery")],
                "original_cents": str(paid),
                "corrected_cents": str(new_s.payout_cents),
                "outcome": f"vendor recovery {rec} cents OPEN (needs offset/request approval)",
            }
        )


def _finish(db: Session, corr: Correction, ch: Challenge, *, actor_id: str) -> None:
    cap = corr.required_capability
    released = release_holds(
        db,
        source_kind="challenge",
        source_id=ch.id,
        actor_id=actor_id,
        note=f"correction {corr.id} completed",
    )
    # statements: new versions from the corrected settlements
    for n in _execution(corr):
        bd = date.fromisoformat(n["business_date"])
        has_stmt = db.execute(
            select(VendorStatement.id).where(
                VendorStatement.vendor_id == ch.vendor_id, VendorStatement.business_date == bd
            )
        ).first()
        if not has_stmt:
            continue
        old_latest = (
            db.execute(
                select(VendorStatement)
                .where(
                    VendorStatement.vendor_id == ch.vendor_id, VendorStatement.business_date == bd
                )
                .order_by(VendorStatement.statement_version.desc())
            )
            .scalars()
            .first()
        )
        stmt, created = issue_vendor_statement(
            db,
            vendor_id=ch.vendor_id,
            business_date=bd,
            demo_effective_at=None,
            actor_id=actor_id,
            actor_capability=cap,
        )
        corr.results = [
            *corr.results,
            {
                "item_id": f"STATEMENT_REISSUE:{old_latest.id}",
                "original": _ref("REPORT", old_latest.id, old_latest.statement_version),
                "corrected": [_ref("REPORT", stmt.id, stmt.statement_version, stmt.status)],
                "original_cents": None,
                "corrected_cents": None,
                "outcome": "statement reissued" if created else "statement unchanged",
            },
        ]
    _step(corr, "statements", "Reissue vendor statements", "DONE", None)
    case = db.get(Case, ch.case_id) if ch.case_id else None
    if case is not None:
        case.challenge_status = "corrected"
        _event(
            db,
            stream_id=f"case:{case.id}",
            event_type="case.challenge_closed",
            aggregate_id=case.id,
            aggregate_version=case.version,
            vendor_id=ch.vendor_id,
            actor_id=actor_id,
            capability=cap,
            payload={"challenge_status": "corrected", "correction_id": corr.id},
            lineage=[("case", case.id), ("correction", corr.id), ("challenge", ch.id)],
            correlation_id=ch.correlation_id,
        )
    ch.status = "CORRECTED"
    ch.version += 1
    corr.status = "COMPLETED"
    corr.version += 1
    corr.completed_at = datetime.now(UTC)
    _step(corr, "holds", "Release challenge holds", "DONE", f"{len(released)} hold(s) released")
    db.flush()
    _event(
        db,
        stream_id=f"challenge:{ch.id}",
        event_type="challenge.corrected",
        aggregate_id=ch.id,
        aggregate_version=ch.version,
        vendor_id=ch.vendor_id,
        actor_id=actor_id,
        capability=cap,
        payload={"status": ch.status, "correction_id": corr.id, "reason": ch.reason},
        lineage=[("challenge", ch.id), ("correction", corr.id)],
        correlation_id=ch.correlation_id,
    )
    _event(
        db,
        stream_id=f"correction:{corr.id}",
        event_type="correction.completed",
        aggregate_id=corr.id,
        aggregate_version=corr.version,
        vendor_id=ch.vendor_id,
        actor_id=actor_id,
        capability=cap,
        payload={
            "status": corr.status,
            "summary": corr.summary,
            "results": len(corr.results),
        },
        lineage=[("correction", corr.id), ("challenge", ch.id)]
        + [
            (_RECORD_TO_REF.get(ref["kind"], ref["kind"].lower()), ref["id"])
            for r in corr.results
            for ref in r.get("corrected") or []
        ],
        correlation_id=ch.correlation_id,
    )


_RECORD_TO_REF = {
    "SETTLEMENT": "settlement",
    "PAYOUT": "payout",
    "JOURNAL_ENTRY": "journal_entry",
    "POLICY_VERSION": "policy_version",
    "REPORT": "vendor_statement",
}


def resolve_contingent(
    db: Session, *, correction_id: str, expected_version: int | None, actor_id: str
) -> Correction:
    corr = _lock_correction(db, correction_id)
    if expected_version is not None and expected_version != corr.version:
        raise VersionConflictError(expected_version, corr.version)
    if corr.status != "IN_PROGRESS":
        raise CorrectionError("INVALID_TRANSITION", f"correction is {corr.status}")
    ch = _lock_challenge(db, corr.challenge_id)
    _lock_vendor_payouts(db, ch.vendor_id)
    nights = _execution(corr)
    still = []
    for n in nights:
        if not n.get("contingent"):
            continue
        states = [db.get(Payout, pid).state for pid in n["pending_payout_ids"]]
        if any(st in dc.PENDING_STATES for st in states):
            still.append({"business_date": n["business_date"], "states": states})
    if still:
        raise CorrectionError(
            "STILL_PENDING",
            "a cancel request is still waiting for the rail outcome; reconcile the payout first",
            details={"pending": still},
        )
    results = list(corr.results)
    plan_nights = {pn["settlement_id"]: pn for pn in corr.plan["nights"]}
    for n in nights:
        if not n.get("contingent"):
            continue
        new_s = db.get(Settlement, n["corrected_settlement_id"])
        _settle_amounts(
            db,
            corr=corr,
            ch=ch,
            night=plan_nights[n["original_settlement_id"]],
            new_s=new_s,
            actor_id=actor_id,
            results=results,
        )
        n["contingent"] = False
        n["resolved_at"] = datetime.now(UTC).isoformat()
    corr.results = results
    _set_execution(corr, nights)
    _step(corr, "contingent", "Reconcile pending cancellations", "DONE", None)
    corr.version += 1
    db.flush()
    _finish(db, corr, ch, actor_id=actor_id)
    return corr


# ---------------------------------------------------------------------------
# Output shapes (lane B draft Challenge / Correction)
# ---------------------------------------------------------------------------


def challenge_out(c: Challenge) -> dict:
    return {
        "id": c.id,
        "version": c.version,
        "status": c.status,
        "festival_id": c.festival_id,
        "vendor_id": c.vendor_id,
        "target": {
            "kind": c.target_kind,
            "id": c.target_id,
            "version": c.target_version,
            "label": None,
        },
        "reason": c.reason,
        "superseding_evidence": [
            {
                "source_id": r["source_document_id"],
                "source_version": r["source_version"],
                "content_hash": f"sha256:{r['content_hash']}",
                "locator_kind": r.get("locator_kind"),
                "locator": r.get("locator"),
                "quoted_excerpt": r.get("quoted_excerpt"),
            }
            for r in c.superseding_evidence or []
        ],
        "evidence_hash": c.evidence_hash,
        "proposed_effect": c.proposed_effect,
        "effective_from": c.effective_from.isoformat() if c.effective_from else None,
        "policy_effect": c.policy_effect,
        "paused_work": c.paused_work,
        "policy_version_id": c.policy_version_id,
        "case_id": c.case_id,
        "contract_term_id": c.contract_term_id,
        "correction_id": c.correction_id,
        "created_by": c.created_by,
        "capability": c.capability,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "correlation_id": c.correlation_id,
    }


def correction_out(db: Session, c: Correction) -> dict:
    approval = db.get(CorrectionApproval, c.approval_id) if c.approval_id else None
    plan = c.plan or {}
    holds = (
        db.execute(
            select(WorkHold).where(
                WorkHold.source_kind == "challenge", WorkHold.source_id == c.challenge_id
            )
        )
        .scalars()
        .all()
    )
    ch = db.get(Challenge, c.challenge_id)
    return {
        "id": c.id,
        "version": c.version,
        "status": c.status,
        "challenge_id": c.challenge_id,
        "festival_id": c.festival_id,
        "vendor_id": c.vendor_id,
        "preview_hash": c.preview_hash,
        "evidence_hash": c.evidence_hash,
        "required_capability": c.required_capability,
        "summary": c.summary,
        "generated_at": c.generated_at.isoformat(),
        "policy_changes": plan.get("policy_changes", []),
        "contract_term_change": plan.get("contract_term_change"),
        "paused_work": ch.paused_work if ch else [],
        "items": plan.get("items", []),
        "nights": plan.get("nights", []),
        "usages": plan.get("usages", {}),
        "totals": plan.get("totals", {}),
        "approval": (
            {
                "approval_id": approval.id,
                "actor_id": approval.actor_id,
                "capability": approval.capability,
                "decided_at": approval.decided_at.isoformat(),
                "preview_hash": approval.preview_hash,
            }
            if approval
            else None
        ),
        "progress": [s for s in (c.progress or []) if s["step_id"] != "_execution"],
        "execution": _execution(c),
        "results": c.results or [],
        "holds": [
            {"hold_id": h.id, "status": h.status, "reason_code": h.reason_code} for h in holds
        ],
        "correlation_id": c.correlation_id,
        "completed_at": c.completed_at.isoformat() if c.completed_at else None,
    }


def active_challenge_for_vendor(db: Session, vendor_id: str) -> Challenge | None:
    hold = active_vendor_hold(db, vendor_id)
    if hold is None or hold.source_kind != "challenge":
        return None
    return db.get(Challenge, hold.source_id)
