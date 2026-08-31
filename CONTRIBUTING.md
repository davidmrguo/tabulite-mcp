# Contributing

Contributions are welcome. This is a small project with a deliberately narrow
scope, so the most useful thing you can do before writing code is read the
**Scope** section below — it will save you building something that gets
declined for reasons that have nothing to do with the quality of the work.

## Scope

The server imports CSVs into SQLite and exposes read-only SQL to an AI client.
That is the whole job.

**Not in scope**, and not likely to become in scope: an embedded LLM,
natural-language-to-SQL inside the server, arbitrary Python execution, pandas or
a DataFrame layer, dashboards or a BI tool, Excel, DuckDB, Polars or Parquet,
embeddings or vector search, cloud deployment, authentication, multi-user
support, background jobs. Your AI client is already the interface and the
reasoning layer.

Charting is the one thing that moved *into* scope, and it stays narrow:
`visualize_data()` renders one matplotlib figure per query result, with every
visual property as an explicit argument. A second chart type is a reasonable
proposal; a dashboard is not.

There are also no domain-specific tools. `query_sql()` exists so the assistant
can answer questions nobody anticipated; a `top_products()` tool would be a step
away from that, not toward it.

If you are unsure whether an idea fits, **open a discussion or an issue before
writing the code**. A short conversation is cheaper than a rejected PR.

## Getting set up

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Run the server directly:

```bash
TABULITE_SOURCE_DIR=./source TABULITE_WORKSPACE_DIR=./workspace tabulite-mcp
```

## Before you open a pull request

```bash
pytest              # all 276 tests
ruff check src tests
```

Both run in CI against Python 3.11, 3.12 and 3.13. A PR cannot merge until they
pass, so running them locally is faster than finding out from CI.

## How changes land

- Fork the repository, branch from `main`, and open a pull request against `main`.
- `main` does not accept direct pushes. Everything goes through a PR, maintainers included.
- PRs are **squash-merged**, and the PR description becomes the commit message.
  A tidy description is worth more here than tidy intermediate commits.
- A maintainer approval is required before an outside contribution merges.
- Pushing new commits dismisses an existing approval, so expect a second look if
  you revise after review.

The first workflow run on a pull request from a fork needs manual approval from a
maintainer. That is a security setting, not a comment on your change — it just
means CI may sit idle briefly until someone presses the button.

## Tests

New behaviour needs a test. The suite is fast (a couple of seconds) and covers
source discovery and traversal rejection, streamed import, NULL vs invalid
handling, SHA-256 file identity, type inference, the `TRY_*` functions,
read-only enforcement at both the scrubber and authorizer layers, export
streaming and filename sanitization, and the two-step delete confirmation.

If you are touching anything under **Security-sensitive areas** below, a test
that pins the behaviour is not optional.

## Security-sensitive areas

Some files carry more weight than their size suggests:

- `src/tabulite_mcp/security.py` — the SQL scrubber and the SQLite authorizer callback
- `src/tabulite_mcp/database.py` — read-only connection setup
- `src/tabulite_mcp/config.py` and `importer.py` — path containment
- `src/tabulite_mcp/confirm.py` — the two-step delete gate

The read-only guarantee is enforced in four layers and the test suite pins the
distinction between "ordinary analytical SQL" and "a write dressed up as one".
Changes here get a closer read, and please do not weaken a layer because another
one appears to cover it.

**Do not report a security vulnerability in a public issue or PR.** See
[SECURITY.md](SECURITY.md).

## Style

Ruff enforces pyflakes rules and a 120-character line limit; beyond that, match
the surrounding code. The codebase deliberately sticks to ordinary standard-library
Python — `sqlite3.connect()`, `conn.executemany()`, `cursor.fetchmany()` — with no
ORM and no dataframe library. Please keep it that way.

## Licensing

Contributions are accepted under the MIT license. There is no CLA and no
copyright assignment; copyright stays with the people who wrote the code.
