"""Application configuration loaded from environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    bybit_ws_url: str
    symbol: str
    orderbook_depth: int
    clickhouse_host: str
    clickhouse_http_port: int
    clickhouse_database: str
    clickhouse_user: str
    clickhouse_password: str

    # Runtime defaults (CLI may override)
    duration_sec: float = 300.0
    batch_size: int = 5000
    flush_interval_sec: float = 1.0
    ticker_sample_interval_sec: float = 1.0
    log_level: str = "INFO"
    queue_maxsize: int = 10_000
    ping_interval_sec: float = 20.0
    heartbeat_interval_sec: float = 30.0
    reconnect_initial_sec: float = 1.0
    reconnect_max_sec: float = 30.0
    reconnect_max_attempts: int = 10


def _require(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise RuntimeError(f"{name} fehlt in der Umgebung/.env")
    return value


def load_settings(
    *,
    dotenv_path: Path | None = None,
    override: bool = False,
) -> Settings:
    path = dotenv_path or (_PROJECT_ROOT / ".env")
    load_dotenv(path, override=override)

    depth_raw = os.environ.get("ORDERBOOK_DEPTH", "200")
    try:
        depth = int(depth_raw)
    except ValueError as exc:
        raise RuntimeError(f"ORDERBOOK_DEPTH ist ungültig: {depth_raw!r}") from exc

    port_raw = _require("CLICKHOUSE_HTTP_PORT")
    try:
        http_port = int(port_raw)
    except ValueError as exc:
        raise RuntimeError(f"CLICKHOUSE_HTTP_PORT ist ungültig: {port_raw!r}") from exc

    return Settings(
        bybit_ws_url=_require("BYBIT_WS_URL"),
        symbol=_require("SYMBOL"),
        orderbook_depth=depth,
        clickhouse_host=_require("CLICKHOUSE_HOST"),
        clickhouse_http_port=http_port,
        clickhouse_database=_require("CLICKHOUSE_DATABASE"),
        clickhouse_user=_require("CLICKHOUSE_USER"),
        clickhouse_password=_require("CLICKHOUSE_PASSWORD"),
    )


def redact_settings(settings: Settings) -> dict[str, object]:
    """Public view of settings without secrets."""
    return {
        "bybit_ws_url": settings.bybit_ws_url,
        "symbol": settings.symbol,
        "orderbook_depth": settings.orderbook_depth,
        "clickhouse_host": settings.clickhouse_host,
        "clickhouse_http_port": settings.clickhouse_http_port,
        "clickhouse_database": settings.clickhouse_database,
        "clickhouse_user": settings.clickhouse_user,
        "clickhouse_password": "***",
        "duration_sec": settings.duration_sec,
        "batch_size": settings.batch_size,
        "flush_interval_sec": settings.flush_interval_sec,
        "ticker_sample_interval_sec": settings.ticker_sample_interval_sec,
        "log_level": settings.log_level,
    }
