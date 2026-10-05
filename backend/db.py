"""Database engine, schema and migrations (PostgreSQL in production, SQLite locally).

The schema is normalized so that one employee's completion touches a few rows
instead of rewriting the whole dataset. All writes go through ``write_tx`` which
serializes writers on one state row; readers never block writers.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path

from sqlalchemy import (
    Boolean, Column, DateTime, Index, Integer, LargeBinary, MetaData, String, Table, Text,
    create_engine, event, select, text,
)
from sqlalchemy.engine import Connection, Engine

metadata = MetaData()

# One row (id=1). ``version`` changes on every write and keys in-process caches;
# ``catalog_rev`` changes only when events or the skills document change;
# ``epoch`` changes when the dataset is replaced and invalidates retry keys.
state = Table(
    "cq_state", metadata,
    Column("id", Integer, primary_key=True),
    Column("version", Integer, nullable=False),
    Column("catalog_rev", Integer, nullable=False),
    Column("epoch", String(32), nullable=False),
    Column("skills_document", Text, nullable=False),
    Column("updated_at", DateTime, nullable=False),
)

schema_migrations = Table(
    "cq_schema_migrations", metadata,
    Column("version", Integer, primary_key=True),
    Column("applied_at", DateTime, nullable=False),
)

employees = Table(
    "employees", metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("employee_id", String(64), nullable=False, unique=True),
    Column("name", String(255), nullable=False),
    Column("role", String(255), nullable=False),
    Column("grade", String(255), nullable=False),
    Column("tenure_months", Integer, nullable=False),
    Column("skills", Text, nullable=False),
    # Incremented whenever the employee or their history changes (cache key).
    Column("rev", Integer, nullable=False, default=0),
)

events = Table(
    "events", metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("event_id", String(64), nullable=False, unique=True),
    Column("payload", Text, nullable=False),
)

history = Table(
    "activity_history", metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("record_id", String(64), nullable=False, unique=True),
    Column("employee_id", String(64), nullable=False),
    Column("event_id", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("event_date", String(10), nullable=False),
    Column("created_by", String(64)),
    Index("ix_history_employee", "employee_id", "seq"),
)

idempotency = Table(
    "idempotency_keys", metadata,
    Column("scope", String(64), primary_key=True),
    Column("request_key", String(64), primary_key=True),
    Column("target", String(255), nullable=False),
    Column("epoch", String(32), nullable=False),
    Column("status_code", Integer, nullable=False, default=200),
    Column("response", Text, nullable=False),
    Column("created_at", DateTime, nullable=False),
)

users = Table(
    "users", metadata,
    Column("user_id", String(64), primary_key=True),
    Column("email", String(254), nullable=False, unique=True),
    Column("name", String(255), nullable=False),
    Column("role", String(16), nullable=False),
    Column("employee_id", String(64)),
    Column("password_hash", String(255)),
    Column("api_token_hash", String(64), unique=True),
    Column("must_change_password", Boolean, nullable=False, default=False),
    Column("is_active", Boolean, nullable=False, default=True),
    Column("failed_attempts", Integer, nullable=False, default=0),
    Column("locked_until", DateTime),
    Column("consent_version", String(32)),
    Column("consent_at", DateTime),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("last_login_at", DateTime),
    Column("password_changed_at", DateTime),
    # One person account per employee profile (service accounts have no password).
    Index("ux_users_employee_person", "employee_id", unique=True,
          postgresql_where=text("role = 'employee' AND password_hash IS NOT NULL"),
          sqlite_where=text("role = 'employee' AND password_hash IS NOT NULL")),
)

sessions = Table(
    "sessions", metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("user_id", String(64), nullable=False, index=True),
    Column("created_at", DateTime, nullable=False),
    Column("expires_at", DateTime, nullable=False),
    Column("last_seen_at", DateTime, nullable=False),
    Column("ip", String(64)),
    Column("user_agent", String(255)),
)

audit_log = Table(
    "audit_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("at", DateTime, nullable=False, index=True),
    Column("actor_id", String(64)),
    Column("actor_role", String(16)),
    Column("action", String(64), nullable=False, index=True),
    Column("target", String(255)),
    Column("details", Text),
    Column("ip", String(64)),
)

completion_requests = Table(
    "completion_requests", metadata,
    Column("request_id", String(64), primary_key=True),
    Column("employee_id", String(64), nullable=False, index=True),
    Column("event_id", String(64), nullable=False),
    Column("status", String(16), nullable=False, index=True),
    Column("requested_by", String(64), nullable=False),
    Column("requested_at", DateTime, nullable=False),
    Column("comment", Text),
    Column("decided_by", String(64)),
    Column("decided_at", DateTime),
    Column("decision_comment", Text),
    Column("record_id", String(64)),
    Column("epoch", String(32), nullable=False),
)

dataset_snapshots = Table(
    "dataset_snapshots", metadata,
    Column("snapshot_id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime, nullable=False),
    Column("created_by", String(64)),
    Column("reason", String(64), nullable=False),
    Column("employees", Integer, nullable=False),
    Column("events", Integer, nullable=False),
    Column("history_records", Integer, nullable=False),
    Column("payload", LargeBinary, nullable=False),
)


def utcnow() -> datetime:
    """Naive UTC timestamps behave identically on SQLite and PostgreSQL."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def iso(value: datetime | None) -> str | None:
    return None if value is None else value.replace(microsecond=0).isoformat() + "Z"


class Database:
    """Owns the engines. SQLite gets a separate write engine using BEGIN IMMEDIATE."""

    def __init__(self, url: str, *, pool_size: int = 5):
        self.url = url
        self.is_sqlite = url.startswith("sqlite")
        if self.is_sqlite:
            path = url.split(":///", 1)[1] if ":///" in url else ""
            if path in ("", ":memory:") or "mode=memory" in url:
                # One shared in-memory connection cannot isolate concurrent transactions.
                raise ValueError("In-memory SQLite is not supported; use a database file path")
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            options = dict(connect_args={"check_same_thread": False, "timeout": 30})
            self.read_engine = create_engine(url, **options)
            self.write_engine = create_engine(url, **options)
            self._configure_sqlite(self.read_engine, immediate=False)
            self._configure_sqlite(self.write_engine, immediate=True)
        else:
            self.read_engine = create_engine(
                url, pool_size=pool_size, max_overflow=pool_size * 2, pool_pre_ping=True,
                pool_recycle=1800)
            # Writers rely on READ COMMITTED: after waiting for the state-row lock
            # they must see rows committed by the previous writer.
            self.write_engine = self.read_engine.execution_options(isolation_level="READ COMMITTED")

    @staticmethod
    def _configure_sqlite(engine: Engine, *, immediate: bool):
        @event.listens_for(engine, "connect")
        def connect(dbapi_connection, _record):
            dbapi_connection.isolation_level = None  # SQLAlchemy emits BEGIN itself
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        @event.listens_for(engine, "begin")
        def begin(connection):
            connection.exec_driver_sql("BEGIN IMMEDIATE" if immediate else "BEGIN")

    @contextmanager
    def read_tx(self):
        """Consistent read snapshot (REPEATABLE READ on PostgreSQL)."""
        with self.read_engine.connect() as connection:
            if not self.is_sqlite:
                connection = connection.execution_options(isolation_level="REPEATABLE READ")
            with connection.begin():
                yield connection

    @contextmanager
    def write_tx(self):
        """Serialized write transaction; the caller must call ``lock_state`` first."""
        with self.write_engine.connect() as connection:
            with connection.begin():
                yield connection

    def ping(self) -> bool:
        with self.read_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True

    def dispose(self):
        self.read_engine.dispose()
        if self.is_sqlite:
            self.write_engine.dispose()


def lock_state(connection: Connection, *, catalog_changed: bool = False, new_epoch: str | None = None):
    """Take the global writer lock and bump the data version; returns (version, catalog_rev, epoch)."""
    values = {"version": state.c.version + 1, "updated_at": utcnow()}
    if catalog_changed:
        values["catalog_rev"] = state.c.catalog_rev + 1
    if new_epoch:
        values["epoch"] = new_epoch
    connection.execute(state.update().where(state.c.id == 1).values(**values))
    row = connection.execute(
        select(state.c.version, state.c.catalog_rev, state.c.epoch).where(state.c.id == 1)).one()
    return row.version, row.catalog_rev, row.epoch


# --- migrations ------------------------------------------------------------

def _migration_1(connection: Connection):
    metadata.create_all(connection)


MIGRATIONS = [(1, _migration_1)]
SCHEMA_VERSION = MIGRATIONS[-1][0]


def migrate(database: Database) -> int:
    """Apply pending migrations in order; safe to run from several processes."""
    with database.write_engine.connect() as connection:
        with connection.begin():
            if not database.is_sqlite:
                # Serialize concurrent migrators (several app workers starting at once).
                connection.execute(text("SELECT pg_advisory_xact_lock(482917)"))
            schema_migrations.create(connection, checkfirst=True)
            applied = set(connection.execute(select(schema_migrations.c.version)).scalars())
            for version, apply in MIGRATIONS:
                if version not in applied:
                    apply(connection)
                    connection.execute(schema_migrations.insert().values(version=version, applied_at=utcnow()))
    return SCHEMA_VERSION


def read_legacy_sqlite_payload(url: str) -> str | None:
    """Return the dataset JSON stored by the pre-SQL version (table ``state``), if any."""
    if not url.startswith("sqlite:///"):
        return None
    import sqlite3

    path = Path(url.split(":///", 1)[1])
    if not path.is_file():
        return None
    with sqlite3.connect(path) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "state" not in tables:
            return None
        row = connection.execute("SELECT payload FROM state WHERE id=1").fetchone()
    return row[0] if row else None


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
