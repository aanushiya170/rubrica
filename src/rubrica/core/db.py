"""SQLite access. One connection per request, WAL mode, foreign keys on.

The schema lives in ``schema.sql`` and is applied with ``CREATE ... IF NOT
EXISTS`` so booting twice is safe. Numbered migrations (for changes after a
release) live in ``MIGRATIONS`` and are recorded in ``schema_migrations``.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .util import iso

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# (version, sql). Append only. Version 1 is the base schema.
MIGRATIONS: list[tuple[int, str]] = [
    (1, "SELECT 1"),
]

_write_lock = threading.RLock()


def database_path() -> str:
    return os.environ.get("DATABASE_PATH", os.path.join("data", "rubrica.db"))


def connect(path: str | None = None) -> sqlite3.Connection:
    path = path or database_path()
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection, extra_schema: list[str] | None = None) -> None:
    """Apply the base schema plus any extension schema fragments."""
    with _write_lock:
        conn.executescript(SCHEMA_PATH.read_text())
        for sql in extra_schema or []:
            conn.executescript(sql)
        applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
        for version, sql in MIGRATIONS:
            if version in applied:
                continue
            conn.executescript(sql)
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, iso()),
            )
        conn.commit()


class Database:
    """Tiny connection holder. Shared across requests; SQLite serialises writes."""

    def __init__(self, path: str | None = None):
        self.path = path or database_path()
        self._conn = connect(self.path)
        self.extra_schema: list[str] = []

    def init(self) -> None:
        init_schema(self._conn, self.extra_schema)

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @contextmanager
    def tx(self):
        """Serialised write transaction."""
        with _write_lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def one(self, sql: str, params=()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()

    def all(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self._conn.execute(sql, params).fetchall()

    def run(self, sql: str, params=()) -> sqlite3.Cursor:
        with self.tx() as c:
            return c.execute(sql, params)

    def close(self) -> None:
        self._conn.close()
