"""Causal multi-start point selection for two_early_medium vs legacy validation.

All regime labels use only candles ``0..start_index`` inclusive.
No future closes, highs, or PnL enter the selection.
"""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from research.backtests.inventory_mtm_freeze import safe_float

# Default research scope: mix of prior drivers, regressions, and majors.
DEFAULT_COINS: tuple[str, ...] = (
    "APTUSDT",
    "ATOMUSDT",
    "ADAUSDT",
    "ARBUSDT",
    "SUIUSDT",
    "SEIUSDT",
    "TIAUSDT",
    "TRXUSDT",
    "DOTUSDT",
    "OPUSDT",
    "BTCUSDT",
    "ETHUSDT",
)

CATEGORY_GRID = "grid"
CATEGORY_RANDOM = "random"
CATEGORY_BULLISH = "bullish"
CATEGORY_BEARISH = "bearish"
CATEGORY_RANGE = "range"
CATEGORY_HIGH_VOL = "high_vol"
CATEGORY_LOW_VOL = "low_vol"
CATEGORY_PRE_HIGH_VOL = "pre_high_vol"
CATEGORY_HISTORICAL_BLOCKER = "historical_blocker"
CATEGORY_NEUTRAL_POOL = "neutral_pool"  # grid ∪ random (analysis grouping)

REGIME_CATEGORIES = (
    CATEGORY_BULLISH,
    CATEGORY_BEARISH,
    CATEGORY_RANGE,
    CATEGORY_HIGH_VOL,
    CATEGORY_LOW_VOL,
    CATEGORY_PRE_HIGH_VOL,
)

ALL_START_CATEGORIES = (
    CATEGORY_GRID,
    CATEGORY_RANDOM,
    *REGIME_CATEGORIES,
    CATEGORY_HISTORICAL_BLOCKER,
)

DEFAULT_SEED = 20260721
DEFAULT_WARMUP = 240
DEFAULT_MIN_REMAINING = 1500
DEFAULT_GRID_STEP = 1200
DEFAULT_TARGET_PER_COIN = 40
DEFAULT_EMA_FAST = 20
DEFAULT_EMA_SLOW = 50
DEFAULT_ATR_PERIOD = 14
DEFAULT_ATR_LOOKBACK = 120
DEFAULT_SLOPE_LOOKBACK = 48


@dataclass(frozen=True)
class StartPoint:
    coin: str
    start_index: int
    primary_category: str
    categories: tuple[str, ...]
    selection_rank: int
    causal_features: dict[str, Any] = field(default_factory=dict)

    @property
    def pair_key(self) -> str:
        return pair_key(self.coin, self.start_index)


def pair_key(coin: str, start_index: int) -> str:
    return f"{str(coin).upper()}|{int(start_index)}"


def profile_run_key(coin: str, start_index: int, profile: str) -> str:
    return f"{pair_key(coin, start_index)}|{profile}"


def _candle_close(c: Any) -> float:
    if isinstance(c, dict):
        return float(c["close"])
    return float(c.close)


def _candle_high(c: Any) -> float:
    if isinstance(c, dict):
        return float(c["high"])
    return float(c.high)


def _candle_low(c: Any) -> float:
    if isinstance(c, dict):
        return float(c["low"])
    return float(c.low)


def _ema_series(values: Sequence[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("ema period must be positive")
    out: list[float | None] = [None] * len(values)
    if not values:
        return out
    alpha = 2.0 / (period + 1.0)
    ema_v: float | None = None
    for i, v in enumerate(values):
        if ema_v is None:
            ema_v = float(v)
        else:
            ema_v = alpha * float(v) + (1.0 - alpha) * ema_v
        out[i] = ema_v
    return out


def _atr_pct_series(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int,
) -> list[float | None]:
    if period <= 0:
        raise ValueError("atr period must be positive")
    n = len(closes)
    trs: list[float] = []
    for i in range(n):
        if i == 0:
            trs.append(max(0.0, highs[i] - lows[i]))
        else:
            trs.append(
                max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]),
                )
            )
    atr: float | None = None
    out: list[float | None] = [None] * n
    alpha = 1.0 / float(period)
    for i, tr in enumerate(trs):
        if atr is None:
            atr = float(tr)
        else:
            atr = alpha * float(tr) + (1.0 - alpha) * atr
        c = closes[i]
        out[i] = (100.0 * atr / c) if c > 0 else None
    return out


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def compute_causal_feature_frame(candles: Sequence[Any]) -> dict[str, list[Any]]:
    """Precompute causal feature series for an entire candle list (no lookahead)."""
    closes = [_candle_close(c) for c in candles]
    highs = [_candle_high(c) for c in candles]
    lows = [_candle_low(c) for c in candles]
    ema_fast = _ema_series(closes, DEFAULT_EMA_FAST)
    ema_slow = _ema_series(closes, DEFAULT_EMA_SLOW)
    atr_pct = _atr_pct_series(highs, lows, closes, DEFAULT_ATR_PERIOD)
    return {
        "close": closes,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "atr_pct": atr_pct,
    }


def classify_regimes_at_index(
    frame: dict[str, list[Any]],
    index: int,
    *,
    atr_lookback: int = DEFAULT_ATR_LOOKBACK,
    slope_lookback: int = DEFAULT_SLOPE_LOOKBACK,
) -> tuple[set[str], dict[str, Any]]:
    """Return regime tags active at ``index`` using only ``0..index``."""
    if index < 0 or index >= len(frame["close"]):
        return set(), {}
    close = float(frame["close"][index])
    ema_f = frame["ema_fast"][index]
    ema_s = frame["ema_slow"][index]
    atr = frame["atr_pct"][index]
    if ema_f is None or ema_s is None or atr is None or close <= 0:
        return set(), {}

    slope_i = index - slope_lookback
    slope = None
    if slope_i >= 0 and frame["ema_slow"][slope_i] not in (None, 0):
        slope = 100.0 * (float(ema_s) - float(frame["ema_slow"][slope_i])) / float(
            frame["ema_slow"][slope_i]
        )

    dist_pct = 100.0 * (float(ema_f) - float(ema_s)) / close
    tags: set[str] = set()
    if slope is not None:
        if float(ema_f) > float(ema_s) and slope > 0.15:
            tags.add(CATEGORY_BULLISH)
        if float(ema_f) < float(ema_s) and slope < -0.15:
            tags.add(CATEGORY_BEARISH)
        if abs(dist_pct) < 0.35 and abs(slope) < 0.12:
            tags.add(CATEGORY_RANGE)

    # Rolling ATR% quantiles ending at index (causal).
    start = max(0, index - atr_lookback + 1)
    window = [float(v) for v in frame["atr_pct"][start : index + 1] if v is not None]
    features: dict[str, Any] = {
        "close": close,
        "ema_fast": float(ema_f),
        "ema_slow": float(ema_s),
        "atr_pct": float(atr),
        "ema_dist_pct": dist_pct,
        "ema_slow_slope_pct": slope,
        "atr_window_n": len(window),
    }
    if len(window) >= max(20, atr_lookback // 3):
        ws = sorted(window)
        p20 = _percentile(ws, 20)
        p40 = _percentile(ws, 40)
        p70 = _percentile(ws, 70)
        p80 = _percentile(ws, 80)
        features.update({"atr_p20": p20, "atr_p40": p40, "atr_p70": p70, "atr_p80": p80})
        if float(atr) >= p80:
            tags.add(CATEGORY_HIGH_VOL)
        if float(atr) <= p20:
            tags.add(CATEGORY_LOW_VOL)
        # Pre-high-vol: mid ATR with rising short ATR slope (still causal).
        atr_prev_i = index - max(8, slope_lookback // 3)
        atr_prev = frame["atr_pct"][atr_prev_i] if atr_prev_i >= 0 else None
        if atr_prev is not None and p40 <= float(atr) <= p70 and float(atr) > float(atr_prev) * 1.08:
            tags.add(CATEGORY_PRE_HIGH_VOL)
            features["atr_rise_ratio"] = float(atr) / float(atr_prev) if atr_prev else None

    return tags, features


def eligible_indices(
    n_candles: int,
    *,
    warmup: int = DEFAULT_WARMUP,
    min_remaining: int = DEFAULT_MIN_REMAINING,
) -> list[int]:
    last = n_candles - int(min_remaining)
    if last < int(warmup):
        return []
    return list(range(int(warmup), last + 1))


def _stable_coin_seed(base_seed: int, coin: str) -> int:
    digest = hashlib.sha256(f"{base_seed}:{coin.upper()}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16) % (2**31 - 1)


def select_start_points_for_coin(
    *,
    coin: str,
    candles: Sequence[Any],
    historical_blocker_starts: Iterable[int] = (),
    target_total: int = DEFAULT_TARGET_PER_COIN,
    seed: int = DEFAULT_SEED,
    warmup: int = DEFAULT_WARMUP,
    min_remaining: int = DEFAULT_MIN_REMAINING,
    grid_step: int = DEFAULT_GRID_STEP,
    regime_quota: int = 4,
    random_quota: int = 6,
    grid_quota: int | None = None,
) -> list[StartPoint]:
    """Deterministic causal start selection for one coin.

    Quotas are soft: missing regime candidates are not backfilled from future
    information; leftover slots are filled from remaining eligible grid/random
    indices without looking at outcomes.
    """
    coin_u = coin.upper()
    n = len(candles)
    eligible = eligible_indices(n, warmup=warmup, min_remaining=min_remaining)
    if not eligible:
        return []

    frame = compute_causal_feature_frame(candles)
    regime_map: dict[int, set[str]] = {}
    feature_map: dict[int, dict[str, Any]] = {}
    for idx in eligible:
        tags, feats = classify_regimes_at_index(frame, idx)
        if tags:
            regime_map[idx] = tags
        feature_map[idx] = feats

    selected: dict[int, set[str]] = {}

    def _add(idx: int, category: str) -> None:
        if idx not in eligible and category != CATEGORY_HISTORICAL_BLOCKER:
            # Blockers may sit near series end; still include as reference if in range.
            if not (0 <= idx < n):
                return
        selected.setdefault(int(idx), set()).add(category)

    # 1) Historical blockers (reference group) — fixed externally, still causal starts.
    for raw in historical_blocker_starts:
        idx = int(raw)
        if 0 <= idx < n:
            _add(idx, CATEGORY_HISTORICAL_BLOCKER)

    # 2) Regular grid
    gq = grid_quota if grid_quota is not None else max(8, target_total // 4)
    grid_idxs = [i for i in eligible if (i - eligible[0]) % int(grid_step) == 0]
    for idx in grid_idxs[:gq]:
        _add(idx, CATEGORY_GRID)

    # 3) Deterministic random
    rng = random.Random(_stable_coin_seed(seed, coin_u))
    pool = [i for i in eligible if i not in selected]
    rng.shuffle(pool)
    for idx in pool[:random_quota]:
        _add(idx, CATEGORY_RANDOM)

    # 4–7) Regime categories
    for cat in REGIME_CATEGORIES:
        candidates = sorted(i for i, tags in regime_map.items() if cat in tags)
        # Evenly spaced sample across the series to avoid clustering.
        if len(candidates) <= regime_quota:
            picks = candidates
        else:
            step = max(1, len(candidates) // regime_quota)
            picks = [candidates[min(i * step, len(candidates) - 1)] for i in range(regime_quota)]
            # de-dupe while preserving order
            seen: set[int] = set()
            uniq: list[int] = []
            for p in picks:
                if p not in seen:
                    seen.add(p)
                    uniq.append(p)
            picks = uniq[:regime_quota]
        for idx in picks:
            _add(idx, cat)

    # Fill up to target_total from remaining eligible (grid-priority then random)
    if len(selected) < target_total:
        need = target_total - len(selected)
        remain = [i for i in eligible if i not in selected]
        # Prefer continuing the grid, then RNG.
        more_grid = [i for i in grid_idxs if i not in selected]
        for idx in more_grid:
            if need <= 0:
                break
            _add(idx, CATEGORY_GRID)
            need -= 1
        if need > 0:
            rng2 = random.Random(_stable_coin_seed(seed + 17, coin_u))
            rest = [i for i in remain if i not in selected]
            rng2.shuffle(rest)
            for idx in rest[:need]:
                _add(idx, CATEGORY_RANDOM)

    # If over target (blockers + quotas), keep all — blockers are required reference.
    # Sort and assign primary category priority.
    priority = (
        CATEGORY_HISTORICAL_BLOCKER,
        CATEGORY_BULLISH,
        CATEGORY_BEARISH,
        CATEGORY_RANGE,
        CATEGORY_HIGH_VOL,
        CATEGORY_LOW_VOL,
        CATEGORY_PRE_HIGH_VOL,
        CATEGORY_GRID,
        CATEGORY_RANDOM,
    )
    points: list[StartPoint] = []
    for rank, idx in enumerate(sorted(selected)):
        cats = selected[idx]
        # Attach overlapping regime tags discovered causally even if not quota-picked.
        if idx in regime_map:
            cats = set(cats) | regime_map[idx]
        primary = next((p for p in priority if p in cats), next(iter(sorted(cats))))
        # Neutral analysis pool tag for grid/random (not exclusive).
        if CATEGORY_GRID in cats or CATEGORY_RANDOM in cats:
            cats = set(cats) | {CATEGORY_NEUTRAL_POOL}
        points.append(
            StartPoint(
                coin=coin_u,
                start_index=int(idx),
                primary_category=primary,
                categories=tuple(sorted(cats)),
                selection_rank=rank,
                causal_features=dict(feature_map.get(idx) or {}),
            )
        )
    return points


def select_universe_start_points(
    *,
    coin_candles: dict[str, Sequence[Any]],
    blocker_starts_by_coin: dict[str, list[int]] | None = None,
    target_per_coin: int = DEFAULT_TARGET_PER_COIN,
    seed: int = DEFAULT_SEED,
    warmup: int = DEFAULT_WARMUP,
    min_remaining: int = DEFAULT_MIN_REMAINING,
    grid_step: int = DEFAULT_GRID_STEP,
) -> list[StartPoint]:
    blocker_starts_by_coin = blocker_starts_by_coin or {}
    out: list[StartPoint] = []
    for coin in sorted(coin_candles):
        out.extend(
            select_start_points_for_coin(
                coin=coin,
                candles=coin_candles[coin],
                historical_blocker_starts=blocker_starts_by_coin.get(coin.upper(), []),
                target_total=target_per_coin,
                seed=seed,
                warmup=warmup,
                min_remaining=min_remaining,
                grid_step=grid_step,
            )
        )
    return out


def assert_no_lookahead_features(
    candles: Sequence[Any],
    start_index: int,
    features: dict[str, Any],
) -> None:
    """Sanity: recomputed features at start must match stored features."""
    frame = compute_causal_feature_frame(candles[: start_index + 1])
    tags, recomputed = classify_regimes_at_index(frame, start_index)
    _ = tags
    for key in ("close", "ema_fast", "ema_slow", "atr_pct"):
        if key not in features or key not in recomputed:
            continue
        a = safe_float(features[key])
        b = safe_float(recomputed[key])
        if abs(a - b) > 1e-9:
            raise AssertionError(f"lookahead/feature mismatch at {start_index} key={key}: {a} vs {b}")


def start_points_to_rows(points: Sequence[StartPoint]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in points:
        row = {
            "coin": p.coin,
            "start_index": p.start_index,
            "pair_key": p.pair_key,
            "primary_category": p.primary_category,
            "categories": list(p.categories),
            "selection_rank": p.selection_rank,
            "is_historical_blocker": CATEGORY_HISTORICAL_BLOCKER in p.categories,
            "is_neutral_pool": CATEGORY_NEUTRAL_POOL in p.categories,
        }
        for k, v in (p.causal_features or {}).items():
            row[f"feat_{k}"] = v
        rows.append(row)
    return rows
