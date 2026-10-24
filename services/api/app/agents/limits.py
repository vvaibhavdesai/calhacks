"""A10: config-driven rate and cost limits for Claude calls.

Every Claude call in this codebase goes through app/agents/runner.py's
`run_batch`, started from `POST /api/v1/agents/extract-documents`. These
limits sit in front of that entry point, BEFORE a client is even built:

  - per-request budget ceiling (CLAUDE_MAX_BUDGET_USD_PER_REQUEST): asking
    for more is a 422, never a silent clamp;
  - per-request document ceiling (CLAUDE_MAX_DOCUMENTS_PER_REQUEST): 422;
  - rolling 24h budget (CLAUDE_DAILY_BUDGET_USD), shared by every API
    process because it is summed from the recorded `document_extractions`
    token usage in PostgreSQL. Exhausted -> 429; otherwise the batch budget
    is capped to what remains (and the response says so);
  - batch-start rate (CLAUDE_MAX_BATCHES_PER_HOUR) per API process -> 429;
  - one batch at a time per API process -> 409 (a second concurrent batch
    would double the effective concurrency and make the budget check racy);
  - worker concurrency (CLAUDE_MAX_CONCURRENCY) passed to `run_batch`.

The spend figure is an ESTIMATE from list prices (app/agents/extraction.py
PRICE_IN/PRICE_OUT), for stopping early -- not billing. The Anthropic
console remains the source of truth for actual spend.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.extraction import PRICE_IN, PRICE_OUT
from app.config import Settings
from app.models_agents import DocumentExtraction


def spent_last_24h_usd(db: Session, *, now: datetime | None = None) -> float:
    now = now or datetime.now(UTC)
    since = now - timedelta(hours=24)
    tin, tout = db.execute(
        select(
            func.coalesce(func.sum(DocumentExtraction.input_tokens), 0),
            func.coalesce(func.sum(DocumentExtraction.output_tokens), 0),
        ).where(DocumentExtraction.created_at >= since)
    ).one()
    return int(tin) * PRICE_IN + int(tout) * PRICE_OUT


class LimitError(Exception):
    def __init__(self, status_code: int, code: str, detail: dict):
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.detail = {"code": code, **detail}


@dataclass
class Grant:
    budget_usd: float
    max_documents: int
    concurrency: int
    capped_by_daily_budget: bool
    daily_remaining_usd: float


class _ProcessGate:
    """In-process batch-start rate window + single-batch lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._busy = threading.Lock()
        self._starts: deque[float] = deque()

    def try_start(self, max_per_hour: int) -> None:
        with self._lock:
            now = time.monotonic()
            while self._starts and now - self._starts[0] > 3600:
                self._starts.popleft()
            if len(self._starts) >= max_per_hour:
                raise LimitError(
                    429,
                    "claude_rate_limited",
                    {"max_batches_per_hour": max_per_hour},
                )
            if not self._busy.acquire(blocking=False):
                raise LimitError(409, "claude_batch_in_progress", {})
            self._starts.append(now)

    def finish(self) -> None:
        if self._busy.locked():
            self._busy.release()

    def reset_for_tests(self) -> None:
        with self._lock:
            self._starts.clear()
            self.finish()


gate = _ProcessGate()


def check_request(
    db: Session, settings: Settings, *, budget_usd: float, max_documents: int
) -> Grant:
    """Validate a requested batch against the configured caps. Raises
    LimitError; never calls Claude."""
    if budget_usd > settings.claude_max_budget_usd_per_request:
        raise LimitError(
            422,
            "budget_exceeds_cap",
            {"cap_usd": settings.claude_max_budget_usd_per_request, "requested_usd": budget_usd},
        )
    if max_documents > settings.claude_max_documents_per_request:
        raise LimitError(
            422,
            "documents_exceed_cap",
            {"cap": settings.claude_max_documents_per_request, "requested": max_documents},
        )
    remaining = settings.claude_daily_budget_usd - spent_last_24h_usd(db)
    if remaining <= 0:
        raise LimitError(
            429,
            "claude_daily_budget_exhausted",
            {"daily_budget_usd": settings.claude_daily_budget_usd},
        )
    return Grant(
        budget_usd=min(budget_usd, remaining),
        max_documents=max_documents,
        concurrency=settings.claude_max_concurrency,
        capped_by_daily_budget=remaining < budget_usd,
        daily_remaining_usd=round(remaining, 4),
    )
