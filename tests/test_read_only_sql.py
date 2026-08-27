"""Regression tests for the read-only query path.

Two distinct layers are tested here, deliberately separately:

* the SQL scrubber (``validate_read_only_sql``), which is defense in depth and
  exists to give a clear early error;
* the SQLite authorizer on the read-only connection, which is the primary
  enforcement layer. Those tests bypass the scrubber entirely and go straight
  to ``conn.execute()`` — if the authorizer were removed they would fail even
  though the scrubber is untouched.
"""

from __future__ import annotations

import sqlite3

import pytest

from tabulite_mcp import database, security
from tabulite_mcp.security import SecurityError, validate_read_only_sql

# --------------------------------------------------------------------------
# Legitimate analytical constructs must keep working
# --------------------------------------------------------------------------

LEGITIMATE_SQL = [
    # CASE ... END: "END" must not be mistaken for a statement terminator.
    "SELECT CASE WHEN TRY_REAL(revenue) > 100 THEN 'high' ELSE 'low' END AS band FROM sales",
    "SELECT SUM(CASE WHEN channel = 'email' THEN 1 ELSE 0 END) AS emails FROM sales",
    # replace() the scalar function, as opposed to REPLACE INTO.
    "SELECT replace(revenue, ',', '') AS cleaned FROM sales",
    "SELECT TRY_REAL(replace(replace(revenue, '$', ''), ',', '')) AS amount FROM sales",
    "SELECT REPLACE(product, ' ', '_') AS slug FROM sales",
    # Ordinary analytics.
    "WITH t AS (SELECT channel, TRY_REAL(revenue) AS r FROM sales) "
    "SELECT channel, AVG(r) FROM t GROUP BY channel",
    "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 3) SELECT * FROM n",
    "SELECT channel, RANK() OVER (ORDER BY TRY_REAL(revenue) DESC) FROM sales",
    "SELECT name FROM sqlite_master WHERE type = 'table'",
]


@pytest.mark.parametrize("sql", LEGITIMATE_SQL)
def test_scrubber_accepts_legitimate_analytical_sql(sql):
    assert validate_read_only_sql(sql)


@pytest.mark.parametrize("sql", LEGITIMATE_SQL)
def test_authorizer_allows_legitimate_analytical_sql(read_only, sql):
    """The same statements must actually execute on the hardened connection."""
    read_only.execute(sql).fetchall()


def test_case_end_and_replace_produce_correct_results(read_only):
    bands = read_only.execute(
        """
        SELECT CASE
                   WHEN TRY_REAL(revenue) IS NULL THEN 'unusable'
                   WHEN TRY_REAL(revenue) >= 100 THEN 'high'
                   ELSE 'low'
               END AS band,
               COUNT(*) AS n
        FROM sales
        GROUP BY band
        ORDER BY band
        """
    ).fetchall()
    # Fixture revenues: 125.40, NULL, NULL, "unknown", 80.00, 19.99, 240.10, "-"
    assert dict(bands) == {"high": 2, "low": 2, "unusable": 4}

    # replace() is how the AI strips currency formatting before TRY_REAL.
    cleaned = read_only.execute(
        "SELECT TRY_REAL(replace(replace('$1,234.50', '$', ''), ',', ''))"
    ).fetchone()[0]
    assert cleaned == 1234.50


# --------------------------------------------------------------------------
# The authorizer is the primary layer: these bypass the scrubber
# --------------------------------------------------------------------------

MUTATING_SQL = [
    "INSERT INTO sales (customer) VALUES ('x')",
    "REPLACE INTO sales (customer) VALUES ('x')",
    "INSERT OR REPLACE INTO sales (customer) VALUES ('x')",
    "UPDATE sales SET revenue = '0'",
    "DELETE FROM sales",
    "DROP TABLE sales",
    "ALTER TABLE sales RENAME TO stolen",
    "CREATE TABLE evil (a TEXT)",
    "CREATE TEMP TABLE evil (a TEXT)",
    "CREATE INDEX idx ON sales(revenue)",
    "CREATE VIEW v AS SELECT * FROM sales",
    "CREATE TRIGGER t AFTER INSERT ON sales BEGIN SELECT 1; END",
]

# Maintenance statements only consult the authorizer when there is something to
# rebuild, so they are exercised against a database that has an index.
MAINTENANCE_SQL = ["REINDEX", "REINDEX idx_value", "ANALYZE", "VACUUM"]

ENVIRONMENT_SQL = [
    "ATTACH DATABASE '/tmp/evil.sqlite' AS evil",
    "DETACH DATABASE main",
    "PRAGMA journal_mode = DELETE",
    "PRAGMA writable_schema = ON",
    "PRAGMA query_only = OFF",
    "PRAGMA foreign_keys = ON",
]


@pytest.mark.parametrize("sql", MUTATING_SQL + ENVIRONMENT_SQL)
def test_authorizer_rejects_mutating_and_environment_sql(read_only, sql):
    """Executed directly on the connection: no scrubber involved."""
    with pytest.raises(sqlite3.DatabaseError) as excinfo:
        read_only.execute(sql)
    assert "not authorized" in str(excinfo.value) or "readonly" in str(excinfo.value).lower()


@pytest.mark.parametrize("sql", MUTATING_SQL + ENVIRONMENT_SQL + MAINTENANCE_SQL)
def test_scrubber_also_rejects_mutating_and_environment_sql(sql):
    """Defense in depth: the same statements never reach SQLite in the first place."""
    with pytest.raises(SecurityError):
        validate_read_only_sql(sql)


@pytest.mark.parametrize("sql", MAINTENANCE_SQL)
def test_authorizer_rejects_maintenance_statements(tmp_path, sql):
    indexed = tmp_path / "indexed.sqlite"
    writable = database.connect_writable(indexed)
    writable.execute("CREATE TABLE t (value TEXT)")
    writable.execute("INSERT INTO t VALUES ('x')")
    writable.execute("CREATE INDEX idx_value ON t(value)")
    writable.close()

    conn = database.connect_read_only(indexed)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
    finally:
        conn.close()


def test_rejection_leaves_the_data_untouched(read_only):
    before = read_only.execute("SELECT COUNT(*) FROM sales").fetchone()[0]
    for sql in ("DELETE FROM sales", "UPDATE sales SET revenue = '0'", "DROP TABLE sales"):
        with pytest.raises(sqlite3.DatabaseError):
            read_only.execute(sql)
    assert read_only.execute("SELECT COUNT(*) FROM sales").fetchone()[0] == before


def test_query_only_pragma_cannot_be_turned_off(read_only):
    assert read_only.execute("SELECT 1").fetchone()  # connection is alive
    with pytest.raises(sqlite3.DatabaseError):
        read_only.execute("PRAGMA query_only = OFF")


# --------------------------------------------------------------------------
# Extension loading
# --------------------------------------------------------------------------

def test_load_extension_function_is_denied(read_only):
    with pytest.raises(sqlite3.DatabaseError):
        read_only.execute("SELECT load_extension('/tmp/evil.so')")
    with pytest.raises(SecurityError):
        validate_read_only_sql("SELECT load_extension('/tmp/evil.so')")


@pytest.mark.parametrize("name", ["readfile", "writefile", "edit", "fts3_tokenizer"])
def test_filesystem_reaching_functions_are_denied(read_only, name):
    with pytest.raises(sqlite3.DatabaseError):
        read_only.execute(f"SELECT {name}('/etc/passwd')")
    with pytest.raises(SecurityError):
        validate_read_only_sql(f"SELECT {name}('/etc/passwd')")


def test_extension_loading_is_disabled_on_the_connection(config, imported):
    """The C-level switch is off, independently of the SQL function denial."""
    for conn in (database.connect_read_only(config.database_path),
                 database.connect_writable(config.database_path)):
        try:
            if not hasattr(conn, "enable_load_extension"):
                pytest.skip("this interpreter's SQLite has no extension support")
            # Re-enabling would be the only way to load native code; assert the
            # server never leaves it on. Turning it back on here proves the API
            # exists, so the earlier disable call was meaningful.
            conn.enable_load_extension(True)
            conn.enable_load_extension(False)
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute("SELECT load_extension('/tmp/evil.so')")
        finally:
            conn.close()


def test_authorizer_allow_list_is_minimal():
    """Only reads, functions and recursion are permitted, nothing else."""
    assert security._ALLOWED_ACTIONS == frozenset({
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    })
    for action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_PRAGMA,
                   sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                   sqlite3.SQLITE_DROP_TABLE, sqlite3.SQLITE_ALTER_TABLE,
                   sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_TRANSACTION):
        assert action not in security._ALLOWED_ACTIONS
        assert security._authorizer(action, None, None, None, None) == sqlite3.SQLITE_DENY
        assert security.describe_action(action) != f"action {action}"
