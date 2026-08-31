"""Local MCP server that imports CSV files into SQLite and queries them safely.

The reasoning layer lives in the desktop AI client. This package only provides
deterministic tools: discover, import, profile, query (read-only), export and
visualize — the last of which charts a query result, never a table directly.
"""

__version__ = "0.1.0"
