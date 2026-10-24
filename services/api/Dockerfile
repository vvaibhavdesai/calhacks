FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir uv==0.5.9

WORKDIR /repo

# Copy only what's needed to resolve dependencies first for layer caching.
COPY pyproject.toml uv.lock* /repo/
RUN uv sync --frozen --no-dev || uv sync --no-dev

# Now copy the actual source.
COPY services/api/app /repo/services/api/app
COPY db/migrations /repo/db/migrations
COPY scripts /repo/scripts

ENV PYTHONPATH=/repo/services/api

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "app.main:app", "--app-dir", "services/api", "--host", "0.0.0.0", "--port", "8000"]
