"""Shared test helpers: an isolated database per test (SQLite by default, PostgreSQL on demand).

Set CAREER_QUEST_TEST_DATABASE_URL=postgresql+psycopg://user@host/db to run the
same suite against PostgreSQL; every test then gets its own schema.
"""

import os
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, text

from backend.settings import Settings, sqlite_url

# Keep test output readable; set CAREER_QUEST_LOG_LEVEL=INFO to see request logs.
os.environ.setdefault("CAREER_QUEST_LOG_LEVEL", "WARNING")


def database_url(test_case, directory) -> str:
    base = os.environ.get("CAREER_QUEST_TEST_DATABASE_URL", "").strip()
    if not base:
        return sqlite_url(Path(directory) / "state.sqlite3")
    schema = "t_" + uuid4().hex[:16]
    engine = create_engine(base)
    with engine.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
    engine.dispose()

    def drop():
        cleanup = create_engine(base)
        with cleanup.begin() as connection:
            connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
        cleanup.dispose()

    test_case.addCleanup(drop)
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}options=-csearch_path%3D{schema}"


def make_settings(**overrides) -> Settings:
    """Deterministic settings independent of the developer's environment."""
    values = dict(environment="test", completion_mode="instant", cookie_secure=False, enable_docs=True,
                  login_ip_limit_per_15min=1000)
    values.update(overrides)
    return Settings(**values)
