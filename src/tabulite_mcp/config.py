"""Runtime configuration.

Everything the server needs to know about paths and limits lives here so the
rest of the modules stay free of environment lookups and are easy to test.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_NULL_MARKERS: tuple[str, ...] = ("", "NULL", "null", "N/A", "NA")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


@dataclass(frozen=True)
class Config:
    """Paths and limits for one project directory."""

    source_dir: Path
    workspace_dir: Path

    # Values equal to one of these markers become SQL NULL during import.
    null_markers: tuple[str, ...] = DEFAULT_NULL_MARKERS

    # Import behavior.
    insert_batch_size: int = 5_000
    read_chunk_bytes: int = 1 << 20

    # Interactive query limits (exports are deliberately unbounded).
    max_query_rows: int = 1_000
    query_timeout_seconds: float = 30.0
    export_timeout_seconds: float = 600.0
    max_sample_rows: int = 100

    # Profiling.
    profile_sample_values: int = 5
    profile_invalid_examples: int = 5
    type_confidence_threshold: float = 0.99

    # Transport.
    host: str = "0.0.0.0"
    port: int = 8000
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()

    @property
    def databases_dir(self) -> Path:
        return self.workspace_dir / "databases"

    @property
    def exports_dir(self) -> Path:
        return self.workspace_dir / "exports"

    @property
    def catalog_path(self) -> Path:
        return self.workspace_dir / "catalog.sqlite"

    @property
    def database_path(self) -> Path:
        """Single analytical database holding every imported table.

        One file keeps cross-table joins possible without ATTACH, which the
        read-only query layer forbids.
        """
        return self.databases_dir / "main.sqlite"

    def ensure_directories(self) -> None:
        self.databases_dir.mkdir(parents=True, exist_ok=True)
        self.exports_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> "Config":
        source = Path(os.environ.get("TABULITE_SOURCE_DIR", "/project/source"))
        workspace = Path(os.environ.get("TABULITE_WORKSPACE_DIR", "/project/workspace"))

        markers_raw = os.environ.get("TABULITE_NULL_MARKERS")
        if markers_raw is None:
            markers = DEFAULT_NULL_MARKERS
        else:
            # Comma separated; an empty item means "the empty string".
            markers = tuple(part.strip() if part.strip() else "" for part in markers_raw.split(","))

        port = _env_int("TABULITE_PORT", 8000)
        hosts_raw = os.environ.get("TABULITE_ALLOWED_HOSTS")
        origins_raw = os.environ.get("TABULITE_ALLOWED_ORIGINS")
        default_hosts = (
            f"localhost:{port}",
            f"127.0.0.1:{port}",
            f"[::1]:{port}",
            f"host.docker.internal:{port}",
        )
        default_origins = tuple(f"http://{h}" for h in default_hosts)

        return cls(
            source_dir=source.resolve(),
            workspace_dir=workspace.resolve(),
            null_markers=markers,
            insert_batch_size=_env_int("TABULITE_BATCH_SIZE", 5_000),
            max_query_rows=_env_int("TABULITE_MAX_QUERY_ROWS", 1_000),
            query_timeout_seconds=_env_float("TABULITE_QUERY_TIMEOUT", 30.0),
            export_timeout_seconds=_env_float("TABULITE_EXPORT_TIMEOUT", 600.0),
            host=os.environ.get("TABULITE_HOST", "0.0.0.0"),
            port=port,
            allowed_hosts=tuple(h.strip() for h in hosts_raw.split(",")) if hosts_raw else default_hosts,
            allowed_origins=tuple(o.strip() for o in origins_raw.split(",")) if origins_raw else default_origins,
        )
