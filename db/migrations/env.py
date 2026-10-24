from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# alembic.ini's prepend_sys_path adds services/api, but be defensive in case
# this is invoked in a way that skips that (e.g. a differently-located ini).
_SERVICES_API = os.path.join(os.path.dirname(__file__), "..", "..", "services", "api")
if _SERVICES_API not in sys.path:
    sys.path.insert(0, os.path.abspath(_SERVICES_API))

from app import models, models_ledger, models_reconciliation  # noqa: E402,F401

# side effect of the import above: registers tables on Base.metadata
from app.db import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not set. The migration runner requires a real "
        "PostgreSQL connection string and refuses to guess one, consistent "
        "with A01's 'missing credentials fail clearly' requirement."
    )
config.set_main_option("sqlalchemy.url", DATABASE_URL)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
