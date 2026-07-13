#!/usr/bin/env python3
"""Read-only multilevel structure policy comparison (M20 / M30 / M50).

Compares Internal=5 + Swing∈{20,30,50} as audit-only direction gates on real
H1 pipeline setups. No production / policy / state-machine integration.
Nothing is staged or committed.
"""
from __future__ import annotations

import csv
import hashlib
import json
import resource
from collections import defaultdict
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd

from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.market_regime_macro_context_audit import aggregate_closed_htf
from research.regime_scanner.multilevel_market_structure import (
    BEARISH,
    BULLISH,
    run_multilevel_structure,
)
from research.regime_scanner.pipeline_counterfactual import compute_forward_outcome
from research.regime_scanner.point_audit import json_safe

OUT = Path("research/regime_scanner/results/multilevel_structure_policy_comparison_audit")
ROOT = Path("research/regime_scanner")
PIPELINE = Path(
    "research/backtests/results/regime_scanner_pipeline_audit_aptusdt_2026_h1"
)

PROTECTED = {
    "market_regime.py": "1e79f30af2ddf95c3f91c1b1a012cded",
    "trend_structure.py": "4976cbd9921e9df58dcfaace5cb125a2",
    "trend_state_machine.py": "3a8ed63f60f86ec29bf05e7831bb3349",
    "trend_state_policy.py": "412f672652b66c93b7d44d4b692da2aa",
    "trend_zones.py": "6378f736a184e51efe070ebd2c2d969c",
    "regime_snapshot.py": "e8eed043f62cb636b972dae3af7e5a48",
}

LOAD_START = "2025-12-27T00:00:00+00:00"
AUDIT_START = "2026-01-06T00:00:00+00:00"
AUDIT_END = "2026-03-16T23:59:00+00:00"
INTERNAL_SIZE = 5
SWING_LENGTHS = (20, 30, 50)
VARIANT_KEYS = {20: "M20", 30: "M30", 50: "M50"}

FOCUS = {
    "jan13_15": ("2026-01-13", "2026-01-15"),
    "jan19_31": ("2026-01-19", "2026-01-31"),
    "feb01_07": ("2026-01-29", "2026-02-07"),
    "mar05_10": ("2026-03-05", "2026-03-10"),
}

Decision = Literal["ALLOW", "BLOCK", "WAIT", "OBSERVE_ONLY"]


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object | None) -> str | None:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    return _ts(v).isoformat()


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _p(msg: str) -> None:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    print(f"{msg}  [rss≈{rss:.0f}MB]", flush=True)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
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
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else v) for k, v in r.items()})


def _truthy(v: object) -> bool:
    if v is True:
        return True
    if v is False or v is None:
        return False
    return str(v).strip().lower() in {"true", "1", "yes"}


def _empty_blockers(v: object) -> bool:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return True
    return str(v).strip() in {"", "[]", "()", "nan", "None"}


def bias_name(v: object) -> str:
    try:
        i = int(v or 0)
    except (TypeError, ValueError):
        return "flat"
    if i > 0:
        return "bullish"
    if i < 0:
        return "bearish"
    return "flat"


def asof_row(rows: list[dict[str, Any]], t: pd.Timestamp, key: str = "decision_timestamp_utc") -> dict[str, Any] | None:
    best = None
    for r in rows:
        dt = _ts(r[key])
        if dt <= t:
            best = r
        else:
            break
    return best


def latest_event_asof(events: list[dict[str, Any]], t: pd.Timestamp) -> str | None:
    best = None
    for e in events:
        if _ts(e["event_decision_timestamp"]) > t:
            break
        best = e
    if best is None:
        return None
    return f"{best['structure_level']}:{best['direction']}_{best['event_type']}@{best['event_decision_timestamp']}"


def policy_decision(
    *,
    direction: str,
    internal_bias: int,
    swing_bias: int,
    primary_label: str,
) -> tuple[Decision, str]:
    """Audit-only multilevel direction gate.

    Reversal labels are evaluated first: possible_* always WAIT, confirmed_*
    only releases the new direction when Internal agrees. Bias branches then
    handle aligned / recovery / pullback blocking.
    """
    is_long = direction == "long"
    is_short = direction == "short"
    label = str(primary_label or "insufficient_structure")

    if swing_bias == 0 and internal_bias == 0:
        return "OBSERVE_ONLY", "insufficient_structure"

    # --- Reversal episode labels (priority over raw swing bias) ---
    if label == "possible_bullish_swing_reversal":
        return "WAIT", "possible_bullish_swing_reversal"
    if label == "possible_bearish_swing_reversal":
        return "WAIT", "possible_bearish_swing_reversal"

    if label == "confirmed_bullish_swing_reversal":
        if is_long:
            if internal_bias == BULLISH:
                return "ALLOW", "confirmed_bull_rev_and_internal_bull"
            return "WAIT", "confirmed_bull_rev_await_internal"
        if is_short:
            return "WAIT", "confirmed_bull_rev_pause_shorts"

    if label == "confirmed_bearish_swing_reversal":
        if is_short:
            if internal_bias == BEARISH:
                return "ALLOW", "confirmed_bear_rev_and_internal_bear"
            return "WAIT", "confirmed_bear_rev_await_internal"
        if is_long:
            if swing_bias == BULLISH:
                return "WAIT", "confirmed_bear_rev_pause_longs"
            return "BLOCK", "confirmed_bearish_long_blocked"

    # --- Swing bearish regime ---
    if swing_bias == BEARISH:
        if is_long:
            if label == "bullish_recovery_inside_bearish_swing":
                return "BLOCK", "recovery_long_blocked"
            if label == "aligned_bearish":
                return "BLOCK", "aligned_bearish_long_blocked"
            if internal_bias == BULLISH:
                return "BLOCK", "internal_bull_swing_bear_recovery_block"
            return "BLOCK", "swing_bearish_long_blocked"
        if is_short:
            return "ALLOW", "swing_bearish_short_allowed"

    # --- Swing bullish regime ---
    if swing_bias == BULLISH:
        if is_short:
            if label in {
                "bearish_pullback_inside_bullish_swing",
                "bearish_recovery_inside_bullish_swing",
            }:
                return "BLOCK", "pullback_short_blocked"
            if label == "aligned_bullish":
                return "BLOCK", "aligned_bullish_short_blocked"
            if internal_bias == BEARISH:
                return "BLOCK", "internal_bear_swing_bull_pullback_block"
            return "BLOCK", "swing_bullish_short_blocked"
        if is_long:
            return "ALLOW", "swing_bullish_long_allowed"

    return "OBSERVE_ONLY", "swing_flat_observe"


def entry_price_at(c5: pd.DataFrame, ts: pd.Timestamp) -> float | None:
    sub = c5.loc[c5["decision_time"] <= ts]
    if sub.empty:
        return None
    return float(sub.iloc[-1]["close"])


def later_moves(c5: pd.DataFrame, entry_ts: pd.Timestamp, entry_px: float, side: str) -> dict[str, float | None]:
    out: dict[str, float | None] = {f"later_move_{h}h": None for h in (1, 2, 4, 8)}
    if not entry_px:
        return out
    future = c5.loc[c5["decision_time"] > entry_ts]
    if future.empty:
        return out
    sign = 1.0 if side == "long" else -1.0
    for h in (1, 2, 4, 8):
        target = entry_ts + pd.Timedelta(hours=h)
        window = future.loc[future["decision_time"] <= target]
        if window.empty:
            continue
        cl = float(window.iloc[-1]["close"])
        out[f"later_move_{h}h"] = sign * (cl - entry_px) / entry_px * 100.0
    return out


def classify_result(mfe: float | None, mae: float | None, move_4h: float | None) -> str:
    if mfe is None or mae is None:
        return "unknown"
    if mfe >= 0.25 and mae < max(mfe, 0.25):
        return "winner"
    if mae >= 0.5 and (move_4h is None or move_4h <= 0):
        return "loser"
    if move_4h is not None and move_4h > 0.15:
        return "winner"
    if move_4h is not None and move_4h < -0.25:
        return "loser"
    return "mixed"


def episode_durations(rows: list[dict[str, Any]], label_pred) -> list[float]:
    """Return durations in hours for contiguous episodes matching predicate."""
    if not rows:
        return []
    durs: list[float] = []
    start = None
    prev_t = None
    for r in rows:
        t = _ts(r["decision_timestamp_utc"])
        hit = bool(label_pred(r))
        if hit and start is None:
            start = t
        if start is not None and (not hit):
            # close episode at previous bar
            if prev_t is not None:
                durs.append((prev_t - start).total_seconds() / 3600.0 + 0.5)
            start = None
        prev_t = t
    if start is not None and prev_t is not None:
        durs.append((prev_t - start).total_seconds() / 3600.0 + 0.5)
    return durs


def swing_reversal_episodes(
    rows: list[dict[str, Any]], events: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """False/true/missed reversals + delays from possible→confirmed on timeline."""
    a0, a1 = _ts(AUDIT_START), _ts(AUDIT_END)
    swing_ev = [
        e
        for e in events
        if e.get("structure_level") == "swing" and a0 <= _ts(e["event_decision_timestamp"]) <= a1
    ]
    possible_starts: list[dict[str, Any]] = []
    prev = None
    for r in rows:
        lab = str(r.get("combined_primary_label") or "")
        is_pos = lab.startswith("possible_")
        if is_pos and (prev is None or not str(prev.get("combined_primary_label") or "").startswith("possible_")):
            possible_starts.append(r)
        prev = r

    true_rev = 0
    false_rev = 0
    delays: list[float] = []
    episode_rows: list[dict[str, Any]] = []
    for start in possible_starts:
        t0 = _ts(start["decision_timestamp_utc"])
        direction = "bullish" if "bullish" in str(start.get("combined_primary_label")) else "bearish"
        confirmed = False
        failed = False
        fail_reason = ""
        t_conf = None
        t_end = None
        end_label = ""
        for r in rows:
            t = _ts(r["decision_timestamp_utc"])
            if t < t0:
                continue
            lab = str(r.get("combined_primary_label") or "")
            if lab == f"confirmed_{direction}_swing_reversal":
                confirmed = True
                t_conf = t
                t_end = t
                end_label = lab
                break
            if direction == "bullish" and int(r.get("swing_bias") or 0) == BEARISH and lab == "aligned_bearish":
                failed = True
                fail_reason = "reverted_aligned_bearish"
                t_end = t
                end_label = lab
                break
            if direction == "bearish" and int(r.get("swing_bias") or 0) == BULLISH and lab == "aligned_bullish":
                failed = True
                fail_reason = "reverted_aligned_bullish"
                t_end = t
                end_label = lab
                break
            if t > t0 + pd.Timedelta(days=14):
                failed = True
                fail_reason = "timeout_14d"
                t_end = t
                end_label = lab
                break
        if confirmed and t_conf is not None:
            true_rev += 1
            delays.append((t_conf - t0).total_seconds() / 3600.0)
            outcome = "confirmed"
        else:
            false_rev += 1
            outcome = "failed"
            if not failed:
                fail_reason = "no_confirm_in_window"
                t_end = t_end or (_ts(rows[-1]["decision_timestamp_utc"]) if rows else t0)
                end_label = end_label or str(rows[-1].get("combined_primary_label") if rows else "")
        episode_rows.append(
            {
                "start_decision_timestamp_utc": _iso(t0),
                "end_decision_timestamp_utc": _iso(t_end) if t_end is not None else "",
                "direction": direction,
                "start_label": start.get("combined_primary_label"),
                "end_label": end_label,
                "outcome": outcome,
                "fail_reason": fail_reason if outcome == "failed" else "",
                "delay_hours": (
                    None
                    if t_conf is None
                    else (t_conf - t0).total_seconds() / 3600.0
                ),
                "internal_bias_at_start": start.get("internal_bias"),
                "swing_bias_at_start": start.get("swing_bias"),
                "close_at_start": start.get("close"),
            }
        )

    confirmed_events = [
        e for e in swing_ev if e.get("event_type") == "bos" and e.get("direction") in {"bullish", "bearish"}
    ]
    choch_to_bos_delays: list[float] = []
    missed = 0
    for d in ("bullish", "bearish"):
        pending = None
        for e in swing_ev:
            if e["direction"] != d:
                if e.get("event_type") == "choch":
                    pending = None
                continue
            if e["event_type"] == "choch":
                pending = e
            elif e["event_type"] == "bos" and pending is not None:
                choch_to_bos_delays.append(
                    (_ts(e["event_decision_timestamp"]) - _ts(pending["event_decision_timestamp"])).total_seconds()
                    / 3600.0
                )
                pending = None

    stats = {
        "n_possible_episodes": len(possible_starts),
        "true_reversals": true_rev,
        "false_reversals": false_rev,
        "missed_reversals": missed,
        "median_possible_to_confirmed_hours": float(np.median(delays)) if delays else None,
        "median_choch_to_bos_hours": float(np.median(choch_to_bos_delays)) if choch_to_bos_delays else None,
        "n_choch_bos_pairs": len(choch_to_bos_delays),
        "n_swing_events": len(swing_ev),
        "n_confirmed_events": len(confirmed_events),
    }
    return stats, episode_rows


def swing_reversal_stats(rows: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    stats, _ = swing_reversal_episodes(rows, events)
    return stats


def metrics_for_variant(
    rows: list[dict[str, Any]],
    *,
    key: str,
    timeline: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    dec = f"{key}_decision"
    allowed = [r for r in rows if r.get(dec) == "ALLOW"]
    blocked = [r for r in rows if r.get(dec) == "BLOCK"]
    wait = [r for r in rows if r.get(dec) == "WAIT"]

    def move_stats(subset: list[dict[str, Any]]) -> dict[str, float | None]:
        moves = [float(r["later_move_4h"]) for r in subset if r.get("later_move_4h") is not None]
        if not moves:
            return {
                "sum_move_4h": None,
                "mean_move_4h": None,
                "profit_factor_proxy": None,
                "win_rate_4h": None,
                "drawdown_proxy": None,
            }
        wins = [m for m in moves if m > 0]
        losses = [m for m in moves if m <= 0]
        gp = sum(wins) if wins else 0.0
        gl = abs(sum(losses)) if losses else 0.0
        pf = (gp / gl) if gl > 0 else (None if gp == 0 else 99.0)
        # drawdown proxy: worst cumulative path of allowed 4h moves in time order
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for r in sorted(subset, key=lambda x: x["timestamp_utc"]):
            m = r.get("later_move_4h")
            if m is None:
                continue
            cum += float(m)
            peak = max(peak, cum)
            max_dd = min(max_dd, cum - peak)
        return {
            "sum_move_4h": float(sum(moves)),
            "mean_move_4h": float(np.mean(moves)),
            "profit_factor_proxy": pf,
            "win_rate_4h": len(wins) / len(moves),
            "drawdown_proxy": float(max_dd),
        }

    # Countertrend relative to swing bias of this variant
    bias_col = f"swing_bias_len{key[1:]}" if key.startswith("M") else None
    # key is M20 -> length 20
    length = int(key[1:])
    bias_col = f"swing_bias_len{length}"
    ctx_col = f"combined_context_len{length}"

    ct = []
    for r in rows:
        sb = bias_name(r.get(bias_col))
        if r["direction"] == "long" and sb == "bearish":
            ct.append(r)
        elif r["direction"] == "short" and sb == "bullish":
            ct.append(r)

    ct_blocked = [r for r in ct if r.get(dec) in {"BLOCK", "WAIT"}]
    ct_blocked_adverse = [
        r for r in ct_blocked if r.get("later_move_4h") is not None and float(r["later_move_4h"]) < 0
    ]
    ct_blocked_favorable = [
        r for r in ct_blocked if r.get("later_move_4h") is not None and float(r["later_move_4h"]) > 0
    ]

    prevented = [
        r
        for r in rows
        if r.get("baseline_allow")
        and r.get(dec) in {"BLOCK", "WAIT"}
        and r.get("result_class") == "loser"
    ]
    blocked_winners = [
        r
        for r in rows
        if r.get("baseline_allow")
        and r.get(dec) in {"BLOCK", "WAIT"}
        and r.get("result_class") == "winner"
    ]

    rev = swing_reversal_stats(timeline, events)
    rec_durs = episode_durations(
        timeline, lambda r: r.get("combined_primary_label") == "bullish_recovery_inside_bearish_swing"
        or r.get("combined_primary_label")
        in {"bearish_pullback_inside_bullish_swing", "bearish_recovery_inside_bullish_swing"}
    )
    pos_durs = episode_durations(
        timeline, lambda r: str(r.get("combined_primary_label") or "").startswith("possible_")
    )

    # possible → confirmed conversion rate
    conversion = None
    if rev["n_possible_episodes"]:
        conversion = rev["true_reversals"] / rev["n_possible_episodes"]

    st = move_stats(allowed)
    return {
        "variant": key,
        "swing_length": length,
        "n_allow": len(allowed),
        "n_block": len(blocked),
        "n_wait": len(wait),
        "n_countertrend_setups": len(ct),
        "countertrend_blocked": len(ct_blocked),
        "countertrend_blocked_adverse_4h": len(ct_blocked_adverse),
        "countertrend_blocked_favorable_4h": len(ct_blocked_favorable),
        "net_ct_block_edge": len(ct_blocked_adverse) - len(ct_blocked_favorable),
        "prevented_losers": len(prevented),
        "blocked_winners": len(blocked_winners),
        "false_reversals": rev["false_reversals"],
        "true_reversals": rev["true_reversals"],
        "missed_reversals": rev["missed_reversals"],
        "n_possible_episodes": rev["n_possible_episodes"],
        "possible_to_confirmed_rate": conversion,
        "median_reversal_delay_hours": rev["median_possible_to_confirmed_hours"],
        "median_choch_to_bos_hours": rev["median_choch_to_bos_hours"],
        "avg_recovery_duration_hours": float(np.mean(rec_durs)) if rec_durs else None,
        "avg_possible_reversal_duration_hours": float(np.mean(pos_durs)) if pos_durs else None,
        "n_recovery_episodes": len(rec_durs),
        "n_possible_duration_episodes": len(pos_durs),
        "sum_move_4h_allowed": st["sum_move_4h"],
        "mean_move_4h_allowed": st["mean_move_4h"],
        "profit_factor_proxy_allowed": st["profit_factor_proxy"],
        "win_rate_4h_allowed": st["win_rate_4h"],
        "drawdown_proxy_allowed": st["drawdown_proxy"],
        "ctx_col": ctx_col,
    }


def focus_slice(rows: list[dict[str, Any]], lo: str, hi: str) -> list[dict[str, Any]]:
    a = _ts(lo)
    b = _ts(hi) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    return [r for r in rows if a <= _ts(r["timestamp_utc"]) <= b]


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    hashes_before = {n: _md5(ROOT / n) for n in PROTECTED}
    for n, exp in PROTECTED.items():
        if hashes_before[n] != exp:
            raise SystemExit(f"hash mismatch before: {n}")

    for n in ("trend_state_policy.py", "trend_state_machine.py"):
        if "multilevel_structure_policy_comparison" in (ROOT / n).read_text(encoding="utf-8"):
            raise SystemExit(f"unexpected comparison import in {n}")

    _p("load candles + setups")
    raw = load_symbol_candles("APTUSDT")
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    end_wall = _ts(AUDIT_END)
    load_start = _ts(LOAD_START)
    sl = raw[(raw["timestamp"] >= load_start) & (raw["timestamp"] <= _ts("2026-03-16 23:55:00+00:00"))].copy()
    ohlcv5 = sl[["timestamp", "open", "high", "low", "close", "volume"]]
    c5 = ohlcv5.copy()
    c5["decision_time"] = c5["timestamp"] + pd.Timedelta(minutes=5)

    setups = pd.read_csv(PIPELINE / "setup_activations.csv")
    setups["timestamp_utc"] = pd.to_datetime(setups["setup_activation_timestamp"], utc=True)
    a0, a1 = _ts(AUDIT_START), _ts(AUDIT_END)
    setups = setups[(setups["timestamp_utc"] >= a0) & (setups["timestamp_utc"] <= a1)].copy()

    mom = pd.read_csv(PIPELINE / "momentum_confirmations.csv")
    mom_ids = set(str(x) for x in mom["setup_id"].tolist()) if len(mom) else set()

    _p("aggregate closed 30m")
    agg30 = aggregate_closed_htf(ohlcv5, 30, end_wall)

    timelines: dict[int, list[dict[str, Any]]] = {}
    engines: dict[int, Any] = {}
    events_by: dict[int, list[dict[str, Any]]] = {}

    for length in SWING_LENGTHS:
        _p(f"build multilevel timeline swing={length}")
        rows, eng = run_multilevel_structure(
            agg30, internal_size=INTERNAL_SIZE, swing_size=length, timeframe="30m"
        )
        # audit-window filter for metrics; keep full for asof warmup
        timelines[length] = rows
        engines[length] = eng
        events_by[length] = [
            e.to_dict()
            for e in eng.all_events
            if e.structure_level in {"internal", "swing"}
        ]
        # determinism spot-check
        rows2, _ = run_multilevel_structure(
            agg30, internal_size=INTERNAL_SIZE, swing_size=length, timeframe="30m"
        )
        if rows != rows2:
            raise SystemExit(f"non-deterministic swing={length}")

    # Lookahead check on primary 50
    pref, _ = run_multilevel_structure(agg30.iloc[:150].copy(), internal_size=5, swing_size=50)
    full = timelines[50]
    lookahead_ok = all(
        a["internal_bias"] == b["internal_bias"] and a["swing_bias"] == b["swing_bias"]
        for a, b in zip(pref, full[: len(pref)])
    )

    _p(f"evaluate {len(setups)} setups")
    decision_rows: list[dict[str, Any]] = []
    for _, s in setups.iterrows():
        ts = _ts(s["timestamp_utc"])
        side = str(s["setup_side"]).lower()
        baseline_allow = _truthy(s.get("setup_activated")) and _empty_blockers(s.get("blockers"))

        snap: dict[int, dict[str, Any] | None] = {}
        for length in SWING_LENGTHS:
            snap[length] = asof_row(timelines[length], ts)

        # internal bias identical across lengths (same internal size) — use M50 snap
        base = snap[50] or snap[30] or snap[20]
        internal_bias = 0 if base is None else int(base.get("internal_bias") or 0)

        latest_internal = latest_event_asof(
            [e for e in events_by[50] if e.get("structure_level") == "internal"], ts
        )

        decisions: dict[str, Any] = {}
        reasons: dict[str, str] = {}
        for length in SWING_LENGTHS:
            key = VARIANT_KEYS[length]
            r = snap[length]
            if r is None:
                decisions[key] = "OBSERVE_ONLY"
                reasons[key] = "no_structure_asof"
                continue
            d, reason = policy_decision(
                direction=side,
                internal_bias=int(r.get("internal_bias") or 0),
                swing_bias=int(r.get("swing_bias") or 0),
                primary_label=str(r.get("combined_primary_label") or "insufficient_structure"),
            )
            decisions[key] = d
            reasons[key] = reason

        px = entry_price_at(c5, ts)
        moves = later_moves(c5, ts, px or 0.0, side) if px else {f"later_move_{h}h": None for h in (1, 2, 4, 8)}
        fo = (
            compute_forward_outcome(c5, ts, float(px), side, horizon_bars=144)
            if px
            else {"mfe_pct": None, "mae_pct": None}
        )
        mfe = fo.get("mfe_pct")
        mae = fo.get("mae_pct")
        result = classify_result(
            None if mfe is None else float(mfe),
            None if mae is None else float(mae),
            moves.get("later_move_4h"),
        )
        eventual = (
            f"momentum_confirmed:{result}" if str(s["setup_id"]) in mom_ids else f"setup_only:{result}"
        )

        primary_reason = reasons.get("M50") or reasons.get("M30") or reasons.get("M20")

        decision_rows.append(
            {
                "setup_id": s["setup_id"],
                "timestamp_utc": _iso(ts),
                "direction": side,
                "baseline_allow": baseline_allow,
                "internal_bias": internal_bias,
                "swing_bias_len20": None if snap[20] is None else snap[20].get("swing_bias"),
                "swing_bias_len30": None if snap[30] is None else snap[30].get("swing_bias"),
                "swing_bias_len50": None if snap[50] is None else snap[50].get("swing_bias"),
                "combined_context_len20": None if snap[20] is None else snap[20].get("combined_primary_label"),
                "combined_context_len30": None if snap[30] is None else snap[30].get("combined_primary_label"),
                "combined_context_len50": None if snap[50] is None else snap[50].get("combined_primary_label"),
                "latest_internal_event": latest_internal,
                "latest_swing_event_len20": latest_event_asof(
                    [e for e in events_by[20] if e.get("structure_level") == "swing"], ts
                ),
                "latest_swing_event_len30": latest_event_asof(
                    [e for e in events_by[30] if e.get("structure_level") == "swing"], ts
                ),
                "latest_swing_event_len50": latest_event_asof(
                    [e for e in events_by[50] if e.get("structure_level") == "swing"], ts
                ),
                "M20_decision": decisions["M20"],
                "M30_decision": decisions["M30"],
                "M50_decision": decisions["M50"],
                "M20_reason": reasons["M20"],
                "M30_reason": reasons["M30"],
                "M50_reason": reasons["M50"],
                "decision_reason": primary_reason,
                **moves,
                "MAE": mae,
                "MFE": mfe,
                "entry_price": px,
                "actual_trade_result": eventual,
                "result_class": result,
            }
        )

    _write_csv(OUT / "all_setup_decisions.csv", decision_rows)

    # derived tables using M50 as reference listing, but include all decisions
    blocked_ct = [
        r
        for r in decision_rows
        if (
            (r["direction"] == "long" and bias_name(r.get("swing_bias_len50")) == "bearish")
            or (r["direction"] == "short" and bias_name(r.get("swing_bias_len50")) == "bullish")
        )
        and any(r[f"{k}_decision"] in {"BLOCK", "WAIT"} for k in ("M20", "M30", "M50"))
    ]
    _write_csv(OUT / "blocked_countertrend_setups.csv", blocked_ct)

    def any_block_winner(key: str) -> list[dict[str, Any]]:
        return [
            r
            for r in decision_rows
            if r.get("baseline_allow")
            and r.get(f"{key}_decision") in {"BLOCK", "WAIT"}
            and r.get("result_class") == "winner"
        ]

    def any_prevented(key: str) -> list[dict[str, Any]]:
        return [
            r
            for r in decision_rows
            if r.get("baseline_allow")
            and r.get(f"{key}_decision") in {"BLOCK", "WAIT"}
            and r.get("result_class") == "loser"
        ]

    # export union prevented/blocked across variants with tags
    bw_rows = []
    for key in ("M20", "M30", "M50"):
        for r in any_block_winner(key):
            bw_rows.append({**r, "blocking_variant": key})
    prev_rows = []
    for key in ("M20", "M30", "M50"):
        for r in any_prevented(key):
            prev_rows.append({**r, "blocking_variant": key})
    _write_csv(OUT / "blocked_winners.csv", bw_rows)
    _write_csv(OUT / "prevented_losses.csv", prev_rows)

    wait_cases = [
        r for r in decision_rows if any(r[f"{k}_decision"] == "WAIT" for k in ("M20", "M30", "M50"))
    ]
    _write_csv(OUT / "wait_cases.csv", wait_cases)

    # context case tables from timelines
    def timeline_cases(length: int, pred) -> list[dict[str, Any]]:
        out = []
        for r in timelines[length]:
            if not (a0 <= _ts(r["decision_timestamp_utc"]) <= a1):
                continue
            if pred(r):
                out.append(
                    {
                        "swing_length": length,
                        "decision_timestamp_utc": r["decision_timestamp_utc"],
                        "combined_primary_label": r.get("combined_primary_label"),
                        "internal_bias": r.get("internal_bias"),
                        "swing_bias": r.get("swing_bias"),
                        "close": r.get("close"),
                    }
                )
        return out

    possible_cases = []
    confirmed_cases = []
    failed_cases = []
    for length in SWING_LENGTHS:
        possible_cases.extend(
            timeline_cases(length, lambda r: str(r.get("combined_primary_label") or "").startswith("possible_"))
        )
        confirmed_cases.extend(
            timeline_cases(length, lambda r: str(r.get("combined_primary_label") or "").startswith("confirmed_"))
        )
        _stats, episodes = swing_reversal_episodes(
            [r for r in timelines[length] if a0 <= _ts(r["decision_timestamp_utc"]) <= a1],
            events_by[length],
        )
        for ep in episodes:
            if ep.get("outcome") == "failed":
                failed_cases.append({"swing_length": length, "variant": VARIANT_KEYS[length], **ep})

    _write_csv(OUT / "possible_reversal_cases.csv", possible_cases)
    _write_csv(OUT / "confirmed_reversal_cases.csv", confirmed_cases)
    _write_csv(OUT / "failed_reversal_cases.csv", failed_cases)

    # filter timelines to audit window for metrics
    audit_timelines = {
        length: [r for r in timelines[length] if a0 <= _ts(r["decision_timestamp_utc"]) <= a1]
        for length in SWING_LENGTHS
    }

    variant_rows = []
    delay_rows = []
    for length in SWING_LENGTHS:
        key = VARIANT_KEYS[length]
        m = metrics_for_variant(
            decision_rows,
            key=key,
            timeline=audit_timelines[length],
            events=events_by[length],
        )
        variant_rows.append(m)
        delay_rows.append(
            {
                "variant": key,
                "swing_length": length,
                "median_possible_to_confirmed_hours": m["median_reversal_delay_hours"],
                "median_choch_to_bos_hours": m["median_choch_to_bos_hours"],
                "avg_recovery_duration_hours": m["avg_recovery_duration_hours"],
                "avg_possible_reversal_duration_hours": m["avg_possible_reversal_duration_hours"],
                "true_reversals": m["true_reversals"],
                "false_reversals": m["false_reversals"],
                "possible_to_confirmed_rate": m["possible_to_confirmed_rate"],
            }
        )
    _write_csv(OUT / "variant_comparison.csv", variant_rows)
    _write_csv(OUT / "reversal_delay_comparison.csv", delay_rows)

    for name, (lo, hi) in FOCUS.items():
        focus = focus_slice(decision_rows, lo, hi)
        _write_csv(OUT / f"{name}_detail.csv", focus)

    # Core answers
    by_key = {m["variant"]: m for m in variant_rows}

    def best(metric: str, higher: bool = True) -> str:
        items = [(k, by_key[k].get(metric)) for k in ("M20", "M30", "M50")]
        items = [(k, v) for k, v in items if v is not None]
        if not items:
            return "none"
        items.sort(key=lambda x: x[1], reverse=higher)
        return items[0][0]

    core = {
        "1_most_adverse_countertrend_blocked": {
            "winner": best("countertrend_blocked_adverse_4h", True),
            "values": {k: by_key[k]["countertrend_blocked_adverse_4h"] for k in by_key},
        },
        "2_fewest_good_reversals_blocked": {
            "winner": best("blocked_winners", False),
            "values": {k: by_key[k]["blocked_winners"] for k in by_key},
        },
        "3_lowest_reversal_delay": {
            "winner": best("median_reversal_delay_hours", False),
            "values": {k: by_key[k]["median_reversal_delay_hours"] for k in by_key},
            "choch_to_bos": {k: by_key[k]["median_choch_to_bos_hours"] for k in by_key},
        },
        "4_false_reversals": {k: by_key[k]["false_reversals"] for k in by_key},
        "5_missed_reversals": {k: by_key[k]["missed_reversals"] for k in by_key},
        "6_avg_recovery_duration_hours": {k: by_key[k]["avg_recovery_duration_hours"] for k in by_key},
        "7_avg_possible_reversal_duration_hours": {
            k: by_key[k]["avg_possible_reversal_duration_hours"] for k in by_key
        },
        "8_possible_to_confirmed_rate": {k: by_key[k]["possible_to_confirmed_rate"] for k in by_key},
        "9_failed_possible_reversals": {k: by_key[k]["false_reversals"] for k in by_key},
        "10_focus_windows": {},
    }

    for name, (lo, hi) in FOCUS.items():
        focus = focus_slice(decision_rows, lo, hi)
        focus_stats: dict[str, Any] = {
            "n_setups": len(focus),
            "long": sum(1 for r in focus if r["direction"] == "long"),
            "short": sum(1 for r in focus if r["direction"] == "short"),
            "contexts_m50": dict(
                pd.Series([r.get("combined_context_len50") for r in focus]).value_counts().to_dict()
            )
            if focus
            else {},
        }
        for key in ("M20", "M30", "M50"):
            focus_stats[f"{key}_block_long"] = sum(
                1 for r in focus if r["direction"] == "long" and r[f"{key}_decision"] == "BLOCK"
            )
            focus_stats[f"{key}_wait"] = sum(1 for r in focus if r[f"{key}_decision"] == "WAIT")
            focus_stats[f"{key}_allow_long"] = sum(
                1 for r in focus if r["direction"] == "long" and r[f"{key}_decision"] == "ALLOW"
            )
        core["10_focus_windows"][name] = focus_stats

    # Jan 13-15 special: no long ALLOW under recovery / possible / bearish swing
    jan = focus_slice(decision_rows, "2026-01-13", "2026-01-15")
    jan_long_allow = {
        k: sum(1 for r in jan if r["direction"] == "long" and r[f"{k}_decision"] == "ALLOW")
        for k in ("M20", "M30", "M50")
    }
    jan_countertrend_long_allow = {
        k: sum(
            1
            for r in jan
            if r["direction"] == "long"
            and r[f"{k}_decision"] == "ALLOW"
            and (
                bias_name(r.get(f"swing_bias_len{k[1:]}")) == "bearish"
                or str(r.get(f"combined_context_len{k[1:]}") or "").startswith(
                    ("bullish_recovery", "possible_bullish", "possible_bearish")
                )
            )
        )
        for k in ("M20", "M30", "M50")
    }

    # Decision J/N/U
    # Prefer clear winner on net_ct_block_edge + sum_move + not too many blocked winners
    scores = {}
    for k, m in by_key.items():
        edge = m.get("net_ct_block_edge") or 0
        move = m.get("sum_move_4h_allowed")
        move_s = 0.0 if move is None else float(move)
        bw = m.get("blocked_winners") or 0
        prev = m.get("prevented_losers") or 0
        delay = m.get("median_reversal_delay_hours")
        delay_pen = 0.0 if delay is None else min(float(delay) / 100.0, 2.0)
        scores[k] = edge + 0.05 * move_s + 0.5 * (prev - bw) - delay_pen

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    best_v, best_s = ranked[0]
    second_s = ranked[1][1] if len(ranked) > 1 else best_s
    jan_ok = all(v == 0 for v in jan_countertrend_long_allow.values())

    adverse_winner = best("countertrend_blocked_adverse_4h", True)
    delay_winner = best("median_reversal_delay_hours", False)
    pf_vals = {
        k: by_key[k].get("profit_factor_proxy_allowed")
        for k in ("M20", "M30", "M50")
        if by_key[k].get("profit_factor_proxy_allowed") is not None
    }
    any_pf_ge_1 = any(float(v) >= 1.0 for v in pf_vals.values())
    conflicting_axes = {adverse_winner, delay_winner, best_v}
    clear_margin = best_s - second_s >= 8

    if not lookahead_ok:
        decision, reason = "N", "Lookahead failure in structure timelines."
    elif not jan_ok:
        decision, reason = (
            "U",
            "Jan 13–15 still allows countertrend / recovery longs under at least one length.",
        )
    elif all((by_key[k].get("net_ct_block_edge") or 0) <= 0 for k in by_key) and not any_pf_ge_1:
        decision, reason = "N", "No length improves practical countertrend blocking edge."
    elif (
        clear_margin
        and best_v == adverse_winner
        and (by_key[best_v].get("net_ct_block_edge") or 0) > 0
        and len(conflicting_axes) == 1
        and any_pf_ge_1
    ):
        decision, reason = (
            "J",
            f"{best_v} shows a clear edge on countertrend blocking / move-sum vs the other lengths.",
        )
    else:
        decision, reason = (
            "U",
            "No length is clearly superior; tradeoffs between delay, blocked winners, CT edge, and PF remain "
            f"(score leader {best_v}, adverse-block leader {adverse_winner}, delay leader {delay_winner}).",
        )

    hashes_after = {n: _md5(ROOT / n) for n in PROTECTED}
    for n, exp in PROTECTED.items():
        if hashes_after[n] != exp or hashes_after[n] != hashes_before[n]:
            raise SystemExit(f"protected hash changed: {n}")

    summary = {
        "decision": decision,
        "decision_reason": reason,
        "n_setups": len(decision_rows),
        "pipeline": str(PIPELINE),
        "variant_comparison": variant_rows,
        "scores": scores,
        "ranked": ranked,
        "core_answers": core,
        "jan13_15_long_allows": jan_long_allow,
        "jan13_15_countertrend_long_allows": jan_countertrend_long_allow,
        "lookahead_ok": lookahead_ok,
        "protected_hashes_before": hashes_before,
        "protected_hashes_after": hashes_after,
    }
    _write_json(OUT / "summary.json", summary)
    _write_json(
        OUT / "audit_metadata.json",
        {
            "audit_window": {"start": AUDIT_START, "end": AUDIT_END},
            "warmup_from": LOAD_START,
            "variants": ["M20", "M30", "M50"],
            "internal_size": INTERNAL_SIZE,
            "closed_30m_only": True,
            "decision_timestamps_only": True,
            "no_lookahead": lookahead_ok,
            "policy_adopted": False,
            "protected_hashes": hashes_after,
        },
    )

    readme = f"""# Multilevel structure policy comparison audit

Read-only comparison of M20 / M30 / M50 on real H1 setups.

## Decision

**{decision}** — {reason}

## Scores

```json
{json.dumps(scores, indent=2)}
```

## Core answers

```json
{json.dumps(core, indent=2, default=str)}
```

## Jan 13–15 long ALLOW counts

```json
{json.dumps(jan_long_allow, indent=2)}
```
"""
    (OUT / "README.md").write_text(readme, encoding="utf-8")
    _p(f"done decision={decision}")


if __name__ == "__main__":
    main()
