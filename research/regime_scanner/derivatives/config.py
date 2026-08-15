"""Configuration for derivatives 5m import (target + read-only source)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote_plus

from research.regime_scanner.mysql_candle_store.config import (
    RegimeDbConfig,
    RegimeDbConfigError,
    load_regime_db_config,
)

IMPORT_VERSION_DEFAULT = "derivatives_5m_v1"
SOURCE_DATABASE_DEFAULT = "liquidation_research"
SOURCE_TABLE = "liquidation_data"
PILOT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "APTUSDT")
KNOWN_UNAVAILABLE_SYMBOLS = frozenset({"ENAUSDT", "ARBUSDT", "OPUSDT"})

# Gap between consecutive source minutes that starts a new sequence.
SEQUENCE_GAP_SECONDS = 60 * 60  # 60 minutes
EXPECTED_ROWS_PER_5M = 5
BUCKET_SECONDS = 300

# Documented collector outage (UTC).
KNOWN_OUTAGE_START = "2026-03-25T18:13:00+00:00"
KNOWN_OUTAGE_END = "2026-03-27T16:46:00+00:00"

SOURCE_SELECT_COLUMNS = (
    "timestamp",
    "symbol",
    "open_interest",
    "open_interest_value",
    "long_liq_usd",
    "short_liq_usd",
    "total_liq_usd",
    "buy_volume",
    "sell_volume",
    "spread",
)


class DerivativeSourceConfigError(ValueError):
    """Raised when DERIVATIVE_SOURCE_DB_* settings are missing or invalid."""


@dataclass(frozen=True)
class DerivativeSourceConfig:
    """Read-only source DB settings. Never log password."""

    host: str
    port: int
    name: str
    user: str
    password: str
    backend: str = "pymysql"  # pymysql | cli
    connect_timeout: int = 15

    @property
    def sqlalchemy_url(self) -> str:
        user = quote_plus(self.user)
        password = quote_plus(self.password)
        return (
            f"mysql+pymysql://{user}:{password}@{self.host}:{self.port}/{self.name}"
            "?charset=utf8mb4"
        )


_SOURCE_REQUIRED = (
    "DERIVATIVE_SOURCE_DB_HOST",
    "DERIVATIVE_SOURCE_DB_PORT",
    "DERIVATIVE_SOURCE_DB_NAME",
    "DERIVATIVE_SOURCE_DB_USER",
)


def load_derivative_source_config(
    environ: dict[str, str] | None = None,
    *,
    require_password: bool = False,
) -> DerivativeSourceConfig:
    """Load source DB env. Password optional for local CLI/socket RO users."""
    env = dict(os.environ if environ is None else environ)
    missing = [k for k in _SOURCE_REQUIRED if not str(env.get(k, "")).strip()]
    if require_password and not str(env.get("DERIVATIVE_SOURCE_DB_PASSWORD", "")):
        # only require when backend is pymysql and explicitly requested
        pass
    if missing:
        raise DerivativeSourceConfigError(
            "Missing required derivative source DB environment variable(s): "
            + ", ".join(missing)
            + ". See research/regime_scanner/env.derivative_source.example. "
            "Do not reuse REGIME_DB_* or writer credentials."
        )
    port_raw = str(env["DERIVATIVE_SOURCE_DB_PORT"]).strip()
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise DerivativeSourceConfigError(
            f"DERIVATIVE_SOURCE_DB_PORT must be an integer, got {port_raw!r}"
        ) from exc
    backend = str(env.get("DERIVATIVE_SOURCE_DB_BACKEND", "pymysql")).strip().lower()
    if backend not in {"pymysql", "cli"}:
        raise DerivativeSourceConfigError(
            f"DERIVATIVE_SOURCE_DB_BACKEND must be 'pymysql' or 'cli', got {backend!r}"
        )
    if backend == "pymysql" and "DERIVATIVE_SOURCE_DB_PASSWORD" not in env:
        raise DerivativeSourceConfigError(
            "DERIVATIVE_SOURCE_DB_PASSWORD must be set for pymysql backend "
            "(use empty string only for passwordless accounts)."
        )
    return DerivativeSourceConfig(
        host=str(env["DERIVATIVE_SOURCE_DB_HOST"]).strip(),
        port=port,
        name=str(env["DERIVATIVE_SOURCE_DB_NAME"]).strip(),
        user=str(env["DERIVATIVE_SOURCE_DB_USER"]).strip(),
        password=str(env.get("DERIVATIVE_SOURCE_DB_PASSWORD", "")),
        backend=backend,
        connect_timeout=int(env.get("DERIVATIVE_SOURCE_DB_CONNECT_TIMEOUT", "15")),
    )


def load_target_config(environ: dict[str, str] | None = None) -> RegimeDbConfig:
    return load_regime_db_config(environ)


def load_env_file(path: str | os.PathLike[str]) -> None:
    """Load KEY=VAL into os.environ if not already set. Never prints values."""
    from pathlib import Path

    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, val = s.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


__all__ = [
    "IMPORT_VERSION_DEFAULT",
    "SOURCE_DATABASE_DEFAULT",
    "SOURCE_TABLE",
    "PILOT_SYMBOLS",
    "KNOWN_UNAVAILABLE_SYMBOLS",
    "SEQUENCE_GAP_SECONDS",
    "EXPECTED_ROWS_PER_5M",
    "BUCKET_SECONDS",
    "KNOWN_OUTAGE_START",
    "KNOWN_OUTAGE_END",
    "SOURCE_SELECT_COLUMNS",
    "DerivativeSourceConfig",
    "DerivativeSourceConfigError",
    "RegimeDbConfig",
    "RegimeDbConfigError",
    "load_derivative_source_config",
    "load_target_config",
    "load_env_file",
]
