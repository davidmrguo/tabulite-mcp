"""Stable references to query results the AI client already has.

``query_sql`` hands its rows straight to the client, so anything that wants to
talk *about* a result afterwards — ``visualize_data`` today — needs a way to
name one. This registry is that name and nothing more: it records the shape of
each result (the SQL, the columns, how many rows came back) and hands out an
id, but never the rows themselves. The client is already holding those, and
keeping a second copy here would be a result cache with all the memory and
staleness problems that implies.

Process-local and bounded: only the most recent results stay referenceable and
a restart forgets all of them, which is the safe direction to fail — a stale
reference produces a clear error rather than a chart of the wrong data.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

# How many recent results stay referenceable. Generous for a conversation,
# small enough that the bookkeeping stays negligible.
DEFAULT_MAX_ENTRIES = 32


class QueryResultError(Exception):
    """Raised when a query_result_id is unknown or no longer retained."""


@dataclass(frozen=True)
class QueryResultRef:
    """What is remembered about one executed query. No rows."""

    result_id: str
    tool: str
    sql: str
    columns: tuple[str, ...]
    returned_rows: int
    truncated: bool
    row_limit: int
    created_at: float


class QueryResultRegistry:
    """In-memory, capacity-bounded record of recent query results."""

    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._max_entries = max_entries
        self._results: OrderedDict[str, QueryResultRef] = OrderedDict()
        self._lock = threading.Lock()

    def record(self, sql: str, result: dict[str, Any], tool: str = "query_sql") -> str:
        """Remember the shape of a result and return its id.

        Takes the dict ``database.execute_query`` produced, so callers stay a
        one-liner and the recorded shape cannot drift from the returned one.
        """
        result_id = f"qr_{secrets.token_hex(4)}"
        ref = QueryResultRef(
            result_id=result_id,
            tool=tool,
            sql=sql,
            columns=tuple(result.get("columns") or ()),
            returned_rows=int(result.get("returned_rows", 0)),
            truncated=bool(result.get("truncated", False)),
            row_limit=int(result.get("row_limit", 0)),
            created_at=time.time(),
        )
        with self._lock:
            self._results[result_id] = ref
            while len(self._results) > self._max_entries:
                self._results.popitem(last=False)  # oldest out first
        return result_id

    def get(self, result_id: str | None) -> QueryResultRef:
        """Look up a recorded result, or raise :class:`QueryResultError`."""
        with self._lock:
            ref = self._results.get(result_id) if result_id else None
            if ref is not None:
                return ref
            latest = next(reversed(self._results), None)

        if not result_id:
            raise QueryResultError(
                "query_result_id is required: run query_sql() first and pass the "
                "query_result_id it returns"
            )
        hint = (
            f"; the most recent one is {latest!r}"
            if latest
            else "; no query results have been recorded on this server yet"
        )
        raise QueryResultError(
            f"unknown query_result_id {result_id!r} — it was never issued, or it has "
            f"been superseded by more recent results or lost to a server restart{hint}. "
            "Re-run the query with query_sql() and use the id it returns"
        )

    def latest_id(self) -> str | None:
        with self._lock:
            return next(reversed(self._results), None)

    def count(self) -> int:
        with self._lock:
            return len(self._results)

    def clear(self) -> None:
        with self._lock:
            self._results.clear()
