from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import (
    create_access_token,
    get_current_user,
    get_user_capability_codes,
    verify_password,
)
from app.db import get_db
from app.models import User

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MeResponse(BaseModel):
    id: str
    email: str
    display_name: str | None
    capabilities: list[str]


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    user = db.execute(select(User).where(User.email == payload.email)).scalar_one_or_none()
    # Constant-shape failure: wrong email and wrong password both return the
    # same 401/detail, so this endpoint doesn't leak which emails exist.
    password_ok = user is not None and verify_password(payload.password, user.hashed_password)
    if user is None or not user.is_active or not password_ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    token = create_access_token(user.id)
    return LoginResponse(access_token=token)


@router.get("/me", response_model=MeResponse)
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> MeResponse:
    codes = sorted(get_user_capability_codes(db, user.id))
    return MeResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        capabilities=codes,
    )
