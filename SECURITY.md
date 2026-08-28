# Security Policy

## Reporting a vulnerability

**Please do not open a public issue, pull request or discussion for a security
vulnerability.**

Report it privately through GitHub's
[private vulnerability reporting](https://github.com/davidmrguo/tabulite-mcp/security/advisories/new).
That opens a private advisory visible only to the maintainers, where a fix can be
prepared before anything is public.

If you have no GitHub account, the maintainer's contact address on their GitHub
profile is fine.

Expect an initial response within about a week. This is a small project
maintained in spare time, so please allow reasonable time for a fix before
disclosing publicly.

## Supported versions

The project is pre-1.0 and alpha. Only the latest commit on `main` is supported;
fixes are not backported.

## What is in scope

This server executes SQL and touches the filesystem on behalf of an AI client,
so the interesting surface is the boundary between "analysis" and "action":

- **Read-only enforcement bypass** — any input that causes a write, schema change,
  `ATTACH`/`DETACH`, `PRAGMA`, transaction control, or extension load to execute
  through `query_sql()` or `export_query()`.
- **Path containment escape** — reading outside `source/` or writing outside
  `workspace/exports/`, via traversal, absolute paths, symlinks, or filename
  sanitization failures.
- **Delete confirmation bypass** — destroying a table through `delete_table()`
  without the two-step confirmation.
- **Resource exhaustion** — an import or query that exhausts memory or disk in a
  way the streaming design is meant to prevent.

Bypasses of the SQL scrubber alone, where the authorizer still correctly denies
the operation, are worth reporting but are defense-in-depth issues rather than
vulnerabilities.

## What is not in scope

- **No authentication, by design.** The server binds to `127.0.0.1` and is meant
  for a client on the same machine. "It is unauthenticated" is a documented
  property, not a vulnerability. Exposing it to a network is outside the
  supported configuration.
- **An AI client issuing a legitimate but unwise query.** The server enforces
  read-only access; it does not judge intent.
- Vulnerabilities in dependencies, which belong upstream — though a heads-up is
  appreciated if this project's usage makes one exploitable.
