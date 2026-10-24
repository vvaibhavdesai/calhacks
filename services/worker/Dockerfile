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

ENV PYTHONPATH=/repo/services/worker \
    PATH=/repo/.venv/bin:$PATH

RUN useradd --system --uid 10001 --home /repo encore
USER encore

EXPOSE 8100

CMD ["python", "-m", "worker.main"]
