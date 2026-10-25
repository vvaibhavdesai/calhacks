"""Festival reads: the registered record, plus the derived record for a
`festival_id` that exists in the data but has never been registered.

The second half is the point. `festival_id` values were minted ad hoc long
before `app/models_festival.py` existed (see that module's docstring), so a
read that only served registered rows would leave every fixture festival --
and every festival a demo imported last week -- invisible, and the ~18
festival-scoped routes unreachable for them. So:

  - a registered festival is served from its own row, and
  - any `festival_id` observed on a festival-scoped table is served as a
    derived record with `registered=False`.

A derived record never invents a fact. Its `name` is the id itself (nobody
ever told us a name), `organizer_name`/`fictional_notice` are null, its
timezone comes from the festival's ACTIVE `finance_configs` row when there
is one, its currency is C01's fixed v1 USD, and its dates are only the
business dates that actually have records. `version` is 0, which is not a
real aggregate version -- it marks "no stored record", so a caller cannot
mistake a derived view for something it can send back as
`expected_version`.

Nothing here writes, except `register_festival`, which is the only way a
row gets into `festivals` and which does not commit (its caller does).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from domain import accounting as acct
from sqlalchemy import select, union
from sqlalchemy.orm import Session

from app.finance_records import DEFAULT_SCHEDULE
from app.models_cash_office import CashOfficeCount
from app.models_festival import Festival
from app.models_finance import DailyClose, FinanceConfig, OpeningBalance
from app.models_ledger import Vendor
from app.models_orchestration import OrchestrationRun
from app.models_payout_dispatch import CashPool
from app.models_reconciliation import BankEntry
from app.models_settlement import Settlement
from app.uow import UnitOfWork

# C01: currency is fixed to USD for v1. Used only for a DERIVED record --
# a registered festival carries its own column.
DEFAULT_CURRENCY = "USD"
DEFAULT_TIMEZONE = DEFAULT_SCHEDULE["timezone"]
DERIVED_STATUS = "UNREGISTERED"

# Every table that carries a festival_id and is a reliable sign that the
# festival exists. Deliberately not every such table: `suppliers`,
# `supplier_invoices` and `journal_entries` hold it NULLABLE (migration
# 0008 added it later), and the ones below already cover anything those
# could reference.
_ID_SOURCES = (
    Vendor.festival_id,
    Settlement.festival_id,
    FinanceConfig.festival_id,
    BankEntry.festival_id,
    DailyClose.festival_id,
    CashOfficeCount.festival_id,
    OrchestrationRun.festival_id,
    CashPool.festival_id,
    OpeningBalance.festival_id,
)

# Where an observed business date can come from. One row per (festival,
# date) after the UNION dedupes.
_DATE_SOURCES = (
    (Settlement.festival_id, Settlement.business_date),
    (DailyClose.festival_id, DailyClose.business_date),
    (BankEntry.festival_id, BankEntry.business_date),
    (CashOfficeCount.festival_id, CashOfficeCount.business_date),
)

# Chart roles (packages/domain/domain/accounting.py) that are bank accounts
# rather than cash on hand or a clearing account. Read from the chart so a
# new BANK_* role is picked up without editing this list.
_BANK_ROLES = tuple(sorted(role for role in acct.CODE_BY_ROLE if role.startswith("BANK")))
_CHART_NAME_BY_ROLE = {role: name for (role, name, _category) in acct.ACCOUNTS.values()}


@dataclass(frozen=True)
class BankAccountView:
    """`source` says where the account came from: "opening_balance" (an
    `opening_balances` row, so it has an opening figure and source refs) or
    "bank_entry" (seen only as `bank_entries.account_id`, so all we honestly
    know is that money moved through it)."""

    id: str
    name: str
    source: str
    opening_as_of: date | None = None
    opening_balance_cents: int | None = None
    source_refs: list = field(default_factory=list)


@dataclass(frozen=True)
class FestivalView:
    id: str
    name: str
    organizer_name: str | None
    status: str
    timezone: str
    currency: str
    start_date: date | None
    end_date: date | None
    business_dates: list[date]
    bank_accounts: list[BankAccountView]
    business_config_id: str | None
    fictional_notice: str | None
    registered: bool
    version: int


def _date_range(first: date, last: date) -> list[date]:
    if last < first:
        return [first]
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def observed_festival_ids(db: Session, *, only: str | None = None) -> set[str]:
    """Every festival_id that appears anywhere in the data, registered or
    not. UNION (not UNION ALL) so each id is returned once. `only` narrows
    every branch to one id so the single-festival read is nine indexed
    lookups rather than nine full scans."""
    selects = []
    for column in _ID_SOURCES:
        stmt = select(column.label("festival_id"))
        selects.append(stmt if only is None else stmt.where(column == only))
    return {row for (row,) in db.execute(union(*selects)).all() if row}


def _observed_business_dates(db: Session, ids: set[str]) -> dict[str, list[date]]:
    if not ids:
        return {}
    stmt = union(
        *[
            select(festival_column.label("festival_id"), date_column.label("business_date")).where(
                festival_column.in_(ids)
            )
            for festival_column, date_column in _DATE_SOURCES
        ]
    )
    found: dict[str, set[date]] = {}
    for festival_id, business_date in db.execute(stmt).all():
        found.setdefault(festival_id, set()).add(business_date)
    return {festival_id: sorted(dates) for festival_id, dates in found.items()}


def _bank_accounts(db: Session, ids: set[str]) -> dict[str, list[BankAccountView]]:
    """Bank accounts as the data already records them -- opening balances
    first (they carry an opening figure and source refs), then any account
    seen on a bank entry that no opening balance covers."""
    if not ids:
        return {}
    accounts: dict[str, list[BankAccountView]] = {}
    openings = (
        db.execute(
            select(OpeningBalance)
            .where(
                OpeningBalance.festival_id.in_(ids),
                OpeningBalance.account_role.in_(_BANK_ROLES),
            )
            .order_by(OpeningBalance.festival_id, OpeningBalance.account_role)
        )
        .scalars()
        .all()
    )
    for opening in openings:
        accounts.setdefault(opening.festival_id, []).append(
            BankAccountView(
                id=opening.account_role,
                name=_CHART_NAME_BY_ROLE.get(opening.account_role, opening.account_role),
                source="opening_balance",
                opening_as_of=opening.as_of,
                opening_balance_cents=opening.amount_cents,
                source_refs=list(opening.source_refs or []),
            )
        )
    seen = db.execute(
        select(BankEntry.festival_id, BankEntry.account_id)
        .where(BankEntry.festival_id.in_(ids))
        .distinct()
        .order_by(BankEntry.festival_id, BankEntry.account_id)
    ).all()
    for festival_id, account_id in seen:
        existing = accounts.setdefault(festival_id, [])
        if any(account.id == account_id for account in existing):
            continue
        existing.append(BankAccountView(id=account_id, name=account_id, source="bank_entry"))
    return accounts


def _active_configs(db: Session, ids: set[str]) -> dict[str, FinanceConfig]:
    if not ids:
        return {}
    rows = (
        db.execute(
            select(FinanceConfig)
            .where(FinanceConfig.festival_id.in_(ids), FinanceConfig.status == "ACTIVE")
            .order_by(FinanceConfig.festival_id, FinanceConfig.version)
        )
        .scalars()
        .all()
    )
    # Ascending version, so the last write per festival is the highest --
    # the same row `app.finance_records.active_config` would return.
    return {row.festival_id: row for row in rows}


def _view(
    festival_id: str,
    row: Festival | None,
    *,
    observed_dates: list[date],
    bank_accounts: list[BankAccountView],
    config: FinanceConfig | None,
) -> FestivalView:
    if row is None:
        return FestivalView(
            id=festival_id,
            name=festival_id,
            organizer_name=None,
            status=DERIVED_STATUS,
            timezone=config.timezone if config else DEFAULT_TIMEZONE,
            currency=DEFAULT_CURRENCY,
            start_date=observed_dates[0] if observed_dates else None,
            end_date=observed_dates[-1] if observed_dates else None,
            business_dates=observed_dates,
            bank_accounts=bank_accounts,
            business_config_id=config.id if config else None,
            fictional_notice=None,
            registered=False,
            version=0,
        )
    # The designed festival nights UNION the dates that actually have
    # records. The union matters both ways: the nights are listable before
    # any data lands, and a date with records outside the registered range
    # (fixtures use a deliberate pre-festival day) stays reachable, so the
    # screens that drive /close and /reports off this list cannot end up
    # unable to ask about a day the books already hold.
    business_dates = sorted(set(_date_range(row.start_date, row.end_date)) | set(observed_dates))
    return FestivalView(
        id=row.id,
        name=row.name,
        organizer_name=row.organizer_name,
        status=row.status,
        timezone=row.timezone,
        currency=row.currency,
        start_date=row.start_date,
        end_date=row.end_date,
        business_dates=business_dates,
        bank_accounts=bank_accounts,
        business_config_id=config.id if config else None,
        fictional_notice=row.fictional_notice,
        registered=True,
        version=row.version,
    )


def list_festivals(db: Session) -> list[FestivalView]:
    """Every festival the system can resolve, registered or derived, by id."""
    registered = {
        row.id: row for row in db.execute(select(Festival).order_by(Festival.id)).scalars()
    }
    ids = set(registered) | observed_festival_ids(db)
    dates = _observed_business_dates(db, ids)
    accounts = _bank_accounts(db, ids)
    configs = _active_configs(db, ids)
    return [
        _view(
            festival_id,
            registered.get(festival_id),
            observed_dates=dates.get(festival_id, []),
            bank_accounts=accounts.get(festival_id, []),
            config=configs.get(festival_id),
        )
        for festival_id in sorted(ids)
    ]


def get_festival(db: Session, festival_id: str) -> FestivalView | None:
    """The one festival, or None if the id is neither registered nor present
    anywhere in the data (which is what a 404 means for this entity -- an
    unregistered id that HAS data is a real festival, not a miss)."""
    row = db.get(Festival, festival_id)
    if row is None and not observed_festival_ids(db, only=festival_id):
        return None
    ids = {festival_id}
    return _view(
        festival_id,
        row,
        observed_dates=_observed_business_dates(db, ids).get(festival_id, []),
        bank_accounts=_bank_accounts(db, ids).get(festival_id, []),
        config=_active_configs(db, ids).get(festival_id),
    )


class FestivalConflictError(ValueError):
    def __init__(self, code: str, details: dict | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.details = details or {}


def register_festival(
    db: Session,
    *,
    festival_id: str,
    name: str,
    start_date: date,
    end_date: date,
    status: str,
    timezone: str,
    currency: str,
    organizer_name: str | None,
    fictional_notice: str | None,
    expected_version: int | None,
    actor_id: str,
    actor_capability: str,
) -> Festival:
    """Register `festival_id`, or update the registration. Idempotent on the
    id: re-sending the same body is a version bump, never a second festival,
    because the id IS the primary key.

    Does not commit (the router does), so the row and its audit event land
    in one transaction -- `app/uow.py`'s contract.
    """
    if end_date < start_date:
        raise FestivalConflictError(
            "festival_dates_reversed",
            {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        )
    row = db.get(Festival, festival_id)
    if row is not None and expected_version is not None and row.version != expected_version:
        raise FestivalConflictError(
            "version_conflict", {"expected": expected_version, "current": row.version}
        )
    if row is None:
        row = Festival(id=festival_id, version=1, created_by=actor_id)
        db.add(row)
        event_type = "festival.registered"
    else:
        row.version += 1
        event_type = "festival.updated"
    row.name = name
    row.organizer_name = organizer_name
    row.status = status
    row.timezone = timezone
    row.currency = currency
    row.start_date = start_date
    row.end_date = end_date
    row.fictional_notice = fictional_notice
    db.flush()
    UnitOfWork(db).append_event(
        stream_id=f"festival:{festival_id}",
        event_type=event_type,
        aggregate_id=festival_id,
        aggregate_version=row.version,
        actor_id=actor_id,
        actor_type="human",
        actor_capability=actor_capability,
        occurred_at=datetime.now(UTC),
        payload={
            "name": name,
            "status": status,
            "timezone": timezone,
            "currency": currency,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "version": row.version,
        },
    )
    return row
