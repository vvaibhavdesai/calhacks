FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/repo/.venv

# A10: uv pinned to a release that reads uv.lock's format (lock revision 3,
# written by uv 0.9.x). The old 0.5.9 pin plus a non-frozen fallback could
# silently re-resolve dependencies at build time; a build must now use the
# lockfile exactly or fail.
RUN pip install --no-cache-dir uv==0.9.27

WORKDIR /repo

# Copy only what's needed to resolve dependencies first for layer caching.
COPY pyproject.toml uv.lock /repo/
RUN uv sync --frozen --no-dev

# Now copy the actual source. Never tests/, evaluator files or .env
# (see /.dockerignore).
COPY services/api/app /repo/services/api/app
COPY packages/domain /repo/packages/domain
COPY packages/contracts /repo/packages/contracts
COPY db/migrations /repo/db/migrations
COPY scripts /repo/scripts

# A04: app/settlement.py is the first real consumer of packages/domain
# (`from domain.settlement import ...`) -- it must be on PYTHONPATH here,
# not only in pyproject.toml's [tool.pytest.ini_options] pythonpath (which
# only applies to `pytest`, never to this container's `uvicorn` process).
ENV PYTHONPATH=/repo/services/api:/repo/packages/domain:/repo/packages/contracts \
    PATH=/repo/.venv/bin:$PATH

# A10: run as an unprivileged user. The documents volume is mounted at
# /data/documents and must be writable by this uid.
RUN useradd --system --uid 10001 --home /repo encore \
    && mkdir -p /data/documents && chown encore /data/documents
USER encore

EXPOSE 8000

# Structured JSON access logs come from app.main's middleware; uvicorn's own
# access log (which includes query strings) is disabled.
CMD ["uvicorn", "app.main:app", "--app-dir", "services/api", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--proxy-headers"]
