"""Load ClickHouse settings for read-only candles_1m. Password is never logged.

Uses the same env keys as the existing live collector (.env), so Research
reads the collector Source of Truth without duplicating credentials.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ENV_FILE = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/"
    "signal_generator_stoch_waves/.env"
)

DEFAULT_DATABASE = "signal_generator"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8123
DEFAULT_EXCHANGE = "bybit"
DEFAULT_TABLE = "candles_1m"

# Process env may be polluted by orderbook_analyse dotenv (orderbook_analysis DB).
# Research candles always read credentials + DB from the collector .env unless
# RESEARCH_CLICKHOUSE_* overrides are set explicitly.
_CONFIG_OVERLAY_KEYS = (
    "CLICKHOUSE_HOST",
    "CLICKHOUSE_HTTP_PORT",
    "CLICKHOUSE_PORT",
    "CLICKHOUSE_USER",
    "CLICKHOUSE_PASSWORD",
    "RESEARCH_CLICKHOUSE_DATABASE",
    "RESEARCH_CANDLE_EXCHANGE",
    "RESEARCH_CANDLE_TABLE",
)

@dataclass(frozen=True)
class ClickHouseConfig:
    host: str
    port: int
    database: str
    user: str
    password: str
    exchange: str = DEFAULT_EXCHANGE
    table: str = DEFAULT_TABLE

    def connect_kwargs(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "username": self.user,
            "password": self.password,
            "database": self.database,
        }


def _parse_env_file(path: Path) -> dict[str, str]:
    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        loaded[key.strip()] = value.strip().strip("'").strip('"')
    return loaded


def load_clickhouse_config(environ: dict[str, str] | None = None) -> ClickHouseConfig:
    file_env = _parse_env_file(DEFAULT_ENV_FILE)
    overlay = environ if environ is not None else dict(os.environ)
    env = dict(file_env)
    for key in _CONFIG_OVERLAY_KEYS:
        val = overlay.get(key)
        if val:
            env[key] = val
    port_raw = env.get("CLICKHOUSE_HTTP_PORT") or env.get("CLICKHOUSE_PORT") or str(DEFAULT_HTTP_PORT)
    missing = [key for key in ("CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD") if key not in env]
    if missing:
        raise RuntimeError("Missing ClickHouse config: " + ", ".join(missing))
    database = str(
        env.get("RESEARCH_CLICKHOUSE_DATABASE")
        or env.get("CLICKHOUSE_DATABASE")
        or DEFAULT_DATABASE
    ).strip()
    return ClickHouseConfig(
        host=str(env.get("CLICKHOUSE_HOST") or DEFAULT_HOST).strip(),
        port=int(str(port_raw).strip()),
        database=database,
        user=str(env["CLICKHOUSE_USER"]).strip(),
        password=str(env.get("CLICKHOUSE_PASSWORD", "")),
        exchange=str(env.get("RESEARCH_CANDLE_EXCHANGE") or DEFAULT_EXCHANGE).strip() or DEFAULT_EXCHANGE,
        table=str(env.get("RESEARCH_CANDLE_TABLE") or DEFAULT_TABLE).strip() or DEFAULT_TABLE,
    )
