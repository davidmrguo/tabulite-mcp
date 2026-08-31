# Tabulite MCP

[![CI](https://github.com/davidmrguo/tabulite-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/davidmrguo/tabulite-mcp/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Analyze CSV files that are too big for a spreadsheet — and too big to paste
into a chat — by giving your AI assistant a local SQLite runtime instead of the
data.**

Tabulite MCP — Tabulite for short — is a local
[MCP](https://modelcontextprotocol.io) server. Point it at a folder of CSV
files, and your desktop AI client can import them into SQLite, inspect what the
columns actually contain, answer questions by writing SQL, and chart the
answers with matplotlib — without a single row of your data leaving your
machine or entering the conversation.

```
Desktop AI client  →  MCP  →  Tabulite ─┬→  sqlite3     →  your CSV files
   (the reasoning)                      └→  matplotlib  →  workspace/charts/
                             (safe, deterministic tools)
```

Query first, then chart: matplotlib is only ever handed a result `sqlite3` just
produced, never numbers the model supplied.

There is no LLM inside the server. Your AI client does the thinking; Tabulite
gives it metadata to think about, a read-only SQL interface to explore with, a
plotting library to draw the answer with, and a direct path to disk when the
answer is a dataset rather than a sentence.

---

## Why

Ask an AI about a 500 MB CSV and you have bad options: paste a sample and lose
the answer, upload the whole thing and burn your context window (and send your
data somewhere), or go write a script yourself.

A single machine and a single SQLite file handle this size without breaking a
sweat. Tabulite puts that runtime next to the data and exposes it over MCP. Your
assistant reads a few hundred tokens of column profiles, writes the SQL, and
gets back aggregates. The rows stay on disk.

**Good fit:** one-off analysis of CSV exports, log dumps and extracts on your own
laptop — files that outgrew Excel but still belong on one machine.
**Not a fit:** production pipelines, scheduled ETL, multi-user access, or
anything that belongs in a real data warehouse.

---

## Quickstart

**Requirements:** [Docker Desktop](https://www.docker.com/products/docker-desktop/)
(or Docker Engine + Compose). Nothing else — no Python setup needed.

```bash
git clone https://github.com/davidmrguo/tabulite-mcp.git
cd tabulite-mcp
docker compose up --build
```

The server is now on `http://localhost:8000/mcp`, with a health check at
`http://localhost:8000/health`.

Two small sample CSVs (`source/sales.csv`, `source/customers.csv`) ship with the
repo so you can try it immediately. Connect your AI client (below), then ask:

> *"Analyze sales.csv. Which channel generated the most revenue?"*

Your assistant will call `list_sources()`, `import_source("sales.csv")`,
`profile_table("sales")`, and then write something like:

```sql
SELECT channel,
       SUM(TRY_REAL(revenue)) AS revenue,
       COUNT(TRY_REAL(revenue)) AS valid_rows,
       COUNT(*) AS total_rows
FROM sales
GROUP BY channel
ORDER BY revenue DESC;
```

Ask for a picture of that and it doesn't re-do the work — it charts the result
it already has:

> *"Chart that."*

```python
visualize_data(query_result_id="qr_9f3c1ab2", chart_type="bar",
               title="Revenue by channel")
```

A 600px PNG comes back inline and lands in `workspace/charts/`. "Make it
horizontal and highlight email" is another call with `chart_type="barh"` and
`highlight="email"` — not a redrawn picture.

### Connect your AI client

**Claude Code**

```bash
claude mcp add --transport http tabulite http://localhost:8000/mcp
```

**Any client with a JSON config** (Claude Desktop, Cursor, and similar):

```json
{
  "mcpServers": {
    "tabulite": {
      "type": "http",
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

**Clients that only speak stdio:** put a bridge such as
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote) in front of the URL.

### Use your own data

Drop CSV files into `source/` — that's it, no restart needed:

```bash
cp ~/Downloads/huge_export.csv source/
```

Your files are mounted **read-only** and are gitignored, so they never get
committed and the server can never modify them. Everything Tabulite creates
(databases, exports) lands in `workspace/`.

---

## The tools your AI gets

| Tool | What it does |
|---|---|
| `list_sources()` | CSV files under `source/`, with sizes and import status |
| `inspect_source(path)` | columns, delimiter and a few sample rows — without importing |
| `import_source(path, table_name?, delimiter?, force?)` | stream a CSV into SQLite and profile it |
| `list_tables()` | imported tables with row counts and where they came from |
| `profile_table(table_name, refresh?)` | compact profile of every column |
| `profile_column(table_name, column_name)` | full detail for one column, with examples |
| `sample_table(table_name, limit=20)` | a few rows, to see what the data looks like |
| `query_sql(sql)` | read-only analytical SQL (capped at 1,000 rows), with a `query_result_id` |
| `export_query(sql, file_name?, format="csv")` | complete result streamed to a file |
| `visualize_data(query_result_id, chart_type?, x?, y?, ...)` | draw a chart of a query result with matplotlib — see below |
| `delete_table(table_name, confirm?, confirmation_token?)` | permanently remove an imported table — two-step, see below |

Notably absent: anything domain-specific. There is no `top_products()` or
`calculate_revenue()`. Your assistant writes the SQL, which is the whole point —
it can answer questions nobody anticipated.

---

## How it works

### CSV fields are stored as TEXT, on purpose

Every imported column is `TEXT`:

```sql
CREATE TABLE sales (
    transaction_id   TEXT,
    transaction_date TEXT,
    revenue          TEXT,
    quantity         TEXT
);
```

Guessing types at import time destroys data before anyone has looked at it:
`"1,234"` becomes `1`, a leading-zero product code becomes an integer,
`"2025-13-40"` silently becomes NULL. So storage keeps what the file said, and
interpretation happens later, where it is visible and reversible.

### Profiles tell the AI what the columns *mean*

After import, every column is profiled and the result is stored in
`workspace/catalog.sqlite`. Here is the real output for the bundled sample:

```
column             logical_type  confidence  nulls  invalid  recommended_cast
transaction_id     TEXT          1.000       0      0        none
transaction_date   DATE          1.000       0      0        TRY_DATE
customer           TEXT          1.000       0      0        none
product            TEXT          1.000       0      0        none
channel            TEXT          1.000       9      0        none
quantity           INTEGER       0.996       0      2        TRY_INTEGER
revenue            REAL          0.996       36     2        TRY_REAL
```

`profile_column("sales", "revenue")` goes further and shows the actual offenders:
`invalid_examples: ["pending", "unknown"]`.

Inference is conservative — a type is assigned only when ≥99% of non-null values
parse as it. Profiles are *evidence for the AI*, never an instruction to the
storage layer: your imported data is never rewritten to match a guess.

### `TRY_*` functions instead of `CAST`

SQLite's `CAST` is dangerously permissive:

```sql
CAST('unknown' AS REAL)    -- 0.0   ← quietly wrong
CAST('12 apples' AS REAL)  -- 12.0  ← quietly wrong
```

An `AVG()` over a column with a few thousand `'unknown'` values silently
averages in zeros. So Tabulite registers strict conversions on every connection:

```sql
TRY_REAL('125.5')    -- 125.5
TRY_REAL('')         -- NULL
TRY_REAL('unknown')  -- NULL
```

Also available: `TRY_INTEGER`, `TRY_DATE`, `TRY_DATETIME`, `TRY_BOOLEAN`.
Because SQLite's aggregates skip NULL, bad values are *excluded* rather than
counted as zero — and your assistant can check the denominator:

```sql
SELECT AVG(TRY_REAL(revenue)) AS average_revenue,
       COUNT(TRY_REAL(revenue)) AS valid_rows,   -- 462
       COUNT(*)                 AS total_rows    -- 500
FROM sales;
```

### Missing and invalid data stay distinguishable

Only configured missing-value markers become SQL `NULL`. Values that merely fail
to parse are kept exactly as written:

| CSV value | Stored as |
|---|---|
| `125.40` | `"125.40"` |
| *(empty)* | `NULL` |
| `N/A` | `NULL` |
| `unknown` | `"unknown"` |
| `-` | `"-"` |

Default markers: empty string, `NULL`, `null`, `N/A`, `NA`. "This field was
blank" and "this field contained garbage" are different findings, and collapsing
them at import time would hide a data-quality problem worth seeing.

### Files are identified by content, not by name

Rename `sales.csv` to `sales_FINAL_v2.csv`, import it again, and Tabulite
recognizes the content and reuses the existing table instead of duplicating it.
Identity is the SHA-256 of the file, computed *during* the import pass rather
than in a separate read. Change one byte and it becomes a new source with its
own table.

Everything imported lives in one database (`workspace/databases/main.sqlite`) so
your assistant can join across files with ordinary SQL. When two files would
claim the same table name — say a `sales.csv` in two different folders — the
second gets a suffix from its own content hash (`sales` and `sales_4b11d3`),
which means a given file always lands on the same table name regardless of
import order.

### Big results go to disk, not into the chat

`query_sql()` returns at most 1,000 rows and always says so
(`"truncated": true`), which is a nudge to aggregate in SQL rather than paginate
a large result into the conversation. Pathological queries — an accidental
Cartesian product, an unbounded recursive CTE — are canceled after a timeout.

When the user actually wants the rows, `export_query()` runs the same read-only
SQL with **no row cap** and streams the cursor straight into a file under
`workspace/exports/`:

> *"Give me all email transactions from 2025 over $1,000 and export them."*

Your assistant builds the query, calls `export_query()`, and hands back the
path — this is the real result on the bundled sample data:

```json
{"file_name": "email_2025_high_value.csv",
 "relative_path": "exports/email_2025_high_value.csv",
 "row_count": 58, "file_size_bytes": 3084}
```

Neither the server nor the conversation ever holds the whole result, so this
works the same way at 58 rows or 5 million.

### Charts are drawn here, by matplotlib

The same bargain as the SQL layer, one level up. Tabulite doesn't ask an AI to
*imagine* a chart; it hands the AI a mature plotting library and lets it drive,
exactly the way it hands over `sqlite3` instead of trying to answer questions
about your data itself. Your assistant decides *what* to draw; matplotlib
decides how a bar chart works.

`query_sql()` returns a `query_result_id` alongside the rows:

```json
{"columns": ["month", "revenue"], "rows": [["2025-01", 125.4], ...],
 "returned_rows": 12, "truncated": false, "query_result_id": "qr_9f3c1ab2"}
```

Pass that id to `visualize_data()` and you get a PNG back — as an image your
client can show inline, and as a file on disk:

```json
{"status": "rendered", "renderer": "matplotlib",
 "query_result_id": "qr_9f3c1ab2", "intent": "revenue trend by month",
 "chart": {"file_name": "line_20260831T161204_a3f9c1.png",
           "relative_path": "charts/line_20260831T161204_a3f9c1.png",
           "chart_type": "line", "width_px": 600, "height_px": 360,
           "x_column": "month", "y_columns": ["revenue"],
           "plotted_rows": 12, "skipped_values": 0},
 "source_result": {"sql": "SELECT ...", "returned_rows": 12}}
```

Charts land in `workspace/charts/`, next to your exports, and an existing file
is never overwritten.

**The data is not an argument.** There is no `rows` or `values` parameter to
pass numbers into — `visualize_data()` takes an id and re-runs *that exact
recorded statement* to get what it plots. A chart of numbers a model
remembered, inferred from a profile, or read out of a sample isn't refused so
much as unreachable: there is nowhere to put them. If you want a different
picture, run a different query and visualize that result.

**Changing the chart is an argument, not a re-roll.** Ask for it wider, or
green, or horizontal, or with the values printed on the bars, and your
assistant calls the tool again with `width_px=`, `colors=`, `chart_type="barh"`
or `value_labels=True`. Nothing is regenerated and nothing drifts — the same
result and the same arguments always produce the same PNG, byte for byte. The
knobs are `chart_type` (bar, barh, line, area, scatter, pie), `x` / `y`,
`title`, `x_label`, `y_label`, `series_labels`, `width_px`, `height_px`,
`theme`, `colors`, `highlight`, `legend`, `grid`, `stacked`, `value_labels` and
`file_name`.

Everything you *don't* pass is decided once, here, rather than improvised per
chart:

- **600px wide by default**, with the height that suits the form — a horizontal
  bar chart grows with the number of bars, so its labels never crush together.
- **A fixed eight-colour palette**, in a fixed order, checked for colour-vision
  separation against both the light and the dark surface. It is never cycled
  and a ninth hue is never invented: a ninth series is an error telling the
  model to group the tail into an "Other" bucket in SQL instead.
- **Thin marks, hairline gridlines, one baseline, a legend only when there are
  two or more series.** Axis labels are *measured* and then rotated or thinned
  until they clear each other, so you never get `2025-012025-02` along the
  bottom.
- **`highlight=` for emphasis** — the named categories take the accent colour
  and the rest recede to grey, which is usually what "make X stand out" should
  mean.

**Values that aren't numbers are dropped, not zeroed.** The renderer converts
with the same `TRY_REAL` rules the SQL layer uses, so the picture and the
aggregate agree about what counts as a number. A row whose value won't convert
gets no bar — never a bar of height zero — and the count comes back in
`skipped_values` so your assistant can say so out loud. A group that is
entirely missing shows as `(null)` rather than as an unlabelled bar.

**No chart without a query.** A `query_result_id` is minted in exactly one
place — inside `query_sql()`, after the SQL has passed validation and actually
run. Nothing else issues one: not `sample_table()`, not `export_query()`, not a
profile, and not a query that errored or was rejected as unsafe. Ids are not
guessable, and `visualize_data()` has no "use the latest result" fallback. The
server keeps only the *shape* of recent results — the SQL, the column names,
the row count — never the rows, and only the 32 most recent, so a stale id
comes back as a clear error telling the assistant to re-run the query rather
than silently charting the wrong data. An empty result is refused outright, and
so is a result whose table has since been deleted.

What that does *not* do is stop a client from drawing a chart without calling
this tool at all — from a `sample_table()` peek, from a profile, or from
numbers it remembers. No MCP server can prevent that; it has no say in what the
client renders. So the guarantee is the same shape as the one on
`delete_table()`: the sanctioned path is airtight and cheap to follow, and
skipping it is conspicuous. Past that boundary the tool description and the
server instructions do the work, and what they ask for is narrow on purpose —
anything presented *as this project's data* comes from a query result. If you
ask your assistant outright to sketch something, chart figures you typed in
yourself, or draw a diagram, that is between you and it. Tabulite has no
business policing what you asked for; it only refuses to dress up invented
numbers as an answer from your data.

### Deleting a table

Imports are cheap to redo but expensive to lose, so `delete_table()` is
deliberately two-step. Ask your assistant to delete a table and the first call
deletes nothing — it returns a warning saying exactly what would go (row count,
columns, profiles) and, importantly, whether the original CSV is still in
`source/` to re-import from:

> This will permanently delete the table 'sales' (500 rows, 7 columns) along
> with its column profiles and its entry in the catalog. The source file
> sales.csv is still in source/, so the table could be rebuilt with
> import_source() afterwards.

**You then have to type `DELETE` in capitals.** Nothing else counts — not
"yes", not "go ahead", not lowercase "delete". Your assistant passes that word
back along with a single-use token from the warning, and only then is the table
dropped, its profiles and catalog entry removed, and the database compacted so
the disk space actually comes back.

The two-step design is enforced by the server, not by the model's good
manners: no single call can delete anything, because the token only exists
once a warning has been issued. What a server cannot verify is that a human
typed the word rather than the model — so treat the warning in your chat as
the real checkpoint. Your CSV in `source/` and anything already written to
`workspace/exports/` are never touched.

---

## Safety

**Your CSVs are never modified.** `source/` is mounted read-only at the Docker
level. Everything written goes to `workspace/`.

**Every AI-generated query is read-only**, enforced in four layers:

1. the connection is opened `file:…?mode=ro`, so the OS holds the file read-only;
2. `PRAGMA query_only=ON` makes SQLite itself refuse writes on that handle;
3. extension loading is disabled explicitly;
4. a `set_authorizer()` callback allows only `SQLITE_SELECT`, `SQLITE_READ`,
   `SQLITE_FUNCTION` (minus filesystem-reaching builtins) and
   `SQLITE_RECURSIVE`, denying everything else — writes, schema changes,
   `ATTACH`/`DETACH`, every `PRAGMA`, transaction control, maintenance.

Layer 4 is the real mechanism: it runs inside SQLite during statement
preparation, so it judges what a query *does*, not how its text is spelled. A
SQL scrubber sits in front of it as defense in depth and to give the model a
readable error (`only read-only statements are allowed; found 'DROP'`) instead
of a bare `not authorized`.

This distinction cuts both ways, and the test suite pins it: `CASE … END` and the
`replace()` scalar function are ordinary analytical SQL and keep working, while
`REPLACE INTO`, `PRAGMA writable_schema = ON` and `load_extension()` are rejected.

**Paths are contained.** The server only reads inside `source/` and only writes
inside `workspace/exports/`. Traversal (`../`), absolute paths and symlinks
pointing outside the project are rejected; export filenames are sanitized and an
existing export is never overwritten.

**No authentication** — by design. The container publishes to `127.0.0.1` only
and is meant for a client on the same machine. Don't expose it to a network.

---

## Configuration

All optional; set them in `compose.yaml`.

| Variable | Default | What it controls |
|---|---|---|
| `TABULITE_SOURCE_DIR` | `/project/source` | read-only source directory |
| `TABULITE_WORKSPACE_DIR` | `/project/workspace` | writable workspace |
| `TABULITE_NULL_MARKERS` | `,NULL,null,N/A,NA` | values imported as SQL NULL |
| `TABULITE_MAX_QUERY_ROWS` | `1000` | interactive row cap |
| `TABULITE_QUERY_TIMEOUT` | `30` | seconds before a query is canceled |
| `TABULITE_EXPORT_TIMEOUT` | `600` | seconds before an export is canceled |
| `TABULITE_BATCH_SIZE` | `5000` | rows per `executemany()` during import |
| `TABULITE_CHART_WIDTH` | `600` | default chart width in pixels |
| `TABULITE_HOST` / `TABULITE_PORT` | `0.0.0.0` / `8000` | bind address inside the container |
| `TABULITE_ALLOWED_ORIGINS` | localhost origins | Origin allow-list (DNS-rebinding protection) |

---

## Project layout

```
tabulite-mcp/
├── source/                  # your CSV files (read-only mount, gitignored)
├── workspace/               # everything generated (gitignored)
│   ├── catalog.sqlite       #   source, import, profile and export metadata
│   ├── databases/main.sqlite#   the imported analytical tables
│   ├── exports/             #   query results written to disk
│   └── charts/              #   rendered PNGs
├── src/tabulite_mcp/
│   ├── server.py            # the MCP tools
│   ├── config.py            # paths and limits
│   ├── confirm.py           # two-step confirmation for destructive tools
│   ├── results.py           # references to recent query results
│   ├── security.py          # path containment + read-only enforcement
│   ├── database.py          # connections, row caps, cancellation
│   ├── importer.py          # streaming CSV → SQLite
│   ├── profiler.py          # logical type inference
│   ├── casting.py           # TRY_* functions
│   ├── catalog.py           # catalog.sqlite
│   ├── exporter.py          # streaming results to files
│   └── charts.py            # matplotlib rendering
├── tests/
├── Dockerfile
└── compose.yaml
```

---

## Development

Run it without Docker:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
TABULITE_SOURCE_DIR=./source TABULITE_WORKSPACE_DIR=./workspace tabulite-mcp
```

Run the tests:

```bash
pytest
```

276 tests cover source discovery and traversal rejection, streamed import, NULL
vs invalid handling, SHA-256 identity (including renamed and modified files),
deterministic table naming, profiling and type inference, the `TRY_*` functions,
`AVG` ignoring invalid values, SELECT/GROUP BY/CTE/join/window queries, result
limits, query cancellation, read-only enforcement at both the scrubber and
authorizer layers, CSV and JSON export, export streaming, filename sanitization,
chart rendering (pixel dimensions, byte-for-byte determinism, dropped non-numeric
values, the palette's limits), the two-step delete confirmation, and tool
invocation over a real in-process MCP session.

**Stack:** Python 3.11+, the standard library's `sqlite3`, the official MCP
Python SDK pinned at `mcp==2.1.1` (v2 API: `MCPServer`, host/port on `run()`),
and matplotlib for chart rendering — which brings NumPy with it, the only
transitive weight in the tree. No pandas, no DataFrame layer, no ORM: the data
path is recognizably ordinary Python — `sqlite3.connect()`,
`conn.executemany()`, `conn.create_function()`, `cursor.fetchmany()` — and
NumPy is matplotlib's business, not something rows are ever loaded into.

**Scale:** a 133 MB / 2,000,000-row CSV imports and profiles in about two
minutes with container memory flat around 100 MB; aggregating over it takes a
couple of seconds. Import is bounded by disk, not RAM.

---

## Troubleshooting

**Port 8000 already in use** — change the host side of the mapping in
`compose.yaml` (`"127.0.0.1:8001:8000"`) and point your client at the new port.

**Client can't connect** — check the server is up with
`curl http://localhost:8000/health`, then `docker compose logs -f`.

**Permission errors writing to `workspace/` (Linux)** — uncomment the `user:`
line in `compose.yaml` so files are created as you rather than as the container
user.

**A file in `source/` isn't listed** — only `.csv` and `.tsv` are discovered, and
dotfiles are skipped.

**"unknown table" after editing a CSV** — changing a file changes its hash, so
re-run `import_source()`; the new content gets its own table.

---

## Not in scope

No embedded LLM, no natural-language-to-SQL in the server, no arbitrary Python
execution, no pandas or DataFrame layer, no dashboards or BI tool (charting
stops at one matplotlib figure per query result), no Excel, DuckDB, Polars or
Parquet, no embeddings or vector search, no cloud deployment, authentication,
multi-user support or background jobs. Your AI client is already the interface
and the reasoning layer.

## Contributing

Bug reports, questions and pull requests are all welcome.

- **Something broken?** [Open an issue](https://github.com/davidmrguo/tabulite-mcp/issues/new/choose).
- **A question, or an idea you want to talk through?**
  [Discussions](https://github.com/davidmrguo/tabulite-mcp/discussions) is the
  place for anything open-ended.
- **Want to send code?** Read [CONTRIBUTING.md](CONTRIBUTING.md) first —
  especially the scope section, which will tell you quickly whether an idea
  fits before you write it.
- **Found a security problem?** Please don't open a public issue.
  [SECURITY.md](SECURITY.md) explains how to report it privately.

Everyone taking part is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## License

[MIT](LICENSE) — do what you like with it, keep the notice.

Contributions are accepted under the same license (no CLA, no
copyright assignment). Copyright stays with the people who wrote the code,
which is deliberate: this is meant to stay an open source project rather than
become someone's product.
