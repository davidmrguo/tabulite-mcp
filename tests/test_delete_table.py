"""delete_table: the two-step confirmation gate and what it actually removes."""

from __future__ import annotations

import time

import anyio
import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from tabulite_mcp import catalog, database
from tabulite_mcp import server as server_module
from tabulite_mcp.confirm import ConfirmationError, ConfirmationRegistry
from tests.conftest import SALES_HEADER, SALES_ROWS, write_csv


@pytest.fixture
def project(monkeypatch, config, sales_csv, customers_csv):
    monkeypatch.setattr(server_module, "CONFIG", config)
    server_module.CONFIRMATIONS.clear()
    return config


# --------------------------------------------------------------------------
# The registry on its own
# --------------------------------------------------------------------------

def test_registry_requires_both_halves():
    registry = ConfirmationRegistry()
    token, _ = registry.issue("delete_table", "sales")

    with pytest.raises(ConfirmationError, match="DELETE"):
        registry.consume(token, "delete_table", "sales", None)
    with pytest.raises(ConfirmationError, match="DELETE"):
        registry.consume(token, "delete_table", "sales", "delete")
    with pytest.raises(ConfirmationError, match="confirmation_token"):
        registry.consume(None, "delete_table", "sales", "DELETE")

    registry.consume(token, "delete_table", "sales", "DELETE")


def test_registry_tokens_are_single_use():
    registry = ConfirmationRegistry()
    token, _ = registry.issue("delete_table", "sales")
    registry.consume(token, "delete_table", "sales", "DELETE")
    with pytest.raises(ConfirmationError, match="unknown, already used or expired"):
        registry.consume(token, "delete_table", "sales", "DELETE")


def test_registry_tokens_are_bound_to_their_target():
    registry = ConfirmationRegistry()
    token, _ = registry.issue("delete_table", "sales")
    with pytest.raises(ConfirmationError, match="issued for 'sales'"):
        registry.consume(token, "delete_table", "customers", "DELETE")


def test_registry_tokens_expire():
    registry = ConfirmationRegistry(ttl_seconds=0.05)
    token, _ = registry.issue("delete_table", "sales")
    time.sleep(0.1)
    with pytest.raises(ConfirmationError):
        registry.consume(token, "delete_table", "sales", "DELETE")
    assert registry.pending_count() == 0


# --------------------------------------------------------------------------
# Step 1: warn, delete nothing
# --------------------------------------------------------------------------

def test_first_call_warns_and_deletes_nothing(project):
    server_module.import_source("sales.csv")

    result = server_module.delete_table("sales")

    assert result["status"] == "confirmation_required"
    assert result["confirmation_token"]
    assert "DELETE" in result["next_step"]
    assert result["will_delete"]["rows"] == len(SALES_ROWS)
    assert result["will_delete"]["column_profiles"] == len(SALES_HEADER)
    assert "permanently delete" in result["warning"]

    # Nothing actually happened.
    assert server_module.list_tables()["table_count"] == 1
    assert server_module.query_sql("SELECT COUNT(*) FROM sales")["rows"] == [[8]]


def test_warning_says_whether_the_source_can_be_reimported(project, config):
    server_module.import_source("sales.csv")
    available = server_module.delete_table("sales")
    assert available["source_file"]["present"] is True
    assert available["source_file"]["re_importable"] is True
    assert "still in source/" in available["warning"]

    # Same table, but the CSV it came from is gone: deletion is now final.
    (config.source_dir / "sales.csv").unlink()
    orphaned = server_module.delete_table("sales")
    assert orphaned["source_file"]["present"] is False
    assert "CANNOT be re-imported" in orphaned["warning"]


def test_unknown_table_is_rejected_before_any_warning(project):
    with pytest.raises(ToolError, match="unknown table"):
        server_module.delete_table("does_not_exist")


# --------------------------------------------------------------------------
# Step 2: the gate
# --------------------------------------------------------------------------

@pytest.mark.parametrize("word", ["delete", "Delete", "DELETE PLEASE", "yes", "y", ""])
def test_only_the_exact_word_is_accepted(project, word):
    server_module.import_source("sales.csv")
    token = server_module.delete_table("sales")["confirmation_token"]

    with pytest.raises(ToolError, match="DELETE"):
        server_module.delete_table("sales", confirm=word, confirmation_token=token)

    assert server_module.query_sql("SELECT COUNT(*) FROM sales")["rows"] == [[8]]


def test_confirmation_without_a_token_is_rejected(project):
    """A single call can never delete, however it is phrased."""
    server_module.import_source("sales.csv")
    with pytest.raises(ToolError, match="missing confirmation_token"):
        server_module.delete_table("sales", confirm="DELETE")
    assert server_module.list_tables()["table_count"] == 1


def test_a_token_cannot_be_reused_or_pointed_at_another_table(project):
    server_module.import_source("sales.csv")
    server_module.import_source("customers.csv")
    token = server_module.delete_table("sales")["confirmation_token"]

    with pytest.raises(ToolError, match="issued for 'sales'"):
        server_module.delete_table("customers", confirm="DELETE", confirmation_token=token)

    server_module.delete_table("sales", confirm="DELETE", confirmation_token=token)
    server_module.import_source("sales.csv")
    with pytest.raises(ToolError, match="already used or expired"):
        server_module.delete_table("sales", confirm="DELETE", confirmation_token=token)


# --------------------------------------------------------------------------
# What deletion actually does
# --------------------------------------------------------------------------

def test_confirmed_delete_removes_table_profile_and_catalog_entries(project, config):
    imported = server_module.import_source("sales.csv")
    server_module.import_source("customers.csv")
    token = server_module.delete_table("sales")["confirmation_token"]

    result = server_module.delete_table("sales", confirm="DELETE", confirmation_token=token)

    assert result["status"] == "deleted"
    assert result["rows_deleted"] == len(SALES_ROWS)
    assert result["column_profiles_deleted"] == len(SALES_HEADER)
    assert result["catalog_source_record_removed"] is True

    # The table is gone from SQLite...
    assert server_module.list_tables()["table_count"] == 1
    with pytest.raises(ToolError, match="unknown table"):
        server_module.profile_table("sales")

    # ...and so is every trace of it in the catalog.
    cat = catalog.connect(config.catalog_path)
    try:
        assert catalog.get_import(cat, "sales") is None
        assert catalog.get_column_profiles(cat, "sales") == []
        assert catalog.get_source(cat, imported["source_id"]) is None
    finally:
        cat.close()

    # The untouched table is untouched.
    assert server_module.query_sql("SELECT COUNT(*) FROM customers")["rows"] == [[4]]


def test_delete_leaves_the_source_csv_and_existing_exports_alone(project, config):
    server_module.import_source("sales.csv")
    export = server_module.export_query("SELECT * FROM sales", file_name="before_delete")
    token = server_module.delete_table("sales")["confirmation_token"]

    server_module.delete_table("sales", confirm="DELETE", confirmation_token=token)

    assert (config.source_dir / "sales.csv").is_file()
    assert (config.exports_dir / "before_delete.csv").is_file()

    # The export's own record survives: it is history, not table data.
    cat = catalog.connect(config.catalog_path)
    try:
        row = cat.execute("SELECT * FROM exports WHERE export_id = ?",
                          (export["export_id"],)).fetchone()
        assert row is not None
    finally:
        cat.close()


def test_deleted_table_can_be_imported_again_cleanly(project):
    first = server_module.import_source("sales.csv")
    token = server_module.delete_table("sales")["confirmation_token"]
    server_module.delete_table("sales", confirm="DELETE", confirmation_token=token)

    again = server_module.import_source("sales.csv")

    # Same name, not sales_<hash>: the old one really is gone.
    assert again["table_name"] == "sales"
    assert again["source_id"] == first["source_id"]
    assert again["reused_existing_import"] is False
    assert again["row_count"] == len(SALES_ROWS)
    assert server_module.profile_table("sales")["row_count"] == len(SALES_ROWS)


def test_delete_reclaims_disk_space(project, config):
    write_csv(config.source_dir / "bulky.csv", ["n", "payload"],
              [[str(i), "x" * 200] for i in range(5_000)])
    server_module.import_source("bulky.csv")
    grown = database.database_size_bytes(config.database_path)

    token = server_module.delete_table("bulky")["confirmation_token"]
    result = server_module.delete_table("bulky", confirm="DELETE", confirmation_token=token)

    assert result["bytes_freed"] > 500_000
    assert database.database_size_bytes(config.database_path) < grown
    assert result["database_bytes_after"] < result["database_bytes_before"]


def test_source_record_is_kept_while_any_import_still_references_it(config):
    """Guard on the catalog helper: the last import out removes the source row."""
    cat = catalog.connect(config.catalog_path)
    try:
        catalog.record_source(cat, source_id="abc123", filename="a.csv",
                              relative_path="a.csv", size_bytes=1, modified_at="2026-01-01T00:00:00+00:00")
        for table in ("first", "second"):
            catalog.record_import(cat, source_id="abc123", database_path="db",
                                  table_name=table, row_count=1)

        catalog.delete_import(cat, "first")
        assert catalog.delete_source_if_unreferenced(cat, "abc123") is False
        assert catalog.get_source(cat, "abc123") is not None

        catalog.delete_import(cat, "second")
        assert catalog.delete_source_if_unreferenced(cat, "abc123") is True
        assert catalog.get_source(cat, "abc123") is None
    finally:
        cat.close()


# --------------------------------------------------------------------------
# Over a real MCP session
# --------------------------------------------------------------------------

def test_delete_flow_over_mcp(project):
    async def scenario():
        async with Client(server_module.server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert tools["delete_table"].annotations.destructive_hint is True

            await client.call_tool("import_source", {"path": "sales.csv"})

            warned = await client.call_tool("delete_table", {"table_name": "sales"})
            assert warned.is_error is False
            body = warned.structured_content
            assert body["status"] == "confirmation_required"

            # A model trying to skip the human step gets nowhere.
            straight = await client.call_tool(
                "delete_table", {"table_name": "sales", "confirm": "DELETE"}
            )
            assert straight.is_error is True

            still_there = await client.call_tool(
                "query_sql", {"sql": "SELECT COUNT(*) AS n FROM sales"}
            )
            assert still_there.structured_content["rows"] == [[8]]

            done = await client.call_tool("delete_table", {
                "table_name": "sales",
                "confirm": "DELETE",
                "confirmation_token": body["confirmation_token"],
            })
            assert done.structured_content["status"] == "deleted"

            listed = await client.call_tool("list_tables", {})
            assert listed.structured_content["table_count"] == 0

    anyio.run(scenario)
