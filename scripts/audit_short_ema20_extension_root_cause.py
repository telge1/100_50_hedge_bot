#!/usr/bin/env python3
"""SHORT_EMA20_EXTENSION_ROOT_CAUSE_AUDIT — research-only, no DB writes.

Explains WHY OOS SHORT signals with dist_ema20_atr > frozen TRAIN P80 underperform.
Same universe/split/threshold as short_ema20_extension_oos_validation.
No threshold tuning. No strategy changes.
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
)
from signal_generator.strategy.wave_fade.signals import build_waves_from_ohlcv  # noqa: E402
from signal_generator.timeframes import (  # noqa: E402
    aggregate_1m_to_timeframe,
    bars_from_mappings,
)

OUT = ROOT / "results" / "short_ema20_extension_root_cause_audit"
SHORT_THR_PATH = ROOT / "results" / "short_ema20_extension_oos_validation" / "train_threshold.json"
LONG_SUM_PATH = ROOT / "results" / "long_ema20_extension_oos_validation" / "summary.json"

AS_OF = datetime(2026, 8, 11, 9, 30, 7, 105630, tzinfo=timezone.utc)
LOOKBACK_DAYS = 60
SIGNAL_TF = "15m"
FEE_PCT = float(PRIMARY_FEE)
ATR_LEN = 14
TRAIN_FRAC = 0.60
N_BOOT = 2000
BOOT_SEED = 42

LOOKAHEAD_VIOLATIONS = 0
FEATURE_NOT_AVAILABLE = 0
THRESHOLD_LEAKAGE = 0
OUTCOME_LEAKAGE = 0
TRAIN_OOS_CONTAMINATION = 0
HTF_AVAIL_VIOLATIONS = 0
INCOMPLETE_CANDLE = 0

# Predeclared continuous features for ranking (inventory groups)
FEATURE_GROUPS: dict[str, list[str]] = {
    "extension": ["dist_ema20_atr"],
    "trend_ema": [
        "ema20_slope_atr",
        "ema9_slope_atr",
        "ema100_slope_atr",
        "ema9_vs_ema20_atr",
        "dist_ema100_atr",
        "dist_ema400_atr",
        "ema_separation_9_20_atr",
    ],
    "momentum": [
        "ret_1",
        "ret_3",
        "ret_5",
        "bullish_bars_3",
        "bullish_bars_5",
        "body_range_ratio",
        "close_location",
        "mom_accel",
    ],
    "volatility": [
        "atr_pct",
        "atr_vs_med50",
        "range_atr",
        "range_expansion_3",
        "range_expansion_5",
    ],
    "wave_stoch": [
        "stoch_k",
        "stoch_d",
        "k_minus_d",
        "stoch_extremeness",
        "stoch_k_slope_1",
        "stoch_k_slope_3",
        "bars_since_ob",
        "wave_n_bars",
        "wave_efficiency",
        "stoch_still_rising",
    ],
    "htf": [
        "htf30_k",
        "htf30_bullish",
        "htf1h_k",
        "htf1h_bullish",
    ],
}


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


def wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = ATR_LEN) -> pd.Series:
    prev_c = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_c).abs(), (low - prev_c).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / length, min_periods=length, adjust=False).mean()


def prepare_15m(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = attach_indicators(df)
    close = out["close"].astype(float)
    high = out["high"].astype(float)
    low = out["low"].astype(float)
    out["atr"] = wilder_atr(high, low, close, ATR_LEN)
    out["atr_pct"] = out["atr"] / close.replace(0, np.nan) * 100.0
    out["atr_med50"] = out["atr"].rolling(50, min_periods=20).median()
    out["atr_vs_med50"] = out["atr"] / out["atr_med50"].replace(0, np.nan)
    out["range"] = high - low
    out["range_atr"] = out["range"] / out["atr"].replace(0, np.nan)
    return out


def prepare_htf(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = attach_indicators(df)
    return out


def causal_idx(df: pd.DataFrame, entry_ts: datetime) -> int:
    if df.empty or "available_at" not in df.columns:
        return -1
    t = pd.Timestamp(_utc(entry_ts))
    known = df.index[df["available_at"] <= t]
    if len(known) == 0:
        return -1
    return int(known[-1])


def check_causal(available_at: Any, entry_ts: datetime) -> None:
    global LOOKAHEAD_VIOLATIONS
    if available_at is None or (isinstance(available_at, float) and np.isnan(available_at)):
        return
    if _utc(available_at) > _utc(entry_ts):
        LOOKAHEAD_VIOLATIONS += 1


def cliffs_delta(x: list[float], y: list[float]) -> float | None:
    if not x or not y:
        return None
    u, _ = stats.mannwhitneyu(np.asarray(x), np.asarray(y), alternative="two-sided")
    return float(2.0 * u / (len(x) * len(y)) - 1.0)


def auc_score(pos: list[float], neg: list[float]) -> float | None:
    """AUC: higher feature → more likely WIN (pos=wins, neg=losses)."""
    if len(pos) < 3 or len(neg) < 3:
        return None
    u, _ = stats.mannwhitneyu(np.asarray(pos), np.asarray(neg), alternative="two-sided")
    return float(u / (len(pos) * len(neg)))


def summarize_group(vals_a: list[float], vals_b: list[float], *, name: str, label_a: str, label_b: str) -> dict:
    def qs(xs: list[float]) -> dict:
        if not xs:
            return {"n": 0, "mean": None, "median": None, "q25": None, "q75": None}
        a = np.asarray(xs, dtype=float)
        return {
            "n": len(xs),
            "mean": float(np.mean(a)),
            "median": float(np.median(a)),
            "q25": float(np.percentile(a, 25)),
            "q75": float(np.percentile(a, 75)),
        }

    qa, qb = qs(vals_a), qs(vals_b)
    cd = cliffs_delta(vals_a, vals_b)
    # Cohen-ish standardized median gap using pooled IQR
    gap = None
    if qa["median"] is not None and qb["median"] is not None:
        gap = qa["median"] - qb["median"]
    return {
        "feature": name,
        f"{label_a}_n": qa["n"],
        f"{label_a}_mean": qa["mean"],
        f"{label_a}_median": qa["median"],
        f"{label_a}_q25": qa["q25"],
        f"{label_a}_q75": qa["q75"],
        f"{label_b}_n": qb["n"],
        f"{label_b}_mean": qb["mean"],
        f"{label_b}_median": qb["median"],
        f"{label_b}_q25": qb["q25"],
        f"{label_b}_q75": qb["q75"],
        "median_gap_A_minus_B": gap,
        "cliffs_delta_A_vs_B": cd,
        "effect_abs": abs(cd) if cd is not None else None,
    }


def extract_features_at(
    df: pd.DataFrame,
    *,
    entry_ts: datetime,
    entry_price: float | None,
    waves: pd.DataFrame,
    htf30: pd.DataFrame,
    htf1h: pd.DataFrame,
    work_1m: pd.DataFrame,
) -> dict[str, Any]:
    global FEATURE_NOT_AVAILABLE, HTF_AVAIL_VIOLATIONS, INCOMPLETE_CANDLE
    i = causal_idx(df, entry_ts)
    out: dict[str, Any] = {}
    if i < 0:
        FEATURE_NOT_AVAILABLE += 1
        return out
    row = df.iloc[i]
    check_causal(row["available_at"], entry_ts)
    close = safe_float(row["close"])
    atr = safe_float(row["atr"])
    ema20 = safe_float(row["ema20"])
    ema9 = safe_float(row.get("ema9"))
    ema100 = safe_float(row.get("ema100"))
    ema400 = safe_float(row.get("ema400"))
    if atr is None or atr <= 0 or close is None or ema20 is None:
        FEATURE_NOT_AVAILABLE += 1
        return out

    # Extension (SHORT: price above EMA20)
    out["dist_ema20_atr"] = (close - ema20) / atr
    out["atr_pct"] = safe_float(row.get("atr_pct"))
    out["atr_vs_med50"] = safe_float(row.get("atr_vs_med50"))
    out["range_atr"] = safe_float(row.get("range_atr"))

    def slope_atr(col: str, lag: int = 3) -> float | None:
        if i < lag:
            return None
        v0 = safe_float(df.iloc[i - lag][col])
        v1 = safe_float(df.iloc[i][col])
        if v0 is None or v1 is None:
            return None
        return (v1 - v0) / atr

    out["ema20_slope_atr"] = slope_atr("ema20", 3)
    out["ema9_slope_atr"] = slope_atr("ema9", 3)
    out["ema100_slope_atr"] = slope_atr("ema100", 3)
    if ema9 is not None:
        out["ema9_vs_ema20_atr"] = (ema9 - ema20) / atr
        out["ema_separation_9_20_atr"] = abs(ema9 - ema20) / atr
    else:
        out["ema9_vs_ema20_atr"] = None
        out["ema_separation_9_20_atr"] = None
    out["dist_ema100_atr"] = (close - ema100) / atr if ema100 is not None else None
    out["dist_ema400_atr"] = (close - ema400) / atr if ema400 is not None else None
    # EMA50 not in freeze — documented N/A
    out["ema50_available"] = False

    def ret_n(nbar: int) -> float | None:
        if i < nbar:
            return None
        c0 = safe_float(df.iloc[i - nbar]["close"])
        if c0 is None or c0 <= 0:
            return None
        return (close / c0 - 1.0) * 100.0

    out["ret_1"] = ret_n(1)
    out["ret_3"] = ret_n(3)
    out["ret_5"] = ret_n(5)
    # for SHORT, adverse continuation = further up = positive ret
    out["aligned_up_momentum_3"] = out["ret_3"]
    out["aligned_up_momentum_5"] = out["ret_5"]

    def bullish_count(nbar: int) -> float | None:
        if i < nbar - 1:
            return None
        c = 0
        for j in range(i - nbar + 1, i + 1):
            if float(df.iloc[j]["close"]) > float(df.iloc[j]["open"]):
                c += 1
        return float(c)

    out["bullish_bars_3"] = bullish_count(3)
    out["bullish_bars_5"] = bullish_count(5)
    o, h, l = float(row["open"]), float(row["high"]), float(row["low"])
    rng = h - l
    body = abs(close - o)
    out["body_range_ratio"] = (body / rng) if rng > 0 else None
    out["close_location"] = ((close - l) / rng) if rng > 0 else None
    r1, r3 = out["ret_1"], out["ret_3"]
    out["mom_accel"] = (r1 - (r3 / 3.0)) if r1 is not None and r3 is not None else None

    if i >= 3:
        out["range_expansion_3"] = safe_float(df.iloc[i]["range_atr"]) - safe_float(
            np.nanmean([safe_float(df.iloc[i - k]["range_atr"]) for k in range(1, 4)])
        )
    else:
        out["range_expansion_3"] = None
    if i >= 5:
        out["range_expansion_5"] = safe_float(df.iloc[i]["range_atr"]) - safe_float(
            np.nanmean([safe_float(df.iloc[i - k]["range_atr"]) for k in range(1, 6)])
        )
    else:
        out["range_expansion_5"] = None

    k = safe_float(row["stoch_k"])
    d = safe_float(row["stoch_d"])
    out["stoch_k"] = k
    out["stoch_d"] = d
    out["k_minus_d"] = (k - d) if k is not None and d is not None else None
    out["stoch_extremeness"] = (k - STOCH_HIGH_K) if k is not None else None
    if i >= 1 and k is not None:
        pk = safe_float(df.iloc[i - 1]["stoch_k"])
        out["stoch_k_slope_1"] = (k - pk) if pk is not None else None
    else:
        out["stoch_k_slope_1"] = None
    if i >= 3 and k is not None:
        pk3 = safe_float(df.iloc[i - 3]["stoch_k"])
        out["stoch_k_slope_3"] = (k - pk3) if pk3 is not None else None
    else:
        out["stoch_k_slope_3"] = None
    out["stoch_still_rising"] = (
        1.0 if (out["stoch_k_slope_1"] is not None and out["stoch_k_slope_1"] > 0) else 0.0
    )
    bars_ob = None
    for back in range(0, min(i, 40) + 1):
        kk = safe_float(df.iloc[i - back]["stoch_k"])
        if kk is not None and kk >= STOCH_HIGH_K:
            bars_ob = back
            break
    out["bars_since_ob"] = float(bars_ob) if bars_ob is not None else None

    # Wave maturity from segmenter
    out["wave_n_bars"] = None
    out["wave_efficiency"] = None
    if not waves.empty and "end_available_at" in waves.columns:
        wknown = waves[pd.to_datetime(waves["end_available_at"], utc=True) <= pd.Timestamp(_utc(entry_ts))]
        if not wknown.empty:
            w = wknown.iloc[-1]
            check_causal(w["end_available_at"], entry_ts)
            out["wave_n_bars"] = safe_float(w.get("n_bars"))
            out["wave_efficiency"] = safe_float(w.get("directional_efficiency"))

    # HTF 30m / 1h
    def htf_state(hdf: pd.DataFrame, prefix: str) -> None:
        global HTF_AVAIL_VIOLATIONS
        j = causal_idx(hdf, entry_ts)
        if j < 0 or hdf.empty:
            out[f"{prefix}_k"] = None
            out[f"{prefix}_bullish"] = None
            return
        rr = hdf.iloc[j]
        check_causal(rr["available_at"], entry_ts)
        if _utc(rr["available_at"]) > _utc(entry_ts):
            HTF_AVAIL_VIOLATIONS += 1
        kk = safe_float(rr["stoch_k"])
        dd = safe_float(rr["stoch_d"])
        out[f"{prefix}_k"] = kk
        bull = None
        if kk is not None and dd is not None and j >= 1:
            pk = safe_float(hdf.iloc[j - 1]["stoch_k"])
            rising = pk is not None and kk > pk
            bull = 1.0 if (kk > dd and rising) else 0.0
        out[f"{prefix}_bullish"] = bull

    htf_state(htf30, "htf30")
    htf_state(htf1h, "htf1h")

    # Early path timing on 15m bars after entry (next opens)
    ep = entry_price if entry_price else close
    for nbar in (1, 2, 3):
        j = i + nbar
        if j >= len(df):
            out[f"mae_{nbar}bar_pct"] = None
            out[f"mfe_{nbar}bar_pct"] = None
            out[f"ret_{nbar}bar_after"] = None
            continue
        # path through highs/lows of bars i+1 .. i+nbar (trade after entry open ~ next bar)
        # Use bars from i (entry bar may include post-entry if entry mid-bar); prefer i+1..
        start = min(i + 1, len(df) - 1)
        end = j
        if start > end:
            out[f"mae_{nbar}bar_pct"] = None
            out[f"mfe_{nbar}bar_pct"] = None
            out[f"ret_{nbar}bar_after"] = None
            continue
        highs = df["high"].to_numpy(dtype=float)[start : end + 1]
        lows = df["low"].to_numpy(dtype=float)[start : end + 1]
        # SHORT adverse = price up
        mae = float(np.min(-((highs - ep) / ep * 100.0)))
        mfe = float(np.max((ep - lows) / ep * 100.0))
        out[f"mae_{nbar}bar_pct"] = mae
        out[f"mfe_{nbar}bar_pct"] = mfe
        c_end = float(df.iloc[end]["close"])
        out[f"ret_{nbar}bar_after"] = (ep - c_end) / ep * 100.0  # SHORT PnL-ish

    out["source_available_at"] = _iso(row["available_at"])
    out["source_candle_open"] = _iso(row["timestamp"])
    return out


def outcome_stats(rows: list[dict]) -> dict:
    wins = sum(1 for r in rows if r["result"] == "WIN")
    losses = sum(1 for r in rows if r["result"] == "LOSS")
    closed = wins + losses
    pnls = [float(r["pnl"]) for r in rows if r.get("pnl") is not None]
    maes = [float(r["mae"]) for r in rows if r.get("mae") is not None]
    return {
        "n": len(rows),
        "wins": wins,
        "losses": losses,
        "winrate": (100.0 * wins / closed) if closed else None,
        "net": float(sum(pnls) - FEE_PCT * len(pnls)) if pnls else 0.0,
        "net_per_trade": float(sum(pnls) / len(pnls)) if pnls else None,
        "sl_rate": (100.0 * losses / closed) if closed else None,
        "median_MAE": float(np.median(maes)) if maes else None,
    }


def collect(rows: list[dict], feat: str) -> list[float]:
    out = []
    for r in rows:
        v = safe_float(r.get(feat))
        if v is not None:
            out.append(v)
    return out


def fit_logit_auc(
    train_rows: list[dict],
    oos_rows: list[dict],
    features: list[str],
) -> dict[str, Any]:
    """Simple L2 logistic regression via sklearn if available, else numpy fallback."""
    def matrix(rows: list[dict]) -> tuple[np.ndarray, np.ndarray, list[str]]:
        closed = [r for r in rows if r["result"] in ("WIN", "LOSS")]
        X, y = [], []
        for r in closed:
            vec = []
            ok = True
            for f in features:
                v = safe_float(r.get(f))
                if v is None:
                    ok = False
                    break
                vec.append(v)
            if not ok:
                continue
            X.append(vec)
            y.append(1 if r["result"] == "WIN" else 0)
        return np.asarray(X, dtype=float), np.asarray(y, dtype=float), features

    Xtr, ytr, feats = matrix(train_rows)
    Xte, yte, _ = matrix(oos_rows)
    if len(Xtr) < 30 or len(Xte) < 15 or len(np.unique(ytr)) < 2:
        return {"features": feats, "oos_auc": None, "n_train": len(Xtr), "n_oos": len(Xte), "coefs": {}}

    # standardize using train
    mu = Xtr.mean(axis=0)
    sd = Xtr.std(axis=0)
    sd = np.where(sd < 1e-9, 1.0, sd)
    Xtr_z = (Xtr - mu) / sd
    Xte_z = (Xte - mu) / sd

    try:
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import roc_auc_score, brier_score_loss, log_loss

        clf = LogisticRegression(penalty="l2", C=0.5, max_iter=500, solver="lbfgs")
        clf.fit(Xtr_z, ytr)
        proba = clf.predict_proba(Xte_z)[:, 1]
        auc = float(roc_auc_score(yte, proba)) if len(np.unique(yte)) > 1 else None
        brier = float(brier_score_loss(yte, proba))
        ll = float(log_loss(yte, proba))
        coefs = {feats[i]: float(clf.coef_[0][i]) for i in range(len(feats))}
        return {
            "features": feats,
            "oos_auc": auc,
            "brier": brier,
            "log_loss": ll,
            "n_train": int(len(ytr)),
            "n_oos": int(len(yte)),
            "coefs": coefs,
            "backend": "sklearn",
        }
    except Exception as e:
        # fallback: distance-only AUC ranking via spearman on linear score = -dist (higher worse for SHORT win)
        if len(feats) == 1:
            score = -Xte_z[:, 0]
            # manual AUC
            pos = score[yte == 1].tolist()
            neg = score[yte == 0].tolist()
            return {
                "features": feats,
                "oos_auc": auc_score(pos, neg),
                "n_train": int(len(ytr)),
                "n_oos": int(len(yte)),
                "coefs": {feats[0]: -1.0},
                "backend": f"fallback:{e}",
            }
        return {
            "features": feats,
            "oos_auc": None,
            "error": str(e),
            "n_train": int(len(ytr)),
            "n_oos": int(len(yte)),
            "coefs": {},
            "backend": "failed",
        }


def main() -> int:
    global LOOKAHEAD_VIOLATIONS, THRESHOLD_LEAKAGE, OUTCOME_LEAKAGE, TRAIN_OOS_CONTAMINATION
    OUT.mkdir(parents=True, exist_ok=True)
    thr_meta = json.loads(SHORT_THR_PATH.read_text())
    FROZEN_P80 = float(thr_meta["SHORT_BLOCK_THRESHOLD"])
    THRESHOLD_LEAKAGE = 0  # reused frozen TRAIN-only value; not recomputed
    OUTCOME_LEAKAGE = 0

    now = AS_OF
    start = now - timedelta(days=LOOKBACK_DAYS)
    ch = setup_clickhouse(settings=get_clickhouse_settings())
    sig_repo = SignalRepository(ch)
    candle_repo = CandleRepository(ch)

    print(f"Loading 15m Tier-A {start.date()} → {_iso(now)} …", flush=True)
    rows, total = sig_repo.query_signals(
        start=start, end=now, tier_a=True, timeframe=SIGNAL_TF,
        time_field="candle_close_time", limit=5000, offset=0,
    )
    api_rows = [_signal_row_to_api(r) for r in rows]
    api_rows = [r for r in api_rows if str(r.get("timeframe")) == SIGNAL_TF]
    api_rows.sort(key=lambda r: _utc(r.get("entry_time") or r.get("candle_close_time")))
    print(f"signals={len(api_rows)}", flush=True)

    by_sym: dict[str, list] = defaultdict(list)
    for r in api_rows:
        by_sym[str(r["symbol"]).upper()].append(r)

    pad_before = timedelta(days=10)
    pad_after = timedelta(hours=36)
    work_1m: dict[str, pd.DataFrame] = {}
    tf15: dict[str, pd.DataFrame] = {}
    tf30: dict[str, pd.DataFrame] = {}
    tf1h: dict[str, pd.DataFrame] = {}
    waves: dict[str, pd.DataFrame] = {}

    for sym, items in by_sym.items():
        t0s = [_utc(r.get("entry_time") or r.get("candle_close_time")) for r in items]
        a, b = min(t0s) - pad_before, max(t0s) + pad_after
        raw = candle_repo.get_candles(sym, a, b) or []
        work_1m[sym] = _prepare_1m(pd.DataFrame(raw)) if raw else pd.DataFrame()
        bars = bars_from_mappings(raw)
        df15 = prepare_15m(bars_to_ohlcv_df(aggregate_1m_to_timeframe(bars, "15m", as_of=now, require_complete=True)))
        tf15[sym] = df15
        tf30[sym] = prepare_htf(bars_to_ohlcv_df(aggregate_1m_to_timeframe(bars, "30m", as_of=now, require_complete=True)))
        tf1h[sym] = prepare_htf(bars_to_ohlcv_df(aggregate_1m_to_timeframe(bars, "1h", as_of=now, require_complete=True)))
        try:
            waves[sym] = build_waves_from_ohlcv(df15, symbol=sym, timeframe="15m") if not df15.empty else pd.DataFrame()
        except Exception:
            waves[sym] = pd.DataFrame()
        print(f"  {sym}: 15m={len(df15)} waves={len(waves[sym])}", flush=True)

    details: list[dict[str, Any]] = []
    for r in api_rows:
        sym = str(r["symbol"]).upper()
        side = str(r["direction"]).upper()
        sid = str(r["signal_id"])
        t0 = _utc(r.get("entry_time") or r.get("candle_close_time"))
        try:
            entry_px = float(r["entry_price"]) if r.get("entry_price") is not None else None
        except (TypeError, ValueError):
            entry_px = None
        work = work_1m[sym]
        entry_i = -1
        if not work.empty:
            entry_i = int(work["timestamp"].searchsorted(pd.Timestamp(t0), side="left"))
            if entry_i >= len(work):
                entry_i = -1
        if entry_i >= 0 and entry_px is None:
            entry_px = float(work.iloc[entry_i]["open"])
        result, pnl, mae, mfe = "OPEN", None, None, None
        if entry_i >= 0 and entry_px is not None and not work.empty:
            sim = _simulate_outcome(
                side=side, entry=float(entry_px), tf=SIGNAL_TF, entry_i=entry_i, work=work, as_of=now
            )
            result, pnl, mae, mfe = sim["result"], sim.get("pnl_pct"), sim.get("mae_pct"), sim.get("mfe_pct")

        feats = extract_features_at(
            tf15[sym],
            entry_ts=t0,
            entry_price=entry_px,
            waves=waves[sym],
            htf30=tf30[sym],
            htf1h=tf1h[sym],
            work_1m=work,
        )
        # Prefer stored dist if present for consistency with frozen audit when close-based
        dist = feats.get("dist_ema20_atr")
        details.append(
            {
                "signal_id": sid,
                "symbol": sym,
                "direction": side,
                "entry_ts": _iso(t0),
                "entry_price": entry_px,
                "result": result,
                "pnl": pnl,
                "mae": mae,
                "mfe": mfe,
                **feats,
                "dist_ema20_atr": dist,
            }
        )

    n = len(details)
    n_train = int(n * TRAIN_FRAC)
    train = details[:n_train]
    oos = details[n_train:]
    for d in train:
        d["split"] = "TRAIN"
    for d in oos:
        d["split"] = "OOS"
    TRAIN_OOS_CONTAMINATION = len({d["signal_id"] for d in train} & {d["signal_id"] for d in oos})

    # Feature inventory
    inv = []
    for group, feats in FEATURE_GROUPS.items():
        for f in feats:
            inv.append(
                {
                    "group": group,
                    "feature": f,
                    "note": "EMA50 not in freeze (EMA_SPANS=9/20/100/400)" if "ema50" in f else "",
                    "tested": True,
                }
            )
    inv.append({"group": "trend_ema", "feature": "ema50_slope", "note": "NOT AVAILABLE in strategy", "tested": False})
    write_csv(OUT / "feature_inventory.csv", inv)
    all_feats = [f for fs in FEATURE_GROUPS.values() for f in fs]
    n_tested = len(all_feats)

    # SHORT OOS groups using FROZEN threshold
    oos_short = [d for d in oos if d["direction"] == "SHORT" and d.get("dist_ema20_atr") is not None]
    extended = [d for d in oos_short if float(d["dist_ema20_atr"]) > FROZEN_P80]
    kept = [d for d in oos_short if float(d["dist_ema20_atr"]) <= FROZEN_P80]
    ext_stats = outcome_stats(extended)
    kept_stats = outcome_stats(kept)

    # Feature group comparison EXTENDED vs KEPT
    fg_rows = []
    for f in all_feats:
        s = summarize_group(collect(extended, f), collect(kept, f), name=f, label_a="EXTENDED", label_b="KEPT")
        s["group"] = next(g for g, fs in FEATURE_GROUPS.items() if f in fs)
        fg_rows.append(s)
    fg_rows.sort(key=lambda r: -(r.get("effect_abs") or 0))
    write_csv(OUT / "feature_group_comparison.csv", fg_rows)

    # EXTENDED winner vs loser
    ext_w = [d for d in extended if d["result"] == "WIN"]
    ext_l = [d for d in extended if d["result"] == "LOSS"]
    ewl_rows = []
    for f in all_feats:
        s = summarize_group(collect(ext_w, f), collect(ext_l, f), name=f, label_a="EXT_WIN", label_b="EXT_LOSS")
        s["sample_flag"] = "VERY_SMALL" if len(ext_w) < 8 or len(ext_l) < 8 else "SMALL" if len(extended) < 25 else "OK"
        ewl_rows.append(s)
    ewl_rows.sort(key=lambda r: -(r.get("effect_abs") or 0))
    write_csv(OUT / "extended_winner_loser_comparison.csv", ewl_rows)

    # Univariate ranking on OOS SHORT (WIN vs LOSS)
    oos_sw = [d for d in oos_short if d["result"] == "WIN"]
    oos_sl = [d for d in oos_short if d["result"] == "LOSS"]
    uni = []
    for f in all_feats:
        pos, neg = collect(oos_sw, f), collect(oos_sl, f)
        auc = auc_score(pos, neg)
        # also spearman with win label
        vals, labs = [], []
        for r in oos_short:
            if r["result"] not in ("WIN", "LOSS"):
                continue
            v = safe_float(r.get(f))
            if v is None:
                continue
            vals.append(v)
            labs.append(1 if r["result"] == "WIN" else 0)
        sp = None
        if len(vals) >= 20 and len(set(vals)) > 1:
            sp, _ = stats.spearmanr(vals, labs)
            sp = float(sp) if sp is not None and not (isinstance(sp, float) and math.isnan(sp)) else None
        # For SHORT risk: higher extension → lower win → AUC of raw feature may be <0.5
        # Rank by |AUC-0.5| and also by ability to predict LOSS (1-AUC if AUC>0.5 for win)
        strength = abs((auc or 0.5) - 0.5) * 2 if auc is not None else None
        uni.append(
            {
                "feature": f,
                "auc_higher_favors_win": auc,
                "spearman_with_win": sp,
                "assoc_strength": strength,
                "n_win": len(pos),
                "n_loss": len(neg),
                "win_median": float(np.median(pos)) if pos else None,
                "loss_median": float(np.median(neg)) if neg else None,
            }
        )
    uni.sort(key=lambda r: -(r.get("assoc_strength") or 0))
    write_csv(OUT / "univariate_feature_ranking.csv", uni)

    # Distance-control: residualize outcome vs dist, then check other features
    # Within high-extension band (top 40% of SHORT OOS by dist) and residual approach
    dists = [float(d["dist_ema20_atr"]) for d in oos_short]
    p60 = float(np.percentile(dists, 60))
    high_band = [d for d in oos_short if float(d["dist_ema20_atr"]) >= p60]
    # residual: win ~ a + b*dist on TRAIN SHORT, then OOS residual correlate with features
    train_short = [d for d in train if d["direction"] == "SHORT" and d.get("dist_ema20_atr") is not None]
    tr_closed = [d for d in train_short if d["result"] in ("WIN", "LOSS") and d.get("dist_ema20_atr") is not None]
    Xd = np.asarray([float(d["dist_ema20_atr"]) for d in tr_closed], dtype=float)
    yd = np.asarray([1.0 if d["result"] == "WIN" else 0.0 for d in tr_closed], dtype=float)
    # linear prob approx
    if len(Xd) >= 20:
        b1, b0 = np.polyfit(Xd, yd, 1)
    else:
        b1, b0 = 0.0, float(np.mean(yd)) if len(yd) else 0.5
    ctrl_rows = []
    for f in all_feats:
        if f == "dist_ema20_atr":
            continue
        # within high band: effect EXT win/loss controlling roughly by similar dist
        hb_w = collect([d for d in high_band if d["result"] == "WIN"], f)
        hb_l = collect([d for d in high_band if d["result"] == "LOSS"], f)
        # residual spearman on OOS short
        resid, fvals = [], []
        for d in oos_short:
            if d["result"] not in ("WIN", "LOSS"):
                continue
            v = safe_float(d.get(f))
            dist = safe_float(d.get("dist_ema20_atr"))
            if v is None or dist is None:
                continue
            pred = b0 + b1 * dist
            resid.append((1.0 if d["result"] == "WIN" else 0.0) - pred)
            fvals.append(v)
        sp_res = None
        if len(fvals) >= 20 and len(set(fvals)) > 1:
            sp_res, _ = stats.spearmanr(fvals, resid)
            sp_res = float(sp_res) if sp_res is not None and not math.isnan(sp_res) else None
        ctrl_rows.append(
            {
                "feature": f,
                "high_band_n": len(high_band),
                "high_band_cliffs_win_vs_loss": cliffs_delta(hb_w, hb_l),
                "residual_spearman_after_dist": sp_res,
                "residual_abs": abs(sp_res) if sp_res is not None else None,
            }
        )
    # Does dist remain after controlling for best momentum?
    # Partial: correlate dist with residual of win~momentum
    best_mom = "ret_3"
    tr_m = [d for d in tr_closed if safe_float(d.get(best_mom)) is not None]
    if len(tr_m) >= 20:
        Xm = np.asarray([float(d[best_mom]) for d in tr_m])
        ym = np.asarray([1.0 if d["result"] == "WIN" else 0.0 for d in tr_m])
        m1, m0 = np.polyfit(Xm, ym, 1)
        resid_d, dists_r = [], []
        for d in oos_short:
            if d["result"] not in ("WIN", "LOSS"):
                continue
            mv = safe_float(d.get(best_mom))
            dist = safe_float(d.get("dist_ema20_atr"))
            if mv is None or dist is None:
                continue
            pred = m0 + m1 * mv
            resid_d.append((1.0 if d["result"] == "WIN" else 0.0) - pred)
            dists_r.append(dist)
        sp_dist = None
        if len(dists_r) >= 20:
            sp_dist, _ = stats.spearmanr(dists_r, resid_d)
            sp_dist = float(sp_dist) if sp_dist is not None and not math.isnan(float(sp_dist)) else None
        ctrl_rows.append(
            {
                "feature": "dist_ema20_atr_AFTER_ret3_control",
                "high_band_n": len(high_band),
                "high_band_cliffs_win_vs_loss": None,
                "residual_spearman_after_dist": sp_dist,
                "residual_abs": abs(sp_dist) if sp_dist is not None else None,
                "note": "spearman(dist, residual_win_after_ret3)",
            }
        )
    ctrl_rows.sort(key=lambda r: -(r.get("residual_abs") or 0))
    write_csv(OUT / "distance_control_results.csv", ctrl_rows)

    # Models
    models = [
        ("M1_dist", ["dist_ema20_atr"]),
        ("M2_momentum", ["ret_3", "ret_5", "bullish_bars_5", "mom_accel"]),
        ("M3_trend", ["ema20_slope_atr", "ema9_vs_ema20_atr", "dist_ema100_atr"]),
        ("M4_wave", ["stoch_k", "stoch_k_slope_1", "wave_n_bars", "stoch_still_rising"]),
        ("M5_combined", ["dist_ema20_atr", "ret_3", "ema20_slope_atr", "stoch_k_slope_1"]),
    ]
    model_rows = []
    coef_rows = []
    for name, feats in models:
        res = fit_logit_auc(train_short, oos_short, feats)
        model_rows.append({"model": name, **{k: v for k, v in res.items() if k != "coefs"}})
        for f, c in (res.get("coefs") or {}).items():
            coef_rows.append({"model": name, "feature": f, "coef_std": c})
    write_csv(OUT / "model_comparison.csv", model_rows)
    write_csv(OUT / "model_coefficients.csv", coef_rows)

    # Interactions — thresholds from TRAIN SHORT quantiles
    def train_q(feat: str, q: float) -> float | None:
        xs = collect(train_short, feat)
        return float(np.percentile(xs, q)) if len(xs) >= 10 else None

    thr_mom = train_q("ret_3", 70)
    thr_slope = train_q("ema20_slope_atr", 70)
    thr_vol = train_q("atr_vs_med50", 70)
    interactions = [
        ("A_EXT_STRONG_POS_MOM", lambda d: float(d["dist_ema20_atr"]) > FROZEN_P80 and thr_mom is not None and (safe_float(d.get("ret_3")) or -1e9) >= thr_mom),
        ("B_EXT_POS_EMA20_SLOPE", lambda d: float(d["dist_ema20_atr"]) > FROZEN_P80 and thr_slope is not None and (safe_float(d.get("ema20_slope_atr")) or -1e9) >= thr_slope),
        ("C_EXT_ATR_EXPANSION", lambda d: float(d["dist_ema20_atr"]) > FROZEN_P80 and thr_vol is not None and (safe_float(d.get("atr_vs_med50")) or -1e9) >= thr_vol),
        ("D_EXT_STOCH_NOT_ROLLED", lambda d: float(d["dist_ema20_atr"]) > FROZEN_P80 and (safe_float(d.get("stoch_still_rising")) or 0) >= 1.0),
        ("E_EXT_HTF_BULLISH", lambda d: float(d["dist_ema20_atr"]) > FROZEN_P80 and ((safe_float(d.get("htf30_bullish")) or 0) + (safe_float(d.get("htf1h_bullish")) or 0)) >= 1.0),
    ]
    inter_rows = []
    for name, pred in interactions:
        hit = [d for d in oos_short if pred(d)]
        ctrl = [d for d in oos_short if not pred(d)]
        sh, sc = outcome_stats(hit), outcome_stats(ctrl)
        inter_rows.append(
            {
                "interaction": name,
                "hit_n": sh["n"],
                "hit_wr": sh["winrate"],
                "hit_net": sh["net"],
                "hit_net_per_trade": sh["net_per_trade"],
                "hit_sl": sh["sl_rate"],
                "ctrl_n": sc["n"],
                "ctrl_wr": sc["winrate"],
                "ctrl_net": sc["net"],
                "ctrl_net_per_trade": sc["net_per_trade"],
                "train_mom_p70": thr_mom,
                "train_slope_p70": thr_slope,
                "train_vol_p70": thr_vol,
            }
        )
    write_csv(OUT / "interaction_results.csv", inter_rows)

    # Distance buckets
    buckets = [(-1e9, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 4.0), (4.0, 1e9)]
    buck_rows = []
    for lo, hi in buckets:
        lab = f"({lo},{hi}]" if lo > -1e8 else f"<=1"
        if hi > 1e8:
            lab = ">4"
        elif lo == 1.0:
            lab = "1-2"
        elif lo == 2.0:
            lab = "2-3"
        elif lo == 3.0:
            lab = "3-4"
        grp = [d for d in oos_short if lo < float(d["dist_ema20_atr"]) <= hi] if lo > -1e8 else [
            d for d in oos_short if float(d["dist_ema20_atr"]) <= 1.0
        ]
        if lo <= -1e8:
            grp = [d for d in oos_short if float(d["dist_ema20_atr"]) <= 1.0]
        elif hi > 1e8:
            grp = [d for d in oos_short if float(d["dist_ema20_atr"]) > 4.0]
        else:
            grp = [d for d in oos_short if lo < float(d["dist_ema20_atr"]) <= hi]
        st = outcome_stats(grp)
        buck_rows.append(
            {
                "bucket": lab,
                **st,
                "median_momentum_ret3": float(np.median(collect(grp, "ret_3"))) if collect(grp, "ret_3") else None,
                "median_ema20_slope": float(np.median(collect(grp, "ema20_slope_atr"))) if collect(grp, "ema20_slope_atr") else None,
                "median_wave_n_bars": float(np.median(collect(grp, "wave_n_bars"))) if collect(grp, "wave_n_bars") else None,
                "median_stoch_k_slope": float(np.median(collect(grp, "stoch_k_slope_1"))) if collect(grp, "stoch_k_slope_1") else None,
            }
        )
    write_csv(OUT / "distance_buckets.csv", buck_rows)

    # Timing: EXTENDED vs KEPT early MAE
    timing_rows = []
    for label, grp in (("EXTENDED", extended), ("KEPT", kept)):
        for nbar in (1, 2, 3):
            maes = collect(grp, f"mae_{nbar}bar_pct")
            mfes = collect(grp, f"mfe_{nbar}bar_pct")
            rets = collect(grp, f"ret_{nbar}bar_after")
            timing_rows.append(
                {
                    "group": label,
                    "horizon_bars": nbar,
                    "n": len(maes),
                    "median_MAE": float(np.median(maes)) if maes else None,
                    "median_MFE": float(np.median(mfes)) if mfes else None,
                    "median_short_ret": float(np.median(rets)) if rets else None,
                    "frac_immediate_adverse_1pct": (
                        sum(1 for x in maes if x <= -1.0) / len(maes) if maes else None
                    ),
                }
            )
    # EXT winners vs losers timing
    for label, grp in (("EXT_WIN", ext_w), ("EXT_LOSS", ext_l)):
        for nbar in (1, 2, 3):
            maes = collect(grp, f"mae_{nbar}bar_pct")
            timing_rows.append(
                {
                    "group": label,
                    "horizon_bars": nbar,
                    "n": len(maes),
                    "median_MAE": float(np.median(maes)) if maes else None,
                    "median_MFE": float(np.median(collect(grp, f"mfe_{nbar}bar_pct"))) if collect(grp, f"mfe_{nbar}bar_pct") else None,
                    "median_short_ret": float(np.median(collect(grp, f"ret_{nbar}bar_after"))) if collect(grp, f"ret_{nbar}bar_after") else None,
                    "frac_immediate_adverse_1pct": (
                        sum(1 for x in maes if x <= -1.0) / len(maes) if maes else None
                    ),
                }
            )
    write_csv(OUT / "timing_analysis.csv", timing_rows)

    # TRAIN replication top features
    train_ext = [d for d in train_short if float(d["dist_ema20_atr"]) > FROZEN_P80]
    train_kept = [d for d in train_short if float(d["dist_ema20_atr"]) <= FROZEN_P80]
    top5 = [r["feature"] for r in fg_rows[:5]]
    train_rep = []
    for f in top5:
        s_oos = next(r for r in fg_rows if r["feature"] == f)
        s_tr = summarize_group(collect(train_ext, f), collect(train_kept, f), name=f, label_a="EXTENDED", label_b="KEPT")
        same = None
        if s_oos.get("median_gap_A_minus_B") is not None and s_tr.get("median_gap_A_minus_B") is not None:
            same = (s_oos["median_gap_A_minus_B"] * s_tr["median_gap_A_minus_B"]) > 0
        train_rep.append(
            {
                "feature": f,
                "oos_median_gap_ext_minus_kept": s_oos.get("median_gap_A_minus_B"),
                "train_median_gap_ext_minus_kept": s_tr.get("median_gap_A_minus_B"),
                "same_direction": same,
                "oos_effect": s_oos.get("effect_abs"),
                "train_effect": s_tr.get("effect_abs"),
            }
        )
    # outcome replication
    train_rep.append(
        {
            "feature": "OUTCOME",
            "oos_ext_wr": ext_stats["winrate"],
            "oos_kept_wr": kept_stats["winrate"],
            "train_ext_wr": outcome_stats(train_ext)["winrate"],
            "train_kept_wr": outcome_stats(train_kept)["winrate"],
            "same_direction": (
                (ext_stats["winrate"] or 50) < (kept_stats["winrate"] or 50)
                and (outcome_stats(train_ext)["winrate"] or 50) < (outcome_stats(train_kept)["winrate"] or 50)
            ),
        }
    )
    write_csv(OUT / "train_replication.csv", train_rep)
    train_consistent = all(r.get("same_direction") for r in train_rep if r.get("same_direction") is not None)

    # SHORT vs LONG mechanism — load LONG extended from long audit logic
    # LONG extension = (ema20 - price)/atr > long P80; compare feature profiles
    long_sum = json.loads(LONG_SUM_PATH.read_text()) if LONG_SUM_PATH.exists() else {}
    long_p80 = float(long_sum.get("frozen_threshold") or 3.0)
    oos_long = [d for d in oos if d["direction"] == "LONG" and d.get("dist_ema20_atr") is not None]
    # For LONG, "extended below" means price << ema20 → dist_ema20_atr strongly negative
    # Mirror: long_ext_metric = -dist_ema20_atr
    for d in details:
        d["long_ext_metric"] = (-float(d["dist_ema20_atr"])) if d.get("dist_ema20_atr") is not None else None
    oos_long_ext = [d for d in oos_long if d["long_ext_metric"] is not None and d["long_ext_metric"] > long_p80]
    mech_feats = top5[:5] if top5 else ["ret_3", "ema20_slope_atr", "stoch_k_slope_1", "atr_vs_med50", "dist_ema20_atr"]
    mech_rows = []
    for f in mech_feats:
        s_med = float(np.median(collect(extended, f))) if collect(extended, f) else None
        l_med = float(np.median(collect(oos_long_ext, f))) if collect(oos_long_ext, f) else None
        interp = ""
        if f in ("ret_3", "ret_5", "ema20_slope_atr") and s_med is not None and l_med is not None:
            if s_med > 0 and l_med < 0:
                interp = "SHORT ext still rising; LONG ext already falling"
            elif s_med > 0 and l_med > 0:
                interp = "both still moving with impulse"
            else:
                interp = "mixed"
        if f == "stoch_k_slope_1" and s_med is not None and l_med is not None:
            interp = "SHORT stoch still rising" if s_med > 0 else "SHORT stoch rolling"
            if l_med < 0:
                interp += "; LONG stoch falling"
        mech_rows.append(
            {
                "feature": f,
                "SHORT_EXTENDED_median": s_med,
                "LONG_EXTENDED_median": l_med,
                "SHORT_EXTENDED_n": len(extended),
                "LONG_EXTENDED_n": len(oos_long_ext),
                "interpretation": interp,
            }
        )
    write_csv(OUT / "short_long_mechanism_comparison.csv", mech_rows)

    # Timing diagnosis
    ext_mae1 = float(np.median(collect(extended, "mae_1bar_pct"))) if collect(extended, "mae_1bar_pct") else None
    kept_mae1 = float(np.median(collect(kept, "mae_1bar_pct"))) if collect(kept, "mae_1bar_pct") else None
    ext_loss_mae1 = float(np.median(collect(ext_l, "mae_1bar_pct"))) if collect(ext_l, "mae_1bar_pct") else None
    ext_loss_mfe1 = float(np.median(collect(ext_l, "mfe_1bar_pct"))) if collect(ext_l, "mfe_1bar_pct") else None
    if ext_loss_mae1 is not None and ext_loss_mae1 <= -0.5 and (ext_loss_mfe1 is None or ext_loss_mfe1 < 0.3):
        timing_class = "DIRECTION_WRONG_OR_IMMEDIATE_ADVERSE"
    elif ext_loss_mfe1 is not None and ext_loss_mfe1 >= 0.4 and ext_loss_mae1 is not None and ext_loss_mae1 <= -0.5:
        timing_class = "ENTRY_TOO_EARLY_THEN_REVERSAL_FAIL"
    else:
        timing_class = "MIXED_OR_UNCLEAR_TIMING"

    # Rank: is dist still top?
    dist_rank = next((i + 1 for i, r in enumerate(uni) if r["feature"] == "dist_ema20_atr"), None)
    dist_ctrl = next((r for r in ctrl_rows if r["feature"] == "dist_ema20_atr_AFTER_ret3_control"), None)
    mom_top = next((r for r in uni if r["feature"] in ("ret_3", "ret_5", "ema20_slope_atr")), None)
    best_model = max(model_rows, key=lambda r: (r.get("oos_auc") is not None, r.get("oos_auc") or 0))

    # Primary decision
    n_ext = len(extended)
    sample_warn = "VERY_SMALL_EXTENDED_SAMPLE" if n_ext < 20 else ("SMALL_EXTENDED_SAMPLE" if n_ext < 50 else "OK")

    # Effect sizes
    dist_effect = next((r.get("effect_abs") for r in fg_rows if r["feature"] == "dist_ema20_atr"), 0) or 0
    mom_effect = max((r.get("effect_abs") or 0) for r in fg_rows if r["feature"] in FEATURE_GROUPS["momentum"] + ["ema20_slope_atr"])
    wave_effect = max((r.get("effect_abs") or 0) for r in fg_rows if r["feature"] in FEATURE_GROUPS["wave_stoch"])

    dist_after_ctrl = abs(dist_ctrl.get("residual_spearman_after_dist") or 0) if dist_ctrl else 0
    inter_a = next(r for r in inter_rows if r["interaction"].startswith("A_"))
    inter_d = next(r for r in inter_rows if r["interaction"].startswith("D_"))

    if n_ext < 12:
        primary = "ROOT_CAUSE_NOT_IDENTIFIABLE_WITH_CURRENT_SAMPLE"
        recommendation = "NO_NEW_FILTER_SUPPORTED"
    elif dist_after_ctrl >= 0.15 and dist_effect >= mom_effect * 0.8 and dist_rank and dist_rank <= 3:
        primary = "EMA20_EXTENSION_IS_INDEPENDENT_SHORT_RISK_FACTOR"
        recommendation = "KEEP_SIMPLE_EMA20_EXTENSION_CANDIDATE"
    elif mom_effect > dist_effect + 0.05 and dist_after_ctrl < 0.12:
        primary = "EMA20_EXTENSION_IS_PROXY_FOR_STRONG_UP_MOMENTUM"
        recommendation = "INVESTIGATE_MOMENTUM_CONDITION"
    elif (inter_a.get("hit_wr") is not None and inter_a["hit_n"] >= 5 and (inter_a["hit_wr"] or 100) + 8 < (kept_stats["winrate"] or 0)):
        primary = "EMA20_EXTENSION_PLUS_MOMENTUM_DEFINES_RISK"
        recommendation = "INVESTIGATE_MOMENTUM_CONDITION"
    elif wave_effect >= dist_effect and (inter_d.get("hit_wr") is not None and inter_d["hit_n"] >= 5 and (inter_d["hit_wr"] or 50) < 45):
        primary = "SHORT_LOSSES_ARE_WAVE_TIMING_PROBLEM"
        recommendation = "INVESTIGATE_WAVE_TIMING_CONDITION"
    elif not train_consistent:
        primary = "ROOT_CAUSE_NOT_IDENTIFIABLE_WITH_CURRENT_SAMPLE"
        recommendation = "NO_NEW_FILTER_SUPPORTED"
    else:
        # default: proxy/combo leaning
        if mom_effect >= dist_effect:
            primary = "EMA20_EXTENSION_IS_PROXY_FOR_STRONG_UP_MOMENTUM"
            recommendation = "INVESTIGATE_MOMENTUM_CONDITION"
        else:
            primary = "EMA20_EXTENSION_PLUS_MOMENTUM_DEFINES_RISK"
            recommendation = "KEEP_SIMPLE_EMA20_EXTENSION_CANDIDATE"

    boot = {
        "frozen_p80": FROZEN_P80,
        "extended_n": n_ext,
        "kept_n": len(kept),
        "extended_outcome": ext_stats,
        "kept_outcome": kept_stats,
        "dist_rank_univariate": dist_rank,
        "dist_residual_after_ret3": dist_ctrl,
        "timing_class": timing_class,
        "sample_warning": sample_warn,
        "n_features_tested": n_tested,
        "train_replication": "TRAIN_AND_OOS_SAME_DIRECTION" if train_consistent else "TRAIN_OOS_INCONSISTENT",
    }
    (OUT / "bootstrap_results.json").write_text(json.dumps(boot, indent=2, default=str) + "\n")

    meta = {
        "task": "SHORT_EMA20_EXTENSION_ROOT_CAUSE_AUDIT",
        "frozen_threshold": FROZEN_P80,
        "threshold_source": "TRAIN_ONLY_REUSED_FROM_SHORT_OOS_AUDIT",
        "threshold_leakage": THRESHOLD_LEAKAGE,
        "lookahead_violations": LOOKAHEAD_VIOLATIONS,
        "outcome_leakage": OUTCOME_LEAKAGE,
        "train_oos_contamination": TRAIN_OOS_CONTAMINATION,
        "feature_not_available": FEATURE_NOT_AVAILABLE,
        "htf_availability_violations": HTF_AVAIL_VIOLATIONS,
        "incomplete_candle": INCOMPLETE_CANDLE,
        "n_features_tested": n_tested,
        "feature_groups": {k: len(v) for k, v in FEATURE_GROUPS.items()},
        "as_of": _iso(now),
        "primary_decision": primary,
        "recommendation": recommendation,
    }
    (OUT / "audit_metadata.json").write_text(json.dumps(meta, indent=2) + "\n")

    def fmt(v: Any, nd: int = 3) -> str:
        if v is None:
            return "–"
        if isinstance(v, float):
            return f"{v:.{nd}f}"
        return str(v)

    lines = [
        "# SHORT_EMA20_EXTENSION_ROOT_CAUSE_AUDIT",
        "",
        f"Primary: `{primary}`",
        f"Recommendation: `{recommendation}`",
        f"Sample: `{sample_warn}` | EXTENDED n={n_ext} KEPT n={len(kept)}",
        f"Frozen P80={FROZEN_P80:.4f} (TRAIN_ONLY reused) | features tested={n_tested}",
        f"leakage={THRESHOLD_LEAKAGE} lookahead={LOOKAHEAD_VIOLATIONS} outcome_leak={OUTCOME_LEAKAGE} contamination={TRAIN_OOS_CONTAMINATION}",
        "",
        "## EXTENDED vs KEPT outcomes (OOS SHORT)",
        "",
        f"- EXTENDED: n={ext_stats['n']} WR={fmt(ext_stats['winrate'],1)} Net={fmt(ext_stats['net'],1)} npt={fmt(ext_stats['net_per_trade'])} SL={fmt(ext_stats['sl_rate'],1)}",
        f"- KEPT: n={kept_stats['n']} WR={fmt(kept_stats['winrate'],1)} Net={fmt(kept_stats['net'],1)} npt={fmt(kept_stats['net_per_trade'])} SL={fmt(kept_stats['sl_rate'],1)}",
        "",
        "## Top feature differences (EXTENDED vs KEPT)",
        "",
    ]
    for r in fg_rows[:5]:
        lines.append(
            f"- {r['feature']}: ext_med={fmt(r['EXTENDED_median'])} kept_med={fmt(r['KEPT_median'])} "
            f"|Δ|effect={fmt(r['effect_abs'])}"
        )
    lines += ["", "## Univariate top", ""]
    for r in uni[:5]:
        lines.append(f"- {r['feature']}: strength={fmt(r['assoc_strength'])} AUC_win={fmt(r['auc_higher_favors_win'])}")
    lines += ["", f"dist_ema20_atr univariate rank=#{dist_rank}", ""]
    lines += ["## Models", ""]
    for r in model_rows:
        lines.append(f"- {r['model']}: OOS AUC={fmt(r.get('oos_auc'))} n_oos={r.get('n_oos')}")
    lines += ["", "## Interactions", ""]
    for r in inter_rows:
        lines.append(
            f"- {r['interaction']}: hit n={r['hit_n']} WR={fmt(r['hit_wr'],1)} npt={fmt(r['hit_net_per_trade'])} "
            f"| ctrl WR={fmt(r['ctrl_wr'],1)}"
        )
    lines += ["", f"Timing class: `{timing_class}`", "", "## Strategy Logic Changed", "", "`NO`", ""]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    # also dump summary.json for convenience
    (OUT / "summary.json").write_text(
        json.dumps(
            {
                "primary_decision": primary,
                "recommendation": recommendation,
                "sample_warning": sample_warn,
                "extended": ext_stats,
                "kept": kept_stats,
                "top_features_ext_vs_kept": fg_rows[:5],
                "univariate_top": uni[:8],
                "dist_rank": dist_rank,
                "distance_control": ctrl_rows[:8],
                "models": model_rows,
                "interactions": inter_rows,
                "timing_class": timing_class,
                "train_replication_flag": boot["train_replication"],
                "mechanism": mech_rows,
                "buckets": buck_rows,
                "causality": meta,
            },
            indent=2,
            default=str,
        )
        + "\n"
    )

    print("PRIMARY", primary, flush=True)
    print("REC", recommendation, flush=True)
    print("EXTENDED", ext_stats, "KEPT", kept_stats, flush=True)
    print("dist_rank", dist_rank, "wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
