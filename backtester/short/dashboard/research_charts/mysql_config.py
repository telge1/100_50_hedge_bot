"""Load REGIME_DB_* for read-only market_candles access. Password is never logged."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ENV_FILE = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/"
    "research/regime_scanner/.env.regime_db"
)

DEFAULT_EXCHANGE = "bybit"


@dataclass(frozen=True)
class MysqlConfig:
    host: str
    port: int
    name: str
    user: str
    password: str
    exchange: str = DEFAULT_EXCHANGE

    def connect_kwargs(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "database": self.name,
            "charset": "utf8mb4",
            "autocommit": True,
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


def load_mysql_config(environ: dict[str, str] | None = None) -> MysqlConfig:
    file_env = _parse_env_file(DEFAULT_ENV_FILE)
    env = {**file_env, **(environ if environ is not None else dict(os.environ))}
    missing = [
        key
        for key in ("REGIME_DB_HOST", "REGIME_DB_PORT", "REGIME_DB_NAME", "REGIME_DB_USER")
        if not str(env.get(key, "")).strip()
    ]
    if "REGIME_DB_PASSWORD" not in env:
        missing.append("REGIME_DB_PASSWORD")
    if missing:
        raise RuntimeError("Missing MySQL config: " + ", ".join(missing))
    return MysqlConfig(
        host=str(env["REGIME_DB_HOST"]).strip(),
        port=int(str(env["REGIME_DB_PORT"]).strip()),
        name=str(env["REGIME_DB_NAME"]).strip(),
        user=str(env["REGIME_DB_USER"]).strip(),
        password=str(env.get("REGIME_DB_PASSWORD", "")),
        exchange=str(env.get("RESEARCH_CANDLE_EXCHANGE") or DEFAULT_EXCHANGE).strip() or DEFAULT_EXCHANGE,
    )
