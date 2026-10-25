FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/repo/.venv

# A10: same uv pin and frozen-lock rule as services/api/Dockerfile.
RUN pip install --no-cache-dir uv==0.9.27

WORKDIR /repo

COPY pyproject.toml uv.lock /repo/
RUN uv sync --frozen --no-dev

COPY services/worker/worker /repo/services/worker/worker
# The worker's loops are thin shells around the application code: the
# nightly-close coordinator, the outbox dispatcher's handlers, the schedule
# and the demo clock all live in services/api/app, and that code imports
# packages/domain. Without these the container starts and then fails on the
# first `from app...` -- which is what the A06 handoff recorded as "Docker is
# not wired". Tests, the evaluator and .env are excluded by /.dockerignore.
COPY services/api/app /repo/services/api/app
COPY packages/domain /repo/packages/domain
COPY packages/contracts /repo/packages/contracts

ENV PYTHONPATH=/repo/services/worker:/repo/services/api:/repo/packages/domain:/repo/packages/contracts \
    PATH=/repo/.venv/bin:$PATH

RUN useradd --system --uid 10001 --home /repo encore
USER encore

EXPOSE 8100

CMD ["python", "-m", "worker.main"]
