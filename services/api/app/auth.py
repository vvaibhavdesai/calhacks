"""Authentication (JWT session) and backend-enforced capability checks.

Mechanism (recorded in docs/adr/0001-runtime.md): server-issued HS256 JWT,
symmetric secret (AUTH_JWT_SECRET), short-lived access token. No third-party
OAuth provider for v1, consistent with AGENT_HANDOFF.md: "one initial user
may hold all roles" and C00's note that the exact mechanism is a setup
choice, not a reopened product decision.

Security properties this module is responsible for:
  - Passwords are hashed with bcrypt; plaintext is never stored or logged.
  - A JWT only ever carries identity (sub=user_id, exp). It never carries a
    capability list. This is the key anti-bypass property: a client cannot
    edit a token (or any browser-local state) to grant itself a capability,
    because `require_capability` re-reads user_capabilities from the
    database on every request.
  - Anonymous requests (no/invalid token) are rejected with 401 before any
    capability check runs. A missing capability is rejected with 403.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Capability, User, UserCapability

JWT_ALGORITHM = "HS256"

# tokenUrl is documentation-only (used by OpenAPI's "Authorize" button); the
# actual route is registered in app.routers.auth.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


def hash_password(plain_password: str) -> str:
    return bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except ValueError:
        # Malformed hash (e.g. corrupted row) -- fail closed, not a 500.
        return False


def create_access_token(user_id: str) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": user_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.auth_token_ttl_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.auth_jwt_secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> str:
    """Returns the user_id (sub) or raises HTTPException(401)."""
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.auth_jwt_secret, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired token",
        ) from exc
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token subject"
        )
    return sub


def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_id = decode_access_token(token)
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="user not found or inactive",
        )
    return user


def get_user_capability_codes(db: Session, user_id: str) -> set[str]:
    rows = db.execute(
        select(Capability.code)
        .join(UserCapability, UserCapability.capability_id == Capability.id)
        .where(UserCapability.user_id == user_id)
    ).scalars()
    return set(rows)


def require_capability(capability_code: str):
    """FastAPI dependency factory: 401 if unauthenticated, 403 if authenticated
    but lacking the capability. Capabilities are always re-read from the
    database for the current request -- never trusted from the token or any
    client-supplied value."""

    def _dependency(
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        codes = get_user_capability_codes(db, user.id)
        if capability_code not in codes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"missing required capability: {capability_code}",
            )
        return user

    return _dependency
