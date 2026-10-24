"""A08 draft tax summaries: pure recalculation and aggregation.

DRAFT, NOT FILED. Rates, taxable categories, exemptions and who owes the
tax are explicit, versioned DEMO configuration (C05: "actual fictional
locality/rate, exemptions, tax remitter ... not yet approved/specified").
Nothing here asserts compliance with any real tax law.

C05 rules implemented:
  - supplied tax is stored and reported separately from recalculated
    expected tax; a discrepancy is shown, never "fixed" by rewriting the
    supplied number;
  - refunds carry negative eligible/tax amounts and reduce the same
    business date's totals they are recorded on; a refund line inherits
    the original's charged tax as its expected tax (any variance is
    reported once, on the original sale line);
  - exempt categories are reported as exempt sales, with zero expected tax;
  - liability owner is explicit per custody: tax collected centrally is
    held by the organizer as agent; tax on vendor-retained sales is the
    vendor's (reported as a memo line, not an organizer liability).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

STATUS_LABEL = "DRAFT, NOT FILED - DEMO configuration, no compliance claim"

DEFAULT_TAX_CONFIG = {
    "label": "DEMO fictional California-style rate; not a real jurisdiction's rate",
    "rates_by_category": {"prepared_food": "0.0925", "packaged_food": "0", "merch": "0.0925"},
    "exempt_categories": ["packaged_food"],
    "liability_owner_by_custody": {
        "central": "ORGANIZER_AS_AGENT",
        "vendor_retained": "VENDOR",
    },
}


def recalc_line_tax(eligible_net_cents: int, rate: Decimal) -> int:
    """ROUND_HALF_UP to cents at the line boundary (C01 proposal). Works
    for negative (refund) lines symmetrically."""
    sign = -1 if eligible_net_cents < 0 else 1
    amount = (Decimal(abs(eligible_net_cents)) * rate).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    return sign * int(amount)


@dataclass(frozen=True)
class TaxLine:
    business_date: date
    order_line_ref: str
    custody: str  # "central" | "vendor_retained"
    tax_category: str | None
    eligible_net_cents: int
    supplied_tax_cents: int
    # A refund/reversal line returns the tax actually charged on the
    # original sale (C05: "refunds refer to original tax"). It is not
    # recalculated independently -- doing so invents one-cent "mismatches"
    # on proportional partial refunds -- and any variance is reported once,
    # on the original line.
    inherits_supplied: bool = False


def summarize(lines: Iterable[TaxLine], config: dict) -> dict:
    rates = {k: Decimal(v) for k, v in config["rates_by_category"].items()}
    exempt = set(config.get("exempt_categories", []))
    owners = config["liability_owner_by_custody"]

    by_date: dict[date, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    mismatches = []
    for ln in lines:
        row = by_date[ln.business_date]
        prefix = "central" if ln.custody == "central" else "vendor_retained"
        row[f"{prefix}_supplied_tax_cents"] += ln.supplied_tax_cents
        if ln.tax_category is None or ln.tax_category not in rates:
            row[f"{prefix}_unclassified_sales_cents"] += ln.eligible_net_cents
            continue
        if ln.tax_category in exempt:
            row[f"{prefix}_exempt_sales_cents"] += ln.eligible_net_cents
        else:
            row[f"{prefix}_taxable_sales_cents"] += ln.eligible_net_cents
        if ln.inherits_supplied:
            expected = ln.supplied_tax_cents
        elif ln.tax_category in exempt:
            expected = 0
        else:
            expected = recalc_line_tax(ln.eligible_net_cents, rates[ln.tax_category])
        row[f"{prefix}_recalculated_tax_cents"] += expected
        if expected != ln.supplied_tax_cents:
            row[f"{prefix}_variance_cents"] += ln.supplied_tax_cents - expected
            mismatches.append(
                {
                    "order_line_ref": ln.order_line_ref,
                    "business_date": ln.business_date.isoformat(),
                    "custody": ln.custody,
                    "tax_category": ln.tax_category,
                    "supplied_tax_cents": ln.supplied_tax_cents,
                    "recalculated_tax_cents": expected,
                }
            )

    rows = []
    for d in sorted(by_date):
        row = dict(by_date[d])
        rows.append(
            {
                "business_date": d.isoformat(),
                "organizer_liability_owner": owners.get("central"),
                "vendor_retained_liability_owner": owners.get("vendor_retained"),
                **{k: row[k] for k in sorted(row)},
            }
        )
    central_supplied = sum(r.get("central_supplied_tax_cents", 0) for r in rows)
    return {
        "status": STATUS_LABEL,
        "config_label": config.get("label"),
        "by_business_date": rows,
        "totals": {
            "central_supplied_tax_cents": central_supplied,
            "central_recalculated_tax_cents": sum(
                r.get("central_recalculated_tax_cents", 0) for r in rows
            ),
            "vendor_retained_supplied_tax_memo_cents": sum(
                r.get("vendor_retained_supplied_tax_cents", 0) for r in rows
            ),
        },
        "mismatch_lines": mismatches,
    }
