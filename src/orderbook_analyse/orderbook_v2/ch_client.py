"""Standalone ClickHouse connection for historical Orderbook V2 import.

Reads CLICKHOUSE_* from the process environment, optionally loading a
project-root ``.env`` first (python-dotenv). No credentials are hardcoded.
The live OI/liquidation collector is not imported.

A TCP/HTTP connection is created only by :func:`get_clickhouse_client`.
Importing this module does not open a network connection.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_DOTENV = _PROJECT_ROOT / ".env"

_REQUIRED_ENV = (
    "CLICKHOUSE_HOST",
    "CLICKHOUSE_HTTP_PORT",
    "CLICKHOUSE_DATABASE",
    "CLICKHOUSE_USER",
)


class ClickHouseConfigError(RuntimeError):
    """Raised when required ClickHouse environment variables are missing."""


@dataclass(frozen=True)
class ClickHouseSettings:
    host: str
    http_port: int
    database: str
    user: str
    password: str


def _maybe_load_dotenv(dotenv_path: Path | None) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    path = dotenv_path if dotenv_path is not None else _DEFAULT_DOTENV
    if path.is_file():
        load_dotenv(path, override=False)


def load_clickhouse_settings(
    *,
    dotenv_path: Path | None = None,
    load_env_file: bool = True,
) -> ClickHouseSettings:
    """Load ClickHouse settings from the environment.

    ``CLICKHOUSE_PASSWORD`` may be empty (local unauthenticated instances).
    Missing required names are reported without printing any values.
    """
    if load_env_file:
        _maybe_load_dotenv(dotenv_path)

    missing = [name for name in _REQUIRED_ENV if not (os.environ.get(name) or "").strip()]
    if missing:
        raise ClickHouseConfigError(
            "Orderbook V2 ClickHouse configuration is incomplete. Missing: "
            + ", ".join(missing)
            + ". Set them in the environment or the project .env file."
        )

    port_raw = os.environ["CLICKHOUSE_HTTP_PORT"].strip()
    try:
        http_port = int(port_raw)
    except ValueError as exc:
        raise ClickHouseConfigError(
            "CLICKHOUSE_HTTP_PORT must be an integer."
        ) from exc

    return ClickHouseSettings(
        host=os.environ["CLICKHOUSE_HOST"].strip(),
        http_port=http_port,
        database=os.environ["CLICKHOUSE_DATABASE"].strip(),
        user=os.environ["CLICKHOUSE_USER"].strip(),
        password=os.environ.get("CLICKHOUSE_PASSWORD") or "",
    )


def get_clickhouse_client(
    settings: ClickHouseSettings | None = None,
    *,
    dotenv_path: Path | None = None,
    load_env_file: bool = True,
) -> Any:
    """Create a clickhouse_connect client. Does not run at import time."""
    cfg = settings or load_clickhouse_settings(
        dotenv_path=dotenv_path, load_env_file=load_env_file
    )
    import clickhouse_connect

    return clickhouse_connect.get_client(
        host=cfg.host,
        port=cfg.http_port,
        username=cfg.user,
        password=cfg.password,
        database=cfg.database,
    )
