"""Causal Python replication of LuxAlgo Liquidation Levels (Pine).

This module estimates potential liquidation price levels from OHLCV candles.
It does **not** read real exchange liquidation feeds.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

REFERENCE_PRICE_MODES = ("open", "close", "oc2", "hl2", "hlc3", "ohlc4", "hlcc4")
SIDE_UPPER = "upper"
SIDE_LOWER = "lower"
STATUS_ACTIVE = "active"
STATUS_SWEPT = "swept"
STATUS_REMOVED = "removed"
MIN_MOVE_DIVISOR = 333.0


@dataclass(frozen=True)
class LiquidationLevelConfig:
    """LuxAlgo-style level generation + research evaluation defaults.

    Baseline defaults match the original research replication exactly.
    Extra fields (cluster / reclaim / path) do not alter Pine level placement
    unless consumed by the caller.
    """

    reference_price: str = "open"
    volume_threshold: float = 1.7
    volatility_threshold: float = 10.0
    leverages: tuple[int, ...] = (25, 50, 100)
    volume_sma_period: int = 13
    max_active_levels: int = 500
    # Configurable research parameters (baseline-preserving defaults)
    minimum_move_divisor: float = MIN_MOVE_DIVISOR
    sweep_strict_cross: bool = True
    cluster_distance_pct: float = 0.10
    cluster_min_level_count: int = 2
    cluster_min_total_strength: int = 3
    reclaim_window_candles: int = 3
    path_horizon_candles: int = 50

    def __post_init__(self) -> None:
        mode = str(self.reference_price).strip().lower()
        if mode not in REFERENCE_PRICE_MODES:
            raise ValueError(
                f"unsupported reference_price={self.reference_price!r}; "
                f"expected one of {REFERENCE_PRICE_MODES}"
            )
        if self.volume_sma_period < 1:
            raise ValueError("volume_sma_period must be >= 1")
        if self.max_active_levels < 1:
            raise ValueError("max_active_levels must be >= 1")
        if not self.leverages:
            raise ValueError("leverages must be non-empty")
        if any(int(x) <= 0 for x in self.leverages):
            raise ValueError("all leverages must be positive integers")
        if len(self.leverages) != len(set(int(x) for x in self.leverages)):
            raise ValueError("duplicate leverages are not allowed")
        if float(self.minimum_move_divisor) <= 0:
            raise ValueError("minimum_move_divisor must be > 0")
        if float(self.cluster_distance_pct) <= 0:
            raise ValueError("cluster_distance_pct must be > 0")
        if int(self.cluster_min_level_count) < 1:
            raise ValueError("cluster_min_level_count must be >= 1")
        if int(self.cluster_min_total_strength) < 1:
            raise ValueError("cluster_min_total_strength must be >= 1")
        if int(self.reclaim_window_candles) < 1:
            raise ValueError("reclaim_window_candles must be >= 1")
        if int(self.path_horizon_candles) < 1:
            raise ValueError("path_horizon_candles must be >= 1")
        object.__setattr__(self, "reference_price", mode)
        object.__setattr__(self, "leverages", tuple(sorted({int(x) for x in self.leverages})))


@dataclass
class LiquidationLevel:
    level_id: int
    side: str
    leverage: int
    level_price: float
    reference_price: float
    created_index: int
    created_timestamp: pd.Timestamp
    created_open: float
    created_high: float
    created_low: float
    created_close: float
    created_volume: float
    volume_sma_13: float | None
    volume_ratio: float | None
    strength: int
    created_by_volume: bool
    created_by_volatility: bool
    status: str = STATUS_ACTIVE
    swept_index: int | None = None
    swept_timestamp: pd.Timestamp | None = None
    age_at_sweep: int | None = None
    removal_reason: str | None = None


@dataclass
class LiquidationCandleState:
    candle_index: int
    timestamp: pd.Timestamp
    active_upper_before: int
    active_lower_before: int
    active_strength_upper_before: int
    active_strength_lower_before: int
    created_upper: int
    created_lower: int
    created_strength_upper: int
    created_strength_lower: int
    swept_upper: int
    swept_lower: int
    swept_strength_upper: int
    swept_strength_lower: int
    active_upper_after: int
    active_lower_after: int
    active_strength_upper_after: int
    active_strength_lower_after: int
    nearest_upper_price_before: float | None
    nearest_lower_price_before: float | None
    nearest_upper_distance_pct_before: float | None
    nearest_lower_distance_pct_before: float | None
    active_level_ids_before: tuple[int, ...]
    created_level_ids: tuple[int, ...]
    swept_level_ids: tuple[int, ...]
    active_level_ids_after: tuple[int, ...]


@dataclass
class LiquidationReplayResult:
    all_levels: list[LiquidationLevel]
    active_levels: list[LiquidationLevel]
    candle_states: list[LiquidationCandleState]
    summary: dict[str, Any]
    config: LiquidationLevelConfig = field(repr=False)


def normalize_ohlcv_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize OHLCV columns and timestamps to UTC-aware chronological order."""
    if df is None or len(df) == 0:
        raise ValueError("OHLCV dataframe is empty")

    out = df.copy()
    colmap = {str(c).strip().lower(): c for c in out.columns}

    ts_col = None
    for candidate in ("timestamp", "datetime", "date", "time"):
        if candidate in colmap:
            ts_col = colmap[candidate]
            break
    if ts_col is None:
        raise ValueError(
            "OHLCV dataframe needs a timestamp column "
            "(timestamp/datetime/date/time)"
        )

    required = ("open", "high", "low", "close", "volume")
    missing = [name for name in required if name not in colmap]
    if missing:
        raise ValueError(f"OHLCV dataframe missing columns: {missing}")

    ts = pd.to_datetime(out[ts_col], utc=True, errors="coerce")
    if ts.isna().any():
        raise ValueError("timestamp column contains values that cannot be parsed as UTC datetime")

    normalized = pd.DataFrame(
        {
            "timestamp": ts,
            "open": pd.to_numeric(out[colmap["open"]], errors="coerce"),
            "high": pd.to_numeric(out[colmap["high"]], errors="coerce"),
            "low": pd.to_numeric(out[colmap["low"]], errors="coerce"),
            "close": pd.to_numeric(out[colmap["close"]], errors="coerce"),
            "volume": pd.to_numeric(out[colmap["volume"]], errors="coerce"),
        }
    )
    if normalized[["open", "high", "low", "close", "volume"]].isna().any().any():
        raise ValueError("OHLCV dataframe contains non-numeric OHLC/volume values")

    normalized = normalized.sort_values("timestamp", kind="mergesort").reset_index(drop=True)
    if normalized["timestamp"].duplicated().any():
        raise ValueError("OHLCV dataframe contains duplicate timestamps")
    return normalized


def compute_reference_price(df: pd.DataFrame, mode: str) -> pd.Series:
    """Compute per-candle reference price for the given mode."""
    mode_norm = str(mode).strip().lower()
    if mode_norm not in REFERENCE_PRICE_MODES:
        raise ValueError(f"unsupported reference_price mode: {mode!r}")

    o = df["open"].astype(float)
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    c = df["close"].astype(float)

    if mode_norm == "open":
        return o
    if mode_norm == "close":
        return c
    if mode_norm == "oc2":
        return (o + c) / 2.0
    if mode_norm == "hl2":
        return (h + l) / 2.0
    if mode_norm == "hlc3":
        return (h + l + c) / 3.0
    if mode_norm == "ohlc4":
        return (o + h + l + c) / 4.0
    # hlcc4
    return (h + l + c + c) / 4.0


def _safe_div(numer: float, denom: float) -> float | None:
    if denom == 0.0 or not np.isfinite(denom) or not np.isfinite(numer):
        return None
    return numer / denom


def compute_volume_flags(
    volume: float,
    vb_ma: float | None,
    volume_threshold: float,
) -> tuple[bool, bool, bool, float | None]:
    """Return (nzVd0, nzVd1, nzVd2, volume_ratio). Warm-up => all False / None."""
    if vb_ma is None or not np.isfinite(vb_ma):
        return False, False, False, None
    ratio = _safe_div(float(volume), float(vb_ma))
    nz0 = float(volume) > float(vb_ma) * float(volume_threshold)
    nz1 = float(volume) > float(vb_ma) * (1.0 + float(volume_threshold))
    nz2 = float(volume) > float(vb_ma) * (2.0 + float(volume_threshold))
    return nz0, nz1, nz2, ratio


def compute_volatility_trigger(
    *,
    reference: float,
    high: float,
    low: float,
    volatility_threshold: float,
) -> bool:
    """Pine lT with division-by-zero safety."""
    thr = float(volatility_threshold)
    ref = float(reference)
    hi = float(high)
    lo = float(low)

    low_ok = False
    if lo != ref:
        denom = ref - lo
        ratio = _safe_div(ref, denom)
        if ratio is not None and ratio <= thr:
            low_ok = True

    high_ok = False
    if hi != ref:
        denom = hi - ref
        ratio = _safe_div(ref, denom)
        if ratio is not None and ratio <= thr:
            high_ok = True

    return low_ok or high_ok


def compute_min_move(
    reference: float,
    high: float,
    low: float,
    *,
    minimum_move_divisor: float = MIN_MOVE_DIVISOR,
) -> bool:
    """Pine eC minimum move condition."""
    ref = float(reference)
    div = float(minimum_move_divisor)
    return (ref * (1.0 + 1.0 / div) < float(high)) or (ref * (1.0 - 1.0 / div) > float(low))


def level_prices(reference: float, leverage: int) -> tuple[float, float]:
    """Return (upper_level, lower_level) for one leverage."""
    ref = float(reference)
    lev = float(leverage)
    return ref * (1.0 + 1.0 / lev), ref * (1.0 - 1.0 / lev)


def strength_from_volume_flags(nz_vd0: bool, nz_vd1: bool, nz_vd2: bool) -> int:
    if nz_vd2:
        return 3
    if nz_vd1:
        return 2
    if nz_vd0:
        return 1
    return 1


def _aggregate_active(active: Sequence[LiquidationLevel]) -> tuple[int, int, int, int]:
    up = lo = su = sl = 0
    for lvl in active:
        if lvl.side == SIDE_UPPER:
            up += 1
            su += int(lvl.strength)
        else:
            lo += 1
            sl += int(lvl.strength)
    return up, lo, su, sl


def _nearest_levels(
    active: Sequence[LiquidationLevel], reference: float
) -> tuple[float | None, float | None, float | None, float | None]:
    ref = float(reference)
    nearest_up = None
    nearest_lo = None
    for lvl in active:
        if lvl.side == SIDE_UPPER:
            if nearest_up is None or lvl.level_price < nearest_up:
                nearest_up = float(lvl.level_price)
        else:
            if nearest_lo is None or lvl.level_price > nearest_lo:
                nearest_lo = float(lvl.level_price)

    up_dist = None if nearest_up is None or ref == 0.0 else (nearest_up - ref) / ref * 100.0
    lo_dist = None if nearest_lo is None or ref == 0.0 else (ref - nearest_lo) / ref * 100.0
    return nearest_up, nearest_lo, up_dist, lo_dist


def _is_swept(
    level_price: float,
    high: float,
    low: float,
    *,
    strict_cross: bool = True,
) -> bool:
    """Sweep test. Baseline strict_cross: high > level and low < level."""
    lp = float(level_price)
    hi = float(high)
    lo = float(low)
    if strict_cross:
        return hi > lp and lo < lp
    return hi >= lp and lo <= lp


def replay_liquidation_levels(
    df: pd.DataFrame,
    config: LiquidationLevelConfig | None = None,
    *,
    progress_every: int = 0,
    progress_callback: Any | None = None,
) -> LiquidationReplayResult:
    """Chronological causal replay of LuxAlgo-style liquidation levels."""
    cfg = config or LiquidationLevelConfig()
    data = normalize_ohlcv_dataframe(df)
    n = len(data)
    refs = compute_reference_price(data, cfg.reference_price).to_numpy(dtype=float)
    volumes = data["volume"].to_numpy(dtype=float)
    highs = data["high"].to_numpy(dtype=float)
    lows = data["low"].to_numpy(dtype=float)
    opens = data["open"].to_numpy(dtype=float)
    closes = data["close"].to_numpy(dtype=float)
    timestamps = pd.to_datetime(data["timestamp"], utc=True)

    # SMA(volume, period); NaN until warm-up complete (need `period` observations).
    vol_sma = (
        pd.Series(volumes, dtype=float)
        .rolling(window=int(cfg.volume_sma_period), min_periods=int(cfg.volume_sma_period))
        .mean()
        .to_numpy(dtype=float)
    )

    all_levels: list[LiquidationLevel] = []
    active: list[LiquidationLevel] = []
    candle_states: list[LiquidationCandleState] = []
    next_id = 1
    removed_by_limit = 0
    created_count = 0
    swept_count = 0

    import time as _time

    t0 = _time.perf_counter()

    for i in range(n):
        active_ids_before = tuple(lvl.level_id for lvl in active)
        up_b, lo_b, su_b, sl_b = _aggregate_active(active)
        near_up, near_lo, near_up_pct, near_lo_pct = _nearest_levels(active, float(refs[i]))

        vb = vol_sma[i]
        vb_ma = None if (isinstance(vb, float) and np.isnan(vb)) or not np.isfinite(vb) else float(vb)
        nz0, nz1, nz2, vol_ratio = compute_volume_flags(float(volumes[i]), vb_ma, cfg.volume_threshold)
        l_t = compute_volatility_trigger(
            reference=float(refs[i]),
            high=float(highs[i]),
            low=float(lows[i]),
            volatility_threshold=cfg.volatility_threshold,
        )
        e_c = compute_min_move(
            float(refs[i]),
            float(highs[i]),
            float(lows[i]),
            minimum_move_divisor=float(cfg.minimum_move_divisor),
        )
        strength = strength_from_volume_flags(nz0, nz1, nz2)
        create_signal = bool(l_t or nz0)

        created_ids: list[int] = []
        created_up = created_lo = 0
        created_su = created_sl = 0

        if create_signal and e_c:
            for lev in cfg.leverages:
                upper, lower = level_prices(float(refs[i]), int(lev))
                # Upper: must sit strictly above current high.
                if upper > float(highs[i]):
                    lvl = LiquidationLevel(
                        level_id=next_id,
                        side=SIDE_UPPER,
                        leverage=int(lev),
                        level_price=float(upper),
                        reference_price=float(refs[i]),
                        created_index=i,
                        created_timestamp=timestamps.iloc[i],
                        created_open=float(opens[i]),
                        created_high=float(highs[i]),
                        created_low=float(lows[i]),
                        created_close=float(closes[i]),
                        created_volume=float(volumes[i]),
                        volume_sma_13=vb_ma,
                        volume_ratio=vol_ratio,
                        strength=int(strength),
                        created_by_volume=bool(nz0),
                        created_by_volatility=bool(l_t),
                        status=STATUS_ACTIVE,
                    )
                    next_id += 1
                    all_levels.append(lvl)
                    active.append(lvl)
                    created_ids.append(lvl.level_id)
                    created_up += 1
                    created_su += int(strength)
                    created_count += 1
                # Lower: must sit strictly below current low.
                if lower < float(lows[i]):
                    lvl = LiquidationLevel(
                        level_id=next_id,
                        side=SIDE_LOWER,
                        leverage=int(lev),
                        level_price=float(lower),
                        reference_price=float(refs[i]),
                        created_index=i,
                        created_timestamp=timestamps.iloc[i],
                        created_open=float(opens[i]),
                        created_high=float(highs[i]),
                        created_low=float(lows[i]),
                        created_close=float(closes[i]),
                        created_volume=float(volumes[i]),
                        volume_sma_13=vb_ma,
                        volume_ratio=vol_ratio,
                        strength=int(strength),
                        created_by_volume=bool(nz0),
                        created_by_volatility=bool(l_t),
                        status=STATUS_ACTIVE,
                    )
                    next_id += 1
                    all_levels.append(lvl)
                    active.append(lvl)
                    created_ids.append(lvl.level_id)
                    created_lo += 1
                    created_sl += int(strength)
                    created_count += 1

        # Sweep check includes newly created levels (same-bar create cannot sweep by construction).
        swept_ids: list[int] = []
        swept_up = swept_lo = 0
        swept_su = swept_sl = 0
        still_active: list[LiquidationLevel] = []
        for lvl in active:
            if _is_swept(
                lvl.level_price,
                float(highs[i]),
                float(lows[i]),
                strict_cross=bool(cfg.sweep_strict_cross),
            ):
                lvl.status = STATUS_SWEPT
                lvl.swept_index = i
                lvl.swept_timestamp = timestamps.iloc[i]
                lvl.age_at_sweep = i - int(lvl.created_index)
                lvl.removal_reason = "swept"
                swept_ids.append(lvl.level_id)
                swept_count += 1
                if lvl.side == SIDE_UPPER:
                    swept_up += 1
                    swept_su += int(lvl.strength)
                else:
                    swept_lo += 1
                    swept_sl += int(lvl.strength)
            else:
                still_active.append(lvl)
        active = still_active

        # Cap active list: drop oldest first.
        while len(active) > int(cfg.max_active_levels):
            oldest = active.pop(0)
            oldest.status = STATUS_REMOVED
            oldest.removal_reason = "max_active_limit"
            # Clear any accidental sweep fields — removed by capacity, not swept.
            oldest.swept_index = None
            oldest.swept_timestamp = None
            oldest.age_at_sweep = None
            removed_by_limit += 1

        up_a, lo_a, su_a, sl_a = _aggregate_active(active)
        candle_states.append(
            LiquidationCandleState(
                candle_index=i,
                timestamp=timestamps.iloc[i],
                active_upper_before=up_b,
                active_lower_before=lo_b,
                active_strength_upper_before=su_b,
                active_strength_lower_before=sl_b,
                created_upper=created_up,
                created_lower=created_lo,
                created_strength_upper=created_su,
                created_strength_lower=created_sl,
                swept_upper=swept_up,
                swept_lower=swept_lo,
                swept_strength_upper=swept_su,
                swept_strength_lower=swept_sl,
                active_upper_after=up_a,
                active_lower_after=lo_a,
                active_strength_upper_after=su_a,
                active_strength_lower_after=sl_a,
                nearest_upper_price_before=near_up,
                nearest_lower_price_before=near_lo,
                nearest_upper_distance_pct_before=near_up_pct,
                nearest_lower_distance_pct_before=near_lo_pct,
                active_level_ids_before=active_ids_before,
                created_level_ids=tuple(created_ids),
                swept_level_ids=tuple(swept_ids),
                active_level_ids_after=tuple(lvl.level_id for lvl in active),
            )
        )

        if progress_every > 0 and ((i + 1) % progress_every == 0 or i + 1 == n):
            elapsed = _time.perf_counter() - t0
            msg = (
                f"candles={i + 1}/{n} active={len(active)} "
                f"created={created_count} swept={swept_count} "
                f"removed_limit={removed_by_limit} elapsed={elapsed:.2f}s"
            )
            if progress_callback is not None:
                progress_callback(msg)
            else:
                print(msg, flush=True)

    ages = [lvl.age_at_sweep for lvl in all_levels if lvl.age_at_sweep is not None]
    by_lev: dict[str, int] = {}
    by_str: dict[str, int] = {}
    for lvl in all_levels:
        by_lev[str(lvl.leverage)] = by_lev.get(str(lvl.leverage), 0) + 1
        by_str[str(lvl.strength)] = by_str.get(str(lvl.strength), 0) + 1

    upper_all = sum(1 for lvl in all_levels if lvl.side == SIDE_UPPER)
    lower_all = sum(1 for lvl in all_levels if lvl.side == SIDE_LOWER)
    swept_all = sum(1 for lvl in all_levels if lvl.status == STATUS_SWEPT)
    swept_up_all = sum(1 for lvl in all_levels if lvl.status == STATUS_SWEPT and lvl.side == SIDE_UPPER)
    swept_lo_all = sum(1 for lvl in all_levels if lvl.status == STATUS_SWEPT and lvl.side == SIDE_LOWER)

    summary = {
        "candle_count": n,
        "created_level_count": len(all_levels),
        "created_upper_count": upper_all,
        "created_lower_count": lower_all,
        "swept_level_count": swept_all,
        "swept_upper_count": swept_up_all,
        "swept_lower_count": swept_lo_all,
        "active_level_count_end": len(active),
        "removed_by_limit_count": removed_by_limit,
        "counts_by_leverage": by_lev,
        "counts_by_strength": by_str,
        "median_age_at_sweep": float(np.median(ages)) if ages else None,
        "mean_age_at_sweep": float(np.mean(ages)) if ages else None,
        "sweep_rate_percent": (
            None if not all_levels else 100.0 * swept_all / len(all_levels)
        ),
        "start_timestamp": None if n == 0 else str(timestamps.iloc[0]),
        "end_timestamp": None if n == 0 else str(timestamps.iloc[-1]),
        "elapsed_seconds": float(_time.perf_counter() - t0),
    }

    return LiquidationReplayResult(
        all_levels=all_levels,
        active_levels=list(active),
        candle_states=candle_states,
        summary=summary,
        config=cfg,
    )


def levels_to_dataframe(result: LiquidationReplayResult) -> pd.DataFrame:
    rows = []
    for lvl in result.all_levels:
        row = asdict(lvl)
        if row.get("created_timestamp") is not None:
            row["created_timestamp"] = str(row["created_timestamp"])
        if row.get("swept_timestamp") is not None:
            row["swept_timestamp"] = str(row["swept_timestamp"])
        rows.append(row)
    return pd.DataFrame(rows)


def candle_states_to_dataframe(result: LiquidationReplayResult) -> pd.DataFrame:
    rows = []
    for st in result.candle_states:
        row = asdict(st)
        row["timestamp"] = str(row["timestamp"])
        row["active_level_ids_before"] = ",".join(str(x) for x in st.active_level_ids_before)
        row["created_level_ids"] = ",".join(str(x) for x in st.created_level_ids)
        row["swept_level_ids"] = ",".join(str(x) for x in st.swept_level_ids)
        row["active_level_ids_after"] = ",".join(str(x) for x in st.active_level_ids_after)
        rows.append(row)
    return pd.DataFrame(rows)


def sweep_events_dataframe(result: LiquidationReplayResult) -> pd.DataFrame:
    """One row per swept level."""
    rows = []
    for lvl in result.all_levels:
        if lvl.status != STATUS_SWEPT:
            continue
        rows.append(
            {
                "level_id": lvl.level_id,
                "side": lvl.side,
                "leverage": lvl.leverage,
                "level_price": lvl.level_price,
                "strength": lvl.strength,
                "created_index": lvl.created_index,
                "created_timestamp": str(lvl.created_timestamp),
                "swept_index": lvl.swept_index,
                "swept_timestamp": str(lvl.swept_timestamp) if lvl.swept_timestamp is not None else None,
                "age_at_sweep": lvl.age_at_sweep,
                "created_by_volume": lvl.created_by_volume,
                "created_by_volatility": lvl.created_by_volatility,
            }
        )
    return pd.DataFrame(rows)
