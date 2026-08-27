"""SQLite connection helpers.

Plain ``sqlite3``: a writable connection for ingestion and a hardened
read-only connection for anything the AI generates.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .casting import register_try_functions
from .security import (
    disable_extension_loading,
    install_read_only_authorizer,
    quote_identifier,
)

# How many SQLite VM instructions between deadline checks.
PROGRESS_HANDLER_OPS = 10_000


class QueryTimeout(Exception):
    """Raised when a statement runs past its deadline."""


def connect_writable(path: Path) -> sqlite3.Connection:
    """Open a read/write connection, creating the database if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    disable_extension_loading(conn)
    register_try_functions(conn)
    return conn


def connect_read_only(path: Path) -> sqlite3.Connection:
    """Open a connection that cannot write, for AI-generated SQL.

    Four layers, applied in this order because each one restricts what the next
    step is allowed to do:

    1. ``file:...?mode=ro`` — the file is opened read-only by the OS;
    2. ``PRAGMA query_only=ON`` — SQLite itself refuses writes on this handle;
    3. extension loading disabled explicitly;
    4. ``set_authorizer()`` — the primary semantic layer, denying every action
       except the reads, functions and recursion an analytical SELECT needs.

    The pragma has to be set before the authorizer is installed, because the
    authorizer denies PRAGMA. The SQL scrubber in ``security`` sits in front of
    all of this as defense in depth and to give clearer early errors — it is not
    what makes the path safe.
    """
    if not path.exists():
        raise FileNotFoundError(f"database does not exist yet: {path}")

    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
    conn.execute("PRAGMA query_only=ON")
    disable_extension_loading(conn)
    register_try_functions(conn)
    install_read_only_authorizer(conn)
    return conn


def set_deadline(conn: sqlite3.Connection, seconds: float) -> None:
    """Abort statements on this connection after ``seconds``.

    SQLite calls the progress handler every N virtual-machine steps; returning
    a non-zero value interrupts the running statement.
    """
    deadline = time.monotonic() + seconds

    def handler() -> int:
        return 1 if time.monotonic() > deadline else 0

    conn.set_progress_handler(handler, PROGRESS_HANDLER_OPS)


def clear_deadline(conn: sqlite3.Connection) -> None:
    conn.set_progress_handler(None, 0)


def iter_batches(cursor: sqlite3.Cursor, size: int) -> Iterator[list[tuple[Any, ...]]]:
    """Yield result rows in batches; never materializes the whole result."""
    while True:
        rows = cursor.fetchmany(size)
        if not rows:
            return
        yield rows


def quote(identifier: str) -> str:
    """Short alias for the identifier quoter used across the SQL builders."""
    return quote_identifier(identifier)


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone()
    return row is not None


def list_table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "AND name NOT LIKE '_tmp_import_%' ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def table_columns(conn: sqlite3.Connection, table_name: str) -> list[tuple[str, str]]:
    """Return ``(column_name, declared_type)`` pairs for a table.

    Uses PRAGMA, so it needs a writable connection: the read-only connection's
    authorizer denies pragmas. Use :func:`column_names` there.
    """
    rows = conn.execute(f"PRAGMA table_info({quote(table_name)})").fetchall()
    return [(row[1], row[2] or "TEXT") for row in rows]


def column_names(conn: sqlite3.Connection, table_name: str) -> list[str]:
    """Column names via an empty SELECT, which read-only connections allow."""
    cursor = conn.execute(f"SELECT * FROM {quote(table_name)} LIMIT 0")
    return [d[0] for d in cursor.description or []]


def row_count(conn: sqlite3.Connection, table_name: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {quote(table_name)}").fetchone()[0])


def execute_query(
    conn: sqlite3.Connection,
    sql: str,
    max_rows: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run an already-validated read-only statement with a row cap.

    Fetches one row more than the cap so truncation can be reported honestly.
    """
    started = time.monotonic()
    set_deadline(conn, timeout_seconds)
    try:
        cursor = conn.execute(sql)
        rows = cursor.fetchmany(max_rows + 1)
        columns = [d[0] for d in cursor.description] if cursor.description else []
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).lower():
            raise QueryTimeout(
                f"query exceeded the {timeout_seconds:g}s time limit and was canceled"
            ) from exc
        raise
    finally:
        clear_deadline(conn)

    truncated = len(rows) > max_rows
    if truncated:
        rows = rows[:max_rows]

    return {
        "columns": columns,
        "rows": [list(row) for row in rows],
        "returned_rows": len(rows),
        "truncated": truncated,
        "row_limit": max_rows,
        "execution_time_seconds": round(time.monotonic() - started, 4),
    }
