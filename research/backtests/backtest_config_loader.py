"""Load live/fixed-cycle bot configs for backtests (Phase 10)."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Literal

from fixed_cycle_hedge_bot.fixed_cycle_strategy import FixedCycleHedgeConfig

Signal = Literal["long", "short"]
ConfigSource = Literal["test", "live", "file"]

DEFAULT_LONG_CONFIG_PATH = Path(
    "live_bots/100_50_hedge_bot/long_bot_1/config/fixed_cycle_config.json"
)
DEFAULT_SHORT_CONFIG_PATH = Path(
    "live_bots/short_hedge_bot/short_bot_1/config/fixed_cycle_config.json"
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]

HIGHLIGHT_BOT_CONFIG_KEYS = (
    "price_tick_size",
    "tp_profit_target_pct",
    "long_fill_distance_pct",
    "short_fill_distance_pct",
    "base_notional_usdt",
    "hedge_ratio_short",
    "recovery_activation_timing",
    "recovery_mode_trigger_override_enabled",
    "recovery_mode_trigger_override_pct",
    "time_distance_refill_trigger_minutes",
)

BACKTEST_RUNTIME_OVERRIDES = {
    "restart": False,
    "rest_poll_after_fill_ms": 0,
    "ws_enabled": False,
    "order_refresh_cooldown_ms": 0,
}


@dataclass(frozen=True)
class BacktestConfigLoadResult:
    config: FixedCycleHedgeConfig
    config_source: str
    config_path: str | None
    config_loaded: bool
    config_load_warning: str | None = None
    config_unknown_keys: tuple[str, ...] = ()
    config_overlay_missing_keys: tuple[str, ...] = ()
    loaded_json: dict[str, Any] | None = None

    def metadata_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "config_source": self.config_source,
            "config_path": self.config_path,
            "config_loaded": self.config_loaded,
            "config_load_warning": self.config_load_warning,
            "config_unknown_keys": list(self.config_unknown_keys),
            "config_overlay_missing_keys": list(self.config_overlay_missing_keys),
        }
        payload.update(extract_highlight_bot_config(self.config))
        return payload


def fixed_cycle_config_field_names() -> set[str]:
    return {field.name for field in fields(FixedCycleHedgeConfig)}


def extract_highlight_bot_config(config: FixedCycleHedgeConfig | object) -> dict[str, Any]:
    """Return important bot config keys using original LiveBot field names."""
    highlighted: dict[str, Any] = {}
    for key in HIGHLIGHT_BOT_CONFIG_KEYS:
        if not hasattr(config, key):
            continue
        value = getattr(config, key)
        if value is not None:
            highlighted[key] = value
    return highlighted


def _read_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"config must be a JSON object: {path}")
    return payload


def _build_test_config(*, signal: Signal, symbol: str) -> FixedCycleHedgeConfig:
    from .hedge_bot_original_simulator import build_test_config

    return build_test_config(signal=signal, symbol=symbol)


def load_fixed_cycle_config_for_backtest(
    path: str | Path,
    *,
    signal: Signal,
    symbol: str,
    project_root: Path | None = None,
) -> BacktestConfigLoadResult:
    """Load a live JSON config, overlay test defaults for missing keys, build dataclass."""
    root = project_root or PROJECT_ROOT
    path_obj = Path(path)
    if not path_obj.is_absolute():
        path_obj = root / path_obj

    warnings: list[str] = []
    if not path_obj.exists():
        warnings.append(f"config file not found: {path_obj}")
        defaults = _build_test_config(signal=signal, symbol=symbol.upper())
        return BacktestConfigLoadResult(
            config=defaults,
            config_source="file",
            config_path=str(path_obj),
            config_loaded=False,
            config_load_warning="; ".join(warnings),
        )

    try:
        loaded_json = _read_json_object(path_obj)
    except Exception as exc:
        defaults = _build_test_config(signal=signal, symbol=symbol.upper())
        return BacktestConfigLoadResult(
            config=defaults,
            config_source="file",
            config_path=str(path_obj),
            config_loaded=False,
            config_load_warning=f"failed to read config: {exc}",
        )

    field_names = fixed_cycle_config_field_names()
    unknown_keys = sorted(key for key in loaded_json if key not in field_names)

    default_config = _build_test_config(signal=signal, symbol=symbol.upper())
    default_dict = {
        field.name: getattr(default_config, field.name)
        for field in fields(FixedCycleHedgeConfig)
    }

    merged: dict[str, Any] = dict(default_dict)
    overlay_missing: list[str] = []
    for key, value in loaded_json.items():
        if key not in field_names:
            continue
        merged[key] = value

    for key in sorted(field_names):
        if key not in loaded_json:
            overlay_missing.append(key)

    json_symbol = loaded_json.get("symbol")
    merged["symbol"] = symbol.upper()
    if json_symbol and str(json_symbol).upper() != symbol.upper():
        warnings.append(f"symbol overridden from {json_symbol} to {symbol.upper()}")

    for key, value in BACKTEST_RUNTIME_OVERRIDES.items():
        if key in field_names:
            merged[key] = value

    if loaded_json.get("strategy_side") in {"long", "short"}:
        merged["strategy_side"] = loaded_json["strategy_side"]
    else:
        merged["strategy_side"] = signal

    if not merged.get("bot_name"):
        merged["bot_name"] = "long_bot_1" if signal == "long" else "short_bot_1"

    config = FixedCycleHedgeConfig(**merged)
    return BacktestConfigLoadResult(
        config=config,
        config_source="file",
        config_path=str(path_obj),
        config_loaded=True,
        config_load_warning="; ".join(warnings) if warnings else None,
        config_unknown_keys=tuple(unknown_keys),
        config_overlay_missing_keys=tuple(overlay_missing),
        loaded_json=loaded_json,
    )


def resolve_backtest_config(
    *,
    config_source: ConfigSource = "test",
    signal: Signal,
    symbol: str,
    long_config_path: str | Path = DEFAULT_LONG_CONFIG_PATH,
    short_config_path: str | Path = DEFAULT_SHORT_CONFIG_PATH,
    file_config_path: str | Path | None = None,
    project_root: Path | None = None,
) -> BacktestConfigLoadResult:
    """Resolve config for a backtest run from test/live/file source."""
    normalized_source = str(config_source or "test").strip().lower()
    symbol_upper = symbol.upper()

    if normalized_source == "test":
        config = _build_test_config(signal=signal, symbol=symbol_upper)
        for key, value in BACKTEST_RUNTIME_OVERRIDES.items():
            if hasattr(config, key):
                setattr(config, key, value)
        return BacktestConfigLoadResult(
            config=config,
            config_source="test",
            config_path=None,
            config_loaded=False,
            config_load_warning=None,
        )

    if normalized_source == "live":
        path = long_config_path if signal == "long" else short_config_path
        result = load_fixed_cycle_config_for_backtest(
            path,
            signal=signal,
            symbol=symbol_upper,
            project_root=project_root,
        )
        return BacktestConfigLoadResult(
            config=result.config,
            config_source="live",
            config_path=result.config_path,
            config_loaded=result.config_loaded,
            config_load_warning=result.config_load_warning,
            config_unknown_keys=result.config_unknown_keys,
            config_overlay_missing_keys=result.config_overlay_missing_keys,
            loaded_json=result.loaded_json,
        )

    if normalized_source == "file":
        if file_config_path is None:
            raise ValueError("--config-path is required when --config-source=file")
        result = load_fixed_cycle_config_for_backtest(
            file_config_path,
            signal=signal,
            symbol=symbol_upper,
            project_root=project_root,
        )
        return BacktestConfigLoadResult(
            config=result.config,
            config_source="file",
            config_path=result.config_path,
            config_loaded=result.config_loaded,
            config_load_warning=result.config_load_warning,
            config_unknown_keys=result.config_unknown_keys,
            config_overlay_missing_keys=result.config_overlay_missing_keys,
            loaded_json=result.loaded_json,
        )

    raise ValueError(f"unsupported config_source: {config_source}")


def apply_config_load_result_to_simulator(sim: object, load_result: BacktestConfigLoadResult) -> None:
    """Attach config load metadata to a simulator instance."""
    sim.config = load_result.config
    sim.config_source = load_result.config_source
    sim.config_path = load_result.config_path
    sim.config_loaded = load_result.config_loaded
    sim.config_load_warning = load_result.config_load_warning
    sim.config_unknown_keys = list(load_result.config_unknown_keys)
    sim.config_overlay_missing_keys = list(load_result.config_overlay_missing_keys)
    sim.loaded_bot_config = extract_highlight_bot_config(load_result.config)


def apply_config_load_result_to_backtest_result(
    result: object,
    load_result: BacktestConfigLoadResult,
) -> None:
    """Copy config load metadata onto a BacktestResult."""
    metadata = load_result.metadata_dict()
    for key, value in metadata.items():
        setattr(result, key, value)
