#!/usr/bin/env python3
"""Read-only multi-stage macro reversal confidence audit (C0–C4).

Separates macro_direction from reversal_state so counter-moves can escalate
through recovery → possible → probable → confirmed without an immediate binary
macro flip (except where a variant explicitly allows it).

C0 = prior R2 structure-gated flip baseline.
C1–C4 = scored confidence ladders (fixed points; not auto-tuned).

Does not modify production modules. No variant adopted.

Example:
  PYTHONPATH=. python3 -u research/regime_scanner/market_regime_reversal_confidence_audit.py
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.market_regime_macro_context_audit import (
    aggregate_closed_htf,
    run_htf_regime_timeline,
)
from research.regime_scanner.market_regime_macro_flip_structure_audit import (
    BEAR_BULL_RECOVERY,
    BULL_BEAR_PULLBACK,
    MACRO_BEAR,
    MACRO_BULL,
    POSSIBLE_BEAR_REV,
    POSSIBLE_BULL_REV,
    TRUE_RANGE_D,
    attach_structure,
    protective_for_bear_flip,
    protective_for_bull_flip,
    rebuild_4h,
    run_gated_variant,
)
from research.regime_scanner.market_regime_macro_stability_audit import apply_s2, display_direction
from research.regime_scanner.point_audit import json_safe

OUT = Path("research/regime_scanner/results/market_regime_reversal_confidence_audit")
MARKET = Path("research/regime_scanner/market_regime.py")
STRUCTURE = Path("research/regime_scanner/trend_structure.py")
MACHINE = Path("research/regime_scanner/trend_state_machine.py")
POLICY = Path("research/regime_scanner/trend_state_policy.py")
ZONES = Path("research/regime_scanner/trend_zones.py")

LOAD_START = "2025-12-27T00:00:00+00:00"
AUDIT_START = "2026-01-06T00:00:00+00:00"
AUDIT_END = "2026-03-16T23:59:00+00:00"
OUTCOME_BARS = 6  # 24h on 4h grid — same horizon as flip audit

FOCUS = {
    "jan13_15": ("2026-01-13T00:00:00+00:00", "2026-01-15T23:59:00+00:00"),
    "jan17_19": ("2026-01-17T00:00:00+00:00", "2026-01-19T23:59:00+00:00"),
    "jan27_31": ("2026-01-27T00:00:00+00:00", "2026-01-31T23:59:00+00:00"),
    "feb05_07": ("2026-02-05T00:00:00+00:00", "2026-02-07T23:59:00+00:00"),
    "feb25_28": ("2026-02-25T00:00:00+00:00", "2026-02-28T23:59:00+00:00"),
    "march_05_10": ("2026-03-05T00:00:00+00:00", "2026-03-10T23:59:00+00:00"),
}

# Pine / timeline display codes
D_MACRO_BULL = 1
D_MACRO_BEAR = 2
D_RECOVERY = 3
D_POSSIBLE = 4
D_PROBABLE = 5
D_CONFIRMED_REV = 6
D_FAILED = 7
D_NEUTRAL = 8

DISPLAY_NAMES = {
    D_MACRO_BULL: "macro_bullish",
    D_MACRO_BEAR: "macro_bearish",
    D_RECOVERY: "countertrend_recovery",
    D_POSSIBLE: "possible_reversal",
    D_PROBABLE: "probable_reversal",
    D_CONFIRMED_REV: "confirmed_reversal",
    D_FAILED: "failed_reversal",
    D_NEUTRAL: "neutral_transition",
}

VARIANT_DEFS = {
    "C0": "R2 structure-gated baseline (binary-ish flip after break+HL/LH).",
    "C1": "Fixed point score ladder; macro flips only at confirmed_reversal (score>=6).",
    "C2": "C1 with time decay on stale confirmations without follow-through.",
    "C3": "C1 but macro direction flips already at probable_reversal (score>=4).",
    "C4": "C1; old macro held until confirmed; probable → neutral transition only.",
}

GROUND_TRUTH_NOTE = {
    "true_reversal_definition": (
        "After a candidate reaches probable/confirmed (or C0 hard flip), look ahead "
        f"{OUTCOME_BARS} closed 4h bars (~24h). true_reversal if price does not return "
        "inside the broken structure level and net progress in the new direction exceeds "
        "adverse move by >20%."
    ),
    "false_reversal_definition": (
        "Within the same window, price re-enters the old structure and adverse move "
        "dominates — treated as false_reversal / failed_reversal."
    ),
    "future_window": f"{OUTCOME_BARS} x 4h bars (~24h) after key timestamp",
    "weaknesses": [
        "24h window may mislabel slow valid reversals as false or unclear.",
        "Does not use multi-day trend destination; chart review still required.",
        "Local 30m alignment is as-of last closed 30m <= 4h decision_time only.",
    ],
    "manual_chart_review_required": list(FOCUS.keys()),
}


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object | None) -> str | None:
    if v is None:
        return None
    return _ts(v).isoformat()


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(json_safe(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
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
        w.writerows(rows)


def pine_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


def build_30m_regime(ind5: pd.DataFrame, end_wall: pd.Timestamp) -> list[dict[str, Any]]:
    """Closed 30m K2_H4 regime timeline (causal)."""
    ohlcv5 = ind5[["timestamp", "open", "high", "low", "close", "volume"]]
    agg30 = aggregate_closed_htf(ohlcv5, 30, end_wall)
    scfg = default_regime_scanner_config()
    ind30 = compute_indicator_frame(agg30, config=scfg).copy()
    ind30["timestamp"] = pd.to_datetime(ind30["timestamp"], utc=True)
    ind30["decision_time"] = pd.to_datetime(agg30["decision_time"], utc=True).to_numpy()
    return run_htf_regime_timeline(ind30)


def asof_30m_dir(tl30: list[dict[str, Any]], t: pd.Timestamp) -> int:
    """Last closed 30m regime direction with decision_time <= t."""
    best = None
    for r in tl30:
        dt = _ts(r["decision_time"])
        if dt <= t:
            best = r
        else:
            break
    if best is None:
        return 0
    reg = best["regime"]
    if reg == "strong_bullish_trend":
        return 1
    if reg == "strong_bearish_trend":
        return -1
    return 0


def count_aligned_strong_30m(
    tl30: list[dict[str, Any]],
    t0: pd.Timestamp,
    t1: pd.Timestamp,
    direction: int,
) -> int:
    """Count 30m strong bars aligned to direction in [t0, t1]."""
    n = 0
    want = "strong_bullish_trend" if direction > 0 else "strong_bearish_trend"
    for r in tl30:
        dt = _ts(r["decision_time"])
        if dt < t0:
            continue
        if dt > t1:
            break
        if r["regime"] == want:
            n += 1
    return n


def prepare_frames() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[int], list[int]]:
    """Return snaps (4h+structure+regime), tl30, s2_codes, r2_display-ish codes."""
    end_wall = _ts(AUDIT_END)
    raw = load_symbol_candles("APTUSDT")
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    sl = raw[(raw["timestamp"] >= _ts(LOAD_START)) & (raw["timestamp"] <= _ts("2026-03-16 23:55:00+00:00"))]
    scfg = default_regime_scanner_config()
    frame5 = compute_indicator_frame(sl, config=scfg)
    frame5["timestamp"] = pd.to_datetime(frame5["timestamp"], utc=True)

    ind4, full_tl = rebuild_4h()
    struct_all = attach_structure(ind4)
    by_dt = {_ts(r["decision_time"]): r for r in full_tl}
    snaps: list[dict[str, Any]] = []
    tl_audit: list[dict[str, Any]] = []
    atr_proxy = None
    run_ext_bull = None
    run_ext_bear = None
    for s in struct_all:
        dt = _ts(s["decision_time"])
        if dt < _ts(AUDIT_START) or dt > _ts(AUDIT_END):
            continue
        if dt not in by_dt:
            continue
        close = float(s["close"])
        high = float(s["high"])
        low = float(s["low"])
        br = max(high - low, 1e-12)
        atr_proxy = br if atr_proxy is None else 0.7 * atr_proxy + 0.3 * br
        run_ext_bull = high if run_ext_bull is None else max(run_ext_bull, high)
        run_ext_bear = low if run_ext_bear is None else min(run_ext_bear, low)
        row = {
            **s,
            "regime": by_dt[dt]["regime"],
            "atr_proxy": atr_proxy,
            "run_extreme_high": run_ext_bull,
            "run_extreme_low": run_ext_bear,
        }
        snaps.append(row)
        tl_audit.append(by_dt[dt])

    tl30 = build_30m_regime(frame5, end_wall)
    s2 = apply_s2(tl_audit)
    r2_codes, _ = run_gated_variant(variant="R2", s2_codes=s2, snaps=snaps)
    return snaps, tl30, s2, r2_codes


# ---------------------------------------------------------------------------
# Feature / score engine
# ---------------------------------------------------------------------------


@dataclass
class Episode:
    case_id: str
    old_macro: int  # +1/-1
    proposed: int  # opposite
    start_i: int
    level: float
    break_i: int | None = None
    score: float = 0.0
    score_components: dict[str, float] = field(default_factory=dict)
    state: str = "countertrend_recovery"
    possible_i: int | None = None
    probable_i: int | None = None
    confirmed_i: int | None = None
    flip_i: int | None = None
    fail_i: int | None = None
    highest_score: float = 0.0
    timeline: list[dict[str, Any]] = field(default_factory=list)
    active: bool = True
    decay_age: int = 0


def score_to_state(score: float) -> str:
    if score <= 1:
        return "countertrend_recovery"
    if score <= 3:
        return "possible_reversal"
    if score <= 5:
        return "probable_reversal"
    return "confirmed_reversal"


def state_to_display(macro_dir: int, rev_state: str, *, neutral: bool = False) -> int:
    if neutral or (macro_dir == 0 and rev_state in {"probable_reversal", "confirmed_reversal"}):
        if rev_state == "confirmed_reversal":
            return D_CONFIRMED_REV
        return D_NEUTRAL
    if rev_state == "none":
        return D_MACRO_BULL if macro_dir > 0 else D_MACRO_BEAR if macro_dir < 0 else D_NEUTRAL
    if rev_state == "countertrend_recovery":
        return D_RECOVERY
    if rev_state == "possible_reversal":
        return D_POSSIBLE
    if rev_state == "probable_reversal":
        return D_PROBABLE
    if rev_state == "confirmed_reversal":
        return D_CONFIRMED_REV
    if rev_state == "failed_reversal":
        return D_FAILED
    return D_NEUTRAL


def compute_features(
    *,
    snaps: list[dict[str, Any]],
    i: int,
    ep: Episode,
    tl30: list[dict[str, Any]],
) -> dict[str, Any]:
    snap = snaps[i]
    close = float(snap["close"])
    level = ep.level
    proposed = ep.proposed
    beyond = close > level if proposed > 0 else close < level
    dist_pct = (close - level) / level * 100.0 if proposed > 0 else (level - close) / level * 100.0

    # closes beyond since break
    n_beyond = 0
    if ep.break_i is not None:
        for k in range(ep.break_i, i + 1):
            c = float(snaps[k]["close"])
            if (c > level) if proposed > 0 else (c < level):
                n_beyond += 1

    hl_after = False
    lh_after = False
    retest_held = False
    if ep.break_i is not None:
        br_t = snaps[ep.break_i]["decision_time"]
        for k in range(ep.break_i + 1, i + 1):
            ev = snaps[k].get("events") or []
            if proposed > 0 and "higher_low" in ev:
                hl_after = True
            if proposed < 0 and "lower_high" in ev:
                lh_after = True
            if proposed > 0 and snaps[k].get("bullish_retest_holds"):
                retest_held = True
            if proposed < 0 and snaps[k].get("bearish_retest_holds"):
                retest_held = True
            # synthetic retest: dipped to/through level then closed beyond again
        if ep.break_i is not None and beyond and n_beyond >= 2:
            dipped = any(
                (float(snaps[k]["close"]) <= level) if proposed > 0 else (float(snaps[k]["close"]) >= level)
                for k in range(ep.break_i + 1, i)
            )
            if dipped:
                retest_held = True

    reentered = not beyond and ep.break_i is not None

    # HH/HL or LH/LL sequence after break
    seq_ok = False
    if ep.break_i is not None:
        evs = []
        for k in range(ep.break_i, i + 1):
            evs.extend(snaps[k].get("events") or [])
        if proposed > 0:
            seq_ok = ("higher_high" in evs) and ("higher_low" in evs)
        else:
            seq_ok = ("lower_low" in evs) and ("lower_high" in evs)

    move_pct = 0.0
    if ep.break_i is not None:
        c0 = float(snaps[ep.start_i]["close"])
        move_pct = (close - c0) / c0 * 100.0 if proposed > 0 else (c0 - close) / c0 * 100.0

    atr = float(snap.get("atr_proxy") or 1e-12)
    move_atr = abs(close - float(snaps[ep.start_i]["close"])) / atr
    # vol expansion vs prior bar
    prev_range = max(float(snaps[i - 1]["high"]) - float(snaps[i - 1]["low"]), 1e-12) if i > 0 else atr
    cur_range = max(float(snap["high"]) - float(snap["low"]), 1e-12)
    vol_expand = cur_range / prev_range

    local_dir = asof_30m_dir(tl30, _ts(snap["decision_time"]))
    local_aligned = local_dir == proposed
    n_local_strong = count_aligned_strong_30m(
        tl30,
        _ts(snaps[ep.start_i]["decision_time"]),
        _ts(snap["decision_time"]),
        proposed,
    )

    # counter-reaction: opposite strong structure event
    counter = False
    if proposed > 0 and ("bearish_choch" in (snap.get("events") or []) or "lower_low" in (snap.get("events") or [])):
        counter = True
    if proposed < 0 and ("bullish_choch" in (snap.get("events") or []) or "higher_high" in (snap.get("events") or [])):
        counter = True

    hours_since_break = None
    if ep.break_i is not None:
        hours_since_break = (_ts(snap["decision_time"]) - _ts(snaps[ep.break_i]["decision_time"])).total_seconds() / 3600.0

    if proposed > 0:
        old_ext = float(snap.get("run_extreme_low") or snaps[ep.start_i]["low"])
        dist_ext = (close - old_ext) / old_ext * 100.0
    else:
        old_ext = float(snap.get("run_extreme_high") or snaps[ep.start_i]["high"])
        dist_ext = (old_ext - close) / old_ext * 100.0

    broke = beyond  # close beyond level
    if ep.break_i is None and beyond:
        broke = True

    return {
        "structure_break": broke or ep.break_i is not None,
        "close_beyond": beyond,
        "break_distance_pct": dist_pct if beyond else None,
        "n_closes_beyond": n_beyond,
        "higher_low_after_break": hl_after,
        "lower_high_after_break": lh_after,
        "retest_held": retest_held,
        "old_structure_reentered": reentered,
        "hh_hl_or_lh_ll_sequence": seq_ok,
        "move_since_start_pct": move_pct,
        "move_vs_atr": move_atr,
        "vol_expansion": vol_expand,
        "local_30m_aligned": local_aligned,
        "n_local_aligned_strong_30m": n_local_strong,
        "counter_reaction": counter,
        "hours_since_break": hours_since_break,
        "distance_to_old_run_extreme_pct": dist_ext,
    }


def points_from_features(feat: dict[str, Any], proposed: int, *, decay: float = 1.0) -> tuple[float, dict[str, float]]:
    """C1 fixed points (optionally scaled by decay for C2)."""
    comp: dict[str, float] = {}
    if feat["structure_break"]:
        comp["structure_break"] = 2.0 * decay
    if feat["n_closes_beyond"] >= 2:
        comp["two_closes_beyond"] = 1.0 * decay
    if proposed > 0 and feat["higher_low_after_break"]:
        comp["hl_after_break"] = 2.0 * decay
    if proposed < 0 and feat["lower_high_after_break"]:
        comp["lh_after_break"] = 2.0 * decay
    if feat["retest_held"]:
        comp["retest_held"] = 2.0 * decay
    if feat["local_30m_aligned"]:
        comp["local_30m_aligned"] = 1.0 * decay
    if feat["hh_hl_or_lh_ll_sequence"]:
        comp["structure_sequence"] = 1.0 * decay
    if feat["old_structure_reentered"]:
        comp["reentered_old_structure"] = -3.0
    if feat["counter_reaction"]:
        comp["counter_structure"] = -3.0
    return float(sum(comp.values())), comp


def outcome_label(
    snaps: list[dict[str, Any]],
    key_i: int,
    proposed: int,
    level: float,
) -> dict[str, Any]:
    end_i = min(len(snaps) - 1, key_i + OUTCOME_BARS)
    close0 = float(snaps[key_i]["close"])
    futures = [float(snaps[k]["close"]) for k in range(key_i, end_i + 1)]
    if proposed > 0:
        returned = any(c < level for c in futures[1:])
        progress = max(futures) - close0
        adverse = close0 - min(futures)
    else:
        returned = any(c > level for c in futures[1:])
        progress = close0 - min(futures)
        adverse = max(futures) - close0
    if returned and adverse >= progress:
        return {"true_reversal": False, "false_reversal": True, "final_outcome": "false_reversal"}
    if returned:
        return {"true_reversal": False, "false_reversal": True, "final_outcome": "countertrend_recovery"}
    if progress > adverse * 1.2:
        return {"true_reversal": True, "false_reversal": False, "final_outcome": "true_reversal"}
    return {"true_reversal": False, "false_reversal": False, "final_outcome": "unclear"}


# ---------------------------------------------------------------------------
# Variant runners
# ---------------------------------------------------------------------------


def map_r2_to_confidence_display(r2_codes: list[int]) -> list[int]:
    out = []
    for c in r2_codes:
        if c == MACRO_BULL:
            out.append(D_MACRO_BULL)
        elif c == MACRO_BEAR:
            out.append(D_MACRO_BEAR)
        elif c in (BEAR_BULL_RECOVERY, BULL_BEAR_PULLBACK):
            out.append(D_RECOVERY)
        elif c in (POSSIBLE_BULL_REV, POSSIBLE_BEAR_REV):
            out.append(D_POSSIBLE)
        elif c == TRUE_RANGE_D:
            out.append(D_NEUTRAL)
        else:
            out.append(D_NEUTRAL)
    return out


def extract_c0_cases(
    snaps: list[dict[str, Any]],
    r2_codes: list[int],
    display: list[int],
) -> list[dict[str, Any]]:
    """Episodes where R2 hard-flips macro side."""
    cases = []
    last_side = 0
    case_n = 0
    for i, c in enumerate(r2_codes):
        side = 1 if c == MACRO_BULL else -1 if c == MACRO_BEAR else 0
        if side != 0 and side != last_side:
            case_n += 1
            level = protective_for_bull_flip(snaps[i]) if side > 0 else protective_for_bear_flip(snaps[i])
            if level is None:
                level = float(snaps[i]["close"])
            oc = outcome_label(snaps, i, side, float(level))
            cases.append(
                {
                    "case_id": f"C0_{case_n:03d}",
                    "variant": "C0",
                    "start_timestamp_utc": _iso(snaps[i]["decision_time"]),
                    "old_macro_direction": {1: "bullish", -1: "bearish", 0: "neutral"}.get(last_side, "neutral"),
                    "proposed_direction": "bullish" if side > 0 else "bearish",
                    "first_structure_break_utc": _iso(snaps[i]["decision_time"]),
                    "possible_reversal_utc": None,
                    "probable_reversal_utc": None,
                    "confirmed_reversal_utc": _iso(snaps[i]["decision_time"]),
                    "final_macro_flip_utc": _iso(snaps[i]["decision_time"]),
                    "failure_timestamp_utc": None,
                    "highest_score": None,
                    "score_timeline": "",
                    "contributing_features": "r2_break_plus_hl_lh",
                    "invalidating_features": "",
                    "old_structure_reentered": oc["false_reversal"],
                    **oc,
                    "missed_reversal": False,
                    "delay_vs_ground_truth_hours": 0.0,
                    "delay_vs_r0_hours": None,
                    "delay_vs_r2_hours": 0.0,
                    "protective_level": level,
                }
            )
            last_side = side
        elif side != 0:
            last_side = side
        elif display[i] == D_NEUTRAL:
            last_side = 0
    return cases


def run_confidence_variant(
    *,
    variant: str,
    snaps: list[dict[str, Any]],
    s2_codes: list[int],
    tl30: list[dict[str, Any]],
) -> tuple[list[int], list[int], list[str], list[dict[str, Any]], list[dict[str, Any]]]:
    """Returns display_codes, macro_dirs, rev_states, cases, score_timeline_rows."""
    use_decay = variant == "C2"
    flip_at_probable = variant == "C3"
    probable_to_neutral = variant == "C4"

    macro = 0
    seeded = False
    ep: Episode | None = None
    displays: list[int] = []
    macros: list[int] = []
    states: list[str] = []
    cases: list[dict[str, Any]] = []
    score_rows: list[dict[str, Any]] = []
    case_n = 0
    failed_flash: int | None = None

    def close_episode(i: int, *, failed: bool) -> None:
        nonlocal ep, case_n, failed_flash
        if ep is None or not ep.active:
            return
        ep.active = False
        if failed:
            ep.fail_i = i
            ep.state = "failed_reversal"
            failed_flash = i
        key_i = ep.confirmed_i or ep.probable_i or ep.possible_i or ep.break_i or ep.start_i
        oc = outcome_label(snaps, key_i, ep.proposed, ep.level)
        case_n += 1
        cases.append(
            {
                "case_id": f"{variant}_{case_n:03d}",
                "variant": variant,
                "start_timestamp_utc": _iso(snaps[ep.start_i]["decision_time"]),
                "old_macro_direction": {1: "bullish", -1: "bearish", 0: "neutral"}[ep.old_macro],
                "proposed_direction": "bullish" if ep.proposed > 0 else "bearish",
                "first_structure_break_utc": None if ep.break_i is None else _iso(snaps[ep.break_i]["decision_time"]),
                "possible_reversal_utc": None if ep.possible_i is None else _iso(snaps[ep.possible_i]["decision_time"]),
                "probable_reversal_utc": None if ep.probable_i is None else _iso(snaps[ep.probable_i]["decision_time"]),
                "confirmed_reversal_utc": None if ep.confirmed_i is None else _iso(snaps[ep.confirmed_i]["decision_time"]),
                "final_macro_flip_utc": None if ep.flip_i is None else _iso(snaps[ep.flip_i]["decision_time"]),
                "failure_timestamp_utc": None if ep.fail_i is None else _iso(snaps[ep.fail_i]["decision_time"]),
                "highest_score": ep.highest_score,
                "score_timeline": "|".join(f"{r['score']:.1f}@{r['decision_time']}" for r in ep.timeline),
                "contributing_features": ",".join(k for k, v in ep.score_components.items() if v > 0),
                "invalidating_features": ",".join(k for k, v in ep.score_components.items() if v < 0),
                "old_structure_reentered": any(r.get("old_structure_reentered") for r in ep.timeline),
                **oc,
                "missed_reversal": False,
                "delay_vs_ground_truth_hours": None,
                "delay_vs_r0_hours": None,
                "delay_vs_r2_hours": None,
                "protective_level": ep.level,
            }
        )
        ep = None

    def start_episode(i: int, proposed: int, snap: dict[str, Any]) -> None:
        nonlocal ep
        level = protective_for_bull_flip(snap) if proposed > 0 else protective_for_bear_flip(snap)
        if level is None:
            level = float(snap["high"] if proposed > 0 else snap["low"])
        ep = Episode(
            case_id="tmp",
            old_macro=macro,
            proposed=proposed,
            start_i=i,
            level=float(level),
        )

    for i, (s2, snap) in enumerate(zip(s2_codes, snaps)):
        intent = display_direction(s2)
        close = float(snap["close"])
        rev_state = "none"
        neutral = False

        if failed_flash is not None and i > failed_flash:
            failed_flash = None

        # Seed macro once from first strong S2 trend (no silent re-seed after clear)
        if not seeded and intent != 0:
            macro = intent
            seeded = True

        # Detect opposing pressure: opposite S2 intent OR close through protective level
        opp = 0
        if macro != 0:
            if intent == -macro:
                opp = intent
            else:
                bull_lvl = protective_for_bull_flip(snap)
                bear_lvl = protective_for_bear_flip(snap)
                if macro < 0 and bull_lvl is not None and close > bull_lvl:
                    opp = 1
                elif macro > 0 and bear_lvl is not None and close < bear_lvl:
                    opp = -1

        if opp != 0 and macro != 0:
            if ep is None or not ep.active:
                start_episode(i, opp, snap)
            elif ep.proposed != opp:
                # reverse the counter-move → fail prior episode, start new
                close_episode(i, failed=True)
                start_episode(i, opp, snap)

        if ep is not None and ep.active:
            if ep.break_i is None:
                lvl = protective_for_bull_flip(snap) if ep.proposed > 0 else protective_for_bear_flip(snap)
                if lvl is not None:
                    ep.level = float(lvl)

            feat = compute_features(snaps=snaps, i=i, ep=ep, tl30=tl30)
            if feat["close_beyond"] and ep.break_i is None:
                ep.break_i = i

            decay = 1.0
            if use_decay:
                if ep.break_i is not None and not feat["close_beyond"]:
                    ep.decay_age += 1
                elif feat["n_closes_beyond"] < 2 and ep.break_i is not None:
                    ep.decay_age += 1
                else:
                    ep.decay_age = max(0, ep.decay_age - 1)
                decay = max(0.25, 1.0 - 0.15 * ep.decay_age)

            score, comp = points_from_features(feat, ep.proposed, decay=decay)
            ep.score = score
            ep.score_components = comp
            ep.highest_score = max(ep.highest_score, score)
            st = score_to_state(score)
            ep.state = st
            if st == "possible_reversal" and ep.possible_i is None:
                ep.possible_i = i
            if st == "probable_reversal" and ep.probable_i is None:
                ep.probable_i = i
            if st == "confirmed_reversal" and ep.confirmed_i is None:
                ep.confirmed_i = i

            ep.timeline.append(
                {
                    "decision_time": _iso(snap["decision_time"]),
                    "score": score,
                    "state": st,
                    "old_structure_reentered": feat["old_structure_reentered"],
                }
            )
            score_rows.append(
                {
                    "variant": variant,
                    "case_active_start": _iso(snaps[ep.start_i]["decision_time"]),
                    "decision_time": _iso(snap["decision_time"]),
                    "macro_direction": {1: "bullish", -1: "bearish", 0: "neutral"}[macro],
                    "proposed_direction": "bullish" if ep.proposed > 0 else "bearish",
                    "score": score,
                    "reversal_state": st,
                    **comp,
                    **{f"feat_{k}": v for k, v in feat.items()},
                }
            )

            flipped = False
            if feat["old_structure_reentered"] and score <= 0 and ep.break_i is not None:
                close_episode(i, failed=True)
                rev_state = "failed_reversal"
            elif flip_at_probable and st in {"probable_reversal", "confirmed_reversal"}:
                macro = ep.proposed
                ep.flip_i = i
                close_episode(i, failed=False)
                rev_state = "none"
                flipped = True
            elif (not flip_at_probable) and st == "confirmed_reversal":
                macro = ep.proposed
                ep.flip_i = i
                close_episode(i, failed=False)
                rev_state = "none"
                flipped = True
            elif probable_to_neutral and st == "probable_reversal":
                neutral = True
                rev_state = "probable_reversal"
            else:
                rev_state = st

            # Same-direction S2 resume with weak score → fail recovery
            if ep is not None and ep.active and intent == ep.old_macro and score < 2 and ep.break_i is not None:
                close_episode(i, failed=True)
                rev_state = "failed_reversal"

            if flipped:
                rev_state = "none"

        # Display
        if failed_flash == i:
            disp = D_FAILED
            rev_state = "failed_reversal"
        elif ep is not None and ep.active:
            disp = state_to_display(macro, rev_state, neutral=neutral)
        elif rev_state == "failed_reversal":
            disp = D_FAILED
        else:
            disp = state_to_display(macro, "none")

        displays.append(disp)
        macros.append(macro)
        states.append(rev_state)

    if ep is not None and ep.active:
        close_episode(len(snaps) - 1, failed=False)

    return displays, macros, states, cases, score_rows


def annotate_delays(
    cases: list[dict[str, Any]],
    r0_flip_times: list[tuple[pd.Timestamp, str]],
    r2_flip_times: list[tuple[pd.Timestamp, str]],
) -> None:
    for c in cases:
        prop = c["proposed_direction"]
        t_flip = c.get("final_macro_flip_utc") or c.get("confirmed_reversal_utc") or c.get("probable_reversal_utc")
        if not t_flip:
            continue
        t = _ts(t_flip)
        for label, pool, key in (
            ("r0", r0_flip_times, "delay_vs_r0_hours"),
            ("r2", r2_flip_times, "delay_vs_r2_hours"),
        ):
            match = None
            for ft, d in pool:
                if d == prop and ft <= t + pd.Timedelta(hours=1):
                    # nearest prior or equal R flip
                    match = ft
            # better: first R flip of same direction at or after case start, compare to our flip
            start = _ts(c["start_timestamp_utc"])
            cand = [ft for ft, d in pool if d == prop and ft >= start - pd.Timedelta(hours=4)]
            if cand:
                ref = cand[0]
                c[key] = (t - ref).total_seconds() / 3600.0
            else:
                c[key] = None


def hard_flip_count(macros: list[int]) -> int:
    n = 0
    last = 0
    for m in macros:
        if m != 0 and m != last and last != 0 and m == -last:
            n += 1
        if m != 0:
            last = m
        if m == 0:
            last = 0
    return n


def collapse_intervals(snaps: list[dict[str, Any]], codes: list[int]) -> list[dict[str, Any]]:
    if not codes:
        return []
    out = []
    start = 0
    for i in range(1, len(codes) + 1):
        if i < len(codes) and codes[i] == codes[start]:
            continue
        chunk = snaps[start:i]
        end_ts = snaps[i]["decision_time"] if i < len(snaps) else _ts(chunk[-1]["decision_time"]) + pd.Timedelta(hours=4)
        out.append(
            {
                "start_utc": _iso(chunk[0]["decision_time"]),
                "end_utc": _iso(end_ts),
                "display_code": codes[start],
                "display_class": DISPLAY_NAMES[codes[start]],
                "n_4h_bars": len(chunk),
            }
        )
        start = i
    return out


def build_pine(variant: str, intervals: list[dict[str, Any]], month: int = 1) -> str:
    start = _ts(f"2026-{month:02d}-01T00:00:00+00:00")
    end = start + pd.offsets.MonthBegin(1)
    month_iv = [r for r in intervals if _ts(r["end_utc"]) > start and _ts(r["start_utc"]) < end]
    items = [(_ts(r["start_utc"]), _ts(r["end_utc"]), int(r["display_code"])) for r in month_iv]
    helpers: list[str] = []
    calls: list[str] = []
    chunk = 8
    if not items:
        helpers.append("f_load_00() =>\n    true")
        calls.append("    f_load_00()")
    else:
        for i in range(0, len(items), chunk):
            fn = f"f_load_{i // chunk:02d}"
            body = [f"{fn}() =>"]
            for s, e, d in items[i : i + chunk]:
                body.append(
                    f"    array.push(macroStarts, f_ts({s.year}, {s.month}, {s.day}, {s.hour}, {s.minute}))"
                )
                body.append(
                    f"    array.push(macroEnds, f_ts({e.year}, {e.month}, {e.day}, {e.hour}, {e.minute}))"
                )
                body.append(f"    array.push(macroTypes, {d})")
            helpers.append("\n".join(body))
            calls.append(f"    {fn}()")

    title = f"Macro Reversal Confidence {variant} 2026-{month:02d}"
    return f"""//@version=6
indicator(
     "{pine_escape(title)}",
     overlay = true,
     max_labels_count = 500,
     max_lines_count = 100
)

// {variant}: {pine_escape(VARIANT_DEFS[variant])}
// 1 macro_bull 2 macro_bear 3 recovery 4 possible 5 probable 6 confirmed_rev 7 failed 8 neutral
// UTC start=decision_time; end exclusive.

showMacroBackground = input.bool(true, "Show macro/reversal background")
showLabels = input.bool(false, "Show labels")
macroTransparency = input.int(85, "Transparency", minval = 70, maxval = 95)

f_ts(y, m, d, h, mi) =>
    timestamp("UTC", y, m, d, h, mi)

var int[] macroStarts = array.new_int()
var int[] macroEnds = array.new_int()
var int[] macroTypes = array.new_int()

{chr(10).join(helpers)}

if barstate.isfirst
{chr(10).join(calls)}

int activeType = 0
bool startBar = false
if array.size(macroStarts) > 0
    for i = 0 to array.size(macroStarts) - 1
        int ms = array.get(macroStarts, i)
        int me = array.get(macroEnds, i)
        if time_close >= ms and time_close < me
            activeType := array.get(macroTypes, i)
        if time_close == ms
            startBar := true
            activeType := array.get(macroTypes, i)

bgcolor(
     showMacroBackground ?
         (activeType == 1 ? color.new(color.green, macroTransparency) :
          activeType == 2 ? color.new(color.red, macroTransparency) :
          activeType == 3 ? color.new(#c5e1a5, math.min(macroTransparency + 3, 95)) :
          activeType == 4 ? color.new(color.yellow, math.min(macroTransparency + 2, 95)) :
          activeType == 5 ? color.new(#ffeb3b, math.min(macroTransparency + 0, 95)) :
          activeType == 6 ? color.new(#00c853, math.min(macroTransparency + 0, 95)) :
          activeType == 7 ? color.new(color.gray, math.min(macroTransparency - 5, 90)) :
          activeType == 8 ? color.new(#9e9e9e, math.min(macroTransparency + 4, 95)) :
          na) :
     na
)

f_label() =>
    activeType == 1 ? "MACRO BULL" :
     activeType == 2 ? "MACRO BEAR" :
     activeType == 3 ? "RECOVERY" :
     activeType == 4 ? "POSSIBLE REV" :
     activeType == 5 ? "PROBABLE REV" :
     activeType == 6 ? "CONFIRMED REV" :
     activeType == 7 ? "FAILED REV" :
     activeType == 8 ? "NEUTRAL" :
     ""

if showLabels and startBar and activeType != 0
    label.new(bar_index, high, f_label(), style = label.style_label_down, color = color.new(color.black, 40), textcolor = color.white, size = size.tiny)

// EOF
"""


def metrics_for(
    variant: str,
    cases: list[dict[str, Any]],
    displays: list[int],
    macros: list[int],
    snaps: list[dict[str, Any]],
) -> dict[str, Any]:
    n_false = sum(1 for c in cases if c.get("false_reversal"))
    n_true = sum(1 for c in cases if c.get("true_reversal"))
    n_failed = sum(1 for c in cases if c.get("failure_timestamp_utc"))
    delays = [c["delay_vs_r2_hours"] for c in cases if c.get("delay_vs_r2_hours") is not None]
    # time in states
    poss = sum(1 for d in displays if d == D_POSSIBLE)
    prob = sum(1 for d in displays if d == D_PROBABLE)
    rec = sum(1 for d in displays if d == D_RECOVERY)

    # jan13_15
    a, b = _ts(FOCUS["jan13_15"][0]), _ts(FOCUS["jan13_15"][1])
    jan_disp = [d for s, d in zip(snaps, displays) if a <= _ts(s["decision_time"]) <= b]
    return {
        "variant": variant,
        "definition": VARIANT_DEFS[variant],
        "n_cases": len(cases),
        "n_false_reversals": n_false,
        "n_true_reversals": n_true,
        "n_failed_candidates": n_failed,
        "n_hard_macro_flips": hard_flip_count(macros),
        "avg_delay_vs_r2_hours": float(sum(delays) / len(delays)) if delays else None,
        "median_delay_vs_r2_hours": float(pd.Series(delays).median()) if delays else None,
        "max_delay_vs_r2_hours": float(max(delays)) if delays else None,
        "bars_possible_reversal": poss,
        "bars_probable_reversal": prob,
        "bars_recovery": rec,
        "jan13_15_macro_bull_bars": sum(1 for d in jan_disp if d == D_MACRO_BULL),
        "jan13_15_recovery_bars": sum(1 for d in jan_disp if d == D_RECOVERY),
        "jan13_15_possible_bars": sum(1 for d in jan_disp if d == D_POSSIBLE),
        "jan13_15_probable_bars": sum(1 for d in jan_disp if d == D_PROBABLE),
        "jan13_15_confirmed_bars": sum(1 for d in jan_disp if d == D_CONFIRMED_REV),
        "jan13_15_ok_no_macro_bull": all(d != D_MACRO_BULL for d in jan_disp),
    }


def decide_letter(metrics: list[dict[str, Any]], r2_false: int = 2, r2_delay: float = 43.0) -> str:
    gated = [m for m in metrics if m["variant"] != "C0"]
    j_candidates = []
    for m in gated:
        jan_ok = m["jan13_15_ok_no_macro_bull"] and (
            m["jan13_15_recovery_bars"] + m["jan13_15_possible_bars"] + m["jan13_15_probable_bars"] > 0
        )
        near_false = m["n_false_reversals"] <= r2_false + 1
        earlier = (m.get("median_delay_vs_r2_hours") is not None) and m["median_delay_vs_r2_hours"] < 0
        low_miss = m.get("n_missed_true_reversals", 99) <= 1
        if jan_ok and near_false and earlier and low_miss and m["n_true_reversals"] >= 3:
            j_candidates.append(m)
    if j_candidates:
        return "J"

    # Useful ladder on reference window (mostly non-macro-bull) but false/delay tradeoff remains
    useful_ladder = [
        m
        for m in gated
        if m["jan13_15_macro_bull_bars"] <= 1
        and (m["jan13_15_possible_bars"] + m["jan13_15_probable_bars"] + m["jan13_15_recovery_bars"]) >= 8
    ]
    if useful_ladder and any(m["n_false_reversals"] > r2_false for m in useful_ladder):
        return "U"
    if useful_ladder and any(
        (m.get("median_delay_vs_r2_hours") is not None and m["median_delay_vs_r2_hours"] < 0) for m in useful_ladder
    ):
        return "U"
    if not any(
        (m["jan13_15_possible_bars"] + m["jan13_15_probable_bars"] + m["jan13_15_recovery_bars"]) > 0 for m in gated
    ):
        return "N"
    return "U"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    hashes_before = {
        "market_regime.py": _md5(MARKET),
        "trend_structure.py": _md5(STRUCTURE),
        "trend_state_machine.py": _md5(MACHINE),
        "trend_state_policy.py": _md5(POLICY),
        "trend_zones.py": _md5(ZONES),
    }
    _write_json(OUT / "hashes_before.json", hashes_before)

    print("Preparing 4h structure + 30m regimes…")
    snaps, tl30, s2, r2_codes = prepare_frames()
    print(f"4h bars={len(snaps)} 30m bars={len(tl30)}")

    # C0 from R2
    c0_disp = map_r2_to_confidence_display(r2_codes)
    c0_cases = extract_c0_cases(snaps, r2_codes, c0_disp)
    # R0 flips from S2 map for delay refs
    from research.regime_scanner.market_regime_macro_flip_structure_audit import map_s2_to_display, extract_flips_from_codes

    r0_codes = map_s2_to_display(s2)
    r0_flips = extract_flips_from_codes("R0", r0_codes, snaps, s2)
    r0_times = [(_ts(f["flip_timestamp_utc"]), f["proposed_new_direction"]) for f in r0_flips]
    r2_flips_codes, r2_flips = run_gated_variant(variant="R2", s2_codes=s2, snaps=snaps)
    r2_times = [(_ts(f["flip_timestamp_utc"]), f["proposed_new_direction"]) for f in r2_flips]

    results: dict[str, Any] = {
        "C0": {
            "displays": c0_disp,
            "macros": [1 if c == MACRO_BULL else -1 if c == MACRO_BEAR else 0 for c in r2_codes],
            "cases": c0_cases,
            "score_rows": [],
        }
    }

    for v in ("C1", "C2", "C3", "C4"):
        disp, macros, states, cases, score_rows = run_confidence_variant(
            variant=v, snaps=snaps, s2_codes=s2, tl30=tl30
        )
        annotate_delays(cases, r0_times, r2_times)
        results[v] = {"displays": disp, "macros": macros, "cases": cases, "score_rows": score_rows, "states": states}
        print(f"{v}: cases={len(cases)} false={sum(1 for c in cases if c.get('false_reversal'))} true={sum(1 for c in cases if c.get('true_reversal'))}")

    annotate_delays(c0_cases, r0_times, r2_times)

    all_cases = []
    all_scores = []
    metrics = []
    comparison = []
    for v in ("C0", "C1", "C2", "C3", "C4"):
        all_cases.extend(results[v]["cases"])
        all_scores.extend(results[v]["score_rows"])
        m = metrics_for(v, results[v]["cases"], results[v]["displays"], results[v]["macros"], snaps)
        # missed: R2 true flips with no confirmed/probable in variant within 48h after
        if v != "C0":
            missed = 0
            for rf in r2_flips:
                if not rf.get("true_reversal"):
                    continue
                t = _ts(rf["flip_timestamp_utc"])
                prop = rf["proposed_new_direction"]
                hit = False
                for c in results[v]["cases"]:
                    if c["proposed_direction"] != prop:
                        continue
                    t2 = c.get("final_macro_flip_utc") or c.get("confirmed_reversal_utc") or c.get("probable_reversal_utc")
                    if t2 and abs((_ts(t2) - t).total_seconds()) <= 48 * 3600:
                        hit = True
                        break
                if not hit:
                    missed += 1
            m["n_missed_true_reversals"] = missed
        else:
            m["n_missed_true_reversals"] = 0
        metrics.append(m)
        comparison.append(
            {
                "variant": v,
                "n_false_reversals": m["n_false_reversals"],
                "n_true_reversals": m["n_true_reversals"],
                "n_missed_true_reversals": m["n_missed_true_reversals"],
                "n_failed_candidates": m["n_failed_candidates"],
                "n_hard_macro_flips": m["n_hard_macro_flips"],
                "avg_delay_vs_r2_hours": m["avg_delay_vs_r2_hours"],
                "median_delay_vs_r2_hours": m["median_delay_vs_r2_hours"],
                "max_delay_vs_r2_hours": m["max_delay_vs_r2_hours"],
                "bars_possible": m["bars_possible_reversal"],
                "bars_probable": m["bars_probable_reversal"],
                "jan13_15_ok_no_macro_bull": m["jan13_15_ok_no_macro_bull"],
                "jan13_15_recovery": m["jan13_15_recovery_bars"],
                "jan13_15_possible": m["jan13_15_possible_bars"],
                "jan13_15_probable": m["jan13_15_probable_bars"],
                "jan13_15_macro_bull": m["jan13_15_macro_bull_bars"],
            }
        )
        _write_csv(
            OUT / f"timeline_{v.lower()}.csv",
            [
                {
                    "variant": v,
                    "decision_time": _iso(s["decision_time"]),
                    "display_class": DISPLAY_NAMES[d],
                    "macro_dir": results[v]["macros"][i],
                    "close": s["close"],
                }
                for i, (s, d) in enumerate(zip(snaps, results[v]["displays"]))
            ],
        )

    _write_csv(OUT / "reversal_cases.csv", all_cases)
    _write_csv(OUT / "reversal_score_timeline.csv", all_scores)
    _write_csv(OUT / "failed_reversals.csv", [c for c in all_cases if c.get("failure_timestamp_utc")])
    _write_csv(
        OUT / "confirmed_reversals.csv",
        [c for c in all_cases if c.get("confirmed_reversal_utc") or c.get("final_macro_flip_utc")],
    )
    _write_csv(OUT / "possible_reversals.csv", [c for c in all_cases if c.get("possible_reversal_utc")])
    _write_csv(OUT / "probable_reversals.csv", [c for c in all_cases if c.get("probable_reversal_utc")])
    _write_csv(OUT / "variant_comparison.csv", comparison)
    _write_csv(
        OUT / "delay_comparison.csv",
        [
            {
                "case_id": c["case_id"],
                "variant": c["variant"],
                "proposed_direction": c["proposed_direction"],
                "delay_vs_r0_hours": c.get("delay_vs_r0_hours"),
                "delay_vs_r2_hours": c.get("delay_vs_r2_hours"),
                "true_reversal": c.get("true_reversal"),
                "false_reversal": c.get("false_reversal"),
            }
            for c in all_cases
        ],
    )

    jan_rows = []
    for v in ("C0", "C1", "C2", "C3", "C4"):
        a, b = FOCUS["jan13_15"]
        for s, d in zip(snaps, results[v]["displays"]):
            if _ts(a) <= _ts(s["decision_time"]) <= _ts(b):
                jan_rows.append(
                    {
                        "variant": v,
                        "decision_time": _iso(s["decision_time"]),
                        "close": s["close"],
                        "display_class": DISPLAY_NAMES[d],
                        "last_lower_high": s.get("last_lower_high"),
                        "last_higher_low": s.get("last_higher_low"),
                        "events": "|".join(s.get("events") or []),
                    }
                )
    _write_csv(OUT / "jan13_15_detail.csv", jan_rows)

    pine_paths = {}
    for v in ("C0", "C1", "C2", "C3", "C4"):
        iv = collapse_intervals(snaps, results[v]["displays"])
        text = build_pine(v, iv, month=1)
        path = OUT / f"market_regime_reversal_confidence_{v.lower()}_2026_01.pine"
        path.write_text(text, encoding="utf-8")
        pine_paths[v] = str(path)

    letter = decide_letter(metrics)
    letter_text = {
        "J": "Eine Confidence-Variante reduziert False-Flips, erkennt echte Reversals deutlich früher als R2 und vermeidet die Trägheit von R4.",
        "N": "Die Zwischenzustände verbessern den Zielkonflikt nicht.",
        "U": "Keine Variante ist eindeutig fachlich überlegen.",
    }[letter]

    hashes_after = {k: _md5(Path(f"research/regime_scanner/{k}")) for k in hashes_before}
    # fix paths
    hashes_after = {
        "market_regime.py": _md5(MARKET),
        "trend_structure.py": _md5(STRUCTURE),
        "trend_state_machine.py": _md5(MACHINE),
        "trend_state_policy.py": _md5(POLICY),
        "trend_zones.py": _md5(ZONES),
    }
    assert hashes_before == hashes_after
    _write_json(OUT / "hashes_after.json", hashes_after)

    summary = {
        "decision": letter,
        "decision_text": letter_text,
        "no_variant_adopted": True,
        "variant_definitions": VARIANT_DEFS,
        "ground_truth": GROUND_TRUTH_NOTE,
        "metrics": metrics,
        "comparison": comparison,
        "pine_files": pine_paths,
        "hashes": hashes_after,
        "answers": {
            "q1_jan13_15_recovery_or_possible_without_macro_bull": {
                v: {
                    "ok": metrics[i]["jan13_15_ok_no_macro_bull"],
                    "recovery": metrics[i]["jan13_15_recovery_bars"],
                    "possible": metrics[i]["jan13_15_possible_bars"],
                    "probable": metrics[i]["jan13_15_probable_bars"],
                    "macro_bull": metrics[i]["jan13_15_macro_bull_bars"],
                }
                for i, v in enumerate(("C0", "C1", "C2", "C3", "C4"))
            },
        },
    }
    _write_json(OUT / "summary.json", summary)
    _write_json(
        OUT / "audit_metadata.json",
        {
            "audit": "market_regime_reversal_confidence_audit",
            "read_only": True,
            "baseline_c0": "R2",
            "decision": letter,
            "audit_window": {"start": AUDIT_START, "end": AUDIT_END},
        },
    )
    (OUT / "README.md").write_text(
        f"""# Macro reversal confidence audit

**Decision: {letter}** — {letter_text}

Read-only. No variant adopted. No policy wiring.

## Variants
"""
        + "\n".join(f"- **{k}**: {v}" for k, v in VARIANT_DEFS.items())
        + """

## Reproduce

```bash
PYTHONPATH=. python3 -u research/regime_scanner/market_regime_reversal_confidence_audit.py
```

## Ground truth caveats

See `summary.json` → `ground_truth`.
""",
        encoding="utf-8",
    )
    (OUT / "final_recommendation.md").write_text(
        f"# Decision {letter}\n\n{letter_text}\n\nNo variant adopted.\n",
        encoding="utf-8",
    )

    print(json.dumps({"decision": letter, "comparison": comparison}, indent=2))
    print("Wrote", OUT)


if __name__ == "__main__":
    main()
