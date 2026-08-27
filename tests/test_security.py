"""Path containment and read-only SQL enforcement."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tabulite_mcp import database
from tabulite_mcp.config import Config
from tabulite_mcp.security import (
    SecurityError,
    quote_identifier,
    resolve_source_path,
    safe_table_name,
    sanitize_export_filename,
    unique_export_path,
    validate_read_only_sql,
)


def test_resolves_a_file_inside_the_source_directory(config: Config, sales_csv: Path):
    assert resolve_source_path("sales.csv", config.source_dir) == sales_csv.resolve()


def test_resolves_a_nested_relative_path(config: Config):
    nested = config.source_dir / "2025" / "q1.csv"
    nested.parent.mkdir()
    nested.write_text("a,b\n1,2\n")
    assert resolve_source_path("2025/q1.csv", config.source_dir) == nested.resolve()


@pytest.mark.parametrize(
    "bad_path",
    ["../secrets.csv", "../../etc/passwd", "subdir/../../outside.csv", "/etc/passwd"],
)
def test_rejects_path_traversal(config: Config, bad_path: str):
    with pytest.raises(SecurityError):
        resolve_source_path(bad_path, config.source_dir)


def test_rejects_symlink_escaping_the_source_directory(config: Config, tmp_path: Path):
    outside = tmp_path / "outside.csv"
    outside.write_text("a\n1\n")
    link = config.source_dir / "sneaky.csv"
    link.symlink_to(outside)
    with pytest.raises(SecurityError):
        resolve_source_path("sneaky.csv", config.source_dir)


def test_rejects_missing_source_file(config: Config):
    with pytest.raises(SecurityError):
        resolve_source_path("nope.csv", config.source_dir)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM sales",
        "  select 1  ",
        "WITH t AS (SELECT 1 AS x) SELECT * FROM t",
        "SELECT CASE WHEN 1 THEN replace('a,b', ',', '') END",
        "SELECT name FROM sqlite_master",
    ],
)
def test_accepts_read_only_statements(sql: str):
    assert validate_read_only_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO sales VALUES (1)",
        "UPDATE sales SET revenue = '0'",
        "DELETE FROM sales",
        "DROP TABLE sales",
        "ALTER TABLE sales RENAME TO x",
        "CREATE TABLE t (a TEXT)",
        "REPLACE INTO sales VALUES (1)",
        "ATTACH DATABASE '/etc/passwd' AS pw",
        "DETACH DATABASE main",
        "VACUUM",
        "PRAGMA journal_mode = DELETE",
        "SELECT 1; DROP TABLE sales",
        "SELECT load_extension('evil.so')",
        "",
        "   ",
    ],
)
def test_rejects_mutating_or_unsafe_statements(sql: str):
    with pytest.raises(SecurityError):
        validate_read_only_sql(sql)


def test_hidden_keywords_in_literals_do_not_trip_validation():
    # A DROP inside a string literal is data, not a statement.
    assert validate_read_only_sql("SELECT 'DROP TABLE sales' AS note")


def test_read_only_connection_refuses_writes_even_if_validation_is_bypassed(
    config: Config, imported
):
    conn = database.connect_read_only(config.database_path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("INSERT INTO sales (customer) VALUES ('x')")
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute("ATTACH DATABASE '/tmp/evil.sqlite' AS evil")
    finally:
        conn.close()


def test_sanitizes_export_filenames():
    assert sanitize_export_filename("report", "csv") == "report.csv"
    assert sanitize_export_filename("my report 2025.csv", "csv") == "my_report_2025.csv"
    assert sanitize_export_filename("weird*name?.csv", "csv") == "weird_name_.csv"


@pytest.mark.parametrize(
    "bad_name",
    ["../escape.csv", "/etc/passwd", "sub/dir.csv", "..\\windows.csv", "..."],
)
def test_rejects_export_filename_traversal(bad_name: str):
    with pytest.raises(SecurityError):
        sanitize_export_filename(bad_name, "csv")


def test_unique_export_path_never_overwrites(config: Config):
    first = unique_export_path(config.exports_dir, "result.csv")
    first.write_text("x")
    second = unique_export_path(config.exports_dir, "result.csv")
    assert second.name == "result_2.csv"
    second.write_text("y")
    assert unique_export_path(config.exports_dir, "result.csv").name == "result_3.csv"


def test_identifier_helpers():
    assert quote_identifier('we"ird') == '"we""ird"'
    assert safe_table_name("2025 Sales-FINAL.v2") == "t_2025_sales_final_v2"
    assert safe_table_name("sqlite_master") == "t_sqlite_master"
