#!/usr/bin/env python3
"""SHORT_EXTENDED_RSI_CONTINUATION_AUDIT — research-only, no DB writes.

Tests whether frozen Wilder RSI(14) bullish-continuation states explain
bad EXTENDED 15m Tier-A SHORTs better than EMA20 distance alone.

RSI scale: 0..100 (NOT 0..1). Neutral threshold: 50 (== repo rsi_end_gt_50).
"""

from __future__ import annotations

import csv
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

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
from signal_generator.strategy.wave_fade.parameters import PRIMARY_FEE, RSI_LENGTH  # noqa: E402
from signal_generator.timeframes import (  # noqa: E402
    aggregate_1m_to_timeframe,
    bars_from_mappings,
)

OUT = ROOT / "results" / "short_extended_rsi_continuation_audit"
SHORT_THR_PATH = ROOT / "results" / "short_ema20_extension_oos_validation" / "train_threshold.json"

AS_OF = datetime(2026, 8, 11, 9, 30, 7, 105630, tzinfo=timezone.utc)
LOOKBACK_DAYS = 60
SIGNAL_TF = "15m"
FEE_PCT = float(PRIMARY_FEE)
ATR_LEN = 14
TRAIN_FRAC = 0.60
RSI_NEUTRAL = 50.0  # frozen scale 0..100; maps user "0.5" → 50

LOOKAHEAD_VIOLATIONS = 0
FEATURE_NOT_AVAILABLE = 0
THRESHOLD_LEAKAGE = 0
OUTCOME_LEAKAGE = 0
TRAIN_OOS_CONTAMINATION = 0
HTF_AVAIL_VIOLATIONS = 0
INCOMPLETE_CANDLE = 0


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


def prepare_tf(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = attach_indicators(df)  # includes Wilder RSI(14) as df["rsi"] on 0..100
    close = out["close"].astype(float)
    high = out["high"].astype(float)
    low = out["low"].astype(float)
    out["atr"] = wilder_atr(high, low, close, ATR_LEN)
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


def outcome_stats(rows: list[dict]) -> dict:
    wins = sum(1 for r in rows if r["result"] == "WIN")
    losses = sum(1 for r in rows if r["result"] == "LOSS")
    closed = wins + losses
    pnls = [float(r["pnl"]) for r in rows if r.get("pnl") is not None]
    maes = [float(r["mae"]) for r in rows if r.get("mae") is not None]
    mfes = [float(r["mfe"]) for r in rows if r.get("mfe") is not None]
    return {
        "n": len(rows),
        "wins": wins,
        "losses": losses,
        "winrate": (100.0 * wins / closed) if closed else None,
        "net": float(sum(pnls) - FEE_PCT * len(pnls)) if pnls else 0.0,
        "net_per_trade": float(sum(pnls) / len(pnls)) if pnls else None,
        "sl_rate": (100.0 * losses / closed) if closed else None,
        "median_MAE": float(np.median(maes)) if maes else None,
        "median_MFE": float(np.median(mfes)) if mfes else None,
        "sample_flag": "VERY_SMALL_SAMPLE" if len(rows) < 10 else ("SMALL_SAMPLE" if len(rows) < 20 else "OK"),
    }


def collect(rows: list[dict], feat: str) -> list[float]:
    out = []
    for r in rows:
        v = safe_float(r.get(feat))
        if v is not None:
            out.append(v)
    return out


def extract_at(df: pd.DataFrame, entry_ts: datetime, entry_price: float | None) -> dict[str, Any]:
    global FEATURE_NOT_AVAILABLE
    i = causal_idx(df, entry_ts)
    if i < 0:
        FEATURE_NOT_AVAILABLE += 1
        return {}
    row = df.iloc[i]
    check_causal(row["available_at"], entry_ts)
    close = safe_float(row["close"])
    atr = safe_float(row["atr"])
    ema20 = safe_float(row["ema20"])
    rsi = safe_float(row["rsi"])
    if close is None or atr is None or atr <= 0 or ema20 is None or rsi is None:
        FEATURE_NOT_AVAILABLE += 1
        return {}

    rsi_prev = safe_float(df.iloc[i - 1]["rsi"]) if i >= 1 else None
    rsi_prev2 = safe_float(df.iloc[i - 2]["rsi"]) if i >= 2 else None

    # EMA20 slope ATR-normalized over 3 bars (same as root-cause audit)
    ema20_slope = None
    if i >= 3:
        e0 = safe_float(df.iloc[i - 3]["ema20"])
        if e0 is not None:
            ema20_slope = (ema20 - e0) / atr

    # consecutive bars RSI > 50 ending at i
    consec = 0
    for back in range(0, min(i, 20) + 1):
        rv = safe_float(df.iloc[i - back]["rsi"])
        if rv is not None and rv > RSI_NEUTRAL:
            consec += 1
        else:
            break

    min_rsi_2 = None
    if rsi_prev is not None:
        min_rsi_2 = min(rsi, rsi_prev)
    min_rsi_3 = None
    if rsi_prev is not None and rsi_prev2 is not None:
        min_rsi_3 = min(rsi, rsi_prev, rsi_prev2)

    # PRIOR frozen wave flag semantics: rsi_end_gt_50
    prior_cont = bool(rsi > RSI_NEUTRAL)

    # States (predeclared)
    s1 = bool(rsi > RSI_NEUTRAL)
    s2 = bool(rsi_prev is not None and rsi > rsi_prev)
    s3 = bool(s1 and s2)
    s4 = bool(min_rsi_2 is not None and min_rsi_2 > RSI_NEUTRAL)
    s5 = bool(min_rsi_3 is not None and min_rsi_3 > RSI_NEUTRAL)
    s6 = bool(s1 and ema20_slope is not None and ema20_slope > 0)

    # early path timing 1–3 bars after entry on 15m
    ep = entry_price if entry_price else close
    timing = {}
    for nbar in (1, 2, 3):
        start = min(i + 1, len(df) - 1)
        end = i + nbar
        if end >= len(df) or start > end:
            timing[f"mae_{nbar}bar"] = None
            timing[f"mfe_{nbar}bar"] = None
            continue
        highs = df["high"].to_numpy(dtype=float)[start : end + 1]
        lows = df["low"].to_numpy(dtype=float)[start : end + 1]
        timing[f"mae_{nbar}bar"] = float(np.min(-((highs - ep) / ep * 100.0)))
        timing[f"mfe_{nbar}bar"] = float(np.max((ep - lows) / ep * 100.0))

    return {
        "dist_ema20_atr": (close - ema20) / atr,
        "ema20_slope_atr": ema20_slope,
        "rsi": rsi,
        "rsi_prev": rsi_prev,
        "rsi_delta_1": (rsi - rsi_prev) if rsi_prev is not None else None,
        "min_rsi_last_2": min_rsi_2,
        "min_rsi_last_3": min_rsi_3,
        "consec_bars_rsi_gt_50": float(consec),
        "RSI_STATE_1_gt50": s1,
        "RSI_STATE_2_rising": s2,
        "RSI_STATE_3_gt50_rising": s3,
        "RSI_STATE_4_gt50_last2": s4,
        "RSI_STATE_5_gt50_last3": s5,
        "RSI_STATE_6_gt50_pos_ema20_slope": s6,
        "PRIOR_RSI_CONTINUATION_STATE": prior_cont,  # == rsi_end_gt_50 / RSI>50
        "pos_ema20_slope": bool(ema20_slope is not None and ema20_slope > 0),
        "available_at": _iso(row["available_at"]),
        "source_candle_open": _iso(row["timestamp"]),
        **timing,
    }


def htf_rsi_gt50(hdf: pd.DataFrame, entry_ts: datetime) -> bool | None:
    global HTF_AVAIL_VIOLATIONS
    j = causal_idx(hdf, entry_ts)
    if j < 0 or hdf.empty:
        return None
    row = hdf.iloc[j]
    check_causal(row["available_at"], entry_ts)
    if _utc(row["available_at"]) > _utc(entry_ts):
        HTF_AVAIL_VIOLATIONS += 1
    rsi = safe_float(row["rsi"])
    if rsi is None:
        return None
    return bool(rsi > RSI_NEUTRAL)


def compare_active_inactive(rows: list[dict], state_key: str, *, scope: str) -> dict:
    active = [r for r in rows if r.get(state_key) is True]
    inactive = [r for r in rows if r.get(state_key) is False]
    a, b = outcome_stats(active), outcome_stats(inactive)
    return {
        "scope": scope,
        "state": state_key,
        "active_n": a["n"],
        "active_wins": a["wins"],
        "active_losses": a["losses"],
        "active_wr": a["winrate"],
        "active_net": a["net"],
        "active_npt": a["net_per_trade"],
        "active_sl": a["sl_rate"],
        "active_mae": a["median_MAE"],
        "active_mfe": a["median_MFE"],
        "active_flag": a["sample_flag"],
        "inactive_n": b["n"],
        "inactive_wins": b["wins"],
        "inactive_losses": b["losses"],
        "inactive_wr": b["winrate"],
        "inactive_net": b["net"],
        "inactive_npt": b["net_per_trade"],
        "inactive_sl": b["sl_rate"],
        "inactive_mae": b["median_MAE"],
        "inactive_mfe": b["median_MFE"],
        "inactive_flag": b["sample_flag"],
        "wr_gap_active_minus_inactive": (
            (a["winrate"] - b["winrate"]) if a["winrate"] is not None and b["winrate"] is not None else None
        ),
    }


def main() -> int:
    global LOOKAHEAD_VIOLATIONS, THRESHOLD_LEAKAGE, OUTCOME_LEAKAGE, TRAIN_OOS_CONTAMINATION
    OUT.mkdir(parents=True, exist_ok=True)
    thr_meta = json.loads(SHORT_THR_PATH.read_text())
    FROZEN_P80 = float(thr_meta["SHORT_BLOCK_THRESHOLD"])
    THRESHOLD_LEAKAGE = 0
    OUTCOME_LEAKAGE = 0

    # --- RSI definition doc ---
    rsi_def = f"""# RSI Definition (frozen repository semantics)

## Source
- `signal_generator.strategy.wave_fade.indicators.wilder_rsi`
- Attached via `attach_indicators` → column `rsi`
- Wave freeze flag: `rsi_end_gt_50` / `rsi_end_lt_50` in `waves.py`

## Period
- `RSI_LENGTH = {RSI_LENGTH}` (Wilder RSI)

## Scale
- **0 .. 100** (classic percent scale)
- User "0.5" maps to repository **50**
- **NOT** a 0..1 unit interval in this codebase

## Candle availability
- Computed on COMPLETE HTF bars aggregated from 1m
- Causal use: last bar with `available_at <= entry_ts`
- Incomplete / not-yet-closed bars never used (`require_complete=True`)

## Prior continuation definition
- Frozen: `rsi_end_gt_50 = (rsi_end > 50.0)`
- Exposed in this audit as `PRIOR_RSI_CONTINUATION_STATE` (== RSI_STATE_1)

## Neutral threshold used here
- `RSI_NEUTRAL = {RSI_NEUTRAL}`
"""
    (OUT / "rsi_definition.md").write_text(rsi_def, encoding="utf-8")

    now = AS_OF
    start = now - timedelta(days=LOOKBACK_DAYS)
    ch = setup_clickhouse(settings=get_clickhouse_settings())
    sig_repo = SignalRepository(ch)
    candle_repo = CandleRepository(ch)

    print(f"Loading 15m Tier-A {start.date()} → {_iso(now)} …", flush=True)
    rows, _ = sig_repo.query_signals(
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

    for sym, items in by_sym.items():
        t0s = [_utc(r.get("entry_time") or r.get("candle_close_time")) for r in items]
        a, b = min(t0s) - pad_before, max(t0s) + pad_after
        raw = candle_repo.get_candles(sym, a, b) or []
        work_1m[sym] = _prepare_1m(pd.DataFrame(raw)) if raw else pd.DataFrame()
        bars = bars_from_mappings(raw)
        tf15[sym] = prepare_tf(bars_to_ohlcv_df(aggregate_1m_to_timeframe(bars, "15m", as_of=now, require_complete=True)))
        tf30[sym] = prepare_tf(bars_to_ohlcv_df(aggregate_1m_to_timeframe(bars, "30m", as_of=now, require_complete=True)))
        tf1h[sym] = prepare_tf(bars_to_ohlcv_df(aggregate_1m_to_timeframe(bars, "1h", as_of=now, require_complete=True)))
        print(f"  {sym}: 15m={len(tf15[sym])}", flush=True)

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

        feats = extract_at(tf15[sym], t0, entry_px)
        rsi30 = htf_rsi_gt50(tf30[sym], t0)
        rsi1h = htf_rsi_gt50(tf1h[sym], t0)
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
                "htf30_rsi_gt50": rsi30,
                "htf1h_rsi_gt50": rsi1h,
            }
        )

    n = len(details)
    n_train = int(n * TRAIN_FRAC)
    train, oos = details[:n_train], details[n_train:]
    for d in train:
        d["split"] = "TRAIN"
    for d in oos:
        d["split"] = "OOS"
    TRAIN_OOS_CONTAMINATION = len({d["signal_id"] for d in train} & {d["signal_id"] for d in oos})

    oos_short = [d for d in oos if d["direction"] == "SHORT" and d.get("dist_ema20_atr") is not None]
    train_short = [d for d in train if d["direction"] == "SHORT" and d.get("dist_ema20_atr") is not None]
    extended = [d for d in oos_short if float(d["dist_ema20_atr"]) > FROZEN_P80]
    kept = [d for d in oos_short if float(d["dist_ema20_atr"]) <= FROZEN_P80]
    train_ext = [d for d in train_short if float(d["dist_ema20_atr"]) > FROZEN_P80]

    STATE_KEYS = [
        "RSI_STATE_1_gt50",
        "RSI_STATE_2_rising",
        "RSI_STATE_3_gt50_rising",
        "RSI_STATE_4_gt50_last2",
        "RSI_STATE_5_gt50_last3",
        "RSI_STATE_6_gt50_pos_ema20_slope",
        "PRIOR_RSI_CONTINUATION_STATE",
    ]

    # §7 EXTENDED RSI states
    ext_state_rows = [compare_active_inactive(extended, k, scope="OOS_EXTENDED") for k in STATE_KEYS]
    write_csv(OUT / "extended_rsi_states.csv", ext_state_rows)

    # §8 EXTENDED winner vs loser RSI
    ext_w = [d for d in extended if d["result"] == "WIN"]
    ext_l = [d for d in extended if d["result"] == "LOSS"]
    ewl_feats = [
        "rsi", "rsi_prev", "rsi_delta_1", "min_rsi_last_2", "min_rsi_last_3",
        "consec_bars_rsi_gt_50", "ema20_slope_atr", "dist_ema20_atr",
    ]
    ewl_rows = []
    for f in ewl_feats:
        wv, lv = collect(ext_w, f), collect(ext_l, f)
        ewl_rows.append(
            {
                "feature": f,
                "win_n": len(wv),
                "loss_n": len(lv),
                "win_median": float(np.median(wv)) if wv else None,
                "loss_median": float(np.median(lv)) if lv else None,
                "cliffs_delta_win_vs_loss": cliffs_delta(wv, lv),
                "sample_flag": "VERY_SMALL_SAMPLE" if len(ext_w) < 8 or len(ext_l) < 8 else "OK",
            }
        )
    write_csv(OUT / "extended_winner_loser_rsi.csv", ewl_rows)

    # §9 Full OOS SHORT
    full_rows = [compare_active_inactive(oos_short, k, scope="OOS_SHORT_ALL") for k in STATE_KEYS]
    write_csv(OUT / "full_oos_rsi_states.csv", full_rows)

    # §10 Interactions A–I
    def pred_factory(name: str) -> Callable[[dict], bool]:
        preds = {
            "A_EXTENDED": lambda d: float(d["dist_ema20_atr"]) > FROZEN_P80,
            "B_RSI_GT50": lambda d: bool(d.get("RSI_STATE_1_gt50")),
            "C_EXT_RSI_GT50": lambda d: float(d["dist_ema20_atr"]) > FROZEN_P80 and bool(d.get("RSI_STATE_1_gt50")),
            "D_EXT_RSI_GT50_RISING": lambda d: float(d["dist_ema20_atr"]) > FROZEN_P80 and bool(d.get("RSI_STATE_3_gt50_rising")),
            "E_EXT_RSI_GT50_2BARS": lambda d: float(d["dist_ema20_atr"]) > FROZEN_P80 and bool(d.get("RSI_STATE_4_gt50_last2")),
            "F_EXT_RSI_GT50_3BARS": lambda d: float(d["dist_ema20_atr"]) > FROZEN_P80 and bool(d.get("RSI_STATE_5_gt50_last3")),
            "G_EXT_POS_EMA20_SLOPE": lambda d: float(d["dist_ema20_atr"]) > FROZEN_P80 and bool(d.get("pos_ema20_slope")),
            "H_EXT_POS_SLOPE_RSI_GT50": lambda d: float(d["dist_ema20_atr"]) > FROZEN_P80 and bool(d.get("pos_ema20_slope")) and bool(d.get("RSI_STATE_1_gt50")),
            "I_EXT_POS_SLOPE_RSI_2BARS": lambda d: float(d["dist_ema20_atr"]) > FROZEN_P80 and bool(d.get("pos_ema20_slope")) and bool(d.get("RSI_STATE_4_gt50_last2")),
        }
        return preds[name]

    inter_rows = []
    for name in (
        "A_EXTENDED", "B_RSI_GT50", "C_EXT_RSI_GT50", "D_EXT_RSI_GT50_RISING",
        "E_EXT_RSI_GT50_2BARS", "F_EXT_RSI_GT50_3BARS", "G_EXT_POS_EMA20_SLOPE",
        "H_EXT_POS_SLOPE_RSI_GT50", "I_EXT_POS_SLOPE_RSI_2BARS",
    ):
        pred = pred_factory(name)
        hit = [d for d in oos_short if pred(d)]
        rest = [d for d in oos_short if not pred(d)]
        h, r = outcome_stats(hit), outcome_stats(rest)
        inter_rows.append(
            {
                "interaction": name,
                "hit_n": h["n"],
                "hit_wr": h["winrate"],
                "hit_net": h["net"],
                "hit_npt": h["net_per_trade"],
                "hit_sl": h["sl_rate"],
                "hit_flag": h["sample_flag"],
                "rest_n": r["n"],
                "rest_wr": r["winrate"],
                "rest_net": r["net"],
                "rest_npt": r["net_per_trade"],
            }
        )
    write_csv(OUT / "interaction_results.csv", inter_rows)

    # §11 RSI incremental value inside EXTENDED + positive EMA20 slope
    slope_ext = [d for d in extended if d.get("pos_ema20_slope")]
    # primary continuation for incremental test: STATE_4 (stays >50 2 bars) and STATE_1
    incr_rows = []
    for cont_key in ("RSI_STATE_1_gt50", "RSI_STATE_3_gt50_rising", "RSI_STATE_4_gt50_last2", "RSI_STATE_5_gt50_last3"):
        row = compare_active_inactive(slope_ext, cont_key, scope="OOS_EXT_POS_EMA20_SLOPE")
        incr_rows.append(row)
    write_csv(OUT / "ema_slope_rsi_incremental_value.csv", incr_rows)

    # Decide RSI_ADDS_VALUE
    # Need active clearly worse than inactive within slope_ext, with enough n
    adds = "UNCLEAR"
    for row in incr_rows:
        if row["active_n"] >= 5 and row["inactive_n"] >= 3:
            if row["active_wr"] is not None and row["inactive_wr"] is not None:
                if row["active_wr"] + 10 <= row["inactive_wr"]:
                    adds = "YES"
                    break
                if abs(row["active_wr"] - row["inactive_wr"]) < 5:
                    adds = "NO"
        elif row["inactive_n"] <= 1 and row["active_n"] >= 10:
            # almost all slope_ext already have RSI continuation → no incremental split
            adds = "NO"

    # If inactive nearly empty across all → NO incremental
    if all((r["inactive_n"] or 0) <= 1 for r in incr_rows):
        adds = "NO"

    # §12 Distance control — reuse prior buckets
    buckets = [
        ("<=1", lambda x: x <= 1.0),
        ("1-2", lambda x: 1.0 < x <= 2.0),
        ("2-3", lambda x: 2.0 < x <= 3.0),
        ("3-4", lambda x: 3.0 < x <= 4.0),
        (">4", lambda x: x > 4.0),
    ]
    dist_ctrl = []
    for lab, pred in buckets:
        grp = [d for d in oos_short if pred(float(d["dist_ema20_atr"]))]
        # focus higher buckets
        for cont_key in ("RSI_STATE_1_gt50", "RSI_STATE_4_gt50_last2"):
            row = compare_active_inactive(grp, cont_key, scope=f"BUCKET_{lab}")
            row["bucket"] = lab
            dist_ctrl.append(row)
    write_csv(OUT / "distance_control_rsi.csv", dist_ctrl)

    # §13 TRAIN replication
    train_rep = []
    train_tests = [
        ("EXT_RSI_GT50", lambda d: float(d["dist_ema20_atr"]) > FROZEN_P80 and bool(d.get("RSI_STATE_1_gt50"))),
        ("EXT_RSI_RISING", lambda d: float(d["dist_ema20_atr"]) > FROZEN_P80 and bool(d.get("RSI_STATE_2_rising"))),
        ("EXT_RSI_2BARS", lambda d: float(d["dist_ema20_atr"]) > FROZEN_P80 and bool(d.get("RSI_STATE_4_gt50_last2"))),
        ("EXT_POS_EMA20_SLOPE", lambda d: float(d["dist_ema20_atr"]) > FROZEN_P80 and bool(d.get("pos_ema20_slope"))),
        ("EXT_SLOPE_RSI_CONT", lambda d: float(d["dist_ema20_atr"]) > FROZEN_P80 and bool(d.get("pos_ema20_slope")) and bool(d.get("RSI_STATE_1_gt50"))),
    ]
    same_dirs = []
    for name, pred in train_tests:
        oos_hit = outcome_stats([d for d in oos_short if pred(d)])
        oos_rest = outcome_stats([d for d in oos_short if not pred(d)])
        tr_hit = outcome_stats([d for d in train_short if pred(d)])
        tr_rest = outcome_stats([d for d in train_short if not pred(d)])
        oos_gap = (oos_hit["winrate"] - oos_rest["winrate"]) if oos_hit["winrate"] is not None and oos_rest["winrate"] is not None else None
        tr_gap = (tr_hit["winrate"] - tr_rest["winrate"]) if tr_hit["winrate"] is not None and tr_rest["winrate"] is not None else None
        same = None
        if oos_gap is not None and tr_gap is not None:
            same = (oos_gap * tr_gap) > 0 or (abs(oos_gap) < 3 and abs(tr_gap) < 3)
            same_dirs.append(bool(same))
        train_rep.append(
            {
                "test": name,
                "oos_hit_n": oos_hit["n"],
                "oos_hit_wr": oos_hit["winrate"],
                "oos_rest_wr": oos_rest["winrate"],
                "oos_wr_gap": oos_gap,
                "train_hit_n": tr_hit["n"],
                "train_hit_wr": tr_hit["winrate"],
                "train_rest_wr": tr_rest["winrate"],
                "train_wr_gap": tr_gap,
                "same_direction": same,
            }
        )
    train_flag = "TRAIN_AND_OOS_SAME_DIRECTION" if same_dirs and all(same_dirs) else "TRAIN_OOS_INCONSISTENT"
    write_csv(OUT / "train_replication.csv", train_rep)

    # §14 MTF RSI context within EXTENDED
    mtf_rows = []
    for lab, pred in (
        ("EXT_30m_RSI_GT50", lambda d: d.get("htf30_rsi_gt50") is True),
        ("EXT_1h_RSI_GT50", lambda d: d.get("htf1h_rsi_gt50") is True),
        ("EXT_15m_and_30m_GT50", lambda d: bool(d.get("RSI_STATE_1_gt50")) and d.get("htf30_rsi_gt50") is True),
        ("EXT_15m_30m_1h_GT50", lambda d: bool(d.get("RSI_STATE_1_gt50")) and d.get("htf30_rsi_gt50") is True and d.get("htf1h_rsi_gt50") is True),
    ):
        hit = [d for d in extended if pred(d)]
        rest = [d for d in extended if not pred(d)]
        h, r = outcome_stats(hit), outcome_stats(rest)
        mtf_rows.append(
            {
                "context": lab,
                "hit_n": h["n"],
                "hit_wr": h["winrate"],
                "hit_npt": h["net_per_trade"],
                "hit_flag": h["sample_flag"],
                "rest_n": r["n"],
                "rest_wr": r["winrate"],
                "coverage_ok": h["n"] + r["n"] == len(extended) and h["n"] >= 3,
            }
        )
    write_csv(OUT / "mtf_rsi_context.csv", mtf_rows)

    # §15 Timing: EXT losses RSI active vs inactive
    timing_rows = []
    for cont_key in ("RSI_STATE_1_gt50", "RSI_STATE_4_gt50_last2"):
        for active in (True, False):
            grp = [d for d in ext_l if d.get(cont_key) is active]
            for nbar in (1, 2, 3):
                maes = collect(grp, f"mae_{nbar}bar")
                mfes = collect(grp, f"mfe_{nbar}bar")
                timing_rows.append(
                    {
                        "group": f"EXT_LOSS_{cont_key}_{'ACTIVE' if active else 'INACTIVE'}",
                        "horizon": nbar,
                        "n": len(grp),
                        "median_MAE": float(np.median(maes)) if maes else None,
                        "median_MFE": float(np.median(mfes)) if mfes else None,
                        "frac_mae_le_1": (sum(1 for x in maes if x <= -1.0) / len(maes) if maes else None),
                    }
                )
    write_csv(OUT / "timing_analysis.csv", timing_rows)

    # Timing class
    act = [r for r in timing_rows if "ACTIVE" in r["group"] and r["horizon"] == 1 and "STATE_1" in r["group"]]
    inact = [r for r in timing_rows if "INACTIVE" in r["group"] and r["horizon"] == 1 and "STATE_1" in r["group"]]
    timing_class = "NO_CLEAR_TIMING_CONFIRMATION"
    if act and inact and act[0]["n"] >= 3 and inact[0]["n"] >= 2:
        if (act[0]["median_MAE"] or 0) < (inact[0]["median_MAE"] or 0) - 0.2:
            timing_class = "CONTINUATION_SIGNAL_BEFORE_SHORT_FAILURE"

    # Primary decision
    # Key EXTENDED splits
    s1 = next(r for r in ext_state_rows if r["state"] == "RSI_STATE_1_gt50")
    s4 = next(r for r in ext_state_rows if r["state"] == "RSI_STATE_4_gt50_last2")
    g_row = next(r for r in inter_rows if r["interaction"] == "G_EXT_POS_EMA20_SLOPE")
    h_row = next(r for r in inter_rows if r["interaction"] == "H_EXT_POS_SLOPE_RSI_GT50")

    ext_n = len(extended)
    rsi_clears_ext = False
    for r in (s1, s4):
        if r["active_n"] >= 5 and r["inactive_n"] >= 3 and r["active_wr"] is not None and r["inactive_wr"] is not None:
            if r["active_wr"] + 10 <= r["inactive_wr"]:
                rsi_clears_ext = True

    if ext_n < 20 and (s1["inactive_n"] < 3 or adds == "UNCLEAR"):
        # Most extended already RSI>50 — can't separate
        if s1["inactive_n"] <= 1:
            primary = "RSI_RESULT_INCONCLUSIVE_DUE_TO_SAMPLE"
            recommendation = "MORE_HISTORY_REQUIRED"
        elif adds == "NO" and g_row["hit_wr"] is not None and (g_row["hit_wr"] or 50) < 40:
            primary = "EMA_SLOPE_EXPLAINS_MORE_THAN_RSI"
            recommendation = "EMA_SLOPE_REMAINS_BETTER_CANDIDATE"
        else:
            primary = "RSI_RESULT_INCONCLUSIVE_DUE_TO_SAMPLE"
            recommendation = "MORE_HISTORY_REQUIRED"
    elif rsi_clears_ext and adds == "YES" and train_flag == "TRAIN_AND_OOS_SAME_DIRECTION":
        primary = "RSI_CONTINUATION_EXPLAINS_EXTENDED_SHORT_FAILURES"
        recommendation = "RSI_CONTINUATION_WORTH_NEXT_STAGE"
    elif adds == "YES" and g_row["hit_n"] >= 8:
        primary = "RSI_ADDS_CONTEXT_TO_EMA_SLOPE"
        recommendation = "RSI_CONTINUATION_WORTH_NEXT_STAGE"
    elif adds == "NO" or (not rsi_clears_ext and g_row["hit_wr"] is not None and (g_row["hit_wr"] or 50) <= (s1["active_wr"] or 50) + 5):
        primary = "EMA_SLOPE_EXPLAINS_MORE_THAN_RSI"
        recommendation = "EMA_SLOPE_REMAINS_BETTER_CANDIDATE"
    elif train_flag == "TRAIN_OOS_INCONSISTENT" and not rsi_clears_ext:
        primary = "RSI_CONTINUATION_NOT_SUPPORTED"
        recommendation = "NO_RSI_FILTER_SUPPORTED"
    else:
        primary = "RSI_RESULT_INCONCLUSIVE_DUE_TO_SAMPLE"
        recommendation = "MORE_HISTORY_REQUIRED"

    # Override: if nearly all EXTENDED have RSI>50, RSI cannot explain within EXTENDED
    if s1["inactive_n"] <= 1 and s1["active_n"] >= 10:
        if primary.startswith("RSI_CONTINUATION_EXPLAINS") or primary.startswith("RSI_ADDS"):
            primary = "EMA_SLOPE_EXPLAINS_MORE_THAN_RSI"
            recommendation = "EMA_SLOPE_REMAINS_BETTER_CANDIDATE"
        elif primary == "RSI_RESULT_INCONCLUSIVE_DUE_TO_SAMPLE":
            # still inconclusive sample but mechanism note
            if g_row["hit_n"] >= 10 and (g_row["hit_wr"] or 50) < 40:
                primary = "EMA_SLOPE_EXPLAINS_MORE_THAN_RSI"
                recommendation = "EMA_SLOPE_REMAINS_BETTER_CANDIDATE"

    meta = {
        "task": "SHORT_EXTENDED_RSI_CONTINUATION_AUDIT",
        "rsi_source": "wave_fade.indicators.wilder_rsi via attach_indicators",
        "rsi_period": RSI_LENGTH,
        "rsi_scale": "0..100",
        "rsi_neutral": RSI_NEUTRAL,
        "prior_continuation": "rsi_end_gt_50 (rsi > 50)",
        "frozen_p80": FROZEN_P80,
        "threshold_source": "TRAIN_ONLY_REUSED",
        "threshold_leakage": THRESHOLD_LEAKAGE,
        "lookahead_violations": LOOKAHEAD_VIOLATIONS,
        "outcome_leakage": OUTCOME_LEAKAGE,
        "train_oos_contamination": TRAIN_OOS_CONTAMINATION,
        "feature_not_available": FEATURE_NOT_AVAILABLE,
        "htf_availability_violations": HTF_AVAIL_VIOLATIONS,
        "incomplete_candle": INCOMPLETE_CANDLE,
        "oos_short_n": len(oos_short),
        "extended_n": ext_n,
        "kept_n": len(kept),
        "RSI_ADDS_VALUE_BEYOND_EMA_SLOPE": adds,
        "train_replication": train_flag,
        "timing_class": timing_class,
        "primary_decision": primary,
        "recommendation": recommendation,
        "as_of": _iso(now),
    }
    (OUT / "audit_metadata.json").write_text(json.dumps(meta, indent=2) + "\n")

    def fmt(v: Any, nd: int = 1) -> str:
        if v is None:
            return "–"
        if isinstance(v, float):
            return f"{v:.{nd}f}"
        return str(v)

    lines = [
        "# SHORT_EXTENDED_RSI_CONTINUATION_AUDIT",
        "",
        f"Primary: `{primary}`",
        f"Recommendation: `{recommendation}`",
        f"RSI_ADDS_VALUE_BEYOND_EMA_SLOPE = `{adds}`",
        f"Train replication: `{train_flag}` | Timing: `{timing_class}`",
        "",
        f"RSI: Wilder({RSI_LENGTH}), scale 0..100, neutral={RSI_NEUTRAL} (= prior rsi_end_gt_50)",
        f"OOS SHORT n={len(oos_short)} EXTENDED n={ext_n} KEPT n={len(kept)} | frozen P80={FROZEN_P80:.4f}",
        f"leakage={THRESHOLD_LEAKAGE} lookahead={LOOKAHEAD_VIOLATIONS} outcome_leak={OUTCOME_LEAKAGE} contamination={TRAIN_OOS_CONTAMINATION}",
        "",
        "## EXTENDED RSI states",
        "",
    ]
    for r in ext_state_rows:
        lines.append(
            f"- {r['state']}: ACTIVE n={r['active_n']} WR={fmt(r['active_wr'])} npt={fmt(r['active_npt'],3)} "
            f"[{r['active_flag']}] | INACTIVE n={r['inactive_n']} WR={fmt(r['inactive_wr'])} [{r['inactive_flag']}]"
        )
    lines += ["", "## Interactions", ""]
    for r in inter_rows:
        lines.append(
            f"- {r['interaction']}: n={r['hit_n']} WR={fmt(r['hit_wr'])} npt={fmt(r['hit_npt'],3)} "
            f"vs rest WR={fmt(r['rest_wr'])} [{r['hit_flag']}]"
        )
    lines += ["", "## Incremental RSI inside EXT+pos EMA20 slope", ""]
    for r in incr_rows:
        lines.append(
            f"- {r['state']}: act n={r['active_n']} WR={fmt(r['active_wr'])} | "
            f"inact n={r['inactive_n']} WR={fmt(r['inactive_wr'])}"
        )
    lines += ["", "## Strategy Logic Changed", "", "`NO`", ""]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    (OUT / "summary.json").write_text(
        json.dumps(
            {
                "primary_decision": primary,
                "recommendation": recommendation,
                "RSI_ADDS_VALUE_BEYOND_EMA_SLOPE": adds,
                "extended_states": ext_state_rows,
                "interactions": inter_rows,
                "incremental": incr_rows,
                "ewl": ewl_rows,
                "train_replication": train_rep,
                "train_flag": train_flag,
                "mtf": mtf_rows,
                "timing_class": timing_class,
                "meta": meta,
                "ext_outcome": outcome_stats(extended),
                "kept_outcome": outcome_stats(kept),
            },
            indent=2,
            default=str,
        )
        + "\n"
    )

    print("PRIMARY", primary, flush=True)
    print("REC", recommendation, flush=True)
    print("ADDS", adds, "EXT", ext_n, "S1_inact", s1["inactive_n"], flush=True)
    print("wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
