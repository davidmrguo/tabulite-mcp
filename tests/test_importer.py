"""CSV ingestion: streaming, NULL handling, TEXT storage and content identity."""

from __future__ import annotations

import hashlib
from dataclasses import replace

from tabulite_mcp import catalog, importer
from tabulite_mcp.database import row_count, table_columns
from tests.conftest import SALES_HEADER, SALES_ROWS, write_csv


def test_import_creates_a_text_only_table_with_the_right_row_count(config, conns, sales_csv):
    db, cat = conns
    result = importer.import_csv(sales_csv, config, db, cat)

    assert result.table_name == "sales"
    assert result.row_count == len(SALES_ROWS)
    assert row_count(db, "sales") == len(SALES_ROWS)
    assert [name for name, _ in table_columns(db, "sales")] == SALES_HEADER
    assert {declared for _, declared in table_columns(db, "sales")} == {"TEXT"}


def test_missing_value_markers_become_sql_null(config, conns, sales_csv):
    db, cat = conns
    importer.import_csv(sales_csv, config, db, cat)

    nulls = db.execute(
        "SELECT transaction_id FROM sales WHERE revenue IS NULL ORDER BY transaction_id"
    ).fetchall()
    # Row 2 is "" and row 3 is "N/A"; both are configured missing markers.
    assert [row[0] for row in nulls] == ["2", "3"]
    assert db.execute("SELECT channel FROM sales WHERE transaction_id='6'").fetchone()[0] is None


def test_malformed_values_are_preserved_as_text(config, conns, sales_csv):
    db, cat = conns
    importer.import_csv(sales_csv, config, db, cat)

    # "unknown" and "-" are not missing-value markers: they must survive intact
    # so the distinction between missing and invalid stays visible.
    assert db.execute("SELECT revenue FROM sales WHERE transaction_id='4'").fetchone()[0] == "unknown"
    assert db.execute("SELECT revenue FROM sales WHERE transaction_id='8'").fetchone()[0] == "-"
    assert db.execute("SELECT quantity FROM sales WHERE transaction_id='6'").fetchone()[0] == "-"
    assert db.execute("SELECT typeof(revenue) FROM sales WHERE transaction_id='1'").fetchone()[0] == "text"


def test_configurable_null_markers(config, conns, tmp_path):
    db, cat = conns
    path = write_csv(config.source_dir / "custom.csv", ["a"], [["-"], ["x"], [""]])
    custom = replace(config, null_markers=("", "-"))
    importer.import_csv(path, custom, db, cat)
    values = [row[0] for row in db.execute("SELECT a FROM custom").fetchall()]
    assert values == [None, "x", None]


def test_sha256_matches_the_file_contents(config, conns, sales_csv):
    db, cat = conns
    result = importer.import_csv(sales_csv, config, db, cat)
    assert result.source_id == hashlib.sha256(sales_csv.read_bytes()).hexdigest()
    assert result.bytes_read == sales_csv.stat().st_size

    stored = catalog.get_source(cat, result.source_id)
    assert stored["sha256"] == result.source_id
    assert stored["filename"] == "sales.csv"


def test_renamed_file_with_identical_content_reuses_the_existing_table(config, conns, sales_csv):
    db, cat = conns
    first = importer.import_csv(sales_csv, config, db, cat)

    renamed = write_csv(config.source_dir / "sales_FINAL_v2.csv", SALES_HEADER, SALES_ROWS)
    assert renamed.read_bytes() == sales_csv.read_bytes()

    second = importer.import_csv(renamed, config, db, cat)
    assert second.reused_existing is True
    assert second.source_id == first.source_id
    assert second.table_name == first.table_name
    assert len(catalog.list_imports(cat)) == 1
    # The staging table is gone: no duplicated data.
    assert db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE '_tmp_import_%'"
    ).fetchone()[0] == 0


def test_changed_content_gets_a_different_identity_and_its_own_table(config, conns, sales_csv):
    db, cat = conns
    first = importer.import_csv(sales_csv, config, db, cat)

    changed = write_csv(
        config.source_dir / "sales_updated.csv", SALES_HEADER,
        SALES_ROWS + [["9", "2025-05-01", "CUST-5", "Mug", "email", "1", "10.00"]],
    )
    second = importer.import_csv(changed, config, db, cat)

    assert second.source_id != first.source_id
    assert second.reused_existing is False
    assert second.table_name == "sales_updated"
    assert second.row_count == len(SALES_ROWS) + 1
    assert len(catalog.list_imports(cat)) == 2


def test_force_reimports_identical_content(config, conns, sales_csv):
    db, cat = conns
    first = importer.import_csv(sales_csv, config, db, cat)
    again = importer.import_csv(sales_csv, config, db, cat, force=True)
    assert again.reused_existing is False
    assert again.table_name == first.table_name
    assert row_count(db, first.table_name) == len(SALES_ROWS)


def test_import_streams_the_file_in_bounded_chunks(config, conns, monkeypatch):
    """The importer must never pull the whole file into memory."""
    db, cat = conns
    big = write_csv(
        config.source_dir / "big.csv",
        ["id", "value"],
        [[str(i), f"value-{i}-{'x' * 40}"] for i in range(5_000)],
    )
    assert big.stat().st_size > 200_000

    reads: list[int] = []
    original = importer.HashingReader.readinto

    def spy(self, buffer):
        count = original(self, buffer)
        reads.append(count)
        return count

    monkeypatch.setattr(importer.HashingReader, "readinto", spy)

    small_chunks = replace(config, read_chunk_bytes=8192, insert_batch_size=500)
    result = importer.import_csv(big, small_chunks, db, cat)

    assert result.row_count == 5_000
    # Many bounded reads rather than one slurp of the whole file...
    assert len(reads) > 10
    assert max(reads) <= 8192
    # ...and many bounded executemany() batches rather than one giant insert.
    assert result.batches == 10


def test_colliding_stems_get_a_stable_content_hash_suffix(config, conns):
    """Two different sales.csv files share one database namespace."""
    db, cat = conns
    first_path = write_csv(config.source_dir / "sales.csv", SALES_HEADER, SALES_ROWS)
    nested = config.source_dir / "archive"
    nested.mkdir()
    second_path = write_csv(nested / "sales.csv", SALES_HEADER, SALES_ROWS[:4])

    first = importer.import_csv(first_path, config, db, cat)
    second = importer.import_csv(second_path, config, db, cat)

    assert first.table_name == "sales"
    # Suffix comes from the file's own content hash, not an import counter.
    assert second.table_name == f"sales_{second.source_id[:6]}"
    assert second.row_count == 4
    assert row_count(db, second.table_name) == 4

    # The catalog is the authoritative mapping from hash to table.
    assert catalog.find_import_by_source(cat, second.source_id)["table_name"] == second.table_name
    assert catalog.get_import(cat, second.table_name)["source_id"] == second.source_id


def test_table_name_does_not_depend_on_import_order(config, conns):
    """Re-importing after removal yields the same name, unlike a counter."""
    db, cat = conns
    first_path = write_csv(config.source_dir / "sales.csv", SALES_HEADER, SALES_ROWS)
    nested = config.source_dir / "archive"
    nested.mkdir()
    second_path = write_csv(nested / "sales.csv", SALES_HEADER, SALES_ROWS[:4])

    importer.import_csv(first_path, config, db, cat)
    original = importer.import_csv(second_path, config, db, cat)

    # Forget it entirely, then import it again.
    db.execute(f'DROP TABLE "{original.table_name}"')
    catalog.delete_import(cat, original.table_name)
    again = importer.import_csv(second_path, config, db, cat)

    assert again.reused_existing is False
    assert again.table_name == original.table_name


def test_deterministic_table_name_falls_back_to_longer_suffixes():
    source_id = "4b11d3" + "a" * 58
    taken = {"sales", f"sales_{source_id[:6]}"}
    assert importer.deterministic_table_name("sales", source_id, lambda n: n in taken) == (
        f"sales_{source_id[:12]}"
    )
    assert importer.deterministic_table_name("orders", source_id, lambda n: n in taken) == "orders"


def test_explicit_table_name_is_honored_and_sanitized(config, conns, sales_csv):
    db, cat = conns
    result = importer.import_csv(sales_csv, config, db, cat, table_name="Q1 Sales!")
    assert result.table_name == "q1_sales"
    assert row_count(db, "q1_sales") == len(SALES_ROWS)


def test_header_names_are_normalized_and_deduplicated(config, conns):
    db, cat = conns
    path = write_csv(config.source_dir / "messy.csv",
                     ["Order ID", "Order Date", "Order ID", ""],
                     [["1", "2025-01-01", "2", "x"]])
    result = importer.import_csv(path, config, db, cat)
    stored = [c["column_name"] for c in result.columns]
    assert stored == ["order_id", "order_date", "order_id_2", "column_4"]
    assert result.columns[0]["source_column"] == "Order ID"


def test_short_and_long_rows_are_padded_and_reported(config, conns):
    db, cat = conns
    path = config.source_dir / "ragged.csv"
    path.write_text("a,b,c\n1,2,3\n4,5\n6,7,8,9\n", encoding="utf-8")
    result = importer.import_csv(path, config, db, cat)
    assert result.row_count == 3
    assert result.malformed_rows == 2
    assert result.warnings
    assert db.execute("SELECT c FROM ragged WHERE a='4'").fetchone()[0] is None


def test_semicolon_delimiter_is_detected(config, conns):
    db, cat = conns
    path = config.source_dir / "euro.csv"
    path.write_text("a;b\n1;2\n3;4\n", encoding="utf-8")
    result = importer.import_csv(path, config, db, cat)
    assert [c["column_name"] for c in result.columns] == ["a", "b"]
    assert result.row_count == 2


def test_inspect_source_reads_only_a_prefix(config, conns, sales_csv):
    db, cat = conns
    info = importer.inspect_csv(sales_csv, config, cat)
    assert info["filename"] == "sales.csv"
    assert info["delimiter"] == ","
    assert [c["column_name"] for c in info["columns"]] == SALES_HEADER
    assert len(info["sample_rows"]) == 5
    assert info["already_imported_hint"] is None

    importer.import_csv(sales_csv, config, db, cat)
    assert importer.inspect_csv(sales_csv, config, cat)["already_imported_hint"]["table_name"] == "sales"
