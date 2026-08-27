"""Shared fixtures: a throwaway project directory with source/ and workspace/."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

from tabulite_mcp import catalog, database, importer, profiler
from tabulite_mcp.config import Config

SALES_HEADER = ["transaction_id", "transaction_date", "customer", "product",
                "channel", "quantity", "revenue"]

SALES_ROWS = [
    ["1", "2025-01-05", "CUST-1", "Lamp", "email", "2", "125.40"],
    ["2", "2025-01-06", "CUST-2", "Mug", "retail", "1", ""],
    ["3", "2025-02-11", "CUST-1", "Lamp", "email", "3", "N/A"],
    ["4", "2025-02-12", "CUST-3", "Kettle", "organic", "1", "unknown"],
    ["5", "2025-03-02", "CUST-2", "Mug", "email", "4", "80.00"],
    ["6", "2025-03-09", "CUST-3", "Kettle", "NA", "-", "19.99"],
    ["7", "2025-04-01", "CUST-4", "Lamp", "retail", "2", "240.10"],
    ["8", "2025-04-15", "CUST-4", "Mug", "email", "1", "-"],
]

CUSTOMER_ROWS = [
    ["CUST-1", "North", "2024-01-01", "true"],
    ["CUST-2", "South", "2024-02-01", "false"],
    ["CUST-3", "North", "2024-03-01", "true"],
    ["CUST-4", "West", "2024-04-01", "true"],
]


def write_csv(path: Path, header: list[str], rows: list[list[str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path


@pytest.fixture
def config(tmp_path: Path) -> Config:
    source = tmp_path / "source"
    workspace = tmp_path / "workspace"
    source.mkdir()
    workspace.mkdir()
    cfg = Config(source_dir=source.resolve(), workspace_dir=workspace.resolve())
    cfg.ensure_directories()
    return cfg


@pytest.fixture
def sales_csv(config: Config) -> Path:
    return write_csv(config.source_dir / "sales.csv", SALES_HEADER, SALES_ROWS)


@pytest.fixture
def customers_csv(config: Config) -> Path:
    return write_csv(
        config.source_dir / "customers.csv",
        ["customer", "region", "signup_date", "is_active"],
        CUSTOMER_ROWS,
    )


@pytest.fixture
def conns(config: Config):
    """Writable analytical connection plus the catalog connection."""
    db = database.connect_writable(config.database_path)
    cat = catalog.connect(config.catalog_path)
    try:
        yield db, cat
    finally:
        db.close()
        cat.close()


@pytest.fixture
def imported(config: Config, conns, sales_csv: Path):
    """A project with sales.csv imported and profiled."""
    db, cat = conns
    result = importer.import_csv(sales_csv, config, db, cat)
    profiles = profiler.profile_table(db, result.table_name, result.source_id, config)
    catalog.replace_column_profiles(cat, result.table_name, profiles)
    return result


@pytest.fixture
def read_only(config: Config, imported) -> sqlite3.Connection:
    conn = database.connect_read_only(config.database_path)
    try:
        yield conn
    finally:
        conn.close()
