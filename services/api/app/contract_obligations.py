"""X0.3 (prevention extension): contract obligations and links.

DB-touching orchestration on top of packages/domain/domain/
contract_obligations.py's pure status-transition/totals logic -- same
split as every other domain-adjacent module in this codebase (app/
payables.py wraps domain.payables, app/term_provenance.py wraps domain.
term_precedence, app/payout_dispatch.py wraps domain.payout_dispatch).

Every write function here takes the caller's session, row-locks what it
decides on, appends a flight-recorder event via UnitOfWork, and never
calls db.commit() itself -- the caller (an endpoint, a dev-fixture script,
or another service function) commits exactly once per request/command,
same convention as app/payables.py and app/term_provenance.py.

Design, in brief (see docs/progress/X0.3-handoff.md for the full
reasoning, reproduced here only in summary):

  - `ContractObligation.amount_cents` is written exactly once, at
    `propose_obligation` time, and never again. There is no function in
    this module that mutates it. This is the direct, structural
    enforcement of TICKET_SPEC.md's "it must never total $40,000"
    requirement -- not a runtime assertion recomputed on every read, but
    simply: nothing ever writes to that column a second time.
  - invoiced/paid/credited/remaining are COMPUTED ON READ from whatever
    `ObligationLink` rows exist right now (`obligation_totals` below calls
    straight into domain.contract_obligations.compute_obligation_totals),
    not maintained as a mutable running-total column on
    `ContractObligation` itself. This is a deliberate departure from app/
    payables.py's own `SupplierInvoice.outstanding_cents`/`credited_cents`/
    `paid_cents` precedent (a real, row-locked, mutated-in-place running
    balance) -- TICKET_SPEC.md explicitly invited this ticket to choose
    either approach and say which, so: compute-on-read was chosen because
    (a) the spec's own wording leans that way ("don't store these as a
    mutable running total that can drift -- compute them"), (b) the
    numbers being computed are a small GROUP BY SUM over `obligation_links`
    rows scoped to one obligation_id (there is no realistic volume where
    this is a performance concern, unlike SupplierInvoice's balance, which
    genuinely needs to answer "can I pay this" under row-lock contention
    many times across a festival's run), and (c) it trivially makes the
    required property ("linking a PO or invoice never changes total
    commitment") true by construction -- there is no running-total column
    for a buggy update to drift, because there is no running-total column
    at all.
  - `link_obligation` still row-locks the PARENT `ContractObligation` row
    before inserting a new link and recomputing totals, even though
    `amount_cents` itself is never written -- this row lock is what
    serializes two concurrent link-and-check-overpayment calls on the SAME
    obligation (same purpose as app/payables.py's `_recompute_po` locking
    the PO row: the thing being protected is "did we definitely see every
    other link that already happened before deciding overpaid," not "is
    this one column's value safe to write").
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from domain.case_routing import route_case
from domain.contract_obligations import (
    COUNTERPARTY_TYPES,
    LINK_SUBJECT_TYPES,
    OBLIGATION_KINDS,
    STATUSES,
    IllegalTransitionError,
    LinkAmount,
    ObligationTotals,
    compute_obligation_totals,
    validate_transition,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models_finance import ContractObligation, ObligationLink
from app.models_ledger import Case
from app.uow import UnitOfWork

OVERPAYMENT_CASE_KIND = "obligation_overpaid"


class ContractObligationNotFoundError(LookupError):
    pass


class ContractObligationConflictError(ValueError):
    def __init__(self, code: str, details: dict | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = details or {}


def _event(db, *, stream, event_type, aggregate_id, version, actor_id, actor_capability, payload):
    UnitOfWork(db).append_event(
        stream_id=stream,
        event_type=event_type,
        aggregate_id=aggregate_id,
        aggregate_version=version,
        actor_id=actor_id,
        actor_type="human" if actor_id else "system",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        payload=payload,
        lineage_refs=[{"ref_kind": "contract_obligation", "ref_id": aggregate_id}],
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def propose_obligation(
    db: Session,
    *,
    festival_id: str,
    contract_id: str,
    counterparty_type: str,
    counterparty_id: str,
    kind: str,
    amount_cents: int,
    due_date: date | None,
    condition: str | None,
    source_document_id: str,
    source_version: int,
    content_hash: str,
    locator_kind: str,
    locator: str,
    quoted_excerpt: str | None,
    actor_id: str,
    actor_capability: str,
) -> ContractObligation:
    """Creates a new `ContractObligation` row, status="proposed". This is
    the ONLY place `amount_cents` is ever written -- see module docstring."""
    if counterparty_type not in COUNTERPARTY_TYPES:
        raise ContractObligationConflictError(
            "unknown_counterparty_type",
            {"counterparty_type": counterparty_type, "expected": list(COUNTERPARTY_TYPES)},
        )
    if kind not in OBLIGATION_KINDS:
        raise ContractObligationConflictError(
            "unknown_obligation_kind", {"kind": kind, "expected": list(OBLIGATION_KINDS)}
        )
    if amount_cents <= 0:
        raise ContractObligationConflictError(
            "amount_must_be_positive", {"amount_cents": amount_cents}
        )

    obligation = ContractObligation(
        festival_id=festival_id,
        contract_id=contract_id,
        counterparty_type=counterparty_type,
        counterparty_id=counterparty_id,
        kind=kind,
        amount_cents=amount_cents,
        due_date=due_date,
        condition=condition,
        status="proposed",
        source_document_id=source_document_id,
        source_version=source_version,
        content_hash=content_hash,
        locator_kind=locator_kind,
        locator=locator,
        quoted_excerpt=quoted_excerpt,
        proposed_by=actor_id,
    )
    db.add(obligation)
    db.flush()
    _event(
        db,
        stream=f"contract_obligation:{obligation.id}",
        event_type="contract_obligation.proposed",
        aggregate_id=obligation.id,
        version=obligation.version,
        actor_id=actor_id,
        actor_capability=actor_capability,
        payload={
            "counterparty_type": counterparty_type,
            "counterparty_id": counterparty_id,
            "kind": kind,
            "amount_cents": amount_cents,
        },
    )
    return obligation


# ---------------------------------------------------------------------------
# Status transitions
# ---------------------------------------------------------------------------


def _lock_obligation(db: Session, obligation_id: str) -> ContractObligation:
    obligation = db.execute(
        select(ContractObligation)
        .where(ContractObligation.id == obligation_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if obligation is None:
        raise ContractObligationNotFoundError(f"contract obligation {obligation_id} not found")
    return obligation


def transition_obligation(
    db: Session,
    *,
    obligation_id: str,
    new_status: str,
    actor_id: str,
    actor_capability: str,
    note: str | None = None,
) -> ContractObligation:
    """Enforces the explicit status state machine (domain.
    contract_obligations.ALLOWED_TRANSITIONS). Raises IllegalTransitionError
    (a ValueError subclass) for anything not an allowed edge -- never
    silently clamps, never treats "already there" as success."""
    obligation = _lock_obligation(db, obligation_id)
    try:
        validate_transition(obligation.status, new_status)
    except IllegalTransitionError as exc:
        raise ContractObligationConflictError(
            "illegal_status_transition",
            {"current_status": exc.current, "attempted_status": exc.attempted},
        ) from exc

    previous_status = obligation.status
    obligation.status = new_status
    obligation.version += 1
    _event(
        db,
        stream=f"contract_obligation:{obligation.id}",
        event_type="contract_obligation.status_changed",
        aggregate_id=obligation.id,
        version=obligation.version,
        actor_id=actor_id,
        actor_capability=actor_capability,
        payload={"from": previous_status, "to": new_status, "note": note},
    )
    return obligation


# ---------------------------------------------------------------------------
# Links and derived totals
# ---------------------------------------------------------------------------


def _link_amounts(db: Session, obligation_id: str) -> list[LinkAmount]:
    rows = db.execute(
        select(ObligationLink.subject_type, ObligationLink.amount_cents).where(
            ObligationLink.obligation_id == obligation_id
        )
    ).all()
    return [LinkAmount(subject_type=r.subject_type, amount_cents=r.amount_cents) for r in rows]


def obligation_totals(db: Session, obligation: ContractObligation) -> ObligationTotals:
    """Read-only. Recomputes fresh from whatever `obligation_links` rows
    exist right now -- see module docstring for why this is compute-on-read
    rather than a stored running balance."""
    return compute_obligation_totals(obligation.amount_cents, _link_amounts(db, obligation.id))


def link_obligation(
    db: Session,
    *,
    obligation_id: str,
    subject_type: str,
    subject_id: str,
    amount_cents: int,
    actor_id: str,
    actor_capability: str,
    note: str | None = None,
) -> tuple[ObligationLink, ContractObligation, ObligationTotals, Case | None]:
    """Links one PO/invoice/payment/credit_note to an obligation and
    recomputes totals. Returns (the new link, the obligation, its fresh
    totals, and a newly-opened overpayment Case if this link caused one --
    None if no case was opened, including when one was already open).

    The obligation row is locked FOR UPDATE for the duration of this
    insert-and-recompute (see module docstring's "Design, in brief" for
    why: `amount_cents` is never written, but the lock still serializes
    concurrent link+recompute calls on the SAME obligation so neither one
    computes totals from a stale/partial view of the other's just-inserted
    link).
    """
    if subject_type not in LINK_SUBJECT_TYPES:
        raise ContractObligationConflictError(
            "unknown_link_subject_type",
            {"subject_type": subject_type, "expected": list(LINK_SUBJECT_TYPES)},
        )
    if amount_cents <= 0:
        raise ContractObligationConflictError(
            "link_amount_must_be_positive", {"amount_cents": amount_cents}
        )

    obligation = _lock_obligation(db, obligation_id)

    link = ObligationLink(
        obligation_id=obligation.id,
        subject_type=subject_type,
        subject_id=subject_id,
        amount_cents=amount_cents,
        note=note,
        actor_id=actor_id,
        actor_capability=actor_capability,
    )
    db.add(link)
    db.flush()

    totals = obligation_totals(db, obligation)

    _event(
        db,
        stream=f"contract_obligation:{obligation.id}",
        event_type="contract_obligation.linked",
        aggregate_id=obligation.id,
        version=obligation.version,
        actor_id=actor_id,
        actor_capability=actor_capability,
        payload={
            "link_id": link.id,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "amount_cents": amount_cents,
            "amount_cents_unchanged": obligation.amount_cents,
            "invoiced_cents": totals.invoiced_cents,
            "paid_cents": totals.paid_cents,
            "credited_cents": totals.credited_cents,
            "remaining_cents": totals.remaining_cents,
        },
    )

    case = None
    if totals.is_overpaid:
        case = _open_overpayment_case_if_absent(
            db,
            obligation=obligation,
            totals=totals,
            actor_id=actor_id,
            actor_capability=actor_capability,
        )

    return link, obligation, totals, case


def _open_overpayment_case_if_absent(
    db: Session,
    *,
    obligation: ContractObligation,
    totals: ObligationTotals,
    actor_id: str,
    actor_capability: str,
) -> Case | None:
    existing = db.execute(
        select(Case).where(
            Case.status == "OPEN",
            Case.subject_type == "contract_obligation",
            Case.subject_id == obligation.id,
            Case.reason_code == OVERPAYMENT_CASE_KIND,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return None

    owner_capability = route_case("contract_obligation", OVERPAYMENT_CASE_KIND)
    case = Case(
        vendor_id=obligation.counterparty_id if obligation.counterparty_type == "vendor" else None,
        reason_code=OVERPAYMENT_CASE_KIND,
        evidence_refs=[
            {"ref_kind": "contract_obligation", "ref_id": obligation.id, "role": "subject"}
        ],
        financial_impact_cents=totals.overpayment_cents,
        proposed_resolution=(
            "Linked payments/credit notes exceed this obligation's committed amount: "
            "a human confirms how the excess is handled (refund, reclassify, or write off)."
        ),
        required_capability=owner_capability,
        subject_type="contract_obligation",
        subject_id=obligation.id,
        status="OPEN",
    )
    db.add(case)
    db.flush()
    _event(
        db,
        stream=f"contract_obligation:{obligation.id}",
        event_type="contract_obligation.overpaid",
        aggregate_id=obligation.id,
        version=obligation.version,
        actor_id=actor_id,
        actor_capability=actor_capability,
        payload={
            "case_id": case.id,
            "amount_cents": obligation.amount_cents,
            "paid_cents": totals.paid_cents,
            "credited_cents": totals.credited_cents,
            "overpayment_cents": totals.overpayment_cents,
        },
    )
    return case


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def contract_totals(db: Session, *, contract_id: str) -> dict:
    """Aggregates every non-cancelled `ContractObligation` sharing
    `contract_id` into one commitment/remaining view -- this is what
    TICKET_SPEC.md's flagship "Done when" scenario means by "a $20,000
    contract ... shows commitment $20,000 and remaining $15,000": the
    $20,000 is the SUM of this contract's obligations' `amount_cents`
    (e.g. a $5,000 deposit obligation + a $15,000 final obligation), not a
    single obligation row's own amount. A cancelled obligation is excluded
    from this sum entirely -- a cancelled commitment is no longer a real
    commitment, so it must not keep contributing to "commitment" at the
    contract level (deliberate judgment call; TICKET_SPEC.md's X0.3 section
    does not say either way, but leaving a cancelled obligation in the sum
    would make "commitment" lie about what is actually still owed).
    """
    obligations = (
        db.execute(
            select(ContractObligation).where(
                ContractObligation.contract_id == contract_id,
                ContractObligation.status != "cancelled",
            )
        )
        .scalars()
        .all()
    )
    per_obligation = []
    commitment_cents = 0
    invoiced_cents = 0
    paid_cents = 0
    credited_cents = 0
    remaining_cents = 0
    for obligation in obligations:
        totals = obligation_totals(db, obligation)
        commitment_cents += totals.amount_cents
        invoiced_cents += totals.invoiced_cents
        paid_cents += totals.paid_cents
        credited_cents += totals.credited_cents
        remaining_cents += totals.remaining_cents
        per_obligation.append(
            {
                "obligation_id": obligation.id,
                "kind": obligation.kind,
                "status": obligation.status,
                "amount_cents": totals.amount_cents,
                "invoiced_cents": totals.invoiced_cents,
                "paid_cents": totals.paid_cents,
                "credited_cents": totals.credited_cents,
                "remaining_cents": totals.remaining_cents,
            }
        )
    return {
        "contract_id": contract_id,
        "commitment_cents": commitment_cents,
        "invoiced_cents": invoiced_cents,
        "paid_cents": paid_cents,
        "credited_cents": credited_cents,
        "remaining_cents": remaining_cents,
        "obligations": per_obligation,
    }


def obligation_view(db: Session, obligation: ContractObligation) -> dict:
    totals = obligation_totals(db, obligation)
    return {
        "id": obligation.id,
        "festival_id": obligation.festival_id,
        "contract_id": obligation.contract_id,
        "counterparty_type": obligation.counterparty_type,
        "counterparty_id": obligation.counterparty_id,
        "kind": obligation.kind,
        "status": obligation.status,
        "due_date": obligation.due_date.isoformat() if obligation.due_date else None,
        "condition": obligation.condition,
        "amount_cents": totals.amount_cents,
        "invoiced_cents": totals.invoiced_cents,
        "paid_cents": totals.paid_cents,
        "credited_cents": totals.credited_cents,
        "remaining_cents": totals.remaining_cents,
        "is_overpaid": totals.is_overpaid,
        "overpayment_cents": totals.overpayment_cents,
    }


__all__ = [
    "STATUSES",
    "ContractObligationConflictError",
    "ContractObligationNotFoundError",
    "contract_totals",
    "link_obligation",
    "obligation_totals",
    "obligation_view",
    "propose_obligation",
    "transition_obligation",
]
