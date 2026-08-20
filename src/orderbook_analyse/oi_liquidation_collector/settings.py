"""Env/settings for the OI+liquidation collector. Never logs secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_UNIVERSE = Path(
    "/home/telgenbuescher/projects/wave_fade_gold_f16ae32/config/universe_tradeable_51.json"
)
DEFAULT_LOCK = PROJECT_ROOT / "logs" / "oi_liquidation_collector.lock"
DEFAULT_PID = PROJECT_ROOT / "logs" / "oi_liquidation_collector.pid"
DEFAULT_BACKFILL_DIR = PROJECT_ROOT / "data" / "oi_liquidation_collector" / "backfill"


@dataclass(frozen=True)
class OICollectorSettings:
    bybit_ws_url: str
    bybit_rest_url: str
    clickhouse_host: str
    clickhouse_http_port: int
    clickhouse_database: str
    clickhouse_user: str
    clickhouse_password: str
    universe_path: Path
    lock_path: Path
    pid_path: Path
    batch_size: int = 500
    flush_interval_sec: float = 1.0
    queue_maxsize: int = 50_000
    ping_interval_sec: float = 20.0
    ping_timeout_sec: float = 20.0
    stale_data_sec: float = 15.0
    heartbeat_interval_sec: float = 30.0
    reconnect_initial_sec: float = 1.0
    reconnect_max_sec: float = 4.0
    subscribe_chunk: int = 10


def load_oi_settings(*, dotenv_path: Path | None = None) -> OICollectorSettings:
    path = dotenv_path or (PROJECT_ROOT / ".env")
    load_dotenv(path, override=False)
    port_raw = os.environ.get("CLICKHOUSE_HTTP_PORT") or "8123"
    universe = Path(os.environ.get("OI_LIQ_UNIVERSE_PATH") or DEFAULT_UNIVERSE)
    return OICollectorSettings(
        bybit_ws_url=os.environ.get("BYBIT_WS_URL") or "wss://stream.bybit.com/v5/public/linear",
        bybit_rest_url=os.environ.get("BYBIT_REST_URL") or "https://api.bybit.com",
        clickhouse_host=os.environ.get("CLICKHOUSE_HOST") or "127.0.0.1",
        clickhouse_http_port=int(port_raw),
        clickhouse_database=os.environ.get("CLICKHOUSE_DATABASE") or "orderbook_analysis",
        clickhouse_user=os.environ.get("CLICKHOUSE_USER") or "",
        clickhouse_password=os.environ.get("CLICKHOUSE_PASSWORD") or "",
        universe_path=universe,
        lock_path=Path(os.environ.get("OI_LIQ_LOCK_PATH") or DEFAULT_LOCK),
        pid_path=Path(os.environ.get("OI_LIQ_PID_PATH") or DEFAULT_PID),
    )


def redact_settings(settings: OICollectorSettings) -> dict[str, object]:
    return {
        "bybit_ws_url": settings.bybit_ws_url,
        "bybit_rest_url": settings.bybit_rest_url,
        "clickhouse_host": settings.clickhouse_host,
        "clickhouse_http_port": settings.clickhouse_http_port,
        "clickhouse_database": settings.clickhouse_database,
        "clickhouse_user": settings.clickhouse_user,
        "clickhouse_password": "***",
        "universe_path": str(settings.universe_path),
    }
