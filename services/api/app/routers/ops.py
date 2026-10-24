"""A10 operations endpoints (read-only; see app/ops.py).

- `GET /api/v1/ops/alerts`: JSON alert summary for any signed-in user.
- `GET /metrics`: Prometheus text exposition. Disabled (404) unless
  OPS_METRICS_TOKEN is configured; the scraper must then send
  `Authorization: Bearer <OPS_METRICS_TOKEN>`. It is never open by default
  because counts of UNKNOWN payouts and spend are business information.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import get_settings
from app.db import get_db
from app.models import User
from app.ops import collect_signals, signals_to_json, signals_to_prometheus

router = APIRouter(tags=["ops"])


@router.get("/api/v1/ops/alerts")
def ops_alerts(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> dict:
    return signals_to_json(collect_signals(db, get_settings()))


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
