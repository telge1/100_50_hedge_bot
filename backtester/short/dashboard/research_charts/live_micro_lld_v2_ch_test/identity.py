"""Canonical PD / engine-input identity for isolated CH test runs (research only)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Sequence

from ..trp_import import ensure_trp_on_path

ENGINE_VERSION = "live_micro_lld_v2@trp"
CANDLE_SOURCE_LABEL = "clickhouse_candles_1m+trp_aggregate_strict"

# Phase-1 PD10 run — preserved for backward-compatible resolution.
LEGACY_RUN_OVERRIDES: dict[str, dict[str, Any]] = {
    "de5f74f6-236f-4dc1-81df-94e15dec1b83": {
        "from_close": "2026-09-08T11:20:00+00:00",
        "to_close": "2026-10-08T11:20:00+00:00",
        "pool_detail": 10,
        "symbol": "TUTUSDT",
        "timeframe": "5m",
    },
}


def build_live_micro_lld_v2_config(pool_detail: int):
    ensure_trp_on_path()
    from indicators.live_micro_lld_v2.config import LiveMicroLldV2Config

    return LiveMicroLldV2Config(pool_detail=int(pool_detail))


def effective_config_dict(cfg) -> dict[str, Any]:
    """All engine-effective fields (preset-resolved where applicable)."""
    return {
        "pool_detail": int(cfg.pool_detail),
        "manual_override": bool(cfg.manual_override),
        "swing_len": int(cfg.swing_len),
        "volume_lookback": int(cfg.volume_lookback),
        "volume_percentile": int(cfg.volume_percentile),
        "min_strength": float(cfg.min_strength),
        "zone_size_mult": float(cfg.zone_size_mult),
        "zone_extension": int(cfg.zone_extension),
        "box_amount": int(cfg.box_amount),
        "show_normal_pools": bool(cfg.show_normal_pools),
        "show_micro_pools": bool(cfg.show_micro_pools),
        "show_reclaim_pools": bool(cfg.show_reclaim_pools),
        "early_micro": bool(cfg.early_micro),
        "micro_left_bars": int(cfg.micro_left_bars),
        "early_timing": int(cfg.early_timing),
        "micro_strength_filter": bool(cfg.micro_strength_filter),
        "micro_min_strength": float(cfg.micro_min_strength),
        "reclaim_detection": bool(cfg.reclaim_detection),
        "reclaim_lookback": int(cfg.reclaim_lookback),
        "reclaim_proximity_pct": float(cfg.reclaim_proximity_pct),
        "require_reclaim": bool(cfg.require_reclaim),
        "confirmation_bars": int(cfg.confirmation_bars),
        "candidate_max_bars": int(cfg.candidate_max_bars),
        "duplicate_pct": float(cfg.duplicate_pct),
    }


def config_hash(cfg) -> str:
    payload = effective_config_dict(cfg)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def preset_matrix_entry(pool_detail: int) -> dict[str, Any]:
    cfg = build_live_micro_lld_v2_config(pool_detail)
    eff = effective_config_dict(cfg)
    return {
        "pool_detail": int(pool_detail),
        "preset_swing_len": eff["swing_len"],
        "preset_volume_lookback": eff["volume_lookback"],
        "preset_volume_percentile": eff["volume_percentile"],
        "preset_min_strength": eff["min_strength"],
        "preset_zone_size_mult": eff["zone_size_mult"],
        "preset_zone_extension": eff["zone_extension"],
        "config_hash": config_hash(cfg),
        "warmup_bars": int(eff["volume_lookback"]) + int(eff["swing_len"]) + 64,
    }


def all_preset_matrix() -> dict[int, dict[str, Any]]:
    return {pd: preset_matrix_entry(pd) for pd in range(1, 11)}


def candle_sequence_fingerprint(candles: Sequence) -> str:
    if not candles:
        return "empty"
    first = candles[0]
    last = candles[-1]
    payload = {
        "count": len(candles),
        "first_open": first.timestamp.isoformat(),
        "last_open": last.timestamp.isoformat(),
        "first_close": float(first.close),
        "last_close": float(last.close),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def engine_input_document(
    *,
    cfg,
    symbol: str,
    timeframe: str,
    from_close: datetime,
    to_close: datetime,
    candle_load_start: datetime,
    candles: Sequence,
    warmup_bars: int,
    history_policy: str | None = None,
) -> dict[str, Any]:
    eff = effective_config_dict(cfg)
    doc = {
        "symbol": str(symbol).upper(),
        "timeframe": str(timeframe),
        "pool_detail": int(cfg.pool_detail),
        "engine_version": ENGINE_VERSION,
        "candle_source": CANDLE_SOURCE_LABEL,
        "config_hash": config_hash(cfg),
        "effective_config": eff,
        "from_close": from_close.isoformat(),
        "to_close": to_close.isoformat(),
        "decision_close": to_close.isoformat(),
        "candle_load_start": candle_load_start.isoformat(),
        "warmup_bars": int(warmup_bars),
        "candle_count": len(candles),
        "candle_first_open": candles[0].timestamp.isoformat() if candles else None,
        "candle_last_open": candles[-1].timestamp.isoformat() if candles else None,
        "candle_fingerprint": candle_sequence_fingerprint(candles),
    }
    if history_policy:
        doc["history_policy"] = str(history_policy)
    return doc


def engine_input_hash(doc: dict[str, Any]) -> str:
    keys = (
        "symbol",
        "timeframe",
        "pool_detail",
        "engine_version",
        "candle_source",
        "config_hash",
        "history_policy",
        "from_close",
        "to_close",
        "candle_load_start",
        "warmup_bars",
        "candle_count",
        "candle_fingerprint",
    )
    payload = {k: doc.get(k) for k in keys}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()
