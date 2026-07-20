"""Phase F0 – causal 2%%-leg speed and path metrics (offline research only).

Level semantics
---------------
Reference price is ``short_avg_after_lock``. Down-levels are
``ref * (1 + level_pct)`` for ``level_pct`` in ``down_levels_pct``.

Primary touch mode ``first_low_touch``: first bar whose ``low <= level``.
Diagnostic ``first_close_below``: first bar whose ``close < level``.

Within one sequence each level counts only once (first touch). Multiple
levels touched by the same candle are all attributed to that bar in
descending price order (most shallow first).

Leg duration uses bar timestamps (5m). ``hours_for_leg`` is floored by
``minimum_leg_duration_seconds`` to avoid division by zero.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

BAR_SECONDS_5M = 300.0


@dataclass(frozen=True)
class PhaseF0Config:
    """Central F0 parameters — no hidden magic numbers in metrics code."""

    down_levels_pct: tuple[float, ...] = (
        0.00,
        -0.02,
        -0.04,
        -0.06,
        -0.08,
        -0.10,
        -0.12,
        -0.15,
    )
    forward_horizons_bars: tuple[int, ...] = (6, 12, 24, 48, 96, 288)
    minimum_leg_duration_seconds: float = 1.0
    minimum_group_sample_size: int = 5
    atr_period: int = 14
    rebound_episode_thresholds_pct: tuple[float, ...] = (0.0025, 0.005, 0.01)
    test_unlock_fraction: float = 0.25
    recovery_tp_pct: float = 0.01
    recovery_stop_pct: float = 0.005
    wait_bars: tuple[int, ...] = (6, 12, 24, 48)
    rebound_confirm_pct: float = 0.005
    fee_rate: float = 0.00055
    slippage_bps: float = 2.0
    long_notional_usdt: float = 100.0
    short_notional_usdt: float = 100.0
    slowdown_bucket_edges: tuple[tuple[float | None, float | None, str], ...] = (
        (None, 0.50, "<0.50_stark_beschleunigt"),
        (0.50, 0.80, "0.50-0.80_beschleunigt"),
        (0.80, 1.25, "0.80-1.25_aehnlich"),
        (1.25, 2.00, "1.25-2.00_verlangsamt"),
        (2.00, None, ">2.00_stark_verlangsamt"),
    )
    duration_bucket_minutes: tuple[tuple[float | None, float | None, str], ...] = (
        (None, 30.0, "<30m"),
        (30.0, 60.0, "30-60m"),
        (60.0, 120.0, "1-2h"),
        (120.0, 240.0, "2-4h"),
        (240.0, 480.0, "4-8h"),
        (480.0, 1440.0, "8-24h"),
        (1440.0, None, ">24h"),
    )
    path_efficiency_buckets: tuple[tuple[float | None, float | None, str], ...] = (
        (None, 0.20, "<0.20_chop"),
        (0.20, 0.35, "0.20-0.35_mixed"),
        (0.35, 0.60, "0.35-0.60_directional"),
        (0.60, None, ">=0.60_efficient"),
    )
    first_touch_races: tuple[tuple[float, float], ...] = (
        (0.005, 0.005),
        (0.0075, 0.005),
        (0.01, 0.005),
        (0.01, 0.0075),
        (0.01, 0.01),
        (0.015, 0.005),
        (0.02, 0.005),
    )
    # Same-bar TP+Stop: conservative assume stop first.
    same_bar_collision_policy: str = "stop_first"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ts_to_datetime(ts: Any) -> datetime:
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts
    s = str(ts)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def _ts_iso(ts: Any) -> str:
    return _ts_to_datetime(ts).isoformat()


def _minutes_between(ts_a: Any, ts_b: Any) -> float:
    return max(
        (_ts_to_datetime(ts_b) - _ts_to_datetime(ts_a)).total_seconds() / 60.0,
        0.0,
    )


def bucket_by_edges(
    value: float | None,
    edges: Sequence[tuple[float | None, float | None, str]],
) -> str | None:
    if value is None or value != value:  # NaN
        return None
    for lo, hi, name in edges:
        if lo is not None and value < lo - 1e-15:
            continue
        if hi is not None and value >= hi - 1e-15:
            continue
        return name
    return None


def true_range_at(
    candles: Sequence[dict[str, Any]], index: int
) -> float:
    h = float(candles[index]["high"])
    l = float(candles[index]["low"])
    if index <= 0:
        return max(h - l, 0.0)
    prev_c = float(candles[index - 1]["close"])
    return max(h - l, abs(h - prev_c), abs(l - prev_c), 0.0)


def atr_mean_pct(
    candles: Sequence[dict[str, Any]],
    *,
    start: int,
    end: int,
    period: int = 14,
) -> tuple[float | None, float | None]:
    """Mean / max ATR%% over ``[start, end]`` using trailing ATR at each bar."""
    if end < start or end >= len(candles):
        return None, None
    trs: list[float] = []
    atrs: list[float] = []
    for i in range(0, end + 1):
        trs.append(true_range_at(candles, i))
        window = trs[max(0, i - period + 1) : i + 1]
        atr = sum(window) / len(window)
        if i >= start:
            close = max(float(candles[i]["close"]), 1e-12)
            atrs.append(atr / close)
    if not atrs:
        return None, None
    return float(sum(atrs) / len(atrs)), float(max(atrs))


def close_path_efficiency(
    candles: Sequence[dict[str, Any]], *, start: int, end: int
) -> float | None:
    if end <= start:
        return None
    closes = [float(candles[i]["close"]) for i in range(start, end + 1)]
    net = abs(closes[-1] - closes[0])
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    if path <= 1e-15:
        return 1.0 if net <= 1e-15 else 0.0
    return float(net / path)


def tr_path_efficiency(
    candles: Sequence[dict[str, Any]],
    *,
    start: int,
    end: int,
    start_price: float,
    end_price: float,
) -> float | None:
    if end < start:
        return None
    total_tr = sum(true_range_at(candles, i) for i in range(start, end + 1))
    net = abs(float(end_price) - float(start_price))
    if total_tr <= 1e-12:
        return 1.0 if net <= 1e-15 else 0.0
    return float(net / total_tr)


def high_low_range_pct(
    candles: Sequence[dict[str, Any]], *, start: int, end: int, ref: float
) -> float | None:
    if end < start or ref <= 0:
        return None
    hi = max(float(candles[i]["high"]) for i in range(start, end + 1))
    lo = min(float(candles[i]["low"]) for i in range(start, end + 1))
    return float((hi - lo) / ref)


def close_std_pct(
    candles: Sequence[dict[str, Any]], *, start: int, end: int, ref: float
) -> float | None:
    if end < start or ref <= 0:
        return None
    closes = [float(candles[i]["close"]) for i in range(start, end + 1)]
    if len(closes) < 2:
        return 0.0
    mean = sum(closes) / len(closes)
    var = sum((c - mean) ** 2 for c in closes) / (len(closes) - 1)
    return float((var**0.5) / ref)


@dataclass
class ReboundEpisodeCounter:
    """Causal rebound episodes from running local low (no double-count)."""

    thresholds: tuple[float, ...]
    _running_low: float | None = None
    _armed: dict[float, bool] = field(default_factory=dict)
    counts: dict[float, int] = field(default_factory=dict)
    max_rebound_pct: float = 0.0

    def __post_init__(self) -> None:
        self.counts = {t: 0 for t in self.thresholds}
        self._armed = {t: False for t in self.thresholds}

    def reset(self, price: float) -> None:
        self._running_low = float(price)
        self.max_rebound_pct = 0.0
        for t in self.thresholds:
            self._armed[t] = False

    def update(self, *, low: float, high: float, close: float) -> None:
        px_low = float(low)
        px_high = float(high)
        if self._running_low is None:
            self.reset(px_low)
            return
        if px_low < self._running_low - 1e-15:
            self._running_low = px_low
            for t in self.thresholds:
                self._armed[t] = False
        assert self._running_low is not None
        if self._running_low <= 1e-15:
            return
        rebound = (px_high - self._running_low) / self._running_low
        self.max_rebound_pct = max(self.max_rebound_pct, rebound)
        for t in self.thresholds:
            if rebound + 1e-15 >= t and not self._armed[t]:
                self.counts[t] += 1
                self._armed[t] = True
        _ = close


def find_level_crossings(
    candles: Sequence[dict[str, Any]],
    *,
    reference_price: float,
    levels_pct: Sequence[float],
    start_index: int,
    end_index: int,
    touch_mode: str = "first_low_touch",
    event_id: str = "",
    window_truncated: bool = False,
) -> list[dict[str, Any]]:
    """Return first-touch crossings for each down-level (once per level)."""
    if reference_price <= 0:
        raise ValueError("reference_price must be positive")
    # Sort by level price descending (0% first, then -2%, ...)
    ordered = sorted(
        [(i, float(p)) for i, p in enumerate(levels_pct)],
        key=lambda x: -x[1],
    )
    targets: list[tuple[int, float, float]] = []
    for level_index, pct in ordered:
        price = float(reference_price) * (1.0 + pct)
        targets.append((level_index, pct, price))

    remaining = list(targets)
    crossings: list[dict[str, Any]] = []
    start_ts = candles[start_index]["timestamp"] if start_index < len(candles) else None

    for bar_i in range(start_index, min(end_index, len(candles) - 1) + 1):
        if not remaining:
            break
        candle = candles[bar_i]
        hit_now: list[tuple[int, float, float]] = []
        still: list[tuple[int, float, float]] = []
        for item in remaining:
            _, pct, level_price = item
            if touch_mode == "first_low_touch":
                touched = float(candle["low"]) <= level_price + 1e-15
            elif touch_mode == "first_close_below":
                touched = float(candle["close"]) < level_price - 1e-15
            else:
                raise ValueError(f"unknown touch_mode: {touch_mode}")
            if touched:
                hit_now.append(item)
            else:
                still.append(item)
        remaining = still
        # Shallow levels first within the same candle.
        hit_now.sort(key=lambda x: -x[1])
        for level_index, pct, level_price in hit_now:
            minutes = _minutes_between(start_ts, candle["timestamp"]) if start_ts else 0.0
            # From previous crossing if any, else from sequence start.
            if crossings:
                prev = crossings[-1]
                leg_start_bar = int(prev["end_bar"])
                leg_start_ts = prev["end_timestamp"]
                leg_start_price = float(prev["level_price"])
            else:
                leg_start_bar = start_index
                leg_start_ts = start_ts
                leg_start_price = float(reference_price)
            bars = max(int(bar_i) - int(leg_start_bar), 0)
            minutes_leg = (
                _minutes_between(leg_start_ts, candle["timestamp"])
                if leg_start_ts is not None
                else bars * (BAR_SECONDS_5M / 60.0)
            )
            crossings.append(
                {
                    "event_id": event_id,
                    "touch_mode": touch_mode,
                    "level_index": level_index,
                    "level_pct": pct,
                    "level_price": level_price,
                    "start_timestamp": _ts_iso(leg_start_ts) if leg_start_ts else None,
                    "end_timestamp": _ts_iso(candle["timestamp"]),
                    "start_bar": leg_start_bar,
                    "end_bar": bar_i,
                    "bars_needed": bars,
                    "minutes_needed": minutes_leg,
                    "hours_needed": minutes_leg / 60.0,
                    "sequence_start_timestamp": _ts_iso(start_ts) if start_ts else None,
                    "sequence_minutes_from_ref": minutes,
                    "actual_start_price": leg_start_price,
                    "actual_end_price": level_price,
                    "candle_low": float(candle["low"]),
                    "candle_close": float(candle["close"]),
                    "previous_level_complete": True,
                    "window_truncated_at_data_end": window_truncated,
                    "reference_price": float(reference_price),
                }
            )
    # Mark incomplete trailing intent: levels never reached stay absent.
    return crossings


def build_leg_metrics(
    crossings: Sequence[dict[str, Any]],
    candles: Sequence[dict[str, Any]],
    cfg: PhaseF0Config,
    *,
    event_id: str = "",
) -> list[dict[str, Any]]:
    """Build 2%% legs between consecutive crossings (skip 0%% as destination)."""
    # Use crossings sorted by level_pct descending.
    ordered = sorted(crossings, key=lambda r: -float(r["level_pct"]))
    legs: list[dict[str, Any]] = []
    for i in range(1, len(ordered)):
        prev = ordered[i - 1]
        cur = ordered[i]
        # Only adjacent configured levels of size ~2%.
        leg_size = abs(float(cur["level_pct"]) - float(prev["level_pct"]))
        if abs(leg_size - 0.02) > 1e-9 and abs(leg_size - 0.03) > 1e-9:
            # Allow 0→-2 and also final -12→-15 (3%) as diagnostic; mark size.
            pass
        start_bar = int(prev["end_bar"])
        end_bar = int(cur["end_bar"])
        minutes = float(cur["minutes_needed"])
        seconds = max(minutes * 60.0, float(cfg.minimum_leg_duration_seconds))
        hours = seconds / 3600.0
        speed = float(leg_size) / hours if hours > 0 else float("nan")
        bars = max(end_bar - start_bar, 0)
        # Path metrics over (start_bar, end_bar]; if same bar, use that bar.
        path_start = start_bar if end_bar > start_bar else max(start_bar - 1, 0)
        if end_bar == start_bar:
            path_start = start_bar
        cpe = close_path_efficiency(candles, start=path_start, end=end_bar)
        tpe = tr_path_efficiency(
            candles,
            start=path_start,
            end=end_bar,
            start_price=float(prev["level_price"]),
            end_price=float(cur["level_price"]),
        )
        atr_mean, atr_max = atr_mean_pct(
            candles, start=path_start, end=end_bar, period=cfg.atr_period
        )
        hl_range = high_low_range_pct(
            candles,
            start=path_start,
            end=end_bar,
            ref=float(prev["level_price"]),
        )
        cstd = close_std_pct(
            candles,
            start=path_start,
            end=end_bar,
            ref=float(prev["level_price"]),
        )
        counter = ReboundEpisodeCounter(thresholds=cfg.rebound_episode_thresholds_pct)
        counter.reset(float(prev["level_price"]))
        for bi in range(path_start, end_bar + 1):
            c = candles[bi]
            counter.update(
                low=float(c["low"]), high=float(c["high"]), close=float(c["close"])
            )

        prev_leg = legs[-1] if legs else None
        slowdown = None
        speed_ratio = None
        if prev_leg is not None and float(prev_leg["bars_for_leg"]) > 0:
            slowdown = float(bars) / float(prev_leg["bars_for_leg"])
        elif prev_leg is not None and float(prev_leg["bars_for_leg"]) == 0:
            # Previous leg instantaneous; current relative to min duration.
            slowdown = float("nan") if bars == 0 else float("inf")
        if (
            prev_leg is not None
            and prev_leg.get("speed_pct_per_hour") is not None
            and float(prev_leg["speed_pct_per_hour"]) == float(prev_leg["speed_pct_per_hour"])
            and float(prev_leg["speed_pct_per_hour"]) > 1e-15
            and speed == speed
        ):
            speed_ratio = float(speed) / float(prev_leg["speed_pct_per_hour"])

        legs.append(
            {
                "event_id": event_id,
                "touch_mode": cur.get("touch_mode"),
                "from_level_pct": float(prev["level_pct"]),
                "to_level_pct": float(cur["level_pct"]),
                "leg_size_pct": float(leg_size),
                "from_level_price": float(prev["level_price"]),
                "to_level_price": float(cur["level_price"]),
                "start_bar": start_bar,
                "end_bar": end_bar,
                "start_timestamp": prev.get("end_timestamp"),
                "end_timestamp": cur.get("end_timestamp"),
                "bars_for_leg": bars,
                "minutes_for_leg": minutes,
                "hours_for_leg": hours,
                "speed_pct_per_hour": speed,
                "slowdown_ratio": slowdown,
                "speed_ratio": speed_ratio,
                "previous_leg_present": prev_leg is not None,
                "slowdown_bucket": bucket_by_edges(
                    None if slowdown != slowdown else slowdown,
                    cfg.slowdown_bucket_edges,
                )
                if slowdown is not None and slowdown == slowdown and slowdown != float("inf")
                else (
                    ">2.00_stark_verlangsamt"
                    if slowdown == float("inf")
                    else None
                ),
                "duration_bucket": bucket_by_edges(minutes, cfg.duration_bucket_minutes),
                "close_path_efficiency": cpe,
                "tr_path_efficiency": tpe,
                "path_efficiency_bucket": bucket_by_edges(
                    cpe, cfg.path_efficiency_buckets
                ),
                "max_intermediate_rebound_pct": counter.max_rebound_pct,
                "number_of_rebounds_0_25pct": counter.counts.get(0.0025, 0),
                "number_of_rebounds_0_50pct": counter.counts.get(0.005, 0),
                "number_of_rebounds_1_00pct": counter.counts.get(0.01, 0),
                "high_low_range_pct": hl_range,
                "close_std_pct": cstd,
                "atr_mean_pct": atr_mean,
                "atr_max_pct": atr_max,
                "directional_efficiency": cpe,
                "window_truncated_at_data_end": cur.get("window_truncated_at_data_end"),
                "reference_price": cur.get("reference_price"),
            }
        )
    return legs
