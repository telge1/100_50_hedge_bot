"""Causal horizon and TP/SL backtests for liquidation sweep signals."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

from research.liquidation_level.liquidation_features import (
    ALL_VARIANTS,
    FeatureBundle,
    SignalEvent,
)

DEFAULT_HORIZONS = (1, 3, 6, 12, 24, 48, 96)
DEFAULT_TPS = (0.25, 0.50, 0.75, 1.00, 1.50, 2.00)
DEFAULT_SLS = (0.25, 0.50, 0.75, 1.00, 1.50, 2.00)
DEFAULT_MAX_HOLDS = (6, 12, 24, 48, 96)


@dataclass(frozen=True)
class BacktestConfig:
    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    take_profits_pct: tuple[float, ...] = DEFAULT_TPS
    stop_losses_pct: tuple[float, ...] = DEFAULT_SLS
    max_holds: tuple[int, ...] = DEFAULT_MAX_HOLDS
    roundtrip_cost_pct: float = 0.12
    in_sample_fraction: float = 0.70
    control_runs: int = 100
    random_seed: int = 42
    skip_tp_sl: bool = False


@dataclass
class HorizonTrade:
    trade_id: str
    signal_id: str
    variant: str
    direction: str
    sample: str
    signal_index: int
    entry_index: int
    horizon: int
    entry_price: float
    exit_close: float
    gross_return_pct: float
    net_return_pct: float
    maximum_favorable_excursion_pct: float
    maximum_adverse_excursion_pct: float
    maximum_high: float
    minimum_low: float
    bars_held: int
    complete_horizon: bool


@dataclass
class TpSlTrade:
    trade_id: str
    signal_id: str
    variant: str
    direction: str
    sample: str
    signal_index: int
    entry_index: int
    tp_pct: float
    sl_pct: float
    max_hold: int
    entry_price: float
    exit_price: float
    exit_reason: str
    gross_return_pct: float
    net_return_pct: float
    bars_held: int
    maximum_favorable_excursion_pct: float
    maximum_adverse_excursion_pct: float


def assign_sample(candle_index: int, n_candles: int, in_sample_fraction: float = 0.70) -> str:
    """Split by candle index (not event count): first 70% in-sample."""
    cut = int(np.floor(float(n_candles) * float(in_sample_fraction)))
    cut = min(max(cut, 0), n_candles)
    return "in_sample" if int(candle_index) < cut else "out_of_sample"


def in_sample_cut(n_candles: int, in_sample_fraction: float = 0.70) -> int:
    return int(np.floor(float(n_candles) * float(in_sample_fraction)))


def long_return_pct(entry: float, exit_: float) -> float:
    return (float(exit_) / float(entry) - 1.0) * 100.0


def short_return_pct(entry: float, exit_: float) -> float:
    return (float(entry) / float(exit_) - 1.0) * 100.0


def apply_cost(gross_return_pct: float, roundtrip_cost_pct: float) -> float:
    return float(gross_return_pct) - float(roundtrip_cost_pct)


def path_mfe_mae_long(entry: float, highs: np.ndarray, lows: np.ndarray) -> tuple[float, float, float, float]:
    max_high = float(np.max(highs))
    min_low = float(np.min(lows))
    mfe = (max_high / float(entry) - 1.0) * 100.0
    mae = (min_low / float(entry) - 1.0) * 100.0  # adverse is negative or less positive
    # MAE as adverse excursion magnitude from entry (negative number preferred in reports)
    return mfe, mae, max_high, min_low


def path_mfe_mae_short(entry: float, highs: np.ndarray, lows: np.ndarray) -> tuple[float, float, float, float]:
    max_high = float(np.max(highs))
    min_low = float(np.min(lows))
    # Favorable for short: price down
    mfe = (float(entry) / min_low - 1.0) * 100.0
    # Adverse for short: price up
    mae = (float(entry) / max_high - 1.0) * 100.0  # negative when adverse
    return mfe, mae, max_high, min_low


def evaluate_horizon_trade(
    *,
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    entry_index: int,
    direction: str,
    horizon: int,
    roundtrip_cost_pct: float,
) -> dict[str, Any] | None:
    """Evaluate fixed-horizon exit at close of entry_index+horizon-1? 

    Holding N candles means exit at the close of the Nth candle after entry open.
    Entry at open[entry_index]; bars_held=N; exit_close = close[entry_index + N - 1]
    when N>=1 and that index exists.
    """
    n = len(opens)
    if entry_index < 0 or entry_index >= n:
        return None
    exit_index = int(entry_index) + int(horizon) - 1
    if exit_index >= n or horizon < 1:
        return None
    entry = float(opens[entry_index])
    if entry <= 0:
        return None
    path_h = highs[entry_index : exit_index + 1]
    path_l = lows[entry_index : exit_index + 1]
    exit_close = float(closes[exit_index])
    if direction == "long":
        gross = long_return_pct(entry, exit_close)
        mfe, mae, mx, mn = path_mfe_mae_long(entry, path_h, path_l)
    else:
        if exit_close <= 0:
            return None
        gross = short_return_pct(entry, exit_close)
        mfe, mae, mx, mn = path_mfe_mae_short(entry, path_h, path_l)
    return {
        "entry_price": entry,
        "exit_close": exit_close,
        "gross_return_pct": gross,
        "net_return_pct": apply_cost(gross, roundtrip_cost_pct),
        "maximum_favorable_excursion_pct": mfe,
        "maximum_adverse_excursion_pct": mae,
        "maximum_high": mx,
        "minimum_low": mn,
        "bars_held": int(horizon),
        "complete_horizon": True,
        "exit_index": exit_index,
    }


def evaluate_tp_sl_trade(
    *,
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    entry_index: int,
    direction: str,
    tp_pct: float,
    sl_pct: float,
    max_hold: int,
    roundtrip_cost_pct: float,
) -> dict[str, Any] | None:
    """Walk forward from entry open; if TP and SL same bar → SL first."""
    n = len(opens)
    if entry_index < 0 or entry_index >= n or max_hold < 1:
        return None
    entry = float(opens[entry_index])
    if entry <= 0:
        return None

    if direction == "long":
        tp_price = entry * (1.0 + float(tp_pct) / 100.0)
        sl_price = entry * (1.0 - float(sl_pct) / 100.0)
    else:
        tp_price = entry * (1.0 - float(tp_pct) / 100.0)
        sl_price = entry * (1.0 + float(sl_pct) / 100.0)

    last_i = min(entry_index + int(max_hold) - 1, n - 1)
    mfe = 0.0
    mae = 0.0
    for i in range(entry_index, last_i + 1):
        hi = float(highs[i])
        lo = float(lows[i])
        if direction == "long":
            mfe = max(mfe, (hi / entry - 1.0) * 100.0)
            mae = min(mae, (lo / entry - 1.0) * 100.0)
            hit_sl = lo <= sl_price
            hit_tp = hi >= tp_price
            if hit_sl and hit_tp:
                exit_price, reason = sl_price, "sl"
            elif hit_sl:
                exit_price, reason = sl_price, "sl"
            elif hit_tp:
                exit_price, reason = tp_price, "tp"
            else:
                continue
            gross = long_return_pct(entry, exit_price)
            return {
                "entry_price": entry,
                "exit_price": float(exit_price),
                "exit_reason": reason,
                "gross_return_pct": gross,
                "net_return_pct": apply_cost(gross, roundtrip_cost_pct),
                "bars_held": int(i - entry_index + 1),
                "maximum_favorable_excursion_pct": mfe,
                "maximum_adverse_excursion_pct": mae,
            }
        else:
            mfe = max(mfe, (entry / lo - 1.0) * 100.0 if lo > 0 else mfe)
            mae = min(mae, (entry / hi - 1.0) * 100.0 if hi > 0 else mae)
            hit_sl = hi >= sl_price
            hit_tp = lo <= tp_price
            if hit_sl and hit_tp:
                exit_price, reason = sl_price, "sl"
            elif hit_sl:
                exit_price, reason = sl_price, "sl"
            elif hit_tp:
                exit_price, reason = tp_price, "tp"
            else:
                continue
            gross = short_return_pct(entry, exit_price)
            return {
                "entry_price": entry,
                "exit_price": float(exit_price),
                "exit_reason": reason,
                "gross_return_pct": gross,
                "net_return_pct": apply_cost(gross, roundtrip_cost_pct),
                "bars_held": int(i - entry_index + 1),
                "maximum_favorable_excursion_pct": mfe,
                "maximum_adverse_excursion_pct": mae,
            }

    # timeout or end_of_data
    exit_price = float(closes[last_i])
    reason = "timeout" if last_i == entry_index + int(max_hold) - 1 else "end_of_data"
    if last_i < entry_index + int(max_hold) - 1:
        reason = "end_of_data"
    if direction == "long":
        gross = long_return_pct(entry, exit_price)
    else:
        if exit_price <= 0:
            return None
        gross = short_return_pct(entry, exit_price)
    return {
        "entry_price": entry,
        "exit_price": exit_price,
        "exit_reason": reason,
        "gross_return_pct": gross,
        "net_return_pct": apply_cost(gross, roundtrip_cost_pct),
        "bars_held": int(last_i - entry_index + 1),
        "maximum_favorable_excursion_pct": mfe,
        "maximum_adverse_excursion_pct": mae,
    }


def profit_factor(returns: Sequence[float]) -> float | None:
    gains = sum(r for r in returns if r > 0)
    losses = abs(sum(r for r in returns if r < 0))
    if losses == 0:
        return None if gains == 0 else float("inf")
    return float(gains / losses)


def summarize_returns(
    trades: Sequence[dict[str, Any]],
    *,
    net_key: str = "net_return_pct",
    gross_key: str = "gross_return_pct",
) -> dict[str, Any]:
    if not trades:
        return {
            "event_count": 0,
            "win_count": 0,
            "loss_count": 0,
            "win_rate_pct": None,
            "mean_gross_return_pct": None,
            "median_gross_return_pct": None,
            "mean_net_return_pct": None,
            "median_net_return_pct": None,
            "cumulative_gross_return_pct": None,
            "cumulative_net_return_pct": None,
            "mean_mfe_pct": None,
            "median_mfe_pct": None,
            "mean_mae_pct": None,
            "median_mae_pct": None,
            "profit_factor_gross": None,
            "profit_factor_net": None,
        }
    gross = [float(t[gross_key]) for t in trades]
    net = [float(t[net_key]) for t in trades]
    mfe = [float(t["maximum_favorable_excursion_pct"]) for t in trades if "maximum_favorable_excursion_pct" in t]
    mae = [float(t["maximum_adverse_excursion_pct"]) for t in trades if "maximum_adverse_excursion_pct" in t]
    wins = sum(1 for r in net if r > 0)
    losses = sum(1 for r in net if r <= 0)
    return {
        "event_count": len(trades),
        "win_count": wins,
        "loss_count": losses,
        "win_rate_pct": 100.0 * wins / len(trades),
        "mean_gross_return_pct": float(np.mean(gross)),
        "median_gross_return_pct": float(np.median(gross)),
        "mean_net_return_pct": float(np.mean(net)),
        "median_net_return_pct": float(np.median(net)),
        "cumulative_gross_return_pct": float(np.sum(gross)),
        "cumulative_net_return_pct": float(np.sum(net)),
        "mean_mfe_pct": float(np.mean(mfe)) if mfe else None,
        "median_mfe_pct": float(np.median(mfe)) if mfe else None,
        "mean_mae_pct": float(np.mean(mae)) if mae else None,
        "median_mae_pct": float(np.median(mae)) if mae else None,
        "profit_factor_gross": profit_factor(gross),
        "profit_factor_net": profit_factor(net),
    }


def _ohlcv_arrays(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    return (
        df["open"].to_numpy(dtype=float),
        df["high"].to_numpy(dtype=float),
        df["low"].to_numpy(dtype=float),
        df["close"].to_numpy(dtype=float),
    )


def build_horizon_trades(
    signals: Sequence[SignalEvent],
    ohlcv: pd.DataFrame,
    config: BacktestConfig,
) -> list[HorizonTrade]:
    opens, highs, lows, closes = _ohlcv_arrays(ohlcv)
    n = len(ohlcv)
    trades: list[HorizonTrade] = []
    tid = 0
    for sig in signals:
        if sig.entry_index is None or sig.entry_timestamp is None:
            continue
        sample = assign_sample(sig.signal_index, n, config.in_sample_fraction)
        for h in config.horizons:
            ev = evaluate_horizon_trade(
                opens=opens,
                highs=highs,
                lows=lows,
                closes=closes,
                entry_index=int(sig.entry_index),
                direction=sig.direction,
                horizon=int(h),
                roundtrip_cost_pct=config.roundtrip_cost_pct,
            )
            if ev is None:
                continue
            tid += 1
            trades.append(
                HorizonTrade(
                    trade_id=f"H_{tid:07d}",
                    signal_id=sig.signal_id,
                    variant=sig.variant,
                    direction=sig.direction,
                    sample=sample,
                    signal_index=int(sig.signal_index),
                    entry_index=int(sig.entry_index),
                    horizon=int(h),
                    entry_price=ev["entry_price"],
                    exit_close=ev["exit_close"],
                    gross_return_pct=ev["gross_return_pct"],
                    net_return_pct=ev["net_return_pct"],
                    maximum_favorable_excursion_pct=ev["maximum_favorable_excursion_pct"],
                    maximum_adverse_excursion_pct=ev["maximum_adverse_excursion_pct"],
                    maximum_high=ev["maximum_high"],
                    minimum_low=ev["minimum_low"],
                    bars_held=ev["bars_held"],
                    complete_horizon=bool(ev["complete_horizon"]),
                )
            )
    return trades


def iter_tp_sl_trade_rows(
    signals: Sequence[SignalEvent],
    ohlcv: pd.DataFrame,
    config: BacktestConfig,
):
    """Yield TP/SL trade dicts without retaining the full list."""
    if config.skip_tp_sl:
        return
        yield  # pragma: no cover — makes this a generator
    opens, highs, lows, closes = _ohlcv_arrays(ohlcv)
    n = len(ohlcv)
    tid = 0
    for sig in signals:
        if sig.entry_index is None or sig.entry_timestamp is None:
            continue
        sample = assign_sample(sig.signal_index, n, config.in_sample_fraction)
        for tp in config.take_profits_pct:
            for sl in config.stop_losses_pct:
                for mh in config.max_holds:
                    ev = evaluate_tp_sl_trade(
                        opens=opens,
                        highs=highs,
                        lows=lows,
                        closes=closes,
                        entry_index=int(sig.entry_index),
                        direction=sig.direction,
                        tp_pct=float(tp),
                        sl_pct=float(sl),
                        max_hold=int(mh),
                        roundtrip_cost_pct=config.roundtrip_cost_pct,
                    )
                    if ev is None:
                        continue
                    tid += 1
                    yield {
                        "trade_id": f"T_{tid:08d}",
                        "signal_id": sig.signal_id,
                        "variant": sig.variant,
                        "direction": sig.direction,
                        "sample": sample,
                        "signal_index": int(sig.signal_index),
                        "entry_index": int(sig.entry_index),
                        "tp_pct": float(tp),
                        "sl_pct": float(sl),
                        "max_hold": int(mh),
                        "entry_price": ev["entry_price"],
                        "exit_price": ev["exit_price"],
                        "exit_reason": ev["exit_reason"],
                        "gross_return_pct": ev["gross_return_pct"],
                        "net_return_pct": ev["net_return_pct"],
                        "bars_held": ev["bars_held"],
                        "maximum_favorable_excursion_pct": ev["maximum_favorable_excursion_pct"],
                        "maximum_adverse_excursion_pct": ev["maximum_adverse_excursion_pct"],
                    }


def build_tp_sl_trades(
    signals: Sequence[SignalEvent],
    ohlcv: pd.DataFrame,
    config: BacktestConfig,
) -> list[TpSlTrade]:
    trades: list[TpSlTrade] = []
    for row in iter_tp_sl_trade_rows(signals, ohlcv, config) or []:
        trades.append(TpSlTrade(**row))
    return trades


def accumulate_tp_sl_summary(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Online aggregate TP/SL metrics without storing all trades."""
    from collections import defaultdict

    buckets: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    # Keep per-bucket running stats instead of all rows when possible.
    # For correctness of medians we still need values; store returns only.
    store: dict[tuple, dict[str, Any]] = {}

    def key_for(row: dict[str, Any], sample: str) -> tuple:
        return (
            row["variant"],
            row["direction"],
            row["tp_pct"],
            row["sl_pct"],
            row["max_hold"],
            sample,
        )

    def ensure(key: tuple) -> dict[str, Any]:
        if key not in store:
            store[key] = {
                "gross": [],
                "net": [],
                "mfe": [],
                "mae": [],
                "bars": [],
                "tp": 0,
                "sl": 0,
                "timeout": 0,
                "end_of_data": 0,
            }
        return store[key]

    for row in rows:
        for sample in ("full", row["sample"]):
            st = ensure(key_for(row, sample))
            st["gross"].append(float(row["gross_return_pct"]))
            st["net"].append(float(row["net_return_pct"]))
            st["mfe"].append(float(row["maximum_favorable_excursion_pct"]))
            st["mae"].append(float(row["maximum_adverse_excursion_pct"]))
            st["bars"].append(int(row["bars_held"]))
            st[row["exit_reason"]] = st.get(row["exit_reason"], 0) + 1

    out: list[dict[str, Any]] = []
    for key, st in sorted(store.items()):
        variant, direction, tp, sl, mh, sample = key
        fake = [
            {
                "gross_return_pct": g,
                "net_return_pct": n,
                "maximum_favorable_excursion_pct": mfe,
                "maximum_adverse_excursion_pct": mae,
            }
            for g, n, mfe, mae in zip(st["gross"], st["net"], st["mfe"], st["mae"])
        ]
        stats = summarize_returns(fake)
        out.append(
            {
                "variant": variant,
                "direction": direction,
                "tp_pct": tp,
                "sl_pct": sl,
                "max_hold": mh,
                "sample": sample,
                **stats,
                "tp_count": st.get("tp", 0),
                "sl_count": st.get("sl", 0),
                "timeout_count": st.get("timeout", 0),
                "end_of_data_count": st.get("end_of_data", 0),
                "average_bars_held": float(np.mean(st["bars"])) if st["bars"] else None,
            }
        )
    return out


def precompute_horizon_returns(
    ohlcv: pd.DataFrame,
    horizons: Sequence[int],
) -> dict[tuple[str, int], np.ndarray]:
    """Return arrays long/short gross % for each entry index; NaN if incomplete."""
    opens, highs, lows, closes = _ohlcv_arrays(ohlcv)
    n = len(opens)
    out: dict[tuple[str, int], np.ndarray] = {}
    for h in horizons:
        long_r = np.full(n, np.nan, dtype=float)
        short_r = np.full(n, np.nan, dtype=float)
        for i in range(n):
            exit_i = i + int(h) - 1
            if exit_i >= n or opens[i] <= 0 or closes[exit_i] <= 0:
                continue
            long_r[i] = long_return_pct(opens[i], closes[exit_i])
            short_r[i] = short_return_pct(opens[i], closes[exit_i])
        out[("long", int(h))] = long_r
        out[("short", int(h))] = short_r
    return out


def run_control_comparison(
    horizon_trades: Sequence[HorizonTrade],
    ohlcv: pd.DataFrame,
    config: BacktestConfig,
) -> list[dict[str, Any]]:
    """Deterministic random-entry controls per variant/horizon/sample."""
    pre = precompute_horizon_returns(ohlcv, config.horizons)
    n = len(ohlcv)
    cut = in_sample_cut(n, config.in_sample_fraction)
    rows: list[dict[str, Any]] = []

    # group event means (per sample + full)
    groups: dict[tuple[str, str, int, str], list[float]] = {}
    for t in horizon_trades:
        groups.setdefault((t.variant, t.direction, int(t.horizon), t.sample), []).append(
            float(t.gross_return_pct)
        )
        groups.setdefault((t.variant, t.direction, int(t.horizon), "full"), []).append(
            float(t.gross_return_pct)
        )

    rng = np.random.default_rng(int(config.random_seed))

    for (variant, direction, horizon, sample), rets in sorted(groups.items()):
        event_mean = float(np.mean(rets))
        n_events = len(rets)
        arr = pre[(direction, horizon)]
        if sample == "in_sample":
            lo, hi = 0, cut
        elif sample == "out_of_sample":
            lo, hi = cut, n
        else:
            lo, hi = 0, n
        valid = np.flatnonzero(np.isfinite(arr[lo:hi])) + lo
        if len(valid) < n_events or n_events == 0:
            rows.append(
                {
                    "variant": variant,
                    "direction": direction,
                    "horizon": horizon,
                    "sample": sample,
                    "event_count": n_events,
                    "event_mean_gross_return_pct": event_mean,
                    "control_mean_gross_return_pct": None,
                    "event_minus_control": None,
                    "control_runs": 0,
                    "fraction_controls_better": None,
                    "empirical_p_value_estimate": None,
                    "note": "insufficient_valid_control_entries",
                }
            )
            continue

        control_means = np.empty(int(config.control_runs), dtype=float)
        for r in range(int(config.control_runs)):
            pick = rng.choice(valid, size=n_events, replace=False)
            control_means[r] = float(np.mean(arr[pick]))
        frac_better = float(np.mean(control_means >= event_mean))
        ctrl_mean = float(np.mean(control_means))
        rows.append(
            {
                "variant": variant,
                "direction": direction,
                "horizon": horizon,
                "sample": sample,
                "event_count": n_events,
                "event_mean_gross_return_pct": event_mean,
                "control_mean_gross_return_pct": ctrl_mean,
                "event_minus_control": event_mean - ctrl_mean,
                "control_runs": int(config.control_runs),
                "fraction_controls_better": frac_better,
                "empirical_p_value_estimate": frac_better,
                "note": "",
            }
        )
    return rows


def horizon_summary_rows(trades: Sequence[HorizonTrade]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    # full / sample splits
    buckets: dict[tuple[str, str, int, str], list[dict[str, Any]]] = {}
    for t in trades:
        d = asdict(t)
        for sample_label in ("full", t.sample):
            key = (t.variant, t.direction, int(t.horizon), sample_label)
            if sample_label == "full" or sample_label == t.sample:
                buckets.setdefault(key, []).append(d)
    # fix: for full include all
    buckets = {}
    for t in trades:
        d = asdict(t)
        buckets.setdefault((t.variant, t.direction, int(t.horizon), "full"), []).append(d)
        buckets.setdefault((t.variant, t.direction, int(t.horizon), t.sample), []).append(d)

    for (variant, direction, horizon, sample), items in sorted(buckets.items()):
        stats = summarize_returns(items)
        rows.append(
            {
                "variant": variant,
                "direction": direction,
                "horizon": horizon,
                "sample": sample,
                **stats,
            }
        )
    return rows


def tp_sl_summary_rows(trades: Sequence[TpSlTrade]) -> list[dict[str, Any]]:
    buckets: dict[tuple, list[dict[str, Any]]] = {}
    for t in trades:
        d = asdict(t)
        for sample in ("full", t.sample):
            key = (t.variant, t.direction, t.tp_pct, t.sl_pct, t.max_hold, sample if sample == "full" else t.sample)
            # simpler:
    buckets = {}
    for t in trades:
        d = asdict(t)
        keys = [
            (t.variant, t.direction, t.tp_pct, t.sl_pct, t.max_hold, "full"),
            (t.variant, t.direction, t.tp_pct, t.sl_pct, t.max_hold, t.sample),
        ]
        for key in keys:
            buckets.setdefault(key, []).append(d)

    rows: list[dict[str, Any]] = []
    for (variant, direction, tp, sl, mh, sample), items in sorted(buckets.items()):
        stats = summarize_returns(items)
        tp_c = sum(1 for x in items if x["exit_reason"] == "tp")
        sl_c = sum(1 for x in items if x["exit_reason"] == "sl")
        to_c = sum(1 for x in items if x["exit_reason"] == "timeout")
        eod = sum(1 for x in items if x["exit_reason"] == "end_of_data")
        avg_bars = float(np.mean([x["bars_held"] for x in items])) if items else None
        rows.append(
            {
                "variant": variant,
                "direction": direction,
                "tp_pct": tp,
                "sl_pct": sl,
                "max_hold": mh,
                "sample": sample,
                **stats,
                "tp_count": tp_c,
                "sl_count": sl_c,
                "timeout_count": to_c,
                "end_of_data_count": eod,
                "average_bars_held": avg_bars,
            }
        )
    return rows


def variant_comparison_rows(
    horizon_rows: Sequence[dict[str, Any]],
    *,
    prefer_horizon: int = 12,
) -> list[dict[str, Any]]:
    """Compact comparison at a default horizon for full/IS/OOS."""
    out = []
    for row in horizon_rows:
        if int(row.get("horizon", -1)) != int(prefer_horizon):
            continue
        out.append(dict(row))
    return out


def monthly_summary_rows(
    trades: Sequence[HorizonTrade],
    ohlcv: pd.DataFrame,
    *,
    horizon: int = 12,
) -> list[dict[str, Any]]:
    ts = pd.to_datetime(ohlcv["timestamp"], utc=True)
    rows: list[dict[str, Any]] = []
    # group by variant/direction/month
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for t in trades:
        if int(t.horizon) != int(horizon):
            continue
        if t.entry_index < 0 or t.entry_index >= len(ts):
            continue
        month = str(ts.iloc[t.entry_index].strftime("%Y-%m"))
        buckets.setdefault((t.variant, t.direction, month), []).append(asdict(t))
    for (variant, direction, month), items in sorted(buckets.items()):
        stats = summarize_returns(items)
        rows.append({"variant": variant, "direction": direction, "month": month, "horizon": horizon, **stats})
    return rows


def build_sample_summary(
    horizon_trades: Sequence[HorizonTrade],
    *,
    sample: str | None,
    feature_summary: dict[str, Any],
    control_rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    if sample is None or sample == "full":
        subset = list(horizon_trades)
        sample_label = "full"
    else:
        subset = [t for t in horizon_trades if t.sample == sample]
        sample_label = sample

    by_variant: dict[str, Any] = {}
    for v in ALL_VARIANTS:
        vtrades = [t for t in subset if t.variant == v]
        # default focus horizon 12
        h12 = [t for t in vtrades if t.horizon == 12]
        by_variant[v] = {
            "signal_trades_h12": summarize_returns([asdict(t) for t in h12]),
            "n_signals_approx": len({t.signal_id for t in vtrades}),
        }

    # best long / short by mean net h12
    def best(direction: str) -> dict[str, Any] | None:
        cands = []
        for v, payload in by_variant.items():
            s = payload["signal_trades_h12"]
            if s["event_count"] and (
                (direction == "long" and v.startswith("L"))
                or (direction == "short" and v.startswith("S"))
                or (direction == "long" and v == "F_LONG")
                or (direction == "short" and v == "F_SHORT")
            ):
                # match direction field
                pass
        for v in ALL_VARIANTS:
            vtrades = [t for t in subset if t.variant == v and t.horizon == 12 and t.direction == direction]
            if not vtrades:
                continue
            stats = summarize_returns([asdict(t) for t in vtrades])
            cands.append((v, stats))
        if not cands:
            return None
        cands.sort(key=lambda x: (x[1]["mean_net_return_pct"] is not None, x[1]["mean_net_return_pct"]), reverse=True)
        return {"variant": cands[0][0], **cands[0][1]}

    ctrl = [r for r in control_rows if r.get("sample") == sample_label or (sample_label == "full" and r.get("sample") in {"in_sample", "out_of_sample"})]
    # for full summary, aggregate control note separately
    return {
        "sample": sample_label,
        "feature_summary": feature_summary,
        "variants_horizon12": by_variant,
        "best_long_horizon12_net": best("long"),
        "best_short_horizon12_net": best("short"),
        "control_rows_related": len(ctrl),
        "n_horizon_trades": len(subset),
    }


@dataclass
class BacktestBundle:
    config: BacktestConfig
    horizon_trades: list[HorizonTrade]
    tp_sl_trades: list[TpSlTrade]
    horizon_summary: list[dict[str, Any]]
    tp_sl_summary: list[dict[str, Any]]
    variant_comparison: list[dict[str, Any]]
    control_comparison: list[dict[str, Any]]
    monthly_summary: list[dict[str, Any]]
    summary_full: dict[str, Any]
    summary_in_sample: dict[str, Any]
    summary_out_of_sample: dict[str, Any]
    meta: dict[str, Any] = field(default_factory=dict)


def run_backtest(
    features: FeatureBundle,
    config: BacktestConfig | None = None,
    *,
    tp_sl_csv_path: Path | None = None,
) -> BacktestBundle:
    import csv

    cfg = config or BacktestConfig()
    print(f"signals={len(features.signals)} building horizon trades...", flush=True)
    h_trades = build_horizon_trades(features.signals, features.ohlcv, cfg)
    print(f"horizon_trades={len(h_trades)}", flush=True)

    tp_trades: list[TpSlTrade] = []
    tp_sum: list[dict[str, Any]] = []
    tp_count = 0
    if not cfg.skip_tp_sl:
        print("building TP/SL trades (streaming)...", flush=True)

        store: dict[tuple, dict[str, Any]] = {}

        def ensure(key: tuple) -> dict[str, Any]:
            if key not in store:
                store[key] = {
                    "gross": [],
                    "net": [],
                    "mfe": [],
                    "mae": [],
                    "bars": [],
                    "tp": 0,
                    "sl": 0,
                    "timeout": 0,
                    "end_of_data": 0,
                }
            return store[key]

        writer = None
        fh = None
        if tp_sl_csv_path is not None:
            tp_sl_csv_path.parent.mkdir(parents=True, exist_ok=True)
            fh = tp_sl_csv_path.open("w", newline="", encoding="utf-8")

        try:
            for row in iter_tp_sl_trade_rows(features.signals, features.ohlcv, cfg):
                tp_count += 1
                if fh is not None:
                    if writer is None:
                        writer = csv.DictWriter(fh, fieldnames=list(row.keys()))
                        writer.writeheader()
                    writer.writerow(row)
                for sample in ("full", row["sample"]):
                    key = (
                        row["variant"],
                        row["direction"],
                        row["tp_pct"],
                        row["sl_pct"],
                        row["max_hold"],
                        sample,
                    )
                    st = ensure(key)
                    st["gross"].append(float(row["gross_return_pct"]))
                    st["net"].append(float(row["net_return_pct"]))
                    st["mfe"].append(float(row["maximum_favorable_excursion_pct"]))
                    st["mae"].append(float(row["maximum_adverse_excursion_pct"]))
                    st["bars"].append(int(row["bars_held"]))
                    st[row["exit_reason"]] = st.get(row["exit_reason"], 0) + 1
                if tp_count % 200000 == 0:
                    print(f"  tp_sl_trades={tp_count}", flush=True)
        finally:
            if fh is not None:
                fh.close()

        for key, st in sorted(store.items()):
            variant, direction, tp, sl, mh, sample = key
            fake = [
                {
                    "gross_return_pct": g,
                    "net_return_pct": n,
                    "maximum_favorable_excursion_pct": mfe,
                    "maximum_adverse_excursion_pct": mae,
                }
                for g, n, mfe, mae in zip(st["gross"], st["net"], st["mfe"], st["mae"])
            ]
            stats = summarize_returns(fake)
            tp_sum.append(
                {
                    "variant": variant,
                    "direction": direction,
                    "tp_pct": tp,
                    "sl_pct": sl,
                    "max_hold": mh,
                    "sample": sample,
                    **stats,
                    "tp_count": st.get("tp", 0),
                    "sl_count": st.get("sl", 0),
                    "timeout_count": st.get("timeout", 0),
                    "end_of_data_count": st.get("end_of_data", 0),
                    "average_bars_held": float(np.mean(st["bars"])) if st["bars"] else None,
                }
            )
        print(f"tp_sl_trades={tp_count}", flush=True)
    else:
        if tp_sl_csv_path is not None:
            tp_sl_csv_path.write_text("", encoding="utf-8")

    print("summaries + controls...", flush=True)
    h_sum = horizon_summary_rows(h_trades)
    var_cmp = variant_comparison_rows(h_sum, prefer_horizon=12)
    ctrl = run_control_comparison(h_trades, features.ohlcv, cfg)
    monthly = monthly_summary_rows(h_trades, features.ohlcv, horizon=12)
    s_full = build_sample_summary(h_trades, sample="full", feature_summary=features.summary, control_rows=ctrl)
    s_is = build_sample_summary(h_trades, sample="in_sample", feature_summary=features.summary, control_rows=ctrl)
    s_oos = build_sample_summary(h_trades, sample="out_of_sample", feature_summary=features.summary, control_rows=ctrl)
    return BacktestBundle(
        config=cfg,
        horizon_trades=h_trades,
        tp_sl_trades=tp_trades,
        horizon_summary=h_sum,
        tp_sl_summary=tp_sum,
        variant_comparison=var_cmp,
        control_comparison=ctrl,
        monthly_summary=monthly,
        summary_full=s_full,
        summary_in_sample=s_is,
        summary_out_of_sample=s_oos,
        meta={
            "n_candles": len(features.ohlcv),
            "in_sample_cut": in_sample_cut(len(features.ohlcv), cfg.in_sample_fraction),
            "tp_sl_trade_count": tp_count,
        },
    )
