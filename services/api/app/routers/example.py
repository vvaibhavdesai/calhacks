"""Demo capability-enforced write endpoint.

Real business write endpoints (resolve case, approve policy, approve payout,
etc.) land in A02+ and reuse the exact same `require_capability` dependency
used here. This route exists so A01's acceptance criterion -- "unauthorized
writes denied" -- has something concrete and testable to point at before any
business schema exists.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth import require_capability
from app.models import CAPABILITY_ADMIN

router = APIRouter(prefix="/api/v1/example", tags=["example"])


class ProtectedWriteResponse(BaseModel):
    status: str
    performed_by: str


@router.post("/protected-write", response_model=ProtectedWriteResponse, status_code=201)
def protected_write(user=Depends(require_capability(CAPABILITY_ADMIN))) -> ProtectedWriteResponse:
    return ProtectedWriteResponse(status="accepted", performed_by=user.id)
