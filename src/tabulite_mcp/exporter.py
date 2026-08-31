"""Streaming export of complete query results.

Interactive queries are capped so they cannot flood the AI's context. Exports
are the escape hatch: the same read-only SQL runs directly against SQLite and
rows are streamed from the cursor to a file with ``fetchmany``, so the full
result never has to fit in memory (or in the conversation).
"""

from __future__ import annotations

import csv
import hashlib
import re
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import catalog
from .config import Config
from .database import QueryTimeout, clear_deadline, iter_batches, set_deadline
from .security import (
    SecurityError,
    sanitize_output_filename,
    scrub_sql,
    unique_output_path,
)

EXPORT_BATCH = 5_000
SUPPORTED_FORMATS = ("csv", "json")


def query_hash(sql: str) -> str:
    return hashlib.sha256(" ".join(sql.split()).encode("utf-8")).hexdigest()


def referenced_tables(sql: str, known_tables: list[str]) -> list[str]:
    """Best-effort list of imported tables mentioned by a statement."""
    tokens = set(re.findall(r"[a-z_][a-z_0-9]*", scrub_sql(sql).lower()))
    return sorted(name for name in known_tables if name.lower() in tokens)


def default_file_name(fmt: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"export_{stamp}_{uuid.uuid4().hex[:6]}.{fmt}"


def _write_csv(handle: Any, cursor: sqlite3.Cursor, columns: list[str]) -> int:
    writer = csv.writer(handle)
    writer.writerow(columns)
    written = 0
    for batch in iter_batches(cursor, EXPORT_BATCH):
        writer.writerows(batch)
        written += len(batch)
    return written


def _write_json(handle: Any, cursor: sqlite3.Cursor, columns: list[str]) -> int:
    """Write a JSON array of objects incrementally, one row at a time."""
    handle.write("[")
    written = 0
    for batch in iter_batches(cursor, EXPORT_BATCH):
        for row in batch:
            if written:
                handle.write(",")
            handle.write("\n  ")
            handle.write(json.dumps(dict(zip(columns, row)), default=str))
            written += 1
    handle.write("\n]\n" if written else "]\n")
    return written


def export_query(
    conn: sqlite3.Connection,
    catalog_conn: sqlite3.Connection,
    sql: str,
    config: Config,
    *,
    file_name: str | None = None,
    fmt: str = "csv",
    known_tables: list[str] | None = None,
) -> dict[str, Any]:
    """Run a validated read-only statement and stream the result to a file."""
    fmt = fmt.lower().strip()
    if fmt not in SUPPORTED_FORMATS:
        raise SecurityError(f"unsupported export format '{fmt}'; use one of {SUPPORTED_FORMATS}")

    config.exports_dir.mkdir(parents=True, exist_ok=True)
    safe_name = (sanitize_output_filename(file_name, fmt, "export") if file_name
                 else default_file_name(fmt))
    target = unique_output_path(config.exports_dir, safe_name)

    set_deadline(conn, config.export_timeout_seconds)
    try:
        cursor = conn.execute(sql)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        with target.open("w", newline="", encoding="utf-8") as handle:
            if fmt == "csv":
                written = _write_csv(handle, cursor, columns)
            else:
                written = _write_json(handle, cursor, columns)
    except sqlite3.OperationalError as exc:
        target.unlink(missing_ok=True)
        if "interrupted" in str(exc).lower():
            raise QueryTimeout(
                f"export exceeded the {config.export_timeout_seconds:g}s time limit"
            ) from exc
        raise
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        clear_deadline(conn)

    export_id = uuid.uuid4().hex
    created_at = catalog.utc_now()
    tables = referenced_tables(sql, known_tables or [])

    catalog.record_export(
        catalog_conn,
        export_id=export_id,
        source_tables=tables,
        query_hash=query_hash(sql),
        file_name=target.name,
        file_path=str(target),
        fmt=fmt,
        row_count=written,
        created_at=created_at,
    )

    return {
        "export_id": export_id,
        "file_name": target.name,
        "relative_path": str(Path("exports") / target.name),
        "absolute_path": str(target),
        "format": fmt,
        "row_count": written,
        "columns": columns,
        "file_size_bytes": target.stat().st_size,
        "source_tables": tables,
        "created_at": created_at,
    }
