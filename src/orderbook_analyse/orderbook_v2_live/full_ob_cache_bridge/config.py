"""Bridge settings — default DISABLED; explicit env required to enable."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path

# Documented defaults for full_ob_cache_bridge_anchor_retention_v2.
DEFAULT_DELTA_RETENTION_SEC = 600.0
DEFAULT_CHECKPOINT_INTERVAL_SEC = 60.0
# Worst-case phase offset ≈ interval; +30s covers recv/wall skew and store latency.
DEFAULT_CHECKPOINT_SAFETY_MARGIN_SEC = 30.0


def compute_checkpoint_retention_sec(
    *,
    delta_retention_sec: float,
    checkpoint_interval_sec: float,
    safety_margin_sec: float,
) -> float:
    """checkpoint_retention >= delta + interval + safety (CONTRACT.md)."""
    return float(delta_retention_sec) + float(checkpoint_interval_sec) + float(
        safety_margin_sec
    )


def compute_max_checkpoints(
    *, checkpoint_retention_sec: float, checkpoint_interval_sec: float
) -> int:
    interval = max(1e-9, float(checkpoint_interval_sec))
    return max(2, int(math.ceil(float(checkpoint_retention_sec) / interval)) + 2)


def _b(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _i(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return int(raw)


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return float(raw)


@dataclass(frozen=True)
class CacheBridgeSettings:
    enabled: bool = False
    socket_path: Path = Path("/tmp/full_ob_cache_bridge.sock")
    dump_root: Path = Path("/tmp/full_ob_cache_bridge_dumps")
    symbols: frozenset[str] = frozenset({"BTCUSDT", "DOGEUSDT"})
    max_pre_roll_seconds: float = DEFAULT_DELTA_RETENTION_SEC
    min_pre_roll_seconds: float = 30.0
    delta_retention_seconds: float = DEFAULT_DELTA_RETENTION_SEC
    checkpoint_interval_sec: float = DEFAULT_CHECKPOINT_INTERVAL_SEC
    checkpoint_safety_margin_sec: float = DEFAULT_CHECKPOINT_SAFETY_MARGIN_SEC
    checkpoint_retention_seconds: float = (
        DEFAULT_DELTA_RETENTION_SEC
        + DEFAULT_CHECKPOINT_INTERVAL_SEC
        + DEFAULT_CHECKPOINT_SAFETY_MARGIN_SEC
    )
    max_payload_bytes: int = 96 * 1024 * 1024
    max_request_bytes: int = 16_384
    request_timeout_sec: float = 30.0
    max_concurrent_requests: int = 1
    min_request_interval_sec: float = 2.0
    max_checkpoints_per_symbol: int = 14
    max_checkpoint_bytes_per_symbol: int = 48 * 1024 * 1024
    stale_after_ms: int = 15_000
    socket_mode: int = 0o600


def load_cache_bridge_settings() -> CacheBridgeSettings:
    """Defaults keep the bridge OFF unless FULL_OB_CACHE_BRIDGE_ENABLED=true."""
    enabled = _b("FULL_OB_CACHE_BRIDGE_ENABLED", False)
    sock = os.environ.get("FULL_OB_CACHE_BRIDGE_SOCKET_PATH") or "/tmp/full_ob_cache_bridge.sock"
    root = os.environ.get("FULL_OB_CACHE_BRIDGE_DUMP_ROOT") or "/tmp/full_ob_cache_bridge_dumps"
    symbols_raw = os.environ.get("FULL_OB_CACHE_BRIDGE_SYMBOLS") or "BTCUSDT,DOGEUSDT"
    symbols = frozenset(s.strip().upper() for s in symbols_raw.split(",") if s.strip())

    max_pre_roll = _f("FULL_OB_CACHE_BRIDGE_MAX_PRE_ROLL_SEC", DEFAULT_DELTA_RETENTION_SEC)
    delta_retention = _f("FULL_OB_CACHE_BRIDGE_DELTA_RETENTION_SEC", max_pre_roll)
    interval = _f(
        "FULL_OB_CACHE_BRIDGE_CHECKPOINT_INTERVAL_SEC", DEFAULT_CHECKPOINT_INTERVAL_SEC
    )
    safety = _f(
        "FULL_OB_CACHE_BRIDGE_CHECKPOINT_SAFETY_MARGIN_SEC",
        DEFAULT_CHECKPOINT_SAFETY_MARGIN_SEC,
    )
    ck_retention = compute_checkpoint_retention_sec(
        delta_retention_sec=delta_retention,
        checkpoint_interval_sec=interval,
        safety_margin_sec=safety,
    )
    ck_retention = max(
        ck_retention,
        _f("FULL_OB_CACHE_BRIDGE_CHECKPOINT_RETENTION_SEC", ck_retention),
    )
    derived_max = compute_max_checkpoints(
        checkpoint_retention_sec=ck_retention, checkpoint_interval_sec=interval
    )
    env_max = _i("FULL_OB_CACHE_BRIDGE_MAX_CHECKPOINTS", derived_max)
    max_ck = max(derived_max, env_max)

    return CacheBridgeSettings(
        enabled=enabled,
        socket_path=Path(sock),
        dump_root=Path(root),
        symbols=symbols or frozenset({"BTCUSDT", "DOGEUSDT"}),
        max_pre_roll_seconds=max_pre_roll,
        min_pre_roll_seconds=_f("FULL_OB_CACHE_BRIDGE_MIN_PRE_ROLL_SEC", 30.0),
        delta_retention_seconds=delta_retention,
        checkpoint_interval_sec=interval,
        checkpoint_safety_margin_sec=safety,
        checkpoint_retention_seconds=ck_retention,
        max_payload_bytes=_i("FULL_OB_CACHE_BRIDGE_MAX_PAYLOAD_BYTES", 96 * 1024 * 1024),
        max_request_bytes=_i("FULL_OB_CACHE_BRIDGE_MAX_REQUEST_BYTES", 16_384),
        request_timeout_sec=_f("FULL_OB_CACHE_BRIDGE_REQUEST_TIMEOUT_SEC", 30.0),
        max_concurrent_requests=max(1, _i("FULL_OB_CACHE_BRIDGE_MAX_CONCURRENT", 1)),
        min_request_interval_sec=_f("FULL_OB_CACHE_BRIDGE_MIN_INTERVAL_SEC", 2.0),
        max_checkpoints_per_symbol=max_ck,
        max_checkpoint_bytes_per_symbol=_i(
            "FULL_OB_CACHE_BRIDGE_MAX_CHECKPOINT_BYTES", 48 * 1024 * 1024
        ),
        stale_after_ms=_i("FULL_OB_CACHE_BRIDGE_STALE_AFTER_MS", 15_000),
        socket_mode=0o600,
    )
