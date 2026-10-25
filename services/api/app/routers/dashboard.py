"""One festival-level read for the operator's landing screen.

Why this exists as its own endpoint rather than the screen assembling it
from the collections it already has: the landing screen needs four
festival-wide aggregates (settled money per night, custody split, the
largest vendors, and how much of the exception load resolved itself).
Deriving those in the client means paging 240 settlements and 1,000+ cases
on every load and summing them in the browser, which both makes the figures
slow and -- more importantly -- puts a second, divergent derivation of the
money next to A04's. Everything here is one SQL pass per aggregate, so a
figure on the dashboard and the same figure on a detail screen come from
the same rows.

Two deliberate choices about WHICH settlement rows count:

1. A row that another row supersedes via `correction_of` is excluded, which
   is the same "live rows only" rule the settled totals use everywhere
   else.

2. Among the surviving rows there can still be more than one per
   (vendor, business_date): a settlement calculated twice for the same
   vendor-night leaves two identical FINAL rows, neither marked as
   correcting the other (this is real in the demo data -- one vendor on
   2026-10-17). Summing both double-counts that vendor, so this read keeps
   exactly one row per vendor-night, newest first. It is a read-side guard,
   not a repair: the duplicate row is still in `settlements`, and
   `duplicate_rows_excluded` reports how many this read dropped so the
   number is visible rather than silently corrected.

Custody comes from `finance_reports.cash_report`, unchanged, so the
dashboard's cash figures are the same object `GET /cash` serves.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

import app.finance_reports as reports
from app.auth import get_current_user
from app.db import get_db
from app.models import User

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])

# One row per vendor-night: live rows only (nothing supersedes them), then
# the newest row per vendor-night. `version`/`created_at` order is the same
# tie-break the settlement detail uses when it shows "latest calculation".
_LIVE_SETTLEMENTS = """
WITH live AS (
    SELECT s.*
      FROM settlements s
     WHERE s.festival_id = :festival_id
       AND NOT EXISTS (SELECT 1 FROM settlements o WHERE o.correction_of = s.id)
), ranked AS (
    SELECT live.*,
           ROW_NUMBER() OVER (
               PARTITION BY vendor_id, business_date
               ORDER BY version DESC, created_at DESC
           ) AS rn
      FROM live
)
"""


class NightOut(BaseModel):
    business_date: date
    vendors: int
    eligible_sales_cents: int
    commission_cents: int
    payout_cents: int


class TopVendorOut(BaseModel):
    vendor_id: str
    name: str | None
    nights: int
    eligible_sales_cents: int
    commission_cents: int
    payout_cents: int
    # Commission actually assessed over eligible sales, in basis points.
    # Above the headline rate wherever a vendor hit the festival minimum.
    effective_rate_bps: int | None


class ReasonOut(BaseModel):
    reason_code: str
    open_cases: int
    auto_resolved: int
    human_resolved: int


class AutonomyOut(BaseModel):
    total_cases: int
    open_cases: int
    auto_resolved: int
    human_resolved: int
    # Of the cases that reached a resolution, the share a policy resolved
    # with no human in the loop. Null while nothing has resolved at all --
    # a rate over zero resolutions would be an invented 0% or 100%.
    autonomy_rate_bps: int | None
    by_reason: list[ReasonOut]


class TotalsOut(BaseModel):
    settlements: int
    vendors: int
    eligible_sales_cents: int
    commission_cents: int
    payout_cents: int
    effective_rate_bps: int | None


class DashboardOut(BaseModel):
    festival_id: str
    as_of: date
    totals: TotalsOut
    nights: list[NightOut]
    top_vendors: list[TopVendorOut]
    autonomy: AutonomyOut
    custody: dict
    duplicate_rows_excluded: int
    labels: list[str]


def _rate_bps(commission: int, eligible: int) -> int | None:
    if eligible <= 0:
        return None
    return round(commission * 10_000 / eligible)


@router.get("", response_model=DashboardOut)
def get_dashboard(
    festival_id: str,
    as_of: date | None = None,
    top: int = Query(8, ge=1, le=50),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DashboardOut:
    as_of_ = as_of or date.today()
    p = {"festival_id": festival_id}

    nights = [
        NightOut(
            business_date=r.business_date,
            vendors=r.vendors,
            eligible_sales_cents=r.eligible or 0,
            commission_cents=r.commission or 0,
            payout_cents=r.payout or 0,
        )
        for r in db.execute(
            text(
                _LIVE_SETTLEMENTS
                + """
                SELECT business_date,
                       COUNT(DISTINCT vendor_id) AS vendors,
                       SUM(eligible_sales_cents) AS eligible,
                       SUM(assessed_fee_cents)   AS commission,
                       SUM(payout_cents)         AS payout
                  FROM ranked WHERE rn = 1
                 GROUP BY business_date ORDER BY business_date
                """
            ),
            p,
        ).all()
    ]

    totals_row = db.execute(
        text(
            _LIVE_SETTLEMENTS
            + """
            SELECT COUNT(*) FILTER (WHERE rn = 1) AS n,
                   COUNT(DISTINCT vendor_id) FILTER (WHERE rn = 1) AS vendors,
                   COALESCE(SUM(eligible_sales_cents) FILTER (WHERE rn = 1), 0) AS eligible,
                   COALESCE(SUM(assessed_fee_cents)   FILTER (WHERE rn = 1), 0) AS commission,
                   COALESCE(SUM(payout_cents)         FILTER (WHERE rn = 1), 0) AS payout,
                   COUNT(*) FILTER (WHERE rn > 1)                               AS dropped
              FROM ranked
            """
        ),
        p,
    ).one()
    # Every aggregate above is filtered to rn = 1, so these are the kept
    # rows already; `dropped` counts the duplicate vendor-nights left out.
    kept = totals_row.n

    top_vendors = [
        TopVendorOut(
            vendor_id=r.vendor_id,
            name=r.name,
            nights=r.nights,
            eligible_sales_cents=r.eligible or 0,
            commission_cents=r.commission or 0,
            payout_cents=r.payout or 0,
            effective_rate_bps=_rate_bps(r.commission or 0, r.eligible or 0),
        )
        for r in db.execute(
            text(
                _LIVE_SETTLEMENTS
                + """
                SELECT r.vendor_id,
                       v.name,
                       COUNT(*) AS nights,
                       SUM(r.eligible_sales_cents) AS eligible,
                       SUM(r.assessed_fee_cents)   AS commission,
                       SUM(r.payout_cents)         AS payout
                  FROM ranked r
                  LEFT JOIN vendors v ON v.id = r.vendor_id
                 WHERE r.rn = 1
                 GROUP BY r.vendor_id, v.name
                 ORDER BY eligible DESC NULLS LAST
                 LIMIT :top
                """
            ),
            {**p, "top": top},
        ).all()
    ]

    # Cases are festival-scoped through the vendor; a case with no vendor
    # (a festival-level one, e.g. an unmatched bank entry) still counts.
    case_rows = db.execute(
        text(
            """
            SELECT c.reason_code,
                   c.status,
                   c.auto_resolved,
                   COUNT(*) AS n
              FROM cases c
              LEFT JOIN vendors v ON v.id = c.vendor_id
             WHERE v.festival_id = :festival_id OR c.vendor_id IS NULL
             GROUP BY c.reason_code, c.status, c.auto_resolved
            """
        ),
        p,
    ).all()

    by_reason: dict[str, dict[str, int]] = {}
    total = open_cases = auto = human = 0
    for r in case_rows:
        slot = by_reason.setdefault(
            r.reason_code, {"open_cases": 0, "auto_resolved": 0, "human_resolved": 0}
        )
        total += r.n
        if r.status == "OPEN":
            open_cases += r.n
            slot["open_cases"] += r.n
        elif r.auto_resolved:
            auto += r.n
            slot["auto_resolved"] += r.n
        else:
            human += r.n
            slot["human_resolved"] += r.n

    resolved = auto + human
    autonomy = AutonomyOut(
        total_cases=total,
        open_cases=open_cases,
        auto_resolved=auto,
        human_resolved=human,
        autonomy_rate_bps=(round(auto * 10_000 / resolved) if resolved else None),
        by_reason=sorted(
            (ReasonOut(reason_code=k, **v) for k, v in by_reason.items()),
            key=lambda x: -(x.open_cases + x.auto_resolved + x.human_resolved),
        ),
    )

    custody = reports.cash_report(db, festival_id=festival_id, as_of=as_of_)
    db.commit()  # cash_report may create the default finance config version

    return DashboardOut(
        festival_id=festival_id,
        as_of=as_of_,
        totals=TotalsOut(
            settlements=kept,
            vendors=totals_row.vendors,
            eligible_sales_cents=totals_row.eligible,
            commission_cents=totals_row.commission,
            payout_cents=totals_row.payout,
            effective_rate_bps=_rate_bps(totals_row.commission, totals_row.eligible),
        ),
        nights=nights,
        top_vendors=top_vendors,
        autonomy=autonomy,
        custody=custody,
        duplicate_rows_excluded=totals_row.dropped,
        labels=[
            "Simulated payouts and supplier payments - no real money movement.",
            "Settled figures exclude superseded rows and keep one settlement per vendor-night.",
        ],
    )
