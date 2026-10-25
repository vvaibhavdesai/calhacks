"""A10 operations endpoints (read-only; see app/ops.py).

- `GET /api/v1/ops/alerts`: JSON alert summary for any signed-in user.
- `GET /api/v1/ops/audit-chain`: full recomputation of the per-stream audit
  hash chain (see app/audit_chain.py). Gated on CAPABILITY_REPLAY -- it is
  the same act as a replay (re-walk the recorded history and compare), and
  it is O(all history), so it is deliberately not on the alerts path that
  a scraper hits every few seconds.
- `GET /metrics`: Prometheus text exposition. Disabled (404) unless
  OPS_METRICS_TOKEN is configured; the scraper must then send
  `Authorization: Bearer <OPS_METRICS_TOKEN>`. It is never open by default
  because counts of UNKNOWN payouts and spend are business information.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.audit_chain import report_to_json, verify_chain
from app.auth import get_current_user, require_capability
from app.config import get_settings
from app.db import get_db
from app.models import CAPABILITY_REPLAY, User
from app.ops import collect_signals, signals_to_json, signals_to_prometheus

router = APIRouter(tags=["ops"])


# Every instant below is declared `str` rather than `datetime`, here and in
# the other routers typed in this pass: the handlers already emit
# `.isoformat()`, which keeps the `+00:00` offset, while a `datetime` field
# would make pydantic re-serialise it as `...Z`. Same instant, different
# bytes -- and screens compare these strings.


class AlertOut(BaseModel):
    """One firing alert (app/ops.py `Alert`)."""

    code: str
    severity: str  # "critical" | "warning"
    count: int
    detail: str


class OpsSignalsOut(BaseModel):
    """The raw gauges the alerts are derived from (app/ops.py `Signals`)."""

    runs_active: int
    runs_stalled: int
    outbox_pending: int
    outbox_pending_stale: int
    # Null when nothing is pending: there is no age to report.
    outbox_oldest_pending_age_seconds: float | None
    outbox_unconsumed_pending: int
    outbox_unknown: int
    outbox_failed: int
    payouts_unknown: int
    payouts_recovery_required: int
    claude_spent_24h_usd_estimate: float
    claude_daily_budget_usd: float
    # Null = no worker URL configured, so liveness was never checked.
    # That is NOT the same as a worker that is down (false).
    worker_up: bool | None
    audit_chain_head_mismatches: int
    audit_chain_head_streams: list[str]


class OpsAlertsOut(BaseModel):
    status: str  # "ok" | "alerting"
    checked_at: str
    alerts: list[AlertOut]
    signals: OpsSignalsOut


class ChainDivergenceOut(BaseModel):
    """One hash/linkage/sequence divergence (app/audit_chain.py `Divergence`)."""

    stream_id: str
    sequence: int
    event_id: str | None
    kind: str
    expected: str | None
    stored: str | None
    detail: str


class ChainStreamOut(BaseModel):
    stream_id: str
    ok: bool
    events: int
    # Null for a stream with no events in scope.
    first_sequence: int | None
    last_sequence: int | None
    sequence_contiguous: bool
    linkage_intact: bool
    hashes_intact: bool
    first_divergent_sequence: int | None
    divergences_truncated: bool
    divergences: list[ChainDivergenceOut]


class ChainHeadFindingOut(BaseModel):
    """A `stream_heads` pointer that disagrees with the events that exist."""

    stream_id: str
    kind: str
    expected: str | None
    stored: str | None
    detail: str


class ChainSummaryOut(BaseModel):
    streams_checked: int
    streams_diverged: int
    events_checked: int
    hash_mismatches: int
    broken_links: int
    sequence_gaps: int
    head_findings: int
    divergences_truncated: bool


class AuditChainOut(BaseModel):
    status: str  # "ok" | "diverged"
    checked_at: str
    summary: ChainSummaryOut
    # Intact streams are omitted unless `include_ok_streams` asks for them.
    streams: list[ChainStreamOut]
    head_findings: list[ChainHeadFindingOut]


@router.get("/api/v1/ops/alerts", response_model=OpsAlertsOut)
def ops_alerts(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    return signals_to_json(collect_signals(db, get_settings()))


@router.get("/api/v1/ops/audit-chain", response_model=AuditChainOut)
def ops_audit_chain(
    stream_id: str | None = Query(default=None, description="verify only this stream"),
    stream_prefix: str | None = Query(
        default=None, description='verify only streams starting with this, e.g. "settlement:"'
    ),
    include_ok_streams: bool = Query(default=False),
    db: Session = Depends(get_db),
    user: User = Depends(require_capability(CAPABILITY_REPLAY)),
) -> dict:
    """Recompute the chain and report where (if anywhere) it diverges.

    `SET TRANSACTION READ ONLY` for the same reason app/replay.py uses it:
    a verifier that could write is not a verifier. Nothing here repairs a
    divergence -- the response names the first divergent sequence per
    stream and leaves the decision to a human.
    """
    # The capability dependency has already queried on this session, so a
    # transaction is open and Postgres would refuse SET TRANSACTION. End it
    # first (nothing is pending on a GET); the next execute starts a fresh
    # transaction with SET TRANSACTION as its first statement.
    db.rollback()
    db.execute(text("SET TRANSACTION READ ONLY"))
    report = verify_chain(
        db,
        stream_ids=[stream_id] if stream_id else None,
        stream_prefix=stream_prefix,
    )
    return report_to_json(report, include_ok_streams=include_ok_streams)


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(
    db: Session = Depends(get_db), authorization: str | None = Header(default=None)
) -> PlainTextResponse:
    token = get_settings().ops_metrics_token
    if not token:
        raise HTTPException(status_code=404, detail="metrics disabled")
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied.encode(), token.encode()):
        raise HTTPException(status_code=401, detail="invalid metrics token")
    body = signals_to_prometheus(collect_signals(db, get_settings()))
    return PlainTextResponse(body, media_type="text/plain; version=0.0.4")
