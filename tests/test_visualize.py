"""visualize_data: referencing a query result and the brief it hands back."""

from __future__ import annotations

import anyio
import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from tabulite_mcp import server as server_module
from tabulite_mcp.results import QueryResultError, QueryResultRegistry
from tests.conftest import write_csv


@pytest.fixture
def project(monkeypatch, config, sales_csv):
    monkeypatch.setattr(server_module, "CONFIG", config)
    server_module.QUERY_RESULTS.clear()
    return config


# --------------------------------------------------------------------------
# The registry on its own
# --------------------------------------------------------------------------

def test_registry_records_shape_but_never_rows():
    registry = QueryResultRegistry()
    result = {
        "columns": ["channel", "revenue"],
        "rows": [["email", 205.4]],
        "returned_rows": 1,
        "truncated": False,
        "row_limit": 1_000,
    }
    ref = registry.get(registry.record("SELECT channel, revenue FROM sales", result))

    assert ref.columns == ("channel", "revenue")
    assert ref.returned_rows == 1
    assert ref.truncated is False
    assert ref.sql == "SELECT channel, revenue FROM sales"
    assert not hasattr(ref, "rows")
    assert "email" not in repr(ref)


def test_registry_rejects_unknown_and_missing_ids():
    registry = QueryResultRegistry()
    with pytest.raises(QueryResultError, match="required"):
        registry.get(None)
    with pytest.raises(QueryResultError, match="no query results have been recorded"):
        registry.get("qr_deadbeef")

    latest = registry.record("SELECT 1", {"columns": ["1"], "returned_rows": 1})
    with pytest.raises(QueryResultError, match=latest):
        registry.get("qr_deadbeef")


def test_registry_forgets_the_oldest_results_first():
    registry = QueryResultRegistry(max_entries=2)
    first = registry.record("SELECT 1", {"returned_rows": 1})
    second = registry.record("SELECT 2", {"returned_rows": 1})
    third = registry.record("SELECT 3", {"returned_rows": 1})

    assert registry.count() == 2
    assert registry.get(second).sql == "SELECT 2"
    assert registry.get(third).sql == "SELECT 3"
    with pytest.raises(QueryResultError, match="superseded"):
        registry.get(first)


def test_registry_ids_are_unique():
    registry = QueryResultRegistry()
    ids = {registry.record("SELECT 1", {"returned_rows": 1}) for _ in range(50)}
    assert len(ids) == 50


# --------------------------------------------------------------------------
# The tool
# --------------------------------------------------------------------------

def test_query_sql_returns_a_referenceable_result_id(project):
    server_module.import_source("sales.csv")
    result = server_module.query_sql("SELECT channel FROM sales")
    assert result["query_result_id"].startswith("qr_")
    assert server_module.QUERY_RESULTS.get(result["query_result_id"]).returned_rows == 8


def test_visualize_data_describes_the_referenced_result(project):
    server_module.import_source("sales.csv")
    queried = server_module.query_sql(
        "SELECT channel, SUM(TRY_REAL(revenue)) AS revenue FROM sales GROUP BY channel"
    )

    payload = server_module.visualize_data(
        queried["query_result_id"], intent="compare revenue by channel"
    )

    assert payload["status"] == "render_in_client"
    assert payload["renderer"] == "ai_client"
    assert payload["query_result_id"] == queried["query_result_id"]
    assert payload["intent"] == "compare revenue by channel"

    source = payload["source_result"]
    assert source["produced_by"] == "query_sql"
    assert source["columns"] == ["channel", "revenue"]
    assert source["returned_rows"] == queried["returned_rows"]
    assert source["truncated"] is False
    assert "GROUP BY channel" in source["sql"]
    assert source["created_at"].endswith("+00:00")

    # No rendered artifact of any kind comes back from the server.
    assert "rows" not in payload and "image" not in payload and "html" not in payload


def test_visualize_data_guidance_keeps_the_client_off_extra_data(project):
    server_module.import_source("sales.csv")
    queried = server_module.query_sql("SELECT product FROM sales")
    guidance = " ".join(server_module.visualize_data(queried["query_result_id"])["guidance"])

    assert "do not re-query" in guidance
    assert "bar, line, scatter or table" in guidance
    assert "No dashboard" in guidance


def test_visualize_data_flags_a_truncated_result(project, config):
    write_csv(config.source_dir / "big.csv", ["n"], [[str(i)] for i in range(1_200)])
    server_module.import_source("big.csv")
    queried = server_module.query_sql("SELECT n FROM big")
    assert queried["truncated"] is True

    payload = server_module.visualize_data(queried["query_result_id"])
    assert payload["source_result"]["truncated"] is True
    assert any("partial view" in line for line in payload["guidance"])


def test_visualize_data_rejects_an_unknown_result_reference(project):
    server_module.import_source("sales.csv")
    with pytest.raises(ToolError, match="unknown query_result_id"):
        server_module.visualize_data("qr_neverissued")


def test_visualize_data_rejects_a_result_lost_to_a_restart(project):
    server_module.import_source("sales.csv")
    stale = server_module.query_sql("SELECT channel FROM sales")["query_result_id"]

    server_module.QUERY_RESULTS.clear()  # what a server restart looks like

    with pytest.raises(ToolError, match="Re-run the query"):
        server_module.visualize_data(stale)


def test_visualize_data_is_reachable_over_mcp(project):
    async def scenario():
        async with Client(server_module.server) as client:
            listed = await client.list_tools()
            assert "visualize_data" in {tool.name for tool in listed.tools}

            await client.call_tool("import_source", {"path": "sales.csv"})
            queried = await client.call_tool(
                "query_sql",
                {"sql": "SELECT product, SUM(TRY_REAL(revenue)) AS revenue FROM sales "
                        "GROUP BY product"},
            )
            result_id = queried.structured_content["query_result_id"]

            rendered = await client.call_tool(
                "visualize_data",
                {"query_result_id": result_id, "intent": "revenue by product"},
            )
            assert rendered.is_error is False
            assert rendered.structured_content["query_result_id"] == result_id
            assert rendered.structured_content["source_result"]["columns"] == [
                "product", "revenue",
            ]

            rejected = await client.call_tool(
                "visualize_data", {"query_result_id": "qr_bogus"}
            )
            assert rejected.is_error is True
            assert "unknown query_result_id" in rejected.content[0].text

    anyio.run(scenario)


# --------------------------------------------------------------------------
# A chart can only ever come from a query
# --------------------------------------------------------------------------

def test_no_other_tool_mints_a_result_id(project):
    """query_sql is the only way a referenceable result comes into existence."""
    assert server_module.QUERY_RESULTS.count() == 0

    server_module.import_source("sales.csv")
    server_module.list_sources()
    server_module.list_tables()
    server_module.inspect_source("sales.csv")
    server_module.profile_table("sales")
    server_module.profile_column("sales", "revenue")
    sampled = server_module.sample_table("sales")
    exported = server_module.export_query("SELECT * FROM sales", file_name="all")

    # Sampling and exporting both produce rows, and neither is visualizable.
    assert "query_result_id" not in sampled
    assert "query_result_id" not in exported
    assert server_module.QUERY_RESULTS.count() == 0
    assert server_module.QUERY_RESULTS.latest_id() is None


def test_a_failed_query_leaves_nothing_to_visualize(project):
    server_module.import_source("sales.csv")

    with pytest.raises(ToolError):
        server_module.query_sql("SELECT * FROM nope")          # bad SQL
    with pytest.raises(ToolError):
        server_module.query_sql("DROP TABLE sales")            # rejected as unsafe

    assert server_module.QUERY_RESULTS.count() == 0


def test_visualize_data_refuses_an_empty_result(project):
    """The one path where a model might otherwise invent plausible numbers."""
    server_module.import_source("sales.csv")
    empty = server_module.query_sql(
        "SELECT channel, revenue FROM sales WHERE channel = 'nonexistent'"
    )
    assert empty["returned_rows"] == 0

    with pytest.raises(ToolError, match="nothing to visualize"):
        server_module.visualize_data(empty["query_result_id"])


def test_visualize_data_forbids_charting_invented_values(project):
    server_module.import_source("sales.csv")
    queried = server_module.query_sql("SELECT channel FROM sales")
    guidance = " ".join(server_module.visualize_data(queried["query_result_id"])["guidance"])

    assert "only the 8 row(s) already returned" in guidance
