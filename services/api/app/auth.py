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

NOTE: those last two properties are currently SUSPENDED. The app runs in
single-operator mode (ENCORE_AUTH_DISABLED, default on outside production) --
see the block above `get_current_user`. Everything else in this module is
intact; only the two dependency checks short-circuit.
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


# ---------------------------------------------------------------------------
# SINGLE-OPERATOR MODE -- authentication is bypassed. TEMPORARY.
#
# One person operates the whole app and holds every capability, so there is
# nobody to authenticate against and no permission boundary to draw. Rather
# than delete the flow, both choke points below short-circuit: every request
# resolves to that one operator, and capability checks pass.
#
# What is deliberately KEPT, because removing it would change behaviour rather
# than just skip a login:
#   - The operator is a real User row, so `actor_id` on every audit event and
#     approval is a real identity, not a placeholder.
#   - `get_user_capability_codes` is untouched and still read from the
#     database, so payout approval still sees that this actor holds
#     finance_lead and the >$5,000 escalation path behaves as designed.
#
# To restore authentication: set ENCORE_AUTH_DISABLED=0 (or delete this block
# and the two `if _auth_disabled()` branches). Nothing else was removed --
# /auth/login, password hashing, token issuing and decoding all still work.
#
# Refuses to bypass when APP_ENV=production, the same gate the demo clock and
# the orchestration crash points already use, so this cannot ship silently.
# ---------------------------------------------------------------------------


def _auth_disabled() -> bool:
    import os

    if os.environ.get("APP_ENV", "").lower() == "production":
        return False
    return os.environ.get("ENCORE_AUTH_DISABLED", "1") == "1"


def _single_operator(db: Session) -> User:
    """The one person running the app.

    The seeded admin by ADMIN_EMAIL when present, else the first active user by
    creation order so a database seeded differently still resolves. Raises if
    there is no user at all -- that is a setup problem worth surfacing, not a
    thing to invent an identity for.
    """
    import os

    email = os.environ.get("ADMIN_EMAIL")
    user = None
    if email:
        user = db.execute(select(User).where(User.email == email)).scalars().first()
    if user is None:
        user = (
            db.execute(select(User).where(User.is_active.is_(True)).order_by(User.created_at.asc()))
            .scalars()
            .first()
        )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="single-operator mode: no user exists; run scripts/seed_admin.py",
        )
    return user


def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    if _auth_disabled():
        return _single_operator(db)
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
        if _auth_disabled():
            # Single-operator mode: one person holds everything, so there is no
            # capability to withhold. The codes are still readable from the
            # database for the code that needs them (payout escalation).
            return user
        codes = get_user_capability_codes(db, user.id)
        if capability_code not in codes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"missing required capability: {capability_code}",
            )
        return user

    return _dependency
