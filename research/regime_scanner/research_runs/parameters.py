"""Canonical research parameter sets and deterministic parameter hashing."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from research.regime_scanner.config import RegimeScannerConfig, default_regime_scanner_config
from research.regime_scanner.momentum import MomentumConfig, default_momentum_config
from research.regime_scanner.price_action import PriceActionConfig, default_price_action_config
from research.regime_scanner.research_runs.hashing import json_hash
from research.regime_scanner.trend_state_machine import TrendStateConfig, default_trend_state_config

SCANNER_NAME = "regime_scanner"
SCANNER_VERSION = "baseline_v1"

# Verified baseline parameter hash (March 2026 window, mysql, skip-pipeline).
BASELINE_PARAMETER_HASH = "46becb86a9e736ee07a1dab14df3a14a2f90d7fe600ec6d83df16e179556ea66"

# Whitelisted dot-path overrides for variant research (no arbitrary mutation).
ALLOWED_PARAMETER_PATHS: frozenset[str] = frozenset(
    {
        "trend_state.adx_confirm",
        "trend_state.di_spread_confirm",
        "trend_state.exit_opposite_closes",
        "trend_state.bearish_impulse_min_closes",
        "trend_state.bullish_impulse_min_closes",
        "trend_state.min_hold_bars.neutral",
        "trend_state.min_hold_bars.bearish_warning",
        "trend_state.min_hold_bars.early_bearish",
        "trend_state.min_hold_bars.strong_bearish",
        "trend_state.min_hold_bars.bearish_weakening",
        "trend_state.min_hold_bars.bottoming",
        "trend_state.min_hold_bars.bullish_warning",
        "trend_state.min_hold_bars.early_bullish",
        "trend_state.min_hold_bars.strong_bullish",
        "trend_state.min_hold_bars.bullish_weakening",
        "trend_state.min_hold_bars.topping",
        "trend_state.min_hold_bars.unavailable",
        "trend_state.structure.epsilon_pct",
        "trend_state.structure.valid_break_hold_bars",
        "trend_state.structure.retest_max_bars",
    }
)

_SECRET_KEY_FRAGMENTS = (
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "private_key",
)


@dataclass(frozen=True)
class ResearchParameterSet:
    scanner_name: str
    scanner_version: str
    exchange: str
    symbol: str
    data_source: str
    timeframes: tuple[str, ...]
    history_candles: int
    regime_scanner: RegimeScannerConfig
    trend_state: TrendStateConfig
    price_action: PriceActionConfig
    momentum: MomentumConfig

    def to_canonical_dict(self) -> dict[str, Any]:
        """Deterministic, secret-free parameter payload for hashing and storage."""
        return {
            "scanner_name": self.scanner_name,
            "scanner_version": self.scanner_version,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "data_source": self.data_source,
            "timeframes": list(self.timeframes),
            "history_candles": int(self.history_candles),
            "regime_scanner": _config_to_dict(self.regime_scanner),
            "trend_state": _config_to_dict(self.trend_state),
            "price_action": _config_to_dict(self.price_action),
            "momentum": _config_to_dict(self.momentum),
        }


def build_baseline_parameter_set(
    *,
    exchange: str = "bybit",
    symbol: str = "APTUSDT",
    data_source: str = "mysql",
    timeframes: tuple[str, ...] = ("5m", "15m", "30m"),
    history_candles: int = 144,
) -> ResearchParameterSet:
    scanner_cfg = default_regime_scanner_config().with_timeframe("5m")
    return ResearchParameterSet(
        scanner_name=SCANNER_NAME,
        scanner_version=SCANNER_VERSION,
        exchange=exchange.lower(),
        symbol=symbol.upper(),
        data_source=data_source,
        timeframes=timeframes,
        history_candles=int(history_candles),
        regime_scanner=scanner_cfg,
        trend_state=default_trend_state_config(),
        price_action=default_price_action_config(),
        momentum=default_momentum_config(),
    )


def parameter_hash(params: ResearchParameterSet) -> str:
    return json_hash(params.to_canonical_dict())


def apply_parameter_overrides(
    base: ResearchParameterSet,
    overrides: dict[str, object],
) -> ResearchParameterSet:
    """Return a new parameter set with whitelisted dot-path overrides applied."""
    if not overrides:
        return base
    trend_state = base.trend_state
    regime_scanner = base.regime_scanner
    price_action = base.price_action
    momentum = base.momentum
    for path, value in sorted(overrides.items()):
        key = str(path).strip()
        if key not in ALLOWED_PARAMETER_PATHS:
            raise ValueError(f"parameter override path not allowed: {key}")
        parts = key.split(".")
        if parts[0] == "trend_state" and parts[1] == "min_hold_bars":
            if len(parts) != 3:
                raise ValueError(f"invalid min_hold_bars path: {key}")
            hold_key = parts[2]
            holds = dict(trend_state.min_hold_bars)
            holds[hold_key] = int(value)
            trend_state = replace(trend_state, min_hold_bars=holds)
        elif parts[0] == "trend_state" and parts[1] == "structure":
            if len(parts) != 3:
                raise ValueError(f"invalid structure path: {key}")
            from research.regime_scanner.trend_structure import TrendStructureConfig

            struct = trend_state.structure
            struct = replace(struct, **{parts[2]: _coerce_field(struct, parts[2], value)})
            trend_state = replace(trend_state, structure=struct)
        elif parts[0] == "trend_state" and len(parts) == 2:
            trend_state = replace(
                trend_state, **{parts[1]: _coerce_field(trend_state, parts[1], value)}
            )
        else:
            raise ValueError(f"unsupported override path: {key}")
    return ResearchParameterSet(
        scanner_name=base.scanner_name,
        scanner_version=base.scanner_version,
        exchange=base.exchange,
        symbol=base.symbol,
        data_source=base.data_source,
        timeframes=base.timeframes,
        history_candles=base.history_candles,
        regime_scanner=regime_scanner,
        trend_state=trend_state,
        price_action=price_action,
        momentum=momentum,
    )


def assert_baseline_parameter_hash(params: ResearchParameterSet) -> str:
    phash = parameter_hash(params)
    if phash != BASELINE_PARAMETER_HASH:
        raise ValueError(
            "baseline parameter_hash mismatch: "
            f"expected {BASELINE_PARAMETER_HASH}, got {phash}"
        )
    return phash


def _coerce_field(obj: Any, field_name: str, value: object) -> object:
    if not hasattr(obj, field_name):
        raise ValueError(f"unknown field {field_name!r} on {type(obj).__name__}")
    current = getattr(obj, field_name)
    if isinstance(current, bool):
        if isinstance(value, bool):
            return value
        if str(value).lower() in {"true", "1", "yes"}:
            return True
        if str(value).lower() in {"false", "0", "no"}:
            return False
        raise ValueError(f"expected bool for {field_name}, got {value!r}")
    if isinstance(current, int) and not isinstance(current, bool):
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, str):
        return str(value)
    return value


def assert_no_secrets_in_parameters(payload: dict[str, Any]) -> None:
    """Raise if a key name suggests a secret (values are never logged)."""
    stack: list[Any] = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                lower = str(key).lower()
                if any(fragment in lower for fragment in _SECRET_KEY_FRAGMENTS):
                    raise ValueError(f"secret-like key in parameters: {key}")
                stack.append(value)
        elif isinstance(item, list):
            stack.extend(item)


def parameters_json(params: ResearchParameterSet) -> str:
    payload = params.to_canonical_dict()
    assert_no_secrets_in_parameters(payload)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _config_to_dict(cfg: Any) -> dict[str, Any]:
    if hasattr(cfg, "to_dict"):
        return cfg.to_dict()  # type: ignore[no-any-return]
    if hasattr(cfg, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(cfg)
    raise TypeError(f"unsupported config type: {type(cfg)!r}")
