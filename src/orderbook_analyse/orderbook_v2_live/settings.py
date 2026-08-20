"""Settings for the Orderbook V3 live collector. Never logs secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from orderbook_analyse.orderbook_v2_live.universe import (
    ADA_SYMBOLS,
    UniverseError,
    symbols_for_mode,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PILOT_SYMBOL = "ADAUSDT"
DEFAULT_LOCK = PROJECT_ROOT / "logs" / "orderbook_v3_live_collector.lock"
DEFAULT_PID = PROJECT_ROOT / "logs" / "orderbook_v3_live_collector.pid"
DEFAULT_WS = "wss://stream.bybit.com/v5/public/linear"


class LiveCollectorConfigError(ValueError):
    pass


@dataclass(frozen=True)
class LiveCollectorSettings:
    bybit_ws_url: str
    clickhouse_host: str
    clickhouse_http_port: int
    clickhouse_database: str
    clickhouse_user: str
    clickhouse_password: str
    symbols: tuple[str, ...]
    mode: str
    lock_path: Path
    pid_path: Path
    health_path: Path | None
    ping_interval_sec: float = 20.0
    ping_timeout_sec: float = 20.0
    stale_data_sec: float = 15.0
    heartbeat_interval_sec: float = 5.0
    reconnect_initial_sec: float = 1.0
    reconnect_max_sec: float = 8.0
    depth: int = 200
    exchange: str = "bybit"
    market: str = "linear"
    ada_only_pilot: bool = True
    subscribe_chunk_size: int = 10
    subscribe_chunk_pause_sec: float = 0.05
    queue_capacity: int = 2048
    insert_batch_size: int = 100
    flush_interval_sec: float = 0.5
    insert_retry_count: int = 3
    shutdown_flush_timeout_sec: float = 10.0

    def orderbook_topics(self) -> list[str]:
        return [f"orderbook.{self.depth}.{s}" for s in self.symbols]


def parse_symbols(raw: str | None, *, ada_only_pilot: bool = True) -> tuple[str, ...]:
    text = (raw or PILOT_SYMBOL).strip()
    symbols = tuple(s.strip().upper() for s in text.split(",") if s.strip())
    if not symbols:
        raise LiveCollectorConfigError("symbols list is empty")
    if ada_only_pilot:
        if symbols != ADA_SYMBOLS:
            raise LiveCollectorConfigError(
                "ADA-only default rejected symbols: " + ",".join(symbols)
                + ". Use --mode shadow3 or --mode universe51 --confirm-universe-51."
            )
    return symbols


def load_live_settings(
    *,
    dotenv_path: Path | None = None,
    symbols_raw: str | None = None,
    mode: str = "ada",
    confirm_universe_51: bool = False,
    health_path: Path | None = None,
) -> LiveCollectorSettings:
    path = dotenv_path or (PROJECT_ROOT / ".env")
    if path.is_file():
        load_dotenv(path, override=False)
    port_raw = os.environ.get("CLICKHOUSE_HTTP_PORT") or "8123"
    ada_only = mode == "ada"
    if mode == "universe51" and not confirm_universe_51:
        raise LiveCollectorConfigError(
            "universe51 requires --confirm-universe-51 (refuses accidental 51-coin start)"
        )
    try:
        if mode in {"ada", "shadow3", "universe51"} and not symbols_raw:
            symbols = symbols_for_mode(mode)
        elif symbols_raw:
            symbols = parse_symbols(symbols_raw, ada_only_pilot=ada_only)
            if mode == "shadow3" and symbols != symbols_for_mode("shadow3"):
                raise LiveCollectorConfigError("shadow3 mode requires ADAUSDT,BTCUSDT,ETHUSDT")
        else:
            symbols = symbols_for_mode("ada")
    except UniverseError as exc:
        raise LiveCollectorConfigError(str(exc)) from exc
    return LiveCollectorSettings(
        bybit_ws_url=os.environ.get("BYBIT_WS_URL") or DEFAULT_WS,
        clickhouse_host=os.environ.get("CLICKHOUSE_HOST") or "127.0.0.1",
        clickhouse_http_port=int(port_raw),
        clickhouse_database=os.environ.get("CLICKHOUSE_DATABASE") or "orderbook_analysis",
        clickhouse_user=os.environ.get("CLICKHOUSE_USER") or "",
        clickhouse_password=os.environ.get("CLICKHOUSE_PASSWORD") or "",
        symbols=symbols,
        mode=mode,
        lock_path=Path(os.environ.get("OB_V3_LIVE_LOCK_PATH") or DEFAULT_LOCK),
        pid_path=Path(os.environ.get("OB_V3_LIVE_PID_PATH") or DEFAULT_PID),
        health_path=health_path,
        ada_only_pilot=ada_only,
        subscribe_chunk_size=int(os.environ.get("OB_V3_SUBSCRIBE_CHUNK") or 10),
        queue_capacity=int(os.environ.get("OB_V3_QUEUE_CAPACITY") or 2048),
        insert_batch_size=int(os.environ.get("OB_V3_INSERT_BATCH") or 100),
        flush_interval_sec=float(os.environ.get("OB_V3_FLUSH_SEC") or 0.5),
        insert_retry_count=int(os.environ.get("OB_V3_INSERT_RETRIES") or 3),
        shutdown_flush_timeout_sec=float(os.environ.get("OB_V3_SHUTDOWN_FLUSH_SEC") or 10),
    )


def redact_settings(settings: LiveCollectorSettings) -> dict[str, object]:
    return {
        "bybit_ws_url": settings.bybit_ws_url,
        "clickhouse_host": settings.clickhouse_host,
        "clickhouse_http_port": settings.clickhouse_http_port,
        "clickhouse_database": settings.clickhouse_database,
        "clickhouse_user": settings.clickhouse_user,
        "clickhouse_password": "***",
        "symbols": list(settings.symbols),
        "mode": settings.mode,
        "lock_path": str(settings.lock_path),
        "pid_path": str(settings.pid_path),
        "ada_only_pilot": settings.ada_only_pilot,
        "depth": settings.depth,
        "subscribe_chunk_size": settings.subscribe_chunk_size,
        "queue_capacity": settings.queue_capacity,
        "insert_batch_size": settings.insert_batch_size,
    }
