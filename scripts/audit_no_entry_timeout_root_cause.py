#!/usr/bin/env python3
"""AUDIT_NO_ENTRY_TIMEOUT_ROOT_CAUSE — research-only, no DB writes.

Analyzes WAIT_1M_EXTREME_TURN_CROSS timeouts on live Tier-A signals (168h).
"""

from __future__ import annotations

import csv
import json
import math
import sys
from collections import Counter, defaultdict
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
from signal_generator.research.one_m_entry_timing.constants import (  # noqa: E402
    TRIGGER_ENTRY_TRIGGERED,
    TRIGGER_NO_ENTRY_TIMEOUT,
    VARIANT_WAIT_1M_EXTREME_TURN_CROSS,
)
from signal_generator.research.one_m_entry_timing.timing import (  # noqa: E402
    _levels,
    _prepare_1m,
    _simulate_outcome,
    _utc,
    evaluate_1m_entry_timing,
)
from signal_generator.strategy.wave_fade.exits import scan_exit_sl_first  # noqa: E402
from signal_generator.strategy.wave_fade.parameters import (  # noqa: E402
    STOCH_HIGH_K,
    STOCH_LOW_K,
    TF_BAR_MIN,
)

OUT = ROOT / "results" / "1m_stoch_entry_timing_audit"
HOURS = 168
VARIANT = VARIANT_WAIT_1M_EXTREME_TURN_CROSS

TIMEOUT_GRID: dict[str, tuple[int, ...]] = {
    "15m": (15, 30, 60),
    "30m": (30, 60, 120),
    "1h": (60, 120, 240),
    "4h": (120, 240, 480),
}

LATE_HORIZONS = (15, 30, 60, 120)


def _iso(ts: Any | None) -> str | None:
    if ts is None:
        return None
    return _utc(ts).isoformat().replace("+00:00", "Z")


def _pctile(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    return float(np.percentile(np.asarray(xs, dtype=float), p))


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    return float(np.median(np.asarray(xs, dtype=float)))


def default_timeout(tf: str) -> int:
    return int(TF_BAR_MIN.get(tf, 15))


def find_extreme_and_turn(
    work: pd.DataFrame,
    *,
    side: str,
    start_i: int,
    scan_end_ts: datetime,
) -> dict[str, Any]:
    """Scan closed bars from start_i while open_time <= scan_end_ts."""
    if work.empty or start_i >= len(work):
        return {
            "extreme_i": None,
            "turn_i": None,
            "entry_i": None,
            "min_k": None,
            "max_k": None,
            "last_i": None,
        }
    k = work["stoch_k"].to_numpy(dtype=float)
    d = work["stoch_d"].to_numpy(dtype=float)
    extreme_i = None
    turn_i = None
    entry_i = None
    last_i = start_i - 1
    ks: list[float] = []

    for i in range(start_i, len(work)):
        bar_t = _utc(work.iloc[i]["timestamp"])
        if bar_t > scan_end_ts:
            break
        last_i = i
        ki = k[i]
        if not np.isnan(ki):
            ks.append(float(ki))
        if extreme_i is None:
            if not np.isnan(ki):
                if side == "LONG" and ki <= STOCH_LOW_K:
                    extreme_i = i
                elif side == "SHORT" and ki >= STOCH_HIGH_K:
                    extreme_i = i
            continue
        if turn_i is None and i > 0:
            vals = (k[i], d[i], k[i - 1], d[i - 1])
            if any(np.isnan(x) for x in vals):
                continue
            crossed = (
                (side == "LONG" and k[i - 1] <= d[i - 1] and k[i] > d[i])
                or (side == "SHORT" and k[i - 1] >= d[i - 1] and k[i] < d[i])
            )
            if crossed:
                turn_i = i
                entry_i = i + 1 if i + 1 < len(work) else None
                # keep scanning for min/max through full window

    return {
        "extreme_i": extreme_i,
        "turn_i": turn_i,
        "entry_i": entry_i if entry_i is not None and entry_i < len(work) else None,
        "min_k": float(min(ks)) if ks else None,
        "max_k": float(max(ks)) if ks else None,
        "last_i": last_i if last_i >= start_i else None,
    }


def coverage_stats(work: pd.DataFrame, t0: datetime, end: datetime) -> tuple[int, int, float]:
    expected = max(0, int((end - t0).total_seconds() // 60) + 1)
    if work.empty:
        return expected, 0, 0.0
    mask = (work["timestamp"] >= pd.Timestamp(_utc(t0))) & (
        work["timestamp"] <= pd.Timestamp(_utc(end))
    )
    present = int(mask.sum())
    cov = (100.0 * present / expected) if expected else 0.0
    return expected, present, cov


def k_bucket(side: str, min_k: float | None, max_k: float | None) -> str:
    if side == "LONG":
        if min_k is None:
            return "UNKNOWN"
        v = min_k
        if v <= 5:
            return "K<=5"
        if v <= 10:
            return "K<=10"
        if v <= 15:
            return "K<=15"
        if v <= 20:
            return "K<=20"
        if v <= 25:
            return "K<=25"
        if v <= 30:
            return "K<=30"
        return "K>30"
    # SHORT: how high did max K get
    if max_k is None:
        return "UNKNOWN"
    v = max_k
    if v >= 95:
        return "K>=95"
    if v >= 90:
        return "K>=90"
    if v >= 85:
        return "K>=85"
    if v >= 80:
        return "K>=80"
    if v >= 75:
        return "K>=75"
    if v >= 70:
        return "K>=70"
    return "K<70"


def classify_root_cause(
    *,
    coverage_pct: float,
    extreme_i: int | None,
    turn_within: bool,
    turn_after: bool,
) -> str:
    if coverage_pct < 80.0:
        return "DATA_COVERAGE_PROBLEM"
    if extreme_i is None:
        return "EXTREME_NEVER_REACHED"
    if turn_after:
        return "TURN_CAME_AFTER_TIMEOUT"
    if not turn_within:
        return "EXTREME_REACHED_BUT_NO_TURN"
    return "OTHER"


def outcome_label(sim: dict[str, Any]) -> str:
    r = sim.get("result")
    if r in ("WIN", "LOSS", "OPEN"):
        return str(r)
    return "OPEN"


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=HOURS)
    # candle lookback: stoch warmup ~80 bars + signal start + max post timeout 480
    candle_pad_before = timedelta(hours=6)
    candle_pad_after = timedelta(minutes=480 + 60)

    ch = setup_clickhouse(settings=get_clickhouse_settings())
    sig_repo = SignalRepository(ch)
    candle_repo = CandleRepository(ch)

    print(f"Loading Tier-A signals {start.isoformat()} → {now.isoformat()} …", flush=True)
    rows, total = sig_repo.query_signals(
        start=start,
        end=now,
        tier_a=True,
        time_field="candle_close_time",
        limit=5000,
        offset=0,
    )
    print(f"Tier-A rows: {len(rows)} (total={total})", flush=True)
    api_rows = [_signal_row_to_api(r) for r in rows]

    # group by symbol for candle loads
    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in api_rows:
        sym = str(r.get("symbol") or "").upper()
        if sym:
            by_sym[sym].append(r)

    candle_cache: dict[str, pd.DataFrame] = {}
    for sym, items in by_sym.items():
        t0s = []
        for r in items:
            et = r.get("entry_time") or r.get("candle_close_time")
            if et:
                t0s.append(_utc(et))
        if not t0s:
            candle_cache[sym] = pd.DataFrame()
            continue
        a = min(t0s) - candle_pad_before
        b = max(t0s) + candle_pad_after
        raw = candle_repo.get_candles(sym, a, b)
        df = pd.DataFrame(raw) if raw else pd.DataFrame()
        candle_cache[sym] = _prepare_1m(df) if not df.empty else df
        print(f"  candles {sym}: {len(candle_cache[sym])}", flush=True)

    detail_rows: list[dict[str, Any]] = []
    timeout_rows: list[dict[str, Any]] = []
    delay_signal_extreme: dict[str, list[float]] = defaultdict(list)
    delay_extreme_turn: dict[str, list[float]] = defaultdict(list)
    delay_signal_turn: dict[str, list[float]] = defaultdict(list)
    turn_lag_buckets = Counter()
    sensitivity: dict[tuple[str, int], dict[str, Any]] = {}

    # Pre-init sensitivity counters
    for tf, windows in TIMEOUT_GRID.items():
        for w in windows:
            sensitivity[(tf, w)] = {
                "tf": tf,
                "timeout_window": w,
                "signals": 0,
                "triggered": 0,
                "timeouts": 0,
                "wins": 0,
                "losses": 0,
                "open": 0,
                "pnl_sum": 0.0,
                "mae_list": [],
                "baseline_winners_missed": 0,
                "baseline_losers_avoided": 0,
            }

    for r in api_rows:
        sym = str(r.get("symbol") or "").upper()
        side = str(r.get("direction") or "").upper()
        tf = str(r.get("timeframe") or "")
        sid = str(r.get("signal_id") or "")
        signal_ts = r.get("candle_close_time") or r.get("generated_at")
        baseline_entry_ts = r.get("entry_time") or signal_ts
        try:
            baseline_px = float(r["entry_price"]) if r.get("entry_price") is not None else None
        except (TypeError, ValueError):
            baseline_px = None
        if not sym or side not in ("LONG", "SHORT") or not tf or signal_ts is None:
            continue

        work = candle_cache.get(sym, pd.DataFrame())
        t0 = _utc(baseline_entry_ts)
        sig_t = _utc(signal_ts)
        timeout_m = default_timeout(tf)
        deadline = t0 + timedelta(minutes=timeout_m)

        # Evaluate production-default timing at as_of=now
        timing = evaluate_1m_entry_timing(
            direction=side,
            signal_tf=tf,
            signal_ts=sig_t,
            baseline_entry_ts=t0,
            baseline_entry_price=baseline_px,
            candles_1m=work if not work.empty else pd.DataFrame({"open_time": [], "open": [], "high": [], "low": [], "close": []}),
            timing_variant=VARIANT,
            as_of=now,
            timeout_minutes=timeout_m,
        )

        # Window analysis (within default timeout)
        if work.empty:
            start_i = 0
            win = {
                "extreme_i": None,
                "turn_i": None,
                "entry_i": None,
                "min_k": None,
                "max_k": None,
                "last_i": None,
            }
            expected, present, cov = coverage_stats(work, t0, deadline)
            post = {
                "extreme_i": None,
                "turn_i": None,
                "entry_i": None,
                "min_k": None,
                "max_k": None,
                "last_i": None,
            }
        else:
            start_i = int(work["timestamp"].searchsorted(pd.Timestamp(t0), side="left"))
            win = find_extreme_and_turn(work, side=side, start_i=start_i, scan_end_ts=deadline)
            expected, present, cov = coverage_stats(work, t0, deadline)
            # Post-timeout scan up to +120m (and record first late turn even later for delay stats)
            post = find_extreme_and_turn(
                work,
                side=side,
                start_i=start_i,
                scan_end_ts=deadline + timedelta(minutes=max(LATE_HORIZONS)),
            )

        extreme_within = win["extreme_i"] is not None
        turn_within = win["turn_i"] is not None and win["entry_i"] is not None
        # Late turn: turn found in extended window but not within timeout
        turn_after = False
        late_delay = None
        if (not turn_within) and post["turn_i"] is not None and post["entry_i"] is not None:
            turn_ts = _utc(work.iloc[post["turn_i"]]["timestamp"])
            if turn_ts > deadline:
                turn_after = True
                late_delay = (turn_ts - deadline).total_seconds() / 60.0

        # Also check if extreme only after timeout
        extreme_after_only = (not extreme_within) and post["extreme_i"] is not None
        if extreme_after_only and post["turn_i"] is not None:
            turn_ts = _utc(work.iloc[post["turn_i"]]["timestamp"])
            if turn_ts > deadline:
                turn_after = True
                late_delay = (turn_ts - deadline).total_seconds() / 60.0

        root = None
        post_timeout_class = None
        if timing.trigger_state == TRIGGER_NO_ENTRY_TIMEOUT:
            # In-window primary failure mode (exclusive)
            if cov < 80.0:
                root = "DATA_COVERAGE_PROBLEM"
            elif extreme_within and not turn_within:
                root = "EXTREME_REACHED_BUT_NO_TURN"
            elif not extreme_within:
                root = "EXTREME_NEVER_REACHED"
            else:
                root = "OTHER"
            # Separate post-timeout label
            if turn_after:
                post_timeout_class = "TURN_CAME_AFTER_TIMEOUT"
            else:
                post_timeout_class = "NEVER_TRIGGERED_WITHIN_+120M"

        # Delay stats for cases with extreme/turn (any time within +120 from signal, using post scan)
        # Prefer first extreme/turn from extended window for delay distributions among non-timeouts and timeouts alike
        ext_i = post["extreme_i"] if post["extreme_i"] is not None else win["extreme_i"]
        trn_i = post["turn_i"] if post["turn_i"] is not None else win["turn_i"]
        if ext_i is not None and not work.empty:
            ext_ts = _utc(work.iloc[ext_i]["timestamp"])
            se = (ext_ts - t0).total_seconds() / 60.0
            if se >= 0:
                delay_signal_extreme[tf].append(se)
        if ext_i is not None and trn_i is not None and not work.empty:
            ext_ts = _utc(work.iloc[ext_i]["timestamp"])
            trn_ts = _utc(work.iloc[trn_i]["timestamp"])
            et = (trn_ts - ext_ts).total_seconds() / 60.0
            st = (trn_ts - t0).total_seconds() / 60.0
            if et >= 0:
                delay_extreme_turn[tf].append(et)
            if st >= 0:
                delay_signal_turn[tf].append(st)
                if st <= 15:
                    turn_lag_buckets["0-15m"] += 1
                elif st <= 30:
                    turn_lag_buckets["15-30m"] += 1
                elif st <= 60:
                    turn_lag_buckets["30-60m"] += 1
                elif st <= 120:
                    turn_lag_buckets["60-120m"] += 1
                elif st <= 240:
                    turn_lag_buckets["120-240m"] += 1
                else:
                    turn_lag_buckets[">240m"] += 1

        # Baseline outcome (immediate entry)
        baseline_sim = {
            "result": "OPEN",
            "pnl_pct": None,
            "mae_pct": None,
            "mfe_pct": None,
        }
        if not work.empty and start_i < len(work):
            b_px = baseline_px if baseline_px else float(work.iloc[start_i]["open"])
            baseline_sim = _simulate_outcome(
                side=side,
                entry=b_px,
                tf=tf,
                entry_i=start_i,
                work=work,
                as_of=now,
            )

        # Late counterfactual entry if late trigger exists
        late_trigger = False
        late_trigger_delay = None
        late_sim = None
        for h in LATE_HORIZONS:
            scan = find_extreme_and_turn(
                work,
                side=side,
                start_i=start_i,
                scan_end_ts=deadline + timedelta(minutes=h),
            )
            if scan["entry_i"] is not None and (
                win["entry_i"] is None
                or (win["turn_i"] is None)
            ):
                # entry must be after deadline
                ets = _utc(work.iloc[scan["entry_i"]]["timestamp"])
                if ets > deadline:
                    late_trigger = True
                    late_trigger_delay = (ets - deadline).total_seconds() / 60.0
                    late_sim = _simulate_outcome(
                        side=side,
                        entry=float(work.iloc[scan["entry_i"]]["open"]),
                        tf=tf,
                        entry_i=scan["entry_i"],
                        work=work,
                        as_of=now,
                    )
                    break

        # GOOD/BAD/NEUTRAL timeout classification
        timeout_quality = None
        if timing.trigger_state == TRIGGER_NO_ENTRY_TIMEOUT:
            b_res = outcome_label(baseline_sim)
            if b_res == "LOSS":
                timeout_quality = "GOOD_TIMEOUT"
            elif b_res == "WIN":
                if late_trigger and late_sim and outcome_label(late_sim) in ("WIN", "OPEN"):
                    timeout_quality = "BAD_TIMEOUT"
                elif late_trigger and late_sim and outcome_label(late_sim) == "LOSS":
                    timeout_quality = "NEUTRAL_TIMEOUT"  # avoided later loss? still missed baseline win
                    # Spec: BAD if baseline TP and later trigger would still work sensibly
                    timeout_quality = "BAD_TIMEOUT"
                else:
                    timeout_quality = "BAD_TIMEOUT"  # missed winner, no useful late trigger
            else:
                timeout_quality = "NEUTRAL_TIMEOUT"

        extreme_at = (
            _iso(work.iloc[win["extreme_i"]]["timestamp"])
            if win["extreme_i"] is not None and not work.empty
            else None
        )
        # For late extreme display
        if extreme_at is None and post["extreme_i"] is not None and not work.empty:
            # only mark extreme_reached if within timeout for the flag used in report
            pass

        row = {
            "signal_id": sid,
            "symbol": sym,
            "direction": side,
            "signal_tf": tf,
            "signal_ts": _iso(sig_t),
            "baseline_entry_ts": _iso(t0),
            "timeout_window": timeout_m,
            "timeout_at": _iso(deadline),
            "trigger_state": timing.trigger_state,
            "root_cause": root,
            "post_timeout_class": post_timeout_class,
            "min_1m_k": win["min_k"],
            "max_1m_k": win["max_k"],
            "k_proximity_bucket": k_bucket(side, win["min_k"], win["max_k"]),
            "extreme_reached": bool(extreme_within),
            "extreme_reached_at": extreme_at,
            "turn_cross_reached": bool(turn_within),
            "turn_cross_at": (
                _iso(work.iloc[win["turn_i"]]["timestamp"])
                if win["turn_i"] is not None and not work.empty
                else None
            ),
            "minutes_after_timeout_if_late": late_delay if turn_after else None,
            "1m_candles_expected": expected,
            "1m_candles_present": present,
            "coverage_pct": round(cov, 2),
            "baseline_outcome": outcome_label(baseline_sim),
            "baseline_pnl": baseline_sim.get("pnl_pct"),
            "baseline_mae": baseline_sim.get("mae_pct"),
            "baseline_mfe": baseline_sim.get("mfe_pct"),
            "timing_outcome": timing.result if timing.trigger_state == TRIGGER_ENTRY_TRIGGERED else timing.trigger_state,
            "timing_pnl": timing.pnl_pct,
            "timing_mae": timing.mae_pct,
            "late_trigger": late_trigger,
            "late_trigger_delay_minutes": late_trigger_delay,
            "late_timing_outcome": outcome_label(late_sim) if late_sim else None,
            "late_timing_mae": late_sim.get("mae_pct") if late_sim else None,
            "late_timing_pnl": late_sim.get("pnl_pct") if late_sim else None,
            "timeout_quality": timeout_quality,
        }
        detail_rows.append(row)
        if timing.trigger_state == TRIGGER_NO_ENTRY_TIMEOUT:
            timeout_rows.append(row)

        # Sensitivity grid (same signal, varying timeout)
        for w in TIMEOUT_GRID.get(tf, ()):
            sens = sensitivity[(tf, w)]
            sens["signals"] += 1
            scan = find_extreme_and_turn(
                work, side=side, start_i=start_i, scan_end_ts=t0 + timedelta(minutes=w)
            )
            if scan["entry_i"] is not None:
                sens["triggered"] += 1
                sim = _simulate_outcome(
                    side=side,
                    entry=float(work.iloc[scan["entry_i"]]["open"]),
                    tf=tf,
                    entry_i=scan["entry_i"],
                    work=work,
                    as_of=now,
                )
                res = outcome_label(sim)
                if res == "WIN":
                    sens["wins"] += 1
                elif res == "LOSS":
                    sens["losses"] += 1
                else:
                    sens["open"] += 1
                if sim.get("pnl_pct") is not None:
                    sens["pnl_sum"] += float(sim["pnl_pct"])
                if sim.get("mae_pct") is not None:
                    sens["mae_list"].append(float(sim["mae_pct"]))
            else:
                sens["timeouts"] += 1
                b_res = outcome_label(baseline_sim)
                if b_res == "WIN":
                    sens["baseline_winners_missed"] += 1
                elif b_res == "LOSS":
                    sens["baseline_losers_avoided"] += 1

    # Aggregations
    n_total = len(detail_rows)
    n_timeout = len(timeout_rows)
    root_counts = Counter(r["root_cause"] for r in timeout_rows if r["root_cause"])
    post_counts = Counter(r["post_timeout_class"] for r in timeout_rows if r.get("post_timeout_class"))
    quality_counts = Counter(r["timeout_quality"] for r in timeout_rows if r["timeout_quality"])
    bucket_counts = Counter(r["k_proximity_bucket"] for r in timeout_rows)

    winners_missed = sum(1 for r in timeout_rows if r["baseline_outcome"] == "WIN")
    losers_avoided = sum(1 for r in timeout_rows if r["baseline_outcome"] == "LOSS")

    # Per TF summary
    tf_summary = []
    for tf in ("15m", "30m", "1h", "4h"):
        subset = [r for r in detail_rows if r["signal_tf"] == tf]
        tos = [r for r in subset if r["trigger_state"] == TRIGGER_NO_ENTRY_TIMEOUT]
        ext = [r for r in tos if r["extreme_reached"]]
        late = [r for r in tos if r.get("post_timeout_class") == "TURN_CAME_AFTER_TIMEOUT"]
        missed = sum(1 for r in tos if r["baseline_outcome"] == "WIN")
        never_ext = sum(1 for r in tos if r["root_cause"] == "EXTREME_NEVER_REACHED")
        no_turn = sum(1 for r in tos if r["root_cause"] == "EXTREME_REACHED_BUT_NO_TURN")
        tf_summary.append(
            {
                "tf": tf,
                "signals": len(subset),
                "timeouts": len(tos),
                "timeout_pct": (100.0 * len(tos) / len(subset)) if subset else None,
                "extreme_reached_pct_of_timeouts": (100.0 * len(ext) / len(tos)) if tos else None,
                "extreme_never_pct_of_timeouts": (100.0 * never_ext / len(tos)) if tos else None,
                "extreme_no_turn_pct_of_timeouts": (100.0 * no_turn / len(tos)) if tos else None,
                "late_turn_pct_of_timeouts": (100.0 * len(late) / len(tos)) if tos else None,
                "baseline_winners_missed": missed,
                "median_signal_to_extreme": _median(delay_signal_extreme[tf]),
                "median_extreme_to_turn": _median(delay_extreme_turn[tf]),
                "p90_signal_to_turn": _pctile(delay_signal_turn[tf], 90),
                "p75_signal_to_turn": _pctile(delay_signal_turn[tf], 75),
                "p95_signal_to_turn": _pctile(delay_signal_turn[tf], 95),
            }
        )

    sens_rows = []
    for (tf, w), s in sorted(sensitivity.items()):
        closed = s["wins"] + s["losses"]
        wr = (100.0 * s["wins"] / closed) if closed else None
        sens_rows.append(
            {
                "tf": tf,
                "timeout_window": w,
                "signals": s["signals"],
                "triggered": s["triggered"],
                "timeouts": s["timeouts"],
                "coverage_triggered_pct": (100.0 * s["triggered"] / s["signals"]) if s["signals"] else None,
                "winrate_triggered": wr,
                "net_pnl": s["pnl_sum"],
                "median_mae": _median(s["mae_list"]),
                "baseline_winners_missed": s["baseline_winners_missed"],
                "baseline_losers_avoided": s["baseline_losers_avoided"],
            }
        )

    # Primary decision
    never = root_counts.get("EXTREME_NEVER_REACHED", 0)
    no_turn = root_counts.get("EXTREME_REACHED_BUT_NO_TURN", 0)
    late = post_counts.get("TURN_CAME_AFTER_TIMEOUT", 0)
    cov_prob = root_counts.get("DATA_COVERAGE_PROBLEM", 0)
    other = root_counts.get("OTHER", 0)

    # Near-miss: timeouts with K just outside 20/80 during wait window
    near_miss_strict = 0
    for r in timeout_rows:
        if r["root_cause"] != "EXTREME_NEVER_REACHED":
            continue
        if r["direction"] == "LONG" and r["min_1m_k"] is not None and 20 < float(r["min_1m_k"]) <= 25:
            near_miss_strict += 1
        if r["direction"] == "SHORT" and r["max_1m_k"] is not None and 75 <= float(r["max_1m_k"]) < 80:
            near_miss_strict += 1

    # TF heterogeneity: timeout rate spread
    tf_rates = [t["timeout_pct"] for t in tf_summary if t["timeout_pct"] is not None]
    tf_spread = (max(tf_rates) - min(tf_rates)) if tf_rates else 0

    # Sensitivity evidence: extending 15m from 15→30 recovers many triggers
    sens_15_15 = next((s for s in sens_rows if s["tf"] == "15m" and s["timeout_window"] == 15), None)
    sens_15_30 = next((s for s in sens_rows if s["tf"] == "15m" and s["timeout_window"] == 30), None)
    lift_15 = 0.0
    if sens_15_15 and sens_15_30 and sens_15_15["coverage_triggered_pct"] is not None:
        lift_15 = float(sens_15_30["coverage_triggered_pct"]) - float(sens_15_15["coverage_triggered_pct"])

    useful = losers_avoided > winners_missed and n_timeout > 0

    if cov_prob >= 0.4 * n_timeout and n_timeout:
        primary = "DATA_COVERAGE_CAUSES_TIMEOUTS"
    elif late >= 0.7 * n_timeout and lift_15 >= 25:
        # Dominant mechanism: triggers arrive shortly after 1×TF deadline (esp. 15m)
        primary = "TIMEOUT_TOO_SHORT"
    elif near_miss_strict >= 0.25 * n_timeout and never >= 0.4 * n_timeout:
        primary = "STOCH_THRESHOLD_TOO_STRICT"
    elif no_turn >= 0.4 * n_timeout and late < 0.5 * n_timeout:
        primary = "TURN_CONFIRMATION_TOO_STRICT"
    elif tf_spread >= 40:
        primary = "MIXED_BY_TIMEFRAME"
    elif useful:
        primary = "TIMEOUTS_ARE_USEFUL_FILTER"
    elif never >= 0.5 * n_timeout and late >= 0.7 * n_timeout:
        primary = "TIMEOUT_TOO_SHORT"
    else:
        primary = "MIXED_BY_TIMEFRAME"

    # Write artifacts
    write_csv(OUT / "timeout_root_cause.csv", timeout_rows if timeout_rows else detail_rows[:0])
    # Also write full detail for transparency
    write_csv(OUT / "all_signals_timing_detail.csv", detail_rows)
    write_csv(OUT / "timeout_summary.csv", tf_summary)
    write_csv(OUT / "timeout_window_sensitivity.csv", sens_rows)

    # Compact summary markdown
    lines = [
        "# NO_ENTRY_TIMEOUT Root Cause Audit",
        "",
        f"Variant: `{VARIANT}`",
        f"Window: last **{HOURS}h** Tier-A (as_of `{_iso(now)}`)",
        f"Default timeout: **1× signal TF bar** (`TF_BAR_MIN`)",
        "",
        "## Primary Decision",
        "",
        f"`{primary}`",
        "",
        "## Totals",
        "",
        f"- Total Tier-A: **{n_total}**",
        f"- Total NO_ENTRY_TIMEOUT: **{n_timeout}**",
        f"- Timeout %: **{(100.0 * n_timeout / n_total) if n_total else 0:.1f}%**",
        f"- ENTRY_TRIGGERED: **{sum(1 for r in detail_rows if r['trigger_state']==TRIGGER_ENTRY_TRIGGERED)}**",
        "",
        "## Root cause",
        "",
        "### In-window (why the timeout fired)",
        "",
        f"- EXTREME_NEVER_REACHED: **{never}**",
        f"- EXTREME_REACHED_BUT_NO_TURN: **{no_turn}**",
        f"- DATA_COVERAGE_PROBLEM: **{cov_prob}**",
        f"- OTHER: **{other}**",
        "",
        "### Post-timeout (did a valid Extreme+Turn still arrive later?)",
        "",
        f"- TURN_CAME_AFTER_TIMEOUT (within +120m): **{late}**",
        f"- NEVER_TRIGGERED_WITHIN_+120M: **{post_counts.get('NEVER_TRIGGERED_WITHIN_+120M', 0)}**",
        f"- late_trigger rate: **{(100.0 * late / n_timeout) if n_timeout else 0:.1f}%**",
        "",
        "Note: 15m accounts for almost all timeouts (see Per TF). Higher TFs (1h/4h) show ~0% timeout at 1× bar window.",
        "",
        "## Were we right to time out?",
        "",
        f"- baseline_winners_lost_due_to_timeout: **{winners_missed}**",
        f"- baseline_losers_avoided_due_to_timeout: **{losers_avoided}**",
        f"- quality counts: `{dict(quality_counts)}`",
        "",
        "## How close were we? (timeout K proximity)",
        "",
        f"`{dict(bucket_counts)}`",
        f"- Near-miss (LONG minK in (20,25] or SHORT maxK in [75,80)): **{near_miss_strict}**",
        "",
        "## Per TF",
        "",
        "| TF | Signals | Timeouts | Timeout% | Extreme reached% | Late turn% | Baseline winners missed |",
        "| -- | ------: | -------: | -------: | ---------------: | ---------: | ----------------------: |",
    ]
    for t in tf_summary:
        lines.append(
            f"| {t['tf']} | {t['signals']} | {t['timeouts']} | "
            f"{t['timeout_pct'] if t['timeout_pct'] is None else f'{t['timeout_pct']:.1f}'} | "
            f"{t['extreme_reached_pct_of_timeouts'] if t['extreme_reached_pct_of_timeouts'] is None else f'{t['extreme_reached_pct_of_timeouts']:.1f}'} | "
            f"{t['late_turn_pct_of_timeouts'] if t['late_turn_pct_of_timeouts'] is None else f'{t['late_turn_pct_of_timeouts']:.1f}'} | "
            f"{t['baseline_winners_missed']} |"
        )

    lines += [
        "",
        "## Delay stats (cases with extreme/turn observed within +120m)",
        "",
        "| TF | median signal→extreme | median extreme→turn | p75 signal→turn | p90 signal→turn | p95 signal→turn |",
        "| -- | --------------------: | ------------------: | --------------: | --------------: | --------------: |",
    ]
    for t in tf_summary:
        lines.append(
            f"| {t['tf']} | {t['median_signal_to_extreme']} | {t['median_extreme_to_turn']} | "
            f"{t['p75_signal_to_turn']} | {t['p90_signal_to_turn']} | {t['p95_signal_to_turn']} |"
        )

    lines += [
        "",
        f"Turn lag buckets (signal→turn): `{dict(turn_lag_buckets)}`",
        "",
        "## Timeout window sensitivity (rule unchanged: 20/80 + K/D cross)",
        "",
        "| TF | Timeout window | Coverage (triggered%) | Winrate | Net | MAE med | Winners missed | Losers avoided |",
        "| -- | -------------: | --------------------: | ------: | --: | ------: | -------------: | -------------: |",
    ]
    for s in sens_rows:
        lines.append(
            f"| {s['tf']} | {s['timeout_window']} | "
            f"{s['coverage_triggered_pct'] if s['coverage_triggered_pct'] is None else f'{s['coverage_triggered_pct']:.1f}'} | "
            f"{s['winrate_triggered'] if s['winrate_triggered'] is None else f'{s['winrate_triggered']:.1f}'} | "
            f"{s['net_pnl']:.1f} | {s['median_mae']} | {s['baseline_winners_missed']} | {s['baseline_losers_avoided']} |"
        )

    lines += [
        "",
        "## Recommendation",
        "",
        "Research only — **do not change** Stoch thresholds, turn rule, or production strategy from this audit alone.",
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
    (OUT / "timeout_summary.md").write_text("\n".join(lines), encoding="utf-8")

    meta = {
        "primary_decision": primary,
        "n_total": n_total,
        "n_timeout": n_timeout,
        "root_counts": dict(root_counts),
        "post_timeout_counts": dict(post_counts),
        "quality_counts": dict(quality_counts),
        "winners_missed": winners_missed,
        "losers_avoided": losers_avoided,
        "near_miss_strict": near_miss_strict,
        "bucket_counts": dict(bucket_counts),
        "turn_lag_buckets": dict(turn_lag_buckets),
        "tf_spread": tf_spread,
        "lift_15m_15_to_30": lift_15,
    }
    (OUT / "run_metadata.json").write_text(json.dumps(meta, indent=2, default=str) + "\n", encoding="utf-8")

    print("PRIMARY", primary, flush=True)
    print("timeouts", n_timeout, "/", n_total, flush=True)
    print("roots", dict(root_counts), flush=True)
    print("wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
