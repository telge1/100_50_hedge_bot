"""Environment-based ClickHouse configuration.

Follows the server convention used by orderbook_analyse:
CLICKHOUSE_HOST, CLICKHOUSE_HTTP_PORT, CLICKHOUSE_DATABASE,
CLICKHOUSE_USER, CLICKHOUSE_PASSWORD.

CLICKHOUSE_PORT is accepted as an alias for the HTTP port.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_DATABASE = "signal_generator"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8123


@dataclass(frozen=True, slots=True)
class ClickHouseSettings:
    host: str
    port: int
    database: str
    user: str
    password: str

    @property
    def http_port(self) -> int:
        return self.port


def _require(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_env_files(*, project_root: Path | None = None) -> None:
    """Load project ``.env`` as the local source of truth (overrides empty defaults).

    Existing process env vars that were exported intentionally still win only if
    ``.env`` is absent; when ``.env`` exists it overrides so this project's
    ``CLICKHOUSE_DATABASE=signal_generator`` is not shadowed by a sibling
    project's shell environment.
    """
    if project_root is None:
        project_root = Path(__file__).resolve().parents[2]
    env_path = project_root / ".env"
    if env_path.is_file():
        load_dotenv(env_path, override=True)


def get_clickhouse_settings(*, load_dotenv_file: bool = True) -> ClickHouseSettings:
    if load_dotenv_file:
        load_env_files()

    port_raw = os.environ.get("CLICKHOUSE_HTTP_PORT") or os.environ.get("CLICKHOUSE_PORT")
    if port_raw is None or port_raw == "":
        port = DEFAULT_HTTP_PORT
    else:
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise RuntimeError(f"Invalid ClickHouse HTTP port: {port_raw!r}") from exc

    database = os.environ.get("CLICKHOUSE_DATABASE") or DEFAULT_DATABASE

    return ClickHouseSettings(
        host=os.environ.get("CLICKHOUSE_HOST", DEFAULT_HOST),
        port=port,
        database=database,
        user=_require("CLICKHOUSE_USER"),
        password=_require("CLICKHOUSE_PASSWORD"),
    )
