FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir uv==0.5.9

WORKDIR /repo

COPY pyproject.toml uv.lock* /repo/
RUN uv sync --frozen --no-dev || uv sync --no-dev

COPY services/worker/worker /repo/services/worker/worker

ENV PYTHONPATH=/repo/services/worker

EXPOSE 8100

CMD ["uv", "run", "python", "-m", "worker.main"]
