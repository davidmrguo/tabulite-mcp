"""Column profiling.

Storage is TEXT for everything; profiling separately works out what each
column appears to *mean*. The result is evidence handed to the AI — the stored
data is never rewritten because of an inferred type.

One streaming scan of the table classifies every value. Distinct values are
memoized, which makes the scan cheap on the low-cardinality columns typical of
CSV exports.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .casting import try_boolean, try_date, try_datetime, try_integer, try_real
from .config import Config
from .database import iter_batches, quote, table_columns

SCAN_BATCH = 10_000
DISTINCT_TRACK_LIMIT = 50_000
VALUE_CACHE_LIMIT = 50_000
FAIL_EXAMPLE_LIMIT = 20

_CHECKS = {
    "INTEGER": try_integer,
    "REAL": try_real,
    "DATE": try_date,
    "DATETIME": try_datetime,
    "BOOLEAN": try_boolean,
}

RECOMMENDED_CAST = {
    "INTEGER": "TRY_INTEGER",
    "REAL": "TRY_REAL",
    "DATE": "TRY_DATE",
    "DATETIME": "TRY_DATETIME",
    "BOOLEAN": "TRY_BOOLEAN",
    "TEXT": "none",
}

# Checked in this order; the first type meeting the confidence threshold wins.
_INFERENCE_ORDER = ("INTEGER", "REAL", "DATE", "DATETIME", "BOOLEAN")


def classify(value: str) -> frozenset[str]:
    """Return the set of logical types a raw value could be."""
    return frozenset(name for name, check in _CHECKS.items() if check(value) is not None)


@dataclass
class _ColumnAccumulator:
    """Running counts for one column during the scan."""

    name: str
    storage_type: str
    row_count: int = 0
    null_count: int = 0
    valid: dict[str, int] = field(default_factory=lambda: {k: 0 for k in _CHECKS})
    distinct: set[str] = field(default_factory=set)
    distinct_overflow: bool = False
    samples: list[str] = field(default_factory=list)
    failures: dict[str, list[str]] = field(default_factory=lambda: {k: [] for k in _CHECKS})
    _cache: dict[str, frozenset[str]] = field(default_factory=dict)

    def add(self, value: Any) -> None:
        self.row_count += 1
        if value is None:
            self.null_count += 1
            return

        text = value if isinstance(value, str) else str(value)

        types = self._cache.get(text)
        if types is None:
            types = classify(text)
            if len(self._cache) < VALUE_CACHE_LIMIT:
                self._cache[text] = types

        for name in _CHECKS:
            if name in types:
                self.valid[name] += 1
            elif len(self.failures[name]) < FAIL_EXAMPLE_LIMIT and text not in self.failures[name]:
                self.failures[name].append(text)

        if not self.distinct_overflow:
            if text not in self.distinct:
                if len(self.distinct) >= DISTINCT_TRACK_LIMIT:
                    self.distinct_overflow = True
                else:
                    self.distinct.add(text)
                    if len(self.samples) < 20:
                        self.samples.append(text)

    def build(self, source_id: str, threshold: float, sample_limit: int,
              invalid_limit: int) -> dict[str, Any]:
        non_null = self.row_count - self.null_count
        logical_type = "TEXT"
        confidence = 1.0

        if non_null:
            for candidate in _INFERENCE_ORDER:
                ratio = self.valid[candidate] / non_null
                if ratio >= threshold:
                    logical_type = candidate
                    confidence = ratio
                    break

        invalid_count = 0 if logical_type == "TEXT" else non_null - self.valid[logical_type]
        invalid_examples = [] if logical_type == "TEXT" else self.failures[logical_type][:invalid_limit]

        return {
            "source_id": source_id,
            "column_name": self.name,
            "storage_type": self.storage_type,
            "logical_type": logical_type,
            "type_confidence": round(confidence, 6),
            "row_count": self.row_count,
            "null_count": self.null_count,
            "non_null_count": non_null,
            "valid_integer_count": self.valid["INTEGER"],
            "valid_real_count": self.valid["REAL"],
            "valid_date_count": self.valid["DATE"],
            "valid_datetime_count": self.valid["DATETIME"],
            "valid_boolean_count": self.valid["BOOLEAN"],
            "invalid_count": invalid_count,
            # Exact below the tracking limit; a floor value beyond it.
            "distinct_count": len(self.distinct),
            "distinct_count_approximate": 1 if self.distinct_overflow else 0,
            "sample_values": self.samples[:sample_limit],
            "invalid_examples": invalid_examples,
            "recommended_cast": RECOMMENDED_CAST[logical_type],
        }


def profile_table(
    conn: sqlite3.Connection,
    table_name: str,
    source_id: str,
    config: Config,
) -> list[dict[str, Any]]:
    """Scan a table once and return one profile dict per column."""
    columns = table_columns(conn, table_name)
    if not columns:
        raise ValueError(f"table not found or has no columns: {table_name}")

    accumulators = [_ColumnAccumulator(name=name, storage_type=declared)
                    for name, declared in columns]

    select_list = ", ".join(quote(name) for name, _ in columns)
    cursor = conn.execute(f"SELECT {select_list} FROM {quote(table_name)}")
    for batch in iter_batches(cursor, SCAN_BATCH):
        for row in batch:
            for accumulator, value in zip(accumulators, row):
                accumulator.add(value)

    return [
        acc.build(
            source_id,
            config.type_confidence_threshold,
            config.profile_sample_values,
            config.profile_invalid_examples,
        )
        for acc in accumulators
    ]
