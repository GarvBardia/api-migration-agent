"""Database engine and session management.

Exposes a single ``engine`` and ``SessionLocal`` factory built from
``DATABASE_URL``. Agents and the orchestrator should depend on the
``get_db`` generator (FastAPI-style) or open a ``SessionLocal()`` directly
for background/Celery tasks.
"""

from __future__ import annotations

import os
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://migration_agent:migration_agent@localhost:5432/migration_agent",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True, future=True)

SessionLocal = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: yields a session and guarantees it's closed."""

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
