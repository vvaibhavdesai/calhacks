from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.logging_config import configure_logging, log_with_fields
from app.routers import (
    agents,
    auth,
    cases,
    cash_office,
    corrections,
    example,
    festivals,
    finance,
    health,
    imports,
    ops,
    orchestration,
    payouts,
    reconciliation,
    recorder,
    settlement,
    terms,
    vendors,
)

_settings = get_settings()
configure_logging(getattr(logging, _settings.log_level.upper(), logging.INFO))
_access_log = logging.getLogger("encore.access")

app = FastAPI(title="Encore API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def access_log(request: Request, call_next):
    """A10: one structured, redacted line per request. Logs the path only --
    never the query string, headers or body, which can carry tokens or
    business data. X-Request-ID is propagated (or generated) for tracing."""
    request_id = request.headers.get("x-request-id", "")[:100] or uuid.uuid4().hex
    start = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        log_with_fields(
            _access_log,
            logging.INFO,
            "request",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status=status_code,
            duration_ms=round((time.perf_counter() - start) * 1000, 1),
        )


app.include_router(health.router)
app.include_router(ops.router)
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
app.include_router(cash_office.router)
app.include_router(agents.router)
app.include_router(orchestration.router)
app.include_router(recorder.router)
app.include_router(corrections.router)
app.include_router(terms.router)
app.include_router(festivals.router)
app.include_router(festivals.current_router)
app.include_router(vendors.router)


@app.get("/")
def root() -> dict:
    return {"service": "encore-api", "status": "ok"}
