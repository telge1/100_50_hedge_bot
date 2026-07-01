"""Backtest-only cycle short-TP distance relief configuration and helpers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class CycleShortTpReliefBand:
    min_cycle_index: int
    max_cycle_index: int | None
    max_short_reduce_distance_pct_from_long_fill: float


@dataclass
class CycleShortTpReliefConfig:
    enabled: bool = False
    start_cycle_index: int = 4
    max_short_reduce_distance_pct_from_long_fill: float = 4.0
    carry_uncovered_loss_to_exit: bool = True
    bands: list[CycleShortTpReliefBand] = field(default_factory=list)
    name: str = "default"


@dataclass(frozen=True)
class ShortTpReliefComputation:
    cycle_index: int
    long_fill_price: float
    normal_short_reduce_price: float
    capped_short_reduce_price: float
    required_profit: float
    covered_profit: float
    uncovered_loss: float
    short_avg_price: float
    short_reduce_qty: float
    cap_applied: bool
    max_distance_pct_from_long_fill: float


def default_cycle_short_tp_relief_config() -> CycleShortTpReliefConfig:
    return CycleShortTpReliefConfig(
        enabled=True,
        start_cycle_index=4,
        max_short_reduce_distance_pct_from_long_fill=4.0,
        carry_uncovered_loss_to_exit=True,
        name="default",
    )


def _normalize_band(raw: Mapping[str, Any]) -> CycleShortTpReliefBand:
    max_cycle = raw.get("max_cycle_index")
    return CycleShortTpReliefBand(
        min_cycle_index=int(raw["min_cycle_index"]),
        max_cycle_index=int(max_cycle) if max_cycle is not None else None,
        max_short_reduce_distance_pct_from_long_fill=float(
            raw["max_short_reduce_distance_pct_from_long_fill"]
        ),
    )


def config_from_dict(payload: Mapping[str, Any]) -> CycleShortTpReliefConfig:
    bands_raw = payload.get("bands")
    bands = (
        [_normalize_band(item) for item in bands_raw]
        if isinstance(bands_raw, list)
        else []
    )
    return CycleShortTpReliefConfig(
        enabled=bool(payload.get("enabled", False)),
        start_cycle_index=int(payload.get("start_cycle_index", 4)),
        max_short_reduce_distance_pct_from_long_fill=float(
            payload.get("max_short_reduce_distance_pct_from_long_fill", 4.0)
        ),
        carry_uncovered_loss_to_exit=bool(payload.get("carry_uncovered_loss_to_exit", True)),
        bands=bands,
        name=str(payload.get("name") or "manual_default"),
    )


def config_from_json_string(raw: str) -> CycleShortTpReliefConfig:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("cycle short tp relief config JSON must be an object")
    return config_from_dict(payload)


def config_to_dict(config: CycleShortTpReliefConfig) -> dict[str, Any]:
    payload = asdict(config)
    payload["bands"] = [asdict(band) for band in config.bands]
    return payload


def _band_matches(band: CycleShortTpReliefBand, cycle_index: int) -> bool:
    if cycle_index < band.min_cycle_index:
        return False
    if band.max_cycle_index is not None and cycle_index > band.max_cycle_index:
        return False
    return True


def get_max_distance_pct(config: CycleShortTpReliefConfig, cycle_index: int) -> float | None:
    for band in config.bands:
        if _band_matches(band, cycle_index):
            return float(band.max_short_reduce_distance_pct_from_long_fill)
    if cycle_index >= int(config.start_cycle_index):
        return float(config.max_short_reduce_distance_pct_from_long_fill)
    return None


def relief_applies(config: CycleShortTpReliefConfig, cycle_index: int) -> bool:
    if not config.enabled:
        return False
    return get_max_distance_pct(config, cycle_index) is not None


def compute_short_tp_relief(
    *,
    cycle_index: int,
    long_fill_price: float,
    normal_short_reduce_price: float,
    short_avg_price: float,
    short_reduce_qty: float,
    required_profit: float,
    max_distance_pct_from_long_fill: float,
) -> ShortTpReliefComputation:
    capped_short_reduce_price = long_fill_price * (1.0 - max_distance_pct_from_long_fill / 100.0)
    cap_applied = normal_short_reduce_price + 1e-12 < capped_short_reduce_price
    final_short_price = (
        capped_short_reduce_price if cap_applied else normal_short_reduce_price
    )
    covered_profit = max(short_reduce_qty * (short_avg_price - final_short_price), 0.0)
    uncovered_loss = (
        max(short_reduce_qty * (capped_short_reduce_price - normal_short_reduce_price), 0.0)
        if cap_applied
        else 0.0
    )
    return ShortTpReliefComputation(
        cycle_index=cycle_index,
        long_fill_price=long_fill_price,
        normal_short_reduce_price=normal_short_reduce_price,
        capped_short_reduce_price=capped_short_reduce_price,
        required_profit=required_profit,
        covered_profit=covered_profit,
        uncovered_loss=uncovered_loss,
        short_avg_price=short_avg_price,
        short_reduce_qty=short_reduce_qty,
        cap_applied=cap_applied,
        max_distance_pct_from_long_fill=max_distance_pct_from_long_fill,
    )
