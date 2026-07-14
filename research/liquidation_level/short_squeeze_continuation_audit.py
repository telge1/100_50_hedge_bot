"""Short-continuation audit after upper LuxAlgo-style liquidation level sweeps.

Estimated levels only — not real exchange liquidations.
Entry only after a known bearish reclaim of the swept upper level (or sweep-only
comparison groups). No scanner/bot integration.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

from research.liquidation_level.leverage_rebound_audit import (
    LEVERAGES,
    first_touch_bar,
    leverage_combo_label,
    deepest_leverage,
)
from research.liquidation_level.liquidation_backtest import (
    apply_cost,
    assign_sample,
    evaluate_tp_sl_trade,
    in_sample_cut,
    profit_factor,
    short_return_pct,
)
from research.liquidation_level.liquidation_features import candle_geometry
from research.liquidation_level.liquidation_levels import (
    SIDE_UPPER,
    STATUS_SWEPT,
    LiquidationLevel,
    LiquidationReplayResult,
    normalize_ohlcv_dataframe,
)

EPS = 1e-12
DEFAULT_HORIZONS = (1, 2, 3, 6, 12, 24, 48, 96)
DEFAULT_TARGETS = (0.10, 0.20, 0.25, 0.30, 0.50, 0.75, 1.00, 1.50, 2.00)
DEFAULT_TPS = (0.25, 0.50, 0.75, 1.00, 1.50, 2.00)
DEFAULT_SLS = (0.25, 0.50, 0.75, 1.00, 1.50, 2.00)
DEFAULT_MAX_HOLDS = (3, 6, 12, 24, 48, 96)
MARCH_START = "2026-03-05"
MARCH_END = "2026-03-10"
COST_PCT = 0.12


@dataclass(frozen=True)
class ShortSqueezeConfig:
    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    targets: tuple[float, ...] = DEFAULT_TARGETS
    take_profits_pct: tuple[float, ...] = DEFAULT_TPS
    stop_losses_pct: tuple[float, ...] = DEFAULT_SLS
    max_holds: tuple[int, ...] = DEFAULT_MAX_HOLDS
    bootstrap_resamples: int = 1000
    seed: int = 42
    in_sample_fraction: float = 0.70
    roundtrip_cost_pct: float = COST_PCT
    skip_tp_sl: bool = False
    skip_bootstrap: bool = False


def aggregate_closed_htf_local(
    candles_5m: pd.DataFrame,
    minutes: int,
    end_wall: pd.Timestamp,
) -> pd.DataFrame:
    """Causal closed HTF buckets only (complete contiguous 5m coverage)."""
    df = candles_5m.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.sort_values("timestamp")
    end_wall = pd.Timestamp(end_wall)
    if end_wall.tzinfo is None:
        end_wall = end_wall.tz_localize("UTC")
    df = df.loc[df["timestamp"] < end_wall].copy()
    if df.empty:
        return pd.DataFrame()

    bucket = df["timestamp"].dt.floor(f"{minutes}min")
    df = df.assign(bucket=bucket)
    rows = []
    delta = pd.Timedelta(minutes=minutes)
    need = minutes // 5
    for b_open, g in df.groupby("bucket", sort=True):
        b_open = pd.Timestamp(b_open)
        if b_open.tzinfo is None:
            b_open = b_open.tz_localize("UTC")
        b_close = b_open + delta
        if b_close > end_wall:
            continue
        if len(g) < need:
            continue
        expected = pd.date_range(b_open, periods=need, freq="5min", tz="UTC")
        have = set(pd.to_datetime(g["timestamp"], utc=True))
        if any(t not in have for t in expected):
            continue
        g2 = g.set_index("timestamp").reindex(expected)
        rows.append(
            {
                "timestamp": b_open,
                "decision_time": b_close,
                "open": float(g2["open"].iloc[0]),
                "high": float(g2["high"].max()),
                "low": float(g2["low"].min()),
                "close": float(g2["close"].iloc[-1]),
                "volume": float(g2["volume"].fillna(0).sum()),
            }
        )
    return pd.DataFrame(rows)


def ema_series(values: np.ndarray, period: int) -> np.ndarray:
    """Causal EMA; NaN until period observations."""
    out = np.full(len(values), np.nan, dtype=float)
    if len(values) < period or period < 1:
        return out
    alpha = 2.0 / (period + 1.0)
    seed = float(np.mean(values[:period]))
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = alpha * float(values[i]) + (1.0 - alpha) * prev
        out[i] = prev
    return out


def enrich_htf_indicators(htf: pd.DataFrame) -> pd.DataFrame:
    if htf.empty:
        return htf.copy()
    out = htf.copy().reset_index(drop=True)
    close = out["close"].to_numpy(float)
    high = out["high"].to_numpy(float)
    low = out["low"].to_numpy(float)
    out["ema9"] = ema_series(close, 9)
    out["ema20"] = ema_series(close, 20)
    slope = np.full(len(out), np.nan)
    for i in range(len(out)):
        if i >= 3 and np.isfinite(out["ema20"].iloc[i]) and np.isfinite(out["ema20"].iloc[i - 3]):
            slope[i] = float(out["ema20"].iloc[i] - out["ema20"].iloc[i - 3])
    out["ema20_slope"] = slope
    # prior bar structure low
    out["prev_low"] = out["low"].shift(1)
    out["prev_close"] = out["close"].shift(1)
    return out


def map_5m_to_latest_closed_htf(
    ts_5m: pd.Series,
    htf: pd.DataFrame,
) -> np.ndarray:
    """For each 5m bar open time, index of latest HTF with decision_time <= 5m close."""
    if htf.empty:
        return np.full(len(ts_5m), -1, dtype=int)
    close_5m = pd.to_datetime(ts_5m, utc=True) + pd.Timedelta(minutes=5)
    dec = pd.to_datetime(htf["decision_time"], utc=True).to_numpy()
    # searchsorted on decision times
    idx = np.searchsorted(dec, close_5m.to_numpy(), side="right") - 1
    return idx.astype(int)


def trend_t1_row(htf: pd.DataFrame, i: int) -> bool:
    if i < 0 or i >= len(htf):
        return False
    row = htf.iloc[i]
    if not (np.isfinite(row["ema9"]) and np.isfinite(row["ema20"]) and np.isfinite(row["ema20_slope"])):
        return False
    return bool(
        float(row["close"]) < float(row["ema20"])
        and float(row["ema9"]) < float(row["ema20"])
        and float(row["ema20_slope"]) < 0.0
    )


def trend_t2_row(htf: pd.DataFrame, i: int) -> bool:
    if not trend_t1_row(htf, i):
        return False
    # at least 2 of last 3 closed bars: lower close or lower low
    start = max(0, i - 2)
    flags = 0
    for j in range(start, i + 1):
        if j == 0:
            continue
        cur = htf.iloc[j]
        prev = htf.iloc[j - 1]
        if float(cur["close"]) < float(prev["close"]) or float(cur["low"]) < float(prev["low"]):
            flags += 1
    if flags < 2:
        return False
    # close under previous structure low (prior bar low)
    prev_low = htf.iloc[i]["prev_low"]
    if not np.isfinite(prev_low):
        return False
    return float(htf.iloc[i]["close"]) < float(prev_low)


def trend_t3(htf15: pd.DataFrame, i15: int, htf30: pd.DataFrame, i30: int) -> bool:
    if not trend_t1_row(htf15, i15):
        return False
    if i30 < 0 or i30 >= len(htf30):
        return False
    row = htf30.iloc[i30]
    if not (np.isfinite(row["ema9"]) and np.isfinite(row["ema20"]) and np.isfinite(row["ema20_slope"])):
        return False
    return bool(
        float(row["close"]) < float(row["ema20"])
        and float(row["ema9"]) < float(row["ema20"])
        and float(row["ema20_slope"]) < 0.0
    )


@dataclass
class ShortSqueezeEvent:
    event_id: str
    timestamp: pd.Timestamp
    candle_index: int
    leverage: int
    level_id: int
    level_price: float
    level_strength: int
    level_age: int
    event_open: float
    event_high: float
    event_low: float
    event_close: float
    event_volume: float
    high_above_level_pct: float
    close_relative_to_level_pct: float
    body_pct: float
    upper_wick_pct: float
    lower_wick_pct: float
    swept_level_count: int
    swept_total_strength: int
    leverage_combination: str
    reclaim_class: str
    exclusive_reclaim_group: str
    reclaim_index: int | None
    reclaim_delay_candles: int | None
    signal_index: int | None
    signal_timestamp: pd.Timestamp | None
    entry_index: int | None
    entry_timestamp: pd.Timestamp | None
    entry_price: float | None
    sample: str
    trend_t1: bool
    trend_t2: bool
    trend_t3: bool
    trend_15m_close: float | None
    trend_15m_ema9: float | None
    trend_15m_ema20: float | None
    trend_15m_ema20_slope: float | None
    trend_30m_close: float | None
    trend_30m_ema9: float | None
    trend_30m_ema20: float | None
    trend_30m_ema20_slope: float | None
    is_march_window: bool
    is_march_06: bool


def _find_bearish_reclaim(
    *,
    level_price: float,
    event_close: float,
    closes: np.ndarray,
    sweep_index: int,
    reclaim_window_candles: int = 3,
) -> tuple[str, str, int | None, int | None]:
    """Return (inclusive_class, exclusive_group, reclaim_index, delay_candles)."""
    lvl = float(level_price)
    window = max(1, int(reclaim_window_candles))
    if float(event_close) < lvl:
        return "immediate_reclaim", "immediate_reclaim", sweep_index, 0

    # look in next 1..window closed candles after sweep
    reclaim_i = None
    for j in range(1, window + 1):
        idx = sweep_index + j
        if idx >= len(closes):
            break
        if float(closes[idx]) < lvl:
            reclaim_i = idx
            break
    if reclaim_i is None:
        return "no_reclaim", "no_reclaim_within_3", None, None

    delay = int(reclaim_i - sweep_index)
    if delay == 1:
        inclusive = "next_candle_reclaim"
    else:
        inclusive = "reclaim_within_3"
    return inclusive, "delayed_reclaim_1_to_3", reclaim_i, delay


def build_upper_squeeze_events(
    result: LiquidationReplayResult,
    ohlcv: pd.DataFrame,
    htf15: pd.DataFrame,
    htf30: pd.DataFrame,
    config: ShortSqueezeConfig,
    *,
    allowed_leverages: Sequence[int] | None = None,
    reclaim_window_candles: int = 3,
) -> list[ShortSqueezeEvent]:
    data = normalize_ohlcv_dataframe(ohlcv)
    n = len(data)
    opens = data["open"].to_numpy(float)
    highs = data["high"].to_numpy(float)
    lows = data["low"].to_numpy(float)
    closes = data["close"].to_numpy(float)
    volumes = data["volume"].to_numpy(float)
    ts = pd.to_datetime(data["timestamp"], utc=True)
    lev_ok = set(int(x) for x in (allowed_leverages if allowed_leverages is not None else LEVERAGES))

    map15 = map_5m_to_latest_closed_htf(ts, htf15)
    map30 = map_5m_to_latest_closed_htf(ts, htf30)

    # group upper sweeps by candle for combo fields
    by_candle: dict[int, list[LiquidationLevel]] = {}
    for lvl in result.all_levels:
        if lvl.status != STATUS_SWEPT or lvl.swept_index is None:
            continue
        if lvl.side != SIDE_UPPER or int(lvl.leverage) not in lev_ok:
            continue
        by_candle.setdefault(int(lvl.swept_index), []).append(lvl)

    march_start = pd.Timestamp(MARCH_START, tz="UTC")
    march_end = pd.Timestamp(MARCH_END, tz="UTC")
    march_06 = pd.Timestamp("2026-03-06", tz="UTC")

    events: list[ShortSqueezeEvent] = []
    seq = 0
    for i, lvls in sorted(by_candle.items()):
        if i < 0 or i >= n:
            continue
        levers = {int(x.leverage) for x in lvls}
        combo = leverage_combo_label(levers)
        for lvl in lvls:
            inclusive, exclusive, reclaim_i, delay = _find_bearish_reclaim(
                level_price=float(lvl.level_price),
                event_close=float(closes[i]),
                closes=closes,
                sweep_index=i,
                reclaim_window_candles=int(reclaim_window_candles),
            )
            # entry semantics
            signal_index = None
            entry_index = None
            if exclusive == "immediate_reclaim":
                signal_index = i
                entry_index = i + 1
            elif exclusive == "delayed_reclaim_1_to_3" and reclaim_i is not None:
                signal_index = reclaim_i
                entry_index = reclaim_i + 1
            # sweep_only / no_reclaim also get a comparison entry at next open after sweep
            # (not a reclaim trade — used for sweep_only metrics)
            sweep_only_entry = i + 1

            if entry_index is not None and entry_index >= n:
                entry_index = None
                signal_index = None
            if sweep_only_entry >= n:
                sweep_only_entry_ok = None
            else:
                sweep_only_entry_ok = sweep_only_entry

            geo = candle_geometry(opens[i], highs[i], lows[i], closes[i])
            i15 = int(map15[i]) if i < len(map15) else -1
            i30 = int(map30[i]) if i < len(map30) else -1
            t1 = trend_t1_row(htf15, i15)
            t2 = trend_t2_row(htf15, i15)
            t3 = trend_t3(htf15, i15, htf30, i30)

            def _htf_val(df: pd.DataFrame, idx: int, col: str) -> float | None:
                if idx < 0 or idx >= len(df):
                    return None
                v = df.iloc[idx][col]
                return None if not np.isfinite(v) else float(v)

            tstamp = ts.iloc[i]
            seq += 1
            # primary entry fields use reclaim entry when available else None
            # For sweep_only analysis we store reclaim entry separately via flags;
            # also store sweep-next entry in entry_* when no reclaim trade.
            use_entry = entry_index
            use_signal = signal_index
            if use_entry is None and exclusive == "no_reclaim_within_3":
                # no reclaim trade entry
                pass

            events.append(
                ShortSqueezeEvent(
                    event_id=f"SS_{seq:06d}",
                    timestamp=tstamp,
                    candle_index=i,
                    leverage=int(lvl.leverage),
                    level_id=int(lvl.level_id),
                    level_price=float(lvl.level_price),
                    level_strength=int(lvl.strength),
                    level_age=int(lvl.age_at_sweep if lvl.age_at_sweep is not None else i - int(lvl.created_index)),
                    event_open=float(opens[i]),
                    event_high=float(highs[i]),
                    event_low=float(lows[i]),
                    event_close=float(closes[i]),
                    event_volume=float(volumes[i]),
                    high_above_level_pct=(
                        (float(highs[i]) - float(lvl.level_price)) / float(lvl.level_price) * 100.0
                        if lvl.level_price
                        else 0.0
                    ),
                    close_relative_to_level_pct=(
                        (float(lvl.level_price) - float(closes[i])) / float(lvl.level_price) * 100.0
                        if lvl.level_price
                        else 0.0
                    ),
                    body_pct=geo["sweep_body_pct"],
                    upper_wick_pct=geo["upper_wick_pct"],
                    lower_wick_pct=geo["lower_wick_pct"],
                    swept_level_count=len(lvls),
                    swept_total_strength=int(sum(int(x.strength) for x in lvls)),
                    leverage_combination=combo,
                    reclaim_class=inclusive if exclusive != "no_reclaim_within_3" else "no_reclaim",
                    exclusive_reclaim_group=exclusive,
                    reclaim_index=reclaim_i,
                    reclaim_delay_candles=delay,
                    signal_index=use_signal,
                    signal_timestamp=None if use_signal is None else ts.iloc[use_signal],
                    entry_index=use_entry,
                    entry_timestamp=None if use_entry is None else ts.iloc[use_entry],
                    entry_price=None if use_entry is None else float(opens[use_entry]),
                    sample=assign_sample(i, n, config.in_sample_fraction),
                    trend_t1=t1,
                    trend_t2=t2,
                    trend_t3=t3,
                    trend_15m_close=_htf_val(htf15, i15, "close"),
                    trend_15m_ema9=_htf_val(htf15, i15, "ema9"),
                    trend_15m_ema20=_htf_val(htf15, i15, "ema20"),
                    trend_15m_ema20_slope=_htf_val(htf15, i15, "ema20_slope"),
                    trend_30m_close=_htf_val(htf30, i30, "close"),
                    trend_30m_ema9=_htf_val(htf30, i30, "ema9"),
                    trend_30m_ema20=_htf_val(htf30, i30, "ema20"),
                    trend_30m_ema20_slope=_htf_val(htf30, i30, "ema20_slope"),
                    is_march_window=bool(march_start <= tstamp < march_end),
                    is_march_06=bool(tstamp.floor("D") == march_06),
                )
            )
            # attach sweep-only entry as extra attributes via side channel? Store in reclaim_class groups.
            # We'll compute sweep_only using candle_index+1 in metrics via a helper entry.
    # stash sweep-only next-open on each event for sweep_only groups
    for e in events:
        so = e.candle_index + 1
        if so < n:
            object.__setattr__(e, "_sweep_only_entry_index", so) if False else None
    # dataclasses aren't frozen; add fields dynamically
    for e in events:
        so = e.candle_index + 1
        e.__dict__["_sweep_only_entry_index"] = so if so < n else None
        e.__dict__["_sweep_only_entry_price"] = float(opens[so]) if so < n else None
    return events


def short_path_metrics(
    entry_price: float,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    horizon: int,
) -> dict[str, Any]:
    if entry_price <= 0 or horizon < 1 or len(highs) < horizon:
        return {}
    h = highs[:horizon]
    l = lows[:horizon]
    c = closes[:horizon]
    fav = (1.0 - l / entry_price) * 100.0
    adv = (h / entry_price - 1.0) * 100.0
    close_ret = short_return_pct(entry_price, float(c[-1]))
    return {
        "mfe_pct": float(np.max(fav)),
        "mae_pct": float(np.max(adv)),
        "fav_path": fav,
        "adv_path": adv,
        "close_return_pct": close_ret,
    }


def first_touch_conservative_short(
    highs: np.ndarray,
    lows: np.ndarray,
    entry: float,
    fav_thr: float,
    adv_thr: float,
) -> str:
    """Per-bar walk; if both hit same bar → adverse/SL first."""
    for i in range(len(highs)):
        hi = float(highs[i])
        lo = float(lows[i])
        hit_adv = (hi / entry - 1.0) * 100.0 >= adv_thr
        hit_fav = (1.0 - lo / entry) * 100.0 >= fav_thr
        if hit_adv and hit_fav:
            return "adverse_first"
        if hit_adv:
            return "adverse_first"
        if hit_fav:
            return "favorable_first"
    return "neither"


def _percentile(arr: Sequence[float], q: float) -> float | None:
    if not arr:
        return None
    return float(np.percentile(np.asarray(arr, float), q))


def summarize_short_group(
    *,
    group: str,
    sample: str,
    entries: list[tuple[int, float]],
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    config: ShortSqueezeConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (horizon_rows, threshold_rows, first_touch_rows)."""
    n = len(highs)
    horizon_rows: list[dict[str, Any]] = []
    threshold_rows: list[dict[str, Any]] = []
    first_touch_rows: list[dict[str, Any]] = []

    for horizon in config.horizons:
        mfes, maes, crets = [], [], []
        paths = []
        for ei, ep in entries:
            if ei + horizon - 1 >= n:
                continue
            m = short_path_metrics(
                ep,
                highs[ei : ei + horizon],
                lows[ei : ei + horizon],
                closes[ei : ei + horizon],
                horizon,
            )
            if not m:
                continue
            mfes.append(m["mfe_pct"])
            maes.append(m["mae_pct"])
            crets.append(m["close_return_pct"])
            paths.append(m)
        cnt = len(mfes)
        base = {
            "group": group,
            "sample": sample,
            "horizon": int(horizon),
            "n": cnt,
            "median_mfe_pct": float(np.median(mfes)) if mfes else None,
            "mean_mfe_pct": float(np.mean(mfes)) if mfes else None,
            "p25_mfe_pct": _percentile(mfes, 25),
            "p50_mfe_pct": _percentile(mfes, 50),
            "p75_mfe_pct": _percentile(mfes, 75),
            "p90_mfe_pct": _percentile(mfes, 90),
            "median_mae_pct": float(np.median(maes)) if maes else None,
            "mean_mae_pct": float(np.mean(maes)) if maes else None,
            "p75_mae_pct": _percentile(maes, 75),
            "p90_mae_pct": _percentile(maes, 90),
            "median_close_return_pct": float(np.median(crets)) if crets else None,
            "mean_close_return_pct": float(np.mean(crets)) if crets else None,
            "share_mfe_gt_mae_pct": (
                None if not mfes else 100.0 * sum(1 for a, b in zip(mfes, maes) if a > b) / cnt
            ),
        }
        horizon_rows.append(base)

        for thr in config.targets:
            hits = 0
            before_eq = 0
            times = []
            for m in paths:
                t = first_touch_bar(m["fav_path"], float(thr))
                if t is not None:
                    hits += 1
                    times.append(float(t))
                t_fav = first_touch_bar(m["fav_path"], float(thr))
                t_adv = first_touch_bar(m["adv_path"], float(thr))
                if t_fav is not None and (t_adv is None or t_fav <= t_adv):
                    # conservative same-bar handled in path construction? first_touch alone
                    # underestimates SL-first; use bar walk for equality case below in first_touch table
                    before_eq += 1
            threshold_rows.append(
                {
                    "group": group,
                    "sample": sample,
                    "horizon": int(horizon),
                    "target_pct": float(thr),
                    "n": cnt,
                    "hit_count": hits,
                    "hit_rate_pct": None if cnt == 0 else 100.0 * hits / cnt,
                    "median_bars_to_target": float(np.median(times)) if times else None,
                    "target_before_equal_adverse_pct": None if cnt == 0 else 100.0 * before_eq / cnt,
                    "mean_mfe_pct": base["mean_mfe_pct"],
                    "mean_mae_pct": base["mean_mae_pct"],
                }
            )

        # first-touch outcomes at key thresholds using max horizon path
        if horizon in (12, 24, 96) and paths:
            for fav_thr, adv_thr in ((0.25, 0.25), (0.50, 0.50), (0.25, 0.50), (0.50, 0.25)):
                fav_first = adv_first = neither = 0
                pre_adv_before_fav = []
                for ei, ep in entries:
                    if ei + horizon - 1 >= n:
                        continue
                    outcome = first_touch_conservative_short(
                        highs[ei : ei + horizon],
                        lows[ei : ei + horizon],
                        float(ep),
                        float(fav_thr),
                        float(adv_thr),
                    )
                    if outcome == "favorable_first":
                        fav_first += 1
                    elif outcome == "adverse_first":
                        adv_first += 1
                    else:
                        neither += 1
                    # max adverse before first fav touch
                    m = short_path_metrics(
                        float(ep),
                        highs[ei : ei + horizon],
                        lows[ei : ei + horizon],
                        closes[ei : ei + horizon],
                        horizon,
                    )
                    if m:
                        t_fav = first_touch_bar(m["fav_path"], float(fav_thr))
                        if t_fav is None:
                            pre_adv_before_fav.append(float(np.max(m["adv_path"])))
                        else:
                            pre_adv_before_fav.append(float(np.max(m["adv_path"][:t_fav])))
                tot = fav_first + adv_first + neither
                first_touch_rows.append(
                    {
                        "group": group,
                        "sample": sample,
                        "horizon": int(horizon),
                        "favorable_thr_pct": float(fav_thr),
                        "adverse_thr_pct": float(adv_thr),
                        "n": tot,
                        "favorable_first_pct": None if tot == 0 else 100.0 * fav_first / tot,
                        "adverse_first_pct": None if tot == 0 else 100.0 * adv_first / tot,
                        "neither_pct": None if tot == 0 else 100.0 * neither / tot,
                        "median_adverse_before_favorable_pct": (
                            float(np.median(pre_adv_before_fav)) if pre_adv_before_fav else None
                        ),
                        "mean_adverse_before_favorable_pct": (
                            float(np.mean(pre_adv_before_fav)) if pre_adv_before_fav else None
                        ),
                    }
                )
    return horizon_rows, threshold_rows, first_touch_rows


def _filter_sample(events: Sequence[ShortSqueezeEvent], sample: str) -> list[ShortSqueezeEvent]:
    if sample == "full":
        return list(events)
    return [e for e in events if e.sample == sample]


def _entries_for_group(events: Sequence[ShortSqueezeEvent], mode: str) -> list[tuple[int, float]]:
    """mode: reclaim | sweep_only"""
    out = []
    for e in events:
        if mode == "reclaim":
            if e.entry_index is None or e.entry_price is None:
                continue
            out.append((int(e.entry_index), float(e.entry_price)))
        else:
            ei = e.__dict__.get("_sweep_only_entry_index")
            ep = e.__dict__.get("_sweep_only_entry_price")
            if ei is None or ep is None:
                continue
            out.append((int(ei), float(ep)))
    return out


def select_group_events(events: Sequence[ShortSqueezeEvent], group: str) -> list[ShortSqueezeEvent]:
    """Parse group name into event subset."""
    evs = list(events)

    def lev_filter(xs, lev: int | None):
        return xs if lev is None else [e for e in xs if e.leverage == lev]

    # combo groups
    if group.startswith("combo_"):
        name = group[len("combo_") :]
        return [e for e in evs if e.leverage_combination == name]

    # patterns like upper_50x_immediate_reclaim_T1
    parts = group.split("__")
    base = parts[0]
    tags = parts[1:] if len(parts) > 1 else []

    lev = None
    if base.startswith("upper_100x"):
        lev = 100
    elif base.startswith("upper_50x"):
        lev = 50
    elif base.startswith("upper_25x"):
        lev = 25
    xs = lev_filter(evs, lev)

    reclaim_tag = None
    trend_tag = None
    for t in tags:
        if t in {
            "sweep_only",
            "immediate_reclaim",
            "next_candle_reclaim",
            "reclaim_within_3",
            "no_reclaim",
            "delayed_reclaim_1_to_3",
            "no_reclaim_within_3",
            "any_reclaim",
        }:
            reclaim_tag = t
        if t in {"T1", "T2", "T3"}:
            trend_tag = t

    if reclaim_tag == "sweep_only" or (not reclaim_tag and "sweep_only" in base):
        pass  # all
    elif reclaim_tag == "immediate_reclaim":
        xs = [e for e in xs if e.exclusive_reclaim_group == "immediate_reclaim"]
    elif reclaim_tag == "delayed_reclaim_1_to_3":
        xs = [e for e in xs if e.exclusive_reclaim_group == "delayed_reclaim_1_to_3"]
    elif reclaim_tag == "next_candle_reclaim":
        xs = [e for e in xs if e.reclaim_class == "next_candle_reclaim"]
    elif reclaim_tag == "reclaim_within_3":
        xs = [
            e
            for e in xs
            if e.exclusive_reclaim_group in {"immediate_reclaim", "delayed_reclaim_1_to_3"}
        ]
    elif reclaim_tag == "any_reclaim":
        xs = [
            e
            for e in xs
            if e.exclusive_reclaim_group in {"immediate_reclaim", "delayed_reclaim_1_to_3"}
        ]
    elif reclaim_tag in {"no_reclaim", "no_reclaim_within_3"}:
        xs = [e for e in xs if e.exclusive_reclaim_group == "no_reclaim_within_3"]

    if trend_tag == "T1":
        xs = [e for e in xs if e.trend_t1]
    elif trend_tag == "T2":
        xs = [e for e in xs if e.trend_t2]
    elif trend_tag == "T3":
        xs = [e for e in xs if e.trend_t3]
    return xs


GROUP_SPECS = [
    "upper_100x__sweep_only",
    "upper_50x__sweep_only",
    "upper_25x__sweep_only",
    "upper_100x__no_reclaim_within_3",
    "upper_50x__no_reclaim_within_3",
    "upper_25x__no_reclaim_within_3",
    "upper_100x__immediate_reclaim",
    "upper_50x__immediate_reclaim",
    "upper_25x__immediate_reclaim",
    "upper_100x__reclaim_within_3",
    "upper_50x__reclaim_within_3",
    "upper_25x__reclaim_within_3",
    "upper_100x__sweep_only__T1",
    "upper_50x__sweep_only__T1",
    "upper_25x__sweep_only__T1",
    "upper_100x__reclaim_within_3__T1",
    "upper_50x__reclaim_within_3__T1",
    "upper_25x__reclaim_within_3__T1",
    "upper_100x__sweep_only__T2",
    "upper_50x__sweep_only__T2",
    "upper_25x__sweep_only__T2",
    "upper_100x__reclaim_within_3__T2",
    "upper_50x__reclaim_within_3__T2",
    "upper_25x__reclaim_within_3__T2",
    "upper_100x__sweep_only__T3",
    "upper_50x__sweep_only__T3",
    "upper_25x__sweep_only__T3",
    "upper_100x__reclaim_within_3__T3",
    "upper_50x__reclaim_within_3__T3",
    "upper_25x__reclaim_within_3__T3",
    "upper_50x__immediate_reclaim__T1",
    "upper_25x__immediate_reclaim__T1",
    "combo_100x_only",
    "combo_50x_only",
    "combo_25x_only",
    "combo_100x_50x",
    "combo_50x_25x",
    "combo_100x_50x_25x",
    "combo_50x_25x__reclaim_within_3__T2",
    "combo_50x_25x__reclaim_within_3__T3",
    "combo_100x_50x_25x__reclaim_within_3__T2",
    "combo_100x_50x_25x__reclaim_within_3__T3",
]

TP_SL_GROUPS = [
    "upper_50x__immediate_reclaim__T1",
    "upper_50x__reclaim_within_3__T2",
    "upper_50x__reclaim_within_3__T3",
    "upper_25x__immediate_reclaim__T1",
    "upper_25x__reclaim_within_3__T2",
    "upper_25x__reclaim_within_3__T3",
    "combo_50x_25x__reclaim_within_3__T2",
    "combo_50x_25x__reclaim_within_3__T3",
]


def _group_entry_mode(group: str) -> str:
    if "__no_reclaim" in group or group.endswith("__sweep_only") or "__sweep_only__" in group:
        if "reclaim" in group and "no_reclaim" not in group and "sweep_only" not in group:
            return "reclaim"
        if "sweep_only" in group or "no_reclaim" in group:
            return "sweep_only"
    if "reclaim" in group:
        return "reclaim"
    if group.startswith("combo_"):
        # combo defaults to reclaim if tagged else sweep_only next open for all combo events
        if "reclaim" in group:
            return "reclaim"
        return "sweep_only"
    return "sweep_only"


def select_group_events_combo_aware(events: Sequence[ShortSqueezeEvent], group: str) -> list[ShortSqueezeEvent]:
    if not group.startswith("combo_"):
        return select_group_events(events, group)
    # combo_50x_25x__reclaim_within_3__T2
    rest = group[len("combo_") :]
    bits = rest.split("__")
    combo_name = bits[0]
    tags = bits[1:]
    xs = [e for e in events if e.leverage_combination == combo_name]
    # one event per candle for combo (dedupe by candle_index keeping first)
    seen = set()
    dedup = []
    for e in xs:
        if e.candle_index in seen:
            continue
        seen.add(e.candle_index)
        dedup.append(e)
    xs = dedup
    for t in tags:
        if t == "reclaim_within_3" or t == "any_reclaim":
            xs = [
                e
                for e in xs
                if e.exclusive_reclaim_group in {"immediate_reclaim", "delayed_reclaim_1_to_3"}
            ]
        elif t == "immediate_reclaim":
            xs = [e for e in xs if e.exclusive_reclaim_group == "immediate_reclaim"]
        elif t == "T1":
            xs = [e for e in xs if e.trend_t1]
        elif t == "T2":
            xs = [e for e in xs if e.trend_t2]
        elif t == "T3":
            xs = [e for e in xs if e.trend_t3]
    return xs


def run_tp_sl_for_group(
    events: Sequence[ShortSqueezeEvent],
    *,
    group: str,
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    config: ShortSqueezeConfig,
    overlapping: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    mode = _group_entry_mode(group)
    xs = select_group_events_combo_aware(events, group)
    # sort by entry
    trades_meta = []
    for e in xs:
        if mode == "reclaim":
            if e.entry_index is None or e.entry_price is None:
                continue
            trades_meta.append((int(e.entry_index), e))
        else:
            ei = e.__dict__.get("_sweep_only_entry_index")
            if ei is None:
                continue
            trades_meta.append((int(ei), e))
    trades_meta.sort(key=lambda x: x[0])

    trade_rows: list[dict[str, Any]] = []
    summary_acc: dict[tuple, list[dict[str, Any]]] = {}

    for tp in config.take_profits_pct:
        for sl in config.stop_losses_pct:
            for mh in config.max_holds:
                active_until = -1
                for ei, e in trades_meta:
                    if not overlapping and ei <= active_until:
                        continue
                    ev = evaluate_tp_sl_trade(
                        opens=opens,
                        highs=highs,
                        lows=lows,
                        closes=closes,
                        entry_index=ei,
                        direction="short",
                        tp_pct=float(tp),
                        sl_pct=float(sl),
                        max_hold=int(mh),
                        roundtrip_cost_pct=config.roundtrip_cost_pct,
                    )
                    if ev is None:
                        continue
                    exit_i = ei + int(ev["bars_held"]) - 1
                    if not overlapping:
                        active_until = exit_i
                    row = {
                        "group": group,
                        "mode": "overlapping" if overlapping else "first_signal_only",
                        "sample": e.sample,
                        "event_id": e.event_id,
                        "entry_index": ei,
                        "tp_pct": float(tp),
                        "sl_pct": float(sl),
                        "max_hold": int(mh),
                        **ev,
                    }
                    trade_rows.append(row)
                    for sample in ("full", e.sample):
                        key = (group, sample, float(tp), float(sl), int(mh), "overlapping" if overlapping else "first_signal_only")
                        summary_acc.setdefault(key, []).append(row)

    summaries = []
    for key, rows in sorted(summary_acc.items()):
        group, sample, tp, sl, mh, mode = key
        # only rows matching sample for non-full
        if sample != "full":
            use = [r for r in rows if r["sample"] == sample]
        else:
            use = rows
        if not use:
            continue
        gross = [float(r["gross_return_pct"]) for r in use]
        net = [float(r["net_return_pct"]) for r in use]
        wins = sum(1 for r in use if r["exit_reason"] == "tp")
        losses = sum(1 for r in use if r["exit_reason"] == "sl")
        timeouts = sum(1 for r in use if r["exit_reason"] in {"timeout", "end_of_data"})
        # serial drawdown on net returns in time order
        ordered = sorted(use, key=lambda r: r["entry_index"])
        cum = peak = 0.0
        max_dd = 0.0
        for r in ordered:
            cum += float(r["net_return_pct"])
            peak = max(peak, cum)
            max_dd = min(max_dd, cum - peak)
        summaries.append(
            {
                "group": group,
                "sample": sample,
                "mode": mode,
                "tp_pct": tp,
                "sl_pct": sl,
                "max_hold": mh,
                "trades": len(use),
                "wins": wins,
                "losses": losses,
                "timeouts": timeouts,
                "winrate_pct": 100.0 * wins / len(use),
                "mean_gross_return_pct": float(np.mean(gross)),
                "median_gross_return_pct": float(np.median(gross)),
                "sum_gross_return_pct": float(np.sum(gross)),
                "mean_net_return_pct": float(np.mean(net)),
                "median_net_return_pct": float(np.median(net)),
                "sum_net_return_pct": float(np.sum(net)),
                "profit_factor_gross": profit_factor(gross),
                "profit_factor_net": profit_factor(net),
                "max_drawdown_serial_net_pct": float(max_dd),
            }
        )
    return trade_rows, summaries


def match_controls(
    events: Sequence[ShortSqueezeEvent],
    ohlcv: pd.DataFrame,
    sweep_indices: set[int],
    *,
    seed: int,
) -> list[dict[str, Any]]:
    data = ohlcv if "timestamp" in ohlcv.columns else normalize_ohlcv_dataframe(ohlcv)
    ts = pd.to_datetime(data["timestamp"], utc=True)
    highs = data["high"].to_numpy(float)
    lows = data["low"].to_numpy(float)
    n = len(data)
    pool_rows = []
    for i in range(n - 1):
        if i in sweep_indices:
            continue
        pool_rows.append(
            {
                "candle_index": i,
                "month": str(ts.iloc[i].strftime("%Y-%m")),
                "hour": int(ts.iloc[i].hour),
                "range": float(highs[i] - lows[i]),
                "entry_index": i + 1,
                "entry_price": float(data.iloc[i + 1]["open"]),
                "trend_t1": False,  # filled below if needed — match on event trend flags instead via filter
            }
        )
    pool = pd.DataFrame(pool_rows)
    if pool.empty:
        return []
    rng = np.random.default_rng(seed)
    out = []
    for e in events:
        if e.exclusive_reclaim_group == "no_reclaim_within_3":
            continue  # controls for reclaim candidates primarily
        month = str(pd.Timestamp(e.timestamp).strftime("%Y-%m"))
        hour = int(pd.Timestamp(e.timestamp).hour)
        cand = pool[(pool["month"] == month) & (pool["hour"] == hour)]
        if cand.empty:
            cand = pool[pool["month"] == month]
        if cand.empty:
            continue
        ev_range = float(e.event_high - e.event_low)
        diffs = (cand["range"] - ev_range).abs().to_numpy()
        order = np.argsort(diffs)
        top = cand.iloc[order[: min(10, len(order))]]
        pick = top.iloc[int(rng.integers(0, len(top)))]
        out.append(
            {
                "source_event_id": e.event_id,
                "leverage": e.leverage,
                "sample": e.sample,
                "trend_t1": e.trend_t1,
                "trend_t2": e.trend_t2,
                "trend_t3": e.trend_t3,
                "control_candle_index": int(pick["candle_index"]),
                "entry_index": int(pick["entry_index"]),
                "entry_price": float(pick["entry_price"]),
            }
        )
    return out


def bootstrap_diff(a: Sequence[float], b: Sequence[float], resamples: int, seed: int) -> dict[str, float | None]:
    if not a or not b:
        return {"diff_mean": None, "ci_low": None, "ci_high": None}
    rng = np.random.default_rng(seed)
    aa = np.asarray(a, float)
    bb = np.asarray(b, float)
    diffs = np.empty(resamples, float)
    for i in range(resamples):
        diffs[i] = float(np.mean(rng.choice(aa, len(aa), replace=True)) - np.mean(rng.choice(bb, len(bb), replace=True)))
    return {
        "diff_mean": float(np.mean(aa) - np.mean(bb)),
        "ci_low": float(np.percentile(diffs, 2.5)),
        "ci_high": float(np.percentile(diffs, 97.5)),
    }


@dataclass
class ShortSqueezeBundle:
    config: ShortSqueezeConfig
    events: list[ShortSqueezeEvent]
    horizon_summary: list[dict[str, Any]]
    threshold_summary: list[dict[str, Any]]
    first_touch_summary: list[dict[str, Any]]
    tp_sl_trades: list[dict[str, Any]]
    tp_sl_summary: list[dict[str, Any]]
    matched_controls: list[dict[str, Any]]
    control_comparison: list[dict[str, Any]]
    variant_comparison: list[dict[str, Any]]
    monthly_summary: list[dict[str, Any]]
    march_events: list[dict[str, Any]]
    march_summary: dict[str, Any]
    summary_full: dict[str, Any]
    summary_in_sample: dict[str, Any]
    summary_out_of_sample: dict[str, Any]
    meta: dict[str, Any] = field(default_factory=dict)
    htf15: pd.DataFrame | None = None
    htf30: pd.DataFrame | None = None


def run_short_squeeze_continuation_audit(
    result: LiquidationReplayResult,
    ohlcv: pd.DataFrame,
    config: ShortSqueezeConfig | None = None,
) -> ShortSqueezeBundle:
    cfg = config or ShortSqueezeConfig()
    data = normalize_ohlcv_dataframe(ohlcv)
    n = len(data)
    end_wall = pd.to_datetime(data["timestamp"].iloc[-1], utc=True) + pd.Timedelta(minutes=5)

    print("aggregating closed 15m/30m...", flush=True)
    htf15 = enrich_htf_indicators(aggregate_closed_htf_local(data, 15, end_wall))
    htf30 = enrich_htf_indicators(aggregate_closed_htf_local(data, 30, end_wall))
    print(f"htf15={len(htf15)} htf30={len(htf30)}", flush=True)

    print("building upper squeeze events...", flush=True)
    events = build_upper_squeeze_events(result, data, htf15, htf30, cfg)
    print(f"events={len(events)}", flush=True)

    opens = data["open"].to_numpy(float)
    highs = data["high"].to_numpy(float)
    lows = data["low"].to_numpy(float)
    closes = data["close"].to_numpy(float)

    horizon_summary: list[dict[str, Any]] = []
    threshold_summary: list[dict[str, Any]] = []
    first_touch_summary: list[dict[str, Any]] = []

    for sample in ("full", "in_sample", "out_of_sample"):
        base = _filter_sample(events, sample)
        for group in GROUP_SPECS:
            xs = select_group_events_combo_aware(base, group)
            mode = _group_entry_mode(group)
            entries = _entries_for_group(xs, mode)
            h_rows, t_rows, f_rows = summarize_short_group(
                group=group,
                sample=sample,
                entries=entries,
                opens=opens,
                highs=highs,
                lows=lows,
                closes=closes,
                config=cfg,
            )
            horizon_summary.extend(h_rows)
            threshold_summary.extend(t_rows)
            first_touch_summary.extend(f_rows)

    # TP/SL
    tp_trades: list[dict[str, Any]] = []
    tp_summary: list[dict[str, Any]] = []
    if not cfg.skip_tp_sl:
        print("TP/SL matrix for key groups...", flush=True)
        for group in TP_SL_GROUPS:
            for overlapping in (True, False):
                tr, sm = run_tp_sl_for_group(
                    events,
                    group=group,
                    opens=opens,
                    highs=highs,
                    lows=lows,
                    closes=closes,
                    config=cfg,
                    overlapping=overlapping,
                )
                tp_trades.extend(tr)
                tp_summary.extend(sm)
        print(f"tp_sl_trades={len(tp_trades)}", flush=True)

    # controls
    print("matched controls...", flush=True)
    sweep_idx = {e.candle_index for e in events}
    # focus controls on reclaim+trend candidates
    ctrl_src = [
        e
        for e in events
        if e.exclusive_reclaim_group in {"immediate_reclaim", "delayed_reclaim_1_to_3"} and e.trend_t1
    ]
    matched = match_controls(ctrl_src, data, sweep_idx, seed=cfg.seed)
    control_comparison = []
    for sample in ("full", "in_sample", "out_of_sample"):
        for lev in (50, 25):
            evs = [
                e
                for e in _filter_sample(ctrl_src, sample)
                if e.leverage == lev and e.entry_index is not None
            ]
            ctr = [
                c
                for c in matched
                if c["leverage"] == lev and (sample == "full" or c["sample"] == sample)
            ]

            def stats(entries: list[tuple[int, float]]) -> dict[str, Any]:
                mfes, hits25, hits50, hits100, crets = [], 0, 0, 0, []
                before = 0
                usable = 0
                for ei, ep in entries:
                    if ei + 11 >= n:
                        continue
                    m = short_path_metrics(ep, highs[ei : ei + 12], lows[ei : ei + 12], closes[ei : ei + 12], 12)
                    if not m:
                        continue
                    usable += 1
                    mfes.append(m["mfe_pct"])
                    crets.append(m["close_return_pct"])
                    if first_touch_bar(m["fav_path"], 0.25):
                        hits25 += 1
                    if first_touch_bar(m["fav_path"], 0.50):
                        hits50 += 1
                    if first_touch_bar(m["fav_path"], 1.00):
                        hits100 += 1
                    ft = first_touch_conservative_short(
                        highs[ei : ei + 12], lows[ei : ei + 12], ep, 0.25, 0.25
                    )
                    if ft == "favorable_first":
                        before += 1
                if usable == 0:
                    return {"n": 0, "mfes": [], "crets": []}
                return {
                    "n": usable,
                    "mean_mfe": float(np.mean(mfes)),
                    "mean_close": float(np.mean(crets)),
                    "hit25": 100.0 * hits25 / usable,
                    "hit50": 100.0 * hits50 / usable,
                    "hit100": 100.0 * hits100 / usable,
                    "fav_before_adv_0_25": 100.0 * before / usable,
                    "mfes": mfes,
                    "crets": crets,
                }

            est = stats([(int(e.entry_index), float(e.entry_price)) for e in evs])
            cst = stats([(int(c["entry_index"]), float(c["entry_price"])) for c in ctr])
            if cfg.skip_bootstrap:
                ci = {"diff_mean": None, "ci_low": None, "ci_high": None}
            else:
                ci = bootstrap_diff(
                    est.get("mfes", []),
                    cst.get("mfes", []),
                    cfg.bootstrap_resamples,
                    cfg.seed + lev,
                )
            control_comparison.append(
                {
                    "group": f"upper_{lev}x_reclaim_T1",
                    "sample": sample,
                    "event_n": est.get("n", 0),
                    "control_n": cst.get("n", 0),
                    "event_mean_mfe_h12": est.get("mean_mfe"),
                    "control_mean_mfe_h12": cst.get("mean_mfe"),
                    "event_minus_control_mfe": (
                        None
                        if est.get("mean_mfe") is None or cst.get("mean_mfe") is None
                        else est["mean_mfe"] - cst["mean_mfe"]
                    ),
                    "event_hit_0_25": est.get("hit25"),
                    "control_hit_0_25": cst.get("hit25"),
                    "event_hit_0_50": est.get("hit50"),
                    "control_hit_0_50": cst.get("hit50"),
                    "event_hit_1_00": est.get("hit100"),
                    "control_hit_1_00": cst.get("hit100"),
                    "event_mean_close_return_h12": est.get("mean_close"),
                    "control_mean_close_return_h12": cst.get("mean_close"),
                    "event_fav_before_adv_0_25": est.get("fav_before_adv_0_25"),
                    "bootstrap_mfe_diff_mean": ci["diff_mean"],
                    "bootstrap_mfe_diff_ci95_low": ci["ci_low"],
                    "bootstrap_mfe_diff_ci95_high": ci["ci_high"],
                    "note": "empirical only; not a formal significance claim",
                }
            )

    # variant comparison at h=12 thr=0.50
    variant_comparison = []
    for sample in ("full", "in_sample", "out_of_sample"):
        for group in [
            "upper_50x__sweep_only",
            "upper_50x__no_reclaim_within_3",
            "upper_50x__immediate_reclaim",
            "upper_50x__reclaim_within_3",
            "upper_50x__reclaim_within_3__T1",
            "upper_50x__reclaim_within_3__T2",
            "upper_50x__reclaim_within_3__T3",
            "upper_25x__sweep_only",
            "upper_25x__reclaim_within_3",
            "upper_25x__reclaim_within_3__T2",
            "upper_25x__reclaim_within_3__T3",
            "combo_50x_25x__reclaim_within_3__T2",
        ]:
            row = next(
                (
                    r
                    for r in threshold_summary
                    if r["group"] == group
                    and r["sample"] == sample
                    and r["horizon"] == 12
                    and abs(float(r["target_pct"]) - 0.50) < 1e-12
                ),
                None,
            )
            hrow = next(
                (
                    r
                    for r in horizon_summary
                    if r["group"] == group and r["sample"] == sample and r["horizon"] == 12
                ),
                None,
            )
            if not row:
                continue
            variant_comparison.append(
                {
                    "group": group,
                    "sample": sample,
                    "n": row["n"],
                    "hit_0_50_h12": row["hit_rate_pct"],
                    "mean_mfe_h12": None if not hrow else hrow["mean_mfe_pct"],
                    "mean_mae_h12": None if not hrow else hrow["mean_mae_pct"],
                    "mean_close_return_h12": None if not hrow else hrow["mean_close_return_pct"],
                }
            )

    # monthly
    monthly = []
    ts = pd.to_datetime(data["timestamp"], utc=True)
    for e in events:
        if e.entry_index is None or e.entry_index + 11 >= n:
            continue
        m = short_path_metrics(
            float(e.entry_price),
            highs[e.entry_index : e.entry_index + 12],
            lows[e.entry_index : e.entry_index + 12],
            closes[e.entry_index : e.entry_index + 12],
            12,
        )
        if not m:
            continue
        monthly.append(
            {
                "month": str(pd.Timestamp(e.timestamp).strftime("%Y-%m")),
                "leverage": e.leverage,
                "reclaim": e.exclusive_reclaim_group,
                "trend_t2": e.trend_t2,
                "mfe_pct": m["mfe_pct"],
                "mae_pct": m["mae_pct"],
                "hit_0_50": first_touch_bar(m["fav_path"], 0.50) is not None,
            }
        )
    monthly_summary = []
    if monthly:
        mdf = pd.DataFrame(monthly)
        for (month, lev), g in mdf.groupby(["month", "leverage"]):
            monthly_summary.append(
                {
                    "month": month,
                    "leverage": int(lev),
                    "n": int(len(g)),
                    "mean_mfe_pct": float(g["mfe_pct"].mean()),
                    "mean_mae_pct": float(g["mae_pct"].mean()),
                    "hit_0_50_pct": 100.0 * float(g["hit_0_50"].mean()),
                }
            )

    # March report
    march_events = []
    for e in events:
        if not e.is_march_window:
            continue
        if e.leverage not in (50, 25):
            continue
        row = asdict(e)
        for h in (3, 6, 12, 24):
            if e.entry_index is None or e.entry_index + h - 1 >= n:
                row[f"mfe_h{h}"] = None
                row[f"mae_h{h}"] = None
                continue
            m = short_path_metrics(
                float(e.entry_price),
                highs[e.entry_index : e.entry_index + h],
                lows[e.entry_index : e.entry_index + h],
                closes[e.entry_index : e.entry_index + h],
                h,
            )
            row[f"mfe_h{h}"] = None if not m else m["mfe_pct"]
            row[f"mae_h{h}"] = None if not m else m["mae_pct"]
            if m and h == 12:
                row["first_0_25"] = first_touch_conservative_short(
                    highs[e.entry_index : e.entry_index + h],
                    lows[e.entry_index : e.entry_index + h],
                    float(e.entry_price),
                    0.25,
                    0.25,
                )
                row["first_0_50"] = first_touch_conservative_short(
                    highs[e.entry_index : e.entry_index + h],
                    lows[e.entry_index : e.entry_index + h],
                    float(e.entry_price),
                    0.50,
                    0.50,
                )
                row["first_1_00"] = first_touch_conservative_short(
                    highs[e.entry_index : e.entry_index + h],
                    lows[e.entry_index : e.entry_index + h],
                    float(e.entry_price),
                    1.00,
                    1.00,
                )
        for k, v in list(row.items()):
            if isinstance(v, pd.Timestamp):
                row[k] = str(v)
        # drop private
        row.pop("_sweep_only_entry_index", None)
        row.pop("_sweep_only_entry_price", None)
        march_events.append(row)

    march_summary = {
        "window": f"{MARCH_START} .. {MARCH_END} UTC",
        "n_events_50_25": len(march_events),
        "n_march_06": sum(1 for r in march_events if r.get("is_march_06")),
        "n_reclaim": sum(
            1
            for r in march_events
            if r.get("exclusive_reclaim_group") in {"immediate_reclaim", "delayed_reclaim_1_to_3"}
        ),
        "n_t2": sum(1 for r in march_events if r.get("trend_t2")),
        "n_t3": sum(1 for r in march_events if r.get("trend_t3")),
        "disclaimer": "Estimated LuxAlgo levels; not real exchange liquidations.",
        "t4_trend_state_machine": "omitted — not reproducibly available without touching protected modules",
    }

    def build_summary(sample: str) -> dict[str, Any]:
        xs = _filter_sample(events, sample)
        counts = {
            f"upper_{lev}x": sum(1 for e in xs if e.leverage == lev) for lev in (100, 50, 25)
        }
        reclaim = {
            "immediate_reclaim": sum(1 for e in xs if e.exclusive_reclaim_group == "immediate_reclaim"),
            "delayed_reclaim_1_to_3": sum(
                1 for e in xs if e.exclusive_reclaim_group == "delayed_reclaim_1_to_3"
            ),
            "no_reclaim_within_3": sum(
                1 for e in xs if e.exclusive_reclaim_group == "no_reclaim_within_3"
            ),
        }
        key_groups = [
            "upper_50x__sweep_only",
            "upper_50x__no_reclaim_within_3",
            "upper_50x__immediate_reclaim",
            "upper_50x__reclaim_within_3__T2",
            "upper_25x__reclaim_within_3__T2",
            "upper_25x__reclaim_within_3__T3",
        ]
        hits = {}
        for g in key_groups:
            for thr in (0.25, 0.50, 1.00):
                row = next(
                    (
                        r
                        for r in threshold_summary
                        if r["group"] == g
                        and r["sample"] == sample
                        and r["horizon"] == 12
                        and abs(float(r["target_pct"]) - thr) < 1e-12
                    ),
                    None,
                )
                hits[f"{g}_h12_thr{thr}"] = row
        best_tp = None
        cands = [
            r
            for r in tp_summary
            if r["sample"] == sample and r["mode"] == "first_signal_only"
        ]
        if cands:
            cands.sort(
                key=lambda r: (
                    r["mean_net_return_pct"] is not None,
                    r["mean_net_return_pct"] or -999,
                ),
                reverse=True,
            )
            best_tp = cands[0]
        return {
            "sample": sample,
            "event_counts": counts,
            "reclaim_counts": reclaim,
            "key_hit_rates_h12": hits,
            "control_rows": [r for r in control_comparison if r["sample"] == sample],
            "best_tp_sl_first_signal_only": best_tp,
            "disclaimer": "Estimated LuxAlgo-style liquidation levels, not real exchange liquidations.",
            "t4_omitted": True,
        }

    meta = {
        "n_candles": n,
        "in_sample_cut": in_sample_cut(n, cfg.in_sample_fraction),
        "start_timestamp": str(data.iloc[0]["timestamp"]),
        "end_timestamp": str(data.iloc[-1]["timestamp"]),
        "htf15_bars": len(htf15),
        "htf30_bars": len(htf30),
        "n_events": len(events),
    }
    return ShortSqueezeBundle(
        config=cfg,
        events=events,
        horizon_summary=horizon_summary,
        threshold_summary=threshold_summary,
        first_touch_summary=first_touch_summary,
        tp_sl_trades=tp_trades,
        tp_sl_summary=tp_summary,
        matched_controls=matched,
        control_comparison=control_comparison,
        variant_comparison=variant_comparison,
        monthly_summary=monthly_summary,
        march_events=march_events,
        march_summary=march_summary,
        summary_full=build_summary("full"),
        summary_in_sample=build_summary("in_sample"),
        summary_out_of_sample=build_summary("out_of_sample"),
        meta=meta,
        htf15=htf15,
        htf30=htf30,
    )


def events_to_dataframe(events: Sequence[ShortSqueezeEvent]) -> pd.DataFrame:
    rows = []
    for e in events:
        row = asdict(e)
        for k, v in list(row.items()):
            if isinstance(v, pd.Timestamp):
                row[k] = str(v)
        row["sweep_only_entry_index"] = e.__dict__.get("_sweep_only_entry_index")
        row["sweep_only_entry_price"] = e.__dict__.get("_sweep_only_entry_price")
        rows.append(row)
    return pd.DataFrame(rows)
