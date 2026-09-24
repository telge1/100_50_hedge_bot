"""Read-only Gold Shadow MySQL config. Never logs passwords. No schema writes."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_DASHBOARD_ENV = Path(__file__).resolve().parents[1] / ".env"


@dataclass(frozen=True)
class GoldShadowDbConfig:
    host: str
    port: int
    name: str
    user: str
    password: str
    connect_timeout: int = 3
    read_timeout: int = 5

    def connect_kwargs(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "user": self.user,
            "password": self.password,
            "database": self.name,
            "charset": "utf8mb4",
            "autocommit": True,
            "connect_timeout": self.connect_timeout,
        }


ALLOWED_DB_NAMES = frozenset({"wave_fade_gold_live_dev", "wave_fade_gold_live_test"})
BLOCKED_DB_NAMES = frozenset({"wave_fade_gold_live"})

STRATEGY_ID = "wave_fade_frozen_f16ae32"
FROZEN_PIN = "5636a7d"
UNIVERSE_SIZE = 51
TIMEFRAMES = ("15m", "30m", "1h", "4h")
SLOT_COUNT = 6
POLL_INTERVAL_MS = 4000
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25
_GOLD_KEYS = (
    "GOLD_SHADOW_DB_HOST",
    "GOLD_SHADOW_DB_PORT",
    "GOLD_SHADOW_DB_NAME",
    "GOLD_SHADOW_DB_USER",
    "GOLD_SHADOW_DB_PASSWORD",
)


def _local_gold_env() -> dict[str, str]:
    loaded: dict[str, str] = {}
    if not _DASHBOARD_ENV.is_file():
        return loaded
    for raw in _DASHBOARD_ENV.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in _GOLD_KEYS:
            continue
        loaded[key] = value.strip().strip("'").strip('"')
    return loaded


def load_gold_shadow_db_config(environ: dict[str, str] | None = None) -> GoldShadowDbConfig | None:
    file_env = _local_gold_env() if environ is None else {}
    env = {**file_env, **(environ if environ is not None else dict(os.environ))}
    name = (env.get("GOLD_SHADOW_DB_NAME") or "wave_fade_gold_live_dev").strip()
    if name in BLOCKED_DB_NAMES or name not in ALLOWED_DB_NAMES:
        raise RuntimeError(f"gold shadow dashboard refused database {name!r}")
    host = (env.get("GOLD_SHADOW_DB_HOST") or "").strip()
    user = (env.get("GOLD_SHADOW_DB_USER") or "").strip()
    if not host or not user or "GOLD_SHADOW_DB_PASSWORD" not in env:
        return None
    return GoldShadowDbConfig(
        host=host,
        port=int((env.get("GOLD_SHADOW_DB_PORT") or "3306").strip()),
        name=name,
        user=user,
        password=env.get("GOLD_SHADOW_DB_PASSWORD") or "",
    )
