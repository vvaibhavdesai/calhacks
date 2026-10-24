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
COPY packages/domain /repo/packages/domain
COPY db/migrations /repo/db/migrations
COPY scripts /repo/scripts

# A04: app/settlement.py is the first real consumer of packages/domain
# (`from domain.settlement import ...`) -- it must be on PYTHONPATH here,
# not only in pyproject.toml's [tool.pytest.ini_options] pythonpath (which
# only applies to `pytest`, never to this container's `uvicorn` process).
# Before A04 this omission was latent/harmless because nothing under
# services/api/app imported from `domain` yet.
ENV PYTHONPATH=/repo/services/api:/repo/packages/domain

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "app.main:app", "--app-dir", "services/api", "--host", "0.0.0.0", "--port", "8000"]
