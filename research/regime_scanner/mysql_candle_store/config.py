"""Environment-based configuration for the regime candle store."""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote_plus


class RegimeDbConfigError(ValueError):
    """Raised when required REGIME_DB_* settings are missing or invalid."""


@dataclass(frozen=True)
class RegimeDbConfig:
    host: str
    port: int
    name: str
    user: str
    password: str

    @property
    def sqlalchemy_url(self) -> str:
        user = quote_plus(self.user)
        password = quote_plus(self.password)
        return (
            f"mysql+pymysql://{user}:{password}@{self.host}:{self.port}/{self.name}"
            "?charset=utf8mb4"
        )


_REQUIRED_ALWAYS = ("REGIME_DB_HOST", "REGIME_DB_PORT", "REGIME_DB_NAME", "REGIME_DB_USER")


def load_regime_db_config(
    environ: dict[str, str] | None = None,
    *,
    require_password: bool = True,
) -> RegimeDbConfig:
    """Load ``REGIME_DB_*`` settings from ``environ`` or ``os.environ``.

    Missing required variables raise :class:`RegimeDbConfigError` with a clear
    message. No secrets are logged.
    """
    env = dict(os.environ if environ is None else environ)
    missing: list[str] = []
    for key in _REQUIRED_ALWAYS:
        if not str(env.get(key, "")).strip():
            missing.append(key)
    if require_password and "REGIME_DB_PASSWORD" not in env:
        missing.append("REGIME_DB_PASSWORD")
    if missing:
        raise RegimeDbConfigError(
            "Missing required regime DB environment variable(s): "
            + ", ".join(missing)
            + ". See research/regime_scanner/MYSQL_DATA.md and "
            "research/regime_scanner/env.regime_db.example."
        )

    host = str(env["REGIME_DB_HOST"]).strip()
    name = str(env["REGIME_DB_NAME"]).strip()
    user = str(env["REGIME_DB_USER"]).strip()
    password = str(env.get("REGIME_DB_PASSWORD", ""))
    port_raw = str(env["REGIME_DB_PORT"]).strip()
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise RegimeDbConfigError(
            f"REGIME_DB_PORT must be an integer, got {port_raw!r}"
        ) from exc
    if port <= 0 or port > 65535:
        raise RegimeDbConfigError(f"REGIME_DB_PORT out of range: {port}")
    return RegimeDbConfig(host=host, port=port, name=name, user=user, password=password)


def has_regime_db_config(environ: dict[str, str] | None = None) -> bool:
    try:
        load_regime_db_config(environ)
        return True
    except RegimeDbConfigError:
        return False
