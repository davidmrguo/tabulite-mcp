"""Streaming CSV -> SQLite ingestion.

One pass over the file does everything: it updates the SHA-256, parses rows and
inserts them in batches. Nothing larger than one batch is ever held in memory,
so source files may be far larger than RAM.

Every field is stored as TEXT. The only interpretation applied at ingest time
is turning configured missing-value markers into SQL NULL; malformed values are
preserved verbatim so that "missing" and "invalid" stay distinguishable.
"""

from __future__ import annotations

import csv
import hashlib
import io
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator

from . import catalog
from .config import Config
from .database import quote, table_columns, table_exists
from .security import SecurityError, safe_table_name

# CSV fields can be large (long free-text columns); raise the parser limit but
# keep it bounded.
csv.field_size_limit(10 * 1024 * 1024)

DEFAULT_ENCODING = "utf-8-sig"
SNIFF_BYTES = 64 * 1024


class HashingReader(io.RawIOBase):
    """Binary wrapper that hashes every byte on its way through.

    Lets the importer compute the content hash without a second pass over the
    file: ``TextIOWrapper`` -> ``csv.reader`` pulls chunks, we hash them here.
    """

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self.hasher = hashlib.sha256()
        self.bytes_read = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        chunk = self._stream.read(len(buffer))
        if not chunk:
            return 0
        buffer[: len(chunk)] = chunk
        self.hasher.update(chunk)
        self.bytes_read += len(chunk)
        return len(chunk)

    @property
    def hexdigest(self) -> str:
        return self.hasher.hexdigest()


@dataclass
class ImportResult:
    table_name: str
    source_id: str
    database_path: str
    row_count: int
    columns: list[dict[str, str]]
    reused_existing: bool
    malformed_rows: int = 0
    bytes_read: int = 0
    batches: int = 0
    warnings: list[str] = field(default_factory=list)


def sniff_delimiter(sample: str, default: str = ",") -> str:
    """Best-effort delimiter detection, falling back to a comma."""
    if not sample.strip():
        return default
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return default


def normalize_column_names(header: list[str]) -> list[dict[str, str]]:
    """Map raw header cells to plain SQLite column names.

    Returns ``[{"source_column": "Order Date", "column_name": "order_date"}]``
    so the AI can see what the original spreadsheet called each field.
    """
    columns: list[dict[str, str]] = []
    used: set[str] = set()
    for index, raw in enumerate(header):
        original = (raw or "").strip()
        name = safe_table_name(original) if original else ""
        if not name:
            name = f"column_{index + 1}"
        candidate = name
        counter = 2
        while candidate in used:
            candidate = f"{name}_{counter}"
            counter += 1
        used.add(candidate)
        columns.append({"source_column": original or f"column_{index + 1}",
                        "column_name": candidate})
    return columns


def _clean_value(value: str | None, null_markers: tuple[str, ...]) -> str | None:
    """Missing-value markers become NULL; everything else is kept as TEXT."""
    if value is None:
        return None
    return None if value in null_markers else value


def _iter_rows(
    reader: Iterator[list[str]],
    width: int,
    null_markers: tuple[str, ...],
) -> Iterator[tuple[tuple[str | None, ...], bool]]:
    """Yield ``(row, malformed)`` padded or trimmed to the header width."""
    for raw_row in reader:
        if not raw_row:
            continue  # truly blank line; csv.reader yields [] for these
        malformed = len(raw_row) != width
        cells = raw_row[:width] + [None] * max(0, width - len(raw_row))
        yield tuple(_clean_value(cell, null_markers) for cell in cells), malformed


def file_metadata(path: Path, source_dir: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "filename": path.name,
        "relative_path": str(path.relative_to(source_dir)),
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        .isoformat(timespec="seconds"),
    }


def inspect_csv(
    path: Path,
    config: Config,
    catalog_conn: sqlite3.Connection,
    sample_rows: int = 5,
) -> dict[str, Any]:
    """Peek at a CSV without reading more than a small prefix of it."""
    meta = file_metadata(path, config.source_dir)

    with path.open("rb") as handle:
        prefix = handle.read(SNIFF_BYTES)
    text = prefix.decode(DEFAULT_ENCODING, errors="replace")
    delimiter = sniff_delimiter(text)

    # Drop a trailing partial line so the parser never sees half a record.
    if len(prefix) == SNIFF_BYTES and "\n" in text:
        text = text[: text.rindex("\n") + 1]

    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        header = []

    columns = normalize_column_names(header)
    rows: list[list[str | None]] = []
    for row, _ in _iter_rows(reader, len(columns), config.null_markers):
        rows.append(list(row))
        if len(rows) >= sample_rows:
            break

    hint = catalog.find_source_by_path(catalog_conn, meta["relative_path"])
    already: dict[str, Any] | None = None
    if hint and hint["size_bytes"] == meta["size_bytes"]:
        existing = catalog.find_import_by_source(catalog_conn, hint["source_id"])
        if existing:
            already = {
                "table_name": existing["table_name"],
                "source_id": existing["source_id"],
                "imported_at": existing["imported_at"],
            }

    return {
        **meta,
        "delimiter": delimiter,
        "column_count": len(columns),
        "columns": columns,
        "sample_rows": rows,
        "sample_truncated": len(prefix) == SNIFF_BYTES,
        "encoding": DEFAULT_ENCODING,
        "null_markers": list(config.null_markers),
        # Path/size match only; SHA-256 during import is authoritative.
        "already_imported_hint": already,
    }


# Suffix lengths tried in turn when a table name is already taken. Six hex
# characters is plenty to separate a handful of same-named CSVs; the longer
# fallbacks exist so the function is total rather than because they are likely.
TABLE_SUFFIX_LENGTHS = (6, 12, 64)


def deterministic_table_name(
    preferred: str, source_id: str, taken: Callable[[str], bool]
) -> str:
    """Pick a table name that depends only on the file, never on import order.

    All imports share one database, so names can collide — two directories can
    both hold a ``sales.csv``. The loser of a collision gets a suffix from its
    own content hash (``sales`` and ``sales_4b11d3``) rather than an
    incrementing counter, so the same file always lands on the same name
    regardless of what was imported before it. The catalog remains the
    authoritative mapping from source file and hash to table name.
    """
    if not taken(preferred):
        return preferred

    for length in TABLE_SUFFIX_LENGTHS:
        candidate = f"{preferred}_{source_id[:length]}"
        if not taken(candidate):
            return candidate

    raise SecurityError(
        f"could not find a free table name for '{preferred}' (source {source_id[:12]})"
    )


def import_csv(
    path: Path,
    config: Config,
    db_conn: sqlite3.Connection,
    catalog_conn: sqlite3.Connection,
    *,
    table_name: str | None = None,
    delimiter: str | None = None,
    force: bool = False,
) -> ImportResult:
    """Stream a CSV into a TEXT-only SQLite table.

    Rows land in a temporary table while the file is read, because the content
    hash — the only authoritative identity — is not known until EOF. At that
    point the temporary table is either renamed into place or dropped in favor
    of the existing import of identical content.
    """
    meta = file_metadata(path, config.source_dir)

    if delimiter is None:
        with path.open("rb") as handle:
            delimiter = sniff_delimiter(handle.read(SNIFF_BYTES).decode(DEFAULT_ENCODING,
                                                                       errors="replace"))

    staging = f"_tmp_import_{uuid.uuid4().hex[:12]}"
    malformed = 0
    inserted = 0
    batches = 0
    warnings: list[str] = []

    with path.open("rb") as raw_handle:
        hashing = HashingReader(raw_handle)
        stream = io.TextIOWrapper(
            io.BufferedReader(hashing, buffer_size=config.read_chunk_bytes),
            encoding=DEFAULT_ENCODING,
            errors="replace",
            newline="",
        )
        reader = csv.reader(stream, delimiter=delimiter)

        try:
            header = next(reader)
        except StopIteration:
            raise SecurityError(f"CSV file is empty: {meta['relative_path']}")

        columns = normalize_column_names(header)
        if not columns:
            raise SecurityError(f"CSV file has no columns: {meta['relative_path']}")

        column_sql = ", ".join(f"{quote(c['column_name'])} TEXT" for c in columns)
        db_conn.execute(f"CREATE TABLE {quote(staging)} ({column_sql})")
        placeholders = ", ".join("?" * len(columns))
        insert_sql = f"INSERT INTO {quote(staging)} VALUES ({placeholders})"

        db_conn.execute("BEGIN")
        try:
            batch: list[tuple[str | None, ...]] = []
            for row, is_malformed in _iter_rows(reader, len(columns), config.null_markers):
                if is_malformed:
                    malformed += 1
                batch.append(row)
                if len(batch) >= config.insert_batch_size:
                    db_conn.executemany(insert_sql, batch)
                    inserted += len(batch)
                    batches += 1
                    batch.clear()
            if batch:
                db_conn.executemany(insert_sql, batch)
                inserted += len(batch)
                batches += 1
            db_conn.execute("COMMIT")
        except Exception:
            db_conn.execute("ROLLBACK")
            db_conn.execute(f"DROP TABLE IF EXISTS {quote(staging)}")
            raise

        # Drain any bytes the CSV reader left unread so the hash covers the
        # whole file even for trailing blank lines.
        while stream.buffer.read(config.read_chunk_bytes):
            pass
        source_id = hashing.hexdigest

    if malformed:
        warnings.append(f"{malformed} row(s) did not match the header width and were padded/trimmed")

    existing = catalog.find_import_by_source(catalog_conn, source_id)
    if not force and existing and table_exists(db_conn, existing["table_name"]):
        db_conn.execute(f"DROP TABLE IF EXISTS {quote(staging)}")
        catalog.record_source(catalog_conn, source_id=source_id, **meta)
        existing_columns = [
            {"source_column": name, "column_name": name}
            for name, _ in table_columns(db_conn, existing["table_name"])
        ]
        return ImportResult(
            table_name=existing["table_name"],
            source_id=source_id,
            database_path=str(config.database_path),
            row_count=int(existing["row_count"]),
            columns=existing_columns,
            reused_existing=True,
            bytes_read=hashing.bytes_read,
            warnings=warnings + [
                "identical content (same SHA-256) was already imported; reusing the existing table"
            ],
        )

    preferred = safe_table_name(table_name or Path(meta["filename"]).stem)
    if force and existing:
        final_name = existing["table_name"]
        db_conn.execute(f"DROP TABLE IF EXISTS {quote(final_name)}")
        catalog.delete_import(catalog_conn, final_name)
    else:
        final_name = deterministic_table_name(
            preferred, source_id, lambda name: table_exists(db_conn, name)
        )

    db_conn.execute(f"ALTER TABLE {quote(staging)} RENAME TO {quote(final_name)}")

    catalog.record_source(catalog_conn, source_id=source_id, **meta)
    catalog.record_import(
        catalog_conn,
        source_id=source_id,
        database_path=str(config.database_path),
        table_name=final_name,
        row_count=inserted,
    )

    return ImportResult(
        table_name=final_name,
        source_id=source_id,
        database_path=str(config.database_path),
        row_count=inserted,
        columns=columns,
        reused_existing=False,
        malformed_rows=malformed,
        bytes_read=hashing.bytes_read,
        batches=batches,
        warnings=warnings,
    )
