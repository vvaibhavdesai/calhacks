"""X0.3 (prevention extension): contract obligations and links.

Pure, side-effect-free -- same convention as domain.payables/domain.
case_routing/domain.term_precedence: plain dataclasses in, plain values
out, no database session, no HTTP, no datetime.now(), no randomness. The
DB-touching orchestration (locking the obligation row, reading its current
`obligation_link` rows, writing the result) lives in
services/api/app/contract_obligations.py.

Status flow (TICKET_SPEC.md X0.3, verbatim): "proposed -> approved ->
committed -> invoiced -> paid | cancelled". Modeled below as an explicit
transition table (ALLOWED_TRANSITIONS), not an ad hoc if/elif chain --
`validate_transition` raises IllegalTransitionError for anything not
listed, including an unrecognized status on either side and a same-state
no-op (a transition is a command, not an idempotent set -- a caller that
wants "already there" to be a no-op checks that itself first, same
discipline app/payout_dispatch.py's callers already follow for Payout.state).

Judgment call on "cancelled" (also documented in docs/progress/
X0.3-handoff.md, not left implicit here only): TICKET_SPEC.md's arrow
notation shows cancelled only as a branch alongside "paid" AFTER
"invoiced" ("invoiced -> paid | cancelled"). Read with full literalness,
that would make an obligation uncancellable while merely
proposed/approved/committed -- which does not match how a real deposit/
milestone/final commitment would actually be managed (a deal can fall
through before any invoice exists, and that must be recordable, not
modeled as stuck forever in "proposed"). ALLOWED_TRANSITIONS instead lets
"cancelled" be reached from every non-terminal state (proposed, approved,
committed, invoiced), never from "paid" -- money has already been
simulated as moved by then, so cancelling would misrepresent what
happened, the same principle CONTRACTS.md's C04 applies to Payout ("state
stays whatever the rail actually reported ... NEVER forced to CANCELLED
once money has moved"). "paid" is reachable only from "invoiced",
matching the spec's literal arrow exactly. Both "paid" and "cancelled" are
terminal: ALLOWED_TRANSITIONS maps each to an empty set.

Derived values (TICKET_SPEC.md: "invoiced, paid, credited, and remaining =
amount - paid - credited"): computed here, purely, from whatever
`obligation_link` rows the caller passes in -- see
`compute_obligation_totals`. This is a COMPUTE-ON-READ design, not a
mutable running-total column on `contract_obligation` itself (contrast
with app/payables.py's `SupplierInvoice.outstanding_cents`/
`credited_cents`/`paid_cents`, which ARE a row-locked, mutated-in-place
running balance). See docs/progress/X0.3-handoff.md's "Design, in brief"
for the full reasoning on why this ticket chose compute-on-read instead
of following that closer precedent.

Overpayment (TICKET_SPEC.md: "An overpayment ... opens a case"): defined
symmetrically with `remaining` -- `is_overpaid` is exactly "remaining
would be negative", i.e. `paid_cents + credited_cents > amount_cents`.
Deliberately does NOT fold `invoiced_cents` into that comparison, for the
same reason `remaining`'s own formula omits it: TICKET_SPEC.md's formula
is "amount - paid - credited", not "amount - invoiced", so an obligation
can be fully invoiced (even over-invoiced) without ever being
"overpaid" -- only real payment/credit links can push it there.
"""

from __future__ import annotations

from dataclasses import dataclass

STATUS_PROPOSED = "proposed"
STATUS_APPROVED = "approved"
STATUS_COMMITTED = "committed"
STATUS_INVOICED = "invoiced"
STATUS_PAID = "paid"
STATUS_CANCELLED = "cancelled"

STATUSES: tuple[str, ...] = (
    STATUS_PROPOSED,
    STATUS_APPROVED,
    STATUS_COMMITTED,
    STATUS_INVOICED,
    STATUS_PAID,
    STATUS_CANCELLED,
)

# state -> set of states it may legally move to next. See module docstring
# for the "cancelled" judgment call. Both terminal states map to an empty
# set: nothing may ever transition out of "paid" or "cancelled".
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_PROPOSED: frozenset({STATUS_APPROVED, STATUS_CANCELLED}),
    STATUS_APPROVED: frozenset({STATUS_COMMITTED, STATUS_CANCELLED}),
    STATUS_COMMITTED: frozenset({STATUS_INVOICED, STATUS_CANCELLED}),
    STATUS_INVOICED: frozenset({STATUS_PAID, STATUS_CANCELLED}),
    STATUS_PAID: frozenset(),
    STATUS_CANCELLED: frozenset(),
}

COUNTERPARTY_TYPES: tuple[str, ...] = ("vendor", "supplier")
OBLIGATION_KINDS: tuple[str, ...] = ("deposit", "milestone", "final")
LINK_SUBJECT_TYPES: tuple[str, ...] = ("po", "invoice", "payment", "credit_note")

# Which obligation_link subject_types feed each derived total. A "po" link
# exists purely for traceability (which purchase order this obligation's
# spend flows through) -- TICKET_SPEC.md's derived-values list never counts
# a PO toward invoiced/paid/credited/remaining; only invoice/payment/
# credit_note links do. This is also exactly what makes the "linking a PO
# or invoice never changes total commitment" property trivially true for
# invoice links too: an "invoice" link moves `invoiced_cents` (a reported
# figure) but never touches `remaining_cents`, which only reacts to
# payment/credit_note links.
_INVOICED_SUBJECT_TYPES = frozenset({"invoice"})
_PAID_SUBJECT_TYPES = frozenset({"payment"})
_CREDITED_SUBJECT_TYPES = frozenset({"credit_note"})


class IllegalTransitionError(ValueError):
    def __init__(self, current: str, attempted: str) -> None:
        super().__init__(f"illegal contract_obligation transition: {current} -> {attempted}")
        self.current = current
        self.attempted = attempted


def validate_transition(current_status: str, new_status: str) -> None:
    """Raises IllegalTransitionError unless `new_status` is in
    ALLOWED_TRANSITIONS[current_status] -- never guesses, never silently
    clamps, same "never guess" discipline as domain.case_routing.route_case
    / domain.term_precedence.resolve_term_precedence."""
    if current_status not in ALLOWED_TRANSITIONS or new_status not in STATUSES:
        raise IllegalTransitionError(current_status, new_status)
    if new_status not in ALLOWED_TRANSITIONS[current_status]:
        raise IllegalTransitionError(current_status, new_status)


@dataclass(frozen=True)
class LinkAmount:
    """The two fields of one obligation_link row that matter for computing
    totals. A plain dataclass (not the ORM row itself), same convention as
    domain.term_precedence.TermCandidate / domain.policy.PolicyCandidate."""

    subject_type: str
    amount_cents: int


@dataclass(frozen=True)
class ObligationTotals:
    amount_cents: int
    invoiced_cents: int
    paid_cents: int
    credited_cents: int
    remaining_cents: int
    is_overpaid: bool
    overpayment_cents: int


def compute_obligation_totals(amount_cents: int, links: list[LinkAmount]) -> ObligationTotals:
    """Pure: `amount_cents` in, unchanged and unmoved, plain totals out.
    Never mutates anything, never reads/writes a database -- the caller
    passes in whatever `obligation_link` rows currently exist for this
    obligation, fresh, every time this is called."""
    if amount_cents <= 0:
        raise ValueError(f"amount_cents must be positive, got {amount_cents}")
    for link in links:
        if link.subject_type not in LINK_SUBJECT_TYPES:
            raise ValueError(
                f"unknown obligation_link subject_type {link.subject_type!r}; "
                f"expected one of {LINK_SUBJECT_TYPES}"
            )
        if link.amount_cents <= 0:
            raise ValueError(
                f"obligation_link amount_cents must be positive, got {link.amount_cents}"
            )

    invoiced_cents = sum(
        link.amount_cents for link in links if link.subject_type in _INVOICED_SUBJECT_TYPES
    )
    paid_cents = sum(
        link.amount_cents for link in links if link.subject_type in _PAID_SUBJECT_TYPES
    )
    credited_cents = sum(
        link.amount_cents for link in links if link.subject_type in _CREDITED_SUBJECT_TYPES
    )
    remaining_cents = amount_cents - paid_cents - credited_cents
    overpayment_cents = max(paid_cents + credited_cents - amount_cents, 0)

    return ObligationTotals(
        amount_cents=amount_cents,
        invoiced_cents=invoiced_cents,
        paid_cents=paid_cents,
        credited_cents=credited_cents,
        remaining_cents=remaining_cents,
        is_overpaid=overpayment_cents > 0,
        overpayment_cents=overpayment_cents,
    )
