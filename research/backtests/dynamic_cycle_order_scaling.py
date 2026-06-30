"""Backtest-only dynamic cycle order scaling (optimizer-ready)."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Mapping

DEFAULT_BAND_REFERENCE_TARGET_PROFIT_PCT = 0.25


@dataclass(frozen=True)
class CycleScalingBand:
    min_cycle_index: int
    max_cycle_index: int | None
    target_profit_pct: float
    cycle_qty_factor: float
    long_add_distance_pct: float


@dataclass
class DynamicCycleOrderScalingConfig:
    enabled: bool = False
    start_cycle_index: int = 3
    bands: list[CycleScalingBand] = field(default_factory=list)
    min_cycle_qty_factor: float = 0.25
    max_short_reduce_distance_pct: float = 3.0
    reachability_guard_enabled: bool = False
    name: str = "manual_default"


@dataclass(frozen=True)
class CycleScalingParams:
    target_profit_pct: float
    cycle_qty_factor: float
    long_add_distance_pct: float
    band: CycleScalingBand


def default_dynamic_cycle_order_scaling_bands() -> list[CycleScalingBand]:
    return [
        CycleScalingBand(1, 2, 0.25, 1.00, 0.50),
        CycleScalingBand(3, 3, 0.20, 0.85, 0.45),
        CycleScalingBand(4, 4, 0.15, 0.70, 0.40),
        CycleScalingBand(5, 5, 0.10, 0.60, 0.35),
        CycleScalingBand(6, None, 0.05, 0.50, 0.30),
    ]


def default_dynamic_cycle_order_scaling_config() -> DynamicCycleOrderScalingConfig:
    return DynamicCycleOrderScalingConfig(
        enabled=True,
        start_cycle_index=3,
        bands=default_dynamic_cycle_order_scaling_bands(),
    )


def _normalize_band(raw: Mapping[str, Any]) -> CycleScalingBand:
    max_cycle = raw.get("max_cycle_index")
    return CycleScalingBand(
        min_cycle_index=int(raw["min_cycle_index"]),
        max_cycle_index=int(max_cycle) if max_cycle is not None else None,
        target_profit_pct=float(raw["target_profit_pct"]),
        cycle_qty_factor=float(raw["cycle_qty_factor"]),
        long_add_distance_pct=float(raw["long_add_distance_pct"]),
    )


def config_from_dict(payload: Mapping[str, Any]) -> DynamicCycleOrderScalingConfig:
    bands_raw = payload.get("bands")
    bands = (
        [_normalize_band(item) for item in bands_raw]
        if isinstance(bands_raw, list)
        else default_dynamic_cycle_order_scaling_bands()
    )
    return DynamicCycleOrderScalingConfig(
        enabled=bool(payload.get("enabled", False)),
        start_cycle_index=int(payload.get("start_cycle_index", 3)),
        bands=bands,
        min_cycle_qty_factor=float(payload.get("min_cycle_qty_factor", 0.25)),
        max_short_reduce_distance_pct=float(
            payload.get("max_short_reduce_distance_pct", 3.0)
        ),
        reachability_guard_enabled=bool(payload.get("reachability_guard_enabled", False)),
        name=str(payload.get("name") or "manual_default"),
    )


def config_to_dict(config: DynamicCycleOrderScalingConfig) -> dict[str, Any]:
    payload = asdict(config)
    payload["bands"] = [asdict(band) for band in config.bands]
    return payload


def config_from_json_string(raw: str) -> DynamicCycleOrderScalingConfig:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("dynamic cycle scaling config JSON must be an object")
    return config_from_dict(payload)


def _band_matches(band: CycleScalingBand, cycle_index: int) -> bool:
    if cycle_index < band.min_cycle_index:
        return False
    if band.max_cycle_index is not None and cycle_index > band.max_cycle_index:
        return False
    return True


def get_cycle_scaling_band(
    config: DynamicCycleOrderScalingConfig,
    cycle_index: int,
) -> CycleScalingBand | None:
    for band in config.bands:
        if _band_matches(band, cycle_index):
            return band
    return None


def scaling_applies(config: DynamicCycleOrderScalingConfig, cycle_index: int) -> bool:
    return bool(config.enabled and cycle_index >= int(config.start_cycle_index))


def get_cycle_scaling_params(
    config: DynamicCycleOrderScalingConfig,
    cycle_index: int,
) -> CycleScalingParams | None:
    band = get_cycle_scaling_band(config, cycle_index)
    if band is None:
        return None
    qty_factor = max(float(band.cycle_qty_factor), float(config.min_cycle_qty_factor))
    return CycleScalingParams(
        target_profit_pct=float(band.target_profit_pct),
        cycle_qty_factor=qty_factor,
        long_add_distance_pct=float(band.long_add_distance_pct),
        band=band,
    )


def get_cycle_target_profit_pct(
    config: DynamicCycleOrderScalingConfig,
    cycle_index: int,
) -> float | None:
    params = get_cycle_scaling_params(config, cycle_index)
    return params.target_profit_pct if params is not None else None


def get_cycle_long_add_distance_pct(
    config: DynamicCycleOrderScalingConfig,
    cycle_index: int,
) -> float | None:
    params = get_cycle_scaling_params(config, cycle_index)
    return params.long_add_distance_pct if params is not None else None


def _normalize_qty_with_rules(qty: float, symbol_rules: Mapping[str, Any] | None) -> float:
    if qty <= 0:
        return 0.0
    if not symbol_rules:
        return qty
    step = float(symbol_rules.get("qty_step") or 0.0)
    min_qty = float(symbol_rules.get("min_order_qty") or 0.0)
    if step > 0:
        qty = math.floor(qty / step) * step
    if min_qty > 0 and qty < min_qty:
        return 0.0
    return qty


def scale_cycle_qty(
    original_qty: float,
    config: DynamicCycleOrderScalingConfig,
    cycle_index: int,
    *,
    symbol_rules: Mapping[str, Any] | None = None,
) -> float:
    if original_qty <= 0 or not scaling_applies(config, cycle_index):
        return original_qty
    params = get_cycle_scaling_params(config, cycle_index)
    if params is None:
        return original_qty
    scaled = float(original_qty) * float(params.cycle_qty_factor)
    if symbol_rules and "qty_step" in symbol_rules:
        rules = {
            "qty_step": float(symbol_rules.get("qty_step") or 0.0),
            "min_order_qty": float(symbol_rules.get("min_order_qty") or 0.0),
        }
        return _normalize_qty_with_rules(scaled, rules)
    return scaled


def compute_scaled_target_profit_usdt(
    baseline_target_profit_usdt: float,
    target_profit_pct: float,
    *,
    reference_target_profit_pct: float = DEFAULT_BAND_REFERENCE_TARGET_PROFIT_PCT,
) -> float:
    baseline = float(baseline_target_profit_usdt or 0.0)
    if baseline <= 0 or reference_target_profit_pct <= 0:
        return baseline
    return baseline * (float(target_profit_pct) / float(reference_target_profit_pct))


def build_dynamic_cycle_debug_metadata(
    *,
    config: DynamicCycleOrderScalingConfig,
    cycle_index: int,
    purpose: str,
    params: CycleScalingParams | None,
    planned_cycle_qty_before_scaling: float | None = None,
    planned_cycle_qty_after_scaling: float | None = None,
    planned_long_add_distance_pct_before_scaling: float | None = None,
    planned_long_add_distance_pct_after_scaling: float | None = None,
    cycle_scaling_reason: str = "dynamic_cycle_order_scaling",
) -> dict[str, Any]:
    band = params.band if params is not None else get_cycle_scaling_band(config, cycle_index)
    return {
        "dynamic_cycle_order_scaling_enabled": bool(config.enabled),
        "dynamic_cycle_scaling_config_name": config.name,
        "cycle_target_profit_pct_used": params.target_profit_pct if params else None,
        "cycle_qty_factor_used": params.cycle_qty_factor if params else None,
        "cycle_long_add_distance_pct_used": params.long_add_distance_pct if params else None,
        "planned_cycle_qty_before_scaling": planned_cycle_qty_before_scaling,
        "planned_cycle_qty_after_scaling": planned_cycle_qty_after_scaling,
        "planned_long_add_distance_pct_before_scaling": planned_long_add_distance_pct_before_scaling,
        "planned_long_add_distance_pct_after_scaling": planned_long_add_distance_pct_after_scaling,
        "cycle_scaling_started": bool(scaling_applies(config, cycle_index)),
        "cycle_scaling_reason": cycle_scaling_reason if scaling_applies(config, cycle_index) else "",
        "dynamic_cycle_scaling_band": asdict(band) if band is not None else None,
        "cycle_index": cycle_index,
        "purpose": purpose,
    }


def symbol_rules_from_runtime(runtime_state: Any) -> dict[str, float] | None:
    instrument_rules = getattr(runtime_state, "instrument_rules", None) or {}
    if not instrument_rules:
        return None
    for rules in instrument_rules.values():
        if not rules:
            continue
        return {
            "qty_step": float(Decimal(str(rules.get("qty_step") or 0))),
            "min_order_qty": float(Decimal(str(rules.get("min_order_qty") or 0))),
            "tick_size": float(Decimal(str(rules.get("tick_size") or 0))),
        }
    return None
