"""Read-only MySQL config for the live EMA-59 signal-generator feed."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_SG_ENV = Path("/home/telgenbuescher/projects/signal-generator/.env")
_DASHBOARD_ENV = Path(__file__).resolve().parents[1] / ".env"
_KEYS = (
    "EMA_SG_MYSQL_HOST",
    "EMA_SG_MYSQL_PORT",
    "EMA_SG_MYSQL_USER",
    "EMA_SG_MYSQL_PASSWORD",
    "EMA_SG_MYSQL_DATABASE",
    "MYSQL_HOST",
    "MYSQL_PORT",
    "MYSQL_USER",
    "MYSQL_PASSWORD",
    "MYSQL_DB_DEV",
    "MYSQL_DB_PROD",
    "TRADING_ENV",
)


@dataclass(frozen=True)
class EmaSgDbConfig:
    host: str
    port: int
    name: str
    user: str
    password: str
    connect_timeout: int = 3
    read_timeout: int = 8

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
            "read_timeout": self.read_timeout,
            # DATETIME is TZ-naive; keep session UTC so NOW()/comparisons stay consistent.
            "init_command": "SET time_zone = '+00:00'",
        }


def _parse_env_file(path: Path) -> dict[str, str]:
    loaded: dict[str, str] = {}
    if not path.is_file():
        return loaded
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in _KEYS:
            continue
        loaded[key] = value.strip().strip("'").strip('"')
    return loaded


def load_ema_sg_db_config(environ: dict[str, str] | None = None) -> EmaSgDbConfig | None:
    file_env = {**_parse_env_file(_SG_ENV), **_parse_env_file(_DASHBOARD_ENV)}
    env = {**file_env, **(environ if environ is not None else dict(os.environ))}
    host = (env.get("EMA_SG_MYSQL_HOST") or env.get("MYSQL_HOST") or "").strip()
    user = (env.get("EMA_SG_MYSQL_USER") or env.get("MYSQL_USER") or "").strip()
    password = env.get("EMA_SG_MYSQL_PASSWORD")
    if password is None:
        password = env.get("MYSQL_PASSWORD")
    trading_env = (env.get("TRADING_ENV") or "dev").strip().lower()
    default_db = env.get("MYSQL_DB_PROD") if trading_env == "prod" else env.get("MYSQL_DB_DEV")
    name = (env.get("EMA_SG_MYSQL_DATABASE") or default_db or "ema_db_dev").strip()
    if not host or not user or password is None:
        return None
    return EmaSgDbConfig(
        host=host,
        port=int((env.get("EMA_SG_MYSQL_PORT") or env.get("MYSQL_PORT") or "3306").strip()),
        name=name,
        user=user,
        password=password or "",
    )
