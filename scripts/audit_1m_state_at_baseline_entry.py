#!/usr/bin/env python3
"""AUDIT_1M_STATE_AT_BASELINE_ENTRY — research-only, no DB writes.

Measures whether 1m StochRSI state *at* BASELINE_IMMEDIATE entry predicts
early adverse MAE / early drawdown. No wait-for-entry rule; baseline unchanged.
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
from signal_generator.strategy.wave_fade.parameters import (  # noqa: E402
    PRIMARY_FEE,
    STOCH_HIGH_K,
    STOCH_LOW_K,
)

OUT = ROOT / "results" / "1m_state_at_baseline_entry_audit"
HOURS = 168
FEE_PCT = float(PRIMARY_FEE)
K_BUCKETS = (
    ("K_0_20", 0.0, 20.0, True, False),   # <=20
    ("K_20_40", 20.0, 40.0, False, False),
    ("K_40_60", 40.0, 60.0, False, False),
    ("K_60_80", 60.0, 80.0, False, False),
    ("K_80_100", 80.0, 100.0, False, True),  # >80
)
CROSS_LOOKBACK = 3
FLAT_EPS = 1e-9
DELAY_WINDOWS = (3, 5, 10)
MAE_WINDOWS = (5, 10, 15, 30)
PATH_HORIZONS = (1, 3, 5, 10, 15, 30)
MIN_BUCKET_N = 8


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


def _pctile(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    return float(np.percentile(np.asarray(xs, dtype=float), p))


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


def k_bucket_name(k: float) -> str:
    if k <= 20:
        return "K_0_20"
    if k <= 40:
        return "K_20_40"
    if k <= 60:
        return "K_40_60"
    if k <= 80:
        return "K_60_80"
    return "K_80_100"


def trend_name(k_delta_1: float | None) -> str:
    if k_delta_1 is None or (isinstance(k_delta_1, float) and np.isnan(k_delta_1)):
        return "UNKNOWN"
    if k_delta_1 > FLAT_EPS:
        return "RISING"
    if k_delta_1 < -FLAT_EPS:
        return "FALLING"
    return "FLAT"


def recent_cross(k: np.ndarray, d: np.ndarray, state_i: int, *, bullish: bool, lookback: int = CROSS_LOOKBACK) -> bool:
    lo = max(1, state_i - lookback + 1)
    for i in range(lo, state_i + 1):
        k0, d0 = k[i - 1], d[i - 1]
        k1, d1 = k[i], d[i]
        if np.isnan(k0) or np.isnan(d0) or np.isnan(k1) or np.isnan(d1):
            continue
        if bullish and k0 <= d0 and k1 > d1:
            return True
        if (not bullish) and k0 >= d0 and k1 < d1:
            return True
    return False


def extract_1m_state(work: pd.DataFrame, state_i: int) -> dict[str, Any]:
    """state_i = last closed 1m bar index known at baseline entry open."""
    empty = {
        "k": None,
        "d": None,
        "k_minus_d": None,
        "k_delta_1": None,
        "k_delta_2": None,
        "k_delta_3": None,
        "d_delta_1": None,
        "is_rising": None,
        "is_falling": None,
        "bullish_cross_recent": None,
        "bearish_cross_recent": None,
        "distance_to_oversold": None,
        "distance_to_overbought": None,
        "min_k_last_3m": None,
        "max_k_last_3m": None,
        "min_k_last_5m": None,
        "max_k_last_5m": None,
        "k_bucket": None,
        "trend": None,
        "state_bucket": None,
        "state_ts": None,
    }
    if work.empty or state_i < 0 or state_i >= len(work):
        return empty
    k = work["stoch_k"].to_numpy(dtype=float)
    d = work["stoch_d"].to_numpy(dtype=float)
    ki = float(k[state_i]) if not np.isnan(k[state_i]) else None
    di = float(d[state_i]) if not np.isnan(d[state_i]) else None
    if ki is None:
        return empty

    def delta(arr: np.ndarray, lag: int) -> float | None:
        j = state_i - lag
        if j < 0 or np.isnan(arr[state_i]) or np.isnan(arr[j]):
            return None
        return float(arr[state_i] - arr[j])

    k_d1 = delta(k, 1)
    k_d2 = delta(k, 2)
    k_d3 = delta(k, 3)
    d_d1 = delta(d, 1)
    tr = trend_name(k_d1)
    kb = k_bucket_name(ki)

    def minmax(n: int) -> tuple[float | None, float | None]:
        a = max(0, state_i - n + 1)
        sl = k[a : state_i + 1]
        sl = sl[~np.isnan(sl)]
        if sl.size == 0:
            return None, None
        return float(np.min(sl)), float(np.max(sl))

    mn3, mx3 = minmax(3)
    mn5, mx5 = minmax(5)
    return {
        "k": ki,
        "d": di,
        "k_minus_d": (ki - di) if di is not None else None,
        "k_delta_1": k_d1,
        "k_delta_2": k_d2,
        "k_delta_3": k_d3,
        "d_delta_1": d_d1,
        "is_rising": bool(tr == "RISING"),
        "is_falling": bool(tr == "FALLING"),
        "bullish_cross_recent": recent_cross(k, d, state_i, bullish=True),
        "bearish_cross_recent": recent_cross(k, d, state_i, bullish=False),
        "distance_to_oversold": ki - float(STOCH_LOW_K),
        "distance_to_overbought": float(STOCH_HIGH_K) - ki,
        "min_k_last_3m": mn3,
        "max_k_last_3m": mx3,
        "min_k_last_5m": mn5,
        "max_k_last_5m": mx5,
        "k_bucket": kb,
        "trend": tr,
        "state_bucket": f"{kb}_{tr}",
        "state_ts": _iso(_utc(work.iloc[state_i]["timestamp"])),
    }


def adverse_mae_within(
    side: str,
    entry: float,
    work: pd.DataFrame,
    entry_i: int,
    minutes: int,
) -> float | None:
    """Worst adverse move % within `minutes` after entry (signed, <=0)."""
    if entry_i < 0 or entry_i >= len(work) or entry <= 0:
        return None
    end_i = min(len(work) - 1, entry_i + minutes)
    if end_i < entry_i:
        return None
    highs = work["high"].to_numpy(dtype=float)[entry_i : end_i + 1]
    lows = work["low"].to_numpy(dtype=float)[entry_i : end_i + 1]
    if highs.size == 0:
        return None
    if side == "LONG":
        return float(np.min((lows / entry - 1.0) * 100.0))
    return float(np.min(-((highs - entry) / entry * 100.0)))


def path_return(
    side: str,
    entry: float,
    work: pd.DataFrame,
    entry_i: int,
    minutes: int,
) -> float | None:
    """Directional return % at close of bar entry_i+minutes (or last available)."""
    j = entry_i + minutes
    if entry_i < 0 or entry <= 0 or work.empty:
        return None
    if j >= len(work):
        return None
    px = float(work.iloc[j]["close"])
    if side == "LONG":
        return (px / entry - 1.0) * 100.0
    return (entry - px) / entry * 100.0


def is_risk_group(side: str, state: dict[str, Any]) -> bool:
    """Fixed hypothesis risk groups (no threshold search)."""
    k = state.get("k")
    tr = state.get("trend")
    if k is None or tr is None:
        return False
    if side == "LONG":
        return bool(k > 40 and tr == "FALLING")
    return bool(k < 60 and tr == "RISING")


def hypothesis_group(side: str, state: dict[str, Any]) -> str:
    k = state.get("k")
    tr = state.get("trend")
    if k is None:
        return "UNKNOWN"
    if side == "LONG":
        if k > 60 and tr == "FALLING":
            return "HYP_LONG_Kgt60_FALLING"
        if k > 40 and tr == "FALLING":
            return "HYP_LONG_Kgt40_FALLING"
        if k <= 20:
            return "HYP_LONG_Kle20"
        if k <= 40 and tr == "RISING":
            return "HYP_LONG_Kle40_RISING"
        if state.get("bullish_cross_recent"):
            return "HYP_LONG_BULL_CROSS_RECENT"
        return "HYP_LONG_OTHER"
    # SHORT mirrored
    if k < 40 and tr == "RISING":
        return "HYP_SHORT_Klt40_RISING"
    if k < 60 and tr == "RISING":
        return "HYP_SHORT_Klt60_RISING"
    if k >= 80:
        return "HYP_SHORT_Kge80"
    if k >= 60 and tr == "FALLING":
        return "HYP_SHORT_Kge60_FALLING"
    if state.get("bearish_cross_recent"):
        return "HYP_SHORT_BEAR_CROSS_RECENT"
    return "HYP_SHORT_OTHER"


def selective_delay_entry(
    work: pd.DataFrame,
    *,
    side: str,
    entry_i: int,
    max_wait: int,
) -> tuple[int, str]:
    """Return (new_entry_i, reason). Entry at first event within max_wait closed bars after entry_i.

    Events (LONG): K stops falling (k_t >= k_{t-1}) OR bullish K/D cross on closed bar t.
    SHORT mirrored. Entry = next bar open after trigger bar (t+1), capped by timeout.
    If no event: timeout → keep original immediate entry (caller decides) — here return
    entry_i with reason TIMEOUT_KEEP_IMMEDIATE for coverage fairness, OR skip.
    Spec: Max wait danach normaler Entry/Timeout — on timeout use immediate? Spec says
    'Max wait danach normaler Entry/Timeout'. We'll enter at last bar open after timeout
    only if event fired; else NO delayed entry → fall back to baseline immediate.
    """
    if work.empty or entry_i < 0 or entry_i >= len(work):
        return entry_i, "NO_DATA"
    k = work["stoch_k"].to_numpy(dtype=float)
    d = work["stoch_d"].to_numpy(dtype=float)
    # Scan closed bars starting from the entry bar itself (forming at entry → first
    # new info is closes of entry_i, entry_i+1, ...). At baseline open of entry_i,
    # state was entry_i-1. First new closed bar after entry is entry_i (closes +1m).
    last_scan = min(len(work) - 1, entry_i + max_wait - 1)
    for t in range(entry_i, last_scan + 1):
        if t <= 0:
            continue
        if np.isnan(k[t]) or np.isnan(k[t - 1]):
            continue
        stop_fall = False
        cross = False
        if side == "LONG":
            stop_fall = bool(k[t] >= k[t - 1])
            if not np.isnan(d[t]) and not np.isnan(d[t - 1]):
                cross = bool(k[t - 1] <= d[t - 1] and k[t] > d[t])
        else:
            stop_fall = bool(k[t] <= k[t - 1])  # stops rising for short
            if not np.isnan(d[t]) and not np.isnan(d[t - 1]):
                cross = bool(k[t - 1] >= d[t - 1] and k[t] < d[t])
        if stop_fall or cross:
            new_i = t + 1  # enter next open after confirmation
            if new_i >= len(work):
                return entry_i, "TRIGGER_NO_NEXT_BAR"
            reason = "STOP_FALL" if stop_fall else "KD_CROSS"
            if stop_fall and cross:
                reason = "STOP_FALL_OR_CROSS"
            return new_i, reason
    return entry_i, "TIMEOUT_KEEP_IMMEDIATE"


def summarize_group(rows: list[dict[str, Any]], *, label: str) -> dict[str, Any]:
    n = len(rows)
    wins = sum(1 for r in rows if r.get("result") == "WIN")
    losses = sum(1 for r in rows if r.get("result") == "LOSS")
    closed = wins + losses
    pnls = [float(r["pnl"]) for r in rows if r.get("pnl") is not None]
    maes = [float(r["mae"]) for r in rows if r.get("mae") is not None]
    # adverse magnitude for p90: more adverse = more negative; p90 of mae is less adverse;
    # user asked p90 adverse MAE → use p10 of signed mae (or p90 of -mae)
    adv = [-float(r["mae"]) for r in rows if r.get("mae") is not None]
    dd1 = sum(1 for r in rows if r.get("early_dd_1pct"))
    dd2 = sum(1 for r in rows if r.get("early_dd_2pct"))
    dd3 = sum(1 for r in rows if r.get("early_dd_3pct"))
    e2_tp = sum(1 for r in rows if r.get("early_dd_2pct") and r.get("result") == "WIN")
    e2_sl = sum(1 for r in rows if r.get("early_dd_2pct") and r.get("result") == "LOSS")
    e3_tp = sum(1 for r in rows if r.get("early_dd_3pct") and r.get("result") == "WIN")
    e3_sl = sum(1 for r in rows if r.get("early_dd_3pct") and r.get("result") == "LOSS")
    return {
        "group": label,
        "count": n,
        "winrate": (100.0 * wins / closed) if closed else None,
        "net": float(sum(pnls) - FEE_PCT * len(pnls)) if pnls else 0.0,
        "gross": float(sum(pnls)) if pnls else 0.0,
        "profit_factor": profit_factor(pnls) if pnls else None,
        "median_MAE": _median(maes),
        "mean_MAE": _mean(maes),
        "p90_adverse_MAE": _pctile(adv, 90),  # magnitude of adverse move
        "rate_EARLY_DD_1PCT": (100.0 * dd1 / n) if n else None,
        "rate_EARLY_DD_2PCT": (100.0 * dd2 / n) if n else None,
        "rate_EARLY_DD_3PCT": (100.0 * dd3 / n) if n else None,
        "tp_rate": (100.0 * wins / closed) if closed else None,
        "sl_rate": (100.0 * losses / closed) if closed else None,
        "early_dd_2pct_then_tp": e2_tp,
        "early_dd_2pct_then_sl": e2_sl,
        "early_dd_3pct_then_tp": e3_tp,
        "early_dd_3pct_then_sl": e3_sl,
        "wins": wins,
        "losses": losses,
    }


def path_summary(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    out: dict[str, Any] = {"group": label, "count": len(rows)}
    for h in PATH_HORIZONS:
        key = f"ret_p{h}m"
        vals = [float(r[key]) for r in rows if r.get(key) is not None]
        out[f"median_{key}"] = _median(vals)
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=HOURS)

    ch = setup_clickhouse(settings=get_clickhouse_settings())
    sig_repo = SignalRepository(ch)
    candle_repo = CandleRepository(ch)

    print(f"Loading Tier-A {start.isoformat()} → {now.isoformat()} …", flush=True)
    rows, total = sig_repo.query_signals(
        start=start,
        end=now,
        tier_a=True,
        timeframe=None,
        time_field="candle_close_time",
        limit=5000,
        offset=0,
    )
    api_rows = [_signal_row_to_api(r) for r in rows]
    print(f"Tier-A loaded: {len(api_rows)} (query total={total})", flush=True)

    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in api_rows:
        by_sym[str(r["symbol"]).upper()].append(r)

    candle_cache: dict[str, pd.DataFrame] = {}
    pad_before = timedelta(hours=6)
    pad_after = timedelta(hours=36)
    for sym, items in by_sym.items():
        t0s = [_utc(r.get("entry_time") or r.get("candle_close_time")) for r in items]
        a, b = min(t0s) - pad_before, max(t0s) + pad_after
        raw = candle_repo.get_candles(sym, a, b)
        df = pd.DataFrame(raw) if raw else pd.DataFrame()
        candle_cache[sym] = _prepare_1m(df) if not df.empty else df
        print(f"  candles {sym}: {len(candle_cache[sym])}", flush=True)

    details: list[dict[str, Any]] = []

    for r in api_rows:
        sym = str(r["symbol"]).upper()
        side = str(r["direction"]).upper()
        tf = str(r.get("timeframe") or "")
        sid = str(r["signal_id"])
        signal_ts = _utc(r.get("candle_close_time") or r.get("generated_at"))
        t0 = _utc(r.get("entry_time") or signal_ts)
        try:
            baseline_px = float(r["entry_price"]) if r.get("entry_price") is not None else None
        except (TypeError, ValueError):
            baseline_px = None

        work = candle_cache.get(sym, pd.DataFrame())
        entry_i = -1
        if not work.empty:
            entry_i = int(work["timestamp"].searchsorted(pd.Timestamp(t0), side="left"))
            if entry_i >= len(work):
                entry_i = -1

        # Causal closed state at baseline open: last bar before entry bar
        state_i = entry_i - 1 if entry_i > 0 else -1
        state = extract_1m_state(work, state_i)

        # Baseline outcome
        result = "OPEN"
        pnl = mae = mfe = None
        exit_reason = None
        entry_px = baseline_px
        if entry_i >= 0 and not work.empty:
            if entry_px is None:
                entry_px = float(work.iloc[entry_i]["open"])
            sim = _simulate_outcome(
                side=side, entry=float(entry_px), tf=tf, entry_i=entry_i, work=work, as_of=now
            )
            result = sim["result"]
            pnl = sim.get("pnl_pct")
            mae = sim.get("mae_pct")
            mfe = sim.get("mfe_pct")
            exit_reason = sim.get("exit_reason")

        mae_win: dict[str, float | None] = {}
        for m in MAE_WINDOWS:
            mae_win[f"mae_{m}m_pct"] = (
                adverse_mae_within(side, float(entry_px), work, entry_i, m)
                if entry_i >= 0 and entry_px
                else None
            )

        # Early DD labels: use worst of 5/10/15/30 (any early window) — primary chart problem
        # Spec: MAE within 5/10/15/30; labels for 1/2/3% adverse. Use mae_30m as cumulative early window.
        mae_early = mae_win.get("mae_30m_pct")
        early_dd_1 = bool(mae_early is not None and mae_early <= -1.0)
        early_dd_2 = bool(mae_early is not None and mae_early <= -2.0)
        early_dd_3 = bool(mae_early is not None and mae_early <= -3.0)

        path: dict[str, float | None] = {}
        for h in PATH_HORIZONS:
            path[f"ret_p{h}m"] = (
                path_return(side, float(entry_px), work, entry_i, h)
                if entry_i >= 0 and entry_px
                else None
            )

        hyp = hypothesis_group(side, state)
        risk = is_risk_group(side, state)

        rec = {
            "signal_id": sid,
            "symbol": sym,
            "direction": side,
            "signal_tf": tf,
            "signal_ts": _iso(signal_ts),
            "baseline_entry_ts": _iso(t0),
            "baseline_entry_price": entry_px,
            "entry_i": entry_i,
            "state_i": state_i,
            **state,
            "hypothesis_group": hyp,
            "is_risk_group": risk,
            "result": result,
            "pnl": pnl,
            "mae": mae,
            "mfe": mfe,
            "exit_reason": exit_reason,
            **mae_win,
            "early_dd_1pct": early_dd_1,
            "early_dd_2pct": early_dd_2,
            "early_dd_3pct": early_dd_3,
            **path,
        }
        details.append(rec)

    write_csv(OUT / "signal_detail.csv", details)

    primary = [d for d in details if d["signal_tf"] == "15m"]
    print(f"15m primary n={len(primary)}", flush=True)

    # --- Bucket summaries (15m primary) ---
    by_state: dict[str, list] = defaultdict(list)
    by_hyp: dict[str, list] = defaultdict(list)
    by_k: dict[str, list] = defaultdict(list)
    by_trend: dict[str, list] = defaultdict(list)
    for d in primary:
        if d.get("state_bucket"):
            by_state[str(d["state_bucket"])].append(d)
        by_hyp[str(d["hypothesis_group"])].append(d)
        if d.get("k_bucket"):
            by_k[str(d["k_bucket"])].append(d)
        if d.get("trend"):
            by_trend[str(d["trend"])].append(d)

    # Risk / safe aggregate
    by_hyp["RISK_K_HIGH_FALLING_OR_SHORT_MIRROR"] = [d for d in primary if d["is_risk_group"]]
    by_hyp["SAFE_NOT_RISK"] = [d for d in primary if not d["is_risk_group"] and d.get("k") is not None]
    by_hyp["ALL_15M"] = primary

    state_rows = [summarize_group(v, label=k) for k, v in sorted(by_state.items(), key=lambda x: -len(x[1]))]
    hyp_rows = [summarize_group(v, label=k) for k, v in sorted(by_hyp.items(), key=lambda x: -len(x[1]))]
    k_rows = [summarize_group(v, label=k) for k, v in sorted(by_k.items())]
    trend_rows = [summarize_group(v, label=k) for k, v in sorted(by_trend.items())]

    bucket_summary = state_rows + [{"group": "---K---"}] + k_rows + [{"group": "---TREND---"}] + trend_rows + [
        {"group": "---HYP---"}
    ] + hyp_rows
    write_csv(OUT / "state_bucket_summary.csv", [r for r in bucket_summary if "count" in r])

    # Early drawdown summary (hypothesis + risk focus)
    write_csv(OUT / "early_drawdown_summary.csv", hyp_rows)

    # Post-entry paths
    path_rows = [path_summary(v, k) for k, v in sorted(by_hyp.items(), key=lambda x: -len(x[1]))]
    # also top state buckets
    for k, v in sorted(by_state.items(), key=lambda x: -len(x[1]))[:12]:
        path_rows.append(path_summary(v, f"STATE::{k}"))
    write_csv(OUT / "post_entry_paths.csv", path_rows)

    # Optional other TFs brief
    tf_brief = []
    for tf in ("15m", "30m", "1h", "4h"):
        subset = [d for d in details if d["signal_tf"] == tf]
        if not subset:
            continue
        risk = [d for d in subset if d["is_risk_group"]]
        safe = [d for d in subset if not d["is_risk_group"] and d.get("k") is not None]
        tf_brief.append({"tf": tf, "scope": "ALL", **summarize_group(subset, label=f"{tf}_ALL")})
        if risk:
            tf_brief.append({"tf": tf, "scope": "RISK", **summarize_group(risk, label=f"{tf}_RISK")})
        if safe:
            tf_brief.append({"tf": tf, "scope": "SAFE", **summarize_group(safe, label=f"{tf}_SAFE")})
    write_csv(OUT / "tf_optional_summary.csv", tf_brief)

    # --- Identify worst/best groups among state buckets with enough sample ---
    usable = [r for r in state_rows if (r.get("count") or 0) >= MIN_BUCKET_N]
    # Worst = highest EARLY_DD_2PCT then worse median MAE (more negative)
    def risk_score(r: dict) -> tuple:
        return (
            r.get("rate_EARLY_DD_2PCT") or 0.0,
            r.get("rate_EARLY_DD_3PCT") or 0.0,
            -(r.get("median_MAE") or 0.0),  # more adverse (more neg MAE) → higher -MAE
            -(r.get("net") or 0.0),
        )

    worst = max(usable, key=risk_score) if usable else None
    best = min(usable, key=risk_score) if usable else None

    risk_sum = next((r for r in hyp_rows if r["group"] == "RISK_K_HIGH_FALLING_OR_SHORT_MIRROR"), None)
    safe_sum = next((r for r in hyp_rows if r["group"] == "SAFE_NOT_RISK"), None)
    all_sum = next((r for r in hyp_rows if r["group"] == "ALL_15M"), None)

    # Clear risk identification?
    clear_risk = False
    if risk_sum and safe_sum and (risk_sum["count"] or 0) >= MIN_BUCKET_N and (safe_sum["count"] or 0) >= MIN_BUCKET_N:
        dd2_gap = (risk_sum["rate_EARLY_DD_2PCT"] or 0) - (safe_sum["rate_EARLY_DD_2PCT"] or 0)
        mae_worse = (risk_sum["median_MAE"] or 0) < (safe_sum["median_MAE"] or 0) - 0.15
        clear_risk = dd2_gap >= 8.0 or (dd2_gap >= 5.0 and mae_worse)

    # --- Selective delay counterfactual (only if clear risk) ---
    delay_summaries: list[dict[str, Any]] = []
    delay_details: list[dict[str, Any]] = []
    ran_selective = False

    if clear_risk:
        ran_selective = True
        risk_ids = {d["signal_id"] for d in primary if d["is_risk_group"]}

        # Baseline on primary
        def pack_outcome(rows_in: list[dict]) -> dict[str, Any]:
            s = summarize_group(rows_in, label="x")
            waits = [float(r.get("entry_delay_minutes") or 0) for r in rows_in]
            return {
                "coverage": 100.0,  # always enter
                "triggered": len(rows_in),
                "winrate": s["winrate"],
                "net": s["net"],
                "profit_factor": s["profit_factor"],
                "sl_rate": s["sl_rate"],
                "median_MAE": s["median_MAE"],
                "mean_MAE": s["mean_MAE"],
                "median_wait": _median(waits),
            }

        base_pack_rows = []
        for d in primary:
            base_pack_rows.append(
                {
                    **d,
                    "entry_delay_minutes": 0.0,
                    "variant": "BASELINE_IMMEDIATE",
                }
            )
        delay_details.extend(base_pack_rows)

        base_all = pack_outcome(base_pack_rows)
        base_risk = pack_outcome([r for r in base_pack_rows if r["signal_id"] in risk_ids])
        base_safe = pack_outcome([r for r in base_pack_rows if r["signal_id"] not in risk_ids])
        delay_summaries.append(
            {
                "variant": "BASELINE_IMMEDIATE",
                "scope": "ALL",
                **base_all,
                "winners_missed": 0,
                "losers_avoided": 0,
            }
        )
        delay_summaries.append({"variant": "BASELINE_IMMEDIATE", "scope": "RISK_ONLY", **base_risk, "winners_missed": 0, "losers_avoided": 0})
        delay_summaries.append({"variant": "BASELINE_IMMEDIATE", "scope": "SAFE_ONLY", **base_safe, "winners_missed": 0, "losers_avoided": 0})

        for wmax in DELAY_WINDOWS:
            var = f"SELECTIVE_1M_DELAY_{wmax}M"
            rows_out: list[dict[str, Any]] = []
            winners_missed = losers_avoided = 0  # N/A for always-enter; track delay vs baseline outcome change
            improved_risk = worsened_risk = 0
            for d in primary:
                sid = d["signal_id"]
                sym = d["symbol"]
                side = d["direction"]
                tf = d["signal_tf"]
                work = candle_cache[sym]
                entry_i = int(d["entry_i"])
                base_px = float(d["baseline_entry_price"]) if d["baseline_entry_price"] is not None else None
                new_i = entry_i
                reason = "NOT_RISK_IMMEDIATE"
                delay_m = 0.0
                if d["is_risk_group"] and entry_i >= 0:
                    new_i, reason = selective_delay_entry(work, side=side, entry_i=entry_i, max_wait=wmax)
                    delay_m = float(max(0, new_i - entry_i))
                if new_i < 0 or work.empty or new_i >= len(work):
                    # keep baseline
                    new_i = entry_i
                    reason = "FALLBACK_BASELINE"
                    delay_m = 0.0
                entry_px = float(work.iloc[new_i]["open"]) if new_i >= 0 else base_px
                sim = _simulate_outcome(
                    side=side, entry=float(entry_px), tf=tf, entry_i=new_i, work=work, as_of=now
                )
                mae_early = adverse_mae_within(side, float(entry_px), work, new_i, 30) if new_i >= 0 else None
                rec = {
                    "signal_id": sid,
                    "symbol": sym,
                    "direction": side,
                    "signal_tf": tf,
                    "variant": var,
                    "is_risk_group": d["is_risk_group"],
                    "delay_reason": reason,
                    "entry_delay_minutes": delay_m,
                    "entry_i": new_i,
                    "entry_price": entry_px,
                    "result": sim["result"],
                    "pnl": sim.get("pnl_pct"),
                    "mae": sim.get("mae_pct"),
                    "mfe": sim.get("mfe_pct"),
                    "exit_reason": sim.get("exit_reason"),
                    "mae_30m_pct": mae_early,
                    "early_dd_1pct": bool(mae_early is not None and mae_early <= -1.0),
                    "early_dd_2pct": bool(mae_early is not None and mae_early <= -2.0),
                    "early_dd_3pct": bool(mae_early is not None and mae_early <= -3.0),
                    "baseline_result": d["result"],
                    "baseline_pnl": d["pnl"],
                }
                rows_out.append(rec)
                if d["is_risk_group"]:
                    bp = d.get("pnl")
                    np_ = sim.get("pnl_pct")
                    if bp is not None and np_ is not None:
                        if float(np_) > float(bp):
                            improved_risk += 1
                        elif float(np_) < float(bp):
                            worsened_risk += 1
                    # miss/avoid relative to baseline win/loss if delay changes outcome
                    if d["result"] == "WIN" and sim["result"] == "LOSS":
                        winners_missed += 1
                    if d["result"] == "LOSS" and sim["result"] == "WIN":
                        losers_avoided += 1

            delay_details.extend(rows_out)
            for scope, subset in (
                ("ALL", rows_out),
                ("RISK_ONLY", [x for x in rows_out if x["is_risk_group"]]),
                ("SAFE_ONLY", [x for x in rows_out if not x["is_risk_group"]]),
            ):
                pack = pack_outcome(subset)
                delay_summaries.append(
                    {
                        "variant": var,
                        "scope": scope,
                        **pack,
                        "winners_missed": winners_missed if scope == "ALL" else None,
                        "losers_avoided": losers_avoided if scope == "ALL" else None,
                        "risk_improved_vs_base": improved_risk if scope == "RISK_ONLY" else None,
                        "risk_worsened_vs_base": worsened_risk if scope == "RISK_ONLY" else None,
                    }
                )

        write_csv(OUT / "selective_delay_summary.csv", delay_summaries)
        write_csv(OUT / "selective_delay_signal_detail.csv", delay_details)

    # --- Primary decision ---
    n15 = len(primary)
    if n15 < 50:
        primary_decision = "INSUFFICIENT_SAMPLE"
        recommendation = "KEEP_BASELINE"
    elif not clear_risk:
        # check weak vs none
        if risk_sum and safe_sum and (risk_sum["count"] or 0) >= 5:
            dd2_gap = abs((risk_sum["rate_EARLY_DD_2PCT"] or 0) - (safe_sum["rate_EARLY_DD_2PCT"] or 0))
            if dd2_gap < 3 and abs((risk_sum["median_MAE"] or 0) - (safe_sum["median_MAE"] or 0)) < 0.1:
                primary_decision = "1M_STATE_DOES_NOT_PREDICT_EARLY_DRAWDOWN"
            else:
                primary_decision = "1M_STATE_ONLY_WEAKLY_PREDICTIVE"
        else:
            primary_decision = "1M_STATE_ONLY_WEAKLY_PREDICTIVE"
        recommendation = "KEEP_BASELINE"
    else:
        primary_decision = "1M_STATE_IDENTIFIES_EARLY_ENTRY_RISK"
        recommendation = "KEEP_BASELINE"
        if ran_selective and delay_summaries:
            # Prefer 5m as mid window for decision
            def find(v: str, scope: str) -> dict | None:
                return next((x for x in delay_summaries if x["variant"] == v and x["scope"] == scope), None)

            base_r = find("BASELINE_IMMEDIATE", "RISK_ONLY")
            base_a = find("BASELINE_IMMEDIATE", "ALL")
            best_delay = None
            for wmax in DELAY_WINDOWS:
                cand = find(f"SELECTIVE_1M_DELAY_{wmax}M", "ALL")
                risk_c = find(f"SELECTIVE_1M_DELAY_{wmax}M", "RISK_ONLY")
                safe_c = find(f"SELECTIVE_1M_DELAY_{wmax}M", "SAFE_ONLY")
                if not cand or not risk_c or not base_r or not base_a:
                    continue
                # Safe untouched → net/wr should match baseline safe
                safe_ok = True
                if safe_c and find("BASELINE_IMMEDIATE", "SAFE_ONLY"):
                    sb = find("BASELINE_IMMEDIATE", "SAFE_ONLY")
                    if sb and abs((safe_c["net"] or 0) - (sb["net"] or 0)) > 0.01:
                        safe_ok = False
                risk_better = (risk_c["net"] or 0) > (base_r["net"] or 0) + 0.5 and (
                    (risk_c["median_MAE"] or -999) > (base_r["median_MAE"] or -999) + 0.05
                    or (risk_c.get("sl_rate") or 100) < (base_r.get("sl_rate") or 0)
                )
                all_better = (cand["net"] or 0) >= (base_a["net"] or 0) - 0.5
                if safe_ok and risk_better and all_better:
                    best_delay = wmax
                    break
                # weaker: risk MAE improves and all net not worse
                risk_mae_better = (risk_c["median_MAE"] or -999) > (base_r["median_MAE"] or -999) + 0.1
                if safe_ok and risk_mae_better and all_better and (cand["net"] or 0) >= (base_a["net"] or 0):
                    best_delay = wmax
                    break

            if best_delay is not None:
                recommendation = "SELECTIVE_1M_DELAY_WORTH_FURTHER_TESTING"
            else:
                # Did delay hurt overall edge?
                hurt = False
                for wmax in DELAY_WINDOWS:
                    cand = find(f"SELECTIVE_1M_DELAY_{wmax}M", "ALL")
                    if cand and base_a and (cand["net"] or 0) < (base_a["net"] or 0) - 2:
                        hurt = True
                if hurt:
                    primary_decision = "1M_STATE_FILTER_HURTS_EDGE"
                    recommendation = "KEEP_BASELINE"

    # Important groups for report
    important_groups = []
    for g in (
        "ALL_15M",
        "RISK_K_HIGH_FALLING_OR_SHORT_MIRROR",
        "SAFE_NOT_RISK",
        "HYP_LONG_Kgt40_FALLING",
        "HYP_LONG_Kgt60_FALLING",
        "HYP_LONG_Kle20",
        "HYP_LONG_Kle40_RISING",
        "HYP_LONG_BULL_CROSS_RECENT",
        "HYP_SHORT_Klt60_RISING",
        "HYP_SHORT_Klt40_RISING",
        "HYP_SHORT_Kge80",
        "HYP_SHORT_Kge60_FALLING",
    ):
        row = next((r for r in hyp_rows if r["group"] == g), None)
        if row and (row.get("count") or 0) > 0:
            important_groups.append(row)
    if worst:
        important_groups.append({**worst, "group": f"WORST_STATE::{worst['group']}"})
    if best:
        important_groups.append({**best, "group": f"BEST_STATE::{best['group']}"})

    payload = {
        "primary_decision": primary_decision,
        "recommendation": recommendation,
        "hours": HOURS,
        "n_tier_a_total": len(details),
        "n_15m": n15,
        "clear_risk_identified": clear_risk,
        "risk_definition": "LONG: K>40 AND FALLING; SHORT: K<60 AND RISING (at last closed 1m before baseline entry)",
        "worst_state_bucket": worst,
        "best_state_bucket": best,
        "risk_vs_safe": {"risk": risk_sum, "safe": safe_sum, "all": all_sum},
        "important_groups": important_groups,
        "selective_delay_ran": ran_selective,
        "selective_delay_summary": delay_summaries,
        "as_of": _iso(now),
        "fee_pct_for_net": FEE_PCT,
    }
    (OUT / "summary.json").write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")

    def fmt(v: Any, nd: int = 1) -> str:
        if v is None:
            return "–"
        if isinstance(v, float):
            if abs(v) == float("inf"):
                return "inf"
            return f"{v:.{nd}f}"
        return str(v)

    lines = [
        "# AUDIT_1M_STATE_AT_BASELINE_ENTRY",
        "",
        "Baseline Tier-A entry unchanged. 1m StochRSI state is observational only.",
        f"Dataset: last **{HOURS}h** Tier-A. Primary TF: **15m** (n={n15}).",
        "",
        "## Primary Decision",
        "",
        f"`{primary_decision}`",
        "",
        f"Recommendation: `{recommendation}`",
        "",
        f"Risk group definition: `{payload['risk_definition']}`",
        f"Clear risk identified: **{clear_risk}**",
        "",
        "## Risk vs Safe (15m)",
        "",
        "| Group | n | WR | Net | Med MAE | DD2% | DD3% | SL |",
        "| ----- | -: | -: | --: | ------: | ---: | ---: | -: |",
    ]
    for lab, row in (("ALL", all_sum), ("RISK", risk_sum), ("SAFE", safe_sum)):
        if not row:
            continue
        lines.append(
            f"| {lab} | {row['count']} | {fmt(row['winrate'])} | {fmt(row['net'])} | "
            f"{fmt(row['median_MAE'], 3)} | {fmt(row['rate_EARLY_DD_2PCT'])} | "
            f"{fmt(row['rate_EARLY_DD_3PCT'])} | {fmt(row['sl_rate'])} |"
        )

    lines += [
        "",
        "## Worst / Best state buckets (n≥8)",
        "",
        f"- Worst: `{worst['group'] if worst else '–'}` n={worst['count'] if worst else '–'} "
        f"DD2={fmt(worst['rate_EARLY_DD_2PCT'] if worst else None)} medMAE={fmt(worst['median_MAE'] if worst else None, 3)}",
        f"- Best: `{best['group'] if best else '–'}` n={best['count'] if best else '–'} "
        f"DD2={fmt(best['rate_EARLY_DD_2PCT'] if best else None)} medMAE={fmt(best['median_MAE'] if best else None, 3)}",
        "",
        "## Hypothesis groups",
        "",
        "| Group | n | WR | Net | Med MAE | DD1 | DD2 | DD3 | e2→TP | e2→SL | e3→TP | e3→SL |",
        "| ----- | -: | -: | --: | ------: | --: | --: | --: | ----: | ----: | ----: | ----: |",
    ]
    for row in important_groups:
        if str(row["group"]).startswith("WORST") or str(row["group"]).startswith("BEST"):
            continue
        lines.append(
            f"| {row['group']} | {row['count']} | {fmt(row['winrate'])} | {fmt(row['net'])} | "
            f"{fmt(row['median_MAE'], 3)} | {fmt(row['rate_EARLY_DD_1PCT'])} | "
            f"{fmt(row['rate_EARLY_DD_2PCT'])} | {fmt(row['rate_EARLY_DD_3PCT'])} | "
            f"{row.get('early_dd_2pct_then_tp')} | {row.get('early_dd_2pct_then_sl')} | "
            f"{row.get('early_dd_3pct_then_tp')} | {row.get('early_dd_3pct_then_sl')} |"
        )

    if ran_selective:
        lines += [
            "",
            "## Selective delay (risk group only)",
            "",
            "| Variant | Scope | WR | Net | PF | SL | Med MAE | Med Wait |",
            "| ------- | ----- | -: | --: | -: | -: | ------: | -------: |",
        ]
        for row in delay_summaries:
            lines.append(
                f"| {row['variant']} | {row['scope']} | {fmt(row.get('winrate'))} | {fmt(row.get('net'))} | "
                f"{fmt(row.get('profit_factor'))} | {fmt(row.get('sl_rate'))} | "
                f"{fmt(row.get('median_MAE'), 3)} | {fmt(row.get('median_wait'))} |"
            )

    lines += [
        "",
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

    print("PRIMARY", primary_decision, flush=True)
    print("REC", recommendation, flush=True)
    print("clear_risk", clear_risk, "n15", n15, flush=True)
    if worst:
        print("worst", worst["group"], "dd2", worst["rate_EARLY_DD_2PCT"], flush=True)
    if best:
        print("best", best["group"], "dd2", best["rate_EARLY_DD_2PCT"], flush=True)
    print("wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
