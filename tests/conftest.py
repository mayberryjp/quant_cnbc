"""Shared fixtures for the quant_cnbc test suite."""

from __future__ import annotations

import os

import pytest
from webtest import TestApp


@pytest.fixture
def app_client() -> TestApp:
    """HTTP test client for the Bottle app (health routes need no DB)."""
    from app.main import app

    return TestApp(app)


@pytest.fixture(scope="session")
def db_engine():
    """Session-scoped Postgres engine with the cnbc schema migrated.

    Skips the whole test if no reachable database is configured
    (via TEST_DATABASE_URL or DATABASE_URL).
    """
    url = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("no test database configured (set TEST_DATABASE_URL)")

    from sqlalchemy import create_engine, text

    engine = create_engine(url, future=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"test database not reachable: {exc}")

    # Apply migrations in-process.
    from alembic import command
    from alembic.config import Config

    os.environ["DATABASE_URL"] = url
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")

    yield engine
    engine.dispose()


@pytest.fixture
def clean_db(db_engine):
    """Truncate all cnbc tables before each repository test."""
    from sqlalchemy import text

    with db_engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE cnbc.referenced_entities, cnbc.sentiments, "
                "cnbc.distillations, cnbc.transcripts, cnbc.ingest_runs, "
                "cnbc.ingest_cursor, cnbc.shows RESTART IDENTITY CASCADE"
            )
        )
    return db_engine
