"""Alembic environment configuration.

Reads the connection string from the ``DATABASE_URL`` environment variable
(falling back to ``alembic.ini``'s placeholder) so the same migrations run
identically in docker-compose, CI, and local dev.
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make `backend/` importable so `db.models` resolves regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from db.models import Base  # noqa: E402

config = context.config

db_url = os.environ.get("DATABASE_URL")
if db_url:
    # psycopg (v3) driver string works for both sync engine creation here
    # and the app's runtime engine.
    config.set_main_option("sqlalchemy.url", db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection (``--sql`` mode)."""

    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live DB connection."""

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
