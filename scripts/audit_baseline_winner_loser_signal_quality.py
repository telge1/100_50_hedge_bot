#!/usr/bin/env python3
"""AUDIT_BASELINE_WINNERS_VS_LOSERS_SIGNAL_QUALITY — research-only, no DB writes.

Compares pre-entry causal features of 15m Tier-A BASELINE_IMMEDIATE winners vs losers.
No strategy changes. No HTF alignment filters. No 1m wait rules.
"""

from __future__ import annotations

import csv
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.bybit.live.control_api import _signal_row_to_api  # noqa: E402
from signal_generator.config import get_clickhouse_settings  # noqa: E402
from signal_generator.db.candles import CandleRepository  # noqa: E402
from signal_generator.db.setup import setup_clickhouse  # noqa: E402
from signal_generator.db.signals import SignalRepository  # noqa: E402
from signal_generator.research.one_m_entry_timing.timing import (  # noqa: E402
    _prepare_1m,
    _simulate_outcome,
    _utc,
)
from signal_generator.strategy.wave_fade.adapter import bars_to_ohlcv_df  # noqa: E402
from signal_generator.strategy.wave_fade.indicators import attach_indicators  # noqa: E402
from signal_generator.strategy.wave_fade.parameters import (  # noqa: E402
    PRIMARY_FEE,
    STOCH_HIGH_K,
    STOCH_LOW_K,
)
from signal_generator.strategy.wave_fade.signals import (  # noqa: E402
    build_waves_from_ohlcv,
)
from signal_generator.timeframes import (  # noqa: E402
    aggregate_1m_to_timeframe,
    bars_from_mappings,
)

OUT = ROOT / "results" / "baseline_winner_loser_signal_quality_audit"
PRIOR_AS_OF = datetime(2026, 8, 11, 9, 30, 7, 105630, tzinfo=timezone.utc)
HOURS = 168
FEE_PCT = float(PRIMARY_FEE)
SIGNAL_TF = "15m"
BAR_MIN = 15

EXPECT_N = 189
EXPECT_LOSSES = 72
EXPECT_WR = 61.1
EXPECT_NET = 20.6
TOL_WR = 0.2
TOL_NET = 0.5

ATR_LEN = 14
ATR_MED_LEN = 50
LOOKAHEAD_VIOLATIONS = 0


def _iso(ts: Any | None) -> str | None:
    if ts is None:
        return None
    return _utc(ts).isoformat().replace("+00:00", "Z")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = ATR_LEN) -> pd.Series:
    prev_c = close.shift(1)
    tr = pd.concat(
        [(high - low).abs(), (high - prev_c).abs(), (low - prev_c).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()


def enrich_tf_frame(df: pd.DataFrame) -> pd.DataFrame:
    """attach_indicators + ATR + audit EMAs 59/200 (strategy has 9/20/100/400)."""
    if df.empty:
        return df
    out = attach_indicators(df)
    close = out["close"].astype(float)
    high = out["high"].astype(float)
    low = out["low"].astype(float)
    out["atr"] = wilder_atr(high, low, close, ATR_LEN)
    out["atr_pct"] = (out["atr"] / close.replace(0, np.nan)) * 100.0
    out["atr_med"] = out["atr"].rolling(ATR_MED_LEN, min_periods=10).median()
    out["atr_vs_med"] = out["atr"] / out["atr_med"].replace(0, np.nan)
    out["range"] = high - low
    out["range_atr"] = out["range"] / out["atr"].replace(0, np.nan)
    out["body"] = (close - out["open"].astype(float)).abs()
    out["body_atr"] = out["body"] / out["atr"].replace(0, np.nan)
    out["ema59"] = close.ewm(span=59, adjust=False, min_periods=59).mean()
    out["ema200"] = close.ewm(span=200, adjust=False, min_periods=200).mean()
    # range percentile causal rolling
    out["range_pctile_50"] = out["range"].rolling(50, min_periods=20).apply(
        lambda x: float(stats.percentileofscore(x, x.iloc[-1], kind="rank")), raw=False
    )
    out["atr_pctile_50"] = out["atr"].rolling(50, min_periods=20).apply(
        lambda x: float(stats.percentileofscore(x, x.iloc[-1], kind="rank")), raw=False
    )
    return out


def causal_idx(df: pd.DataFrame, entry_ts: datetime) -> int:
    """Last bar with available_at <= entry_ts."""
    if df.empty or "available_at" not in df.columns:
        return -1
    t = pd.Timestamp(_utc(entry_ts))
    known = df.index[df["available_at"] <= t]
    if len(known) == 0:
        return -1
    return int(known[-1])


def check_causal(available_at: Any, entry_ts: datetime, feature: str) -> None:
    global LOOKAHEAD_VIOLATIONS
    if available_at is None or (isinstance(available_at, float) and np.isnan(available_at)):
        return
    if _utc(available_at) > _utc(entry_ts):
        LOOKAHEAD_VIOLATIONS += 1
        print(f"LOOKAHEAD: {feature} available_at={available_at} > entry={entry_ts}", flush=True)


def safe_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def candle_struct(df: pd.DataFrame, i: int) -> dict[str, float | None | bool]:
    if i < 0 or i >= len(df):
        return {}
    o, h, l, c = (float(df.iloc[i][k]) for k in ("open", "high", "low", "close"))
    rng = h - l
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    atr = safe_float(df.iloc[i].get("atr"))
    return {
        "body_pct_of_range": (body / rng * 100.0) if rng > 0 else None,
        "upper_wick_pct": (upper / rng * 100.0) if rng > 0 else None,
        "lower_wick_pct": (lower / rng * 100.0) if rng > 0 else None,
        "body_atr": (body / atr) if atr and atr > 0 else None,
        "range_atr": (rng / atr) if atr and atr > 0 else None,
        "close_position_in_range": ((c - l) / rng) if rng > 0 else None,
        "bullish_candle": c > o,
        "bearish_candle": c < o,
    }


def aligned_ret(side: str, px0: float, px1: float) -> float | None:
    """Positive = move in fade-adverse direction (continuation against fade)."""
    if px0 <= 0:
        return None
    raw = (px1 / px0 - 1.0) * 100.0
    # For LONG fade after down-move: adverse continuation = further down = negative raw → we want
    # "aligned_move" as magnitude of impulse INTO the fade (direction of prior trend).
    # LONG: prior impulse down → aligned = -raw (positive if price fell)
    # SHORT: prior impulse up → aligned = +raw
    if side == "LONG":
        return -raw
    return raw


def momentum_regime(aligned_1: float | None, aligned_3: float | None, aligned_5: float | None) -> str:
    """SLOWING / STABLE / ACCELERATING of impulse into the fade (simple)."""
    vals = [v for v in (aligned_5, aligned_3, aligned_1) if v is not None]
    if len(vals) < 2:
        return "UNKNOWN"
    # Compare recent 1-bar aligned move vs average of longer window intensity per bar
    a1 = aligned_1 if aligned_1 is not None else 0.0
    a3 = (aligned_3 / 3.0) if aligned_3 is not None else a1
    if a1 > a3 * 1.25 and a1 > 0.05:
        return "ACCELERATING"
    if a1 < a3 * 0.75:
        return "SLOWING"
    return "STABLE"


def cliffs_delta(x: list[float], y: list[float]) -> float | None:
    """Cliff's delta: dominance of x over y. +1 => all x > all y."""
    if not x or not y:
        return None
    xa = np.asarray(x, dtype=float)
    ya = np.asarray(y, dtype=float)
    # efficient approx via mannwhitney
    nx, ny = len(xa), len(ya)
    u, _ = stats.mannwhitneyu(xa, ya, alternative="two-sided")
    return float((2.0 * u) / (nx * ny) - 1.0)


def auc_score(wins: list[float], losses: list[float]) -> float | None:
    """AUC treating higher feature → more likely WIN. 0.5 = no discrimination."""
    if len(wins) < 3 or len(losses) < 3:
        return None
    # Mann-Whitney U / (n1*n2)
    u, _ = stats.mannwhitneyu(wins, losses, alternative="two-sided")
    return float(u / (len(wins) * len(losses)))


def summarize_feat(wins: list[float], losses: list[float], name: str) -> dict[str, Any]:
    def qs(xs: list[float]) -> dict[str, float | None]:
        if not xs:
            return {"count": 0, "mean": None, "median": None, "p25": None, "p75": None}
        a = np.asarray(xs, dtype=float)
        return {
            "count": len(xs),
            "mean": float(np.mean(a)),
            "median": float(np.median(a)),
            "p25": float(np.percentile(a, 25)),
            "p75": float(np.percentile(a, 75)),
        }

    wq, lq = qs(wins), qs(losses)
    delta = cliffs_delta(wins, losses)
    auc = auc_score(wins, losses)
    # association: |auc-0.5|*2 as strength; also spearman with label win=1
    labels = [1] * len(wins) + [0] * len(losses)
    vals = wins + losses
    spearman = None
    if len(vals) >= 10 and len(set(vals)) > 1:
        sp, _ = stats.spearmanr(vals, labels)
        spearman = float(sp) if sp is not None and not (isinstance(sp, float) and math.isnan(sp)) else None
    strength = abs((auc or 0.5) - 0.5) * 2.0 if auc is not None else None
    return {
        "feature": name,
        "winner_count": wq["count"],
        "winner_mean": wq["mean"],
        "winner_median": wq["median"],
        "winner_p25": wq["p25"],
        "winner_p75": wq["p75"],
        "loser_count": lq["count"],
        "loser_mean": lq["mean"],
        "loser_median": lq["median"],
        "loser_p25": lq["p25"],
        "loser_p75": lq["p75"],
        "cliffs_delta_win_vs_loss": delta,
        "auc_higher_favors_win": auc,
        "spearman_with_win": spearman,
        "assoc_strength": strength,
        "median_gap_win_minus_loss": (
            (wq["median"] - lq["median"]) if wq["median"] is not None and lq["median"] is not None else None
        ),
    }


def adverse_mae(side: str, entry: float, work: pd.DataFrame, entry_i: int, minutes: int) -> float | None:
    if entry_i < 0 or entry <= 0 or work.empty:
        return None
    end_i = min(len(work) - 1, entry_i + minutes)
    highs = work["high"].to_numpy(dtype=float)[entry_i : end_i + 1]
    lows = work["low"].to_numpy(dtype=float)[entry_i : end_i + 1]
    if highs.size == 0:
        return None
    if side == "LONG":
        return float(np.min((lows / entry - 1.0) * 100.0))
    return float(np.min(-((highs - entry) / entry * 100.0)))


def parse_meta(r: dict) -> dict:
    m = r.get("metadata")
    if isinstance(m, str):
        try:
            m = json.loads(m)
        except Exception:
            m = {}
    return m if isinstance(m, dict) else {}


def main() -> int:
    global LOOKAHEAD_VIOLATIONS
    LOOKAHEAD_VIOLATIONS = 0
    OUT.mkdir(parents=True, exist_ok=True)
    now = PRIOR_AS_OF
    start = now - timedelta(hours=HOURS)

    ch = setup_clickhouse(settings=get_clickhouse_settings())
    sig_repo = SignalRepository(ch)
    candle_repo = CandleRepository(ch)

    print(f"Loading Tier-A {start.isoformat()} → {now.isoformat()} …", flush=True)
    rows, total = sig_repo.query_signals(
        start=start,
        end=now,
        tier_a=True,
        timeframe=SIGNAL_TF,
        time_field="candle_close_time",
        limit=5000,
        offset=0,
    )
    api_rows = [_signal_row_to_api(r) for r in rows]
    api_rows = [r for r in api_rows if str(r.get("timeframe")) == SIGNAL_TF]
    # chronological for repeat-context
    api_rows.sort(key=lambda r: _utc(r.get("entry_time") or r.get("candle_close_time")))
    print(f"15m Tier-A: {len(api_rows)} (total={total})", flush=True)

    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in api_rows:
        by_sym[str(r["symbol"]).upper()].append(r)

    # Warmup for EMA200 on 15m: 200*15m ≈ 50h → pad 10d for ATR percentile + EMA400
    pad_before = timedelta(days=12)
    pad_after = timedelta(hours=36)
    raw_1m: dict[str, list] = {}
    work_1m: dict[str, pd.DataFrame] = {}
    tf15: dict[str, pd.DataFrame] = {}
    waves: dict[str, pd.DataFrame] = {}

    for sym, items in by_sym.items():
        t0s = [_utc(r.get("entry_time") or r.get("candle_close_time")) for r in items]
        a, b = min(t0s) - pad_before, max(t0s) + pad_after
        raw = candle_repo.get_candles(sym, a, b) or []
        raw_1m[sym] = raw
        work_1m[sym] = _prepare_1m(pd.DataFrame(raw)) if raw else pd.DataFrame()
        bars = bars_from_mappings(raw)
        htf = aggregate_1m_to_timeframe(bars, SIGNAL_TF, as_of=now, require_complete=True)
        df = bars_to_ohlcv_df(htf)
        df = enrich_tf_frame(df)
        tf15[sym] = df
        try:
            waves[sym] = build_waves_from_ohlcv(df, symbol=sym, timeframe=SIGNAL_TF) if not df.empty else pd.DataFrame()
        except Exception as e:
            print(f"  wave build fail {sym}: {e}", flush=True)
            waves[sym] = pd.DataFrame()
        print(f"  {sym}: 1m={len(work_1m[sym])} 15m={len(df)} waves={len(waves[sym])}", flush=True)

    details: list[dict[str, Any]] = []
    history_by_sym: dict[str, list[dict]] = defaultdict(list)

    for r in api_rows:
        sym = str(r["symbol"]).upper()
        side = str(r["direction"]).upper()
        sid = str(r["signal_id"])
        signal_ts = _utc(r.get("candle_close_time") or r.get("generated_at"))
        t0 = _utc(r.get("entry_time") or signal_ts)
        meta = parse_meta(r)
        try:
            entry_px = float(r["entry_price"]) if r.get("entry_price") is not None else None
        except (TypeError, ValueError):
            entry_px = None

        work = work_1m[sym]
        df = tf15[sym]
        entry_i_1m = -1
        if not work.empty:
            entry_i_1m = int(work["timestamp"].searchsorted(pd.Timestamp(t0), side="left"))
            if entry_i_1m >= len(work):
                entry_i_1m = -1
        if entry_i_1m >= 0 and entry_px is None:
            entry_px = float(work.iloc[entry_i_1m]["open"])

        result, pnl, mae, mfe, exit_reason = "OPEN", None, None, None, None
        if entry_i_1m >= 0 and entry_px is not None and not work.empty:
            sim = _simulate_outcome(
                side=side, entry=float(entry_px), tf=SIGNAL_TF, entry_i=entry_i_1m, work=work, as_of=now
            )
            result, pnl, mae, mfe, exit_reason = (
                sim["result"],
                sim.get("pnl_pct"),
                sim.get("mae_pct"),
                sim.get("mfe_pct"),
                sim.get("exit_reason"),
            )

        mae30 = (
            adverse_mae(side, float(entry_px), work, entry_i_1m, 30)
            if entry_i_1m >= 0 and entry_px
            else None
        )
        early_dd_2 = bool(mae30 is not None and mae30 <= -2.0)
        early_dd_3 = bool(mae30 is not None and mae30 <= -3.0)

        i = causal_idx(df, t0)
        feat: dict[str, Any] = {
            "signal_id": sid,
            "symbol": sym,
            "direction": side,
            "signal_tf": SIGNAL_TF,
            "signal_ts": _iso(signal_ts),
            "entry_ts": _iso(t0),
            "entry_price": entry_px,
            "result": result,
            "pnl": pnl,
            "mae": mae,
            "mfe": mfe,
            "exit_reason": exit_reason,
            "early_dd_2pct": early_dd_2,
            "early_dd_3pct": early_dd_3,
            "source_tf": SIGNAL_TF,
        }

        if i < 0 or df.empty:
            details.append(feat)
            history_by_sym[sym].append({"ts": t0, "side": side, "sid": sid})
            continue

        row = df.iloc[i]
        avail = row["available_at"]
        check_causal(avail, t0, "signal_bar")
        feat["source_candle_open"] = _iso(row["timestamp"])
        feat["available_at"] = _iso(avail)

        # --- Group A: Stoch / wave ---
        k = safe_float(row["stoch_k"])
        d = safe_float(row["stoch_d"])
        feat["stoch_k"] = k
        feat["stoch_d"] = d
        feat["distance_k_d"] = (k - d) if k is not None and d is not None else None
        if side == "LONG":
            feat["stoch_extremeness"] = (STOCH_LOW_K - k) if k is not None else None  # deeper OS => higher
            feat["distance_from_20_or_80"] = (k - STOCH_LOW_K) if k is not None else None
        else:
            feat["stoch_extremeness"] = (k - STOCH_HIGH_K) if k is not None else None
            feat["distance_from_20_or_80"] = (STOCH_HIGH_K - k) if k is not None else None

        for lag, name in ((1, "stoch_slope_1"), (2, "stoch_slope_2"), (3, "stoch_slope_3")):
            if i >= lag and k is not None:
                pk = safe_float(df.iloc[i - lag]["stoch_k"])
                feat[name] = (k - pk) if pk is not None else None
            else:
                feat[name] = None
        s1, s2 = feat.get("stoch_slope_1"), feat.get("stoch_slope_2")
        feat["stoch_acceleration"] = (s1 - s2) if s1 is not None and s2 is not None else None

        # bars since extreme / kd cross
        bars_ext = None
        bars_cross = None
        if k is not None:
            for back in range(0, min(i, 40) + 1):
                kk = safe_float(df.iloc[i - back]["stoch_k"])
                if kk is None:
                    continue
                hit = (side == "LONG" and kk <= STOCH_LOW_K) or (side == "SHORT" and kk >= STOCH_HIGH_K)
                if hit:
                    bars_ext = back
                    break
        for back in range(0, min(i, 40) + 1):
            rr = df.iloc[i - back]
            if side == "LONG" and bool(rr.get("stoch_bullish_cross")):
                bars_cross = back
                break
            if side == "SHORT" and bool(rr.get("stoch_bearish_cross")):
                bars_cross = back
                break
        feat["bars_since_stoch_extreme"] = bars_ext
        feat["bars_since_kd_cross"] = bars_cross

        # wave from strategy segmenter: last wave ending at/before this bar
        wdf = waves[sym]
        wave_n = safe_float(meta.get("n_bars"))
        wave_eff = meta.get("eff_quantile")
        wave_dur = None
        if not wdf.empty and "end_available_at" in wdf.columns:
            wknown = wdf[pd.to_datetime(wdf["end_available_at"], utc=True) <= pd.Timestamp(t0)]
            if not wknown.empty:
                w = wknown.iloc[-1]
                check_causal(w["end_available_at"], t0, "wave_end")
                wave_n = int(w["n_bars"]) if w.get("n_bars") is not None else wave_n
                wave_dur = float(w["n_bars"]) * BAR_MIN if w.get("n_bars") is not None else None
                feat["wave_price_move_pct"] = safe_float(w.get("signed_price_move_pct"))
                feat["wave_directional_efficiency"] = safe_float(w.get("directional_efficiency"))
                feat["wave_stoch_k_end"] = safe_float(w.get("stoch_k_end"))
                feat["wave_stoch_delta"] = safe_float(w.get("stoch_delta"))
        feat["wave_length_bars"] = wave_n
        feat["wave_duration_minutes"] = wave_dur if wave_dur is not None else (
            float(wave_n) * BAR_MIN if wave_n is not None else None
        )
        feat["eff_quantile_meta"] = wave_eff
        feat["db_stoch_k"] = safe_float(r.get("stoch_k"))

        # --- Group B: extension / distance to mean ---
        atr = safe_float(row["atr"])
        close = float(row["close"])
        feat["atr"] = atr
        feat["atr_pct"] = safe_float(row["atr_pct"])
        feat["candle_range_atr"] = safe_float(row["range_atr"])
        if i >= 1:
            feat["prev_candle_range_atr"] = safe_float(df.iloc[i - 1]["range_atr"])
        else:
            feat["prev_candle_range_atr"] = None

        def move_atr(nbar: int) -> float | None:
            if i < nbar or not atr or atr <= 0:
                return None
            c0 = float(df.iloc[i - nbar]["close"])
            return (close - c0) / atr

        feat["move_3bar_atr"] = move_atr(3)
        feat["move_5bar_atr"] = move_atr(5)
        # direction-normalized extension into fade (positive = more stretched into fade)
        if side == "LONG":
            feat["extension_3bar_atr"] = (-feat["move_3bar_atr"]) if feat["move_3bar_atr"] is not None else None
            feat["extension_5bar_atr"] = (-feat["move_5bar_atr"]) if feat["move_5bar_atr"] is not None else None
        else:
            feat["extension_3bar_atr"] = feat["move_3bar_atr"]
            feat["extension_5bar_atr"] = feat["move_5bar_atr"]

        for span in (9, 20, 59, 200, 100, 400):
            col = f"ema{span}"
            if col not in df.columns:
                continue
            ema = safe_float(row[col])
            feat[f"dist_ema{span}_pct"] = ((close / ema - 1.0) * 100.0) if ema and ema > 0 else None
            feat[f"dist_ema{span}_atr"] = ((close - ema) / atr) if ema is not None and atr and atr > 0 else None
            # fade-aligned: LONG wants price below EMA (positive = below)
            if feat[f"dist_ema{span}_pct"] is not None:
                rawp = feat[f"dist_ema{span}_pct"]
                feat[f"ext_below_ema{span}_pct"] = (-rawp if side == "LONG" else rawp)

        # --- Group C: momentum ---
        def ret_n(nbar: int) -> float | None:
            if i < nbar:
                return None
            c0 = float(df.iloc[i - nbar]["close"])
            return (close / c0 - 1.0) * 100.0 if c0 else None

        for nbar in (1, 2, 3, 5):
            feat[f"ret_{nbar}_bar"] = ret_n(nbar)
            feat[f"aligned_move_{nbar}"] = (
                aligned_ret(side, float(df.iloc[i - nbar]["close"]), close) if i >= nbar else None
            )
        feat["momentum_regime"] = momentum_regime(
            feat.get("aligned_move_1"), feat.get("aligned_move_3"), feat.get("aligned_move_5")
        )
        a1, a3 = feat.get("aligned_move_1"), feat.get("aligned_move_3")
        feat["momentum_change"] = (a1 - (a3 / 3.0)) if a1 is not None and a3 is not None else None

        # --- Group D: candle structure ---
        cs = candle_struct(df, i)
        for k2, v in cs.items():
            feat[f"sig_{k2}"] = v
        if i >= 1:
            cs1 = candle_struct(df, i - 1)
            feat["prev_body_pct"] = cs1.get("body_pct_of_range")
            feat["prev_bearish"] = cs1.get("bearish_candle")
            feat["prev_bullish"] = cs1.get("bullish_candle")
            feat["prev_lower_wick_pct"] = cs1.get("lower_wick_pct")
            feat["prev_upper_wick_pct"] = cs1.get("upper_wick_pct")
            feat["prev_range_atr"] = cs1.get("range_atr")
        # consecutive same direction
        cons = 0
        if i >= 0:
            bull = close > float(row["open"])
            for back in range(0, min(i, 10) + 1):
                rr = df.iloc[i - back]
                b = float(rr["close"]) > float(rr["open"])
                if b == bull:
                    cons += 1
                else:
                    break
        feat["consecutive_same_direction_bars"] = cons
        # largest range last 3/5
        for nbar in (3, 5):
            if i >= nbar - 1:
                sl = df.iloc[i - nbar + 1 : i + 1]["range_atr"].astype(float)
                feat[f"largest_range_last_{nbar}"] = safe_float(sl.max())
            else:
                feat[f"largest_range_last_{nbar}"] = None

        # side-specific candle flags
        if side == "LONG":
            feat["rejection_wick"] = bool((cs.get("lower_wick_pct") or 0) >= 40)
            feat["close_off_extreme"] = bool((cs.get("close_position_in_range") or 0) >= 0.4)
            feat["full_body_into_fade"] = bool(
                (feat.get("prev_bearish") is True)
                and (feat.get("prev_body_pct") or 0) >= 70
                and (feat.get("prev_range_atr") or 0) >= 1.0
            )
        else:
            feat["rejection_wick"] = bool((cs.get("upper_wick_pct") or 0) >= 40)
            feat["close_off_extreme"] = bool((cs.get("close_position_in_range") or 1) <= 0.6)
            feat["full_body_into_fade"] = bool(
                (feat.get("prev_bullish") is True)
                and (feat.get("prev_body_pct") or 0) >= 70
                and (feat.get("prev_range_atr") or 0) >= 1.0
            )

        # --- Group E: simple reversal evidence ---
        feat["inside_bar"] = False
        feat["engulfing"] = False
        feat["swing_hl_lh"] = False
        if i >= 1:
            p = df.iloc[i - 1]
            feat["inside_bar"] = bool(
                float(row["high"]) <= float(p["high"]) and float(row["low"]) >= float(p["low"])
            )
            if side == "LONG":
                feat["engulfing"] = bool(
                    float(row["close"]) > float(row["open"])
                    and float(row["open"]) <= float(p["close"])
                    and float(row["close"]) >= float(p["open"])
                    and float(p["close"]) < float(p["open"])
                )
                if i >= 2:
                    feat["swing_hl_lh"] = bool(float(row["low"]) > float(df.iloc[i - 2]["low"]))
            else:
                feat["engulfing"] = bool(
                    float(row["close"]) < float(row["open"])
                    and float(row["open"]) >= float(p["close"])
                    and float(row["close"]) <= float(p["open"])
                    and float(p["close"]) > float(p["open"])
                )
                if i >= 2:
                    feat["swing_hl_lh"] = bool(float(row["high"]) < float(df.iloc[i - 2]["high"]))
        feat["reversal_evidence_count"] = int(
            sum(
                bool(feat.get(x))
                for x in ("rejection_wick", "close_off_extreme", "inside_bar", "engulfing", "swing_hl_lh")
            )
        )
        feat["no_reversal_evidence"] = feat["reversal_evidence_count"] == 0

        # --- Group F: volatility ---
        feat["atr_vs_med"] = safe_float(row["atr_vs_med"])
        feat["atr_pctile_50"] = safe_float(row["atr_pctile_50"])
        feat["range_pctile_50"] = safe_float(row["range_pctile_50"])
        feat["vol_expanding"] = bool((feat["atr_vs_med"] or 1.0) >= 1.25)
        feat["vol_contracting"] = bool((feat["atr_vs_med"] or 1.0) <= 0.85)

        # --- Group G: repeat context ---
        hist = history_by_sym[sym]
        def mins_since(pred) -> float | None:
            for h in reversed(hist):
                if pred(h):
                    return (t0 - h["ts"]).total_seconds() / 60.0
            return None

        feat["mins_since_prev_tier_a"] = mins_since(lambda h: True)
        feat["mins_since_prev_same_side"] = mins_since(lambda h: h["side"] == side)
        feat["mins_since_prev_opp_side"] = mins_since(lambda h: h["side"] != side)
        feat["tier_a_last_1h"] = sum(1 for h in hist if (t0 - h["ts"]) <= timedelta(hours=1))
        feat["tier_a_last_4h"] = sum(1 for h in hist if (t0 - h["ts"]) <= timedelta(hours=4))
        feat["repeat_same_side_4h"] = bool(
            feat["mins_since_prev_same_side"] is not None and feat["mins_since_prev_same_side"] <= 240
        )

        # Root-cause flags (descriptive)
        feat["RC_MOMENTUM_STILL_ACCELERATING"] = feat["momentum_regime"] == "ACCELERATING"
        feat["RC_INSUFFICIENT_PRICE_EXTENSION"] = bool(
            (feat.get("extension_5bar_atr") is not None) and feat["extension_5bar_atr"] < 0.5
        )
        feat["RC_VOLATILITY_EXPANSION"] = feat["vol_expanding"]
        feat["RC_NO_REVERSAL_EVIDENCE"] = feat["no_reversal_evidence"]
        feat["RC_EXTREME_EXTENSION_CONTINUATION"] = bool(
            (feat.get("extension_5bar_atr") is not None)
            and feat["extension_5bar_atr"] >= 2.5
            and feat["momentum_regime"] == "ACCELERATING"
        )
        feat["RC_REPEAT_SIGNAL_CONTEXT"] = feat["repeat_same_side_4h"]

        details.append(feat)
        history_by_sym[sym].append({"ts": t0, "side": side, "sid": sid})

    # --- Baseline guard ---
    wins = [d for d in details if d["result"] == "WIN"]
    losses = [d for d in details if d["result"] == "LOSS"]
    closed = wins + losses
    pnls = [float(d["pnl"]) for d in details if d.get("pnl") is not None]
    n = len(details)
    wr = (100.0 * len(wins) / len(closed)) if closed else None
    net = float(sum(pnls) - FEE_PCT * len(pnls)) if pnls else 0.0
    print(
        f"BASELINE n={n} wins={len(wins)} losses={len(losses)} open={n-len(closed)} wr={wr} net={net}",
        flush=True,
    )
    print(f"lookahead_violations={LOOKAHEAD_VIOLATIONS}", flush=True)

    if (
        n != EXPECT_N
        or len(losses) != EXPECT_LOSSES
        or wr is None
        or abs(wr - EXPECT_WR) > TOL_WR
        or abs(net - EXPECT_NET) > TOL_NET
    ):
        msg = {
            "error": "BASELINE_NOT_REPRODUCED",
            "got": {"n": n, "wins": len(wins), "losses": len(losses), "wr": wr, "net": net},
            "expected": {
                "n": EXPECT_N,
                "losses": EXPECT_LOSSES,
                "wr": EXPECT_WR,
                "net": EXPECT_NET,
                "note": "Prior audits: wins=113 (+4 OPEN). User text said wins=117 — using WR/Net/n/losses gate.",
            },
        }
        (OUT / "STOP_baseline_mismatch.json").write_text(json.dumps(msg, indent=2) + "\n")
        print("STOP", msg, flush=True)
        return 2

    write_csv(OUT / "signal_feature_detail.csv", details)

    # Continuous features for comparison
    CONT = [
        "stoch_k",
        "stoch_extremeness",
        "distance_from_20_or_80",
        "distance_k_d",
        "stoch_slope_1",
        "stoch_slope_2",
        "stoch_slope_3",
        "stoch_acceleration",
        "bars_since_stoch_extreme",
        "bars_since_kd_cross",
        "wave_length_bars",
        "wave_duration_minutes",
        "wave_directional_efficiency",
        "wave_price_move_pct",
        "atr_pct",
        "atr_vs_med",
        "atr_pctile_50",
        "range_pctile_50",
        "candle_range_atr",
        "prev_candle_range_atr",
        "extension_3bar_atr",
        "extension_5bar_atr",
        "ext_below_ema9_pct",
        "ext_below_ema20_pct",
        "ext_below_ema59_pct",
        "ext_below_ema200_pct",
        "dist_ema20_atr",
        "aligned_move_1",
        "aligned_move_3",
        "aligned_move_5",
        "momentum_change",
        "ret_1_bar",
        "ret_3_bar",
        "ret_5_bar",
        "sig_body_pct_of_range",
        "sig_upper_wick_pct",
        "sig_lower_wick_pct",
        "sig_body_atr",
        "sig_range_atr",
        "sig_close_position_in_range",
        "prev_body_pct",
        "prev_lower_wick_pct",
        "prev_upper_wick_pct",
        "consecutive_same_direction_bars",
        "largest_range_last_3",
        "largest_range_last_5",
        "reversal_evidence_count",
        "mins_since_prev_tier_a",
        "mins_since_prev_same_side",
        "tier_a_last_1h",
        "tier_a_last_4h",
    ]

    def collect(rows: list[dict], feat: str) -> list[float]:
        out = []
        for r in rows:
            v = safe_float(r.get(feat))
            if v is not None:
                out.append(v)
        return out

    def feat_table(subset_w: list, subset_l: list, scope: str) -> list[dict]:
        rows = []
        for f in CONT:
            s = summarize_feat(collect(subset_w, f), collect(subset_l, f), f)
            s["scope"] = scope
            rows.append(s)
        return rows

    wl_all = feat_table(wins, losses, "ALL")
    wl_long = feat_table(
        [d for d in wins if d["direction"] == "LONG"],
        [d for d in losses if d["direction"] == "LONG"],
        "LONG",
    )
    wl_short = feat_table(
        [d for d in wins if d["direction"] == "SHORT"],
        [d for d in losses if d["direction"] == "SHORT"],
        "SHORT",
    )
    wl_rows = wl_all + wl_long + wl_short
    write_csv(OUT / "winner_loser_feature_summary.csv", wl_rows)

    # Direction split binary features
    BIN = [
        "rejection_wick",
        "close_off_extreme",
        "full_body_into_fade",
        "inside_bar",
        "engulfing",
        "swing_hl_lh",
        "no_reversal_evidence",
        "vol_expanding",
        "vol_contracting",
        "repeat_same_side_4h",
        "RC_MOMENTUM_STILL_ACCELERATING",
        "RC_INSUFFICIENT_PRICE_EXTENSION",
        "RC_VOLATILITY_EXPANSION",
        "RC_NO_REVERSAL_EVIDENCE",
        "RC_EXTREME_EXTENSION_CONTINUATION",
        "RC_REPEAT_SIGNAL_CONTEXT",
    ]
    dir_rows = []
    for scope, sub in (
        ("ALL", details),
        ("LONG", [d for d in details if d["direction"] == "LONG"]),
        ("SHORT", [d for d in details if d["direction"] == "SHORT"]),
    ):
        sw = [d for d in sub if d["result"] == "WIN"]
        sl = [d for d in sub if d["result"] == "LOSS"]
        for f in BIN + ["momentum_regime"]:
            if f == "momentum_regime":
                for val in ("ACCELERATING", "STABLE", "SLOWING", "UNKNOWN"):
                    lw = sum(1 for d in sl if d.get(f) == val)
                    ww = sum(1 for d in sw if d.get(f) == val)
                    nL, nW = len(sl), len(sw)
                    lr = (lw / nL) if nL else None
                    wr_ = (ww / nW) if nW else None
                    rr = (lr / wr_) if lr is not None and wr_ and wr_ > 0 else None
                    dir_rows.append(
                        {
                            "scope": scope,
                            "feature": f"{f}={val}",
                            "loser_count": lw,
                            "winner_count": ww,
                            "loser_rate": lr,
                            "winner_rate": wr_,
                            "relative_risk": rr,
                            "n_losers": nL,
                            "n_winners": nW,
                        }
                    )
                continue
            lw = sum(1 for d in sl if d.get(f))
            ww = sum(1 for d in sw if d.get(f))
            nL, nW = len(sl), len(sw)
            lr = (lw / nL) if nL else None
            wr_ = (ww / nW) if nW else None
            rr = (lr / wr_) if lr is not None and wr_ and wr_ > 0 else None
            dir_rows.append(
                {
                    "scope": scope,
                    "feature": f,
                    "loser_count": lw,
                    "winner_count": ww,
                    "loser_rate": lr,
                    "winner_rate": wr_,
                    "relative_risk": rr,
                    "n_losers": nL,
                    "n_winners": nW,
                }
            )
    write_csv(OUT / "direction_split_summary.csv", dir_rows)

    # Quantiles for top continuous by assoc_strength
    wl_all_sorted = sorted(
        [r for r in wl_all if r.get("assoc_strength") is not None],
        key=lambda r: -(r["assoc_strength"] or 0),
    )
    top_for_q = [r["feature"] for r in wl_all_sorted[:12]]
    q_rows = []
    closed_rows = [d for d in details if d["result"] in ("WIN", "LOSS")]

    def outcome_stats(rows: list[dict]) -> dict:
        w = sum(1 for r in rows if r["result"] == "WIN")
        l = sum(1 for r in rows if r["result"] == "LOSS")
        c = w + l
        pn = [float(r["pnl"]) for r in rows if r.get("pnl") is not None]
        maes = [float(r["mae"]) for r in rows if r.get("mae") is not None]
        mfes = [float(r["mfe"]) for r in rows if r.get("mfe") is not None]
        return {
            "trades": len(rows),
            "winrate": (100.0 * w / c) if c else None,
            "net": float(sum(pn) - FEE_PCT * len(pn)) if pn else 0.0,
            "sl_rate": (100.0 * l / c) if c else None,
            "median_MAE": float(np.median(maes)) if maes else None,
            "median_MFE": float(np.median(mfes)) if mfes else None,
        }

    for f in top_for_q:
        vals = [(safe_float(d.get(f)), d) for d in closed_rows]
        vals = [(v, d) for v, d in vals if v is not None]
        if len(vals) < 25:
            continue
        vs = np.array([v for v, _ in vals], dtype=float)
        edges = np.percentile(vs, [0, 20, 40, 60, 80, 100])
        # unique edges
        edges = np.unique(edges)
        if len(edges) < 3:
            continue
        for qi in range(len(edges) - 1):
            lo, hi = edges[qi], edges[qi + 1]
            if qi < len(edges) - 2:
                bucket = [d for v, d in vals if lo <= v < hi]
            else:
                bucket = [d for v, d in vals if lo <= v <= hi]
            st = outcome_stats(bucket)
            q_rows.append({"feature": f, "quantile": f"Q{qi+1}", "lo": float(lo), "hi": float(hi), **st})
    write_csv(OUT / "feature_quantiles.csv", q_rows)

    # Early DD feature summary
    ed_rows = []
    for label, flag in (("EARLY_DD_2PCT", "early_dd_2pct"), ("EARLY_DD_3PCT", "early_dd_3pct")):
        pos = [d for d in closed_rows if d.get(flag)]
        neg = [d for d in closed_rows if not d.get(flag)]
        for f in CONT[:40]:
            s = summarize_feat(collect(neg, f), collect(pos, f), f)  # treat "no DD" like win for AUC naming
            # Recompute: higher favors NO early DD
            s["scope"] = label
            s["group_pos"] = "HAS_EARLY_DD"
            s["group_neg"] = "NO_EARLY_DD"
            # overwrite with clearer fields
            ed_rows.append(
                {
                    "label": label,
                    "feature": f,
                    "no_dd_median": s["winner_median"],
                    "dd_median": s["loser_median"],
                    "cliffs_delta_no_vs_dd": s["cliffs_delta_win_vs_loss"],
                    "auc_higher_favors_no_dd": s["auc_higher_favors_win"],
                    "assoc_strength": s["assoc_strength"],
                    "n_no_dd": s["winner_count"],
                    "n_dd": s["loser_count"],
                }
            )
    write_csv(OUT / "early_dd_feature_summary.csv", ed_rows)

    # Loser root causes with winner comparison
    rc_rows = []
    for f in [
        "RC_MOMENTUM_STILL_ACCELERATING",
        "RC_INSUFFICIENT_PRICE_EXTENSION",
        "RC_VOLATILITY_EXPANSION",
        "RC_NO_REVERSAL_EVIDENCE",
        "RC_EXTREME_EXTENSION_CONTINUATION",
        "RC_REPEAT_SIGNAL_CONTEXT",
    ]:
        lw = sum(1 for d in losses if d.get(f))
        ww = sum(1 for d in wins if d.get(f))
        lr = lw / len(losses) if losses else None
        wr_ = ww / len(wins) if wins else None
        rr = (lr / wr_) if lr is not None and wr_ and wr_ > 0 else None
        rc_rows.append(
            {
                "cause": f.replace("RC_", ""),
                "loser_count": lw,
                "winner_count": ww,
                "loser_rate": lr,
                "winner_rate": wr_,
                "relative_risk": rr,
                "interesting": bool(rr is not None and rr >= 1.25 and lw >= 8),
            }
        )
    # unexplained: no RC flags
    def any_rc(d):
        return any(
            d.get(x)
            for x in (
                "RC_MOMENTUM_STILL_ACCELERATING",
                "RC_INSUFFICIENT_PRICE_EXTENSION",
                "RC_VOLATILITY_EXPANSION",
                "RC_NO_REVERSAL_EVIDENCE",
                "RC_EXTREME_EXTENSION_CONTINUATION",
                "RC_REPEAT_SIGNAL_CONTEXT",
            )
        )

    uL = sum(1 for d in losses if not any_rc(d))
    uW = sum(1 for d in wins if not any_rc(d))
    rc_rows.append(
        {
            "cause": "UNEXPLAINED",
            "loser_count": uL,
            "winner_count": uW,
            "loser_rate": uL / len(losses) if losses else None,
            "winner_rate": uW / len(wins) if wins else None,
            "relative_risk": None,
            "interesting": False,
        }
    )
    write_csv(OUT / "loser_root_causes.csv", rc_rows)

    # Stability half splits
    closed_sorted = sorted(closed_rows, key=lambda d: d["entry_ts"] or "")
    mid = len(closed_sorted) // 2
    halves = {"first_half": closed_sorted[:mid], "second_half": closed_sorted[mid:]}
    stab_rows = []
    top5 = [r["feature"] for r in wl_all_sorted[:8]]
    for f in top5:
        for half, rows_h in halves.items():
            hw = [d for d in rows_h if d["result"] == "WIN"]
            hl = [d for d in rows_h if d["result"] == "LOSS"]
            s = summarize_feat(collect(hw, f), collect(hl, f), f)
            stab_rows.append(
                {
                    "feature": f,
                    "half": half,
                    "winner_median": s["winner_median"],
                    "loser_median": s["loser_median"],
                    "median_gap": s["median_gap_win_minus_loss"],
                    "auc": s["auc_higher_favors_win"],
                    "cliffs_delta": s["cliffs_delta_win_vs_loss"],
                    "n_win": s["winner_count"],
                    "n_loss": s["loser_count"],
                }
            )
        g1 = next(r for r in stab_rows if r["feature"] == f and r["half"] == "first_half")
        g2 = next(r for r in stab_rows if r["feature"] == f and r["half"] == "second_half")
        # stable if gap same sign
        stable = None
        if g1["median_gap"] is not None and g2["median_gap"] is not None:
            stable = (g1["median_gap"] * g2["median_gap"]) > 0
        stab_rows.append(
            {
                "feature": f,
                "half": "STABLE_SAME_SIGN",
                "winner_median": None,
                "loser_median": None,
                "median_gap": None,
                "auc": None,
                "cliffs_delta": None,
                "n_win": None,
                "n_loss": None,
                "stable_same_sign": stable,
            }
        )
    write_csv(OUT / "feature_stability.csv", stab_rows)

    # Identify clear candidates for filters (binary or quantile extremes)
    interesting_rc = [r for r in rc_rows if r.get("interesting")]
    # continuous: strong assoc + stable
    stable_feats = set()
    for f in top5:
        row = next(
            (r for r in stab_rows if r["feature"] == f and r["half"] == "STABLE_SAME_SIGN"),
            None,
        )
        if row and row.get("stable_same_sign"):
            stable_feats.add(f)

    strong = [
        r
        for r in wl_all_sorted
        if (r.get("assoc_strength") or 0) >= 0.12
        and r["feature"] in stable_feats
        and r["winner_count"] >= 40
        and r["loser_count"] >= 25
    ][:5]

    # Direction dependence
    long_top = sorted(
        [r for r in wl_long if r.get("assoc_strength") is not None],
        key=lambda r: -(r["assoc_strength"] or 0),
    )[:5]
    short_top = sorted(
        [r for r in wl_short if r.get("assoc_strength") is not None],
        key=lambda r: -(r["assoc_strength"] or 0),
    )[:5]

    def top_ed(label: str) -> list[dict]:
        rows = [r for r in ed_rows if r["label"] == label and r.get("assoc_strength") is not None]
        return sorted(rows, key=lambda r: -(r["assoc_strength"] or 0))[:5]

    ed2_top = top_ed("EARLY_DD_2PCT")
    ed3_top = top_ed("EARLY_DD_3PCT")

    # Simple combinations + counterfactual (max 3) only if clear
    cf_rows = []
    baseline_net = net
    baseline_wr = wr

    def cf_eval(name: str, block_pred) -> dict:
        kept = [d for d in closed_rows if not block_pred(d)]
        blocked = [d for d in closed_rows if block_pred(d)]
        st = outcome_stats(kept)
        w_rem = sum(1 for d in blocked if d["result"] == "WIN")
        l_rem = sum(1 for d in blocked if d["result"] == "LOSS")
        gp = sum(float(d["pnl"]) for d in kept if d.get("pnl") is not None and float(d["pnl"]) > 0)
        gl = sum(-float(d["pnl"]) for d in kept if d.get("pnl") is not None and float(d["pnl"]) < 0)
        pf = (gp / gl) if gl > 0 else None
        return {
            "variant": name,
            "trades_kept": st["trades"],
            "coverage_pct": (100.0 * st["trades"] / len(closed_rows)) if closed_rows else None,
            "winners_removed": w_rem,
            "losers_removed": l_rem,
            "winrate": st["winrate"],
            "net": st["net"],
            "profit_factor": pf,
            "sl_rate": st["sl_rate"],
            "median_MAE": st["median_MAE"],
            "net_delta": st["net"] - baseline_net,
        }

    cf_rows.append(
        {
            "variant": "BASELINE_IMMEDIATE",
            "trades_kept": len(closed_rows),
            "coverage_pct": 100.0,
            "winners_removed": 0,
            "losers_removed": 0,
            "winrate": baseline_wr,
            "net": baseline_net,
            "profit_factor": None,
            "sl_rate": 100.0 * len(losses) / len(closed_rows),
            "median_MAE": float(np.median([float(d["mae"]) for d in closed_rows if d.get("mae") is not None])),
            "net_delta": 0.0,
        }
    )

    # Candidates from interesting RC with RR>=1.25
    filter_preds: list[tuple[str, Any]] = []
    for r in interesting_rc[:3]:
        cause = r["cause"]
        key = f"RC_{cause}"
        filter_preds.append((f"BLOCK_{cause}", lambda d, k=key: bool(d.get(k))))

    # Add combination if two interesting
    if len(interesting_rc) >= 2:
        k1 = f"RC_{interesting_rc[0]['cause']}"
        k2 = f"RC_{interesting_rc[1]['cause']}"
        filter_preds.append(
            (
                f"BLOCK_{interesting_rc[0]['cause']}_AND_{interesting_rc[1]['cause']}",
                lambda d, a=k1, b=k2: bool(d.get(a)) and bool(d.get(b)),
            )
        )

    # Quantile extreme filter if one strong continuous: block loser-favoring extreme Q
    # e.g. if losers have higher extension → block top quintile of that feature when gap favors
    for s in strong[:2]:
        f = s["feature"]
        # if loser median > winner median, block high values; else block low
        gap = s.get("median_gap_win_minus_loss")
        vals = [safe_float(d.get(f)) for d in closed_rows]
        vals = [v for v in vals if v is not None]
        if len(vals) < 40 or gap is None:
            continue
        thr = float(np.percentile(vals, 80 if gap < 0 else 20))
        if gap < 0:
            filter_preds.append((f"BLOCK_HIGH_{f}", lambda d, feat=f, t=thr: (safe_float(d.get(feat)) or -1e9) >= t))
        else:
            filter_preds.append((f"BLOCK_LOW_{f}", lambda d, feat=f, t=thr: (safe_float(d.get(feat)) or 1e9) <= t))

    # Cap at 3 filters beyond baseline
    filter_preds = filter_preds[:3]
    for name, pred in filter_preds:
        cf_rows.append(cf_eval(name, pred))
    write_csv(OUT / "counterfactual_filters.csv", cf_rows)

    best_cf = None
    for r in cf_rows:
        if r["variant"] == "BASELINE_IMMEDIATE":
            continue
        # Require clear loser skew, not near-coin-flip removals
        w_rem = r.get("winners_removed") or 0
        l_rem = r.get("losers_removed") or 0
        if (r.get("net_delta") or 0) > 1.0 and l_rem >= w_rem + 5 and l_rem >= max(8, int(1.3 * w_rem)):
            if best_cf is None or (r["net_delta"] > best_cf["net_delta"]):
                best_cf = r

    # Detect if a high-scoring ALL feature is mostly one-sided artifact
    short_only_ext = False
    if closed_rows:
        vals = [safe_float(d.get("dist_ema20_atr")) for d in closed_rows]
        vals_ok = [v for v in vals if v is not None]
        if vals_ok:
            thr80 = float(np.percentile(vals_ok, 80))
            blocked = [
                d
                for d in closed_rows
                if safe_float(d.get("dist_ema20_atr")) is not None
                and safe_float(d.get("dist_ema20_atr")) >= thr80
            ]
            if blocked and all(d["direction"] == "SHORT" for d in blocked):
                short_only_ext = True

    # Best quantile pattern: largest WR spread across quantiles for a feature
    best_q_pattern = None
    by_f: dict[str, list] = defaultdict(list)
    for r in q_rows:
        by_f[r["feature"]].append(r)
    for f, rows_q in by_f.items():
        wrs = [r["winrate"] for r in rows_q if r.get("winrate") is not None]
        if len(wrs) < 3:
            continue
        spread = max(wrs) - min(wrs)
        mono_up = all(wrs[i] <= wrs[i + 1] + 1e-9 for i in range(len(wrs) - 1))
        mono_dn = all(wrs[i] >= wrs[i + 1] - 1e-9 for i in range(len(wrs) - 1))
        if best_q_pattern is None or spread > best_q_pattern["wr_spread"]:
            best_q_pattern = {
                "feature": f,
                "wr_spread": spread,
                "monotonic": mono_up or mono_dn,
                "direction": "up" if mono_up else ("down" if mono_dn else "non_monotonic"),
                "quantiles": rows_q,
            }

    # Primary decision
    n_stable_strong = len(strong)
    direction_dep = bool(short_only_ext)
    if long_top and short_top:
        lset = {r["feature"] for r in long_top[:3]}
        sset = {r["feature"] for r in short_top[:3]}
        if not (lset & sset) and (long_top[0]["assoc_strength"] or 0) >= 0.15:
            direction_dep = True
    for f in set(r["feature"] for r in wl_long) & set(r["feature"] for r in wl_short):
        L = next(r for r in wl_long if r["feature"] == f)
        S = next(r for r in wl_short if r["feature"] == f)
        if (
            L.get("median_gap_win_minus_loss") is not None
            and S.get("median_gap_win_minus_loss") is not None
            and L["median_gap_win_minus_loss"] * S["median_gap_win_minus_loss"] < 0
            and abs(L["median_gap_win_minus_loss"]) > 0.05
            and abs(S["median_gap_win_minus_loss"]) > 0.05
            and (L.get("assoc_strength") or 0) >= 0.1
            and (S.get("assoc_strength") or 0) >= 0.1
        ):
            direction_dep = True
            break

    filters_hurt = (
        len(filter_preds) > 0
        and all((r.get("net_delta") or 0) <= 0 for r in cf_rows if r["variant"] != "BASELINE_IMMEDIATE")
    )
    filters_mixed = any(
        (r.get("net_delta") or 0) > 0 and (r.get("losers_removed") or 0) <= (r.get("winners_removed") or 0) + 2
        for r in cf_rows
        if r["variant"] != "BASELINE_IMMEDIATE"
    )

    max_assoc = wl_all_sorted[0]["assoc_strength"] if wl_all_sorted else 0
    max_rr = max((r.get("relative_risk") or 0) for r in interesting_rc) if interesting_rc else 0

    if n < 80:
        primary = "INSUFFICIENT_SAMPLE"
        recommendation = "NEEDS_MORE_HISTORY"
    elif best_cf and (best_cf["net_delta"] or 0) > 3 and n_stable_strong >= 1 and not short_only_ext:
        primary = "CLEAR_PRE_ENTRY_FAILURE_CONTEXT_FOUND"
        recommendation = "ONE_CONTEXT_FILTER_WORTH_FURTHER_TESTING"
    elif direction_dep and (max_assoc or 0) >= 0.12:
        primary = "FAILURES_DIRECTION_DEPENDENT"
        # SHORT-only stretch filter can still be research-worthy if ΔNet>0 but not "clear"
        if any((r.get("net_delta") or 0) > 2 for r in cf_rows if r["variant"] != "BASELINE_IMMEDIATE"):
            recommendation = "ONE_CONTEXT_FILTER_WORTH_FURTHER_TESTING"
        else:
            recommendation = "KEEP_BASELINE"
    elif filters_hurt and (max_assoc or 0) >= 0.1:
        primary = "SIMPLE_FILTERS_HURT_EDGE"
        recommendation = "KEEP_BASELINE"
    elif filters_mixed and (max_assoc or 0) >= 0.1:
        primary = "SIGNAL_QUALITY_ONLY_WEAKLY_PREDICTIVE"
        recommendation = "KEEP_BASELINE"
    elif max_assoc is not None and max_assoc >= 0.08 and (n_stable_strong >= 1 or max_rr >= 1.25):
        primary = "SIGNAL_QUALITY_ONLY_WEAKLY_PREDICTIVE"
        recommendation = "KEEP_BASELINE"
    else:
        primary = "NO_ROBUST_PRE_ENTRY_FAILURE_SIGNAL"
        recommendation = "KEEP_BASELINE"

    top10 = wl_all_sorted[:10]

    payload = {
        "primary_decision": primary,
        "recommendation": recommendation,
        "baseline": {
            "n": n,
            "wins": len(wins),
            "losses": len(losses),
            "open": n - len(closed),
            "winrate": wr,
            "net": net,
            "note": "User text said wins=117; reproduced closed wins match prior audits (113 + OPEN).",
        },
        "lookahead_violations": LOOKAHEAD_VIOLATIONS,
        "feature_docs": {
            "signal_bar": "last COMPLETE 15m bar with available_at <= entry_ts",
            "emas": "strategy EMA9/20/100/400 via attach_indicators; audit also EMA59/200",
            "atr": "Wilder ATR(14) on 15m — research-only (not in freeze strategy)",
            "vwap": "NOT used (not canonical in repo)",
            "wave": "build_waves_from_ohlcv / metadata n_bars",
            "directional_rule_momentum": "aligned_move = impulse into fade; ACCELERATING if 1-bar > 1.25× 3-bar/3",
        },
        "top10_features": top10,
        "top_long": long_top[:5],
        "top_short": short_top[:5],
        "top_early_dd2": ed2_top,
        "top_early_dd3": ed3_top,
        "loser_root_causes": rc_rows,
        "best_quantile_pattern": {
            "feature": best_q_pattern["feature"] if best_q_pattern else None,
            "wr_spread": best_q_pattern["wr_spread"] if best_q_pattern else None,
            "monotonic": best_q_pattern["monotonic"] if best_q_pattern else None,
            "direction": best_q_pattern["direction"] if best_q_pattern else None,
        },
        "stability_top": [r for r in stab_rows if r["half"] == "STABLE_SAME_SIGN"],
        "counterfactuals": cf_rows,
        "best_counterfactual": best_cf,
        "as_of": _iso(now),
    }
    (OUT / "summary.json").write_text(json.dumps(payload, indent=2, default=str) + "\n")

    def fmt(v: Any, nd: int = 3) -> str:
        if v is None:
            return "–"
        if isinstance(v, float):
            return f"{v:.{nd}f}"
        return str(v)

    lines = [
        "# AUDIT_BASELINE_WINNERS_VS_LOSERS_SIGNAL_QUALITY",
        "",
        f"as_of `{_iso(now)}` | lookahead_violations=**{LOOKAHEAD_VIOLATIONS}**",
        "",
        "## Primary Decision",
        "",
        f"`{primary}`",
        "",
        f"Recommendation: `{recommendation}`",
        "",
        "## Baseline",
        "",
        f"n={n} wins={len(wins)} losses={len(losses)} open={n-len(closed)} WR={fmt(wr,1)} Net={fmt(net,1)}",
        "",
        "## Top features (ALL)",
        "",
        "| Feature | Win Med | Loss Med | Cliff Δ | AUC | Strength |",
        "| ------- | ------: | -------: | ------: | --: | -------: |",
    ]
    for r in top10:
        lines.append(
            f"| {r['feature']} | {fmt(r['winner_median'])} | {fmt(r['loser_median'])} | "
            f"{fmt(r['cliffs_delta_win_vs_loss'])} | {fmt(r['auc_higher_favors_win'])} | {fmt(r['assoc_strength'])} |"
        )
    lines += ["", "## Root causes", ""]
    for r in rc_rows:
        lines.append(
            f"- {r['cause']}: L={r['loser_count']} W={r['winner_count']} RR={fmt(r.get('relative_risk'), 2)} "
            f"{'★' if r.get('interesting') else ''}"
        )
    lines += ["", "## Counterfactuals", ""]
    for r in cf_rows:
        lines.append(
            f"- {r['variant']}: kept={r['trades_kept']} WR={fmt(r.get('winrate'),1)} Net={fmt(r.get('net'),1)} "
            f"ΔNet={fmt(r.get('net_delta'),1)} rem W/L={r.get('winners_removed')}/{r.get('losers_removed')}"
        )
    lines += ["", "## Strategy Logic Changed", "", "`NO`", "", "## DB Changed", "", "`NO`", ""]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    print("PRIMARY", primary, flush=True)
    print("REC", recommendation, flush=True)
    if top10:
        print("top1", top10[0]["feature"], "str", top10[0]["assoc_strength"], flush=True)
    print("wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
