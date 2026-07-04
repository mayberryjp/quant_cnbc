"""Database engine helpers (SQLAlchemy 2.0, psycopg driver)."""

from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine, text

from app.config import settings

_engine: Engine | None = None


def get_engine() -> Engine:
    """Return a lazily-created, process-wide SQLAlchemy engine."""
    global _engine
    if _engine is None:
        url = settings.database_url or os.environ.get("DATABASE_URL", "")
        if not url:
            raise RuntimeError("DATABASE_URL is not configured")
        _engine = create_engine(url, pool_pre_ping=True, future=True)
    return _engine


def set_engine(engine: Engine | None) -> None:
    """Override the process engine (used by tests)."""
    global _engine
    _engine = engine


def ping() -> bool:
    """Return True if the database is reachable."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
