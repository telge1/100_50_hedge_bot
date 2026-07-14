"""Phase B: sweep-activated causal analysis windows (no entry / classification).

Follow candles after each validated upper 50x sweep. Window sizes 3 / 6 / 12.
Reuses Phase A feature-store causality; does not modify regime_scanner.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from research.liquidation_level.leverage_rebound_audit import close_relative_to_level_pct
from research.liquidation_level.liquidation_control_validation import (
    EXPECTED_FULL,
    EXPECTED_IS,
    EXPECTED_OOS,
)
from research.liquidation_level.liquidation_levels import SIDE_UPPER
from research.liquidation_level.sweep_scanner_join import (
    SOURCE_CONFIG_ID,
    EventCountMismatchError,
    ScannerFeatureStore,
    SweepScannerSnapshot,
    SweepTriggerEvent,
    _feature_pack_from_indicator_row,
    _finite,
    _last_closed_htf_index,
    _regime_label_from_row,
    _SLOPE_EXPORT,
    decision_time_from_signal_open,
    ensure_utc,
    join_sweep_event,
    precompute_scanner_feature_store,
    reproduce_winner_events,
    select_timeline_event_indices,
    validation_events_to_triggers,
)
from research.regime_scanner.timeframes import timeframe_timedelta

DEFAULT_WINDOW_SIZES: tuple[int, ...] = (3, 6, 12)

STATE_IDLE = "IDLE"
STATE_SWEEP_DETECTED = "SWEEP_DETECTED"
STATE_ANALYSIS_ACTIVE = "ANALYSIS_ACTIVE"
STATE_WINDOW_COMPLETED = "WINDOW_COMPLETED"
STATE_INCOMPLETE_END_OF_DATA = "INCOMPLETE_END_OF_DATA"
STATE_INVALIDATED = "INVALIDATED"

FORBIDDEN_STATES = frozenset({"SHORT_CONFIRMED", "LONG_CONTINUATION_CONFIRMED"})

_FEATURE_KEYS = (
    "ema_9",
    "ema_20",
    "ema_59",
    "ema_200",
    "ema_9_20_distance",
    "ema_20_59_distance",
    "adx",
    "di_plus",
    "di_minus",
    "atr",
    "atr_pct",
    "regime",
    "structure_bias",
    "structure_pair",
    "hh",
    "hl",
    "lh",
    "ll",
    "last_bos",
    "last_choch",
    "last_failed_breakout",
    "last_failed_breakdown",
    "retest_level",
    "retest_direction",
    "raw_volume",
    "volume_ratio",
) + _SLOPE_EXPORT


def _rel_pct(price: float, level: float) -> float:
    return float(close_relative_to_level_pct(SIDE_UPPER, float(price), float(level)))


def level_behavior_for_bar(
    *,
    open_: float,
    high: float,
    low: float,
    close: float,
    level: float,
) -> dict[str, Any]:
    """Observational only — no classification / entry semantics."""
    lvl = float(level)
    o, h, l, c = float(open_), float(high), float(low), float(close)
    touched = l <= lvl <= h
    crossed = (o < lvl and c > lvl) or (o > lvl and c < lvl) or (h > lvl and l < lvl and o != c)
    # reclaim: traded at/above level, closed below
    traded_at_or_above = h >= lvl or o >= lvl
    reclaimed_below = bool(traded_at_or_above and c < lvl)
    accepted_above = bool(c > lvl)
    rejected = bool(h >= lvl and c < lvl)
    return {
        "open_relative_to_level_pct": _rel_pct(o, lvl),
        "high_relative_to_level_pct": _rel_pct(h, lvl),
        "low_relative_to_level_pct": _rel_pct(l, lvl),
        "close_relative_to_level_pct": _rel_pct(c, lvl),
        "close_above_level": bool(c > lvl),
        "close_below_level": bool(c < lvl),
        "high_above_level": bool(h > lvl),
        "low_below_level": bool(l < lvl),
        "touched_level": bool(touched),
        "crossed_level": bool(crossed),
        "reclaimed_below_level": reclaimed_below,
        "accepted_above_level_candidate": accepted_above,
        "rejected_from_level_candidate": rejected,
    }


@dataclass
class SweepAnalysisWindow:
    event_id: str
    source_config_id: str
    signal_index: int
    signal_timestamp: pd.Timestamp
    sample: str
    window_size: int
    start_index: int
    start_timestamp: pd.Timestamp | None
    end_index: int | None
    end_timestamp: pd.Timestamp | None
    status: str
    available_candle_count: int
    expected_candle_count: int
    complete: bool
    invalidation_reason: str | None
    initial_sweep_level: float | None
    initial_cluster_center_price: float | None
    initial_close_relative_to_level_pct: float | None
    frozen_context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["signal_timestamp"] = ensure_utc(self.signal_timestamp).isoformat()
        d["start_timestamp"] = (
            None if self.start_timestamp is None else ensure_utc(self.start_timestamp).isoformat()
        )
        d["end_timestamp"] = (
            None if self.end_timestamp is None else ensure_utc(self.end_timestamp).isoformat()
        )
        return d


@dataclass
class SweepAnalysisBar:
    event_id: str
    window_size: int
    window_offset: int
    candle_index: int
    timestamp: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool
    available_at: pd.Timestamp
    state_before: str
    state_after: str
    level_behavior: dict[str, Any]
    frozen_5m: dict[str, Any]
    frozen_15m: dict[str, Any]
    frozen_30m: dict[str, Any]
    current_5m: dict[str, Any]
    current_15m: dict[str, Any]
    current_30m: dict[str, Any]
    deltas: dict[str, Any]
    htf_15m: dict[str, Any]
    htf_30m: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "window_size": self.window_size,
            "window_offset": self.window_offset,
            "candle_index": self.candle_index,
            "timestamp": ensure_utc(self.timestamp).isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "is_closed": self.is_closed,
            "available_at": ensure_utc(self.available_at).isoformat(),
            "state_before": self.state_before,
            "state_after": self.state_after,
            **{f"lvl_{k}": v for k, v in self.level_behavior.items()},
            **{f"frozen_5m_{k}": v for k, v in self.frozen_5m.items()},
            **{f"frozen_15m_{k}": v for k, v in self.frozen_15m.items()},
            **{f"frozen_30m_{k}": v for k, v in self.frozen_30m.items()},
            **{f"current_5m_{k}": v for k, v in self.current_5m.items()},
            **{f"current_15m_{k}": v for k, v in self.current_15m.items()},
            **{f"current_30m_{k}": v for k, v in self.current_30m.items()},
            **{f"delta_{k}": v for k, v in self.deltas.items()},
            **{f"htf15_{k}": v for k, v in self.htf_15m.items()},
            **{f"htf30_{k}": v for k, v in self.htf_30m.items()},
        }


@dataclass
class WindowPathMetrics:
    event_id: str
    window_size: int
    status: str
    complete: bool
    available_candle_count: int
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "window_size": self.window_size,
            "status": self.status,
            "complete": self.complete,
            "available_candle_count": self.available_candle_count,
            **self.metrics,
        }


@dataclass
class AnalysisWindowBundle:
    windows: list[SweepAnalysisWindow]
    bars: list[SweepAnalysisBar]
    path_metrics: list[WindowPathMetrics]
    htf_updates: list[dict[str, Any]]
    overlap_rows: list[dict[str, Any]]
    incomplete_windows: list[dict[str, Any]]
    validation: dict[str, Any]


def assert_no_entry_fields(payload: Mapping[str, Any]) -> None:
    forbidden = {
        "entry_index",
        "entry_price",
        "entry_timestamp",
        "pnl",
        "tp",
        "sl",
        "fees",
        "position",
        "trade_id",
    }
    hit = sorted(k for k in payload if k in forbidden or str(k).startswith("entry_"))
    if hit:
        raise RuntimeError(f"forbidden entry/pnl fields present: {hit}")


def resolve_sweep_level(event: SweepTriggerEvent) -> float | None:
    if event.cluster_center_price is not None and np.isfinite(float(event.cluster_center_price)):
        return float(event.cluster_center_price)
    return None


def snapshot_frozen_packs(snap: SweepScannerSnapshot) -> dict[str, dict[str, Any]]:
    def _pick(src: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k in _FEATURE_KEYS:
            out[k] = copy.deepcopy(src.get(k))
        # never invent PA/momentum if absent
        out["price_action_state"] = copy.deepcopy(src.get("price_action_state"))
        out["momentum_state"] = copy.deepcopy(src.get("momentum_state"))
        out["momentum_confirmation_age"] = copy.deepcopy(src.get("momentum_confirmation_age"))
        return out

    return {
        "5m": _pick(snap.features_5m),
        "15m": _pick(snap.features_15m),
        "30m": _pick(snap.features_30m),
        "meta": {
            "tf15_bucket_start": None
            if snap.tf15_bucket_start is None
            else ensure_utc(snap.tf15_bucket_start).isoformat(),
            "tf15_bucket_end": None
            if snap.tf15_bucket_end is None
            else ensure_utc(snap.tf15_bucket_end).isoformat(),
            "tf15_available_at": None
            if snap.tf15_available_at is None
            else ensure_utc(snap.tf15_available_at).isoformat(),
            "tf30_bucket_start": None
            if snap.tf30_bucket_start is None
            else ensure_utc(snap.tf30_bucket_start).isoformat(),
            "tf30_bucket_end": None
            if snap.tf30_bucket_end is None
            else ensure_utc(snap.tf30_bucket_end).isoformat(),
            "tf30_available_at": None
            if snap.tf30_available_at is None
            else ensure_utc(snap.tf30_available_at).isoformat(),
            "decision_time": ensure_utc(snap.decision_time).isoformat(),
        },
    }


def features_at_index(
    store: ScannerFeatureStore,
    index: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Causal 5m + last-closed 15m/30m feature packs at candle ``index`` close."""
    if index < 0 or index >= len(store.ind_5m):
        raise IndexError(index)
    row5 = store.ind_5m.iloc[index].to_dict()
    ts = ensure_utc(store.ind_5m.iloc[index]["timestamp"])
    decision = decision_time_from_signal_open(ts)
    struct5 = store.structure_5m[index]
    regime5 = _regime_label_from_row(row5, timeframe="5m", bar_index=index)
    vol_r = _finite(store.volume_ratio_5m[index])
    feats5, _ = _feature_pack_from_indicator_row(
        row5,
        structure=struct5,
        regime=regime5,
        timeframe="5m",
        volume_ratio=vol_r,
        include_pa_momentum=True,
    )
    # Strip invented PA/momentum — remain None unless already present
    feats5["price_action_state"] = None
    feats5["momentum_state"] = None
    feats5["momentum_confirmation_age"] = None

    i15 = _last_closed_htf_index(store.available_at_15m, decision)
    i30 = _last_closed_htf_index(store.available_at_30m, decision)

    def _htf(i: int | None, ind: pd.DataFrame, structs: list, tf: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if i is None:
            empty = {k: None for k in _FEATURE_KEYS}
            meta = {
                "bucket_start": None,
                "bucket_end": None,
                "available_at": None,
                "age_minutes": None,
                "is_closed": False,
            }
            return empty, meta
        row = ind.iloc[i].to_dict()
        st = structs[i]
        reg = _regime_label_from_row(row, timeframe=tf, bar_index=i)
        feats, _ = _feature_pack_from_indicator_row(
            row, structure=st, regime=reg, timeframe=tf, volume_ratio=None
        )
        feats["price_action_state"] = None
        feats["momentum_state"] = None
        feats["momentum_confirmation_age"] = None
        start = ensure_utc(ind.iloc[i]["timestamp"])
        end = start + timeframe_timedelta(tf)
        if end > decision:
            raise RuntimeError(f"HTF lookahead {tf}: bucket_end={end} > decision={decision}")
        age = float((decision - end).total_seconds() / 60.0)
        meta = {
            "bucket_start": start.isoformat(),
            "bucket_end": end.isoformat(),
            "available_at": end.isoformat(),
            "age_minutes": age,
            "is_closed": True,
        }
        return feats, meta

    feats15, meta15 = _htf(i15, store.ind_15m, store.structure_15m, "15m")
    feats30, meta30 = _htf(i30, store.ind_30m, store.structure_30m, "30m")
    return feats5, feats15, feats30, meta15, meta30


def compute_feature_deltas(
    frozen_5m: Mapping[str, Any],
    current_5m: Mapping[str, Any],
    *,
    frozen_15m: Mapping[str, Any],
    current_15m: Mapping[str, Any],
    frozen_30m: Mapping[str, Any],
    current_30m: Mapping[str, Any],
) -> dict[str, Any]:
    def _delta(a: object, b: object) -> float | None:
        fa, fb = _finite(a), _finite(b)
        if fa is None or fb is None:
            return None
        return float(fb - fa)

    di_f = None
    if _finite(frozen_5m.get("di_plus")) is not None and _finite(frozen_5m.get("di_minus")) is not None:
        di_f = float(frozen_5m["di_plus"]) - float(frozen_5m["di_minus"])
    di_c = None
    if _finite(current_5m.get("di_plus")) is not None and _finite(current_5m.get("di_minus")) is not None:
        di_c = float(current_5m["di_plus"]) - float(current_5m["di_minus"])

    return {
        "ema_9_20_distance": _delta(frozen_5m.get("ema_9_20_distance"), current_5m.get("ema_9_20_distance")),
        "ema_20_59_distance": _delta(frozen_5m.get("ema_20_59_distance"), current_5m.get("ema_20_59_distance")),
        "adx": _delta(frozen_5m.get("adx"), current_5m.get("adx")),
        "di_spread": None if di_f is None or di_c is None else float(di_c - di_f),
        "atr_pct": _delta(frozen_5m.get("atr_pct"), current_5m.get("atr_pct")),
        "volume_ratio": _delta(frozen_5m.get("volume_ratio"), current_5m.get("volume_ratio")),
        "regime_changed": frozen_5m.get("regime") != current_5m.get("regime"),
        "structure_bias_changed": frozen_5m.get("structure_bias") != current_5m.get("structure_bias"),
        "structure_pair_changed": frozen_5m.get("structure_pair") != current_5m.get("structure_pair"),
        "tf15_state_changed_since_sweep": frozen_15m.get("regime") != current_15m.get("regime")
        or frozen_15m.get("structure_bias") != current_15m.get("structure_bias"),
        "tf30_state_changed_since_sweep": frozen_30m.get("regime") != current_30m.get("regime")
        or frozen_30m.get("structure_bias") != current_30m.get("structure_bias"),
        "tf15_bucket_changed": False,  # filled by caller when meta differs
        "tf30_bucket_changed": False,
    }


def compute_path_metrics(
    *,
    bars: Sequence[SweepAnalysisBar],
    sweep_close: float,
    level: float | None,
) -> dict[str, Any]:
    if not bars:
        return {
            "max_high_from_sweep_close_pct": None,
            "min_low_from_sweep_close_pct": None,
            "close_return_end_pct": None,
            "max_high_from_level_pct": None,
            "min_low_from_level_pct": None,
            "first_close_above_level_offset": None,
            "first_close_below_level_offset": None,
            "first_reclaim_below_offset": None,
            "first_touch_level_offset": None,
            "candles_closed_above_level": 0,
            "candles_closed_below_level": 0,
            "consecutive_closes_above_max": 0,
            "consecutive_closes_below_max": 0,
            "number_of_level_crosses": 0,
            "number_of_reclaims_below": 0,
            "final_close_relative_to_level_pct": None,
            "window_range_pct": None,
        }
    highs = np.array([b.high for b in bars], dtype=float)
    lows = np.array([b.low for b in bars], dtype=float)
    closes = np.array([b.close for b in bars], dtype=float)
    sc = float(sweep_close)
    max_high = float(np.max((highs / sc - 1.0) * 100.0))
    min_low = float(np.min((lows / sc - 1.0) * 100.0))
    close_end = float((closes[-1] / sc - 1.0) * 100.0)
    window_range = float((np.max(highs) - np.min(lows)) / sc * 100.0) if sc else None

    above = [bool(b.level_behavior["close_above_level"]) for b in bars]
    below = [bool(b.level_behavior["close_below_level"]) for b in bars]
    crosses = sum(1 for b in bars if b.level_behavior["crossed_level"])
    reclaims = sum(1 for b in bars if b.level_behavior["reclaimed_below_level"])

    def _first(pred) -> int | None:
        for b in bars:
            if pred(b):
                return int(b.window_offset)
        return None

    def _consec(flags: list[bool]) -> int:
        best = cur = 0
        for f in flags:
            if f:
                cur += 1
                best = max(best, cur)
            else:
                cur = 0
        return best

    max_from_lvl = min_from_lvl = final_rel = None
    if level is not None and float(level) != 0.0:
        lvl = float(level)
        max_from_lvl = float(np.max((highs / lvl - 1.0) * 100.0))
        min_from_lvl = float(np.min((lows / lvl - 1.0) * 100.0))
        final_rel = _rel_pct(float(closes[-1]), lvl)

    return {
        "max_high_from_sweep_close_pct": max_high,
        "min_low_from_sweep_close_pct": min_low,
        "close_return_end_pct": close_end,
        "max_high_from_level_pct": max_from_lvl,
        "min_low_from_level_pct": min_from_lvl,
        "first_close_above_level_offset": _first(lambda b: b.level_behavior["close_above_level"]),
        "first_close_below_level_offset": _first(lambda b: b.level_behavior["close_below_level"]),
        "first_reclaim_below_offset": _first(lambda b: b.level_behavior["reclaimed_below_level"]),
        "first_touch_level_offset": _first(lambda b: b.level_behavior["touched_level"]),
        "candles_closed_above_level": int(sum(above)),
        "candles_closed_below_level": int(sum(below)),
        "consecutive_closes_above_max": _consec(above),
        "consecutive_closes_below_max": _consec(below),
        "number_of_level_crosses": int(crosses),
        "number_of_reclaims_below": int(reclaims),
        "final_close_relative_to_level_pct": final_rel,
        "window_range_pct": window_range,
    }


def build_single_window(
    event: SweepTriggerEvent,
    *,
    store: ScannerFeatureStore,
    frozen_snap: SweepScannerSnapshot,
    window_size: int,
) -> tuple[SweepAnalysisWindow, list[SweepAnalysisBar], WindowPathMetrics, list[dict[str, Any]]]:
    if window_size < 1:
        raise ValueError("window_size must be >= 1")

    frozen = snapshot_frozen_packs(frozen_snap)
    # Freeze deep copies so later dynamic updates cannot mutate
    frozen_5m = copy.deepcopy(frozen["5m"])
    frozen_15m = copy.deepcopy(frozen["15m"])
    frozen_30m = copy.deepcopy(frozen["30m"])
    frozen_meta = copy.deepcopy(frozen["meta"])

    level = resolve_sweep_level(event)
    signal_i = int(event.signal_index)
    n = len(store.ohlcv)
    start_i = signal_i + 1
    expected = int(window_size)

    if start_i >= n:
        win = SweepAnalysisWindow(
            event_id=event.event_id,
            source_config_id=event.source_config_id,
            signal_index=signal_i,
            signal_timestamp=ensure_utc(event.signal_timestamp),
            sample=event.sample,
            window_size=expected,
            start_index=start_i,
            start_timestamp=None,
            end_index=None,
            end_timestamp=None,
            status=STATE_INCOMPLETE_END_OF_DATA,
            available_candle_count=0,
            expected_candle_count=expected,
            complete=False,
            invalidation_reason=None,
            initial_sweep_level=level,
            initial_cluster_center_price=event.cluster_center_price,
            initial_close_relative_to_level_pct=event.close_relative_to_level_pct,
            frozen_context={
                "5m": frozen_5m,
                "15m": frozen_15m,
                "30m": frozen_30m,
                "meta": frozen_meta,
            },
        )
        metrics = WindowPathMetrics(
            event_id=event.event_id,
            window_size=expected,
            status=win.status,
            complete=False,
            available_candle_count=0,
            metrics=compute_path_metrics(bars=[], sweep_close=float(event.sweep_candle_close), level=level),
        )
        return win, [], metrics, []

    if level is None or not np.isfinite(float(level)) or float(level) == 0.0:
        win = SweepAnalysisWindow(
            event_id=event.event_id,
            source_config_id=event.source_config_id,
            signal_index=signal_i,
            signal_timestamp=ensure_utc(event.signal_timestamp),
            sample=event.sample,
            window_size=expected,
            start_index=start_i,
            start_timestamp=ensure_utc(store.ohlcv.iloc[start_i]["timestamp"]),
            end_index=None,
            end_timestamp=None,
            status=STATE_INVALIDATED,
            available_candle_count=0,
            expected_candle_count=expected,
            complete=False,
            invalidation_reason="missing_or_invalid_sweep_level",
            initial_sweep_level=level,
            initial_cluster_center_price=event.cluster_center_price,
            initial_close_relative_to_level_pct=event.close_relative_to_level_pct,
            frozen_context={
                "5m": frozen_5m,
                "15m": frozen_15m,
                "30m": frozen_30m,
                "meta": frozen_meta,
            },
        )
        metrics = WindowPathMetrics(
            event_id=event.event_id,
            window_size=expected,
            status=win.status,
            complete=False,
            available_candle_count=0,
            metrics=compute_path_metrics(bars=[], sweep_close=float(event.sweep_candle_close), level=level),
        )
        return win, [], metrics, []

    bars: list[SweepAnalysisBar] = []
    htf_updates: list[dict[str, Any]] = []
    state = STATE_SWEEP_DETECTED
    last_15_bucket = frozen_meta.get("tf15_bucket_start")
    last_30_bucket = frozen_meta.get("tf30_bucket_start")

    max_offset = min(expected, n - start_i)
    for offset in range(1, max_offset + 1):
        idx = signal_i + offset
        if idx == signal_i:
            raise RuntimeError("sweep candle must not appear as follow candle")
        if idx >= n:
            break
        row = store.ohlcv.iloc[idx]
        ts = ensure_utc(row["timestamp"])
        available_at = decision_time_from_signal_open(ts)
        # causality: never use a bar whose close is beyond "now" relative to itself — always closed by construction
        state_before = state
        if state == STATE_SWEEP_DETECTED:
            state = STATE_ANALYSIS_ACTIVE
        state_after = state

        o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
        vol = float(row["volume"])
        behavior = level_behavior_for_bar(open_=o, high=h, low=l, close=c, level=float(level))

        cur5, cur15, cur30, meta15, meta30 = features_at_index(store, idx)
        # Ensure frozen packs unchanged (identity check via deep equality after copy)
        deltas = compute_feature_deltas(
            frozen_5m,
            cur5,
            frozen_15m=frozen_15m,
            current_15m=cur15,
            frozen_30m=frozen_30m,
            current_30m=cur30,
        )
        bucket15 = meta15.get("bucket_start")
        bucket30 = meta30.get("bucket_start")
        tf15_changed = bucket15 is not None and bucket15 != last_15_bucket
        tf30_changed = bucket30 is not None and bucket30 != last_30_bucket
        deltas["tf15_bucket_changed"] = bool(tf15_changed)
        deltas["tf30_bucket_changed"] = bool(tf30_changed)
        if tf15_changed:
            htf_updates.append(
                {
                    "event_id": event.event_id,
                    "window_size": expected,
                    "window_offset": offset,
                    "timeframe": "15m",
                    "previous_bucket_start": last_15_bucket,
                    "new_bucket_start": bucket15,
                    "new_bucket_end": meta15.get("bucket_end"),
                    "available_at": meta15.get("available_at"),
                    "candle_index": idx,
                    "candle_timestamp": ts.isoformat(),
                }
            )
            last_15_bucket = bucket15
        if tf30_changed:
            htf_updates.append(
                {
                    "event_id": event.event_id,
                    "window_size": expected,
                    "window_offset": offset,
                    "timeframe": "30m",
                    "previous_bucket_start": last_30_bucket,
                    "new_bucket_start": bucket30,
                    "new_bucket_end": meta30.get("bucket_end"),
                    "available_at": meta30.get("available_at"),
                    "candle_index": idx,
                    "candle_timestamp": ts.isoformat(),
                }
            )
            last_30_bucket = bucket30

        if offset == expected:
            state_after = STATE_WINDOW_COMPLETED
            state = STATE_WINDOW_COMPLETED

        bar = SweepAnalysisBar(
            event_id=event.event_id,
            window_size=expected,
            window_offset=offset,
            candle_index=idx,
            timestamp=ts,
            open=o,
            high=h,
            low=l,
            close=c,
            volume=vol,
            is_closed=True,
            available_at=available_at,
            state_before=state_before,
            state_after=state_after,
            level_behavior=behavior,
            frozen_5m=copy.deepcopy(frozen_5m),
            frozen_15m=copy.deepcopy(frozen_15m),
            frozen_30m=copy.deepcopy(frozen_30m),
            current_5m={k: cur5.get(k) for k in _FEATURE_KEYS},
            current_15m={k: cur15.get(k) for k in _FEATURE_KEYS},
            current_30m={k: cur30.get(k) for k in _FEATURE_KEYS},
            deltas=deltas,
            htf_15m={
                **meta15,
                "state_changed_since_sweep": deltas["tf15_state_changed_since_sweep"],
            },
            htf_30m={
                **meta30,
                "state_changed_since_sweep": deltas["tf30_state_changed_since_sweep"],
            },
        )
        assert_no_entry_fields(bar.to_dict())
        bars.append(bar)

    available = len(bars)
    complete = available == expected
    if available == 0:
        status = STATE_INCOMPLETE_END_OF_DATA
    elif complete:
        status = STATE_WINDOW_COMPLETED
    else:
        status = STATE_INCOMPLETE_END_OF_DATA
        if bars:
            bars[-1].state_after = STATE_INCOMPLETE_END_OF_DATA

    end_i = bars[-1].candle_index if bars else None
    end_ts = bars[-1].timestamp if bars else None
    start_ts = ensure_utc(store.ohlcv.iloc[start_i]["timestamp"]) if start_i < n else None

    win = SweepAnalysisWindow(
        event_id=event.event_id,
        source_config_id=event.source_config_id,
        signal_index=signal_i,
        signal_timestamp=ensure_utc(event.signal_timestamp),
        sample=event.sample,
        window_size=expected,
        start_index=start_i,
        start_timestamp=start_ts,
        end_index=end_i,
        end_timestamp=end_ts,
        status=status,
        available_candle_count=available,
        expected_candle_count=expected,
        complete=complete,
        invalidation_reason=None,
        initial_sweep_level=float(level),
        initial_cluster_center_price=event.cluster_center_price,
        initial_close_relative_to_level_pct=event.close_relative_to_level_pct,
        frozen_context={
            "5m": frozen_5m,
            "15m": frozen_15m,
            "30m": frozen_30m,
            "meta": frozen_meta,
        },
    )
    assert_no_entry_fields(win.to_dict())
    # Freeze integrity: mutate original packs must not affect frozen_context copies
    metrics = WindowPathMetrics(
        event_id=event.event_id,
        window_size=expected,
        status=status,
        complete=complete,
        available_candle_count=available,
        metrics=compute_path_metrics(
            bars=bars, sweep_close=float(event.sweep_candle_close), level=float(level)
        ),
    )
    return win, bars, metrics, htf_updates


def compute_overlap_diagnostics(
    windows: Sequence[SweepAnalysisWindow],
    *,
    n_candles: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """All overlapping windows kept; diagnostics only."""
    # active count per candle across all window sizes
    counts = np.zeros(n_candles, dtype=int)
    coverage: list[tuple[str, int, int, int]] = []  # event, size, start, end_inclusive
    for w in windows:
        if w.available_candle_count <= 0 or w.start_index is None:
            continue
        if w.end_index is None:
            continue
        a = int(w.start_index)
        b = int(w.end_index)
        if a >= n_candles:
            continue
        b = min(b, n_candles - 1)
        counts[a : b + 1] += 1
        coverage.append((w.event_id, int(w.window_size), a, b))

    events_with_overlaps: set[str] = set()
    for i, (e1, s1, a1, b1) in enumerate(coverage):
        for e2, s2, a2, b2 in coverage[i + 1 :]:
            if e1 == e2:
                continue
            if a1 <= b2 and a2 <= b1:
                events_with_overlaps.add(e1)
                events_with_overlaps.add(e2)

    rows = [
        {"candle_index": int(i), "active_window_count": int(counts[i])}
        for i in range(n_candles)
        if counts[i] > 0
    ]
    summary = {
        "events_with_overlaps": int(len(events_with_overlaps)),
        "maximum_concurrent_windows": int(counts.max()) if len(counts) else 0,
        "candles_with_any_window": int(np.sum(counts > 0)),
    }
    return rows, summary


def build_analysis_windows_for_events(
    events: Sequence[SweepTriggerEvent],
    *,
    store: ScannerFeatureStore,
    frozen_snapshots: Sequence[SweepScannerSnapshot],
    window_sizes: Sequence[int] = DEFAULT_WINDOW_SIZES,
    progress: Any | None = None,
) -> AnalysisWindowBundle:
    if len(events) != len(frozen_snapshots):
        raise ValueError("events and frozen_snapshots length mismatch")
    sizes = tuple(int(x) for x in window_sizes)
    windows: list[SweepAnalysisWindow] = []
    bars: list[SweepAnalysisBar] = []
    metrics: list[WindowPathMetrics] = []
    htf_updates: list[dict[str, Any]] = []

    for i, (ev, snap) in enumerate(zip(events, frozen_snapshots)):
        # freeze snapshot once more before use
        frozen = copy.deepcopy(snap)
        for ws in sizes:
            win, win_bars, path, updates = build_single_window(
                ev, store=store, frozen_snap=frozen, window_size=ws
            )
            # Ensure frozen context identity across bars
            for b in win_bars:
                if b.frozen_5m.get("ema_9") != win.frozen_context["5m"].get("ema_9"):
                    raise RuntimeError("frozen 5m context mutated inside window")
            windows.append(win)
            bars.extend(win_bars)
            metrics.append(path)
            htf_updates.extend(updates)
        if progress is not None and (i + 1) % 250 == 0:
            progress(f"Fenster gebaut: {i + 1}/{len(events)} Events")

    overlap_rows, overlap_summary = compute_overlap_diagnostics(windows, n_candles=len(store.ohlcv))
    incomplete = [
        {
            "event_id": w.event_id,
            "window_size": w.window_size,
            "status": w.status,
            "available_candle_count": w.available_candle_count,
            "expected_candle_count": w.expected_candle_count,
            "signal_index": w.signal_index,
            "start_index": w.start_index,
            "end_index": w.end_index,
        }
        for w in windows
        if not w.complete
    ]
    validation = {
        "expected": {"full": EXPECTED_FULL, "in_sample": EXPECTED_IS, "out_of_sample": EXPECTED_OOS},
        "overlap_summary": overlap_summary,
    }
    if progress is not None:
        progress(f"Fenster gebaut: {len(windows)} (sizes={sizes})")
    bundle = AnalysisWindowBundle(
        windows=windows,
        bars=bars,
        path_metrics=metrics,
        htf_updates=htf_updates,
        overlap_rows=overlap_rows,
        incomplete_windows=incomplete,
        validation=validation,
    )
    return bundle


def bundle_deterministic_hash(bundle: AnalysisWindowBundle) -> str:
    payload = {
        "windows": [w.to_dict() for w in bundle.windows],
        "bars": [
            {
                "event_id": b.event_id,
                "window_size": b.window_size,
                "window_offset": b.window_offset,
                "candle_index": b.candle_index,
                "close": b.close,
                "state_after": b.state_after,
                "lvl_close_above_level": b.level_behavior.get("close_above_level"),
                "delta_adx": b.deltas.get("adx"),
            }
            for b in bundle.bars
        ],
        "metrics": [m.to_dict() for m in bundle.path_metrics],
    }
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def load_or_build_phase_a_inputs(
    ohlcv: pd.DataFrame,
    *,
    expect_counts: bool = True,
    progress: Any | None = None,
) -> tuple[list[SweepTriggerEvent], list[SweepScannerSnapshot], ScannerFeatureStore, dict[str, Any]]:
    def _p(msg: str) -> None:
        if progress is not None:
            progress(msg)

    _p("reproducing winner events")
    try:
        validation_events, replay, validation = reproduce_winner_events(
            ohlcv, expect_counts=expect_counts
        )
    except EventCountMismatchError:
        raise
    triggers = validation_events_to_triggers(validation_events, replay, ohlcv)
    _p(f"Sweep-Events: {len(triggers)}")
    store = precompute_scanner_feature_store(ohlcv, progress=progress)
    _p("joining frozen Phase-A snapshots")
    snaps = [join_sweep_event(ev, store) for ev in triggers]
    snaps = [copy.deepcopy(s) for s in snaps]
    return triggers, snaps, store, validation


def parse_window_sizes(raw: str | Sequence[int] | None) -> tuple[int, ...]:
    if raw is None:
        return DEFAULT_WINDOW_SIZES
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        return tuple(int(p) for p in parts)
    return tuple(int(x) for x in raw)


__all__ = [
    "DEFAULT_WINDOW_SIZES",
    "STATE_SWEEP_DETECTED",
    "STATE_ANALYSIS_ACTIVE",
    "STATE_WINDOW_COMPLETED",
    "STATE_INCOMPLETE_END_OF_DATA",
    "STATE_INVALIDATED",
    "SweepAnalysisWindow",
    "SweepAnalysisBar",
    "WindowPathMetrics",
    "AnalysisWindowBundle",
    "level_behavior_for_bar",
    "build_single_window",
    "build_analysis_windows_for_events",
    "compute_overlap_diagnostics",
    "bundle_deterministic_hash",
    "load_or_build_phase_a_inputs",
    "parse_window_sizes",
    "assert_no_entry_fields",
    "features_at_index",
]
