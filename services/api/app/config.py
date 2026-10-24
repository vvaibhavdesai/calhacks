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

    # Intentionally not required / not used in A01. A06 revisits with the user
    # before any live Claude call is made, because it spends real API credit.
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    anthropic_model: str | None = Field(default=None, alias="ANTHROPIC_MODEL")

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


def get_settings() -> Settings:
    # Re-read every call (cheap, env-backed) rather than caching a module-level
    # singleton, so tests can monkeypatch os.environ between cases.
    return Settings()  # type: ignore[call-arg]
