"""A09 command envelope + Idempotency-Key handling (C03).

Request body: {command_id, expected_version, payload}. The actor always
comes from the authenticated session, never from the body (unknown body
fields are rejected). `Idempotency-Key` is required on effectful commands
and scoped by authenticated user + action:
  - same key + same canonical request -> the original response, replayed
    (no second effect);
  - same key + different request -> 409 IDEMPOTENCY_KEY_REUSED.
The command record is inserted in the SAME transaction as the command's
effects, so a rolled-back command leaves no record and a committed one
cannot run twice (unique scope_key).
"""

from __future__ import annotations

from typing import Any

from domain.corrections import canonical_hash
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models_corrections import CommandRecord


class CommandEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(min_length=1, max_length=64)
    expected_version: int | None = None
    payload: dict[str, Any]


def unwrap_envelope(body: dict) -> dict:
    """Flatten a C03 command envelope onto the top level.

    The endpoints that take this envelope (the A09 commands) and the ones
    that take a flat body are both real: the UI's command client sends
    every effectful call as {command_id, expected_version, payload: {...}},
    while the verify scripts post the fields directly. An endpoint whose
    request model reads the flat shape therefore sees the envelope's
    `payload` as one unknown field and -- with pydantic's default, which
    ignores unknown fields -- silently drops everything inside it. That is
    how `POST /payouts/{id}/approve` came to discard the approver's
    `amount_cents`/`beneficiary_version` echo while appearing to bind it.

    Flattening here, BEFORE the typed model validates, lets one request
    model accept both shapes. The envelope's own top-level fields
    (`command_id`, `expected_version`) are kept; `payload`'s contents win
    on a key collision, since that is where the UI puts its intent.

    Copies of this flattening already live in `app/routers/cases.py` and
    `app/routers/imports.py`; this is the shared version, in the module
    that defines the envelope. Those two can collapse onto it (a one-line
    import each) whenever their owners are next in those files -- not done
    here, to stay out of files another agent is editing right now.
    """
    payload = body.get("payload")
    if isinstance(payload, dict):
        merged = {k: v for k, v in body.items() if k != "payload"}
        merged.update(payload)
        return merged
    return body


def error(status: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={
            "code": code,
            "message": message,
            "retryable": status in (409, 429, 503),
            "field_errors": extra.pop("field_errors", []),
            **extra,
        },
    )


def scope_key(user_id: str, action: str, key: str) -> str:
    return f"{user_id}:{action}:{key}"


def request_hash(envelope: CommandEnvelope, **path) -> str:
    return canonical_hash({"envelope": envelope.model_dump(), "path": path})


def replayed(
    db: Session, *, user_id: str, action: str, key: str, req_hash: str
) -> JSONResponse | None:
    rec = db.execute(
        select(CommandRecord).where(CommandRecord.scope_key == scope_key(user_id, action, key))
    ).scalar_one_or_none()
    if rec is None:
        return None
    if rec.request_hash != req_hash:
        raise error(
            409,
            "IDEMPOTENCY_KEY_REUSED",
            "this Idempotency-Key was already used with a different request",
        )
    return JSONResponse(
        status_code=rec.status_code, content=rec.response, headers={"Idempotent-Replay": "true"}
    )


def remember(
    db: Session,
    *,
    user_id: str,
    action: str,
    key: str,
    req_hash: str,
    status_code: int,
    response: dict,
) -> None:
    db.add(
        CommandRecord(
            scope_key=scope_key(user_id, action, key),
            action=action,
            request_hash=req_hash,
            status_code=status_code,
            response=response,
            actor_id=user_id,
        )
    )
    db.flush()


def require_key(key: str | None) -> str:
    if not key or not (8 <= len(key) <= 100):
        raise error(
            422,
            "IDEMPOTENCY_KEY_REQUIRED",
            "Idempotency-Key header (8-100 chars) is required for this command",
        )
    return key
