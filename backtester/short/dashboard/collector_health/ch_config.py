"""ClickHouse config for orderbook_analysis (OI/PT canonical). Never logs password."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

OA_ENV = Path(
    os.environ.get(
        "ORDERBOOK_ANALYSE_ENV",
        "/home/telgenbuescher/projects/orderbook_analyse/.env",
    )
)
SG_ENV = Path(
    os.environ.get(
        "STOCH_COLLECTOR_ENV",
        "/home/telgenbuescher/projects/Signal_Generator_Ralf/signal_generator_stoch_waves/.env",
    )
)


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("'").strip('"')
    return out


@dataclass(frozen=True)
class CHConfig:
    host: str
    port: int
    user: str
    password: str
    database: str

    def connect_kwargs(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "username": self.user,
            "password": self.password,
            "database": self.database,
        }


def load_orderbook_ch_config(environ: dict[str, str] | None = None) -> CHConfig:
    overlay = environ if environ is not None else dict(os.environ)
    env = {}
    env.update(_parse_env_file(SG_ENV))
    env.update(_parse_env_file(OA_ENV))
    for key in (
        "CLICKHOUSE_HOST",
        "CLICKHOUSE_HTTP_PORT",
        "CLICKHOUSE_PORT",
        "CLICKHOUSE_USER",
        "CLICKHOUSE_PASSWORD",
        "CLICKHOUSE_DATABASE",
        "ORDERBOOK_CLICKHOUSE_DATABASE",
    ):
        if overlay.get(key):
            env[key] = overlay[key]
    port_raw = env.get("CLICKHOUSE_HTTP_PORT") or env.get("CLICKHOUSE_PORT") or "8123"
    database = (
        overlay.get("ORDERBOOK_CLICKHOUSE_DATABASE")
        or env.get("ORDERBOOK_CLICKHOUSE_DATABASE")
        or "orderbook_analysis"
    )
    user = env.get("CLICKHOUSE_USER")
    password = env.get("CLICKHOUSE_PASSWORD", "")
    if not user:
        raise RuntimeError("Missing CLICKHOUSE_USER for orderbook_analysis")
    return CHConfig(
        host=str(env.get("CLICKHOUSE_HOST") or "127.0.0.1").strip(),
        port=int(str(port_raw).strip()),
        user=str(user).strip(),
        password=str(password),
        database=str(database).strip(),
    )
