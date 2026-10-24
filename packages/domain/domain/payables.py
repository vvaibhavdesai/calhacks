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

    @property
    def outstanding_cents(self) -> int:
        return self.gross_cents - self.credited_cents - self.paid_cents


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
) -> None:
    """Raises PaymentRefused unless paying `amount_cents` now is allowed.
    The caller must hold the invoice row lock, so `balance` is current and
    two concurrent payments serialize: the second sees the first's paid
    amount and is refused if it would overpay."""
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
