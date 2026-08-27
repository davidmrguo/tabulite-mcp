"""The MCP tool surface, exercised both directly and over an MCP session."""

from __future__ import annotations

import anyio
import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from tabulite_mcp import server as server_module
from tests.conftest import SALES_HEADER, SALES_ROWS, write_csv


@pytest.fixture
def project(monkeypatch, config, sales_csv, customers_csv):
    """Point the server module at a throwaway project directory."""
    monkeypatch.setattr(server_module, "CONFIG", config)
    return config


# --------------------------------------------------------------------------
# Tools called directly
# --------------------------------------------------------------------------

def test_list_sources_reports_discovery_and_import_status(project):
    listing = server_module.list_sources()
    assert listing["file_count"] == 2
    names = {entry["filename"]: entry for entry in listing["files"]}
    assert names["sales.csv"]["import_status"] == "not imported"
    assert names["sales.csv"]["size_bytes"] > 0

    server_module.import_source("sales.csv")
    after = {e["filename"]: e for e in server_module.list_sources()["files"]}
    assert after["sales.csv"]["import_status"] == "imported"
    assert after["sales.csv"]["table_name"] == "sales"
    assert after["sales.csv"]["source_id"]


def test_list_sources_does_not_leave_the_source_directory(project, tmp_path):
    (tmp_path / "outside.csv").write_text("a\n1\n")
    listed = {entry["filename"] for entry in server_module.list_sources()["files"]}
    assert listed == {"sales.csv", "customers.csv"}


def test_inspect_source_rejects_traversal(project):
    with pytest.raises(ToolError):
        server_module.inspect_source("../outside.csv")
    with pytest.raises(ToolError):
        server_module.import_source("/etc/passwd")


def test_import_then_profile_then_query(project):
    imported = server_module.import_source("sales.csv")
    assert imported["table_name"] == "sales"
    assert imported["row_count"] == len(SALES_ROWS)
    assert imported["reused_existing_import"] is False

    profile = server_module.profile_table("sales")
    types = {c["column_name"]: c["logical_type"] for c in profile["columns"]}
    assert types["transaction_date"] == "DATE"
    # This deliberately messy fixture has two unparseable revenues out of six
    # non-null values, so conservative inference leaves both columns TEXT.
    assert types["revenue"] == "TEXT"
    assert types["quantity"] == "TEXT"

    column = server_module.profile_column("sales", "revenue")
    assert column["null_count"] == 2          # "" and "N/A"
    assert column["valid_real_count"] == 4
    assert column["sample_values"]

    tables = server_module.list_tables()
    assert tables["tables"][0]["table_name"] == "sales"
    assert tables["tables"][0]["source_filename"] == "sales.csv"

    result = server_module.query_sql(
        "SELECT channel, SUM(TRY_REAL(revenue)) AS revenue FROM sales GROUP BY channel"
    )
    assert "channel" in result["columns"]
    assert result["truncated"] is False


def test_import_of_renamed_identical_file_is_deduplicated(project, config):
    first = server_module.import_source("sales.csv")
    write_csv(config.source_dir / "sales_FINAL_v2.csv", SALES_HEADER, SALES_ROWS)

    second = server_module.import_source("sales_FINAL_v2.csv")
    assert second["reused_existing_import"] is True
    assert second["source_id"] == first["source_id"]
    assert second["table_name"] == first["table_name"]
    assert server_module.list_tables()["table_count"] == 1


def test_sample_table_is_capped(project):
    server_module.import_source("sales.csv")
    assert server_module.sample_table("sales")["returned_rows"] == 8
    assert server_module.sample_table("sales", limit=3)["returned_rows"] == 3
    assert server_module.sample_table("sales", limit=10_000)["returned_rows"] == 8
    with pytest.raises(ToolError):
        server_module.sample_table("nope")


def test_query_sql_rejects_mutating_statements(project):
    server_module.import_source("sales.csv")
    for sql in ["DROP TABLE sales", "DELETE FROM sales", "UPDATE sales SET revenue='0'",
                "ATTACH DATABASE '/tmp/x.sqlite' AS x", "SELECT 1; DROP TABLE sales"]:
        with pytest.raises(ToolError):
            server_module.query_sql(sql)

    assert server_module.query_sql("SELECT COUNT(*) AS n FROM sales")["rows"] == [[8]]


def test_query_sql_caps_rows(project, config):
    write_csv(config.source_dir / "big.csv", ["n"], [[str(i)] for i in range(1_200)])
    server_module.import_source("big.csv")
    result = server_module.query_sql("SELECT n FROM big")
    assert result["returned_rows"] == 1_000
    assert result["truncated"] is True


def test_export_query_writes_into_the_workspace(project, config):
    server_module.import_source("sales.csv")
    result = server_module.export_query(
        "SELECT transaction_id, TRY_REAL(revenue) AS revenue FROM sales "
        "WHERE TRY_REAL(revenue) IS NOT NULL",
        file_name="clean_revenue",
    )
    assert result["row_count"] == 4
    assert result["relative_path"] == "exports/clean_revenue.csv"
    assert (config.exports_dir / "clean_revenue.csv").exists()

    with pytest.raises(ToolError):
        server_module.export_query("DELETE FROM sales", file_name="bad")
    with pytest.raises(ToolError):
        server_module.export_query("SELECT 1", file_name="../escape.csv")


# --------------------------------------------------------------------------
# Tools called over a real MCP session
# --------------------------------------------------------------------------

def test_tools_are_reachable_over_mcp(project):
    async def scenario():
        async with Client(server_module.server) as client:
            listed = await client.list_tools()
            names = {tool.name for tool in listed.tools}
            assert names == {
                "list_sources", "inspect_source", "import_source", "list_tables",
                "profile_table", "profile_column", "sample_table", "query_sql",
                "export_query",
            }

            sources = await client.call_tool("list_sources", {})
            assert sources.is_error is False
            assert sources.structured_content["file_count"] == 2

            imported = await client.call_tool("import_source", {"path": "sales.csv"})
            assert imported.structured_content["table_name"] == "sales"
            assert imported.structured_content["row_count"] == 8

            queried = await client.call_tool(
                "query_sql",
                {"sql": "SELECT product, SUM(TRY_REAL(revenue)) AS revenue FROM sales "
                        "GROUP BY product ORDER BY revenue DESC"},
            )
            assert queried.is_error is False
            assert queried.structured_content["columns"] == ["product", "revenue"]

            rejected = await client.call_tool("query_sql", {"sql": "DROP TABLE sales"})
            assert rejected.is_error is True
            # The reason reaches the model so it can correct itself.
            assert "forbidden" in rejected.content[0].text or "read-only" in rejected.content[0].text

            broken = await client.call_tool("query_sql", {"sql": "SELECT * FROM nope"})
            assert broken.is_error is True
            assert "no such table" in broken.content[0].text

            exported = await client.call_tool(
                "export_query",
                {"sql": "SELECT * FROM sales", "file_name": "everything"},
            )
            assert exported.structured_content["row_count"] == 8
            assert (project.exports_dir / "everything.csv").exists()

    anyio.run(scenario)
