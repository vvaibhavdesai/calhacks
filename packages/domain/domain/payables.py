"""A08 payables: pure supplier-invoice status and payment-guard rules.

No DB, no clock. app/payables.py reads row-locked invoice/PO state and
calls these to decide.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class InvoiceBalance:
    gross_cents: int
    credited_cents: int
    paid_cents: int
    deposit_applied_cents: int = 0

    @property
    def outstanding_cents(self) -> int:
        return self.gross_cents - self.credited_cents - self.paid_cents - self.deposit_applied_cents


def normalize_invoice_number(raw: str) -> str:
    """Case/whitespace-insensitive key, e.g. " b-001 " == "B-001". Uses
    `.lower()`, not `.casefold()`, so Python and the matching SQL
    expression `lower(btrim(invoice_number, E' \\t\\r\\n'))` agree on every
    input this fixture pack or any real invoice number can contain."""
    return raw.strip().lower()


def delivery_supported_cents(accepted_qty: int, unit_price_cents: int) -> int:
    return accepted_qty * unit_price_cents


def max_payable_now(balance: InvoiceBalance, delivery_supported_cents: int | None) -> int:
    """The most that can be paid right now without dispute. With no
    delivery limit (no PO lines), it's just the outstanding balance. With
    one, it's also capped by what's left of the supported value once the
    deposit and prior payments are accounted for -- `supported - paid -
    deposit_applied`, same quantity `check_payment` below refuses against.
    Verified against the fixture: at 4/6 units ($1,500,000 supported), an
    $800,000 deposit applied and nothing paid yet, this is $700,000 --
    not the invoice's full $1,450,000 outstanding."""
    if delivery_supported_cents is None:
        return max(balance.outstanding_cents, 0)
    remaining_supported = max(
        delivery_supported_cents - balance.paid_cents - balance.deposit_applied_cents, 0
    )
    return max(min(balance.outstanding_cents, remaining_supported), 0)


def invoice_status(
    balance: InvoiceBalance,
    *,
    po_excess_cents: int,
    variance_approved: bool,
    due_date: date | None,
    as_of: date,
) -> str:
    if balance.outstanding_cents <= 0:
        return "PAID"
    if po_excess_cents > 0 and not variance_approved:
        return "PAYMENT_BLOCKED"
    if due_date is not None and due_date < as_of:
        return "OVERDUE"
    return "OPEN"


def po_excess(po_amount_cents: int, invoiced_to_date_cents: int) -> int:
    """How far the PO's invoiced total (gross, net of credit notes) exceeds
    the PO. Zero when within the PO."""
    return max(invoiced_to_date_cents - po_amount_cents, 0)


class PaymentRefused(ValueError):
    def __init__(self, reason_code: str, details: dict) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.details = details


def check_payment(
    balance: InvoiceBalance,
    amount_cents: int,
    *,
    po_excess_cents: int,
    variance_approved: bool,
    is_credit_note: bool,
    delivery_supported_cents: int | None = None,
) -> None:
    """Raises PaymentRefused unless paying `amount_cents` now is allowed.
    The caller must hold the invoice row lock, so `balance` is current and
    two concurrent payments serialize: the second sees the first's paid
    amount and is refused if it would overpay.

    `delivery_supported_cents` is None when the invoice's PO has no lines
    (every PO before this feature) -- no delivery-based limit applies.
    When it IS given, this is the LAST check, after everything else
    passes: paid + deposit_applied + amount may not exceed it."""
    if is_credit_note:
        raise PaymentRefused("credit_note_not_payable", {})
    if amount_cents <= 0:
        raise PaymentRefused("amount_must_be_positive", {"amount_cents": amount_cents})
    if po_excess_cents > 0 and not variance_approved:
        raise PaymentRefused(
            "invoice_exceeds_po_variance_not_approved", {"po_excess_cents": po_excess_cents}
        )
    if balance.outstanding_cents <= 0:
        raise PaymentRefused(
            "invoice_already_paid", {"outstanding_cents": balance.outstanding_cents}
        )
    if amount_cents > balance.outstanding_cents:
        raise PaymentRefused(
            "amount_exceeds_outstanding",
            {"amount_cents": amount_cents, "outstanding_cents": balance.outstanding_cents},
        )
    if delivery_supported_cents is not None:
        settled = balance.paid_cents + balance.deposit_applied_cents
        if settled + amount_cents > delivery_supported_cents:
            raise PaymentRefused(
                "amount_exceeds_delivery_supported",
                {
                    "delivery_supported_cents": delivery_supported_cents,
                    "settled_cents": settled,
                    "max_payable_now_cents": max(delivery_supported_cents - settled, 0),
                },
            )
