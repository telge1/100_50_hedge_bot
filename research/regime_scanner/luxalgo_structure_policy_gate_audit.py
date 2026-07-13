#!/usr/bin/env python3
"""Read-only LuxAlgo structure policy-gate audit (research only).

Uses existing pipeline setup activation timestamps + LuxAlgo structure reference
+ S2 macro + K2_H4 local. Does not change production modules, policy, or commit.

Attribution for structure semantics: Smart Money Concepts [LuxAlgo], CC BY-NC-SA 4.0.
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

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.luxalgo_structure_reference import (
    BEARISH,
    BULLISH,
    run_lux_structure,
)
from research.regime_scanner.market_regime import (
    MarketRegimeClassifier,
    compute_market_regime_features,
    default_market_regime_config,
)
from research.regime_scanner.market_regime_macro_context_audit import aggregate_closed_htf
from research.regime_scanner.market_regime_macro_stability_audit import (
    apply_s2,
    display_direction,
)
from research.regime_scanner.pipeline_counterfactual import compute_forward_outcome
from research.regime_scanner.point_audit import json_safe

OUT = Path("research/regime_scanner/results/luxalgo_structure_policy_gate_audit")
ROOT = Path("research/regime_scanner")
PIPELINE = Path(
    "research/backtests/results/regime_scanner_pipeline_audit_aptusdt_2026_h1"
)
LUX_RESULTS = Path("research/regime_scanner/results/luxalgo_structure_audit")

PROTECTED = {
    "market_regime.py": "1e79f30af2ddf95c3f91c1b1a012cded",
    "trend_structure.py": "4976cbd9921e9df58dcfaace5cb125a2",
    "trend_state_machine.py": "3a8ed63f60f86ec29bf05e7831bb3349",
    "trend_state_policy.py": "412f672652b66c93b7d44d4b692da2aa",
    "trend_zones.py": "6378f736a184e51efe070ebd2c2d969c",
}

LOAD_START = "2025-12-27T00:00:00+00:00"
AUDIT_START = "2026-01-06T00:00:00+00:00"
AUDIT_END = "2026-03-16T23:59:00+00:00"

# Operational 4h length for G3–G5 (timelier than 50); biases for 20/30/50 still reported.
GATE_4H_LEN = 20

Decision = Literal["ALLOW", "BLOCK", "WAIT", "OBSERVE_ONLY"]

FOCUS = {
    "jan13_15": ("2026-01-13", "2026-01-15"),
    "jan17_19": ("2026-01-17", "2026-01-19"),
    "jan19_31": ("2026-01-19", "2026-01-31"),
    "feb01_07": ("2026-01-29", "2026-02-07"),
    "mar05_10": ("2026-03-05", "2026-03-10"),
}


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
    s = str(v).strip()
    return s in {"", "[]", "()", "nan", "None"}


def s2_side(code: int | None) -> int:
    """+1 bullish, -1 bearish, 0 flat/unknown."""
    if code is None:
        return 0
    d = display_direction(int(code))
    return int(d)


def local_side(regime: str | None) -> int:
    if regime == "strong_bullish_trend":
        return 1
    if regime == "strong_bearish_trend":
        return -1
    return 0


def run_k2_timeline(agg: pd.DataFrame) -> list[dict[str, Any]]:
    scfg = default_regime_scanner_config()
    ind = compute_indicator_frame(agg, config=scfg).copy()
    ind["timestamp"] = pd.to_datetime(agg["timestamp"], utc=True).to_numpy()
    ind["decision_time"] = pd.to_datetime(agg["decision_time"], utc=True).to_numpy()
    cfg = default_market_regime_config()
    clf = MarketRegimeClassifier(cfg)
    close = ind["close"].astype(float).to_numpy()
    high = ind["high"].astype(float).to_numpy()
    low = ind["low"].astype(float).to_numpy()
    ema9 = ind["ema_9"].astype(float).to_numpy()
    ema20 = ind["ema_20"].astype(float).to_numpy()
    atr = ind["atr"].astype(float).to_numpy()
    out: list[dict[str, Any]] = []
    for i in range(len(ind)):
        feat = compute_market_regime_features(
            close[: i + 1],
            high[: i + 1],
            low[: i + 1],
            ema9[: i + 1],
            ema20[: i + 1],
            atr[: i + 1],
            cfg=cfg,
        )
        if feat is None:
            continue
        dt = _ts(ind.iloc[i]["decision_time"])
        ctx = clf.update(decision_time=dt, features=feat)
        out.append(
            {
                "decision_time": dt,
                "regime": ctx.regime,
                "close": float(close[i]),
            }
        )
    return out


def asof_row(rows: list[dict[str, Any]], t: pd.Timestamp, key: str = "decision_time") -> dict[str, Any] | None:
    """Last row with decision_time <= t (no lookahead)."""
    best = None
    for r in rows:
        dt = _ts(r[key])
        if dt <= t:
            best = r
        else:
            break
    return best


def load_or_build_lux(frame: pd.DataFrame, timeframe: str, swing: int, cache_name: str) -> list[dict[str, Any]]:
    del cache_name  # full warm rebuild required for causal asof before audit start
    return run_lux_structure(frame, timeframe=timeframe, internal_size=5, swing_size=swing)

def extract_events(rows: list[dict[str, Any]], scope: str = "swing") -> list[dict[str, Any]]:
    mapping = {
        f"{scope}_bullish_bos": ("bullish", "bos"),
        f"{scope}_bearish_bos": ("bearish", "bos"),
        f"{scope}_bullish_choch": ("bullish", "choch"),
        f"{scope}_bearish_choch": ("bearish", "choch"),
    }
    evs = []
    for r in rows:
        for flag, (direction, kind) in mapping.items():
            if _truthy(r.get(flag)):
                evs.append(
                    {
                        "decision_time": _ts(r["event_decision_timestamp"]),
                        "direction": direction,
                        "kind": kind,
                        "level": r.get("broken_level"),
                        "bias_after": int(r.get(f"{scope}_bias") or 0),
                    }
                )
    return evs


def latest_event(
    events: list[dict[str, Any]],
    t: pd.Timestamp,
    *,
    direction: str | None = None,
    kind: str | None = None,
) -> dict[str, Any] | None:
    best = None
    for e in events:
        if e["decision_time"] > t:
            break
        if direction and e["direction"] != direction:
            continue
        if kind and e["kind"] != kind:
            continue
        best = e
    return best


def choch_then_bos(
    events: list[dict[str, Any]],
    t: pd.Timestamp,
    direction: str,
    *,
    max_bars_gap_hours: float = 7 * 24,
) -> dict[str, Any]:
    """True if a CHoCH in ``direction`` is followed by BOS in same direction by time t."""
    choch = None
    bos = None
    for e in events:
        if e["decision_time"] > t:
            break
        if e["direction"] != direction:
            # opposite structure cancels pending choch
            if e["kind"] == "choch":
                choch = None
                bos = None
            continue
        if e["kind"] == "choch":
            choch = e
            bos = None
        elif e["kind"] == "bos" and choch is not None:
            gap_h = (e["decision_time"] - choch["decision_time"]).total_seconds() / 3600.0
            if gap_h <= max_bars_gap_hours:
                bos = e
    return {
        "choch": choch,
        "bos": bos,
        "followed": choch is not None and bos is not None,
        "failed_by_opposite": False,
    }


def failed_reversal(
    events: list[dict[str, Any]],
    t: pd.Timestamp,
    attempt_direction: str,
) -> bool:
    """CHoCH in attempt_direction later negated by opposite CHoCH, without lasting BOS confirm."""
    opp = "bearish" if attempt_direction == "bullish" else "bullish"
    pending_choch: dict[str, Any] | None = None
    for e in events:
        if e["decision_time"] > t:
            break
        if e["direction"] == attempt_direction and e["kind"] == "choch":
            pending_choch = e
            continue
        if pending_choch is None:
            continue
        if e["direction"] == attempt_direction and e["kind"] == "bos":
            pending_choch = None  # confirmed — not a failed reversal
            continue
        if e["direction"] == opp and e["kind"] == "choch":
            return True
    return False


def sticky_macro_from_4h_structure(
    s2_side_now: int,
    events_4h: list[dict[str, Any]],
    t: pd.Timestamp,
) -> tuple[int, str]:
    """G3: keep prior macro side until 4h CHoCH + BOS confirms flip.

    Approximation: if against-S2 4h CHoCH+BOS completed, macro may flip to that side;
    else keep S2 side (or last confirmed structure bias).
    """
    bull = choch_then_bos(events_4h, t, "bullish")
    bear = choch_then_bos(events_4h, t, "bearish")
    if bull["followed"] and (not bear["followed"] or bull["bos"]["decision_time"] >= bear["bos"]["decision_time"]):
        return 1, "4h_choch_bos_bull"
    if bear["followed"] and (not bull["followed"] or bear["bos"]["decision_time"] >= bull["bos"]["decision_time"]):
        return -1, "4h_choch_bos_bear"
    # no confirmed flip — sticky S2
    return s2_side_now, "sticky_s2"


def gate_decisions(ctx: dict[str, Any]) -> dict[str, Any]:
    """Compute G0–G5 decisions for one setup context."""
    side = ctx["direction"]  # long/short
    is_long = side == "long"
    is_short = side == "short"
    s2 = int(ctx["s2_macro_side"])
    local = int(ctx["k2_local_side"])

    latest_30_choch = ctx.get("lux_30m_latest_choch")
    latest_4_choch = ctx.get("lux_4h_latest_choch")
    failed = bool(ctx.get("failed_reversal_flag"))

    bull_30_choch = latest_30_choch == "bullish"
    bear_30_choch = latest_30_choch == "bearish"
    bull_30_bos_after = bool(ctx.get("bull_choch_followed_by_bos"))
    bear_30_bos_after = bool(ctx.get("bear_choch_followed_by_bos"))

    bull_4_choch = latest_4_choch == "bullish"
    bear_4_choch = latest_4_choch == "bearish"
    bull_4_confirmed = bool(ctx.get("bull_4h_choch_followed_by_bos"))
    bear_4_confirmed = bool(ctx.get("bear_4h_choch_followed_by_bos"))

    sticky_side = int(ctx.get("g3_macro_side") or s2)

    reasons: dict[str, str] = {}

    # --- G0 baseline ---
    g0: Decision = "ALLOW" if ctx["existing_decision"] == "ALLOW" else "BLOCK"
    reasons["G0"] = "baseline_existing"

    # --- G1 recovery warning ---
    g1: Decision = g0
    if s2 < 0:  # bearish macro
        if is_long:
            g1 = "BLOCK"
            reasons["G1"] = "bearish_s2_long_blocked"
            if bull_30_choch:
                reasons["G1"] = "RECOVERY_WARNING_bull_30m_choch_long_blocked"
        elif is_short:
            g1 = "ALLOW"
            reasons["G1"] = "bearish_s2_short_allowed_even_if_bull_choch"
    elif s2 > 0:
        if is_short:
            g1 = "BLOCK"
            reasons["G1"] = "bullish_s2_short_blocked"
            if bear_30_choch:
                reasons["G1"] = "RECOVERY_WARNING_bear_30m_choch_short_blocked"
        elif is_long:
            g1 = "ALLOW"
            reasons["G1"] = "bullish_s2_long_allowed_even_if_bear_choch"
    else:
        g1 = "OBSERVE_ONLY"
        reasons["G1"] = "s2_flat_observe"

    # --- G2 local choch+bos ---
    g2: Decision = g0
    if s2 < 0:
        if is_long:
            g2 = "BLOCK"
            if bull_30_bos_after:
                reasons["G2"] = "LOCAL_REVERSAL_CONFIRMED_long_not_auto_released"
            elif bull_30_choch:
                reasons["G2"] = "bull_30m_choch_alone_long_blocked"
            else:
                reasons["G2"] = "bearish_s2_long_blocked"
        elif is_short:
            if bull_30_bos_after:
                g2 = "WAIT"
                reasons["G2"] = "LOCAL_REVERSAL_CONFIRMED_pause_new_shorts"
            else:
                g2 = "ALLOW"
                reasons["G2"] = "bearish_s2_short_allowed"
    elif s2 > 0:
        if is_short:
            g2 = "BLOCK"
            if bear_30_bos_after:
                reasons["G2"] = "LOCAL_REVERSAL_CONFIRMED_short_not_auto_released"
            elif bear_30_choch:
                reasons["G2"] = "bear_30m_choch_alone_short_blocked"
            else:
                reasons["G2"] = "bullish_s2_short_blocked"
        elif is_long:
            if bear_30_bos_after:
                g2 = "WAIT"
                reasons["G2"] = "LOCAL_REVERSAL_CONFIRMED_pause_new_longs"
            else:
                g2 = "ALLOW"
                reasons["G2"] = "bullish_s2_long_allowed"
    else:
        g2 = "OBSERVE_ONLY"
        reasons["G2"] = "s2_flat_observe"

    # --- G3 4h structure sticky macro ---
    g3: Decision
    if sticky_side < 0:
        if is_long:
            g3 = "BLOCK"
            reasons["G3"] = "sticky_bear_macro_counter_long_recovery"
            if bull_4_choch and not bull_4_confirmed:
                reasons["G3"] = "4h_bull_choch_possible_reversal_long_blocked"
        else:
            g3 = "ALLOW"
            reasons["G3"] = "sticky_bear_macro_short_allowed"
            if bull_4_confirmed:
                g3 = "WAIT"
                reasons["G3"] = "4h_bull_structure_confirmed_wait"
    elif sticky_side > 0:
        if is_short:
            g3 = "BLOCK"
            reasons["G3"] = "sticky_bull_macro_counter_short_recovery"
            if bear_4_choch and not bear_4_confirmed:
                reasons["G3"] = "4h_bear_choch_possible_reversal_short_blocked"
        else:
            g3 = "ALLOW"
            reasons["G3"] = "sticky_bull_macro_long_allowed"
            if bear_4_confirmed:
                g3 = "WAIT"
                reasons["G3"] = "4h_bear_structure_confirmed_wait"
    else:
        g3 = "OBSERVE_ONLY"
        reasons["G3"] = "no_sticky_macro"

    # --- G4 ladder ---
    g4: Decision
    audit_state = "NONE"
    # Prefer S2 for ladder base
    if s2 < 0:
        if bull_4_confirmed:
            audit_state = "MACRO_REVERSAL_CONFIRMED"
        elif bull_4_choch:
            audit_state = "MACRO_REVERSAL_WARNING"
        elif bull_30_bos_after:
            audit_state = "LOCAL_REVERSAL_CONFIRMED"
        elif bull_30_choch:
            audit_state = "RECOVERY_WARNING"
        if is_long:
            if audit_state == "MACRO_REVERSAL_CONFIRMED" and local > 0:
                g4 = "ALLOW"
                reasons["G4"] = "macro_rev_confirmed_and_local_bull"
            elif audit_state in {"MACRO_REVERSAL_WARNING", "LOCAL_REVERSAL_CONFIRMED"}:
                g4 = "WAIT"
                reasons["G4"] = audit_state
            else:
                g4 = "BLOCK"
                reasons["G4"] = audit_state if audit_state != "NONE" else "bearish_macro_long_blocked"
        else:  # short
            if audit_state in {"MACRO_REVERSAL_WARNING", "MACRO_REVERSAL_CONFIRMED", "LOCAL_REVERSAL_CONFIRMED"}:
                g4 = "WAIT"
                reasons["G4"] = f"pause_shorts_{audit_state}"
            else:
                g4 = "ALLOW"
                reasons["G4"] = "bearish_macro_short_allowed"
    elif s2 > 0:
        if bear_4_confirmed:
            audit_state = "MACRO_REVERSAL_CONFIRMED"
        elif bear_4_choch:
            audit_state = "MACRO_REVERSAL_WARNING"
        elif bear_30_bos_after:
            audit_state = "LOCAL_REVERSAL_CONFIRMED"
        elif bear_30_choch:
            audit_state = "RECOVERY_WARNING"
        if is_short:
            if audit_state == "MACRO_REVERSAL_CONFIRMED" and local < 0:
                g4 = "ALLOW"
                reasons["G4"] = "macro_rev_confirmed_and_local_bear"
            elif audit_state in {"MACRO_REVERSAL_WARNING", "LOCAL_REVERSAL_CONFIRMED"}:
                g4 = "WAIT"
                reasons["G4"] = audit_state
            else:
                g4 = "BLOCK"
                reasons["G4"] = audit_state if audit_state != "NONE" else "bullish_macro_short_blocked"
        else:
            if audit_state in {"MACRO_REVERSAL_WARNING", "MACRO_REVERSAL_CONFIRMED", "LOCAL_REVERSAL_CONFIRMED"}:
                g4 = "WAIT"
                reasons["G4"] = f"pause_longs_{audit_state}"
            else:
                g4 = "ALLOW"
                reasons["G4"] = "bullish_macro_long_allowed"
    else:
        g4 = "OBSERVE_ONLY"
        reasons["G4"] = "s2_flat"
        audit_state = "FLAT"

    # --- G5 asymmetric trading ---
    g5: Decision
    if s2 < 0:
        if is_long:
            if bull_4_confirmed and local > 0:
                g5 = "ALLOW"
                reasons["G5"] = "4h_macro_rev_and_local_bull"
            else:
                g5 = "BLOCK"
                reasons["G5"] = "long_blocked_until_4h_rev_and_local_bull"
                if failed or (bull_30_choch and not bull_30_bos_after):
                    reasons["G5"] = "failed_or_recovery_long_blocked"
        else:
            if bull_4_choch or bull_30_bos_after:
                g5 = "WAIT"
                reasons["G5"] = "WAIT_bull_4h_choch_or_30m_choch_bos"
            else:
                g5 = "ALLOW"
                reasons["G5"] = "short_allowed_no_counter_structure"
    elif s2 > 0:
        if is_short:
            if bear_4_confirmed and local < 0:
                g5 = "ALLOW"
                reasons["G5"] = "4h_macro_rev_and_local_bear"
            else:
                g5 = "BLOCK"
                reasons["G5"] = "short_blocked_until_4h_rev_and_local_bear"
                if failed or (bear_30_choch and not bear_30_bos_after):
                    reasons["G5"] = "failed_or_recovery_short_blocked"
        else:
            if bear_4_choch or bear_30_bos_after:
                g5 = "WAIT"
                reasons["G5"] = "WAIT_bear_4h_choch_or_30m_choch_bos"
            else:
                g5 = "ALLOW"
                reasons["G5"] = "long_allowed_no_counter_structure"
    else:
        g5 = "OBSERVE_ONLY"
        reasons["G5"] = "s2_flat"

    # Jan 13-15 safety: never ALLOW counter-macro long as confirmed uptrend under G2/G4/G5
    # when only 30m choch without bos (failed/recovery)
    if s2 < 0 and is_long and bull_30_choch and not bull_30_bos_after and not bull_4_confirmed:
        if g2 == "ALLOW":
            g2 = "BLOCK"
            reasons["G2"] = "safety_no_confirmed_macro_up_from_30m_choch_alone"
        if g4 == "ALLOW":
            g4 = "BLOCK"
            reasons["G4"] = "safety_no_confirmed_macro_up_from_30m_choch_alone"
        if g5 == "ALLOW":
            g5 = "BLOCK"
            reasons["G5"] = "safety_no_confirmed_macro_up_from_30m_choch_alone"
    return {
        "G0_decision": g0,
        "G1_decision": g1,
        "G2_decision": g2,
        "G3_decision": g3,
        "G4_decision": g4,
        "G5_decision": g5,
        "g4_audit_state": audit_state,
        "block_or_wait_reason": reasons.get("G5") or reasons.get("G4") or reasons.get("G2") or reasons.get("G1"),
        "reasons": reasons,
    }


def later_moves(c5: pd.DataFrame, entry_ts: pd.Timestamp, entry_px: float, side: str) -> dict[str, float | None]:
    hours = (1, 2, 4, 8, 12)
    out: dict[str, float | None] = {f"later_move_{h}h": None for h in hours}
    dec = c5["decision_time"]
    future = c5.loc[dec > entry_ts]
    if future.empty or not entry_px:
        return out
    sign = 1.0 if side == "long" else -1.0
    for h in hours:
        target = entry_ts + pd.Timedelta(hours=h)
        # last closed 5m decision at or before target+epsilon, after entry
        window = future.loc[dec <= target]
        if window.empty:
            continue
        cl = float(window.iloc[-1]["close"])
        out[f"later_move_{h}h"] = sign * (cl - entry_px) / entry_px * 100.0
    return out


def entry_price_at(c5: pd.DataFrame, ts: pd.Timestamp) -> float | None:
    """Use close of last closed 5m bar at/before ts (decision_time <= ts)."""
    sub = c5.loc[c5["decision_time"] <= ts]
    if sub.empty:
        return None
    return float(sub.iloc[-1]["close"])


def classify_outcome(mfe: float | None, mae: float | None, move_4h: float | None) -> str:
    if mfe is None or mae is None:
        return "unknown"
    # crude: winner if MFE>=0.25 and MAE < MFE; loser if MAE>=0.5 and move_4h<=0
    if mfe >= 0.25 and (mae is None or mae < max(mfe, 0.25)):
        return "winner"
    if mae >= 0.5 and (move_4h is None or move_4h <= 0):
        return "loser"
    if move_4h is not None and move_4h > 0.15:
        return "winner"
    if move_4h is not None and move_4h < -0.25:
        return "loser"
    return "mixed"


def metrics_for_variant(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    dec_col = f"{key}_decision"
    allowed = [r for r in rows if r.get(dec_col) == "ALLOW"]
    blocked = [r for r in rows if r.get(dec_col) == "BLOCK"]
    wait = [r for r in rows if r.get(dec_col) == "WAIT"]

    def pnl_proxy(subset: list[dict[str, Any]]) -> dict[str, float | None]:
        moves = [r.get("later_move_4h") for r in subset if r.get("later_move_4h") is not None]
        mfes = [r.get("MFE") for r in subset if r.get("MFE") is not None]
        maes = [r.get("MAE") for r in subset if r.get("MAE") is not None]
        if not moves:
            return {
                "n": len(subset),
                "sum_move_4h": None,
                "mean_move_4h": None,
                "mean_mfe": None,
                "mean_mae": None,
                "profit_factor_proxy": None,
                "win_rate_4h": None,
                "counter_trend_loss_rate": None,
            }
        s = float(sum(moves))
        wins = [m for m in moves if m > 0]
        losses = [m for m in moves if m <= 0]
        gp = sum(wins) if wins else 0.0
        gl = abs(sum(losses)) if losses else 0.0
        pf = (gp / gl) if gl > 0 else (None if gp == 0 else 99.0)
        # counter-trend: long under bear s2 or short under bull s2
        ct = [
            r
            for r in subset
            if (r["direction"] == "long" and r["S2_macro_direction"] == "bearish")
            or (r["direction"] == "short" and r["S2_macro_direction"] == "bullish")
        ]
        ct_loss = [
            r
            for r in ct
            if r.get("later_move_4h") is not None and float(r["later_move_4h"]) < 0
        ]
        return {
            "n": len(subset),
            "sum_move_4h": s,
            "mean_move_4h": s / len(moves),
            "mean_mfe": float(np.mean(mfes)) if mfes else None,
            "mean_mae": float(np.mean(maes)) if maes else None,
            "profit_factor_proxy": pf,
            "win_rate_4h": len(wins) / len(moves),
            "counter_trend_loss_rate": (len(ct_loss) / len(ct)) if ct else None,
            "n_counter_trend": len(ct),
        }

    base = pnl_proxy([r for r in rows if r.get("G0_decision") == "ALLOW"])
    var = pnl_proxy(allowed)
    # prevented losses: G0 ALLOW loser that variant BLOCK/WAIT
    prev = [
        r
        for r in rows
        if r.get("G0_decision") == "ALLOW"
        and r.get(dec_col) in {"BLOCK", "WAIT"}
        and r.get("result_class") == "loser"
    ]
    blocked_winners = [
        r
        for r in rows
        if r.get("G0_decision") == "ALLOW"
        and r.get(dec_col) in {"BLOCK", "WAIT"}
        and r.get("result_class") == "winner"
    ]
    # false long releases prevented: long ALLOW under G0, BLOCK under variant, and loser / adverse
    false_long_prev = [
        r
        for r in rows
        if r["direction"] == "long"
        and r.get("G0_decision") == "ALLOW"
        and r.get(dec_col) in {"BLOCK", "WAIT"}
        and r.get("result_class") == "loser"
    ]
    good_long_blocked = [
        r
        for r in rows
        if r["direction"] == "long"
        and r.get("G0_decision") == "ALLOW"
        and r.get(dec_col) in {"BLOCK", "WAIT"}
        and r.get("result_class") == "winner"
    ]
    bad_short_wait = [
        r
        for r in rows
        if r["direction"] == "short"
        and r.get("G0_decision") == "ALLOW"
        and r.get(dec_col) == "WAIT"
        and r.get("result_class") == "loser"
    ]
    # adverse counter-trend longs blocked (regardless of winner/loser label)
    ct_long_blocked = [
        r
        for r in rows
        if r["direction"] == "long"
        and r.get("S2_macro_direction") == "bearish"
        and r.get("G0_decision") == "ALLOW"
        and r.get(dec_col) in {"BLOCK", "WAIT"}
    ]
    ct_long_blocked_adverse = [
        r for r in ct_long_blocked if r.get("later_move_4h") is not None and float(r["later_move_4h"]) < 0
    ]
    ct_long_blocked_favorable = [
        r for r in ct_long_blocked if r.get("later_move_4h") is not None and float(r["later_move_4h"]) > 0
    ]
    return {
        "variant": key,
        "n_allow": len(allowed),
        "n_block": len(blocked),
        "n_wait": len(wait),
        "sum_move_4h_allowed": var["sum_move_4h"],
        "mean_move_4h_allowed": var["mean_move_4h"],
        "mean_mfe_allowed": var["mean_mfe"],
        "mean_mae_allowed": var["mean_mae"],
        "profit_factor_proxy_allowed": var["profit_factor_proxy"],
        "win_rate_4h_allowed": var["win_rate_4h"],
        "counter_trend_loss_rate_allowed": var["counter_trend_loss_rate"],
        "delta_sum_move_4h_vs_g0": (
            None
            if var["sum_move_4h"] is None or base["sum_move_4h"] is None
            else var["sum_move_4h"] - base["sum_move_4h"]
        ),
        "prevented_losers": len(prev),
        "blocked_winners": len(blocked_winners),
        "false_long_releases_prevented": len(false_long_prev),
        "profitable_longs_extra_blocked": len(good_long_blocked),
        "bad_shorts_waited": len(bad_short_wait),
        "counter_trend_longs_blocked": len(ct_long_blocked),
        "counter_trend_longs_blocked_adverse_4h": len(ct_long_blocked_adverse),
        "counter_trend_longs_blocked_favorable_4h": len(ct_long_blocked_favorable),
        "net_edge_proxy": len(prev) - len(blocked_winners),
        "net_ct_long_edge_4h": len(ct_long_blocked_adverse) - len(ct_long_blocked_favorable),
    }


def build_pine(month: int, rows: list[dict[str, Any]], events_30: list[dict], events_4: list[dict]) -> str:
    a = _ts(f"2026-{month:02d}-01")
    if month == 12:
        b = _ts("2027-01-01")
    else:
        b = _ts(f"2026-{month + 1:02d}-01")
    setups = [r for r in rows if a <= _ts(r["timestamp_utc"]) < b]
    e30 = [e for e in events_30 if a <= e["decision_time"] < b]
    e4 = [e for e in events_4 if a <= e["decision_time"] < b]
    lines = [
        "//@version=6",
        f'indicator("Lux Structure Gate 2026-{month:02d}", overlay=true, max_labels_count=200)',
        "",
        "// Precomputed audit markers only — no regime math in Pine.",
        "// Attribution: LuxAlgo SMC structure subset — CC BY-NC-SA 4.0",
        "// Timestamps = decision_time UTC (no backpaint).",
        "",
        'showLabels = input.bool(false, "Show labels")',
        'showSetups = input.bool(true, "Show setups")',
        'show30m = input.bool(true, "Show 30m CHoCH/BOS")',
        'show4h = input.bool(true, "Show 4h CHoCH/BOS")',
        'showG5 = input.bool(true, "Color by G5 decision")',
        "",
    ]

    def emit_shape(cond: str, title: str, shape: str, loc: str, col: str, gate: str) -> None:
        lines.append(
            f'plotshape({gate} and {cond}, title="{title}", style={shape}, location={loc}, color={col}, size=size.tiny)'
        )

    for r in setups:
        t = _ts(r["timestamp_utc"])
        y, m, d, h, mi = t.year, t.month, t.day, t.hour, t.minute
        cond = f"(year=={y} and month=={m} and dayofmonth=={d} and hour=={h} and minute=={mi})"
        g5 = r.get("G5_decision") or "OBSERVE_ONLY"
        side = r["direction"]
        col = {
            "ALLOW": "color.lime",
            "BLOCK": "color.red",
            "WAIT": "color.orange",
            "OBSERVE_ONLY": "color.gray",
        }.get(g5, "color.gray")
        loc = "location.belowbar" if side == "long" else "location.abovebar"
        shape = "shape.triangleup" if side == "long" else "shape.triangledown"
        emit_shape(cond, f"setup_{r['setup_id']}_{g5}", shape, loc, col, "showSetups and showG5")
        lines.append(f"if showLabels and showSetups and {cond}")
        reason = str(r.get("block_or_wait_reason") or g5).replace('"', "")[:40]
        lines.append(
            f'    label.new(bar_index, {"low" if side == "long" else "high"}, '
            f'"{side[0].upper()} {g5}\\n{reason}", style=label.style_label_{"up" if side == "long" else "down"}, '
            f"color=color.new({col}, 40), textcolor=color.white, size=size.tiny)"
        )

    for e in e30:
        t = e["decision_time"]
        y, m, d, h, mi = t.year, t.month, t.day, t.hour, t.minute
        cond = f"(year=={y} and month=={m} and dayofmonth=={d} and hour=={h} and minute=={mi})"
        bull = e["direction"] == "bullish"
        choch = e["kind"] == "choch"
        col = "color.aqua" if choch else "color.teal"
        if not bull:
            col = "color.fuchsia" if choch else "color.maroon"
        shape = "shape.diamond" if choch else "shape.circle"
        loc = "location.belowbar" if bull else "location.abovebar"
        emit_shape(cond, f"30m_{e['kind']}_{e['direction']}", shape, loc, col, "show30m")

    for e in e4:
        t = e["decision_time"]
        y, m, d, h, mi = t.year, t.month, t.day, t.hour, t.minute
        cond = f"(year=={y} and month=={m} and dayofmonth=={d} and hour=={h} and minute=={mi})"
        bull = e["direction"] == "bullish"
        choch = e["kind"] == "choch"
        col = "color.yellow" if choch else "color.olive"
        shape = "shape.diamond" if choch else "shape.square"
        loc = "location.belowbar" if bull else "location.abovebar"
        emit_shape(cond, f"4h_{e['kind']}_{e['direction']}", shape, loc, col, "show4h")

    return "\n".join(lines) + "\n"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    hashes_before = {n: _md5(ROOT / n) for n in PROTECTED}
    for n, exp in PROTECTED.items():
        if hashes_before[n] != exp:
            raise SystemExit(f"hash mismatch before: {n}")

    # Safety: this audit must not import policy/state machine for decisions
    for n in ("trend_state_policy.py", "trend_state_machine.py"):
        txt = (ROOT / n).read_text(encoding="utf-8")
        if "luxalgo_structure_policy_gate" in txt:
            raise SystemExit(f"unexpected gate import in {n}")

    _p("load candles + setups")
    raw = load_symbol_candles("APTUSDT")
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    end_wall = _ts(AUDIT_END)
    load_start = _ts(LOAD_START)
    sl = raw[(raw["timestamp"] >= load_start) & (raw["timestamp"] <= _ts("2026-03-16 23:55:00+00:00"))].copy()
    ohlcv5 = sl[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    c5 = ohlcv5.copy()
    c5["decision_time"] = c5["timestamp"] + pd.Timedelta(minutes=5)

    setups = pd.read_csv(PIPELINE / "setup_activations.csv")
    setups["timestamp_utc"] = pd.to_datetime(setups["setup_activation_timestamp"], utc=True)
    a0, a1 = _ts(AUDIT_START), _ts(AUDIT_END)
    setups = setups[(setups["timestamp_utc"] >= a0) & (setups["timestamp_utc"] <= a1)].copy()
    mom = pd.read_csv(PIPELINE / "momentum_confirmations.csv")
    mom_by = {}
    if len(mom):
        for _, r in mom.iterrows():
            sid = str(r["setup_id"])
            if sid not in mom_by:
                mom_by[sid] = r.to_dict()

    _p("aggregate 30m/4h + regimes + lux")
    agg30 = aggregate_closed_htf(ohlcv5, 30, end_wall)
    agg4 = aggregate_closed_htf(ohlcv5, 240, end_wall)

    k2_30 = run_k2_timeline(agg30)
    # sort already chronological
    from research.regime_scanner.market_regime_macro_context_audit import run_htf_regime_timeline

    scfg = default_regime_scanner_config()
    ind4 = compute_indicator_frame(agg4, config=scfg).copy()
    ind4["timestamp"] = pd.to_datetime(agg4["timestamp"], utc=True).to_numpy()
    ind4["decision_time"] = pd.to_datetime(agg4["decision_time"], utc=True).to_numpy()
    m2_tl = run_htf_regime_timeline(ind4)
    s2_codes = apply_s2(m2_tl)
    s2_rows = [
        {"decision_time": _ts(r["decision_time"]), "code": c, "regime": r["regime"]}
        for r, c in zip(m2_tl, s2_codes)
    ]

    lux30 = load_or_build_lux(agg30, "30m", 50, "structure_timeline_30m.csv")
    lux4_20 = load_or_build_lux(agg4, "4h", 20, "structure_timeline_4h_len20.csv")
    lux4_30 = load_or_build_lux(agg4, "4h", 30, "structure_timeline_4h_len30.csv")
    lux4_50 = load_or_build_lux(agg4, "4h", 50, "structure_timeline_4h_len50.csv")
    lux4 = {20: lux4_20, 30: lux4_30, 50: lux4_50}[GATE_4H_LEN]

    ev30 = extract_events(lux30, "swing")
    ev30_int = extract_events(lux30, "internal")
    ev4 = extract_events(lux4, "swing")
    ev4_20 = extract_events(lux4_20, "swing")
    ev4_30 = extract_events(lux4_30, "swing")
    ev4_50 = extract_events(lux4_50, "swing")

    # structure stats for core questions
    n_choch_30 = sum(1 for e in ev30 if e["kind"] == "choch" and a0 <= e["decision_time"] <= a1)
    n_bos_30 = sum(1 for e in ev30 if e["kind"] == "bos" and a0 <= e["decision_time"] <= a1)
    followed = 0
    failed_n = 0
    choch_only = 0
    for direction in ("bullish", "bearish"):
        # count chochs in window and whether bos followed before opposite choch / audit end
        chochs = [e for e in ev30 if e["kind"] == "choch" and e["direction"] == direction and a0 <= e["decision_time"] <= a1]
        for ch in chochs:
            st = choch_then_bos(ev30, a1, direction)
            # per-choch: find bos after this choch before opposite choch
            bos_after = None
            opp = "bearish" if direction == "bullish" else "bullish"
            cancelled = False
            for e in ev30:
                if e["decision_time"] <= ch["decision_time"]:
                    continue
                if e["decision_time"] > a1:
                    break
                if e["direction"] == opp and e["kind"] == "choch":
                    cancelled = True
                    break
                if e["direction"] == direction and e["kind"] == "bos":
                    bos_after = e
                    break
            if bos_after:
                followed += 1
            elif cancelled:
                failed_n += 1
                choch_only += 1
            else:
                choch_only += 1

    _p(f"evaluate {len(setups)} setups")
    rows: list[dict[str, Any]] = []
    for _, s in setups.iterrows():
        ts = _ts(s["timestamp_utc"])
        side = str(s["setup_side"]).lower()
        existing = "ALLOW" if _truthy(s.get("setup_activated")) and _empty_blockers(s.get("blockers")) else "BLOCK"

        s2r = asof_row(s2_rows, ts)
        k2r = asof_row(k2_30, ts)
        l30 = asof_row(lux30, ts, key="event_decision_timestamp")
        l420 = asof_row(lux4_20, ts, key="event_decision_timestamp")
        l430 = asof_row(lux4_30, ts, key="event_decision_timestamp")
        l450 = asof_row(lux4_50, ts, key="event_decision_timestamp")
        l4 = asof_row(lux4, ts, key="event_decision_timestamp")

        s2_code = None if s2r is None else int(s2r["code"])
        s2_dir_i = s2_side(s2_code)
        s2_name = {1: "bullish", -1: "bearish", 0: "flat"}.get(s2_dir_i, "flat")
        k2_reg = None if k2r is None else k2r["regime"]
        k2_side_i = local_side(k2_reg)

        latest_30_c = latest_event(ev30, ts, kind="choch")
        latest_30_b = latest_event(ev30, ts, kind="bos")
        latest_4_c = latest_event(ev4, ts, kind="choch")
        latest_4_b = latest_event(ev4, ts, kind="bos")

        bull_follow = choch_then_bos(ev30, ts, "bullish")
        bear_follow = choch_then_bos(ev30, ts, "bearish")
        bull_4_follow = choch_then_bos(ev4, ts, "bullish")
        bear_4_follow = choch_then_bos(ev4, ts, "bearish")
        failed_bull = failed_reversal(ev30, ts, "bullish")
        failed_bear = failed_reversal(ev30, ts, "bearish")

        g3_side, g3_why = sticky_macro_from_4h_structure(s2_dir_i, ev4, ts)

        # latest choch direction string for context (most recent choch overall)
        lux_30_latest_choch = None if latest_30_c is None else latest_30_c["direction"]
        lux_30_latest_bos = None if latest_30_b is None else latest_30_b["direction"]
        lux_4_latest_choch = None if latest_4_c is None else latest_4_c["direction"]
        lux_4_latest_bos = None if latest_4_b is None else latest_4_b["direction"]

        # Against-macro choch+bos relevance
        if s2_dir_i < 0:
            choch_followed = bool(bull_follow["followed"])
            failed_flag = failed_bull
        elif s2_dir_i > 0:
            choch_followed = bool(bear_follow["followed"])
            failed_flag = failed_bear
        else:
            choch_followed = bool(bull_follow["followed"] or bear_follow["followed"])
            failed_flag = failed_bull or failed_bear

        ctx = {
            "direction": side,
            "existing_decision": existing,
            "s2_macro_side": s2_dir_i,
            "k2_local_side": k2_side_i,
            "lux_30m_swing_bias": 0 if l30 is None else int(l30.get("swing_bias") or 0),
            "lux_30m_latest_choch": lux_30_latest_choch,
            "lux_30m_latest_bos": lux_30_latest_bos,
            "choch_followed_by_bos": choch_followed,
            "bull_choch_followed_by_bos": bool(bull_follow["followed"]),
            "bear_choch_followed_by_bos": bool(bear_follow["followed"]),
            "bull_4h_choch_followed_by_bos": bool(bull_4_follow["followed"]),
            "bear_4h_choch_followed_by_bos": bool(bear_4_follow["followed"]),
            "lux_4h_latest_choch": lux_4_latest_choch,
            "lux_4h_latest_bos": lux_4_latest_bos,
            "failed_reversal_flag": failed_flag,
            "g3_macro_side": g3_side,
        }
        gates = gate_decisions(ctx)

        px = entry_price_at(c5, ts)
        moves = later_moves(c5, ts, px or 0.0, side) if px else {f"later_move_{h}h": None for h in (1, 2, 4, 8, 12)}
        fo = (
            compute_forward_outcome(c5, ts, float(px), side, horizon_bars=144)
            if px
            else {"mfe_pct": None, "mae_pct": None, "evaluable": False}
        )
        mfe = fo.get("mfe_pct")
        mae = fo.get("mae_pct")
        result = classify_outcome(
            None if mfe is None else float(mfe),
            None if mae is None else float(mae),
            moves.get("later_move_4h"),
        )
        mom_row = mom_by.get(str(s["setup_id"]))
        eventual = result
        if mom_row is None:
            eventual = f"setup_only:{result}"
        else:
            eventual = f"momentum_confirmed:{result}"

        row = {
            "setup_id": s["setup_id"],
            "timestamp_utc": _iso(ts),
            "direction": side,
            "existing_decision": existing,
            "S2_macro_direction": s2_name,
            "S2_display_code": s2_code,
            "K2_H4_local_regime": k2_reg,
            "lux_30m_internal_bias": None if l30 is None else l30.get("internal_bias"),
            "lux_30m_swing_bias": None if l30 is None else l30.get("swing_bias"),
            "lux_30m_latest_choch": lux_30_latest_choch,
            "lux_30m_latest_bos": lux_30_latest_bos,
            "choch_followed_by_bos": choch_followed,
            "bull_choch_followed_by_bos": bool(bull_follow["followed"]),
            "bear_choch_followed_by_bos": bool(bear_follow["followed"]),
            "failed_reversal_flag": failed_flag,
            "lux_4h_swing_bias_len20": None if l420 is None else l420.get("swing_bias"),
            "lux_4h_swing_bias_len30": None if l430 is None else l430.get("swing_bias"),
            "lux_4h_swing_bias_len50": None if l450 is None else l450.get("swing_bias"),
            "lux_4h_latest_choch": lux_4_latest_choch,
            "lux_4h_latest_bos": lux_4_latest_bos,
            "bull_4h_choch_followed_by_bos": bool(bull_4_follow["followed"]),
            "bear_4h_choch_followed_by_bos": bool(bear_4_follow["followed"]),
            "g3_macro_side": g3_side,
            "g3_macro_reason": g3_why,
            "g4_audit_state": gates["g4_audit_state"],
            "G0_decision": gates["G0_decision"],
            "G1_decision": gates["G1_decision"],
            "G2_decision": gates["G2_decision"],
            "G3_decision": gates["G3_decision"],
            "G4_decision": gates["G4_decision"],
            "G5_decision": gates["G5_decision"],
            "block_or_wait_reason": gates["block_or_wait_reason"],
            "reasons_json": json.dumps(gates["reasons"]),
            **moves,
            "MFE": mfe,
            "MAE": mae,
            "entry_price": px,
            "eventual_trade_result": eventual,
            "result_class": result,
            "momentum_confirmed": mom_row is not None,
            "setup_type": s.get("setup_type"),
        }
        rows.append(row)

    _write_csv(OUT / "all_setup_gate_decisions.csv", rows)

    # derived tables
    blocked_long = [r for r in rows if r["direction"] == "long" and any(r[f"G{i}_decision"] == "BLOCK" for i in range(1, 6))]
    blocked_short = [r for r in rows if r["direction"] == "short" and any(r[f"G{i}_decision"] == "BLOCK" for i in range(1, 6))]
    wait_cases = [r for r in rows if any(r[f"G{i}_decision"] == "WAIT" for i in range(1, 6))]
    choch_wo_bos = [r for r in rows if r.get("lux_30m_latest_choch") and not r.get("choch_followed_by_bos")]
    choch_bos_cases = [r for r in rows if r.get("choch_followed_by_bos")]
    failed_cases = [r for r in rows if r.get("failed_reversal_flag")]

    def prevented(variant: str) -> list[dict[str, Any]]:
        return [
            r
            for r in rows
            if r["G0_decision"] == "ALLOW"
            and r[f"{variant}_decision"] in {"BLOCK", "WAIT"}
            and r["result_class"] == "loser"
        ]

    def blocked_win(variant: str) -> list[dict[str, Any]]:
        return [
            r
            for r in rows
            if r["G0_decision"] == "ALLOW"
            and r[f"{variant}_decision"] in {"BLOCK", "WAIT"}
            and r["result_class"] == "winner"
        ]

    # primary comparison uses G4/G5 for prevented/blocked lists
    prev_g5 = prevented("G5")
    bw_g5 = blocked_win("G5")
    _write_csv(OUT / "blocked_long_setups.csv", blocked_long)
    _write_csv(OUT / "blocked_short_setups.csv", blocked_short)
    _write_csv(OUT / "wait_cases.csv", wait_cases)
    _write_csv(OUT / "choch_without_bos_cases.csv", choch_wo_bos)
    _write_csv(OUT / "choch_then_bos_cases.csv", choch_bos_cases)
    _write_csv(OUT / "failed_reversal_cases.csv", failed_cases)
    _write_csv(OUT / "prevented_losses.csv", prev_g5)
    _write_csv(OUT / "blocked_winners.csv", bw_g5)

    for name, (lo, hi) in FOCUS.items():
        lo_t, hi_t = _ts(lo), _ts(hi) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        focus_rows = [r for r in rows if lo_t <= _ts(r["timestamp_utc"]) <= hi_t]
        fname = {
            "jan13_15": "jan13_15_detail.csv",
            "jan17_19": "jan17_19_detail.csv",
            "jan19_31": "jan19_31_detail.csv",
            "feb01_07": "feb01_07_detail.csv",
            "mar05_10": "mar05_10_detail.csv",
        }[name]
        _write_csv(OUT / fname, focus_rows)

    variant_rows = [metrics_for_variant(rows, f"G{i}") for i in range(0, 6)]
    _write_csv(OUT / "variant_comparison.csv", variant_rows)

    # 4h length usability
    len_stats = {}
    for length, evs in ((20, ev4_20), (30, ev4_30), (50, ev4_50)):
        in_win = [e for e in evs if a0 <= e["decision_time"] <= a1]
        chochs = [e for e in in_win if e["kind"] == "choch"]
        bosses = [e for e in in_win if e["kind"] == "bos"]
        # median lag from first opposite choch to bos if any
        lags = []
        for d in ("bullish", "bearish"):
            for ch in [e for e in chochs if e["direction"] == d]:
                for b in bosses:
                    if b["direction"] == d and b["decision_time"] > ch["decision_time"]:
                        lags.append((b["decision_time"] - ch["decision_time"]).total_seconds() / 3600.0)
                        break
        jan = [e for e in in_win if _ts("2026-01-13") <= e["decision_time"] <= _ts("2026-01-15 23:59")]
        len_stats[str(length)] = {
            "n_choch": len(chochs),
            "n_bos": len(bosses),
            "n_events_jan13_15": len(jan),
            "median_choch_to_bos_hours": float(np.median(lags)) if lags else None,
            "too_late_for_jan13_15": len(jan) == 0,
        }

    # Jan 13-15 reference assertion
    jan_detail = [r for r in rows if _ts("2026-01-13") <= _ts(r["timestamp_utc"]) <= _ts("2026-01-15 23:59")]
    jan_long_allow_g245 = [
        r
        for r in jan_detail
        if r["direction"] == "long"
        and r["S2_macro_direction"] == "bearish"
        and (
            r["G2_decision"] == "ALLOW"
            or r["G4_decision"] == "ALLOW"
            or r["G5_decision"] == "ALLOW"
        )
    ]

    m_g0 = next(m for m in variant_rows if m["variant"] == "G0")
    m_g4 = next(m for m in variant_rows if m["variant"] == "G4")
    m_g5 = next(m for m in variant_rows if m["variant"] == "G5")

    # Decision J/N/U
    edge_g5 = (m_g5.get("net_edge_proxy") or 0)
    edge_g4 = (m_g4.get("net_edge_proxy") or 0)
    false_long_prev = m_g5.get("false_long_releases_prevented") or 0
    good_long_blk = m_g5.get("profitable_longs_extra_blocked") or 0
    delta_pnl = m_g5.get("delta_sum_move_4h_vs_g0")
    ct_edge = m_g5.get("net_ct_long_edge_4h") or 0
    no_false_jan = len(jan_long_allow_g245) == 0
    pf_g0 = m_g0.get("profit_factor_proxy_allowed") or 0
    pf_g5 = m_g5.get("profit_factor_proxy_allowed") or 0

    if not rows:
        decision, reason = "U", "No setups in audit window."
    elif not no_false_jan:
        decision, reason = "U", "Jan 13–15 reference failed: G2/G4/G5 allowed bearish-macro longs."
    elif (
        no_false_jan
        and (delta_pnl is not None and delta_pnl > 0)
        and ct_edge > 0
        and false_long_prev >= good_long_blk
        and pf_g5 >= pf_g0
    ):
        decision, reason = (
            "J",
            "G5 improves 4h move-sum vs G0, blocks more adverse than favorable counter-trend longs, "
            "keeps/improves PF proxy, and never releases Jan 13–15 as confirmed macro uptrend.",
        )
    elif edge_g5 > 0 and false_long_prev > good_long_blk and (delta_pnl is None or delta_pnl >= 0):
        decision, reason = (
            "J",
            "G5 prevents more losers than winners vs G0 and does not worsen 4h move sum; "
            "Jan 13–15 stays recovery/failed (no confirmed macro long release).",
        )
    elif edge_g4 > 0 and (m_g4.get("false_long_releases_prevented") or 0) > (m_g4.get("profitable_longs_extra_blocked") or 0):
        decision, reason = (
            "J",
            "G4 shows positive prevented-loss edge vs blocked winners; G5 mixed or similar.",
        )
    elif good_long_blk > false_long_prev * 1.5 and (delta_pnl is not None and delta_pnl < 0):
        decision, reason = "N", "Gates block too many profitable longs and worsen 4h move sum."
    elif len_stats["50"]["too_late_for_jan13_15"] and (m_g5.get("n_allow") or 0) < 5:
        decision, reason = "N", "4h structure gates too sparse/late for actionable decisions."
    else:
        decision, reason = (
            "U",
            "Mixed edge: review variant_comparison and focus windows before adopting any gate.",
        )
    # Pine
    for month in (1, 2, 3):
        (OUT / f"lux_structure_gate_2026_{month:02d}.pine").write_text(
            build_pine(month, rows, ev30, ev4),
            encoding="utf-8",
        )

    hashes_after = {n: _md5(ROOT / n) for n in PROTECTED}
    for n, exp in PROTECTED.items():
        if hashes_after[n] != exp or hashes_after[n] != hashes_before[n]:
            raise SystemExit(f"protected hash changed: {n}")

    core_answers = {
        "1_false_long_releases_prevented_G5": m_g5.get("false_long_releases_prevented"),
        "2_profitable_longs_extra_blocked_G5": m_g5.get("profitable_longs_extra_blocked"),
        "3_bad_shorts_waited_G5": m_g5.get("bad_shorts_waited"),
        "4_how_often_30m_choch_alone_insufficient": {
            "choch_without_bos_setup_rows": len(choch_wo_bos),
            "structure_choch_only_count": choch_only,
            "note": "30m CHoCH alone never releases long under G2/G4/G5 in bearish S2",
        },
        "5_choch_followed_by_bos_count": followed,
        "6_failed_reversal_choch_negated": failed_n,
        "7_4h_length_usability": len_stats,
        "8_len50_too_late": bool(len_stats["50"]["too_late_for_jan13_15"])
        or (
            (len_stats["50"]["median_choch_to_bos_hours"] or 0)
            > (len_stats["20"]["median_choch_to_bos_hours"] or 0) * 1.5
        ),
        "9_g4_g5_vs_g0": {
            "G4": m_g4,
            "G5": m_g5,
            "G0": m_g0,
        },
        "10_blocks_longs_in_strong_downtrends": {
            "n_long_under_bear_s2": sum(
                1 for r in rows if r["direction"] == "long" and r["S2_macro_direction"] == "bearish"
            ),
            "n_blocked_g5": sum(
                1
                for r in rows
                if r["direction"] == "long"
                and r["S2_macro_direction"] == "bearish"
                and r["G5_decision"] == "BLOCK"
            ),
            "n_allowed_g5": sum(
                1
                for r in rows
                if r["direction"] == "long"
                and r["S2_macro_direction"] == "bearish"
                and r["G5_decision"] == "ALLOW"
            ),
        },
        "jan13_15_no_confirmed_macro_long_release": no_false_jan,
        "jan13_15_n_setups": len(jan_detail),
    }

    summary = {
        "decision": decision,
        "decision_reason": reason,
        "n_setups": len(rows),
        "pipeline": str(PIPELINE),
        "gate_4h_length": GATE_4H_LEN,
        "variant_comparison": variant_rows,
        "core_answers": core_answers,
        "structure_event_counts_audit_window": {
            "30m_swing_choch": n_choch_30,
            "30m_swing_bos": n_bos_30,
            "choch_then_bos": followed,
            "choch_failed_or_alone": choch_only,
            "failed_reversals": failed_n,
        },
        "protected_hashes_before": hashes_before,
        "protected_hashes_after": hashes_after,
        "no_lookahead": True,
        "decision_timestamps_only": True,
    }
    _write_json(OUT / "summary.json", summary)
    _write_json(
        OUT / "audit_metadata.json",
        {
            "audit_window": {"start": AUDIT_START, "end": AUDIT_END},
            "warmup_from": LOAD_START,
            "pipeline_dir": str(PIPELINE),
            "variants": ["G0", "G1", "G2", "G3", "G4", "G5"],
            "gate_4h_operational_length": GATE_4H_LEN,
            "aggregation": "closed 30m/4h only",
            "attribution": "LuxAlgo SMC structure subset — CC BY-NC-SA 4.0",
            "protected_hashes": hashes_after,
            "imports_into_policy_or_state_machine": False,
            "orders_executed": False,
            "variant_adopted": False,
        },
    )

    readme = f"""# LuxAlgo structure policy-gate audit

Read-only. No production policy changes. No orders. No commit.

## Attribution

Structure semantics from Smart Money Concepts [LuxAlgo] (CC BY-NC-SA 4.0).

## Decision

**{decision}** — {reason}

## Core answers

```json
{json.dumps(core_answers, indent=2, default=str)}
```

## Variants

- G0 baseline (existing setup allow)
- G1 30m CHoCH recovery warning
- G2 30m CHoCH+BOS local reversal
- G3 4h sticky macro (len={GATE_4H_LEN})
- G4 combined ladder
- G5 asymmetric trading gate

## Jan 13–15 reference

Must stay recovery / failed reversal under G2/G4/G5 (no confirmed macro long release).
Check: `jan13_15_no_confirmed_macro_long_release={no_false_jan}`
"""
    (OUT / "README.md").write_text(readme, encoding="utf-8")
    _p(f"done decision={decision} setups={len(rows)}")


if __name__ == "__main__":
    main()
