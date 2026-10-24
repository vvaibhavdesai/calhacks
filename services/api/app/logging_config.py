"""Structured JSON logging with secret redaction.

AGENT_HANDOFF.md / A01 require "redacted structured logs; secrets only
server-side". This module provides a stdlib-logging JSON formatter that:
  - always emits one JSON object per line (structured, machine-parseable);
  - redacts any field whose key looks secret-shaped, and scrubs common
    bearer-token / password patterns out of free-text messages too, so a
    developer accidentally logging a header or password doesn't leak it.

This is defense in depth, not a substitute for simply not logging secrets.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_REDACT_KEYS = {
    "password",
    "hashed_password",
    "secret",
    "auth_jwt_secret",
    "token",
    "access_token",
    "authorization",
    "api_key",
    "anthropic_api_key",
    "document_storage_credential",
}
# A10: any key CONTAINING one of these fragments is also redacted, so e.g.
# `refresh_token`, `x_api_key`, `ops_metrics_token`, `db_password` or
# `set_cookie` are covered without listing every spelling.
_REDACT_KEY_FRAGMENTS = ("password", "secret", "token", "api_key", "credential", "cookie", "dsn")

_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9\-_\.]+", re.IGNORECASE)
# A10: Anthropic-style keys and credentials embedded in URLs
# (postgresql+psycopg://user:PASSWORD@host/db) are scrubbed from free text.
_ANTHROPIC_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9\-_]{8,}")
_URL_CREDENTIALS_RE = re.compile(r"(?P<scheme>[a-z][a-z0-9+.\-]*://)(?P<user>[^:/@\s]+):[^@/\s]+@")
_REDACTED = "[REDACTED]"


def _is_secret_key(key: str) -> bool:
    k = key.lower()
    return k in _REDACT_KEYS or any(f in k for f in _REDACT_KEY_FRAGMENTS)


def _redact_value(key: str, value: Any) -> Any:
    if _is_secret_key(key):
        return _REDACTED
    if isinstance(value, dict):
        return {k: _redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, str):
        return _scrub_text(value)
    return value


def _scrub_text(text: str) -> str:
    text = _BEARER_RE.sub("Bearer " + _REDACTED, text)
    text = _ANTHROPIC_KEY_RE.sub(_REDACTED, text)
    return _URL_CREDENTIALS_RE.sub(lambda m: f"{m['scheme']}{m['user']}:{_REDACTED}@", text)


class RedactingJSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _scrub_text(record.getMessage()),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            for k, v in extra.items():
                payload[k] = _redact_value(k, v)
        if record.exc_info:
            payload["exc_info"] = _scrub_text(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    # Replace handlers so repeated configure_logging() calls (e.g. in tests)
    # don't duplicate output.
    root.handlers.clear()
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(RedactingJSONFormatter())
    root.addHandler(handler)


def log_with_fields(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    logger.log(level, message, extra={"extra_fields": fields})
