"""Causal sweep events, clusters, and signal variants for liquidation-level research.

Builds on ``replay_liquidation_levels`` without modifying the Pine replication.
A sweep is only actionable after the sweep candle closes → entry at next open.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

from research.liquidation_level.liquidation_levels import (
    SIDE_LOWER,
    SIDE_UPPER,
    STATUS_SWEPT,
    LiquidationLevel,
    LiquidationLevelConfig,
    LiquidationReplayResult,
    normalize_ohlcv_dataframe,
)

EPS = 1e-12

LONG_VARIANTS = ("L1", "L2", "L3", "L4", "L5", "L6", "L7")
SHORT_VARIANTS = ("S1", "S2", "S3", "S4", "S5", "S6", "S7")
CONTINUATION_VARIANTS = ("F_SHORT", "F_LONG")
ALL_VARIANTS = LONG_VARIANTS + SHORT_VARIANTS + CONTINUATION_VARIANTS


@dataclass(frozen=True)
class FeatureConfig:
    cluster_max_gap_pct: float = 0.10
    # Defaults 1/1 preserve historical cluster feature behaviour (no min filter).
    # Use FeatureConfig.from_liquidation_config(...) for documented research baseline mins.
    cluster_min_level_count: int = 1
    cluster_min_total_strength: int = 1

    @classmethod
    def from_liquidation_config(cls, cfg: LiquidationLevelConfig | None) -> "FeatureConfig":
        if cfg is None:
            return cls()
        return cls(
            cluster_max_gap_pct=float(cfg.cluster_distance_pct),
            cluster_min_level_count=int(cfg.cluster_min_level_count),
            cluster_min_total_strength=int(cfg.cluster_min_total_strength),
        )


@dataclass
class LevelSweepEvent:
    event_id: str
    level_id: int
    signal_index: int
    signal_timestamp: pd.Timestamp
    entry_index: int
    entry_timestamp: pd.Timestamp | None
    side: str
    leverage: int
    level_price: float
    strength: int
    created_index: int
    level_age: int
    sweep_candle_open: float
    sweep_candle_high: float
    sweep_candle_low: float
    sweep_candle_close: float
    sweep_candle_volume: float
    sweep_body_pct: float
    upper_wick_pct: float
    lower_wick_pct: float
    close_location_value: float
    sweep_depth_pct: float
    active_upper_count_before: int
    active_lower_count_before: int
    active_upper_strength_before: int
    active_lower_strength_before: int


@dataclass
class CandleSweepEvent:
    event_id: str
    signal_index: int
    signal_timestamp: pd.Timestamp
    entry_index: int
    entry_timestamp: pd.Timestamp | None
    side: str
    swept_level_count: int
    swept_total_strength: int
    swept_leverages: tuple[int, ...]
    minimum_level_price: float
    maximum_level_price: float
    weighted_center_price: float
    oldest_level_age: int
    median_level_age: float
    strongest_level_strength: int
    sweep_candle_open: float
    sweep_candle_high: float
    sweep_candle_low: float
    sweep_candle_close: float
    sweep_candle_volume: float
    sweep_body_pct: float
    upper_wick_pct: float
    lower_wick_pct: float
    close_location_value: float
    active_upper_count_before: int
    active_lower_count_before: int
    active_upper_strength_before: int
    active_lower_strength_before: int
    swept_level_ids: tuple[int, ...]


@dataclass
class ClusterSnapshot:
    cluster_id: str
    candle_index: int
    timestamp: pd.Timestamp
    side: str
    level_count: int
    total_strength: int
    min_price: float
    max_price: float
    center_price: float
    oldest_age: int
    median_age: float
    leverage_count: int
    level_ids: tuple[int, ...]


@dataclass
class ClusterSweepEvent:
    event_id: str
    cluster_id: str
    signal_index: int
    signal_timestamp: pd.Timestamp
    entry_index: int
    entry_timestamp: pd.Timestamp | None
    side: str
    swept_level_count: int
    swept_total_strength: int
    level_count_in_cluster: int
    total_strength_in_cluster: int
    min_price: float
    max_price: float
    center_price: float
    oldest_age: int
    median_age: float
    leverage_count: int
    sweep_candle_open: float
    sweep_candle_high: float
    sweep_candle_low: float
    sweep_candle_close: float
    sweep_candle_volume: float
    sweep_body_pct: float
    upper_wick_pct: float
    lower_wick_pct: float
    close_location_value: float
    trigger_reason: str
    swept_level_ids: tuple[int, ...]


@dataclass
class SignalEvent:
    signal_id: str
    variant: str
    direction: str  # long | short
    signal_index: int
    signal_timestamp: pd.Timestamp
    entry_index: int
    entry_timestamp: pd.Timestamp | None
    source_event_id: str
    side: str
    close_location_value: float
    sweep_body_pct: float
    upper_wick_pct: float
    lower_wick_pct: float
    swept_level_count: int
    swept_total_strength: int
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class FeatureBundle:
    ohlcv: pd.DataFrame
    level_events: list[LevelSweepEvent]
    candle_events: list[CandleSweepEvent]
    cluster_snapshots: list[ClusterSnapshot]
    cluster_events: list[ClusterSweepEvent]
    signals: list[SignalEvent]
    summary: dict[str, Any]


def candle_geometry(
    open_: float,
    high: float,
    low: float,
    close: float,
) -> dict[str, float]:
    """Body / wick percentages of range and close-location value."""
    o, h, l, c = float(open_), float(high), float(low), float(close)
    rng = max(h - l, EPS)
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    return {
        "sweep_body_pct": 100.0 * body / rng,
        "upper_wick_pct": 100.0 * max(upper_wick, 0.0) / rng,
        "lower_wick_pct": 100.0 * max(lower_wick, 0.0) / rng,
        "close_location_value": (c - l) / rng,
    }


def sweep_depth_pct(side: str, level_price: float, high: float, low: float) -> float:
    px = float(level_price)
    if px == 0.0:
        return 0.0
    if side == SIDE_UPPER:
        return 100.0 * (float(high) - px) / px
    return 100.0 * (px - float(low)) / px


def weighted_center(prices: Sequence[float], weights: Sequence[float]) -> float:
    if not prices:
        raise ValueError("prices must be non-empty")
    w = np.asarray(weights, dtype=float)
    p = np.asarray(prices, dtype=float)
    sw = float(w.sum())
    if sw <= 0:
        return float(p.mean())
    return float((p * w).sum() / sw)


def cluster_levels_by_price_gap(
    levels: Sequence[LiquidationLevel],
    *,
    side: str,
    candle_index: int,
    max_gap_pct: float = 0.10,
) -> list[list[LiquidationLevel]]:
    """Group same-side levels by adjacent price gap <= max_gap_pct (percent)."""
    same = [lvl for lvl in levels if lvl.side == side]
    if not same:
        return []
    ordered = sorted(same, key=lambda x: (float(x.level_price), int(x.level_id)))
    clusters: list[list[LiquidationLevel]] = [[ordered[0]]]
    for lvl in ordered[1:]:
        prev = clusters[-1][-1]
        prev_px = float(prev.level_price)
        gap_pct = 0.0 if prev_px == 0.0 else abs(float(lvl.level_price) - prev_px) / prev_px * 100.0
        if gap_pct <= float(max_gap_pct) + 1e-15:
            clusters[-1].append(lvl)
        else:
            clusters.append([lvl])
    return clusters


def cluster_is_swept(
    cluster: Sequence[LiquidationLevel],
    swept_ids: set[int],
) -> tuple[bool, str, list[LiquidationLevel]]:
    """Cluster-sweep if >=2 members swept or swept strength >= 3."""
    swept = [lvl for lvl in cluster if int(lvl.level_id) in swept_ids]
    if not swept:
        return False, "", []
    strength = sum(int(lvl.strength) for lvl in swept)
    if len(swept) >= 2:
        return True, "level_count", swept
    if strength >= 3:
        return True, "total_strength", swept
    return False, "", swept


def _entry_ts(ohlcv: pd.DataFrame, entry_index: int) -> pd.Timestamp | None:
    if entry_index < 0 or entry_index >= len(ohlcv):
        return None
    return pd.Timestamp(ohlcv.iloc[entry_index]["timestamp"])


def _levels_by_id(result: LiquidationReplayResult) -> dict[int, LiquidationLevel]:
    return {int(lvl.level_id): lvl for lvl in result.all_levels}


def build_level_sweep_events(
    result: LiquidationReplayResult,
    ohlcv: pd.DataFrame,
) -> list[LevelSweepEvent]:
    """One event per swept level; entry at next candle open."""
    data = ohlcv if "timestamp" in getattr(ohlcv, "columns", []) else normalize_ohlcv_dataframe(ohlcv)
    if list(data.columns)[:6] != ["timestamp", "open", "high", "low", "close", "volume"]:
        data = normalize_ohlcv_dataframe(ohlcv)

    state_by_idx = {int(st.candle_index): st for st in result.candle_states}
    events: list[LevelSweepEvent] = []
    seq = 0
    for lvl in result.all_levels:
        if lvl.status != STATUS_SWEPT or lvl.swept_index is None:
            continue
        i = int(lvl.swept_index)
        if i < 0 or i >= len(data):
            continue
        row = data.iloc[i]
        geo = candle_geometry(row["open"], row["high"], row["low"], row["close"])
        st = state_by_idx.get(i)
        entry_index = i + 1
        seq += 1
        events.append(
            LevelSweepEvent(
                event_id=f"LVL_{seq:06d}",
                level_id=int(lvl.level_id),
                signal_index=i,
                signal_timestamp=pd.Timestamp(row["timestamp"]),
                entry_index=entry_index,
                entry_timestamp=_entry_ts(data, entry_index),
                side=str(lvl.side),
                leverage=int(lvl.leverage),
                level_price=float(lvl.level_price),
                strength=int(lvl.strength),
                created_index=int(lvl.created_index),
                level_age=int(lvl.age_at_sweep if lvl.age_at_sweep is not None else i - int(lvl.created_index)),
                sweep_candle_open=float(row["open"]),
                sweep_candle_high=float(row["high"]),
                sweep_candle_low=float(row["low"]),
                sweep_candle_close=float(row["close"]),
                sweep_candle_volume=float(row["volume"]),
                sweep_body_pct=geo["sweep_body_pct"],
                upper_wick_pct=geo["upper_wick_pct"],
                lower_wick_pct=geo["lower_wick_pct"],
                close_location_value=geo["close_location_value"],
                sweep_depth_pct=sweep_depth_pct(str(lvl.side), float(lvl.level_price), float(row["high"]), float(row["low"])),
                active_upper_count_before=0 if st is None else int(st.active_upper_before),
                active_lower_count_before=0 if st is None else int(st.active_lower_before),
                active_upper_strength_before=0 if st is None else int(st.active_strength_upper_before),
                active_lower_strength_before=0 if st is None else int(st.active_strength_lower_before),
            )
        )
    return events


def _aggregate_side_levels(
    levels: Sequence[LiquidationLevel],
    *,
    signal_index: int,
    data: pd.DataFrame,
    st,
    side: str,
    seq: int,
) -> CandleSweepEvent:
    ages = [int(lvl.age_at_sweep if lvl.age_at_sweep is not None else signal_index - int(lvl.created_index)) for lvl in levels]
    prices = [float(lvl.level_price) for lvl in levels]
    strengths = [int(lvl.strength) for lvl in levels]
    row = data.iloc[signal_index]
    geo = candle_geometry(row["open"], row["high"], row["low"], row["close"])
    entry_index = signal_index + 1
    return CandleSweepEvent(
        event_id=f"CND_{side}_{seq:06d}",
        signal_index=signal_index,
        signal_timestamp=pd.Timestamp(row["timestamp"]),
        entry_index=entry_index,
        entry_timestamp=_entry_ts(data, entry_index),
        side=side,
        swept_level_count=len(levels),
        swept_total_strength=int(sum(strengths)),
        swept_leverages=tuple(sorted({int(lvl.leverage) for lvl in levels})),
        minimum_level_price=float(min(prices)),
        maximum_level_price=float(max(prices)),
        weighted_center_price=weighted_center(prices, strengths),
        oldest_level_age=int(max(ages)),
        median_level_age=float(np.median(ages)),
        strongest_level_strength=int(max(strengths)),
        sweep_candle_open=float(row["open"]),
        sweep_candle_high=float(row["high"]),
        sweep_candle_low=float(row["low"]),
        sweep_candle_close=float(row["close"]),
        sweep_candle_volume=float(row["volume"]),
        sweep_body_pct=geo["sweep_body_pct"],
        upper_wick_pct=geo["upper_wick_pct"],
        lower_wick_pct=geo["lower_wick_pct"],
        close_location_value=geo["close_location_value"],
        active_upper_count_before=0 if st is None else int(st.active_upper_before),
        active_lower_count_before=0 if st is None else int(st.active_lower_before),
        active_upper_strength_before=0 if st is None else int(st.active_strength_upper_before),
        active_lower_strength_before=0 if st is None else int(st.active_strength_lower_before),
        swept_level_ids=tuple(int(lvl.level_id) for lvl in levels),
    )


def build_candle_sweep_events(
    result: LiquidationReplayResult,
    ohlcv: pd.DataFrame,
) -> list[CandleSweepEvent]:
    """One aggregated event per (candle, side) with at least one sweep."""
    data = ohlcv if "timestamp" in ohlcv.columns else normalize_ohlcv_dataframe(ohlcv)
    by_id = _levels_by_id(result)
    events: list[CandleSweepEvent] = []
    seq = 0
    for st in result.candle_states:
        if not st.swept_level_ids:
            continue
        swept_levels = [by_id[i] for i in st.swept_level_ids if i in by_id]
        for side in (SIDE_LOWER, SIDE_UPPER):
            side_lvls = [lvl for lvl in swept_levels if lvl.side == side]
            if not side_lvls:
                continue
            seq += 1
            events.append(
                _aggregate_side_levels(
                    side_lvls,
                    signal_index=int(st.candle_index),
                    data=data,
                    st=st,
                    side=side,
                    seq=seq,
                )
            )
    return events


def build_cluster_features(
    result: LiquidationReplayResult,
    ohlcv: pd.DataFrame,
    *,
    max_gap_pct: float = 0.10,
    min_level_count: int = 1,
    min_total_strength: int = 1,
    snapshot_every_candle: bool = False,
) -> tuple[list[ClusterSnapshot], list[ClusterSweepEvent]]:
    """Cluster active-before levels; emit cluster-sweep events causally."""
    data = ohlcv if "timestamp" in ohlcv.columns else normalize_ohlcv_dataframe(ohlcv)
    by_id = _levels_by_id(result)
    snapshots: list[ClusterSnapshot] = []
    cluster_events: list[ClusterSweepEvent] = []
    snap_seq = 0
    evt_seq = 0

    for st in result.candle_states:
        i = int(st.candle_index)
        active_levels = [by_id[lid] for lid in st.active_level_ids_before if lid in by_id]
        swept_ids = set(int(x) for x in st.swept_level_ids)
        row = data.iloc[i]
        ts = pd.Timestamp(row["timestamp"])
        geo = candle_geometry(row["open"], row["high"], row["low"], row["close"])

        for side in (SIDE_LOWER, SIDE_UPPER):
            clusters = cluster_levels_by_price_gap(
                active_levels, side=side, candle_index=i, max_gap_pct=max_gap_pct
            )
            for c_idx, cluster in enumerate(clusters):
                ages = [i - int(lvl.created_index) for lvl in cluster]
                prices = [float(lvl.level_price) for lvl in cluster]
                strengths = [int(lvl.strength) for lvl in cluster]
                total_strength = int(sum(strengths))
                if len(cluster) < int(min_level_count) or total_strength < int(min_total_strength):
                    continue
                cluster_id = f"CL_{i:06d}_{side}_{c_idx:03d}"
                snap = ClusterSnapshot(
                    cluster_id=cluster_id,
                    candle_index=i,
                    timestamp=ts,
                    side=side,
                    level_count=len(cluster),
                    total_strength=total_strength,
                    min_price=float(min(prices)),
                    max_price=float(max(prices)),
                    center_price=weighted_center(prices, strengths),
                    oldest_age=int(max(ages)),
                    median_age=float(np.median(ages)),
                    leverage_count=len({int(lvl.leverage) for lvl in cluster}),
                    level_ids=tuple(int(lvl.level_id) for lvl in cluster),
                )
                # Keep CSV bounded: always snapshot when sweep on this side, or dense mode.
                side_swept = any(lvl.side == side and int(lvl.level_id) in swept_ids for lvl in active_levels)
                # Also include levels created then swept same bar? They are in active after create
                # before sweep check — they are in `active` list during sweep, but active_before
                # excludes them. Cluster uses active_before only (pre-candle). Same-bar creates
                # cannot be swept, so OK.
                if snapshot_every_candle or side_swept or len(cluster) >= 2:
                    snap_seq += 1
                    snapshots.append(snap)

                hit, reason, swept = cluster_is_swept(cluster, swept_ids)
                if not hit:
                    continue
                evt_seq += 1
                s_ages = [i - int(lvl.created_index) for lvl in swept]
                s_prices = [float(lvl.level_price) for lvl in swept]
                s_strengths = [int(lvl.strength) for lvl in swept]
                entry_index = i + 1
                cluster_events.append(
                    ClusterSweepEvent(
                        event_id=f"CLS_{evt_seq:06d}",
                        cluster_id=cluster_id,
                        signal_index=i,
                        signal_timestamp=ts,
                        entry_index=entry_index,
                        entry_timestamp=_entry_ts(data, entry_index),
                        side=side,
                        swept_level_count=len(swept),
                        swept_total_strength=int(sum(s_strengths)),
                        level_count_in_cluster=len(cluster),
                        total_strength_in_cluster=int(sum(strengths)),
                        min_price=float(min(s_prices)),
                        max_price=float(max(s_prices)),
                        center_price=weighted_center(s_prices, s_strengths),
                        oldest_age=int(max(s_ages)),
                        median_age=float(np.median(s_ages)),
                        leverage_count=len({int(lvl.leverage) for lvl in swept}),
                        sweep_candle_open=float(row["open"]),
                        sweep_candle_high=float(row["high"]),
                        sweep_candle_low=float(row["low"]),
                        sweep_candle_close=float(row["close"]),
                        sweep_candle_volume=float(row["volume"]),
                        sweep_body_pct=geo["sweep_body_pct"],
                        upper_wick_pct=geo["upper_wick_pct"],
                        lower_wick_pct=geo["lower_wick_pct"],
                        close_location_value=geo["close_location_value"],
                        trigger_reason=reason,
                        swept_level_ids=tuple(int(lvl.level_id) for lvl in swept),
                    )
                )
    return snapshots, cluster_events


def _signal(
    *,
    variant: str,
    direction: str,
    signal_index: int,
    signal_timestamp: pd.Timestamp,
    entry_index: int,
    entry_timestamp: pd.Timestamp | None,
    source_event_id: str,
    side: str,
    clv: float,
    body: float,
    upper_wick: float,
    lower_wick: float,
    swept_count: int,
    swept_strength: int,
    seq: int,
    meta: dict[str, Any] | None = None,
) -> SignalEvent:
    return SignalEvent(
        signal_id=f"{variant}_{seq:06d}",
        variant=variant,
        direction=direction,
        signal_index=signal_index,
        signal_timestamp=signal_timestamp,
        entry_index=entry_index,
        entry_timestamp=entry_timestamp,
        source_event_id=source_event_id,
        side=side,
        close_location_value=float(clv),
        sweep_body_pct=float(body),
        upper_wick_pct=float(upper_wick),
        lower_wick_pct=float(lower_wick),
        swept_level_count=int(swept_count),
        swept_total_strength=int(swept_strength),
        meta=meta or {},
    )


def generate_signals(
    candle_events: Sequence[CandleSweepEvent],
    cluster_events: Sequence[ClusterSweepEvent],
) -> list[SignalEvent]:
    """Build L1–L7, S1–S7, F_LONG, F_SHORT from candle/cluster sweep events."""
    signals: list[SignalEvent] = []
    counters = {v: 0 for v in ALL_VARIANTS}

    lower_candle = [e for e in candle_events if e.side == SIDE_LOWER]
    upper_candle = [e for e in candle_events if e.side == SIDE_UPPER]
    lower_cluster = [e for e in cluster_events if e.side == SIDE_LOWER]
    upper_cluster = [e for e in cluster_events if e.side == SIDE_UPPER]

    def add(variant: str, direction: str, e: CandleSweepEvent | ClusterSweepEvent, **extra: Any) -> None:
        counters[variant] += 1
        signals.append(
            _signal(
                variant=variant,
                direction=direction,
                signal_index=e.signal_index,
                signal_timestamp=e.signal_timestamp,
                entry_index=e.entry_index,
                entry_timestamp=e.entry_timestamp,
                source_event_id=e.event_id,
                side=e.side,
                clv=e.close_location_value,
                body=e.sweep_body_pct,
                upper_wick=e.upper_wick_pct,
                lower_wick=e.lower_wick_pct,
                swept_count=e.swept_level_count,
                swept_strength=e.swept_total_strength,
                seq=counters[variant],
                meta=extra,
            )
        )

    # Long reversals from lower sweeps
    for e in lower_candle:
        add("L1", "long", e)
        if e.swept_total_strength >= 3:
            add("L2", "long", e)
        if e.swept_level_count >= 2:
            add("L3", "long", e)

    for e in lower_cluster:
        add("L4", "long", e, cluster_id=e.cluster_id, trigger_reason=e.trigger_reason)
        if e.close_location_value >= 0.60:
            add("L5", "long", e, cluster_id=e.cluster_id)
        if e.lower_wick_pct > e.sweep_body_pct:
            add("L6", "long", e, cluster_id=e.cluster_id)
        if e.close_location_value >= 0.60 and e.lower_wick_pct > e.sweep_body_pct:
            add("L7", "long", e, cluster_id=e.cluster_id)
        if e.close_location_value <= 0.25:
            add("F_SHORT", "short", e, cluster_id=e.cluster_id)

    # Short reversals from upper sweeps
    for e in upper_candle:
        add("S1", "short", e)
        if e.swept_total_strength >= 3:
            add("S2", "short", e)
        if e.swept_level_count >= 2:
            add("S3", "short", e)

    for e in upper_cluster:
        add("S4", "short", e, cluster_id=e.cluster_id, trigger_reason=e.trigger_reason)
        if e.close_location_value <= 0.40:
            add("S5", "short", e, cluster_id=e.cluster_id)
        if e.upper_wick_pct > e.sweep_body_pct:
            add("S6", "short", e, cluster_id=e.cluster_id)
        if e.close_location_value <= 0.40 and e.upper_wick_pct > e.sweep_body_pct:
            add("S7", "short", e, cluster_id=e.cluster_id)
        if e.close_location_value >= 0.75:
            add("F_LONG", "long", e, cluster_id=e.cluster_id)

    return signals


def build_feature_bundle(
    result: LiquidationReplayResult,
    ohlcv: pd.DataFrame,
    config: FeatureConfig | None = None,
) -> FeatureBundle:
    cfg = config or FeatureConfig()
    data = normalize_ohlcv_dataframe(ohlcv)
    level_events = build_level_sweep_events(result, data)
    candle_events = build_candle_sweep_events(result, data)
    snapshots, cluster_events = build_cluster_features(
        result,
        data,
        max_gap_pct=cfg.cluster_max_gap_pct,
        min_level_count=cfg.cluster_min_level_count,
        min_total_strength=cfg.cluster_min_total_strength,
        snapshot_every_candle=False,
    )
    signals = generate_signals(candle_events, cluster_events)
    summary = {
        "candle_count": len(data),
        "level_sweep_events": len(level_events),
        "candle_sweep_events": len(candle_events),
        "candle_sweep_lower": sum(1 for e in candle_events if e.side == SIDE_LOWER),
        "candle_sweep_upper": sum(1 for e in candle_events if e.side == SIDE_UPPER),
        "cluster_snapshots": len(snapshots),
        "cluster_sweep_events": len(cluster_events),
        "signals_total": len(signals),
        "signals_by_variant": {
            v: sum(1 for s in signals if s.variant == v) for v in ALL_VARIANTS
        },
        "cluster_max_gap_pct": cfg.cluster_max_gap_pct,
    }
    return FeatureBundle(
        ohlcv=data,
        level_events=level_events,
        candle_events=candle_events,
        cluster_snapshots=snapshots,
        cluster_events=cluster_events,
        signals=signals,
        summary=summary,
    )


def _records(objs: Iterable[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for obj in objs:
        row = asdict(obj)
        for k, v in list(row.items()):
            if isinstance(v, pd.Timestamp):
                row[k] = str(v)
            elif isinstance(v, tuple):
                row[k] = ",".join(str(x) for x in v)
            elif isinstance(v, dict):
                row[k] = str(v)
        rows.append(row)
    return rows


def level_events_to_dataframe(events: Sequence[LevelSweepEvent]) -> pd.DataFrame:
    return pd.DataFrame(_records(events))


def candle_events_to_dataframe(events: Sequence[CandleSweepEvent]) -> pd.DataFrame:
    return pd.DataFrame(_records(events))


def cluster_snapshots_to_dataframe(events: Sequence[ClusterSnapshot]) -> pd.DataFrame:
    return pd.DataFrame(_records(events))


def cluster_events_to_dataframe(events: Sequence[ClusterSweepEvent]) -> pd.DataFrame:
    return pd.DataFrame(_records(events))


def signals_to_dataframe(events: Sequence[SignalEvent]) -> pd.DataFrame:
    return pd.DataFrame(_records(events))
