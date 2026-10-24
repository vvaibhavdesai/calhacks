"""Terms-resolution interface used by the TERMS_RESOLVED milestone.

Deterministic terms resolution (turning code-verified document claims into
the vendor's applicable contract terms, or a case when they conflict) is
being built separately in `app/terms_resolution.py`. This module defines
the seam the nightly close calls, so the two can merge without editing the
graph:

    app.terms_resolution.resolve_vendor_day_terms(
        db, *, vendor, business_date, eligible_sales_cents, run_id,
        actor_id, actor_capability,
    ) -> object with
        status:   "RESOLVED" | "HELD"
        case_ids: list[str]      (cases that hold the vendor, if any)
        detail:   dict           (JSON-safe; term ids/versions used)

It must be idempotent per (vendor, business_date): re-running returns the
same cases instead of opening new ones, and it must never commit (the node
commits). Plain dicts with those keys are accepted too.

Until that module exists, the fallback is A05's existing deterministic
conflict detector (`app.policy.detect_contract_term_conflicts`), which is
idempotent per vendor/business_date and auto-resolves only through an
ACTIVE, human-approved policy.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy.orm import Session

from app.models_ledger import Vendor
from app.policy import detect_contract_term_conflicts
from app.uow import UnitOfWork

EXTERNAL_MODULE = "app.terms_resolution"
EXTERNAL_FUNCTION = "resolve_vendor_day_terms"


@dataclass
class TermsOutcome:
    status: str  # RESOLVED | HELD
    case_ids: list[str] = field(default_factory=list)
    source: str = "a05_conflict_detector"
    detail: dict = field(default_factory=dict)


def _external():
    try:
        module = importlib.import_module(EXTERNAL_MODULE)
    except ModuleNotFoundError as exc:
        if exc.name != EXTERNAL_MODULE:
            raise
        return None
    fn = getattr(module, EXTERNAL_FUNCTION, None)
    return fn if callable(fn) else None


def _adapt(raw) -> TermsOutcome:
    get = raw.get if isinstance(raw, dict) else lambda k, d=None: getattr(raw, k, d)
    status = get("status")
    if status not in ("RESOLVED", "HELD"):
        raise ValueError(f"terms resolution returned unknown status {status!r}")
    return TermsOutcome(
        status=status,
        case_ids=list(get("case_ids", []) or []),
        source=EXTERNAL_MODULE,
        detail=dict(get("detail", {}) or {}),
    )


def resolve_terms(
    db: Session,
    *,
    vendor: Vendor,
    business_date: date,
    eligible_sales_cents: int,
    run_id: str,
    actor_id: str,
    actor_capability: str,
) -> TermsOutcome:
    external = _external()
    if external is not None:
        return _adapt(
            external(
                db,
                vendor=vendor,
                business_date=business_date,
                eligible_sales_cents=eligible_sales_cents,
                run_id=run_id,
                actor_id=actor_id,
                actor_capability=actor_capability,
            )
        )
    case = detect_contract_term_conflicts(
        db,
        UnitOfWork(db),
        vendor=vendor,
        business_date_=business_date,
        eligible_sales_cents_today=eligible_sales_cents,
        run_id=run_id,
        actor_id=actor_id,
        actor_capability=actor_capability,
    )
    if case is None:
        return TermsOutcome(status="RESOLVED", detail={"conflict": False})
    if case.status == "OPEN":
        return TermsOutcome(status="HELD", case_ids=[case.id], detail={"conflict": True})
    return TermsOutcome(
        status="RESOLVED",
        detail={
            "conflict": True,
            "case_id": case.id,
            "case_status": case.status,
            "auto_resolved": bool(case.auto_resolved),
            "applied_policy_version_id": case.applied_policy_version_id,
        },
    )
