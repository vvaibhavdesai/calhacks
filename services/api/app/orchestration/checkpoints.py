"""LangGraph PostgresSaver for the nightly close.

The checkpoint tables are created by alembic migration 0010 (schema owner),
not by `PostgresSaver.setup()`: the API/worker run as `encore_app`, which
cannot run DDL. `verify_checkpoint_schema` refuses to run the graph if the
installed library expects a checkpoint migration that 0010 did not apply,
instead of silently degrading.

Checkpoints are workflow state only (where each vendor's graph is). They
are never the audit record and never authorize an effect.
"""

from __future__ import annotations

import threading
from importlib import metadata

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy.engine import make_url

from app.config import get_settings

_lock = threading.Lock()
_pool: ConnectionPool | None = None
_saver: PostgresSaver | None = None


class CheckpointSchemaError(RuntimeError):
    pass


def library_versions() -> dict[str, str]:
    out = {}
    for pkg in ("langgraph", "langgraph-checkpoint", "langgraph-checkpoint-postgres"):
        try:
            out[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:  # pragma: no cover
            out[pkg] = "unknown"
    return out


def _libpq_url(database_url: str) -> str:
    url = make_url(database_url)
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def verify_checkpoint_schema(pool: ConnectionPool) -> None:
    expected = len(PostgresSaver.MIGRATIONS) - 1
    with pool.connection() as conn:
        try:
            row = conn.execute("SELECT max(v) AS v FROM checkpoint_migrations").fetchone()
        except Exception as exc:  # noqa: BLE001 -- surface as a clear config error
            raise CheckpointSchemaError(
                "checkpoint tables missing: run `alembic upgrade head` (migration 0010)"
            ) from exc
    applied = row["v"] if row else None
    if applied is None or applied < expected:
        raise CheckpointSchemaError(
            f"checkpoint schema at v{applied}, installed langgraph-checkpoint-postgres "
            f"expects v{expected}: add an alembic migration (encore_app cannot run DDL)"
        )


def get_checkpointer(max_size: int = 10) -> PostgresSaver:
    global _pool, _saver
    with _lock:
        if _saver is None:
            pool = ConnectionPool(
                _libpq_url(get_settings().database_url),
                min_size=1,
                max_size=max_size,
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
                open=True,
            )
            verify_checkpoint_schema(pool)
            _pool = pool
            _saver = PostgresSaver(pool)
        return _saver


def reset_checkpointer_for_tests() -> None:
    global _pool, _saver
    with _lock:
        if _pool is not None:
            _pool.close()
        _pool = None
        _saver = None
