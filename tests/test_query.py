"""Read-only query execution: analytics, limits and cancellation."""

from __future__ import annotations

import pytest

from tabulite_mcp import database, importer
from tabulite_mcp.config import Config
from tabulite_mcp.database import QueryTimeout, execute_query
from tests.conftest import write_csv


@pytest.fixture
def joined(config, conns, sales_csv, customers_csv):
    db, cat = conns
    importer.import_csv(sales_csv, config, db, cat)
    importer.import_csv(customers_csv, config, db, cat)
    conn = database.connect_read_only(config.database_path)
    try:
        yield conn
    finally:
        conn.close()


def run(conn, sql, config: Config):
    return execute_query(conn, sql, config.max_query_rows, config.query_timeout_seconds)


def test_plain_select(read_only, config):
    result = run(read_only, "SELECT transaction_id, revenue FROM sales ORDER BY transaction_id", config)
    assert result["columns"] == ["transaction_id", "revenue"]
    assert result["returned_rows"] == 8
    assert result["truncated"] is False
    assert result["rows"][0] == ["1", "125.40"]
    assert result["execution_time_seconds"] >= 0


def test_group_by_with_safe_conversion(read_only, config):
    result = run(
        read_only,
        """
        SELECT channel,
               SUM(TRY_REAL(revenue)) AS revenue,
               COUNT(TRY_REAL(revenue)) AS valid_rows,
               COUNT(*) AS total_rows
        FROM sales
        WHERE channel IS NOT NULL
        GROUP BY channel
        ORDER BY revenue DESC
        """,
        config,
    )
    rows = {row[0]: row[1:] for row in result["rows"]}
    assert rows["email"][0] == pytest.approx(205.40)   # 125.40 + 80.00
    assert rows["email"][1] == 2                       # two convertible values
    assert rows["email"][2] == 4                       # out of four email rows
    assert rows["organic"][0] is None                  # its only revenue is "unknown"


def test_common_table_expression(read_only, config):
    result = run(
        read_only,
        """
        WITH monthly AS (
            SELECT substr(TRY_DATE(transaction_date), 1, 7) AS month,
                   SUM(TRY_REAL(revenue)) AS revenue
            FROM sales
            GROUP BY month
        )
        SELECT month, revenue FROM monthly WHERE revenue IS NOT NULL ORDER BY month
        """,
        config,
    )
    assert [row[0] for row in result["rows"]] == ["2025-01", "2025-03", "2025-04"]


def test_join_across_imported_tables(joined, config):
    result = run(
        joined,
        """
        SELECT c.region, SUM(TRY_REAL(s.revenue)) AS revenue
        FROM sales s
        JOIN customers c ON c.customer = s.customer
        GROUP BY c.region
        ORDER BY revenue DESC NULLS LAST
        """,
        config,
    )
    regions = {row[0]: row[1] for row in result["rows"]}
    assert regions["North"] == pytest.approx(145.39)   # 125.40 + 19.99
    assert regions["West"] == pytest.approx(240.10)


def test_window_function(read_only, config):
    result = run(
        read_only,
        """
        SELECT transaction_id,
               TRY_REAL(revenue) AS revenue,
               RANK() OVER (ORDER BY TRY_REAL(revenue) DESC) AS rank
        FROM sales
        WHERE TRY_REAL(revenue) IS NOT NULL
        """,
        config,
    )
    assert result["rows"][0] == ["7", 240.10, 1]


def test_recursive_cte_is_allowed(read_only, config):
    result = run(
        read_only,
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 5) "
        "SELECT SUM(x) FROM n",
        config,
    )
    assert result["rows"] == [[15]]


def test_read_only_metadata_inspection_is_allowed(read_only, config):
    result = run(read_only, "SELECT name FROM sqlite_master WHERE type = 'table'", config)
    assert ["sales"] in result["rows"]


def test_results_are_capped_and_truncation_is_reported(config, conns):
    db, cat = conns
    path = write_csv(config.source_dir / "big.csv", ["n"], [[str(i)] for i in range(1_500)])
    importer.import_csv(path, config, db, cat)

    conn = database.connect_read_only(config.database_path)
    try:
        result = run(conn, "SELECT n FROM big", config)
        assert result["returned_rows"] == 1_000
        assert result["truncated"] is True
        assert result["row_limit"] == 1_000

        # Aggregating inside SQLite is the way around the cap.
        summary = run(conn, "SELECT COUNT(*), SUM(TRY_INTEGER(n)) FROM big", config)
        assert summary["rows"] == [[1_500, sum(range(1_500))]]
        assert summary["truncated"] is False
    finally:
        conn.close()


def test_pathological_query_is_canceled(read_only):
    """An unbounded recursive CTE must not occupy the server indefinitely."""
    with pytest.raises(QueryTimeout):
        execute_query(
            read_only,
            "WITH RECURSIVE forever(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM forever) "
            "SELECT COUNT(*) FROM forever",
            max_rows=10,
            timeout_seconds=0.25,
        )

    # The connection is still usable afterwards.
    assert execute_query(read_only, "SELECT 1", 10, 5.0)["rows"] == [[1]]
