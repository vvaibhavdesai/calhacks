"""A08 accounting: pure posting map, journal-line builders and balance math.

Everything here takes plain values and returns plain values -- no database
session, no HTTP, no clock -- same convention as domain.settlement (A04),
domain.policy (A05) and domain.payout_dispatch (A07). The DB-touching
orchestration lives in services/api/app/accounting.py.

Posting map (C02, still DRAFT -- a fictional bookkeeping convention, not
legal/tax advice): the organizer acts as collection agent for vendor sales
and recognizes only its commission as revenue. Vendor gross sales are NOT
organizer revenue; they are a liability (VENDOR_FUNDS_PAYABLE) until paid
out. Collected sales tax, tips and customer money owed back are protected
liabilities, never income. Account codes and roles deliberately match the
DRAFT map in lane B's independent oracle (tests/oracle/oracle_full.py on
`lane-b`) so A-track journals can be compared to it by ROLE; the oracle's
own arithmetic is not reused here.

Sign convention: a builder may produce a negative debit or credit (e.g. a
refund that reverses part of a sale). `normalize` moves any negative amount
to the opposite side, so every stored line has non-negative debit/credit
and exactly one of them non-zero.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

CHART_VERSION = "DRAFT-chart-0.1"
POSTING_RULE_VERSION = "DRAFT-posting-map-0.1"

# code -> (role, name, category)
ACCOUNTS: dict[str, tuple[str, str, str]] = {
    "1000": ("BANK_OPERATING", "Bank cash - operating", "ASSET"),
    "1050": ("CASH_OFFICE", "Cash on hand - organizer cash office", "ASSET"),
    "1100": ("PROCESSOR_CLEARING", "Processor clearing (card + wristband)", "ASSET"),
    "1200": ("VENDOR_RECEIVABLE", "Vendor commission receivable / unpaid balance", "ASSET"),
    "1999": ("SUSPENSE", "Unclassified bank activity (suspense)", "ASSET"),
    "2000": ("VENDOR_FUNDS_PAYABLE", "Vendor funds payable", "LIABILITY"),
    "2100": ("SALES_TAX_LIABILITY", "Sales tax liability/control (DRAFT, not filed)", "LIABILITY"),
    "2200": ("TIPS_LIABILITY", "Tips liability/control", "LIABILITY"),
    "2300": ("SUPPLIER_AP", "Supplier accounts payable", "LIABILITY"),
    "2400": ("DEFERRED_TICKET_REVENUE", "Deferred ticket revenue", "LIABILITY"),
    "2500": ("CUSTOMER_REFUNDS_PAYABLE", "Customer refunds payable", "LIABILITY"),
    "3000": ("OPENING_EQUITY", "Opening balance equity", "EQUITY"),
    "4000": ("COMMISSION_REVENUE", "Organizer commission revenue", "REVENUE"),
    "4100": ("TICKET_REVENUE", "Ticket revenue", "REVENUE"),
    "4200": ("SPONSORSHIP_REVENUE", "Sponsorship revenue", "REVENUE"),
    "5100": ("PROCESSOR_FEE_EXPENSE", "Processor fee expense", "EXPENSE"),
    "5110": ("TICKETING_FEE_EXPENSE", "Ticketing platform fee expense", "EXPENSE"),
    "5200": ("CASH_OVER_SHORT", "Cash over/short", "EXPENSE"),
    "5300": ("SUPPLIER_EXPENSE", "Supplier costs (incl. supplier tax)", "EXPENSE"),
    "5400": ("OPERATING_EXPENSE", "Operating expenses", "EXPENSE"),
}
CODE_BY_ROLE: dict[str, str] = {role: code for code, (role, _n, _c) in ACCOUNTS.items()}

# Liabilities that hold other people's money. Their balances are never
# freely spendable organizer cash (task: "Reserved tax/tips cannot become
# freely spendable income").
PROTECTED_LIABILITY_ROLES = (
    "SALES_TAX_LIABILITY",
    "TIPS_LIABILITY",
    "CUSTOMER_REFUNDS_PAYABLE",
)


class UnbalancedEntryError(ValueError):
    pass


class UnknownAccountError(ValueError):
    pass


@dataclass(frozen=True)
class Line:
    account_code: str
    debit_cents: int
    credit_cents: int

    @property
    def role(self) -> str:
        return ACCOUNTS[self.account_code][0]


def normalize(raw: Iterable[tuple[str, int, int]]) -> list[Line]:
    """(code, debit, credit) triples -> validated Lines. Negative amounts
    flip side; all-zero lines are dropped; unknown codes are rejected."""
    out: list[Line] = []
    for code, dr, cr in raw:
        if code not in ACCOUNTS:
            raise UnknownAccountError(code)
        if not isinstance(dr, int) or not isinstance(cr, int):
            raise TypeError("journal amounts must be integer cents")
        debit = max(dr, 0) + max(-cr, 0)
        credit = max(cr, 0) + max(-dr, 0)
        net = debit - credit
        if net == 0:
            continue
        out.append(Line(code, max(net, 0), max(-net, 0)))
    return out


def assert_balanced(lines: list[Line]) -> None:
    debits = sum(line.debit_cents for line in lines)
    credits = sum(line.credit_cents for line in lines)
    if debits != credits:
        raise UnbalancedEntryError(f"debits {debits} != credits {credits}")
    if not lines:
        raise UnbalancedEntryError("an entry must have at least one non-zero line")


def build(raw: Iterable[tuple[str, int, int]]) -> list[Line]:
    lines = normalize(raw)
    assert_balanced(lines)
    return lines


# ---------------------------------------------------------------------------
# Posting rules (one function per entry kind). Every amount is integer cents.
# ---------------------------------------------------------------------------


def opening_balance(amount_cents: int) -> list[Line]:
    return build([("1000", amount_cents, 0), ("3000", 0, amount_cents)])


def sales_capture(
    *,
    asset_code: str,
    gross_cents: int,
    tax_cents: int,
    tips_cents: int,
    customer_money_cents: int = 0,
) -> list[Line]:
    """Centrally collected sales: the organizer holds the money as agent.
    Eligible (gross - tax - tips - customer money) is owed to the vendor;
    tax and tips are protected liabilities; `customer_money_cents` (e.g. a
    duplicate charge, captured beyond the order total) is owed back to the
    customer. Organizer revenue is NOT credited here -- commission is
    recognized separately when the settlement is posted. Negative inputs
    (a refund/chargeback summary) reverse the same lines."""
    eligible = gross_cents - tax_cents - tips_cents - customer_money_cents
    return build(
        [
            (asset_code, gross_cents, 0),
            ("2000", 0, eligible),
            ("2100", 0, tax_cents),
            ("2200", 0, tips_cents),
            ("2500", 0, customer_money_cents),
        ]
    )


@dataclass(frozen=True)
class OrderMoney:
    """How one order's centrally collected payments split, in cents."""

    gross_net_cents: int  # captures minus refunds/chargebacks actually moved
    goods_cents: int  # net amount that is vendor sales (incl. tax and tips)
    goods_refunded_cents: int  # refunds/chargebacks that reversed goods
    customer_money_cents: int  # captured beyond the order total, still owed back


def split_order_money(
    *, order_gross_cents: int, captures_cents: int, reversals_cents: int
) -> OrderMoney:
    """Captures beyond the order total are customer money (a duplicate
    charge), not vendor sales. A later refund/chargeback first returns that
    excess; only the remainder reverses goods. A refund ORDER (negative
    gross, lane B's representation) is all goods reversal.

    All inputs are non-negative except `order_gross_cents`."""
    if order_gross_cents < 0:
        return OrderMoney(
            gross_net_cents=captures_cents - reversals_cents,
            goods_cents=-reversals_cents,
            goods_refunded_cents=reversals_cents,
            customer_money_cents=0,
        )
    captured_goods = min(captures_cents, order_gross_cents)
    over = captures_cents - captured_goods
    excess_returned = min(reversals_cents, over)
    goods_refunded = reversals_cents - excess_returned
    return OrderMoney(
        gross_net_cents=captures_cents - reversals_cents,
        goods_cents=captured_goods - goods_refunded,
        goods_refunded_cents=goods_refunded,
        customer_money_cents=over - excess_returned,
    )


def processor_settlement(*, bank_cents: int, fee_cents: int, gross_cents: int) -> list[Line]:
    """A processor deposit clears `gross` out of processor clearing: `bank`
    arrives in the bank account and the processor kept `fee`."""
    return build([("1000", bank_cents, 0), ("5100", fee_cents, 0), ("1100", 0, gross_cents)])


def commission_settlement(
    *, assessed_cents: int, collected_from_payable_cents: int, receivable_cents: int
) -> list[Line]:
    """Assessed commission is organizer revenue. What was collected out of
    centrally held vendor funds reduces the vendor payable; anything not
    collectible becomes a vendor receivable (unpaid balance) -- never taken
    from tax or tips, which this rule cannot touch."""
    return build(
        [
            ("2000", collected_from_payable_cents, 0),
            ("1200", receivable_cents, 0),
            ("4000", 0, assessed_cents),
        ]
    )


def processor_fee_recharge(amount_cents: int) -> list[Line]:
    """A processor fee the vendor contract lets the organizer deduct."""
    return build([("2000", amount_cents, 0), ("5100", 0, amount_cents)])


def vendor_payout(amount_cents: int) -> list[Line]:
    """Simulated payout, posted only once CONFIRMED."""
    return build([("2000", amount_cents, 0), ("1000", 0, amount_cents)])


def ticket_settlement(
    *, gross_cents: int, fee_cents: int, net_cents: int, deferred: bool
) -> list[Line]:
    revenue = "2400" if deferred else "4100"
    return build([("1000", net_cents, 0), ("5110", fee_cents, 0), (revenue, 0, gross_cents)])


def ticket_recognition(amount_cents: int) -> list[Line]:
    return build([("2400", amount_cents, 0), ("4100", 0, amount_cents)])


def sponsorship_receipt(amount_cents: int) -> list[Line]:
    return build([("1000", amount_cents, 0), ("4200", 0, amount_cents)])


def supplier_invoice(gross_cents: int) -> list[Line]:
    return build([("5300", gross_cents, 0), ("2300", 0, gross_cents)])


def supplier_credit_note(gross_cents: int) -> list[Line]:
    return build([("2300", gross_cents, 0), ("5300", 0, gross_cents)])


def supplier_payment(amount_cents: int) -> list[Line]:
    return build([("2300", amount_cents, 0), ("1000", 0, amount_cents)])


def operating_expense(amount_cents: int) -> list[Line]:
    return build([("5400", amount_cents, 0), ("1000", 0, amount_cents)])


def reversal(lines: list[Line]) -> list[Line]:
    """Exact mirror of an entry: the correcting half of a correction. The
    original entry is never touched."""
    return build([(ln.account_code, ln.credit_cents, ln.debit_cents) for ln in lines])


# ---------------------------------------------------------------------------
# Balances, trial balance, P&L
# ---------------------------------------------------------------------------


def balances(lines: Iterable[Line]) -> dict[str, int]:
    """Signed balance per account code, debit-positive."""
    bal = {code: 0 for code in ACCOUNTS}
    for line in lines:
        bal[line.account_code] += line.debit_cents - line.credit_cents
    return bal


def trial_balance(bal: dict[str, int]) -> dict:
    rows = []
    total_dr = total_cr = 0
    for code in sorted(ACCOUNTS):
        b = bal.get(code, 0)
        role, name, category = ACCOUNTS[code]
        dr, cr = max(b, 0), max(-b, 0)
        total_dr += dr
        total_cr += cr
        rows.append(
            {
                "account_code": code,
                "account_role": role,
                "name": name,
                "category": category,
                "debit_cents": dr,
                "credit_cents": cr,
            }
        )
    return {
        "rows": rows,
        "total_debit_cents": total_dr,
        "total_credit_cents": total_cr,
        "balanced": total_dr == total_cr,
    }


def profit_and_loss(bal: dict[str, int]) -> dict:
    """Organizer P&L. Revenue is credit-normal, expense debit-normal; vendor
    gross sales never appear here (they live in VENDOR_FUNDS_PAYABLE)."""
    revenue = []
    expenses = []
    for code in sorted(ACCOUNTS):
        role, name, category = ACCOUNTS[code]
        b = bal.get(code, 0)
        row = {"account_code": code, "account_role": role, "name": name}
        if category == "REVENUE":
            revenue.append({**row, "amount_cents": -b})
        elif category == "EXPENSE":
            expenses.append({**row, "amount_cents": b})
    total_revenue = sum(r["amount_cents"] for r in revenue)
    total_expenses = sum(e["amount_cents"] for e in expenses)
    return {
        "revenue": revenue,
        "expenses": expenses,
        "total_revenue_cents": total_revenue,
        "total_expenses_cents": total_expenses,
        "net_income_cents": total_revenue - total_expenses,
    }
