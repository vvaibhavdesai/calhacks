"""X0.1 generic Approval mechanism: DB-touching orchestration.

The deterministic "which capability does this action need, and against
which subject_types is it legal" logic lives in `domain.approvals` -- a
pure, DB-free module, same convention as domain.policy (A05). Everything
in THIS module is the glue: validate against a real `Case` row when one is
cited, insert the append-only `Approval` row, and write the flight-recorder
event through the same `UnitOfWork` every other command in this codebase
uses (A02's "domain write + audit event" atomicity).

This is a NEW, PARALLEL mechanism (see `Approval`'s docstring in
app/models_ledger.py) -- it does not touch A05's `resolve_case`/
`approve_case_policy`, A08's `approve_variance`, or A07/A08's
`SupplierPayment`/`Payout` approval fields. Those three existing
hand-rolled call-sites keep working exactly as before.
"""

from __future__ import annotations

from datetime import UTC, datetime

from domain.approvals import required_capability_for_action, validate_subject_type_for_action
from domain.case_routing import SUBJECT_TYPES
from sqlalchemy.orm import Session

from app.models_ledger import Approval, Case
from app.uow import UnitOfWork


class ApprovalNotFoundError(LookupError):
    pass


class ApprovalConflictError(ValueError):
    def __init__(self, code: str, details: dict | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = details or {}


def record_approval(
    db: Session,
    uow: UnitOfWork,
    *,
    subject_type: str,
    subject_id: str,
    action: str,
    case_id: str | None,
    note: str | None,
    payload: dict | None,
    actor_id: str,
    actor_capability: str,
) -> Approval:
    """Records one already-granted decision. The caller (the router) is
    responsible for having already confirmed `actor_capability ==
    required_capability_for_action(action)` is actually held by the real
    authenticated user -- this function re-derives the required capability
    itself (never trusts a caller-supplied value) and stores THAT, so a
    caller cannot pass a broader capability than the action actually
    needed."""
    if subject_type not in SUBJECT_TYPES:
        raise ApprovalConflictError(
            "unknown_subject_type", {"subject_type": subject_type, "expected": list(SUBJECT_TYPES)}
        )
    try:
        required_capability = required_capability_for_action(action)
        validate_subject_type_for_action(action, subject_type)
    except ValueError as exc:
        raise ApprovalConflictError("invalid_action_or_subject_type", {"detail": str(exc)}) from exc

    case: Case | None = None
    if case_id is not None:
        case = db.get(Case, case_id)
        if case is None:
            raise ApprovalNotFoundError(f"case {case_id} not found")
        if case.subject_type != subject_type or case.subject_id != subject_id:
            raise ApprovalConflictError(
                "case_subject_mismatch",
                {
                    "case_subject_type": case.subject_type,
                    "case_subject_id": case.subject_id,
                    "requested_subject_type": subject_type,
                    "requested_subject_id": subject_id,
                },
            )

    approval = Approval(
        subject_type=subject_type,
        subject_id=subject_id,
        case_id=case_id,
        action=action,
        required_capability=required_capability,
        note=note,
        payload=payload or {},
        actor_id=actor_id,
        actor_capability=actor_capability,
    )
    db.add(approval)
    db.flush()

    lineage = [{"ref_kind": "approval", "ref_id": approval.id}]
    if case is not None:
        lineage.append({"ref_kind": "case", "ref_id": case.id})
    uow.append_event(
        stream_id=f"approval:{approval.id}",
        event_type="approval.recorded",
        aggregate_id=approval.id,
        aggregate_version=1,
        actor_id=actor_id,
        actor_type="human",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        payload={
            "subject_type": subject_type,
            "subject_id": subject_id,
            "action": action,
            "required_capability": required_capability,
            "case_id": case_id,
            "note": note,
        },
        lineage_refs=lineage,
    )
    return approval
