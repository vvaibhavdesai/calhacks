"""Application settings.

All configuration is read from the environment (see .env.example at repo root
and docs/adr/0001-runtime.md). Required secrets have NO default: a missing
DATABASE_URL or AUTH_JWT_SECRET causes Settings() to raise a pydantic
ValidationError at process startup, which FastAPI/uvicorn surfaces as a clear
startup failure rather than the app silently running unauthenticated or
without a database. This directly satisfies the A01 acceptance criterion
"missing credentials fail clearly".

Secrets are never exposed to the frontend: the frontend only ever receives
VITE_API_BASE_URL at build time, which is a public value, not a secret.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")

    app_env: str = Field(default="development", alias="APP_ENV")

    # Required. No default -- missing value fails startup immediately.
    database_url: str = Field(alias="DATABASE_URL")

    # Required. No default -- an API that could start without a signing
    # secret would be a silent authentication bypass risk.
    auth_jwt_secret: str = Field(alias="AUTH_JWT_SECRET")
    auth_token_ttl_minutes: int = Field(default=60, alias="AUTH_TOKEN_TTL_MINUTES")

    document_storage_backend: str = Field(default="local", alias="DOCUMENT_STORAGE_BACKEND")
    document_storage_root: str = Field(default="./.data/documents", alias="DOCUMENT_STORAGE_ROOT")

    allowed_origins: str = Field(default="http://localhost:5173", alias="ALLOWED_ORIGINS")

    # A07: C00 "PAYMENT_MODE is simulated for v1; reject unsupported real-
    # payment mode." app/rail_simulator.py refuses to run unless this is
    # exactly "simulated" -- there is no real-payment code path to fall
    # back to, so this is a deliberate fail-closed guard against a
    # misconfigured environment claiming to be production-ready.
    payment_mode: str = Field(default="simulated", alias="PAYMENT_MODE")

    # Intentionally not required / not used in A01. A06 revisits with the user
    # before any live Claude call is made, because it spends real API credit.
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_model: str | None = Field(default=None, alias="ANTHROPIC_MODEL")

    # --- A10 operations: alert thresholds (app/ops.py) ---
    # A run still QUEUED/RUNNING with no update for this long is "stalled".
    ops_stalled_run_minutes: int = Field(default=30, ge=1, alias="OPS_STALLED_RUN_MINUTES")
    # An outbox intent still PENDING after this long is a stale backlog item.
    ops_outbox_stale_minutes: int = Field(default=10, ge=1, alias="OPS_OUTBOX_STALE_MINUTES")
    # Alert when more than this many intents are PENDING, regardless of age.
    ops_outbox_backlog_max: int = Field(default=100, ge=0, alias="OPS_OUTBOX_BACKLOG_MAX")
    # Comma-separated outbox intent types that have NO dispatcher yet and so
    # stay PENDING by design (A02's import notification has no consumer).
    # Reported separately, excluded from the backlog alert. Remove a type
    # from this list the moment a dispatcher for it ships.
    ops_outbox_unconsumed_intent_types: str = Field(
        default="source.imported.notify", alias="OPS_OUTBOX_UNCONSUMED_INTENT_TYPES"
    )
    # Shared secret for scraping GET /metrics. Unset -> /metrics is disabled
    # (404), never open. Secret: inject from the environment only.
    ops_metrics_token: str | None = Field(default=None, alias="OPS_METRICS_TOKEN")
    # Optional worker health URL the API probes for the worker-liveness alert
    # (e.g. http://worker:8100/health/live inside docker-compose).
    ops_worker_health_url: str | None = Field(default=None, alias="OPS_WORKER_HEALTH_URL")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # --- A10 Claude rate/cost limits (app/agents/limits.py) ---
    # Hard ceilings an operator sets per environment. A request asking for
    # more is rejected (422), not silently clamped, so a caller always knows
    # what budget actually applied.
    claude_max_budget_usd_per_request: float = Field(
        default=5.0, gt=0, alias="CLAUDE_MAX_BUDGET_USD_PER_REQUEST"
    )
    # Rolling 24h ESTIMATED spend across all processes (summed from recorded
    # document_extractions token usage). New batches are refused once
    # exhausted and capped to what remains otherwise.
    claude_daily_budget_usd: float = Field(default=20.0, gt=0, alias="CLAUDE_DAILY_BUDGET_USD")
    claude_max_documents_per_request: int = Field(
        default=200, ge=1, alias="CLAUDE_MAX_DOCUMENTS_PER_REQUEST"
    )
    claude_max_concurrency: int = Field(default=4, ge=1, le=16, alias="CLAUDE_MAX_CONCURRENCY")
    # Per-API-process sliding-window limit on extraction batches started.
    claude_max_batches_per_hour: int = Field(default=20, ge=1, alias="CLAUDE_MAX_BATCHES_PER_HOUR")

    @property
    def outbox_unconsumed_intent_types(self) -> list[str]:
        return [t.strip() for t in self.ops_outbox_unconsumed_intent_types.split(",") if t.strip()]

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


def get_settings() -> Settings:
    # Re-read every call (cheap, env-backed) rather than caching a module-level
    # singleton, so tests can monkeypatch os.environ between cases.
    return Settings()  # type: ignore[call-arg]
