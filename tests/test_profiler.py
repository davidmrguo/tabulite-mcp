"""Logical type inference and column statistics."""

from __future__ import annotations

from tabulite_mcp import catalog, importer, profiler
from tests.conftest import write_csv


def _profile(config, conns, header, rows, name="t"):
    db, cat = conns
    path = write_csv(config.source_dir / f"{name}.csv", header, rows)
    result = importer.import_csv(path, config, db, cat)
    profiles = profiler.profile_table(db, result.table_name, result.source_id, config)
    return {p["column_name"]: p for p in profiles}, result


def test_infers_integer_real_text_and_date(config, conns):
    profiles, _ = _profile(
        config, conns,
        ["quantity", "revenue", "channel", "order_date", "flag"],
        [
            ["1", "10.5", "email", "2025-01-01", "true"],
            ["2", "20.25", "retail", "2025-01-02", "false"],
            ["3", "30", "email", "2025-01-03", "yes"],
            ["4", "40.75", "organic", "2025-01-04", "no"],
        ],
    )
    assert profiles["quantity"]["logical_type"] == "INTEGER"
    assert profiles["quantity"]["recommended_cast"] == "TRY_INTEGER"
    assert profiles["revenue"]["logical_type"] == "REAL"
    assert profiles["revenue"]["recommended_cast"] == "TRY_REAL"
    assert profiles["channel"]["logical_type"] == "TEXT"
    assert profiles["channel"]["recommended_cast"] == "none"
    assert profiles["order_date"]["logical_type"] == "DATE"
    assert profiles["order_date"]["recommended_cast"] == "TRY_DATE"
    assert profiles["flag"]["logical_type"] == "BOOLEAN"
    assert profiles["flag"]["recommended_cast"] == "TRY_BOOLEAN"


def test_infers_datetime(config, conns):
    profiles, _ = _profile(
        config, conns, ["seen_at"],
        [["2025-01-01 10:00:00"], ["2025-01-02T11:30:00"], ["2025-01-03 12:15:00"]],
    )
    assert profiles["seen_at"]["logical_type"] == "DATETIME"
    assert profiles["seen_at"]["recommended_cast"] == "TRY_DATETIME"


def test_storage_type_is_always_text(config, conns):
    profiles, _ = _profile(config, conns, ["n"], [["1"], ["2"]])
    assert profiles["n"]["storage_type"] == "TEXT"


def test_counts_nulls_invalids_and_distinct_values(config, conns):
    rows = [[f"{i}.5"] for i in range(200)] + [["10.5"], [""], ["N/A"], ["unknown"]]
    profiles, _ = _profile(config, conns, ["revenue"], rows)

    revenue = profiles["revenue"]
    assert revenue["row_count"] == 204
    assert revenue["null_count"] == 2            # "" and "N/A" are missing
    assert revenue["non_null_count"] == 202
    assert revenue["valid_real_count"] == 201
    assert revenue["logical_type"] == "REAL"
    assert revenue["invalid_count"] == 1         # "unknown" is invalid, not missing
    assert revenue["invalid_examples"] == ["unknown"]
    assert revenue["distinct_count"] == 201      # 10.5 appears twice
    assert revenue["sample_values"] == ["0.5", "1.5", "2.5", "3.5", "4.5"]


def test_inference_is_conservative_about_mixed_columns(config, conns):
    """Too many unparseable values and the column stays TEXT."""
    rows = [["1"] for _ in range(90)] + [["not a number"] for _ in range(10)]
    profiles, _ = _profile(config, conns, ["mixed"], rows)
    assert profiles["mixed"]["logical_type"] == "TEXT"
    assert profiles["mixed"]["valid_integer_count"] == 90


def test_confidence_reflects_the_valid_ratio(config, conns):
    rows = [[str(i)] for i in range(999)] + [["oops"]]
    profiles, _ = _profile(config, conns, ["n"], rows)
    assert profiles["n"]["logical_type"] == "INTEGER"
    assert profiles["n"]["type_confidence"] == 0.999
    assert profiles["n"]["invalid_count"] == 1


def test_all_null_column_stays_text(config, conns):
    profiles, _ = _profile(config, conns, ["empty"], [[""], ["NA"], ["NULL"]])
    assert profiles["empty"]["logical_type"] == "TEXT"
    assert profiles["empty"]["non_null_count"] == 0
    assert profiles["empty"]["type_confidence"] == 1.0


def test_profiles_are_persisted_to_the_catalog(config, conns):
    db, cat = conns
    path = write_csv(
        config.source_dir / "billing.csv", ["invoice", "amount"],
        [[f"INV-{i}", f"{i}.25"] for i in range(200)] + [["INV-X", "pending"], ["INV-Y", "N/A"]],
    )
    result = importer.import_csv(path, config, db, cat)
    profiles = profiler.profile_table(db, result.table_name, result.source_id, config)
    catalog.replace_column_profiles(cat, result.table_name, profiles)

    stored = catalog.get_column_profiles(cat, "billing")
    assert [p["column_name"] for p in stored] == ["invoice", "amount"]

    amount = catalog.get_column_profile(cat, "billing", "amount")
    assert amount["logical_type"] == "REAL"
    assert amount["null_count"] == 1             # "N/A"
    assert amount["invalid_count"] == 1          # "pending"
    assert amount["invalid_examples"] == ["pending"]
    assert amount["source_id"] == result.source_id
    assert catalog.get_column_profile(cat, "billing", "invoice")["logical_type"] == "TEXT"

    # Re-profiling replaces rather than duplicates.
    catalog.replace_column_profiles(cat, "billing", profiles)
    assert len(catalog.get_column_profiles(cat, "billing")) == 2


def test_profiling_does_not_modify_the_stored_data(config, conns, sales_csv):
    db, cat = conns
    result = importer.import_csv(sales_csv, config, db, cat)
    before = db.execute("SELECT revenue FROM sales ORDER BY transaction_id").fetchall()
    profiler.profile_table(db, result.table_name, result.source_id, config)
    after = db.execute("SELECT revenue FROM sales ORDER BY transaction_id").fetchall()
    assert before == after
    assert db.execute("SELECT typeof(revenue) FROM sales WHERE transaction_id='1'").fetchone()[0] == "text"
