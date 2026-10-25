"""A09 vendor recovery: money a vendor owes back after it was already paid.

Origins: an approved A09 correction that lowered a paid entitlement, A04's
own correction path (a late refund recalculated after a confirmed payout:
the correction settlement's `overpayment_recovery` line), or a payout that
won a cancel race.

Never a negative payout, never taken from tax/tips, never automatic. A
recovery opens in OPEN and places a vendor hold, so the vendor's next
payout cannot be released before a human decides how to recover:

  - OFFSET (approve_correction): reduce one of the vendor's DRAFT payouts
    by up to the remaining amount. The payout row's amount changes while it
    is still a draft (nothing was approved or reserved against it yet);
    the vendor-payable account nets out by itself when that smaller payout
    is confirmed, so no extra journal entry is needed.
  - REQUEST (approve_correction): ask the vendor to pay it back; the
    recovery stays REQUESTED until receipts cover it.
  - record_receipt (close_books): money received from the vendor,
    Dr bank / Cr vendor funds payable (A08 posting map).
  - waive (finance_lead): Dr operating expense / Cr vendor funds payable.
    DRAFT accounting choice, documented, not an approved policy.

Every decision is an append-only `recovery_actions` row plus an audit event.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from domain import corrections as dc
from sqlalchemy import select
from sqlalchemy.orm import Session

import app.accounting as accounting
from app.holds import place_hold, release_holds
from app.models_corrections import RecoveryAction, VendorRecovery
from app.models_ledger import Payout, Vendor
from app.models_settlement import Settlement, SettlementLineItem
from app.uow import UnitOfWork

HOLD_REASON = "vendor_recovery_pending_decision"


class RecoveryError(ValueError):
    def __init__(self, code: str, message: str, *, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _event(db, r: VendorRecovery, event_type: str, *, actor_id, capability, payload, extra=None):
    UnitOfWork(db).append_event(
        stream_id=f"recovery:{r.id}",
        event_type=event_type,
        aggregate_id=r.id,
        aggregate_version=r.version,
        actor_id=actor_id,
        actor_type="human" if actor_id else "system",
        actor_capability=capability,
        occurred_at=datetime.now(UTC),
        vendor_id=r.vendor_id,
        payload={
            "status": r.status,
            "amount_cents": r.amount_cents,
            "remaining_cents": r.remaining_cents,
            "business_date": r.business_date.isoformat() if r.business_date else None,
            "origin_kind": r.origin_kind,
            "origin_id": r.origin_id,
            **payload,
        },
        lineage_refs=[{"ref_kind": "recovery", "ref_id": r.id}]
        + ([{"ref_kind": "payout", "ref_id": r.payout_id}] if r.payout_id else [])
        + ([{"ref_kind": "settlement", "ref_id": r.settlement_id}] if r.settlement_id else [])
        + (extra or []),
    )


def open_recovery(
    db: Session,
    *,
    recovery_key: str,
    festival_id: str,
    vendor_id: str,
    origin_kind: str,
    origin_id: str,
    settlement_id: str | None,
    payout_id: str | None,
    business_date: date | None,
    amount_cents: int,
    reason: str,
    actor_id: str | None,
    capability: str | None,
    correlation_id: str | None = None,
) -> VendorRecovery:
    if amount_cents <= 0:
        raise RecoveryError("NOTHING_TO_RECOVER", "recovery amount must be positive")
    existing = db.execute(
        select(VendorRecovery).where(VendorRecovery.recovery_key == recovery_key)
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    r = VendorRecovery(
        recovery_key=recovery_key,
        festival_id=festival_id,
        vendor_id=vendor_id,
        origin_kind=origin_kind,
        origin_id=origin_id,
        settlement_id=settlement_id,
        payout_id=payout_id,
        business_date=business_date,
        amount_cents=amount_cents,
        status="OPEN",
        reason=reason,
        created_by=actor_id,
    )
    db.add(r)
    db.flush()
    place_hold(
        db,
        festival_id=festival_id,
        vendor_id=vendor_id,
        record_kind="VENDOR",
        record_id=vendor_id,
        reason_code=HOLD_REASON,
        source_kind="recovery",
        source_id=r.id,
        actor_id=actor_id,
    )
    db.add(
        RecoveryAction(
            recovery_id=r.id,
            action="OPENED",
            amount_cents=amount_cents,
            payout_id=payout_id,
            actor_id=actor_id,
            capability=capability,
            note=reason,
        )
    )
    db.flush()
    _event(
        db,
        r,
        "recovery.opened",
        actor_id=actor_id,
        capability=capability,
        payload={"reason": reason, "correlation_id": correlation_id},
        extra=[{"ref_kind": origin_kind, "ref_id": origin_id}],
    )
    return r


def open_from_settlement_correction(
    db: Session, *, settlement_id: str, actor_id: str, capability: str
) -> VendorRecovery:
    """A04's late-refund path: a CORRECTION settlement whose
    `overpayment_recovery` line is positive."""
    s = db.get(Settlement, settlement_id)
    if s is None:
        raise RecoveryError("NOT_FOUND", "settlement not found", status=404)
    if s.correction_of is None:
        raise RecoveryError("NOT_A_CORRECTION", "settlement is not a correction")
    line = db.execute(
        select(SettlementLineItem).where(
            SettlementLineItem.settlement_id == s.id,
            SettlementLineItem.line_type == "overpayment_recovery",
        )
    ).scalar_one_or_none()
    if line is None or line.amount_cents <= 0:
        raise RecoveryError("NOTHING_TO_RECOVER", "this correction carries no overpayment")
    paid = (
        db.execute(
            select(Payout)
            .where(
                Payout.settlement_id == s.correction_of,
                Payout.state.in_(("SUBMITTED", "CONFIRMED")),
            )
            .order_by(Payout.created_at.desc())
        )
        .scalars()
        .first()
    )
    vendor = db.get(Vendor, s.vendor_id)
    return open_recovery(
        db,
        recovery_key=f"settlement:{s.id}",
        festival_id=vendor.festival_id,
        vendor_id=s.vendor_id,
        origin_kind="settlement_correction",
        origin_id=s.id,
        settlement_id=s.id,
        payout_id=paid.id if paid else None,
        business_date=s.business_date,
        amount_cents=line.amount_cents,
        reason=s.correction_reason or "settlement recalculated after a confirmed payout",
        actor_id=actor_id,
        capability=capability,
    )


def _lock(db: Session, recovery_id: str) -> VendorRecovery:
    r = db.execute(
        select(VendorRecovery)
        .where(VendorRecovery.id == recovery_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if r is None:
        raise RecoveryError("NOT_FOUND", "recovery not found", status=404)
    return r


def _check_version(r: VendorRecovery, expected_version: int | None) -> None:
    if expected_version is not None and expected_version != r.version:
        raise RecoveryError(
            "VERSION_CONFLICT",
            f"expected version {expected_version}, current {r.version}",
            status=409,
        )


def _close_if_done(db: Session, r: VendorRecovery, actor_id: str | None) -> None:
    if r.remaining_cents == 0:
        r.status = "CLOSED"


def approve_recovery(
    db: Session,
    *,
    recovery_id: str,
    expected_version: int | None,
    mode: str,
    target_payout_id: str | None,
    note: str | None,
    actor_id: str,
    capability: str,
) -> VendorRecovery:
    r = _lock(db, recovery_id)
    _check_version(r, expected_version)
    if r.status != "OPEN":
        raise RecoveryError(
            "INVALID_TRANSITION", f"recovery is {r.status}; only OPEN can be decided"
        )
    if mode not in ("OFFSET", "REQUEST"):
        raise RecoveryError("INVALID_MODE", "mode must be OFFSET or REQUEST")
    extra = []
    if mode == "OFFSET":
        if not target_payout_id:
            raise RecoveryError("TARGET_REQUIRED", "OFFSET needs target_payout_id")
        from app.payout_dispatch import _append_payout_event, _lock_payout

        payout = _lock_payout(db, target_payout_id)
        if payout is None or payout.vendor_id != r.vendor_id:
            raise RecoveryError("TARGET_INVALID", "target payout not found for this vendor")
        if payout.state != "DRAFT":
            raise RecoveryError(
                "TARGET_NOT_DRAFT",
                f"only a DRAFT payout can absorb an offset (state {payout.state}); "
                "an approved or released payout keeps the amount its approval bound",
            )
        amount = dc.offset_cents(
            remaining_cents=r.remaining_cents, payout_amount_cents=payout.amount_cents
        )
        if amount <= 0:
            raise RecoveryError("NOTHING_TO_OFFSET", "target payout has no amount to offset")
        before = payout.amount_cents
        payout.amount_cents -= amount
        payout.version += 1
        _append_payout_event(
            db,
            payout,
            event_type="payout.offset_applied",
            actor_id=actor_id,
            actor_capability=capability,
            payload={"recovery_id": r.id, "offset_cents": amount, "amount_before_cents": before},
        )
        r.offset_cents += amount
        db.add(
            RecoveryAction(
                recovery_id=r.id,
                action="APPROVED_OFFSET",
                amount_cents=amount,
                payout_id=payout.id,
                actor_id=actor_id,
                capability=capability,
                note=note,
            )
        )
        extra.append({"ref_kind": "payout", "ref_id": payout.id})
        r.status = "REQUESTED" if r.remaining_cents else "CLOSED"
    else:
        db.add(
            RecoveryAction(
                recovery_id=r.id,
                action="APPROVED_REQUEST",
                amount_cents=r.remaining_cents,
                actor_id=actor_id,
                capability=capability,
                note=note,
            )
        )
        r.status = "REQUESTED"
    r.version += 1
    release_holds(
        db,
        source_kind="recovery",
        source_id=r.id,
        actor_id=actor_id,
        note=f"recovery {mode} approved",
    )
    db.flush()
    _event(
        db,
        r,
        "recovery.offset_approved" if mode == "OFFSET" else "recovery.request_approved",
        actor_id=actor_id,
        capability=capability,
        payload={"mode": mode, "offset_cents": r.offset_cents, "note": note},
        extra=extra,
    )
    return r


def record_receipt(
    db: Session,
    *,
    recovery_id: str,
    expected_version: int | None,
    amount_cents: int,
    business_date: date | None,
    actor_id: str,
    capability: str,
) -> VendorRecovery:
    r = _lock(db, recovery_id)
    _check_version(r, expected_version)
    if r.status != "REQUESTED":
        raise RecoveryError(
            "INVALID_TRANSITION", f"recovery is {r.status}; receipts need REQUESTED"
        )
    if amount_cents <= 0 or amount_cents > r.remaining_cents:
        raise RecoveryError(
            "INVALID_AMOUNT", f"receipt must be 1..{r.remaining_cents} cents (the remaining amount)"
        )
    n = len(
        db.execute(
            select(RecoveryAction.id).where(
                RecoveryAction.recovery_id == r.id, RecoveryAction.action == "RECEIPT_RECORDED"
            )
        ).all()
    )
    bd = business_date or datetime.now(UTC).astimezone(ZoneInfo("America/Los_Angeles")).date()
    posted = accounting.post_cumulative(
        db,
        festival_id=r.festival_id,
        base_key=f"RECOVERY_RECEIPT:{r.id}:{n + 1}",
        business_date=bd,
        kind="VENDOR_RECOVERY_RECEIPT",
        lines=dc.recovery_receipt(amount_cents),
        source_record_ids=[r.id] + ([r.payout_id] if r.payout_id else []),
        note="Vendor repaid an overpayment (SIMULATED receipt).",
        actor_id=actor_id,
        actor_capability=capability,
    )
    r.received_cents += amount_cents
    db.add(
        RecoveryAction(
            recovery_id=r.id,
            action="RECEIPT_RECORDED",
            amount_cents=amount_cents,
            journal_entry_id=posted.entry.id if posted.entry else None,
            actor_id=actor_id,
            capability=capability,
        )
    )
    _close_if_done(db, r, actor_id)
    r.version += 1
    db.flush()
    _event(
        db,
        r,
        "recovery.receipt_recorded",
        actor_id=actor_id,
        capability=capability,
        payload={
            "received_cents": amount_cents,
            "journal_entry_id": posted.entry.id if posted.entry else None,
        },
    )
    return r


def waive(
    db: Session,
    *,
    recovery_id: str,
    expected_version: int | None,
    note: str,
    business_date: date | None,
    actor_id: str,
    capability: str,
) -> VendorRecovery:
    r = _lock(db, recovery_id)
    _check_version(r, expected_version)
    if r.status == "CLOSED":
        raise RecoveryError("INVALID_TRANSITION", "recovery is already CLOSED")
    if not (note or "").strip():
        raise RecoveryError("NOTE_REQUIRED", "a waiver needs a written reason")
    amount = r.remaining_cents
    bd = business_date or datetime.now(UTC).astimezone(ZoneInfo("America/Los_Angeles")).date()
    posted = accounting.post_cumulative(
        db,
        festival_id=r.festival_id,
        base_key=f"RECOVERY_WAIVER:{r.id}",
        business_date=bd,
        kind="VENDOR_RECOVERY_WAIVER",
        lines=dc.recovery_waiver(amount),
        source_record_ids=[r.id] + ([r.payout_id] if r.payout_id else []),
        note=f"Overpayment waived by finance lead: {note.strip()} (DRAFT account choice).",
        actor_id=actor_id,
        actor_capability=capability,
    )
    r.waived_cents += amount
    db.add(
        RecoveryAction(
            recovery_id=r.id,
            action="WAIVED",
            amount_cents=amount,
            journal_entry_id=posted.entry.id if posted.entry else None,
            actor_id=actor_id,
            capability=capability,
            note=note.strip(),
        )
    )
    r.status = "CLOSED"
    r.version += 1
    release_holds(db, source_kind="recovery", source_id=r.id, actor_id=actor_id, note="waived")
    db.flush()
    _event(
        db,
        r,
        "recovery.waived",
        actor_id=actor_id,
        capability=capability,
        payload={"waived_cents": amount, "note": note.strip()},
    )
    return r


def recovery_out(db: Session, r: VendorRecovery) -> dict:
    actions = (
        db.execute(
            select(RecoveryAction)
            .where(RecoveryAction.recovery_id == r.id)
            .order_by(RecoveryAction.created_at, RecoveryAction.id)
        )
        .scalars()
        .all()
    )
    return {
        "id": r.id,
        "version": r.version,
        "status": r.status,
        "festival_id": r.festival_id,
        "vendor_id": r.vendor_id,
        "origin_kind": r.origin_kind,
        "origin_id": r.origin_id,
        "settlement_id": r.settlement_id,
        "payout_id": r.payout_id,
        "business_date": r.business_date.isoformat() if r.business_date else None,
        "amount_cents": r.amount_cents,
        "offset_cents": r.offset_cents,
        "received_cents": r.received_cents,
        "waived_cents": r.waived_cents,
        "remaining_cents": r.remaining_cents,
        "reason": r.reason,
        "label": "SIMULATED money; no real recovery collected",
        "actions": [
            {
                "id": a.id,
                "action": a.action,
                "amount_cents": a.amount_cents,
                "payout_id": a.payout_id,
                "journal_entry_id": a.journal_entry_id,
                "actor_id": a.actor_id,
                "capability": a.capability,
                "note": a.note,
                "at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in actions
        ],
    }
