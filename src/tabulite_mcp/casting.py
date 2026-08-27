"""Safe query-time conversions.

SQLite's own ``CAST`` is lenient: ``CAST('12 apples' AS REAL)`` is ``12.0`` and
``CAST('unknown' AS REAL)`` is ``0.0``, which quietly corrupts averages. These
functions convert only values that are unambiguously of the requested type and
return NULL for everything else, so SQLite's aggregates skip them instead of
counting them as zero.

They are registered on each connection with ``Connection.create_function()``.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime

_INTEGER_RE = re.compile(r"^[+-]?\d+$")
_REAL_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")

_TRUE_TOKENS = {"true", "t", "yes", "y", "1"}
_FALSE_TOKENS = {"false", "f", "no", "n", "0"}

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d")
_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
    "%Y/%m/%d %H:%M:%S",
)


def _text(value: object) -> str | None:
    """Normalize an incoming SQLite value to a trimmed string, or None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def try_integer(value: object) -> int | None:
    """``'42'`` -> 42; ``''``, ``'unknown'``, ``'4.2'`` -> None."""
    text = _text(value)
    if text is None or not _INTEGER_RE.match(text):
        return None
    return int(text)


def try_real(value: object) -> float | None:
    """``'125.5'`` -> 125.5; ``''``, ``'unknown'`` -> None."""
    text = _text(value)
    if text is None or not _REAL_RE.match(text):
        return None
    return float(text)


def try_date(value: object) -> str | None:
    """Return an ISO ``YYYY-MM-DD`` string, or None."""
    text = _text(value)
    if text is None:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def try_datetime(value: object) -> str | None:
    """Return an ISO ``YYYY-MM-DD HH:MM:SS`` string, or None.

    A date-only value converts to midnight, matching SQLite's ``datetime()``.
    """
    text = _text(value)
    if text is None:
        return None
    candidate = text[:-1] if text.endswith("Z") else text
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(candidate, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def try_boolean(value: object) -> int | None:
    """Return 1, 0 or None."""
    text = _text(value)
    if text is None:
        return None
    lowered = text.lower()
    if lowered in _TRUE_TOKENS:
        return 1
    if lowered in _FALSE_TOKENS:
        return 0
    return None


def is_textual_boolean(value: str) -> bool:
    """True for boolean spellings that are not just 0/1."""
    lowered = value.strip().lower()
    return lowered in (_TRUE_TOKENS | _FALSE_TOKENS) - {"0", "1"}


TRY_FUNCTIONS = {
    "TRY_INTEGER": try_integer,
    "TRY_REAL": try_real,
    "TRY_DATE": try_date,
    "TRY_DATETIME": try_datetime,
    "TRY_BOOLEAN": try_boolean,
}


def register_try_functions(conn: sqlite3.Connection) -> None:
    """Register the TRY_* helpers on a connection.

    ``deterministic=True`` lets SQLite use them in indexed contexts and in
    partial indexes; they are pure functions of their input.
    """
    for name, func in TRY_FUNCTIONS.items():
        conn.create_function(name, 1, func, deterministic=True)
