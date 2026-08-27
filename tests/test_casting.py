"""TRY_* conversion functions, standalone and inside SQL."""

from __future__ import annotations

import sqlite3

import pytest

from tabulite_mcp.casting import (
    register_try_functions,
    try_boolean,
    try_date,
    try_datetime,
    try_integer,
    try_real,
)


@pytest.mark.parametrize(
    "value,expected",
    [("42", 42), ("-7", -7), ("  12  ", 12), ("0", 0),
     ("4.2", None), ("", None), (None, None), ("unknown", None), ("1,000", None)],
)
def test_try_integer(value, expected):
    assert try_integer(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [("125.5", 125.5), ("125", 125.0), ("-0.5", -0.5), ("1e3", 1000.0), (".5", 0.5),
     ("", None), (None, None), ("unknown", None), ("$125.40", None), ("nan", None)],
)
def test_try_real(value, expected):
    assert try_real(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [("2025-01-05", "2025-01-05"), ("2025/01/05", "2025-01-05"),
     ("2025-13-01", None), ("not a date", None), ("", None), (None, None)],
)
def test_try_date(value, expected):
    assert try_date(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [("2025-01-05 13:45:00", "2025-01-05 13:45:00"),
     ("2025-01-05T13:45:00", "2025-01-05 13:45:00"),
     ("2025-01-05T13:45:00Z", "2025-01-05 13:45:00"),
     # A date-only value is midnight, matching SQLite's own datetime().
     ("2025-01-05", "2025-01-05 00:00:00"),
     ("nope", None), (None, None)],
)
def test_try_datetime(value, expected):
    assert try_datetime(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [("true", 1), ("TRUE", 1), ("yes", 1), ("1", 1),
     ("false", 0), ("no", 0), ("0", 0),
     ("maybe", None), ("", None), (None, None)],
)
def test_try_boolean(value, expected):
    assert try_boolean(value) == expected


def test_functions_are_registered_on_the_connection():
    conn = sqlite3.connect(":memory:")
    register_try_functions(conn)
    assert conn.execute("SELECT TRY_REAL('125.5')").fetchone()[0] == 125.5
    assert conn.execute("SELECT TRY_REAL('unknown')").fetchone()[0] is None
    assert conn.execute("SELECT TRY_INTEGER('42')").fetchone()[0] == 42
    assert conn.execute("SELECT TRY_DATE('2025-01-05')").fetchone()[0] == "2025-01-05"
    assert conn.execute("SELECT TRY_BOOLEAN('yes')").fetchone()[0] == 1
    conn.close()


def test_avg_ignores_missing_and_invalid_values(read_only):
    """The point of TRY_REAL: bad values become NULL, not zero."""
    row = read_only.execute(
        """
        SELECT AVG(TRY_REAL(revenue)) AS average_revenue,
               COUNT(TRY_REAL(revenue)) AS valid_rows,
               COUNT(*) AS total_rows
        FROM sales
        """
    ).fetchone()
    average, valid_rows, total_rows = row

    # Fixture revenues: 125.40, NULL, NULL, "unknown", 80.00, 19.99, 240.10, "-"
    assert valid_rows == 4
    assert total_rows == 8
    assert average == pytest.approx((125.40 + 80.00 + 19.99 + 240.10) / 4)

    # Ordinary CAST would have quietly turned "unknown" and "-" into 0.0 and
    # dragged the average down.
    naive = read_only.execute(
        "SELECT AVG(CAST(revenue AS REAL)) FROM sales"
    ).fetchone()[0]
    assert naive < average
