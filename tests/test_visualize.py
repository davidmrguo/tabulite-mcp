"""visualize_data: referencing a query result, and the chart drawn from it."""

from __future__ import annotations

import struct
from pathlib import Path

import anyio
import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from tabulite_mcp import charts
from tabulite_mcp import server as server_module
from tabulite_mcp.results import QueryResultError, QueryResultRegistry
from tests.conftest import write_csv


@pytest.fixture
def project(monkeypatch, config, sales_csv):
    monkeypatch.setattr(server_module, "CONFIG", config)
    server_module.QUERY_RESULTS.clear()
    return config


def png_size(path: Path) -> tuple[int, int]:
    """Width and height straight out of the PNG header."""
    header = path.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return struct.unpack(">II", header[16:24])


def chart_of(payload) -> dict:
    return payload.structured_content["chart"]


def revenue_by_channel(project) -> str:
    server_module.import_source("sales.csv")
    return server_module.query_sql(
        "SELECT channel, SUM(TRY_REAL(revenue)) AS revenue FROM sales GROUP BY channel"
    )["query_result_id"]


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
# The tool draws a real file
# --------------------------------------------------------------------------

def test_query_sql_returns_a_referenceable_result_id(project):
    server_module.import_source("sales.csv")
    result = server_module.query_sql("SELECT channel FROM sales")
    assert result["query_result_id"].startswith("qr_")
    assert server_module.QUERY_RESULTS.get(result["query_result_id"]).returned_rows == 8


def test_visualize_data_writes_a_png_into_the_charts_directory(project):
    payload = server_module.visualize_data(
        revenue_by_channel(project), intent="compare revenue by channel"
    )
    chart = chart_of(payload)

    assert payload.structured_content["status"] == "rendered"
    assert payload.structured_content["renderer"] == "matplotlib"
    assert payload.structured_content["intent"] == "compare revenue by channel"

    written = Path(chart["absolute_path"])
    assert written.parent == project.charts_dir
    assert written.exists() and written.suffix == ".png"
    assert chart["relative_path"] == f"charts/{written.name}"
    assert chart["file_size_bytes"] > 0


def test_the_image_comes_back_for_the_client_to_show(project):
    payload = server_module.visualize_data(revenue_by_channel(project))

    images = [block for block in payload.content if block.type == "image"]
    assert len(images) == 1
    assert images[0].mime_type == "image/png"
    assert images[0].data  # base64 of the file on disk


def test_charts_are_600px_wide_by_default(project):
    payload = server_module.visualize_data(revenue_by_channel(project))
    chart = chart_of(payload)

    assert chart["width_px"] == 600
    assert png_size(Path(chart["absolute_path"])) == (600, chart["height_px"])


def test_height_follows_the_chart_type(project):
    result_id = revenue_by_channel(project)
    heights = {
        kind: chart_of(server_module.visualize_data(result_id, chart_type=kind))["height_px"]
        for kind in ("bar", "line", "area", "pie")
    }
    assert heights == {"bar": 380, "line": 360, "area": 360, "pie": 400}

    numeric = server_module.query_sql(
        "SELECT TRY_REAL(quantity) AS quantity, TRY_REAL(revenue) AS revenue "
        "FROM sales WHERE TRY_REAL(revenue) IS NOT NULL"
    )["query_result_id"]
    assert chart_of(
        server_module.visualize_data(numeric, chart_type="scatter")
    )["height_px"] == 420


def test_a_horizontal_bar_chart_grows_with_its_rows(project, config):
    write_csv(
        config.source_dir / "many.csv", ["name", "n"],
        [[f"row-{i}", str(i)] for i in range(20)],
    )
    server_module.import_source("many.csv")

    few = server_module.query_sql("SELECT name, n FROM many LIMIT 3")["query_result_id"]
    lots = server_module.query_sql("SELECT name, n FROM many")["query_result_id"]

    short = chart_of(server_module.visualize_data(few, chart_type="barh"))
    tall = chart_of(server_module.visualize_data(lots, chart_type="barh"))

    assert tall["height_px"] > short["height_px"]
    assert png_size(Path(tall["absolute_path"]))[1] == tall["height_px"]


def test_the_caller_controls_size_deterministically(project):
    payload = server_module.visualize_data(
        revenue_by_channel(project), width_px=900, height_px=500
    )
    assert png_size(Path(chart_of(payload)["absolute_path"])) == (900, 500)


def test_the_same_request_renders_the_same_bytes(project):
    result_id = revenue_by_channel(project)
    first = server_module.visualize_data(result_id, title="Revenue")
    second = server_module.visualize_data(result_id, title="Revenue")

    assert (
        Path(chart_of(first)["absolute_path"]).read_bytes()
        == Path(chart_of(second)["absolute_path"]).read_bytes()
    )


def test_an_existing_chart_is_never_overwritten(project):
    result_id = revenue_by_channel(project)
    first = chart_of(server_module.visualize_data(result_id, file_name="revenue"))
    second = chart_of(server_module.visualize_data(result_id, file_name="revenue"))

    assert first["file_name"] == "revenue.png"
    assert second["file_name"] == "revenue_2.png"
    assert Path(first["absolute_path"]).exists()


def test_a_chart_file_name_cannot_escape_the_charts_directory(project):
    result_id = revenue_by_channel(project)
    with pytest.raises(ToolError, match="must not contain a path"):
        server_module.visualize_data(result_id, file_name="../../escape.png")


# --------------------------------------------------------------------------
# What is drawn comes from the query, and only from the query
# --------------------------------------------------------------------------

def test_the_tool_takes_no_data_argument(project):
    """The rows are re-read from the query; there is no way to pass values in."""
    import inspect

    parameters = set(inspect.signature(server_module.visualize_data).parameters)
    assert "query_result_id" in parameters
    assert not parameters & {"rows", "data", "values", "columns", "labels"}


def test_the_chart_reflects_the_recorded_sql(project):
    payload = server_module.visualize_data(revenue_by_channel(project))
    source = payload.structured_content["source_result"]

    assert source["produced_by"] == "query_sql"
    assert "GROUP BY channel" in source["sql"]
    assert source["columns"] == ["channel", "revenue"]
    assert source["created_at"].endswith("+00:00")
    assert chart_of(payload)["x_column"] == "channel"
    assert chart_of(payload)["y_columns"] == ["revenue"]


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


def test_visualize_data_refuses_an_empty_result(project):
    """The one path where a model might otherwise invent plausible numbers."""
    server_module.import_source("sales.csv")
    empty = server_module.query_sql(
        "SELECT channel, revenue FROM sales WHERE channel = 'nonexistent'"
    )
    assert empty["returned_rows"] == 0

    with pytest.raises(ToolError, match="nothing to visualize"):
        server_module.visualize_data(empty["query_result_id"])


def test_a_chart_of_a_deleted_table_fails_instead_of_drawing_something_else(project):
    result_id = revenue_by_channel(project)

    token = server_module.delete_table("sales")["confirmation_token"]
    server_module.delete_table("sales", confirm="DELETE", confirmation_token=token)

    with pytest.raises(ToolError, match="may have been deleted"):
        server_module.visualize_data(result_id)


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


def test_a_truncated_result_is_flagged_on_the_chart(project, config):
    write_csv(config.source_dir / "big.csv", ["n"], [[str(i)] for i in range(1_200)])
    server_module.import_source("big.csv")
    queried = server_module.query_sql("SELECT n, n AS value FROM big")
    assert queried["truncated"] is True

    payload = server_module.visualize_data(queried["query_result_id"], chart_type="line")
    assert payload.structured_content["source_result"]["truncated"] is True
    assert any("partial result" in w for w in payload.structured_content["warnings"])


# --------------------------------------------------------------------------
# Values that are not numbers are dropped, never drawn as zero
# --------------------------------------------------------------------------

def test_unconvertible_values_are_left_out_rather_than_plotted_as_zero(project):
    """The chart and TRY_REAL() agree about what counts as a number."""
    server_module.import_source("sales.csv")
    # revenue is raw TEXT here, so "N/A", "unknown", "-" and "" are all in play.
    queried = server_module.query_sql("SELECT product, revenue FROM sales")

    payload = server_module.visualize_data(
        queried["query_result_id"], chart_type="bar", y="revenue"
    )
    chart = chart_of(payload)

    assert chart["skipped_values"] == 4  # "", "N/A", "unknown", "-"
    assert chart["plotted_rows"] == 8
    assert any("drawn as zero" in w for w in payload.structured_content["warnings"])


def test_a_column_with_no_numbers_at_all_is_refused(project):
    server_module.import_source("sales.csv")
    queried = server_module.query_sql("SELECT channel, product FROM sales")

    with pytest.raises(ToolError, match="no numeric column"):
        server_module.visualize_data(queried["query_result_id"])


# --------------------------------------------------------------------------
# The renderer's own rules
# --------------------------------------------------------------------------

def test_chart_type_must_be_one_we_actually_draw(project):
    with pytest.raises(ToolError, match="unknown chart_type"):
        server_module.visualize_data(revenue_by_channel(project), chart_type="sankey")


def test_theme_switches_the_whole_palette(project):
    result_id = revenue_by_channel(project)
    light = chart_of(server_module.visualize_data(result_id, theme="light"))
    dark = chart_of(server_module.visualize_data(result_id, theme="dark"))

    assert light["theme"] == "light" and dark["theme"] == "dark"
    assert light["series_colors"] != dark["series_colors"]
    with pytest.raises(ToolError, match="unknown theme"):
        server_module.visualize_data(result_id, theme="solarized")


def test_a_lone_column_name_is_accepted_where_a_list_is(project):
    """Models pass `y="revenue"` as often as `y=["revenue"]`."""
    result_id = revenue_by_channel(project)
    one = chart_of(server_module.visualize_data(result_id, y="revenue"))
    many = chart_of(server_module.visualize_data(result_id, y=["revenue"]))

    assert one["y_columns"] == many["y_columns"] == ["revenue"]


def test_an_unknown_column_says_which_columns_exist(project):
    with pytest.raises(ToolError, match="is not in this result"):
        server_module.visualize_data(revenue_by_channel(project), y="profit")


def test_highlight_emphasises_categories_and_greys_the_rest(project):
    payload = server_module.visualize_data(
        revenue_by_channel(project), highlight="email"
    )
    assert not any("are not in" in w for w in payload.structured_content["warnings"])

    ignored = server_module.visualize_data(
        revenue_by_channel(project), highlight="carrier pigeon"
    )
    assert any("are not in" in w for w in ignored.structured_content["warnings"])


def test_the_legend_appears_for_two_series_and_not_for_one(project):
    server_module.import_source("sales.csv")
    one = server_module.query_sql(
        "SELECT channel, SUM(TRY_REAL(revenue)) AS revenue FROM sales GROUP BY channel"
    )["query_result_id"]
    two = server_module.query_sql(
        "SELECT channel, SUM(TRY_REAL(revenue)) AS revenue, "
        "SUM(TRY_REAL(quantity)) AS units FROM sales GROUP BY channel"
    )["query_result_id"]

    assert chart_of(server_module.visualize_data(one))["legend"] is False
    assert chart_of(server_module.visualize_data(two))["legend"] is True
    # ...and the caller can always override it.
    assert chart_of(server_module.visualize_data(one, legend=True))["legend"] is True


def test_colors_must_cover_every_series(project):
    result_id = revenue_by_channel(project)
    assert chart_of(
        server_module.visualize_data(result_id, colors="#ff0000")
    )["series_colors"] == ["#ff0000"]

    two = server_module.query_sql(
        "SELECT channel, SUM(TRY_REAL(revenue)) AS revenue, "
        "SUM(TRY_REAL(quantity)) AS units FROM sales GROUP BY channel"
    )["query_result_id"]
    with pytest.raises(ToolError, match="colour"):
        server_module.visualize_data(two, colors="#ff0000")


def test_a_scatter_plot_insists_on_a_numeric_x_axis(project):
    with pytest.raises(ToolError, match="needs a numeric x axis"):
        server_module.visualize_data(revenue_by_channel(project), chart_type="scatter")


def test_a_pie_takes_exactly_one_value_column(project):
    server_module.import_source("sales.csv")
    two = server_module.query_sql(
        "SELECT channel, SUM(TRY_REAL(revenue)) AS revenue, "
        "SUM(TRY_REAL(quantity)) AS units FROM sales GROUP BY channel"
    )["query_result_id"]

    with pytest.raises(ToolError, match="pie chart shows one value column"):
        server_module.visualize_data(two, chart_type="pie")


def test_a_pie_refuses_negative_values(project, config):
    write_csv(config.source_dir / "swings.csv", ["name", "delta"],
              [["up", "10"], ["down", "-4"]])
    server_module.import_source("swings.csv")
    queried = server_module.query_sql("SELECT name, TRY_REAL(delta) AS delta FROM swings")

    with pytest.raises(ToolError, match="cannot show negative values"):
        server_module.visualize_data(queried["query_result_id"], chart_type="pie")


def test_the_palette_is_never_stretched_past_its_validated_length(project, config):
    header = ["label"] + [f"s{i}" for i in range(9)]
    write_csv(config.source_dir / "wide.csv", header,
              [["a"] + [str(i) for i in range(9)]])
    server_module.import_source("wide.csv")
    queried = server_module.query_sql("SELECT * FROM wide")

    with pytest.raises(ToolError, match="does not invent a ninth"):
        server_module.visualize_data(queried["query_result_id"])
    # A scatter compares every series against every other, so it caps lower.
    with pytest.raises(ToolError, match="only separates three"):
        server_module.visualize_data(
            queried["query_result_id"], chart_type="scatter",
            x="s0", y=["s1", "s2", "s3", "s4"],
        )


def test_a_failed_render_leaves_no_half_written_file(project):
    before = set(project.charts_dir.iterdir())
    with pytest.raises(ToolError):
        server_module.visualize_data(revenue_by_channel(project), chart_type="scatter")
    assert set(project.charts_dir.iterdir()) == before


# --------------------------------------------------------------------------
# The renderer, directly
# --------------------------------------------------------------------------

def test_render_names_missing_categories_rather_than_leaving_them_blank(tmp_path):
    info = charts.render(
        columns=["channel", "revenue"],
        rows=[[None, 10.0], ["", 5.0], ["email", 20.0]],
        target=tmp_path / "c.png",
        chart_type="bar",
    )
    assert info["plotted_rows"] == 3
    assert (tmp_path / "c.png").exists()


def test_render_refuses_a_result_with_no_rows(tmp_path):
    with pytest.raises(charts.ChartError, match="no rows"):
        charts.render(columns=["a", "b"], rows=[], target=tmp_path / "c.png")


def test_number_formatting_stays_readable_at_every_magnitude():
    assert charts.format_number(0) == "0"
    assert charts.format_number(1234) == "1,234"
    assert charts.format_number(1234.5) == "1,234"
    assert charts.format_number(12.75) == "12.75"
    assert charts.format_number(2_500_000) == "2.5M"
    assert charts.format_number(4_000_000_000) == "4B"


# --------------------------------------------------------------------------
# Over the wire
# --------------------------------------------------------------------------

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
                {"query_result_id": result_id, "chart_type": "barh",
                 "intent": "revenue by product", "title": "Revenue by product"},
            )
            assert rendered.is_error is False
            body = rendered.structured_content
            assert body["chart"]["chart_type"] == "barh"
            assert body["query_result_id"] == result_id
            assert Path(body["chart"]["absolute_path"]).exists()
            assert any(block.type == "image" for block in rendered.content)

            rejected = await client.call_tool(
                "visualize_data", {"query_result_id": "qr_bogus"}
            )
            assert rejected.is_error is True
            assert "unknown query_result_id" in rejected.content[0].text

    anyio.run(scenario)


# --------------------------------------------------------------------------
# Regressions
# --------------------------------------------------------------------------

def bar_columns(path: Path, theme_surface: tuple[int, int, int] = (252, 252, 251)) -> set[int]:
    """Which x columns carry a mark, across the middle band of the plot."""
    from PIL import Image

    image = Image.open(path).convert("RGB")
    width, height = image.size
    pixels = image.load()
    return {
        x
        for x in range(width)
        for y in range(height // 3, height // 2)
        if pixels[x, y] != theme_surface
    }


def test_highlighting_recolours_bars_without_moving_them(project):
    """Emphasis once split one series into two lanes, shifting every bar."""
    result_id = revenue_by_channel(project)
    plain = Path(chart_of(server_module.visualize_data(result_id))["absolute_path"])
    lit = Path(
        chart_of(server_module.visualize_data(result_id, highlight="email"))["absolute_path"]
    )

    # Same geometry, different colours: the bars occupy exactly the same columns.
    assert bar_columns(lit) == bar_columns(plain)
    assert plain.read_bytes() != lit.read_bytes()


def test_a_stack_keeps_its_total_when_the_top_segment_is_missing():
    from tabulite_mcp.charts import _Series

    series = [
        _Series("email", [10.0, 15.0, 12.0], "#000"),
        _Series("retail", [20.0, 25.0, 18.0], "#111"),
        _Series("organic", [30.0, 5.0, float("nan")], "#222"),
    ]
    assert charts.stack_totals(series, 3) == [60.0, 45.0, 30.0]


def test_a_stack_with_nothing_in_it_gets_no_total():
    from tabulite_mcp.charts import _Series

    nan = float("nan")
    assert charts.stack_totals([_Series("a", [nan, 2.0], "#000")], 2) == [None, 2.0]
