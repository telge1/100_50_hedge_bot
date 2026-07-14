"""Liquidation-level research config load / validate / hash / grid helpers.

Field meanings (schema_version 1):
- reference_price: candle reference for level placement (open/close/hl2/…)
- volume_sma_period: SMA window for volume flags
- volume_threshold: LuxAlgo volume multiplier (nzVd0 base)
- volatility_threshold: LuxAlgo wick/volatility trigger threshold
- leverages: leverage set used to place upper/lower levels
- cluster_distance_pct: max relative gap when clustering active levels
- cluster_min_level_count / cluster_min_total_strength: cluster filters
- max_active_levels: FIFO cap on concurrent active levels
- minimum_move_divisor: Pine eC min-move divisor (baseline 333)
- sweep_strict_cross: high > level and low < level (baseline true)
- reclaim_window_candles: max candles after sweep for delayed bearish reclaim
- path_horizon_candles: default path excursion horizon
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, fields, replace
from itertools import product
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from research.liquidation_level.liquidation_levels import (
    REFERENCE_PRICE_MODES,
    LiquidationLevelConfig,
)

SCHEMA_VERSION = 1
BASELINE_CONFIG_NAME = "luxalgo_baseline"
CODE_SCHEMA_VERSION = "liquidation_optimizer_v1"

CANONICAL_KEYS = (
    "schema_version",
    "reference_price",
    "volume_sma_period",
    "volume_threshold",
    "volatility_threshold",
    "leverages",
    "cluster_distance_pct",
    "cluster_min_level_count",
    "cluster_min_total_strength",
    "max_active_levels",
    "minimum_move_divisor",
    "sweep_strict_cross",
    "reclaim_window_candles",
    "path_horizon_candles",
)

LEVEL_GENERATION_KEYS = (
    "reference_price",
    "volume_sma_period",
    "volume_threshold",
    "volatility_threshold",
    "leverages",
    "max_active_levels",
    "minimum_move_divisor",
    "sweep_strict_cross",
)

CLUSTER_EVALUATION_KEYS = (
    "cluster_distance_pct",
    "cluster_min_level_count",
    "cluster_min_total_strength",
)


def baseline_config() -> LiquidationLevelConfig:
    """Exact LuxAlgo research baseline defaults."""
    return LiquidationLevelConfig()


def config_to_canonical_dict(config: LiquidationLevelConfig) -> dict[str, Any]:
    """Stable, sorted canonical representation for hashing / identity."""
    validate_liquidation_config(config)
    return {
        "schema_version": SCHEMA_VERSION,
        "reference_price": str(config.reference_price),
        "volume_sma_period": int(config.volume_sma_period),
        "volume_threshold": float(config.volume_threshold),
        "volatility_threshold": float(config.volatility_threshold),
        "leverages": [int(x) for x in sorted({int(v) for v in config.leverages})],
        "cluster_distance_pct": float(config.cluster_distance_pct),
        "cluster_min_level_count": int(config.cluster_min_level_count),
        "cluster_min_total_strength": int(config.cluster_min_total_strength),
        "max_active_levels": int(config.max_active_levels),
        "minimum_move_divisor": float(config.minimum_move_divisor),
        "sweep_strict_cross": bool(config.sweep_strict_cross),
        "reclaim_window_candles": int(config.reclaim_window_candles),
        "path_horizon_candles": int(config.path_horizon_candles),
    }


def config_hash(config: LiquidationLevelConfig) -> str:
    payload = json.dumps(config_to_canonical_dict(config), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def level_generation_key(config: LiquidationLevelConfig) -> str:
    d = config_to_canonical_dict(config)
    payload = {k: d[k] for k in LEVEL_GENERATION_KEYS}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def cluster_evaluation_key(config: LiquidationLevelConfig) -> str:
    d = config_to_canonical_dict(config)
    payload = {k: d[k] for k in CLUSTER_EVALUATION_KEYS}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def validate_liquidation_config(config: LiquidationLevelConfig) -> None:
    mode = str(config.reference_price).strip().lower()
    if mode not in REFERENCE_PRICE_MODES:
        raise ValueError(
            f"reference_price={config.reference_price!r} invalid; "
            f"allowed={REFERENCE_PRICE_MODES}"
        )
    if int(config.volume_sma_period) < 1:
        raise ValueError("volume_sma_period must be >= 1")
    if float(config.volume_threshold) <= 0:
        raise ValueError("volume_threshold must be > 0")
    if float(config.volatility_threshold) < 0:
        raise ValueError("volatility_threshold must be >= 0")
    levs = [int(x) for x in config.leverages]
    if not levs:
        raise ValueError("leverages must be non-empty")
    if any(x <= 0 for x in levs):
        raise ValueError("all leverages must be positive integers")
    if len(levs) != len(set(levs)):
        raise ValueError(f"duplicate leverages not allowed: {levs}")
    if float(config.cluster_distance_pct) <= 0:
        raise ValueError("cluster_distance_pct must be > 0")
    if int(config.cluster_min_level_count) < 1:
        raise ValueError("cluster_min_level_count must be >= 1")
    if int(config.cluster_min_total_strength) < 1:
        raise ValueError("cluster_min_total_strength must be >= 1")
    if int(config.max_active_levels) < 1:
        raise ValueError("max_active_levels must be >= 1")
    if float(config.minimum_move_divisor) <= 0:
        raise ValueError("minimum_move_divisor must be > 0")
    if not isinstance(config.sweep_strict_cross, (bool, int)):
        raise ValueError("sweep_strict_cross must be boolean")
    if int(config.reclaim_window_candles) < 1:
        raise ValueError("reclaim_window_candles must be >= 1")
    if int(config.path_horizon_candles) < 1:
        raise ValueError("path_horizon_candles must be >= 1")


def liquidation_config_from_mapping(raw: Mapping[str, Any]) -> LiquidationLevelConfig:
    """Build config from JSON-like mapping; unknown keys ignored except errors on bad types."""
    base = baseline_config()
    data = dict(raw)
    # aliases
    if "leverage_sets" in data and "leverages" not in data:
        data["leverages"] = data["leverage_sets"]
    if "cluster_max_gap_pct" in data and "cluster_distance_pct" not in data:
        data["cluster_distance_pct"] = data["cluster_max_gap_pct"]

    kwargs: dict[str, Any] = {}
    field_names = {f.name for f in fields(LiquidationLevelConfig)}
    for key in field_names:
        if key in data:
            kwargs[key] = data[key]
    if "leverages" in kwargs:
        kwargs["leverages"] = tuple(int(x) for x in kwargs["leverages"])
    cfg = replace(base, **kwargs) if kwargs else base
    validate_liquidation_config(cfg)
    return cfg


def load_liquidation_config(path: str | Path) -> LiquidationLevelConfig:
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"config root must be object: {p}")
    return liquidation_config_from_mapping(raw)


def save_liquidation_config(path: str | Path, config: LiquidationLevelConfig, *, name: str | None = None) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = config_to_canonical_dict(config)
    if name:
        payload = {"name": name, **payload}
    p.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_optimizer_grid(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("grid config root must be object")
    if "grid" not in raw or "fixed" not in raw:
        raise ValueError("grid config requires 'fixed' and 'grid' sections")
    return raw


def expand_grid_configurations(grid_cfg: Mapping[str, Any]) -> list[LiquidationLevelConfig]:
    """Cartesian product of grid axes with fixed fields applied."""
    fixed = dict(grid_cfg.get("fixed") or {})
    grid = dict(grid_cfg.get("grid") or {})

    ref = list(grid.get("reference_price") or ["open"])
    vol_thr = list(grid.get("volume_threshold") or [1.7])
    vola_thr = list(grid.get("volatility_threshold") or [10])
    lev_sets = list(grid.get("leverage_sets") or [[25, 50, 100]])
    cluster_d = list(grid.get("cluster_distance_pct") or [0.10])
    cluster_n = list(grid.get("cluster_min_level_count") or [2])
    cluster_s = list(grid.get("cluster_min_total_strength") or [3])

    out: list[LiquidationLevelConfig] = []
    seen: set[str] = set()
    for vals in product(ref, vol_thr, vola_thr, lev_sets, cluster_d, cluster_n, cluster_s):
        rp, vt, va, levs, cd, cn, cs = vals
        payload = {
            **fixed,
            "reference_price": rp,
            "volume_threshold": vt,
            "volatility_threshold": va,
            "leverages": list(levs),
            "cluster_distance_pct": cd,
            "cluster_min_level_count": cn,
            "cluster_min_total_strength": cs,
        }
        cfg = liquidation_config_from_mapping(payload)
        hid = config_hash(cfg)
        if hid in seen:
            continue
        seen.add(hid)
        out.append(cfg)
    return out


def grid_axis_counts(grid_cfg: Mapping[str, Any]) -> dict[str, int]:
    grid = dict(grid_cfg.get("grid") or {})
    return {
        "reference_price": len(grid.get("reference_price") or []),
        "volume_threshold": len(grid.get("volume_threshold") or []),
        "volatility_threshold": len(grid.get("volatility_threshold") or []),
        "leverage_sets": len(grid.get("leverage_sets") or []),
        "cluster_distance_pct": len(grid.get("cluster_distance_pct") or []),
        "cluster_min_level_count": len(grid.get("cluster_min_level_count") or []),
        "cluster_min_total_strength": len(grid.get("cluster_min_total_strength") or []),
    }


def select_screening_configurations(
    all_configs: Sequence[LiquidationLevelConfig],
    *,
    max_configs: int,
    seed: int,
    always_include_baseline: bool = True,
) -> list[LiquidationLevelConfig]:
    """Deterministic subset covering the grid; always includes baseline when requested."""
    if max_configs < 1:
        raise ValueError("max_configs must be >= 1")
    base = baseline_config()
    base_hash = config_hash(base)
    by_hash = {config_hash(c): c for c in all_configs}
    ordered_hashes = sorted(by_hash.keys())

    selected: list[str] = []
    if always_include_baseline:
        # prefer exact baseline object even if not in expanded grid
        selected.append(base_hash)
        by_hash.setdefault(base_hash, base)

    # Stratify: pick evenly across sorted hash space then fill with seeded RNG
    need = max_configs
    if len(ordered_hashes) <= need:
        picks = list(ordered_hashes)
    else:
        step = len(ordered_hashes) / float(need)
        evenly = [ordered_hashes[min(len(ordered_hashes) - 1, int(i * step))] for i in range(need)]
        picks = list(dict.fromkeys(evenly))  # preserve order, unique
        # fill remaining with seeded shuffle of leftovers
        import numpy as np

        rng = np.random.default_rng(int(seed))
        leftover = [h for h in ordered_hashes if h not in picks]
        rng.shuffle(leftover)
        for h in leftover:
            if len(picks) >= need:
                break
            picks.append(h)

    # ensure baseline first
    final_hashes: list[str] = []
    if always_include_baseline and base_hash not in picks:
        picks = [base_hash] + picks
    for h in picks:
        if h not in final_hashes:
            final_hashes.append(h)
        if len(final_hashes) >= need:
            break
    # move baseline to front
    if always_include_baseline and base_hash in final_hashes:
        final_hashes = [base_hash] + [h for h in final_hashes if h != base_hash]

    return [by_hash[h] for h in final_hashes[:need]]


def build_refinement_grid(
    top_configs: Sequence[LiquidationLevelConfig],
    full_grid_cfg: Mapping[str, Any],
) -> dict[str, Any]:
    """Export a neighborhood refinement grid around Stage-A winners (not auto-run)."""
    refs = sorted({c.reference_price for c in top_configs})
    vol_t = sorted({float(c.volume_threshold) for c in top_configs})
    vola = sorted({float(c.volatility_threshold) for c in top_configs})
    levs = []
    seen_l = set()
    for c in top_configs:
        key = tuple(sorted(c.leverages))
        if key not in seen_l:
            seen_l.add(key)
            levs.append(list(key))
    cluster_d = sorted({float(c.cluster_distance_pct) for c in top_configs})
    cluster_n = sorted({int(c.cluster_min_level_count) for c in top_configs})
    cluster_s = sorted({int(c.cluster_min_total_strength) for c in top_configs})
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_name": f"{full_grid_cfg.get('experiment_name', 'grid')}_refinement",
        "search_mode": "grid",
        "fixed": dict(full_grid_cfg.get("fixed") or {}),
        "grid": {
            "reference_price": refs,
            "volume_threshold": vol_t,
            "volatility_threshold": vola,
            "leverage_sets": levs,
            "cluster_distance_pct": cluster_d,
            "cluster_min_level_count": cluster_n,
            "cluster_min_total_strength": cluster_s,
        },
        "evaluation": dict(full_grid_cfg.get("evaluation") or {}),
        "note": "Stage-B refinement neighborhood around Stage-A winners; not auto-started.",
    }
