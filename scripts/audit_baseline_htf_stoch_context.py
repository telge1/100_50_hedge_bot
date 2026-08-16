#!/usr/bin/env python3
"""AUDIT_BASELINE_LOSSES_VS_HIGHER_TF_STOCH_CONTEXT — research-only, no DB writes.

Analyzes whether 15m Tier-A BASELINE_IMMEDIATE losses coincide with
causal higher-TF (30m/1h/4h) StochRSI conflict. No strategy changes.
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

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
from signal_generator.strategy.wave_fade.indicators import stochastic_rsi  # noqa: E402
from signal_generator.strategy.wave_fade.parameters import PRIMARY_FEE  # noqa: E402
from signal_generator.timeframes import (  # noqa: E402
    aggregate_1m_to_timeframe,
    bars_from_mappings,
)

OUT = ROOT / "results" / "baseline_higher_tf_stoch_context_audit"
# Lock to prior 1m-state audit wall-clock so baseline n/WR/net reproduce.
PRIOR_AS_OF = datetime(2026, 8, 11, 9, 30, 7, 105630, tzinfo=timezone.utc)
HOURS = 168
FEE_PCT = float(PRIMARY_FEE)
HTF_TFS = ("30m", "1h", "4h")
CROSS_LOOKBACK = 3
FLAT_EPS = 1e-9

# Expected baseline from prior audit (STOP if not matched)
EXPECT_N = 189
EXPECT_WR = 61.1
EXPECT_NET = 20.6
TOL_WR = 0.15
TOL_NET = 0.5


def _iso(ts: Any | None) -> str | None:
    if ts is None:
        return None
    return _utc(ts).isoformat().replace("+00:00", "Z")


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    return float(np.median(np.asarray(xs, dtype=float)))


def _mean(xs: list[float]) -> float | None:
    if not xs:
        return None
    return float(np.mean(np.asarray(xs, dtype=float)))


def profit_factor(pnls: list[float]) -> float | None:
    gp = sum(p for p in pnls if p > 0)
    gl = sum(-p for p in pnls if p < 0)
    if gl == 0:
        return None if gp == 0 else float("inf")
    return float(gp / gl)


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


def zone_name(k: float) -> str:
    if k <= 20:
        return "0-20"
    if k <= 40:
        return "20-40"
    if k <= 60:
        return "40-60"
    if k <= 80:
        return "60-80"
    return "80-100"


def trend_name(delta: float | None) -> str:
    if delta is None or (isinstance(delta, float) and np.isnan(delta)):
        return "UNKNOWN"
    if delta > FLAT_EPS:
        return "RISING"
    if delta < -FLAT_EPS:
        return "FALLING"
    return "FLAT"


def recent_cross(k: np.ndarray, d: np.ndarray, i: int, *, bullish: bool) -> bool:
    lo = max(1, i - CROSS_LOOKBACK + 1)
    for j in range(lo, i + 1):
        if np.isnan(k[j - 1]) or np.isnan(d[j - 1]) or np.isnan(k[j]) or np.isnan(d[j]):
            continue
        if bullish and k[j - 1] <= d[j - 1] and k[j] > d[j]:
            return True
        if (not bullish) and k[j - 1] >= d[j - 1] and k[j] < d[j]:
            return True
    return False


def directional_state(k: float, d: float, k_trend: str) -> str:
    """Documented simple rule (no optimization):

    BULLISH: K > D AND K RISING
    BEARISH: K < D AND K FALLING
    NEUTRAL: otherwise
    """
    if k_trend == "UNKNOWN":
        return "NEUTRAL"
    if k > d and k_trend == "RISING":
        return "BULLISH"
    if k < d and k_trend == "FALLING":
        return "BEARISH"
    return "NEUTRAL"


def prepare_htf_stoch(bars_1m_raw: list[dict], *, tf: str, as_of: datetime) -> pd.DataFrame:
    """Aggregate 1m→HTF (complete closed only) and attach StochRSI."""
    bars = bars_from_mappings(bars_1m_raw)
    htf = aggregate_1m_to_timeframe(bars, tf, as_of=as_of, require_complete=True)
    df = bars_to_ohlcv_df(htf)
    if df.empty:
        return df
    k, d = stochastic_rsi(df["close"])
    df = df.copy()
    df["stoch_k"] = k
    df["stoch_d"] = d
    # available_at == close_time for causal use
    df["available_at"] = pd.to_datetime(df["available_at"], utc=True)
    return df.reset_index(drop=True)


def causal_htf_state(df: pd.DataFrame, *, as_of_entry: datetime) -> dict[str, Any]:
    """Last HTF bar with available_at <= entry_ts (no lookahead)."""
    empty = {
        "k": None,
        "d": None,
        "k_minus_d": None,
        "k_trend": None,
        "d_trend": None,
        "kd_orientation": None,
        "bullish_cross_recent": None,
        "bearish_cross_recent": None,
        "zone": None,
        "directional": None,
        "bar_open": None,
        "bar_available_at": None,
    }
    if df is None or df.empty:
        return empty
    t = pd.Timestamp(_utc(as_of_entry))
    # bars known at entry: available_at <= t
    known = df.loc[df["available_at"] <= t]
    if known.empty:
        return empty
    i = int(known.index[-1])  # position in original df if reset; use iloc on known
    # Use position within full df via last known row
    row = known.iloc[-1]
    # Find integer location in df
    idx = int(df.index.get_loc(row.name)) if row.name in df.index else len(known) - 1
    # Safer: rebuild arrays from known only for deltas
    k_arr = known["stoch_k"].to_numpy(dtype=float)
    d_arr = known["stoch_d"].to_numpy(dtype=float)
    j = len(known) - 1
    ki = float(k_arr[j]) if not np.isnan(k_arr[j]) else None
    di = float(d_arr[j]) if not np.isnan(d_arr[j]) else None
    if ki is None or di is None:
        return empty

    def delta(arr: np.ndarray) -> float | None:
        if j < 1 or np.isnan(arr[j]) or np.isnan(arr[j - 1]):
            return None
        return float(arr[j] - arr[j - 1])

    kd1 = delta(k_arr)
    dd1 = delta(d_arr)
    kt = trend_name(kd1)
    dt = trend_name(dd1)
    orient = "BULLISH" if ki > di else ("BEARISH" if ki < di else "FLAT")
    directional = directional_state(ki, di, kt)
    return {
        "k": ki,
        "d": di,
        "k_minus_d": ki - di,
        "k_trend": kt,
        "d_trend": dt,
        "kd_orientation": orient,
        "bullish_cross_recent": recent_cross(k_arr, d_arr, j, bullish=True),
        "bearish_cross_recent": recent_cross(k_arr, d_arr, j, bullish=False),
        "zone": zone_name(ki),
        "directional": directional,
        "bar_open": _iso(row["timestamp"]),
        "bar_available_at": _iso(row["available_at"]),
        "_df_idx": idx,
    }


def is_conflict(side: str, directional: str | None) -> bool:
    if not directional:
        return False
    if side == "LONG":
        return directional == "BEARISH"
    return directional == "BULLISH"


def is_support(side: str, directional: str | None) -> bool:
    if not directional:
        return False
    if side == "LONG":
        return directional == "BULLISH"
    return directional == "BEARISH"


def adverse_mae_within(
    side: str, entry: float, work: pd.DataFrame, entry_i: int, minutes: int
) -> float | None:
    if entry_i < 0 or entry_i >= len(work) or entry <= 0:
        return None
    end_i = min(len(work) - 1, entry_i + minutes)
    highs = work["high"].to_numpy(dtype=float)[entry_i : end_i + 1]
    lows = work["low"].to_numpy(dtype=float)[entry_i : end_i + 1]
    if highs.size == 0:
        return None
    if side == "LONG":
        return float(np.min((lows / entry - 1.0) * 100.0))
    return float(np.min(-((highs - entry) / entry * 100.0)))


def summarize(rows: list[dict[str, Any]], *, label: str) -> dict[str, Any]:
    n = len(rows)
    wins = sum(1 for r in rows if r.get("result") == "WIN")
    losses = sum(1 for r in rows if r.get("result") == "LOSS")
    closed = wins + losses
    pnls = [float(r["pnl"]) for r in rows if r.get("pnl") is not None]
    maes = [float(r["mae"]) for r in rows if r.get("mae") is not None]
    mfes = [float(r["mfe"]) for r in rows if r.get("mfe") is not None]
    dd1 = sum(1 for r in rows if r.get("early_dd_1pct"))
    dd2 = sum(1 for r in rows if r.get("early_dd_2pct"))
    dd3 = sum(1 for r in rows if r.get("early_dd_3pct"))
    return {
        "group": label,
        "count": n,
        "winrate": (100.0 * wins / closed) if closed else None,
        "net": float(sum(pnls) - FEE_PCT * len(pnls)) if pnls else 0.0,
        "gross": float(sum(pnls)) if pnls else 0.0,
        "profit_factor": profit_factor(pnls) if pnls else None,
        "sl_rate": (100.0 * losses / closed) if closed else None,
        "tp_rate": (100.0 * wins / closed) if closed else None,
        "median_MAE": _median(maes),
        "median_MFE": _median(mfes),
        "early_dd_1pct_rate": (100.0 * dd1 / n) if n else None,
        "early_dd_2pct_rate": (100.0 * dd2 / n) if n else None,
        "early_dd_3pct_rate": (100.0 * dd3 / n) if n else None,
        "wins": wins,
        "losses": losses,
    }


def state_label(st: dict[str, Any]) -> str:
    if st.get("k") is None:
        return "NA"
    return f"z{st['zone']}_{st['k_trend']}_{st['directional']}"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    now = PRIOR_AS_OF
    start = now - timedelta(hours=HOURS)

    ch = setup_clickhouse(settings=get_clickhouse_settings())
    sig_repo = SignalRepository(ch)
    candle_repo = CandleRepository(ch)

    print(f"Loading Tier-A {start.isoformat()} → {now.isoformat()} (locked as_of) …", flush=True)
    rows, total = sig_repo.query_signals(
        start=start,
        end=now,
        tier_a=True,
        timeframe="15m",
        time_field="candle_close_time",
        limit=5000,
        offset=0,
    )
    api_rows = [_signal_row_to_api(r) for r in rows]
    api_rows = [r for r in api_rows if str(r.get("timeframe")) == "15m"]
    print(f"15m Tier-A: {len(api_rows)} (query total={total})", flush=True)

    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in api_rows:
        by_sym[str(r["symbol"]).upper()].append(r)

    # Warmup for 4h StochRSI: ~40 bars * 4h ≈ 7d → pad 14d
    pad_before = timedelta(days=14)
    pad_after = timedelta(hours=36)
    raw_1m: dict[str, list[dict]] = {}
    work_1m: dict[str, pd.DataFrame] = {}
    htf_cache: dict[str, dict[str, pd.DataFrame]] = {}

    for sym, items in by_sym.items():
        t0s = [_utc(r.get("entry_time") or r.get("candle_close_time")) for r in items]
        a, b = min(t0s) - pad_before, max(t0s) + pad_after
        raw = candle_repo.get_candles(sym, a, b)
        raw_1m[sym] = raw or []
        work_1m[sym] = _prepare_1m(pd.DataFrame(raw)) if raw else pd.DataFrame()
        htf_cache[sym] = {}
        for tf in HTF_TFS:
            htf_cache[sym][tf] = prepare_htf_stoch(raw_1m[sym], tf=tf, as_of=now)
            print(f"  {sym} {tf}: {len(htf_cache[sym][tf])} bars", flush=True)
        print(f"  {sym} 1m: {len(work_1m[sym])}", flush=True)

    details: list[dict[str, Any]] = []

    for r in api_rows:
        sym = str(r["symbol"]).upper()
        side = str(r["direction"]).upper()
        sid = str(r["signal_id"])
        signal_ts = _utc(r.get("candle_close_time") or r.get("generated_at"))
        t0 = _utc(r.get("entry_time") or signal_ts)
        try:
            baseline_px = float(r["entry_price"]) if r.get("entry_price") is not None else None
        except (TypeError, ValueError):
            baseline_px = None

        work = work_1m.get(sym, pd.DataFrame())
        entry_i = -1
        if not work.empty:
            entry_i = int(work["timestamp"].searchsorted(pd.Timestamp(t0), side="left"))
            if entry_i >= len(work):
                entry_i = -1

        entry_px = baseline_px
        result, pnl, mae, mfe, exit_reason = "OPEN", None, None, None, None
        if entry_i >= 0 and not work.empty:
            if entry_px is None:
                entry_px = float(work.iloc[entry_i]["open"])
            sim = _simulate_outcome(
                side=side, entry=float(entry_px), tf="15m", entry_i=entry_i, work=work, as_of=now
            )
            result = sim["result"]
            pnl = sim.get("pnl_pct")
            mae = sim.get("mae_pct")
            mfe = sim.get("mfe_pct")
            exit_reason = sim.get("exit_reason")

        mae30 = (
            adverse_mae_within(side, float(entry_px), work, entry_i, 30)
            if entry_i >= 0 and entry_px
            else None
        )
        early_dd_1 = bool(mae30 is not None and mae30 <= -1.0)
        early_dd_2 = bool(mae30 is not None and mae30 <= -2.0)
        early_dd_3 = bool(mae30 is not None and mae30 <= -3.0)

        htf_states: dict[str, dict] = {}
        for tf in HTF_TFS:
            htf_states[tf] = causal_htf_state(htf_cache[sym][tf], as_of_entry=t0)

        conflicts = {tf: is_conflict(side, htf_states[tf].get("directional")) for tf in HTF_TFS}
        supports = {tf: is_support(side, htf_states[tf].get("directional")) for tf in HTF_TFS}
        conflict_count = sum(1 for tf in HTF_TFS if conflicts[tf])
        support_count = sum(1 for tf in HTF_TFS if supports[tf])

        c30, c1h, c4h = conflicts["30m"], conflicts["1h"], conflicts["4h"]
        conflict_pattern = (
            f"{'30' if c30 else '-'}"
            f"{'1h' if c1h else '-'}"
            f"{'4h' if c4h else '-'}"
        )

        rec: dict[str, Any] = {
            "signal_id": sid,
            "symbol": sym,
            "direction": side,
            "signal_tf": "15m",
            "signal_ts": _iso(signal_ts),
            "entry_ts": _iso(t0),
            "entry_price": entry_px,
            "result": result,
            "pnl": pnl,
            "mae": mae,
            "mfe": mfe,
            "exit_reason": exit_reason,
            "mae_30m_pct": mae30,
            "early_dd_1pct": early_dd_1,
            "early_dd_2pct": early_dd_2,
            "early_dd_3pct": early_dd_3,
            "htf_support_count": support_count,
            "htf_conflict_count": conflict_count,
            "conflict_30m": c30,
            "conflict_1h": c1h,
            "conflict_4h": c4h,
            "support_30m": supports["30m"],
            "support_1h": supports["1h"],
            "support_4h": supports["4h"],
            "conflict_pattern": conflict_pattern,
            "any_htf_conflict": conflict_count >= 1,
            "all_htf_conflict": conflict_count == 3,
            "no_htf_conflict": conflict_count == 0,
        }
        for tf in HTF_TFS:
            st = htf_states[tf]
            p = tf.replace("m", "m").replace("h", "h")  # noqa: keep names
            prefix = {"30m": "m30", "1h": "h1", "4h": "h4"}[tf]
            rec[f"{prefix}_k"] = st["k"]
            rec[f"{prefix}_d"] = st["d"]
            rec[f"{prefix}_k_minus_d"] = st["k_minus_d"]
            rec[f"{prefix}_k_trend"] = st["k_trend"]
            rec[f"{prefix}_d_trend"] = st["d_trend"]
            rec[f"{prefix}_kd_orientation"] = st["kd_orientation"]
            rec[f"{prefix}_bullish_cross_recent"] = st["bullish_cross_recent"]
            rec[f"{prefix}_bearish_cross_recent"] = st["bearish_cross_recent"]
            rec[f"{prefix}_zone"] = st["zone"]
            rec[f"{prefix}_directional"] = st["directional"]
            rec[f"{prefix}_state"] = state_label(st)
            rec[f"{prefix}_bar_available_at"] = st["bar_available_at"]

        # Loss class (filled later only for losses, but compute for all)
        if conflict_count >= 2:
            loss_class = "LOSS_WITH_STRONG_HTF_CONFLICT"
        elif support_count >= 2 and conflict_count == 0:
            loss_class = "LOSS_DESPITE_HTF_ALIGNMENT"
        else:
            loss_class = "LOSS_WITH_MIXED_HTF"
        rec["htf_loss_class"] = loss_class  # meaningful for SL rows
        details.append(rec)

    # --- Baseline gate ---
    closed = [d for d in details if d["result"] in ("WIN", "LOSS")]
    wins = [d for d in details if d["result"] == "WIN"]
    losses = [d for d in details if d["result"] == "LOSS"]
    pnls = [float(d["pnl"]) for d in details if d.get("pnl") is not None]
    n = len(details)
    wr = (100.0 * len(wins) / len(closed)) if closed else None
    net = float(sum(pnls) - FEE_PCT * len(pnls)) if pnls else 0.0
    print(f"BASELINE n={n} wr={wr} net={net} wins={len(wins)} losses={len(losses)}", flush=True)

    if n != EXPECT_N or wr is None or abs(wr - EXPECT_WR) > TOL_WR or abs(net - EXPECT_NET) > TOL_NET:
        msg = {
            "error": "BASELINE_NOT_REPRODUCED",
            "got": {"n": n, "winrate": wr, "net": net, "wins": len(wins), "losses": len(losses)},
            "expected": {"n": EXPECT_N, "winrate": EXPECT_WR, "net": EXPECT_NET},
            "as_of": _iso(now),
        }
        (OUT / "STOP_baseline_mismatch.json").write_text(json.dumps(msg, indent=2) + "\n")
        print("STOP: baseline not reproduced", msg, flush=True)
        return 2

    write_csv(OUT / "signal_detail.csv", details)

    # Loss detail
    loss_rows = []
    for d in losses:
        loss_rows.append(
            {
                "signal_id": d["signal_id"],
                "symbol": d["symbol"],
                "side": d["direction"],
                "entry_ts": d["entry_ts"],
                "entry_price": d["entry_price"],
                "30m_k": d["m30_k"],
                "30m_d": d["m30_d"],
                "30m_state": d["m30_state"],
                "1h_k": d["h1_k"],
                "1h_d": d["h1_d"],
                "1h_state": d["h1_state"],
                "4h_k": d["h4_k"],
                "4h_d": d["h4_d"],
                "4h_state": d["h4_state"],
                "htf_support_count": d["htf_support_count"],
                "htf_conflict_count": d["htf_conflict_count"],
                "MAE": d["mae"],
                "MFE": d["mfe"],
                "outcome": d["result"],
                "loss_class": d["htf_loss_class"],
                "early_dd_2pct": d["early_dd_2pct"],
                "early_dd_3pct": d["early_dd_3pct"],
            }
        )
    write_csv(OUT / "loss_detail.csv", loss_rows)

    # Winner vs loser state distributions (by side, by TF)
    wvl_rows: list[dict[str, Any]] = []
    for side in ("LONG", "SHORT"):
        for outcome, subset_name in (("WIN", "WINNERS"), ("LOSS", "LOSERS")):
            sub = [d for d in details if d["direction"] == side and d["result"] == outcome]
            for tf, pref in (("30m", "m30"), ("1h", "h1"), ("4h", "h4")):
                # zone
                zones: dict[str, int] = defaultdict(int)
                dirs: dict[str, int] = defaultdict(int)
                trends: dict[str, int] = defaultdict(int)
                combo: dict[str, int] = defaultdict(int)
                for d in sub:
                    z = d.get(f"{pref}_zone") or "NA"
                    dr = d.get(f"{pref}_directional") or "NA"
                    tr = d.get(f"{pref}_k_trend") or "NA"
                    zones[str(z)] += 1
                    dirs[str(dr)] += 1
                    trends[str(tr)] += 1
                    combo[f"{z}|{tr}|{dr}"] += 1
                for z, c in sorted(zones.items()):
                    wvl_rows.append(
                        {
                            "side": side,
                            "outcome": subset_name,
                            "tf": tf,
                            "feature": "zone",
                            "value": z,
                            "count": c,
                            "pct": (100.0 * c / len(sub)) if sub else None,
                            "n_subset": len(sub),
                        }
                    )
                for v, c in sorted(dirs.items()):
                    wvl_rows.append(
                        {
                            "side": side,
                            "outcome": subset_name,
                            "tf": tf,
                            "feature": "directional",
                            "value": v,
                            "count": c,
                            "pct": (100.0 * c / len(sub)) if sub else None,
                            "n_subset": len(sub),
                        }
                    )
                for v, c in sorted(trends.items()):
                    wvl_rows.append(
                        {
                            "side": side,
                            "outcome": subset_name,
                            "tf": tf,
                            "feature": "k_trend",
                            "value": v,
                            "count": c,
                            "pct": (100.0 * c / len(sub)) if sub else None,
                            "n_subset": len(sub),
                        }
                    )
                for v, c in sorted(combo.items(), key=lambda x: -x[1])[:8]:
                    wvl_rows.append(
                        {
                            "side": side,
                            "outcome": subset_name,
                            "tf": tf,
                            "feature": "zone_trend_dir",
                            "value": v,
                            "count": c,
                            "pct": (100.0 * c / len(sub)) if sub else None,
                            "n_subset": len(sub),
                        }
                    )
    write_csv(OUT / "winner_vs_loser_summary.csv", wvl_rows)

    # Conflict group summaries
    def pick(pred) -> list[dict]:
        return [d for d in details if pred(d)]

    conflict_groups = {
        "ALL": pick(lambda d: True),
        "NO_HTF_CONFLICT": pick(lambda d: d["no_htf_conflict"]),
        "ANY_HTF_CONFLICT": pick(lambda d: d["any_htf_conflict"]),
        "ALL_HTF_CONFLICT": pick(lambda d: d["all_htf_conflict"]),
        "30m_conflict_only": pick(lambda d: d["conflict_30m"] and not d["conflict_1h"] and not d["conflict_4h"]),
        "1h_conflict_only": pick(lambda d: d["conflict_1h"] and not d["conflict_30m"] and not d["conflict_4h"]),
        "4h_conflict_only": pick(lambda d: d["conflict_4h"] and not d["conflict_30m"] and not d["conflict_1h"]),
        "30m_conflict": pick(lambda d: d["conflict_30m"]),
        "1h_conflict": pick(lambda d: d["conflict_1h"]),
        "4h_conflict": pick(lambda d: d["conflict_4h"]),
        "30m+1h_conflict": pick(lambda d: d["conflict_30m"] and d["conflict_1h"]),
        "1h+4h_conflict": pick(lambda d: d["conflict_1h"] and d["conflict_4h"]),
        "30m+1h+4h_conflict": pick(lambda d: d["all_htf_conflict"]),
        "LONG_ALL": pick(lambda d: d["direction"] == "LONG"),
        "SHORT_ALL": pick(lambda d: d["direction"] == "SHORT"),
        "LONG_ANY_CONFLICT": pick(lambda d: d["direction"] == "LONG" and d["any_htf_conflict"]),
        "LONG_NO_CONFLICT": pick(lambda d: d["direction"] == "LONG" and d["no_htf_conflict"]),
        "SHORT_ANY_CONFLICT": pick(lambda d: d["direction"] == "SHORT" and d["any_htf_conflict"]),
        "SHORT_NO_CONFLICT": pick(lambda d: d["direction"] == "SHORT" and d["no_htf_conflict"]),
    }
    conflict_summary = [summarize(v, label=k) for k, v in conflict_groups.items()]
    write_csv(OUT / "htf_conflict_summary.csv", conflict_summary)

    # Alignment count
    align_rows = []
    for sc in (0, 1, 2, 3):
        sub = [d for d in details if d["htf_support_count"] == sc]
        align_rows.append(summarize(sub, label=f"support_count_{sc}"))
    for side in ("LONG", "SHORT"):
        for sc in (0, 1, 2, 3):
            sub = [d for d in details if d["direction"] == side and d["htf_support_count"] == sc]
            align_rows.append(summarize(sub, label=f"{side}_support_count_{sc}"))
    write_csv(OUT / "alignment_count_summary.csv", align_rows)

    # Position x direction deep slices (selected)
    pos_rows = []
    for side in ("LONG", "SHORT"):
        for tf, pref in (("30m", "m30"), ("1h", "h1"), ("4h", "h4")):
            for zone in ("0-20", "20-40", "40-60", "60-80", "80-100"):
                for tr in ("RISING", "FALLING", "FLAT"):
                    sub = [
                        d
                        for d in details
                        if d["direction"] == side
                        and d.get(f"{pref}_zone") == zone
                        and d.get(f"{pref}_k_trend") == tr
                    ]
                    if len(sub) < 3:
                        continue
                    s = summarize(sub, label=f"{side}|{tf}|z{zone}|{tr}")
                    pos_rows.append(s)
    write_csv(OUT / "htf_position_trend_slices.csv", pos_rows)

    # Loss class counts
    loss_class_counts = defaultdict(int)
    for d in losses:
        loss_class_counts[d["htf_loss_class"]] += 1

    # Counterfactual filters
    baseline_s = summarize(details, label="BASELINE_IMMEDIATE")

    def counterfactual(name: str, block_pred) -> dict[str, Any]:
        kept = [d for d in details if not block_pred(d)]
        blocked = [d for d in details if block_pred(d)]
        w_block = sum(1 for d in blocked if d["result"] == "WIN")
        l_block = sum(1 for d in blocked if d["result"] == "LOSS")
        s = summarize(kept, label=name)
        base_net = baseline_s["net"] or 0.0
        return {
            "variant": name,
            "trades": s["count"],
            "coverage_pct": (100.0 * s["count"] / n) if n else None,
            "winrate": s["winrate"],
            "net": s["net"],
            "profit_factor": s["profit_factor"],
            "sl_rate": s["sl_rate"],
            "median_MAE": s["median_MAE"],
            "winners_blocked": w_block,
            "losers_blocked": l_block,
            "net_effect_if_blocked": float(s["net"] - base_net),
            "blocked_count": len(blocked),
        }

    filters = [
        ("A_block_4h_conflict", lambda d: d["conflict_4h"]),
        ("B_block_1h_and_4h_conflict", lambda d: d["conflict_1h"] and d["conflict_4h"]),
        ("C_block_all_htf_conflict", lambda d: d["all_htf_conflict"]),
        ("D_require_ge1_htf_support", lambda d: d["htf_support_count"] < 1),
        ("E_require_ge2_htf_support", lambda d: d["htf_support_count"] < 2),
    ]

    # Always compute counterfactuals (fixed set); decision uses clarity later
    cf_rows = [
        {
            "variant": "BASELINE_IMMEDIATE",
            "trades": baseline_s["count"],
            "coverage_pct": 100.0,
            "winrate": baseline_s["winrate"],
            "net": baseline_s["net"],
            "profit_factor": baseline_s["profit_factor"],
            "sl_rate": baseline_s["sl_rate"],
            "median_MAE": baseline_s["median_MAE"],
            "winners_blocked": 0,
            "losers_blocked": 0,
            "net_effect_if_blocked": 0.0,
            "blocked_count": 0,
        }
    ]
    for name, pred in filters:
        cf_rows.append(counterfactual(name, pred))
    write_csv(OUT / "counterfactual_filters.csv", cf_rows)

    # Best filter among those that improve net and block more losers than winners
    candidates = [
        r
        for r in cf_rows
        if r["variant"] != "BASELINE_IMMEDIATE"
        and (r.get("net_effect_if_blocked") or 0) > 0.5
        and (r.get("losers_blocked") or 0) > (r.get("winners_blocked") or 0)
    ]
    best_cf = max(candidates, key=lambda r: r["net_effect_if_blocked"]) if candidates else None

    # Clarity of conflict explanation
    no_c = next(r for r in conflict_summary if r["group"] == "NO_HTF_CONFLICT")
    any_c = next(r for r in conflict_summary if r["group"] == "ANY_HTF_CONFLICT")
    all_c = next(r for r in conflict_summary if r["group"] == "ALL_HTF_CONFLICT")
    c4 = next(r for r in conflict_summary if r["group"] == "4h_conflict")

    n_loss_strong = loss_class_counts.get("LOSS_WITH_STRONG_HTF_CONFLICT", 0)
    n_loss_align = loss_class_counts.get("LOSS_DESPITE_HTF_ALIGNMENT", 0)
    n_loss_mixed = loss_class_counts.get("LOSS_WITH_MIXED_HTF", 0)

    wr_gap = (no_c["winrate"] or 0) - (any_c["winrate"] or 0)
    # 4h conflict alone: if WR not worse → confirms prior finding
    four_h_invalidates = (c4["winrate"] or 100) < (baseline_s["winrate"] or 0) - 5

    # Support monotonicity
    sc_stats = [next(r for r in align_rows if r["group"] == f"support_count_{i}") for i in range(4)]
    support_improves = False
    if all((sc_stats[i]["count"] or 0) >= 5 for i in (0, 2, 3)):
        support_improves = (sc_stats[3]["winrate"] or 0) > (sc_stats[0]["winrate"] or 0) + 5 and (
            sc_stats[3]["net"] or 0
        ) / max(sc_stats[3]["count"], 1) > (sc_stats[0]["net"] or 0) / max(sc_stats[0]["count"], 1)

    long_any = next(r for r in conflict_summary if r["group"] == "LONG_ANY_CONFLICT")
    long_no = next(r for r in conflict_summary if r["group"] == "LONG_NO_CONFLICT")
    short_any = next(r for r in conflict_summary if r["group"] == "SHORT_ANY_CONFLICT")
    short_no = next(r for r in conflict_summary if r["group"] == "SHORT_NO_CONFLICT")
    mixed_by_dir = False
    if (long_any["count"] or 0) >= 10 and (short_any["count"] or 0) >= 10:
        long_gap = (long_no["winrate"] or 0) - (long_any["winrate"] or 0)
        short_gap = (short_no["winrate"] or 0) - (short_any["winrate"] or 0)
        # opposite signs with meaningful magnitude
        if (long_gap > 5 and short_gap < -5) or (short_gap > 5 and long_gap < -5):
            mixed_by_dir = True

    # All fixed filters hurt net? (fade setup is structurally "conflict")
    filter_deltas = [
        float(r.get("net_effect_if_blocked") or 0)
        for r in cf_rows
        if r["variant"] != "BASELINE_IMMEDIATE"
    ]
    all_filters_hurt = bool(filter_deltas) and all(x <= 0 for x in filter_deltas)
    any_conflict_dominates = (any_c["count"] or 0) >= 0.85 * n
    all_conflict_not_worse = (all_c["winrate"] or 0) >= (baseline_s["winrate"] or 0) - 2

    if n < 50:
        primary = "INSUFFICIENT_SAMPLE"
        recommendation = "KEEP_BASELINE"
    elif mixed_by_dir:
        primary = "MIXED_BY_DIRECTION_OR_TIMEFRAME"
        recommendation = "KEEP_BASELINE"
    elif all_filters_hurt and (
        min(filter_deltas) <= -3 or (any_conflict_dominates and all_conflict_not_worse)
    ):
        # Blocking HTF "conflict" removes more winners than it saves — fade DNA.
        primary = "HTF_ALIGNMENT_FILTER_HURTS_EDGE"
        recommendation = "KEEP_BASELINE"
    elif best_cf and (best_cf["net_effect_if_blocked"] or 0) > 3 and (
        best_cf["losers_blocked"] >= best_cf["winners_blocked"] + 3
    ):
        if (best_cf["coverage_pct"] or 0) < 50:
            primary = "HTF_ALIGNMENT_FILTER_HURTS_EDGE"
            recommendation = "KEEP_BASELINE"
        else:
            primary = "HTF_STOCH_CONTEXT_WEAKLY_EXPLAINS_LOSSES"
            recommendation = "HTF_CONTEXT_WORTH_FURTHER_TESTING"
    elif n_loss_align >= max(5, int(0.25 * len(losses))) and n_loss_align >= n_loss_strong:
        primary = "HTF_STOCH_CONFLICT_DOES_NOT_EXPLAIN_LOSSES"
        recommendation = "KEEP_BASELINE"
    elif (
        (no_c["count"] or 0) >= 15
        and wr_gap >= 8
        and (no_c["net"] or 0) > (any_c["net"] or 0) + 2
        and support_improves
    ):
        primary = "HTF_STOCH_CONFLICT_EXPLAINS_LOSSES"
        recommendation = "HTF_CONTEXT_WORTH_FURTHER_TESTING"
    elif (no_c["count"] or 0) >= 15 and wr_gap >= 3 and best_cf and (
        best_cf["net_effect_if_blocked"] or 0
    ) > 1:
        primary = "HTF_STOCH_CONTEXT_WEAKLY_EXPLAINS_LOSSES"
        recommendation = "HTF_CONTEXT_WORTH_FURTHER_TESTING"
    elif any_conflict_dominates and all_conflict_not_worse:
        primary = "HTF_STOCH_CONFLICT_DOES_NOT_EXPLAIN_LOSSES"
        recommendation = "KEEP_BASELINE"
    else:
        e = next(r for r in cf_rows if r["variant"] == "E_require_ge2_htf_support")
        if (e["net_effect_if_blocked"] or 0) < -3:
            primary = "HTF_ALIGNMENT_FILTER_HURTS_EDGE"
        else:
            primary = "HTF_STOCH_CONFLICT_DOES_NOT_EXPLAIN_LOSSES"
        recommendation = "KEEP_BASELINE"

    # Confirm prior 4h finding
    prior_4h_note = (
        "CONFIRMED: 4h conflict alone does NOT clearly invalidate 15m fade "
        f"(4h_conflict WR={c4['winrate']}, baseline WR={baseline_s['winrate']})"
        if not four_h_invalidates
        else "UPDATED: 4h conflict looks worse than baseline on this sample"
    )

    # Dominant winner/loser states for report
    def top_states(side: str, outcome: str, tf: str, feature: str = "directional", top: int = 3) -> list[str]:
        rows = [
            r
            for r in wvl_rows
            if r["side"] == side and r["outcome"] == outcome and r["tf"] == tf and r["feature"] == feature
        ]
        rows = sorted(rows, key=lambda x: -x["count"])[:top]
        return [f"{r['value']}={r['count']}({r['pct']:.0f}%)" for r in rows]

    payload = {
        "primary_decision": primary,
        "recommendation": recommendation,
        "prior_4h_finding": prior_4h_note,
        "baseline": {
            "n": n,
            "winrate": wr,
            "net": net,
            "wins": len(wins),
            "losses": len(losses),
            "profit_factor": baseline_s["profit_factor"],
            "sl_rate": baseline_s["sl_rate"],
        },
        "directional_rule": "BULLISH: K>D AND K RISING; BEARISH: K<D AND K FALLING; else NEUTRAL",
        "conflict_rule": "LONG_CONFLICT if HTF BEARISH; SHORT_CONFLICT if HTF BULLISH",
        "loss_class_counts": dict(loss_class_counts),
        "no_vs_any": {"NO_HTF_CONFLICT": no_c, "ANY_HTF_CONFLICT": any_c, "ALL_HTF_CONFLICT": all_c},
        "alignment": sc_stats,
        "best_counterfactual": best_cf,
        "counterfactuals": cf_rows,
        "conflict_summary": conflict_summary,
        "as_of": _iso(now),
        "hours": HOURS,
        "fee_pct": FEE_PCT,
    }
    (OUT / "summary.json").write_text(json.dumps(payload, indent=2, default=str) + "\n")

    def fmt(v: Any, nd: int = 1) -> str:
        if v is None:
            return "–"
        if isinstance(v, float):
            if abs(v) == float("inf"):
                return "inf"
            return f"{v:.{nd}f}"
        return str(v)

    lines = [
        "# AUDIT_BASELINE_LOSSES_VS_HIGHER_TF_STOCH_CONTEXT",
        "",
        f"as_of locked: `{_iso(now)}` | hours={HOURS} | TF=15m Tier-A BASELINE_IMMEDIATE",
        "",
        "## Primary Decision",
        "",
        f"`{primary}`",
        "",
        f"Recommendation: `{recommendation}`",
        "",
        f"Prior 4h note: {prior_4h_note}",
        "",
        "## Directional / conflict rules",
        "",
        f"- Directional: `{payload['directional_rule']}`",
        f"- Conflict: `{payload['conflict_rule']}`",
        "",
        "## Baseline",
        "",
        f"- n={n} | WR={fmt(wr)} | Net={fmt(net)} | losses={len(losses)}",
        "",
        "## NO vs ANY vs ALL conflict",
        "",
        "| Group | n | WR | Net | SL | Med MAE | DD2 |",
        "| ----- | -: | -: | --: | -: | ------: | --: |",
    ]
    for row in (no_c, any_c, all_c):
        lines.append(
            f"| {row['group']} | {row['count']} | {fmt(row['winrate'])} | {fmt(row['net'])} | "
            f"{fmt(row['sl_rate'])} | {fmt(row['median_MAE'], 3)} | {fmt(row['early_dd_2pct_rate'])} |"
        )

    lines += [
        "",
        "## Support count",
        "",
        "| Support | n | WR | Net | SL | Med MAE |",
        "| ------- | -: | -: | --: | -: | ------: |",
    ]
    for row in sc_stats:
        lines.append(
            f"| {row['group']} | {row['count']} | {fmt(row['winrate'])} | {fmt(row['net'])} | "
            f"{fmt(row['sl_rate'])} | {fmt(row['median_MAE'], 3)} |"
        )

    lines += [
        "",
        "## Loss classes",
        "",
        f"- STRONG_HTF_CONFLICT: **{n_loss_strong}**",
        f"- MIXED_HTF: **{n_loss_mixed}**",
        f"- DESPITE_HTF_ALIGNMENT: **{n_loss_align}**",
        "",
        "## Counterfactual filters",
        "",
        "| Variant | trades | cov% | WR | Net | ΔNet | losers_blocked | winners_blocked |",
        "| ------- | -----: | ---: | -: | --: | ---: | -------------: | --------------: |",
    ]
    for row in cf_rows:
        lines.append(
            f"| {row['variant']} | {row['trades']} | {fmt(row.get('coverage_pct'))} | {fmt(row.get('winrate'))} | "
            f"{fmt(row.get('net'))} | {fmt(row.get('net_effect_if_blocked'))} | "
            f"{row.get('losers_blocked')} | {row.get('winners_blocked')} |"
        )

    lines += [
        "",
        "## Winner vs Loser directional (top)",
        "",
    ]
    for side in ("LONG", "SHORT"):
        lines.append(f"### {side}")
        for tf in HTF_TFS:
            lines.append(
                f"- {tf} WINNERS: {', '.join(top_states(side, 'WINNERS', tf)) or '–'}"
            )
            lines.append(
                f"- {tf} LOSERS: {', '.join(top_states(side, 'LOSERS', tf)) or '–'}"
            )
        lines.append("")

    lines += [
        "## Strategy Logic Changed",
        "",
        "`NO`",
        "",
        "## DB Changed",
        "",
        "`NO`",
        "",
    ]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    print("PRIMARY", primary, flush=True)
    print("REC", recommendation, flush=True)
    print("loss_classes", dict(loss_class_counts), flush=True)
    print("best_cf", best_cf["variant"] if best_cf else None, flush=True)
    print("wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
