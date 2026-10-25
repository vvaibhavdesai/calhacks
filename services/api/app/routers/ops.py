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
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.audit_chain import report_to_json, verify_chain
from app.auth import get_current_user, require_capability
from app.config import get_settings
from app.db import get_db
from app.models import CAPABILITY_REPLAY, User
from app.ops import collect_signals, signals_to_json, signals_to_prometheus

router = APIRouter(tags=["ops"])


@router.get("/api/v1/ops/alerts")
def ops_alerts(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    return signals_to_json(collect_signals(db, get_settings()))


@router.get("/api/v1/ops/audit-chain")
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
