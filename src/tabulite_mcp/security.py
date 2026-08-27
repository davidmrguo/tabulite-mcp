"""Path and SQL safety.

Two jobs:

* keep every filesystem operation inside the project source/workspace dirs;
* keep every AI-generated statement read-only.

SQL safety is layered rather than a naive prefix check: the statement is
scrubbed and inspected, the connection is opened read-only with
``PRAGMA query_only``, and a ``set_authorizer`` callback denies every action
except reading, selecting and calling safe functions.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger("tabulite_mcp.security")

# Statement kinds we are willing to run.
_ALLOWED_LEADING_KEYWORDS = {"SELECT", "WITH", "VALUES"}

# Belt-and-braces list; the authorizer is what actually enforces read-only.
_FORBIDDEN_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
    "ATTACH", "DETACH", "VACUUM", "PRAGMA", "REINDEX", "ANALYZE", "TRIGGER",
    "TRUNCATE", "GRANT", "COMMIT", "ROLLBACK", "SAVEPOINT", "BEGIN",
}

# REPLACE is both a mutating statement (REPLACE INTO) and a perfectly ordinary
# scalar function (replace(text, from, to)), so it is checked by context.
_CONTEXTUAL_KEYWORDS = {"REPLACE"}

# SQLite functions that touch the filesystem or the process.
_FORBIDDEN_FUNCTIONS = {
    "load_extension", "readfile", "writefile", "edit", "fts3_tokenizer",
    "sqlite_compileoption_get", "sqlite_compileoption_used",
}

_IDENTIFIER_SAFE = re.compile(r"[^0-9a-zA-Z_]+")
_FILENAME_SAFE = re.compile(r"[^0-9a-zA-Z._-]+")


class SecurityError(Exception):
    """Raised when a path or statement is not allowed."""


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def _resolve_inside(base: Path, candidate: Path) -> Path:
    """Resolve ``candidate`` and confirm it stays inside ``base``."""
    base_resolved = base.resolve()
    resolved = candidate.resolve()
    if resolved != base_resolved and base_resolved not in resolved.parents:
        raise SecurityError(f"path escapes the allowed directory: {candidate}")
    return resolved


def resolve_source_path(relative_path: str, source_dir: Path) -> Path:
    """Resolve a user/AI supplied source path inside the source directory."""
    if not relative_path or not relative_path.strip():
        raise SecurityError("source path is empty")
    if "\x00" in relative_path:
        raise SecurityError("source path contains a null byte")

    candidate = Path(relative_path)
    if candidate.is_absolute():
        # Absolute paths are accepted only when they already point inside the
        # source directory (an AI client may echo back what list_sources gave).
        resolved = _resolve_inside(source_dir, candidate)
    else:
        if ".." in candidate.parts:
            raise SecurityError(f"path traversal is not allowed: {relative_path}")
        resolved = _resolve_inside(source_dir, source_dir / candidate)

    if not resolved.exists():
        raise SecurityError(f"source file not found: {relative_path}")
    if not resolved.is_file():
        raise SecurityError(f"source path is not a file: {relative_path}")
    return resolved


def sanitize_export_filename(file_name: str, fmt: str) -> str:
    """Turn an arbitrary requested name into a bare, safe filename."""
    if "\x00" in file_name:
        raise SecurityError("export file name contains a null byte")
    if "/" in file_name or "\\" in file_name:
        raise SecurityError(f"export file name must not contain a path: {file_name}")
    if ".." in file_name:
        raise SecurityError(f"export file name must not contain '..': {file_name}")

    cleaned = _FILENAME_SAFE.sub("_", file_name).strip("._")
    if not cleaned:
        raise SecurityError(f"export file name is empty after sanitization: {file_name}")

    suffix = f".{fmt}"
    if not cleaned.lower().endswith(suffix):
        cleaned += suffix
    return cleaned


def unique_export_path(exports_dir: Path, file_name: str) -> Path:
    """Never overwrite an existing export; pick the next free name instead."""
    target = _resolve_inside(exports_dir, exports_dir / file_name)
    if not target.exists():
        return target

    stem, dot, suffix = file_name.partition(".")
    counter = 2
    while True:
        candidate = exports_dir / f"{stem}_{counter}{dot}{suffix}"
        if not candidate.exists():
            return _resolve_inside(exports_dir, candidate)
        counter += 1


# --------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------

def quote_identifier(name: str) -> str:
    """Quote a table or column name for interpolation into SQL."""
    if "\x00" in name:
        raise SecurityError("identifier contains a null byte")
    return '"' + name.replace('"', '""') + '"'


def safe_table_name(raw: str) -> str:
    """Derive a plain SQLite table name from a filename stem."""
    name = _IDENTIFIER_SAFE.sub("_", raw).strip("_").lower()
    if not name:
        name = "table"
    if name[0].isdigit():
        name = f"t_{name}"
    if name.startswith("sqlite_"):
        name = f"t_{name}"
    return name


# --------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------

def scrub_sql(sql: str) -> str:
    """Return ``sql`` with comments and literals blanked out.

    Keyword inspection then cannot be fooled by a DROP hidden inside a string
    literal or a comment.
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "-" and sql.startswith("--", i):
            i = sql.find("\n", i)
            if i == -1:
                break
            out.append(" ")
        elif ch == "/" and sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
            out.append(" ")
        elif ch in "'\"`[":
            closing = {"'": "'", '"': '"', "`": "`", "[": "]"}[ch]
            j = i + 1
            while j < n:
                if sql[j] == closing:
                    if closing != "]" and j + 1 < n and sql[j + 1] == closing:
                        j += 2  # doubled quote inside the literal
                        continue
                    break
                j += 1
            # Identifiers keep a placeholder token, string literals blank out.
            out.append("_" if ch != "'" else " ")
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def validate_read_only_sql(sql: str) -> str:
    """Validate an AI-generated statement, returning it stripped.

    Raises :class:`SecurityError` for anything that is not a single read-only
    query.
    """
    if not sql or not sql.strip():
        raise SecurityError("SQL statement is empty")

    stripped = sql.strip()
    scrubbed = scrub_sql(stripped)

    statements = [part for part in scrubbed.split(";") if part.strip()]
    if len(statements) > 1:
        raise SecurityError("only a single SQL statement may be executed")

    matches = list(re.finditer(r"[A-Za-z_][A-Za-z_0-9]*", scrubbed))
    if not matches:
        raise SecurityError("no SQL statement found")
    tokens = [m.group(0) for m in matches]

    leading = tokens[0].upper()
    if leading not in _ALLOWED_LEADING_KEYWORDS:
        raise SecurityError(
            f"only read-only statements are allowed; found '{leading}'"
        )

    for match in matches:
        token = match.group(0)
        upper = token.upper()
        if upper in _FORBIDDEN_KEYWORDS:
            raise SecurityError(f"statement contains forbidden keyword '{upper}'")
        if upper in _CONTEXTUAL_KEYWORDS and not scrubbed[match.end():].lstrip().startswith("("):
            raise SecurityError(f"statement contains forbidden keyword '{upper}'")
        if token.lower() in _FORBIDDEN_FUNCTIONS:
            raise SecurityError(f"statement calls forbidden function '{token}'")

    return stripped


# The only SQLite actions an analytical query legitimately needs: reading rows
# and columns, running a SELECT (including each subquery), evaluating scalar and
# aggregate functions, and stepping a recursive CTE.
_ALLOWED_ACTIONS = frozenset({
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,   # narrowed further by _FORBIDDEN_FUNCTIONS below
    sqlite3.SQLITE_RECURSIVE,
})

# Deny-by-default already covers these; naming them keeps the policy readable
# and gives a precise reason in the error and the log.
_NAMED_DENIALS: dict[int, str] = {
    sqlite3.SQLITE_ATTACH: "ATTACH",
    sqlite3.SQLITE_DETACH: "DETACH",
    sqlite3.SQLITE_PRAGMA: "PRAGMA",
    sqlite3.SQLITE_INSERT: "INSERT",
    sqlite3.SQLITE_UPDATE: "UPDATE",
    sqlite3.SQLITE_DELETE: "DELETE",
    sqlite3.SQLITE_CREATE_TABLE: "CREATE TABLE",
    sqlite3.SQLITE_CREATE_TEMP_TABLE: "CREATE TEMP TABLE",
    sqlite3.SQLITE_CREATE_INDEX: "CREATE INDEX",
    sqlite3.SQLITE_CREATE_TEMP_INDEX: "CREATE TEMP INDEX",
    sqlite3.SQLITE_CREATE_VIEW: "CREATE VIEW",
    sqlite3.SQLITE_CREATE_TEMP_VIEW: "CREATE TEMP VIEW",
    sqlite3.SQLITE_CREATE_TRIGGER: "CREATE TRIGGER",
    sqlite3.SQLITE_CREATE_TEMP_TRIGGER: "CREATE TEMP TRIGGER",
    sqlite3.SQLITE_CREATE_VTABLE: "CREATE VIRTUAL TABLE",
    sqlite3.SQLITE_DROP_TABLE: "DROP TABLE",
    sqlite3.SQLITE_DROP_TEMP_TABLE: "DROP TEMP TABLE",
    sqlite3.SQLITE_DROP_INDEX: "DROP INDEX",
    sqlite3.SQLITE_DROP_VIEW: "DROP VIEW",
    sqlite3.SQLITE_DROP_TRIGGER: "DROP TRIGGER",
    sqlite3.SQLITE_DROP_VTABLE: "DROP VIRTUAL TABLE",
    sqlite3.SQLITE_ALTER_TABLE: "ALTER TABLE",
    sqlite3.SQLITE_REINDEX: "REINDEX",
    sqlite3.SQLITE_ANALYZE: "ANALYZE",
    sqlite3.SQLITE_TRANSACTION: "transaction control",
    sqlite3.SQLITE_SAVEPOINT: "SAVEPOINT",
}


def describe_action(action: int) -> str:
    """Human-readable name for an authorizer action code."""
    return _NAMED_DENIALS.get(action, f"action {action}")


def _authorizer(action: int, arg1: str | None, arg2: str | None,
                db_name: str | None, trigger: str | None) -> int:
    """Deny every SQLite action except the ones analytical reads need.

    This is the primary enforcement layer. It runs inside SQLite while the
    statement is being prepared, so it applies to the actual semantics of the
    query rather than to how the text happens to be spelled — which is why it,
    not the scrubber, is what stops ATTACH, mutating pragmas, schema changes
    and extension loading.
    """
    if action == sqlite3.SQLITE_FUNCTION:
        # arg2 is the function name. Everything registered on the connection is
        # pure; the denylist covers filesystem/process reaching builtins.
        if arg2 and arg2.lower() in _FORBIDDEN_FUNCTIONS:
            logger.warning("denied SQLite function call: %s", arg2)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    if action in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK

    logger.warning("denied SQLite operation: %s", describe_action(action))
    return sqlite3.SQLITE_DENY


def install_read_only_authorizer(conn: sqlite3.Connection) -> None:
    """Install the deny-by-default authorizer on ``conn``."""
    conn.set_authorizer(_authorizer)


def disable_extension_loading(conn: sqlite3.Connection) -> None:
    """Turn off SQLite extension loading explicitly on this connection.

    Python disables it by default and the authorizer denies the
    ``load_extension()`` SQL function, but a loaded extension could register
    arbitrary native code, so it is worth switching off in its own right rather
    than relying on a default. Both switches are optional depending on how the
    interpreter's SQLite was built, hence the tolerant handling.
    """
    try:
        conn.enable_load_extension(False)
    except (AttributeError, sqlite3.NotSupportedError):
        pass  # not compiled in: loading is unavailable anyway

    setconfig = getattr(conn, "setconfig", None)  # Python 3.12+
    if setconfig is not None:
        try:
            setconfig(sqlite3.SQLITE_DBCONFIG_ENABLE_LOAD_EXTENSION, False)
        except (AttributeError, sqlite3.OperationalError, sqlite3.NotSupportedError):
            pass
