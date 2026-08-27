"""The project catalog: workspace/catalog.sqlite.

Deliberately a separate database file so that server metadata never mixes with
the analytical tables the AI queries.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    source_id     TEXT PRIMARY KEY,   -- SHA-256 of the file contents
    filename      TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    modified_at   TEXT,
    sha256        TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS imports (
    table_name    TEXT PRIMARY KEY,
    source_id     TEXT NOT NULL REFERENCES sources(source_id),
    database_path TEXT NOT NULL,
    row_count     INTEGER NOT NULL,
    imported_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS imports_source_idx ON imports(source_id);

CREATE TABLE IF NOT EXISTS column_profiles (
    source_id           TEXT NOT NULL,
    table_name          TEXT NOT NULL,
    column_name         TEXT NOT NULL,
    storage_type        TEXT NOT NULL,
    logical_type        TEXT NOT NULL,
    type_confidence     REAL NOT NULL,
    row_count           INTEGER NOT NULL,
    null_count          INTEGER NOT NULL,
    non_null_count      INTEGER NOT NULL,
    valid_integer_count INTEGER NOT NULL,
    valid_real_count    INTEGER NOT NULL,
    valid_date_count    INTEGER NOT NULL,
    valid_datetime_count INTEGER NOT NULL,
    valid_boolean_count INTEGER NOT NULL,
    invalid_count       INTEGER NOT NULL,
    distinct_count      INTEGER,
    distinct_count_approximate INTEGER NOT NULL DEFAULT 0,
    sample_values       TEXT NOT NULL,   -- JSON array
    invalid_examples    TEXT NOT NULL,   -- JSON array
    recommended_cast    TEXT NOT NULL,
    profiled_at         TEXT NOT NULL,
    PRIMARY KEY (table_name, column_name)
);

CREATE TABLE IF NOT EXISTS exports (
    export_id     TEXT PRIMARY KEY,
    source_tables TEXT NOT NULL,        -- JSON array of referenced tables
    query_hash    TEXT NOT NULL,
    file_name     TEXT NOT NULL,
    file_path     TEXT NOT NULL,
    format        TEXT NOT NULL,
    row_count     INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(catalog_path: Path) -> sqlite3.Connection:
    """Open (creating if needed) the catalog database."""
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(catalog_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# --------------------------------------------------------------------------
# sources
# --------------------------------------------------------------------------

def record_source(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    filename: str,
    relative_path: str,
    size_bytes: int,
    modified_at: str,
) -> None:
    """Insert or refresh the row identified by content hash."""
    now = utc_now()
    conn.execute(
        """
        INSERT INTO sources (source_id, filename, relative_path, size_bytes,
                             modified_at, sha256, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET
            filename = excluded.filename,
            relative_path = excluded.relative_path,
            size_bytes = excluded.size_bytes,
            modified_at = excluded.modified_at,
            last_seen_at = excluded.last_seen_at
        """,
        (source_id, filename, relative_path, size_bytes, modified_at,
         source_id, now, now),
    )


def get_source(conn: sqlite3.Connection, source_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,)).fetchone()
    return dict(row) if row else None


def find_source_by_path(conn: sqlite3.Connection, relative_path: str) -> dict[str, Any] | None:
    """Fast, non-authoritative hint used by list_sources()."""
    row = conn.execute(
        "SELECT * FROM sources WHERE relative_path = ? ORDER BY last_seen_at DESC LIMIT 1",
        (relative_path,),
    ).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------
# imports
# --------------------------------------------------------------------------

def record_import(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    database_path: str,
    table_name: str,
    row_count: int,
) -> None:
    conn.execute(
        """
        INSERT INTO imports (table_name, source_id, database_path, row_count, imported_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(table_name) DO UPDATE SET
            source_id = excluded.source_id,
            database_path = excluded.database_path,
            row_count = excluded.row_count,
            imported_at = excluded.imported_at
        """,
        (table_name, source_id, database_path, row_count, utc_now()),
    )


def find_import_by_source(conn: sqlite3.Connection, source_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM imports WHERE source_id = ? ORDER BY imported_at LIMIT 1",
        (source_id,),
    ).fetchone()
    return dict(row) if row else None


def get_import(conn: sqlite3.Connection, table_name: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM imports WHERE table_name = ?", (table_name,)).fetchone()
    return dict(row) if row else None


def list_imports(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT i.table_name, i.source_id, i.row_count, i.imported_at, i.database_path,
               s.filename, s.relative_path
        FROM imports i
        LEFT JOIN sources s ON s.source_id = i.source_id
        ORDER BY i.table_name
        """
    ).fetchall()
    return [dict(row) for row in rows]


def delete_import(conn: sqlite3.Connection, table_name: str) -> None:
    conn.execute("DELETE FROM imports WHERE table_name = ?", (table_name,))
    conn.execute("DELETE FROM column_profiles WHERE table_name = ?", (table_name,))


# --------------------------------------------------------------------------
# column profiles
# --------------------------------------------------------------------------

_PROFILE_COLUMNS = (
    "source_id", "table_name", "column_name", "storage_type", "logical_type",
    "type_confidence", "row_count", "null_count", "non_null_count",
    "valid_integer_count", "valid_real_count", "valid_date_count",
    "valid_datetime_count", "valid_boolean_count", "invalid_count",
    "distinct_count", "distinct_count_approximate", "sample_values",
    "invalid_examples", "recommended_cast", "profiled_at",
)


def replace_column_profiles(
    conn: sqlite3.Connection, table_name: str, profiles: list[dict[str, Any]]
) -> None:
    """Store a freshly computed profile set, replacing any earlier one."""
    now = utc_now()
    rows = []
    for profile in profiles:
        record = dict(profile)
        record["table_name"] = table_name
        record["profiled_at"] = now
        record["sample_values"] = json.dumps(record.get("sample_values", []))
        record["invalid_examples"] = json.dumps(record.get("invalid_examples", []))
        rows.append(tuple(record[name] for name in _PROFILE_COLUMNS))

    placeholders = ", ".join("?" * len(_PROFILE_COLUMNS))
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM column_profiles WHERE table_name = ?", (table_name,))
        conn.executemany(
            f"INSERT INTO column_profiles ({', '.join(_PROFILE_COLUMNS)}) VALUES ({placeholders})",
            rows,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_column_profiles(conn: sqlite3.Connection, table_name: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM column_profiles WHERE table_name = ? ORDER BY rowid", (table_name,)
    ).fetchall()
    profiles = []
    for row in rows:
        profile = dict(row)
        profile["sample_values"] = json.loads(profile["sample_values"])
        profile["invalid_examples"] = json.loads(profile["invalid_examples"])
        profiles.append(profile)
    return profiles


def get_column_profile(
    conn: sqlite3.Connection, table_name: str, column_name: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM column_profiles WHERE table_name = ? AND column_name = ?",
        (table_name, column_name),
    ).fetchone()
    if row is None:
        return None
    profile = dict(row)
    profile["sample_values"] = json.loads(profile["sample_values"])
    profile["invalid_examples"] = json.loads(profile["invalid_examples"])
    return profile


# --------------------------------------------------------------------------
# exports
# --------------------------------------------------------------------------

def record_export(
    conn: sqlite3.Connection,
    *,
    export_id: str,
    source_tables: list[str],
    query_hash: str,
    file_name: str,
    file_path: str,
    fmt: str,
    row_count: int,
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO exports (export_id, source_tables, query_hash, file_name,
                             file_path, format, row_count, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (export_id, json.dumps(source_tables), query_hash, file_name, file_path,
         fmt, row_count, created_at),
    )
