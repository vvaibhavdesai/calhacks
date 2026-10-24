from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.logging_config import configure_logging
from app.routers import (
    agents,
    approvals,
    auth,
    cases,
    example,
    finance,
    health,
    imports,
    payouts,
    reconciliation,
    settlement,
)

configure_logging()

app = FastAPI(title="Encore API", version="0.1.0")

_settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(example.router)
app.include_router(imports.router)
app.include_router(reconciliation.router)
app.include_router(settlement.router)
app.include_router(cases.router)
app.include_router(cases.policies_router)
app.include_router(payouts.router)
app.include_router(payouts.cash_pool_router)
app.include_router(payouts.vendors_router)
app.include_router(finance.router)
app.include_router(agents.router)
app.include_router(approvals.router)


@app.get("/")
def root() -> dict:
    return {"service": "encore-api", "status": "ok"}
