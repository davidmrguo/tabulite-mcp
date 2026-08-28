"""MCP server exposing a local SQLite runtime over Streamable HTTP.

The tool surface is deliberately small and deterministic: discover sources,
import them, profile them, run read-only SQL, export results. There is no LLM,
no natural-language-to-SQL and no domain logic in here — the desktop AI client
is the reasoning layer.
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Iterator

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__, catalog, database, exporter, importer, profiler
from .config import Config
from .confirm import CONFIRMATION_WORD, ConfirmationError, ConfirmationRegistry
from .database import QueryTimeout
from .security import SecurityError, resolve_source_path, validate_read_only_sql

logger = logging.getLogger("tabulite_mcp")

CONFIG = Config.from_env()

SOURCE_SUFFIXES = {".csv", ".tsv"}

# Pending destructive actions, keyed by single-use token. Process-local: a
# restart cancels anything awaiting confirmation.
CONFIRMATIONS = ConfirmationRegistry()

INSTRUCTIONS = """\
A local SQLite runtime sitting next to large CSV files.

Typical flow: list_sources -> import_source -> profile_table -> query_sql, and
export_query when the final result is too large for the conversation.

CSV fields are stored as TEXT. Use the TRY_* functions in your SQL to convert
values safely: TRY_INTEGER, TRY_REAL, TRY_DATE, TRY_DATETIME, TRY_BOOLEAN.
They return NULL for missing or malformed values instead of the misleading
zeros ordinary CAST() produces, so AVG()/SUM() skip them. Check denominators
with COUNT(TRY_REAL(col)) against COUNT(*) when a number matters.

Aggregate inside SQLite rather than pulling raw rows: query_sql is row-capped.

delete_table is destructive and deliberately two-step: call it once to get a
warning, show that warning to the user, and only call it again once the user
has typed DELETE themselves.
"""

server = MCPServer(
    name="tabulite-mcp",
    title="Tabulite — local CSV analysis in SQLite",
    instructions=INSTRUCTIONS,
    version=__version__,
)


# --------------------------------------------------------------------------
# Connection plumbing
# --------------------------------------------------------------------------

def _bootstrap() -> None:
    """Create the workspace layout and both database files if needed."""
    CONFIG.ensure_directories()
    conn = database.connect_writable(CONFIG.database_path)
    conn.close()
    catalog.connect(CONFIG.catalog_path).close()


@contextlib.contextmanager
def _writable() -> Iterator[tuple[sqlite3.Connection, sqlite3.Connection]]:
    """Writable analytical connection plus the catalog connection."""
    _bootstrap()
    db = database.connect_writable(CONFIG.database_path)
    cat = catalog.connect(CONFIG.catalog_path)
    try:
        yield db, cat
    finally:
        db.close()
        cat.close()


@contextlib.contextmanager
def _read_only() -> Iterator[tuple[sqlite3.Connection, sqlite3.Connection]]:
    """Hardened read-only analytical connection plus the catalog connection."""
    _bootstrap()
    db = database.connect_read_only(CONFIG.database_path)
    cat = catalog.connect(CONFIG.catalog_path)
    try:
        yield db, cat
    finally:
        db.close()
        cat.close()


def _fail(exc: Exception) -> ToolError:
    """Surface an anticipated failure to the client with its reason intact.

    ToolError messages reach the model (unlike arbitrary exceptions, which the
    SDK masks), so the AI can correct its own SQL or path.
    """
    return ToolError(str(exc))


def _require_table(db: sqlite3.Connection, table_name: str) -> str:
    if table_name not in database.list_table_names(db):
        raise ToolError(f"unknown table '{table_name}'; call list_tables() first")
    return table_name


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------

@server.tool()
def list_sources() -> dict[str, Any]:
    """List the CSV files available under the project source directory.

    Returns each file's relative path, size and modification time, plus — when
    the catalog recognizes it — the source_id and the table it was imported
    into. Nothing outside the source directory is ever listed.
    """
    _bootstrap()
    cat = catalog.connect(CONFIG.catalog_path)
    try:
        files: list[dict[str, Any]] = []
        for path in sorted(CONFIG.source_dir.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.suffix.lower() not in SOURCE_SUFFIXES:
                continue

            meta = importer.file_metadata(path, CONFIG.source_dir)
            entry: dict[str, Any] = {**meta, "import_status": "not imported",
                                     "source_id": None, "table_name": None}

            hint = catalog.find_source_by_path(cat, meta["relative_path"])
            if hint:
                imported = catalog.find_import_by_source(cat, hint["source_id"])
                same_size = hint["size_bytes"] == meta["size_bytes"]
                if imported and same_size:
                    entry.update(
                        source_id=hint["source_id"],
                        table_name=imported["table_name"],
                        import_status="imported",
                        imported_at=imported["imported_at"],
                    )
                elif imported:
                    entry["import_status"] = "changed since import"
            files.append(entry)

        return {
            "source_dir": str(CONFIG.source_dir),
            "file_count": len(files),
            "files": files,
        }
    finally:
        cat.close()


@server.tool()
def inspect_source(path: str) -> dict[str, Any]:
    """Peek at a CSV file without importing it or reading all of it.

    Reads only a small prefix of the file and returns the detected delimiter,
    the column names (both as written in the header and as they will be stored)
    and a handful of sample rows.
    """
    try:
        resolved = resolve_source_path(path, CONFIG.source_dir)
    except SecurityError as exc:
        raise _fail(exc) from exc

    _bootstrap()
    cat = catalog.connect(CONFIG.catalog_path)
    try:
        return importer.inspect_csv(resolved, CONFIG, cat)
    finally:
        cat.close()


@server.tool()
def import_source(
    path: str,
    table_name: str | None = None,
    delimiter: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Import a CSV file into SQLite and profile its columns.

    The file is streamed, so size is limited by disk rather than memory. Every
    field is stored as TEXT; configured missing-value markers (by default "",
    NULL, null, N/A, NA) become SQL NULL while other unparseable values are kept
    verbatim.

    Identity is the SHA-256 of the file contents, computed during the same pass.
    Re-importing identical content — even under a different filename — reuses
    the existing table instead of duplicating it. Pass force=True to re-import
    anyway.
    """
    try:
        resolved = resolve_source_path(path, CONFIG.source_dir)
    except SecurityError as exc:
        raise _fail(exc) from exc

    with _writable() as (db, cat):
        try:
            result = importer.import_csv(
                resolved, CONFIG, db, cat,
                table_name=table_name, delimiter=delimiter, force=force,
            )
        except SecurityError as exc:
            raise _fail(exc) from exc

        profiles = profiler.profile_table(db, result.table_name, result.source_id, CONFIG)
        catalog.replace_column_profiles(cat, result.table_name, profiles)

    return {
        "table_name": result.table_name,
        "source_id": result.source_id,
        "database_path": result.database_path,
        "row_count": result.row_count,
        "column_count": len(result.columns),
        "columns": result.columns,
        "reused_existing_import": result.reused_existing,
        "malformed_rows": result.malformed_rows,
        "bytes_read": result.bytes_read,
        "insert_batches": result.batches,
        "warnings": result.warnings,
        "profile_summary": [
            {
                "column_name": p["column_name"],
                "logical_type": p["logical_type"],
                "recommended_cast": p["recommended_cast"],
            }
            for p in profiles
        ],
    }


@server.tool()
def list_tables() -> dict[str, Any]:
    """List the analytical tables that have been imported into the project."""
    with _read_only() as (db, cat):
        recorded = {row["table_name"]: row for row in catalog.list_imports(cat)}
        tables = []
        for name in database.list_table_names(db):
            entry: dict[str, Any] = {
                "table_name": name,
                "row_count": database.row_count(db, name),
                "columns": database.column_names(db, name),
            }
            meta = recorded.get(name)
            if meta:
                entry.update(
                    source_id=meta["source_id"],
                    source_filename=meta["filename"],
                    source_relative_path=meta["relative_path"],
                    imported_at=meta["imported_at"],
                )
            tables.append(entry)

    return {"database_path": str(CONFIG.database_path), "table_count": len(tables),
            "tables": tables}


@server.tool()
def profile_table(table_name: str, refresh: bool = False) -> dict[str, Any]:
    """Return a compact profile of every column in an imported table.

    Storage type is always TEXT; logical_type is what the values appear to mean
    and recommended_cast is the TRY_* function to use in SQL. Profiles are
    evidence — the stored data is never modified to match them.
    """
    with _writable() as (db, cat):
        _require_table(db, table_name)
        profiles = [] if refresh else catalog.get_column_profiles(cat, table_name)
        if not profiles:
            record = catalog.get_import(cat, table_name)
            source_id = record["source_id"] if record else ""
            computed = profiler.profile_table(db, table_name, source_id, CONFIG)
            catalog.replace_column_profiles(cat, table_name, computed)
            profiles = catalog.get_column_profiles(cat, table_name)
        rows = database.row_count(db, table_name)

    return {
        "table_name": table_name,
        "row_count": rows,
        "columns": [
            {
                "column_name": p["column_name"],
                "storage_type": p["storage_type"],
                "logical_type": p["logical_type"],
                "type_confidence": p["type_confidence"],
                "null_count": p["null_count"],
                "invalid_count": p["invalid_count"],
                "distinct_count": p["distinct_count"],
                "recommended_cast": p["recommended_cast"],
            }
            for p in profiles
        ],
        "profiled_at": profiles[0]["profiled_at"] if profiles else None,
    }


@server.tool()
def profile_column(table_name: str, column_name: str) -> dict[str, Any]:
    """Return the full profile of a single column, including examples.

    Use this when profile_table() shows a surprising invalid_count and you need
    to see which values are failing conversion.
    """
    with _writable() as (db, cat):
        _require_table(db, table_name)
        profile = catalog.get_column_profile(cat, table_name, column_name)
        if profile is None:
            record = catalog.get_import(cat, table_name)
            source_id = record["source_id"] if record else ""
            computed = profiler.profile_table(db, table_name, source_id, CONFIG)
            catalog.replace_column_profiles(cat, table_name, computed)
            profile = catalog.get_column_profile(cat, table_name, column_name)
        if profile is None:
            raise ToolError(f"unknown column '{column_name}' in table '{table_name}'")
    return profile


@server.tool()
def sample_table(table_name: str, limit: int = 20) -> dict[str, Any]:
    """Return a few rows from a table for semantic inspection.

    Capped conservatively — this is for understanding what the data looks like,
    not for pulling the dataset into the conversation.
    """
    if limit < 1:
        raise ToolError("limit must be at least 1")
    limit = min(limit, CONFIG.max_sample_rows)

    with _read_only() as (db, _):
        _require_table(db, table_name)
        sql = f"SELECT * FROM {database.quote(table_name)} LIMIT {int(limit)}"
        result = database.execute_query(db, sql, limit, CONFIG.query_timeout_seconds)
    result["table_name"] = table_name
    return result


@server.tool()
def query_sql(sql: str) -> dict[str, Any]:
    """Execute read-only SQLite SQL against the imported tables.

    This is the primary analytical tool: write whatever SELECT / WITH / join /
    window-function query answers the question. Only read-only statements run —
    the connection is opened read-only and a SQLite authorizer denies anything
    that would modify data or attach files.

    Wrap TEXT columns in TRY_REAL / TRY_INTEGER / TRY_DATE / TRY_DATETIME /
    TRY_BOOLEAN for safe conversion. Results are capped; when `truncated` is
    true, aggregate further in SQL or use export_query() instead.
    """
    try:
        statement = validate_read_only_sql(sql)
    except SecurityError as exc:
        raise _fail(exc) from exc

    with _read_only() as (db, _):
        try:
            result = database.execute_query(
                db, statement, CONFIG.max_query_rows, CONFIG.query_timeout_seconds
            )
        except QueryTimeout as exc:
            raise _fail(exc) from exc
        except sqlite3.DatabaseError as exc:
            raise ToolError(f"SQL error: {exc}") from exc
    return result


@server.tool()
def export_query(sql: str, file_name: str | None = None, format: str = "csv") -> dict[str, Any]:
    """Run a read-only query and stream the complete result to a file.

    The result is written under workspace/exports without passing through the
    conversation and without the row cap that applies to query_sql(). Use this
    when the user wants the dataset itself.

    Formats: csv (default) or json. Filenames are sanitized and never
    overwrite an existing export; omit file_name for a generated one.
    """
    try:
        statement = validate_read_only_sql(sql)
    except SecurityError as exc:
        raise _fail(exc) from exc

    with _read_only() as (db, cat):
        try:
            return exporter.export_query(
                db, cat, statement, CONFIG,
                file_name=file_name, fmt=format,
                known_tables=database.list_table_names(db),
            )
        except (SecurityError, QueryTimeout) as exc:
            raise _fail(exc) from exc
        except sqlite3.DatabaseError as exc:
            raise ToolError(f"SQL error: {exc}") from exc


@server.tool(
    annotations=ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False
    )
)
def delete_table(
    table_name: str,
    confirm: str | None = None,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    """Permanently delete an imported table, its profile and its catalog record.

    Use when an import went wrong and the user wants to start over, or when
    they are finished and want the disk space back. The table, its column
    profiles and its import record are removed and the database file is
    compacted. The CSV in source/ is never touched, and files already written
    to workspace/exports/ are left alone.

    THIS TOOL IS TWO-STEP AND YOU MUST NOT SHORT-CIRCUIT IT.

    Step 1 - call with table_name only. Nothing is deleted. You get back a
    warning describing exactly what would be lost and a confirmation_token.
    Show the user that warning, including whether the source CSV is still
    available to re-import from, and ask them to reply with DELETE in capitals.

    Step 2 - only after the user has themselves typed DELETE, call again with
    confirm="DELETE" and the confirmation_token from step 1.

    Never invent the confirmation on the user's behalf, never pass confirm on
    a first call, and never treat "yes", "go ahead" or "delete it" as the
    confirmation - ask them for the exact word. If they decline or say
    anything else, simply do not call this tool again.
    """
    with _writable() as (db, cat):
        _require_table(db, table_name)
        record = catalog.get_import(cat, table_name)
        source_id = record["source_id"] if record else None
        source = catalog.get_source(cat, source_id) if source_id else None
        rows = database.row_count(db, table_name)
        columns = [name for name, _ in database.table_columns(db, table_name)]
        profiles = catalog.get_column_profiles(cat, table_name)

        source_path = source["relative_path"] if source else None
        source_present = bool(
            source_path and (CONFIG.source_dir / source_path).is_file()
        )

        # ---- Step 1: warn, and hand back a single-use token ----------------
        if confirm is None and confirmation_token is None:
            token, expires_at = CONFIRMATIONS.issue("delete_table", table_name)
            if source_present:
                recovery = (
                    f"The source file {source_path} is still in source/, so the table "
                    "could be rebuilt with import_source() afterwards."
                )
            else:
                recovery = (
                    "WARNING: the source CSV is NOT in source/ any more, so this table "
                    "CANNOT be re-imported. Deleting it destroys the only copy of this "
                    "data held by this server."
                )
            return {
                "status": "confirmation_required",
                "table_name": table_name,
                "warning": (
                    f"This will permanently delete the table '{table_name}' "
                    f"({rows:,} rows, {len(columns)} columns) along with its column "
                    f"profiles and its entry in the catalog. {recovery}"
                ),
                "will_delete": {
                    "table": table_name,
                    "rows": rows,
                    "columns": columns,
                    "column_profiles": len(profiles),
                    "catalog_import_record": record is not None,
                },
                "will_keep": [
                    "the original CSV in source/",
                    "any files already written to workspace/exports/",
                ],
                "source_file": {
                    "relative_path": source_path,
                    "present": source_present,
                    "re_importable": source_present,
                },
                "confirmation_token": token,
                "expires_at": datetime.fromtimestamp(expires_at, timezone.utc)
                .isoformat(timespec="seconds"),
                "next_step": (
                    f"Show the warning to the user and ask them to reply with "
                    f"{CONFIRMATION_WORD} in capitals. Only if they do, call "
                    f"delete_table(table_name={table_name!r}, "
                    f"confirm=\"{CONFIRMATION_WORD}\", "
                    f"confirmation_token={token!r})."
                ),
            }

        # ---- Step 2: verify the confirmation, then delete ------------------
        try:
            CONFIRMATIONS.consume(confirmation_token, "delete_table", table_name, confirm)
        except ConfirmationError as exc:
            raise _fail(exc) from exc

        started = time.monotonic()
        # Fold in the WAL first: a size read with one outstanding is meaningless.
        database.checkpoint_wal(db)
        size_before = database.database_size_bytes(CONFIG.database_path)

        database.drop_table(db, table_name)
        catalog.delete_import(cat, table_name)
        source_removed = (
            catalog.delete_source_if_unreferenced(cat, source_id) if source_id else False
        )
        database.vacuum(db)

    # Measured after the connection closes, so it matches the file on disk.
    size_after = database.database_size_bytes(CONFIG.database_path)

    logger.info("deleted table %s (%s rows) after user confirmation", table_name, rows)
    return {
        "status": "deleted",
        "table_name": table_name,
        "rows_deleted": rows,
        "column_profiles_deleted": len(profiles),
        "catalog_source_record_removed": source_removed,
        "database_bytes_before": size_before,
        "database_bytes_after": size_after,
        "bytes_freed": max(0, size_before - size_after),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "source_file_kept": source_path,
        "note": (
            "The table and its profiles are gone and the database has been compacted. "
            "The CSV in source/ and any previous exports were not touched."
            + ("" if source_present else " The source CSV is no longer present, so this"
               " data cannot be re-imported.")
        ),
    }


# --------------------------------------------------------------------------
# HTTP extras
# --------------------------------------------------------------------------

@server.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """Liveness probe for Docker."""
    return JSONResponse(
        {
            "status": "ok",
            "source_dir": str(CONFIG.source_dir),
            "workspace_dir": str(CONFIG.workspace_dir),
            "source_dir_present": CONFIG.source_dir.is_dir(),
        }
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    _bootstrap()
    logger.info("source dir:    %s", CONFIG.source_dir)
    logger.info("workspace dir: %s", CONFIG.workspace_dir)
    logger.info("listening on http://%s:%s/mcp", CONFIG.host, CONFIG.port)

    server.run(
        transport="streamable-http",
        host=CONFIG.host,
        port=CONFIG.port,
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(CONFIG.allowed_hosts),
            allowed_origins=list(CONFIG.allowed_origins),
        ),
    )


if __name__ == "__main__":
    main()
