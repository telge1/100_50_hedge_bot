"""ClickHouse config for shadow registry sync (live_forward DB)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SHADOW_CH_DATABASE = "live_forward"
SHADOW_CH_SYNC_ENV = "SHADOW_CH_SYNC_ENABLED"

_DEFAULT_ENV_FILE = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves/.env"
)
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_HTTP_PORT = 8123


@dataclass(frozen=True)
class ShadowClickHouseConfig:
    host: str
    port: int
    user: str
    password: str
    database: str = SHADOW_CH_DATABASE

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
    if not path.is_file():
        return loaded
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        loaded[key.strip()] = value.strip().strip("'").strip('"')
    return loaded


def apply_live_scanner_runtime_env() -> None:
    """Persistent live-scanner default: CH sync on unless env or collector .env disables it."""
    file_env = _parse_env_file(_DEFAULT_ENV_FILE)
    if SHADOW_CH_SYNC_ENV in file_env:
        os.environ.setdefault(SHADOW_CH_SYNC_ENV, file_env[SHADOW_CH_SYNC_ENV])
    else:
        os.environ.setdefault(SHADOW_CH_SYNC_ENV, "1")


def shadow_ch_sync_enabled(environ: dict[str, str] | None = None) -> bool:
    env = dict(environ) if environ is not None else dict(os.environ)
    if SHADOW_CH_SYNC_ENV not in env:
        file_env = _parse_env_file(_DEFAULT_ENV_FILE)
        if SHADOW_CH_SYNC_ENV in file_env:
            env[SHADOW_CH_SYNC_ENV] = file_env[SHADOW_CH_SYNC_ENV]
    raw = env.get(SHADOW_CH_SYNC_ENV, "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


def load_shadow_ch_config(environ: dict[str, str] | None = None) -> ShadowClickHouseConfig:
    file_env = _parse_env_file(_DEFAULT_ENV_FILE)
    overlay = environ if environ is not None else dict(os.environ)
    env = dict(file_env)
    for key in (
        "CLICKHOUSE_HOST",
        "CLICKHOUSE_HTTP_PORT",
        "CLICKHOUSE_PORT",
        "CLICKHOUSE_USER",
        "CLICKHOUSE_PASSWORD",
    ):
        val = overlay.get(key)
        if val:
            env[key] = val
    port_raw = env.get("CLICKHOUSE_HTTP_PORT") or env.get("CLICKHOUSE_PORT") or str(_DEFAULT_HTTP_PORT)
    missing = [key for key in ("CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD") if key not in env]
    if missing:
        raise RuntimeError("Missing ClickHouse config: " + ", ".join(missing))
    database = str(overlay.get("SHADOW_CH_DATABASE") or SHADOW_CH_DATABASE).strip()
    return ShadowClickHouseConfig(
        host=str(env.get("CLICKHOUSE_HOST") or _DEFAULT_HOST).strip(),
        port=int(str(port_raw).strip()),
        user=str(env["CLICKHOUSE_USER"]).strip(),
        password=str(env.get("CLICKHOUSE_PASSWORD", "")),
        database=database,
    )
