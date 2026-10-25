"""A09 command endpoints: challenges, correction preview/approval, vendor
recoveries, what-if branches and replays.

Commands take lane B's draft envelope {command_id, expected_version,
payload} plus an Idempotency-Key header (app/commands.py). Capabilities
are re-read from the database per request (app/auth.py); correction
approval checks the correction's own `required_capability`.
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.corrections as corrections
import app.recovery as recovery
import app.replay as replay
import app.whatif as whatif
from app.auth import get_current_user, get_user_capability_codes, require_capability
from app.commands import CommandEnvelope, error, remember, replayed, request_hash, require_key
from app.db import get_db
from app.models import (
    CAPABILITY_APPROVE_CORRECTION,
    CAPABILITY_CHALLENGE,
    CAPABILITY_CLOSE_BOOKS,
    CAPABILITY_FINANCE_LEAD,
    CAPABILITY_REPLAY,
    CAPABILITY_WHAT_IF,
    User,
)
from app.models_corrections import Challenge, Correction, Replay, VendorRecovery, WhatIfBranch

router = APIRouter(prefix="/api/v1", tags=["corrections"])


# -- Response models --------------------------------------------------------
#
# Two rules run through this router.
#
# MONEY IS A STRING here. A09 renders corrected amounts, deltas and every
# `totals` figure with `str(...)` so a cent is never handed to a reader as a
# float. The models below say `str`, not `int`: retyping them would change
# the JSON and break any screen that compares or displays the string.
#
# Instants are `str` too -- the handlers emit `.isoformat()`, and a
# `datetime` field would make pydantic rewrite the `+00:00` offset as `Z`.
#
# Every COMMAND below (the POSTs) returns a JSONResponse through
# `_run_command`, so FastAPI neither validates nor filters those bodies.
# Their models document the reply; they do not shape it.


class RecordRefOut(BaseModel):
    """Which record a row is about."""

    kind: str
    id: str | None
    version: int | None
    label: str | None


class EvidenceRefOut(BaseModel):
    """A superseding document, as the challenge reports it. Note the key is
    `source_id` here, not `source_document_id`."""

    source_id: str | None
    source_version: int | None
    # Prefixed "sha256:" by `challenge_out`.
    content_hash: str | None
    locator_kind: str | None
    locator: str | None
    quoted_excerpt: str | None


class ChallengeTargetOut(BaseModel):
    kind: str | None
    id: str | None
    version: int | None
    # Always null today; reserved for a human-readable target name.
    label: str | None


class PausedWorkOut(BaseModel):
    """Work a challenge paused. `hold_id` is only present for an entry that
    took an actual hold, which is why the reads here serialise with
    `response_model_exclude_unset`."""

    record: dict
    detail: str
    hold_id: str | None = None


class ChallengeOut(BaseModel):
    id: str
    version: int
    status: str
    festival_id: str | None
    vendor_id: str | None
    target: ChallengeTargetOut
    reason: str
    superseding_evidence: list[EvidenceRefOut]
    evidence_hash: str | None
    # The change the challenger proposes; its keys are whatever the proposal
    # states (e.g. `commission_bps`), so it stays an open object.
    proposed_effect: dict | None
    effective_from: str | None
    # What raising the challenge did to the policy that was applied.
    policy_effect: dict | None
    paused_work: list[PausedWorkOut]
    policy_version_id: str | None
    case_id: str | None
    contract_term_id: str | None
    correction_id: str | None
    created_by: str | None
    capability: str | None
    created_at: str | None
    correlation_id: str | None


class ChallengeListOut(BaseModel):
    items: list[ChallengeOut]
    # Always null: challenges are a page, not a stream.
    next_cursor: str | None


class CorrectionItemOut(BaseModel):
    """One line of a correction plan. The three amounts are STRINGS."""

    item_id: str
    description: str
    record: dict
    original_cents: str
    corrected_cents: str
    delta_cents: str
    amount_label: str
    treatment: str
    state_at_preview: str
    state_group: str
    requires_human: bool
    # Set when this item only applies if another one does.
    contingent_on: str | None


class CorrectionApprovalOut(BaseModel):
    approval_id: str
    actor_id: str | None
    capability: str | None
    decided_at: str
    preview_hash: str | None


class CorrectionHoldOut(BaseModel):
    hold_id: str
    status: str
    reason_code: str


class CorrectionOut(BaseModel):
    id: str
    version: int
    status: str
    challenge_id: str | None
    festival_id: str | None
    vendor_id: str | None
    preview_hash: str | None
    evidence_hash: str | None
    required_capability: str | None
    summary: str | None
    generated_at: str
    # Taken from the stored plan. `policy_changes`, `nights` and `usages`
    # keep their own shapes: each is the plan's own working, and a model over
    # them would drop whatever a later plan adds.
    policy_changes: list[dict]
    contract_term_change: dict | None
    paused_work: list[PausedWorkOut]
    items: list[CorrectionItemOut]
    nights: list[dict]
    # What already used the figures being corrected, per record kind.
    usages: dict
    # Every figure here is a STRING.
    totals: dict[str, str]
    # Null until a human has approved.
    approval: CorrectionApprovalOut | None
    progress: list[dict]
    # The per-night execution log, lifted out of `progress`.
    execution: list[dict]
    results: list[dict]
    holds: list[CorrectionHoldOut]
    correlation_id: str | None
    completed_at: str | None


class CorrectionListOut(BaseModel):
    items: list[CorrectionOut]
    next_cursor: str | None


class RecoveryActionOut(BaseModel):
    id: str
    action: str
    amount_cents: int
    payout_id: str | None
    journal_entry_id: str | None
    actor_id: str | None
    capability: str | None
    note: str | None
    at: str | None


class RecoveryOut(BaseModel):
    id: str
    version: int
    status: str
    festival_id: str | None
    vendor_id: str
    origin_kind: str
    origin_id: str
    settlement_id: str | None
    payout_id: str | None
    business_date: str | None
    # These five are ints: a recovery carries raw cents, unlike a correction
    # plan, which renders them.
    amount_cents: int
    offset_cents: int
    received_cents: int
    waived_cents: int
    remaining_cents: int
    reason: str | None
    # Constant: no real money is ever collected by this system.
    label: str
    actions: list[RecoveryActionOut]


class RecoveryListOut(BaseModel):
    items: list[RecoveryOut]
    next_cursor: str | None


class BranchComparisonOut(BaseModel):
    """Production vs branch for one measure. All three amounts are STRINGS."""

    record: dict
    label: str
    measure: str  # payout | commission
    production_cents: str
    branch_cents: str
    delta_cents: str


class BranchPromotionOut(BaseModel):
    # Always false: a branch is never merged or applied.
    allowed: bool
    how: str


class BranchOut(BaseModel):
    id: str
    version: int
    status: str
    label: str
    festival_id: str
    base_knowledge_cutoff: str
    business_date: str | None
    overrides: list[dict]
    # The branch's proof that it touched nothing in production.
    isolation: dict
    comparison: list[BranchComparisonOut]
    # Six STRING figures: production/branch payout and fee, plus both deltas.
    totals: dict[str, str]
    summary: dict
    note: str | None
    promotion: BranchPromotionOut
    created_by: str | None
    created_at: str | None


class BranchListOut(BaseModel):
    items: list[BranchOut]
    next_cursor: str | None


class ReplayOut(BaseModel):
    id: str
    version: int
    status: str
    mode: str
    # Lifted out of `scope`, which also carries festival_id and vendor_id.
    run_id: str | None
    scope: dict
    from_sequence: int | None
    to_sequence: int | None
    # Counts of side effects the replay guard refused; all zero on a clean
    # replay, which is the point of reporting them.
    effects: dict
    processed: int
    total: int
    matching: int
    divergences: list[dict]
    # What the replay compared and how much of it, plus the before/after
    # production row counts that prove nothing was written.
    checks: dict
    label: str
    created_by: str | None
    created_at: str | None
    completed_at: str | None


class ReplayListOut(BaseModel):
    items: list[ReplayOut]
    next_cursor: str | None


# -- Command replies --------------------------------------------------------
# All of these come back as a JSONResponse, so they are documentation.


class ChallengeCommandOut(BaseModel):
    command_id: str
    aggregate_id: str
    version: int
    # The challenge's own status, not a command status.
    status: str
    resource: ChallengeOut


class CorrectionCommandOut(BaseModel):
    command_id: str
    aggregate_id: str
    version: int
    status: str
    resource: CorrectionOut


class CorrectionApproveOut(BaseModel):
    """Approval is a long job (sent 202), so it carries the C03 job envelope
    AND the correction. `status` here is the JOB's status; the correction's
    own status is under `correction_status` -- two different things that
    would otherwise collide on one name."""

    job_id: str
    status_url: str
    status: str  # SUCCEEDED | RUNNING
    command_id: str
    aggregate_id: str
    version: int
    correction_status: str
    resource: CorrectionOut


class RecoveryCommandOut(BaseModel):
    command_id: str
    aggregate_id: str
    version: int
    status: str
    resource: RecoveryOut


class BranchCommandOut(BaseModel):
    command_id: str
    aggregate_id: str
    version: int
    status: str
    resource: BranchOut


class ReplayCommandOut(BaseModel):
    command_id: str
    aggregate_id: str
    version: int
    status: str
    resource: ReplayOut


def _domain_error(exc: Exception) -> HTTPException:
    if isinstance(exc, corrections.NotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, corrections.VersionConflictError):
        return error(
            409,
            "VERSION_CONFLICT",
            f"expected version {exc.expected}, current {exc.current}",
            current_version=exc.current,
        )
    if isinstance(exc, corrections.CorrectionError):
        return error(exc.status, exc.code, exc.message, **exc.details)
    if isinstance(exc, recovery.RecoveryError):
        return error(exc.status, exc.code, exc.message)
    if isinstance(exc, whatif.BranchError):
        return error(422, exc.code, exc.message)
    raise exc


_DOMAIN = (
    corrections.NotFoundError,
    corrections.VersionConflictError,
    corrections.CorrectionError,
    recovery.RecoveryError,
    whatif.BranchError,
)


def _run_command(
    db: Session,
    *,
    user: User,
    action: str,
    key: str | None,
    env: CommandEnvelope,
    path: dict,
    fn,
    status_code: int = 200,
):
    """Idempotency + one commit point around `fn() -> dict`."""
    key = require_key(key)
    rh = request_hash(env, **path)
    prior = replayed(db, user_id=user.id, action=action, key=key, req_hash=rh)
    if prior is not None:
        return prior
    try:
        body, status_code = fn()
    except corrections.StalePlanError as exc:
        db.commit()  # persist the STALE marking + its audit event
        raise _domain_error(exc) from exc
    except _DOMAIN as exc:
        db.rollback()
        raise _domain_error(exc) from exc
    remember(
        db,
        user_id=user.id,
        action=action,
        key=key,
        req_hash=rh,
        status_code=status_code,
        response=body,
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        prior = replayed(db, user_id=user.id, action=action, key=key, req_hash=rh)
        if prior is not None:
            return prior
        raise
    return JSONResponse(status_code=status_code, content=body)


def _payload_action(env: CommandEnvelope, expected: str) -> dict:
    p = env.payload
    if p.get("action") != expected:
        raise error(
            422,
            "INVALID_ACTION",
            f"payload.action must be {expected}",
            field_errors=[{"field": "payload.action", "message": f"expected {expected}"}],
        )
    for forbidden in ("actor_id", "actor", "approved_by", "promote_to_production"):
        if forbidden in p:
            raise error(
                422,
                "FORBIDDEN_FIELD",
                f"payload.{forbidden} is not accepted (actor comes from the session; "
                "branches are never promoted)",
                field_errors=[{"field": f"payload.{forbidden}", "message": "not allowed"}],
            )
    return p


def _date(v, field: str) -> date | None:
    if v in (None, ""):
        return None
    try:
        return date.fromisoformat(v)
    except (TypeError, ValueError) as exc:
        raise error(422, "INVALID_DATE", f"{field} must be YYYY-MM-DD") from exc


# ---------------------------------------------------------------------------
# Challenges
# ---------------------------------------------------------------------------


@router.get("/challenges", response_model=ChallengeListOut, response_model_exclude_unset=True)
def list_challenges(
    target_id: str | None = None,
    vendor_id: str | None = None,
    festival_id: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    q = select(Challenge)
    if target_id:
        q = q.where(Challenge.target_id == target_id)
    if vendor_id:
        q = q.where(Challenge.vendor_id == vendor_id)
    if festival_id:
        q = q.where(Challenge.festival_id == festival_id)
    if status:
        q = q.where(Challenge.status == status)
    rows = db.execute(q.order_by(Challenge.created_at.desc())).scalars()
    return {"items": [corrections.challenge_out(c) for c in rows], "next_cursor": None}


@router.get(
    "/challenges/{challenge_id}", response_model=ChallengeOut, response_model_exclude_unset=True
)
def get_challenge(
    challenge_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    c = db.get(Challenge, challenge_id)
    if c is None:
        raise HTTPException(status_code=404, detail="challenge not found")
    return corrections.challenge_out(c)


@router.post("/challenges", response_model=ChallengeCommandOut)
def create_challenge(
    env: CommandEnvelope,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CHALLENGE)),
):
    p = _payload_action(env, "CHALLENGE")
    target = p.get("target") or {}

    def fn():
        c = corrections.create_challenge(
            db,
            target_kind=target.get("kind"),
            target_id=target.get("id"),
            expected_version=(
                env.expected_version if env.expected_version is not None else target.get("version")
            ),
            evidence=p.get("superseding_evidence") or [],
            reason=p.get("reason") or "",
            proposed_effect=p.get("proposed_effect"),
            effective_from=_date(p.get("effective_from"), "effective_from"),
            actor_id=user.id,
            capability=CAPABILITY_CHALLENGE,
            command_id=env.command_id,
        )
        out = corrections.challenge_out(c)
        return {
            "command_id": env.command_id,
            "aggregate_id": c.id,
            "version": c.version,
            "status": c.status,
            "resource": out,
        }, 200

    return _run_command(
        db, user=user, action="challenge.create", key=idempotency_key, env=env, path={}, fn=fn
    )


@router.post("/challenges/{challenge_id}/withdraw", response_model=ChallengeCommandOut)
def withdraw_challenge(
    challenge_id: str,
    env: CommandEnvelope,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CHALLENGE)),
):
    _payload_action(env, "WITHDRAW_CHALLENGE")

    def fn():
        c = corrections.withdraw_challenge(
            db,
            challenge_id=challenge_id,
            expected_version=env.expected_version,
            actor_id=user.id,
            capability=CAPABILITY_CHALLENGE,
        )
        return {
            "command_id": env.command_id,
            "aggregate_id": c.id,
            "version": c.version,
            "status": c.status,
            "resource": corrections.challenge_out(c),
        }, 200

    return _run_command(
        db,
        user=user,
        action="challenge.withdraw",
        key=idempotency_key,
        env=env,
        path={"id": challenge_id},
        fn=fn,
    )


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


@router.post("/corrections/preview", response_model=CorrectionCommandOut)
def preview_correction(
    env: CommandEnvelope,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CHALLENGE)),
):
    p = _payload_action(env, "PREVIEW_CORRECTION")

    def fn():
        c = corrections.preview_correction(
            db,
            challenge_id=p.get("challenge_id"),
            expected_version=env.expected_version,
            actor_id=user.id,
            capability=CAPABILITY_CHALLENGE,
            command_id=env.command_id,
            required_capability=CAPABILITY_APPROVE_CORRECTION,
        )
        return {
            "command_id": env.command_id,
            "aggregate_id": c.id,
            "version": c.version,
            "status": c.status,
            "resource": corrections.correction_out(db, c),
        }, 200

    return _run_command(
        db, user=user, action="correction.preview", key=idempotency_key, env=env, path={}, fn=fn
    )


@router.get("/corrections", response_model=CorrectionListOut, response_model_exclude_unset=True)
def list_corrections(
    challenge_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    q = select(Correction)
    if challenge_id:
        q = q.where(Correction.challenge_id == challenge_id)
    rows = db.execute(q.order_by(Correction.created_at.desc())).scalars()
    return {"items": [corrections.correction_out(db, c) for c in rows], "next_cursor": None}


@router.get(
    "/corrections/{correction_id}", response_model=CorrectionOut, response_model_exclude_unset=True
)
def get_correction(
    correction_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    c = db.get(Correction, correction_id)
    if c is None:
        raise HTTPException(status_code=404, detail="correction not found")
    return corrections.correction_out(db, c)


@router.post(
    "/corrections/{correction_id}/approve",
    response_model=CorrectionApproveOut,
    responses={202: {"model": CorrectionApproveOut}},
)
def approve_correction(
    correction_id: str,
    env: CommandEnvelope,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    p = _payload_action(env, "APPROVE_CORRECTION")
    if p.get("correction_id") not in (None, correction_id):
        raise error(422, "TARGET_MISMATCH", "payload.correction_id does not match the path")

    def fn():
        c = corrections.approve_correction(
            db,
            correction_id=correction_id,
            expected_version=env.expected_version,
            preview_hash=p.get("preview_hash") or "",
            evidence_hash=p.get("evidence_hash") or "",
            item_ids=list(p.get("item_ids") or []),
            note=p.get("note"),
            actor_id=user.id,
            user_capabilities=get_user_capability_codes(db, user.id),
            command_id=env.command_id,
        )
        return {
            "job_id": c.id,
            "status_url": f"/api/v1/corrections/{c.id}",
            "status": "SUCCEEDED" if c.status == "COMPLETED" else "RUNNING",
            "command_id": env.command_id,
            "aggregate_id": c.id,
            "version": c.version,
            "correction_status": c.status,
            "resource": corrections.correction_out(db, c),
        }, 202

    return _run_command(
        db,
        user=user,
        action="correction.approve",
        key=idempotency_key,
        env=env,
        path={"id": correction_id},
        fn=fn,
    )


@router.post("/corrections/{correction_id}/resolve-contingent", response_model=CorrectionCommandOut)
def resolve_contingent(
    correction_id: str,
    env: CommandEnvelope,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_APPROVE_CORRECTION)),
):
    _payload_action(env, "RESOLVE_CONTINGENT")

    def fn():
        c = corrections.resolve_contingent(
            db, correction_id=correction_id, expected_version=env.expected_version, actor_id=user.id
        )
        return {
            "command_id": env.command_id,
            "aggregate_id": c.id,
            "version": c.version,
            "status": c.status,
            "resource": corrections.correction_out(db, c),
        }, 200

    return _run_command(
        db,
        user=user,
        action="correction.resolve_contingent",
        key=idempotency_key,
        env=env,
        path={"id": correction_id},
        fn=fn,
    )


# ---------------------------------------------------------------------------
# Vendor recoveries
# ---------------------------------------------------------------------------


@router.get("/recoveries", response_model=RecoveryListOut)
def list_recoveries(
    festival_id: str | None = None,
    vendor_id: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    q = select(VendorRecovery)
    if festival_id:
        q = q.where(VendorRecovery.festival_id == festival_id)
    if vendor_id:
        q = q.where(VendorRecovery.vendor_id == vendor_id)
    if status:
        q = q.where(VendorRecovery.status == status)
    rows = db.execute(q.order_by(VendorRecovery.created_at)).scalars()
    return {"items": [recovery.recovery_out(db, r) for r in rows], "next_cursor": None}


@router.get("/recoveries/{recovery_id}", response_model=RecoveryOut)
def get_recovery(
    recovery_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> dict:
    r = db.get(VendorRecovery, recovery_id)
    if r is None:
        raise HTTPException(status_code=404, detail="recovery not found")
    return recovery.recovery_out(db, r)


def _recovery_result(env, r, db, code=200):
    return {
        "command_id": env.command_id,
        "aggregate_id": r.id,
        "version": r.version,
        "status": r.status,
        "resource": recovery.recovery_out(db, r),
    }, code


@router.post("/recoveries", response_model=RecoveryCommandOut)
def open_recovery(
    env: CommandEnvelope,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_APPROVE_CORRECTION)),
):
    p = _payload_action(env, "OPEN_RECOVERY_FROM_SETTLEMENT")

    def fn():
        r = recovery.open_from_settlement_correction(
            db,
            settlement_id=p.get("settlement_id"),
            actor_id=user.id,
            capability=CAPABILITY_APPROVE_CORRECTION,
        )
        return _recovery_result(env, r, db)

    return _run_command(
        db, user=user, action="recovery.open", key=idempotency_key, env=env, path={}, fn=fn
    )


@router.post("/recoveries/{recovery_id}/approve", response_model=RecoveryCommandOut)
def approve_recovery(
    recovery_id: str,
    env: CommandEnvelope,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_APPROVE_CORRECTION)),
):
    p = _payload_action(env, "APPROVE_RECOVERY")

    def fn():
        r = recovery.approve_recovery(
            db,
            recovery_id=recovery_id,
            expected_version=env.expected_version,
            mode=p.get("mode"),
            target_payout_id=p.get("target_payout_id"),
            note=p.get("note"),
            actor_id=user.id,
            capability=CAPABILITY_APPROVE_CORRECTION,
        )
        return _recovery_result(env, r, db)

    return _run_command(
        db,
        user=user,
        action="recovery.approve",
        key=idempotency_key,
        env=env,
        path={"id": recovery_id},
        fn=fn,
    )


@router.post("/recoveries/{recovery_id}/receipts", response_model=RecoveryCommandOut)
def record_receipt(
    recovery_id: str,
    env: CommandEnvelope,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CLOSE_BOOKS)),
):
    p = _payload_action(env, "RECORD_RECOVERY_RECEIPT")

    def fn():
        r = recovery.record_receipt(
            db,
            recovery_id=recovery_id,
            expected_version=env.expected_version,
            amount_cents=int(p.get("amount_cents") or 0),
            business_date=_date(p.get("business_date"), "business_date"),
            actor_id=user.id,
            capability=CAPABILITY_CLOSE_BOOKS,
        )
        return _recovery_result(env, r, db)

    return _run_command(
        db,
        user=user,
        action="recovery.receipt",
        key=idempotency_key,
        env=env,
        path={"id": recovery_id},
        fn=fn,
    )


@router.post("/recoveries/{recovery_id}/waive", response_model=RecoveryCommandOut)
def waive_recovery(
    recovery_id: str,
    env: CommandEnvelope,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_FINANCE_LEAD)),
):
    p = _payload_action(env, "WAIVE_RECOVERY")

    def fn():
        r = recovery.waive(
            db,
            recovery_id=recovery_id,
            expected_version=env.expected_version,
            note=p.get("note") or "",
            business_date=_date(p.get("business_date"), "business_date"),
            actor_id=user.id,
            capability=CAPABILITY_FINANCE_LEAD,
        )
        return _recovery_result(env, r, db)

    return _run_command(
        db,
        user=user,
        action="recovery.waive",
        key=idempotency_key,
        env=env,
        path={"id": recovery_id},
        fn=fn,
    )


# ---------------------------------------------------------------------------
# What-if branches
# ---------------------------------------------------------------------------


@router.get("/branches", response_model=BranchListOut)
def list_branches(
    festival_id: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_WHAT_IF)),
) -> dict:
    q = select(WhatIfBranch)
    if festival_id:
        q = q.where(WhatIfBranch.festival_id == festival_id)
    rows = db.execute(q.order_by(WhatIfBranch.created_at.desc())).scalars()
    return {"items": [whatif.branch_out(db, b) for b in rows], "next_cursor": None}


@router.get("/branches/{branch_id}", response_model=BranchOut)
def get_branch(
    branch_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_WHAT_IF)),
) -> dict:
    b = db.get(WhatIfBranch, branch_id)
    if b is None:
        raise HTTPException(status_code=404, detail="branch not found")
    return whatif.branch_out(db, b)


@router.post("/branches", response_model=BranchCommandOut)
def create_branch(
    env: CommandEnvelope,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_WHAT_IF)),
):
    p = _payload_action(env, "CREATE_WHAT_IF_BRANCH")
    try:
        cutoff = datetime.fromisoformat(str(p.get("base_knowledge_cutoff")).replace("Z", "+00:00"))
    except ValueError as exc:
        raise error(
            422, "INVALID_CUTOFF", "base_knowledge_cutoff must be an RFC3339 instant"
        ) from exc
    if cutoff.tzinfo is None:
        raise error(422, "TIMEZONE_REQUIRED", "base_knowledge_cutoff needs an offset")
    festival_id = p.get("festival_id")
    if not festival_id:
        raise error(422, "FESTIVAL_REQUIRED", "payload.festival_id is required")

    def fn():
        b = whatif.create_branch(
            db,
            festival_id=festival_id,
            label=p.get("label") or "",
            base_knowledge_cutoff=cutoff,
            business_date=_date(p.get("business_date"), "business_date"),
            overrides=list(p.get("overrides") or []),
            actor_id=user.id,
            capability=CAPABILITY_WHAT_IF,
        )
        return {
            "command_id": env.command_id,
            "aggregate_id": b.id,
            "version": b.version,
            "status": b.status,
            "resource": whatif.branch_out(db, b),
        }, 200

    return _run_command(
        db, user=user, action="branch.create", key=idempotency_key, env=env, path={}, fn=fn
    )


# ---------------------------------------------------------------------------
# Replays
# ---------------------------------------------------------------------------


@router.get("/replays", response_model=ReplayListOut)
def list_replays(
    db: Session = Depends(get_db), user: User = Depends(require_capability(CAPABILITY_REPLAY))
) -> dict:
    rows = db.execute(select(Replay).order_by(Replay.created_at.desc())).scalars()
    return {"items": [replay.replay_out(r) for r in rows], "next_cursor": None}


@router.get("/replays/{replay_id}", response_model=ReplayOut)
def get_replay(
    replay_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_REPLAY)),
) -> dict:
    r = db.get(Replay, replay_id)
    if r is None:
        raise HTTPException(status_code=404, detail="replay not found")
    return replay.replay_out(r)


@router.post("/replays", response_model=ReplayCommandOut)
def start_replay(
    env: CommandEnvelope,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_REPLAY)),
):
    p = _payload_action(env, "START_REPLAY")
    if p.get("mode") != "RECORDED_RESULTS_ONLY":
        raise error(422, "INVALID_MODE", "mode must be RECORDED_RESULTS_ONLY")
    if not p.get("festival_id"):
        raise error(422, "FESTIVAL_REQUIRED", "payload.festival_id is required")

    def fn():
        r = replay.run_replay(
            db,
            festival_id=p["festival_id"],
            vendor_id=p.get("vendor_id"),
            run_id=p.get("run_id"),
            actor_id=user.id,
            capability=CAPABILITY_REPLAY,
        )
        return {
            "command_id": env.command_id,
            "aggregate_id": r.id,
            "version": r.version,
            "status": r.status,
            "resource": replay.replay_out(r),
        }, 200

    return _run_command(
        db, user=user, action="replay.start", key=idempotency_key, env=env, path={}, fn=fn
    )
