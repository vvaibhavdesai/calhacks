"""Festival reads, and the one write that registers a festival.

Why this router is the unblocking one: roughly eighteen existing routes
require `festival_id` as a query parameter (`/cash`, `/journal`,
`/invoices`, `/suppliers`, `/purchase-orders`, `/reports/tax`,
`/reports/pnl`, `/reports/festival`, `/close`, `/close/checks`,
`/finance/config`, `/finance/cash-office`, `/finance/chargeback-fees`,
`/finance/cash-cases`, `/history/snapshots`, `/history/verify`,
`/lineage/tax-total`, `/cash-pool/{festival_id}`) and nothing exposed the
festival, so no caller could discover a value to send. These two reads are
that discovery.

Capabilities follow A01's `require_capability` unchanged, and the split
matches A08's finance router exactly: a read needs any authenticated user,
and registering a festival needs `close_books` -- the same capability that
writes `PUT /finance/config`, which is the neighbouring decision about a
festival's timezone and schedule. No new capability was added.

`PUT /festivals/{festival_id}` is the only write here. It exists because a
registry with no writer could never hold anything: the festival record has
to be creatable for the reads below to serve a real name, status and date
range rather than only the derived view. It is an upsert keyed on the
festival_id, so it is idempotent and can never mint a second festival for
the same id.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import app.festivals as festivals
from app.auth import get_current_user, require_capability
from app.db import get_db
from app.models import CAPABILITY_CLOSE_BOOKS, User

router = APIRouter(prefix="/api/v1/festivals", tags=["festivals"])

# The UI asks "which festival am I operating?" with no id to give, so it needs a
# singular route. Kept as its own router because the collection above is
# plural-prefixed, not because it is a different resource.
current_router = APIRouter(prefix="/api/v1/festival", tags=["festivals"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class BankAccountOut(BaseModel):
    id: str
    name: str
    # "opening_balance" -> an `opening_balances` row (so the opening figure
    # and its source refs are real); "bank_entry" -> the account was only
    # ever seen as `bank_entries.account_id`, so nothing more is known.
    source: str
    opening_as_of: date | None
    opening_balance_cents: int | None
    source_refs: list


class FestivalOut(BaseModel):
    id: str
    name: str
    organizer_name: str | None
    status: str
    timezone: str
    currency: str
    # Null only on a derived festival with no dated records anywhere -- a
    # registered one always has both.
    start_date: date | None
    end_date: date | None
    business_dates: list[date]
    bank_accounts: list[BankAccountOut]
    # The ACTIVE `finance_configs` row every report pins; null until one
    # exists (GET /finance/config creates it on first read).
    business_config_id: str | None
    fictional_notice: str | None
    # False = no `festivals` row; the record was derived from the data that
    # already carries this festival_id. See app/festivals.py.
    registered: bool
    # 0 on a derived record: there is no stored aggregate to version.
    version: int


class FestivalListOut(BaseModel):
    items: list[FestivalOut]
    next_cursor: str | None


def _bank_out(account: festivals.BankAccountView) -> BankAccountOut:
    return BankAccountOut(
        id=account.id,
        name=account.name,
        source=account.source,
        opening_as_of=account.opening_as_of,
        opening_balance_cents=account.opening_balance_cents,
        source_refs=account.source_refs,
    )


def _festival_out(view: festivals.FestivalView) -> FestivalOut:
    return FestivalOut(
        id=view.id,
        name=view.name,
        organizer_name=view.organizer_name,
        status=view.status,
        timezone=view.timezone,
        currency=view.currency,
        start_date=view.start_date,
        end_date=view.end_date,
        business_dates=view.business_dates,
        bank_accounts=[_bank_out(a) for a in view.bank_accounts],
        business_config_id=view.business_config_id,
        fictional_notice=view.fictional_notice,
        registered=view.registered,
        version=view.version,
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@router.get("", response_model=FestivalListOut)
def list_festivals(
    registered_only: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FestivalListOut:
    """Every festival that can be resolved, by id. `next_cursor` is always
    null: a festival is a workspace-level record (there are a handful, not a
    stream of them), so the envelope keeps the shape every other list here
    uses without pretending to page."""
    views = festivals.list_festivals(db)
    if registered_only:
        views = [v for v in views if v.registered]
    return FestivalListOut(items=[_festival_out(v) for v in views], next_cursor=None)


@router.get("/{festival_id}", response_model=FestivalOut)
def get_festival(
    festival_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FestivalOut:
    view = festivals.get_festival(db, festival_id)
    if view is None:
        raise HTTPException(status_code=404, detail="festival not found")
    return _festival_out(view)


@current_router.get("", response_model=FestivalOut)
def get_current_festival(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FestivalOut:
    """The festival this deployment operates.

    A registered festival wins: registering one is the explicit statement of
    which festival this is, and a dev or test database can hold dozens of
    derived ids from fixtures. With exactly one registered we return it; with
    several, this is ambiguous and the caller has to name one, so we say so
    rather than guessing. With none registered we fall back to the derived
    record carrying the most business dates, so a fresh database still answers.
    """
    views = festivals.list_festivals(db)
    registered = [v for v in views if v.registered]
    if len(registered) == 1:
        return _festival_out(registered[0])
    if len(registered) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "festival_ambiguous",
                "message": "several festivals are registered; call /api/v1/festivals/{festival_id}",
                "festival_ids": [v.id for v in registered],
            },
        )
    if not views:
        raise HTTPException(status_code=404, detail="no festival found")
    best = max(views, key=lambda v: (len(v.business_dates or []), v.id))
    return _festival_out(best)


# ---------------------------------------------------------------------------
# Registration (upsert on the festival_id -- see the module docstring)
# ---------------------------------------------------------------------------


class FestivalIn(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    start_date: date
    end_date: date
    organizer_name: str | None = Field(default=None, max_length=300)
    status: str = Field(default="ACTIVE", max_length=20)
    timezone: str = Field(default="America/Los_Angeles", max_length=64)
    # C01 fixes v1 to USD; the column exists so a second currency is a data
    # change rather than a schema change, and anything else is refused here.
    currency: str = Field(default="USD", pattern=r"^USD$")
    fictional_notice: str | None = None
    expected_version: int | None = None


@router.put("/{festival_id}", response_model=FestivalOut)
def register_festival(
    festival_id: str,
    body: FestivalIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_CLOSE_BOOKS)),
) -> FestivalOut:
    # The id is the primary key and every festival_id column is String(64),
    # so refuse an over-long id here rather than letting Postgres truncate
    # or 500 on the INSERT.
    if not festival_id.strip() or len(festival_id) > 64:
        raise HTTPException(
            status_code=422,
            detail={"code": "festival_id_invalid", "details": {"max_length": 64}},
        )
    try:
        festivals.register_festival(
            db,
            festival_id=festival_id,
            name=body.name,
            start_date=body.start_date,
            end_date=body.end_date,
            status=body.status,
            timezone=body.timezone,
            currency=body.currency,
            organizer_name=body.organizer_name,
            fictional_notice=body.fictional_notice,
            expected_version=body.expected_version,
            actor_id=user.id,
            actor_capability=CAPABILITY_CLOSE_BOOKS,
        )
    except festivals.FestivalConflictError as exc:
        raise HTTPException(
            status_code=409, detail={"code": exc.code, "details": exc.details}
        ) from exc
    db.commit()
    # The row exists now, so the derived branch of get_festival cannot be
    # taken and the read back always reflects what was just committed.
    return _festival_out(festivals.get_festival(db, festival_id))
