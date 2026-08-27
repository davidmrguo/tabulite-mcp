"""Exporting complete query results to disk."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from tabulite_mcp import database, exporter, importer
from tabulite_mcp.security import SecurityError
from tests.conftest import write_csv


@pytest.fixture
def big_table(config, conns):
    db, cat = conns
    path = write_csv(
        config.source_dir / "big.csv",
        ["n", "label"],
        [[str(i), f"row-{i}"] for i in range(1_500)],
    )
    importer.import_csv(path, config, db, cat)
    return path


def _export(config, conn, cat, sql, **kwargs):
    return exporter.export_query(
        conn, cat, sql, config, known_tables=database.list_table_names(conn), **kwargs
    )


def test_exports_a_csv_file_with_metadata(config, conns, read_only):
    _, cat = conns
    result = _export(
        config, read_only, cat,
        "SELECT transaction_id, TRY_REAL(revenue) AS revenue FROM sales "
        "WHERE TRY_REAL(revenue) > 100",
        file_name="high_value",
    )

    assert result["file_name"] == "high_value.csv"
    assert result["relative_path"] == "exports/high_value.csv"
    assert result["format"] == "csv"
    assert result["row_count"] == 2
    assert result["columns"] == ["transaction_id", "revenue"]
    assert result["source_tables"] == ["sales"]
    assert result["file_size_bytes"] > 0

    written = Path(result["absolute_path"])
    assert written.parent == config.exports_dir
    rows = list(csv.reader(written.open(newline="", encoding="utf-8")))
    assert rows[0] == ["transaction_id", "revenue"]
    assert {row[0] for row in rows[1:]} == {"1", "7"}


def test_exports_json(config, conns, read_only):
    _, cat = conns
    result = _export(config, read_only, cat, "SELECT transaction_id FROM sales LIMIT 3",
                     file_name="sample", fmt="json")
    payload = json.loads(Path(result["absolute_path"]).read_text())
    assert payload == [{"transaction_id": "1"}, {"transaction_id": "2"}, {"transaction_id": "3"}]
    assert result["row_count"] == 3


def test_export_is_not_bound_by_the_interactive_row_limit(config, conns, big_table):
    _, cat = conns
    conn = database.connect_read_only(config.database_path)
    try:
        interactive = database.execute_query(conn, "SELECT n FROM big", config.max_query_rows, 30)
        assert interactive["returned_rows"] == config.max_query_rows
        assert interactive["truncated"] is True

        result = _export(config, conn, cat, "SELECT n, label FROM big", file_name="full")
    finally:
        conn.close()

    assert result["row_count"] == 1_500
    lines = Path(result["absolute_path"]).read_text().splitlines()
    assert len(lines) == 1_501  # header + rows


def test_export_streams_rather_than_materializing_the_result(config, conns, big_table, monkeypatch):
    """Rows must reach the file in batches while the cursor is still open."""
    _, cat = conns
    observed: list[tuple[int, int]] = []
    real_iter_batches = exporter.iter_batches

    def spy(cursor, size):
        for batch in real_iter_batches(cursor, size):
            bytes_on_disk = sum(p.stat().st_size for p in config.exports_dir.glob("*.csv"))
            observed.append((len(batch), bytes_on_disk))
            yield batch

    monkeypatch.setattr(exporter, "iter_batches", spy)
    monkeypatch.setattr(exporter, "EXPORT_BATCH", 100)

    conn = database.connect_read_only(config.database_path)
    try:
        result = _export(config, conn, cat, "SELECT n, label FROM big", file_name="streamed")
    finally:
        conn.close()

    assert result["row_count"] == 1_500
    # Fetched in many bounded batches, never one 1500-row list...
    assert len(observed) == 15
    assert max(size for size, _ in observed) == 100
    # ...and data was already on disk before the last batch was read.
    assert observed[-1][1] > 0


def test_generated_file_names_are_unique(config, conns, read_only):
    _, cat = conns
    first = _export(config, read_only, cat, "SELECT 1 AS a")
    second = _export(config, read_only, cat, "SELECT 1 AS a")
    assert first["file_name"] != second["file_name"]
    assert first["export_id"] != second["export_id"]


def test_duplicate_file_names_never_overwrite(config, conns, read_only):
    _, cat = conns
    first = _export(config, read_only, cat, "SELECT 1 AS a", file_name="result.csv")
    second = _export(config, read_only, cat, "SELECT 2 AS a", file_name="result.csv")

    assert first["file_name"] == "result.csv"
    assert second["file_name"] == "result_2.csv"
    assert Path(first["absolute_path"]).read_text().splitlines()[1] == "1"
    assert Path(second["absolute_path"]).read_text().splitlines()[1] == "2"


def test_file_names_are_sanitized(config, conns, read_only):
    _, cat = conns
    result = _export(config, read_only, cat, "SELECT 1 AS a", file_name="2025 Q1 report!")
    assert result["file_name"] == "2025_Q1_report.csv"
    assert Path(result["absolute_path"]).parent == config.exports_dir


@pytest.mark.parametrize("bad_name", ["../escape.csv", "/tmp/escape.csv", "sub/dir.csv"])
def test_export_path_traversal_is_rejected(config, conns, read_only, bad_name):
    _, cat = conns
    with pytest.raises(SecurityError):
        _export(config, read_only, cat, "SELECT 1 AS a", file_name=bad_name)
    assert list(config.exports_dir.iterdir()) == []


def test_unsupported_format_is_rejected(config, conns, read_only):
    _, cat = conns
    with pytest.raises(SecurityError):
        _export(config, read_only, cat, "SELECT 1 AS a", fmt="parquet")


def test_exports_are_recorded_in_the_catalog(config, conns, read_only):
    _, cat = conns
    result = _export(config, read_only, cat, "SELECT channel FROM sales", file_name="channels")
    row = cat.execute("SELECT * FROM exports WHERE export_id = ?", (result["export_id"],)).fetchone()
    assert row["file_name"] == "channels.csv"
    assert row["row_count"] == 8
    assert json.loads(row["source_tables"]) == ["sales"]
    assert row["query_hash"] == exporter.query_hash("SELECT channel FROM sales")


def test_failed_export_leaves_no_partial_file(config, conns, read_only):
    _, cat = conns
    with pytest.raises(Exception):
        _export(config, read_only, cat, "SELECT * FROM does_not_exist", file_name="broken")
    assert not (config.exports_dir / "broken.csv").exists()
