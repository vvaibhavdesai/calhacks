from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_db

router = APIRouter(tags=["health"])
logger = logging.getLogger("encore.health")


@router.get("/health/live")
def liveness() -> dict:
    """Process is up. No external dependency check -- this must stay cheap
    and always answer even if the database is unreachable, so orchestrators
    can distinguish 'process is wedged' from 'dependency is down'."""
    return {"status": "ok"}


@router.get("/health/ready")
def readiness(response: Response, db: Session = Depends(get_db)) -> dict:
    """Process is up AND can reach Postgres."""
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any DB failure means not-ready
        # A10: only the exception CLASS goes to this unauthenticated caller;
        # the message can carry host/role names. Full detail goes to the
        # (redacted) server log instead.
        logger.warning("readiness check failed: %s", exc)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "status": "unavailable",
            "detail": "database unreachable",
            "error": type(exc).__name__,
        }
    return {"status": "ok"}
