#!/usr/bin/env python3
"""Read-only audit: structure-gated macro direction flips (R0–R4).

Baseline R0 = stability S2 semantics. R1–R4 require confirmed 4h market-structure
breaks (and optional HL/LH / multi-close / retest) before accepting a macro flip.

Does not modify market_regime.py, trend_structure.py, or any policy modules.
No variant is adopted.

Example:
  PYTHONPATH=. python3 -u research/regime_scanner/market_regime_macro_flip_structure_audit.py
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from dataclasses import dataclass
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
from research.regime_scanner.market_regime_macro_stability_audit import (
    BEAR_CONSOL,
    BEAR_TRENDING,
    BULL_CONSOL,
    BULL_TRENDING,
    POSSIBLE_REVERSAL,
    TRUE_RANGE,
    apply_s2,
    display_direction,
)
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.swings import find_confirmed_pivots
from research.regime_scanner.trend_structure import (
    MarketStructureState,
    default_trend_structure_config,
    update_market_structure,
)

OUT = Path("research/regime_scanner/results/market_regime_macro_flip_structure_audit")
MARKET = Path("research/regime_scanner/market_regime.py")
STRUCTURE = Path("research/regime_scanner/trend_structure.py")
MACHINE = Path("research/regime_scanner/trend_state_machine.py")
POLICY = Path("research/regime_scanner/trend_state_policy.py")
ZONES = Path("research/regime_scanner/trend_zones.py")

LOAD_START = "2025-12-27T00:00:00+00:00"
AUDIT_START = "2026-01-06T00:00:00+00:00"
AUDIT_END = "2026-03-16T23:59:00+00:00"

# 4h pivot width for structure (audit-local; 2+2 balances sparsity vs reactivity)
HTF_PIVOT_LEFT = 2
HTF_PIVOT_RIGHT = 2

FOCUS = {
    "jan13_15": ("2026-01-13T00:00:00+00:00", "2026-01-15T23:59:00+00:00"),
    "jan17_18": ("2026-01-17T00:00:00+00:00", "2026-01-18T23:59:00+00:00"),
    "jan27_29": ("2026-01-27T00:00:00+00:00", "2026-01-29T23:59:00+00:00"),
    "feb06_07": ("2026-02-06T00:00:00+00:00", "2026-02-07T23:59:00+00:00"),
    "march_05_06": ("2026-03-05T00:00:00+00:00", "2026-03-06T23:59:00+00:00"),
    "jan19_downtrend": ("2026-01-19T00:00:00+00:00", "2026-01-31T23:59:00+00:00"),
    "jan29_feb06": ("2026-01-29T00:00:00+00:00", "2026-02-06T23:59:00+00:00"),
}

# Display codes for Pine / timelines
MACRO_BULL = 1
MACRO_BEAR = 2
BEAR_BULL_RECOVERY = 3  # bearish_with_bullish_recovery
BULL_BEAR_PULLBACK = 4  # bullish_with_bearish_pullback
POSSIBLE_BULL_REV = 5
POSSIBLE_BEAR_REV = 6
TRUE_RANGE_D = 7

DISPLAY_NAMES = {
    MACRO_BULL: "macro_bullish",
    MACRO_BEAR: "macro_bearish",
    BEAR_BULL_RECOVERY: "bearish_with_bullish_recovery",
    BULL_BEAR_PULLBACK: "bullish_with_bearish_pullback",
    POSSIBLE_BULL_REV: "possible_bullish_reversal",
    POSSIBLE_BEAR_REV: "possible_bearish_reversal",
    TRUE_RANGE_D: "true_range",
}

VARIANT_DEFS = {
    "R0": "S2 sticky semantics mapped to macro display classes (no structure gate).",
    "R1": "Bull flip only after closed 4h close breaks last relevant LH; bear flip breaks last relevant HL.",
    "R2": "R1 + confirmed HL after bullish break / confirmed LH after bearish break.",
    "R3": "R1 + two consecutive closed 4h bars beyond the structure level.",
    "R4": "R2 + retest hold (broken level not sustainably lost on retest).",
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


def confirmed_dir_of(code: int) -> int:
    if code == MACRO_BULL:
        return 1
    if code == MACRO_BEAR:
        return -1
    if code in (BEAR_BULL_RECOVERY, POSSIBLE_BEAR_REV):
        return -1  # still under bearish macro umbrella for recovery states
    if code in (BULL_BEAR_PULLBACK, POSSIBLE_BULL_REV):
        return 1
    return 0


def macro_side(code: int) -> int:
    """Signed macro side for flip detection (+1 bull confirmed only, -1 bear confirmed only)."""
    if code == MACRO_BULL:
        return 1
    if code == MACRO_BEAR:
        return -1
    return 0


# ---------------------------------------------------------------------------
# Data rebuild
# ---------------------------------------------------------------------------


def rebuild_4h() -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    end_wall = _ts(AUDIT_END)
    raw = load_symbol_candles("APTUSDT")
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    sl = raw[(raw["timestamp"] >= _ts(LOAD_START)) & (raw["timestamp"] <= _ts("2026-03-16 23:55:00+00:00"))]
    scfg = default_regime_scanner_config()
    frame5 = compute_indicator_frame(sl, config=scfg)
    frame5["timestamp"] = pd.to_datetime(frame5["timestamp"], utc=True)
    ohlcv5 = frame5[["timestamp", "open", "high", "low", "close", "volume"]]
    agg4 = aggregate_closed_htf(ohlcv5, 240, end_wall)
    ind4 = compute_indicator_frame(agg4, config=scfg).copy()
    ind4["timestamp"] = pd.to_datetime(ind4["timestamp"], utc=True)
    ind4["decision_time"] = pd.to_datetime(agg4["decision_time"], utc=True).to_numpy()
    timeline = run_htf_regime_timeline(ind4)
    return ind4, timeline


def attach_structure(ind4: pd.DataFrame) -> list[dict[str, Any]]:
    """Causal 4h structure snapshot per closed bar (read-only use of trend_structure)."""
    pivots = find_confirmed_pivots(ind4, pivot_left=HTF_PIVOT_LEFT, pivot_right=HTF_PIVOT_RIGHT)
    state = MarketStructureState(timeframe="4h")
    cfg = default_trend_structure_config()
    out: list[dict[str, Any]] = []
    for i in range(len(ind4)):
        row = ind4.iloc[i]
        dt = _ts(row["decision_time"])
        atr = float(row["atr"]) if "atr" in ind4.columns and pd.notna(row["atr"]) else None
        state, events = update_market_structure(
            state, candle=row, pivots=pivots, decision_time=dt, atr=atr, cfg=cfg
        )
        lh = state.last_lower_high
        hl = state.last_higher_low
        sh = state.last_confirmed_swing_high
        sl = state.last_confirmed_swing_low
        ev_types = [e.event_type for e in events]
        out.append(
            {
                "decision_time": dt,
                "close": float(row["close"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "structure_bias": state.current_structure_bias,
                "last_lower_high": None if lh is None else float(lh.price),
                "last_lower_high_ts": None if lh is None else lh.confirmation_timestamp,
                "last_higher_low": None if hl is None else float(hl.price),
                "last_higher_low_ts": None if hl is None else hl.confirmation_timestamp,
                "last_swing_high": None if sh is None else float(sh.price),
                "last_swing_low": None if sl is None else float(sl.price),
                "protective_high": state.protective_high_level,
                "protective_low": state.protective_low_level,
                "active_retest_level": state.active_retest_level,
                "active_retest_direction": state.active_retest_direction,
                "events": ev_types,
                "bullish_choch": "bullish_choch" in ev_types,
                "bearish_choch": "bearish_choch" in ev_types,
                "bullish_retest_holds": "bullish_retest_holds" in ev_types,
                "bearish_retest_holds": "bearish_retest_holds" in ev_types,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Flip engines
# ---------------------------------------------------------------------------


def map_s2_to_display(s2_codes: list[int]) -> list[int]:
    """R0: map stability S2 classes onto macro flip display taxonomy."""
    out: list[int] = []
    last_side = 0
    for c in s2_codes:
        if c == BULL_TRENDING:
            out.append(MACRO_BULL)
            last_side = 1
        elif c == BEAR_TRENDING:
            out.append(MACRO_BEAR)
            last_side = -1
        elif c == BEAR_CONSOL:
            out.append(BEAR_BULL_RECOVERY)
            last_side = -1
        elif c == BULL_CONSOL:
            out.append(BULL_BEAR_PULLBACK)
            last_side = 1
        elif c == TRUE_RANGE:
            out.append(TRUE_RANGE_D)
            last_side = 0
        else:  # POSSIBLE_REVERSAL
            if last_side > 0:
                out.append(POSSIBLE_BEAR_REV)
            elif last_side < 0:
                out.append(POSSIBLE_BULL_REV)
            else:
                out.append(TRUE_RANGE_D)
    return out


@dataclass
class PendingFlip:
    target: int  # +1 bull / -1 bear
    level: float
    break_index: int | None = None
    closes_beyond: int = 0
    hl_after_break: bool = False
    lh_after_break: bool = False
    retest_held: bool = False
    broke: bool = False


def protective_for_bull_flip(snap: dict[str, Any]) -> float | None:
    """Last relevant LH for bullish structure shift."""
    for key in ("last_lower_high", "protective_high", "last_swing_high"):
        v = snap.get(key)
        if v is not None:
            return float(v)
    return None


def protective_for_bear_flip(snap: dict[str, Any]) -> float | None:
    for key in ("last_higher_low", "protective_low", "last_swing_low"):
        v = snap.get(key)
        if v is not None:
            return float(v)
    return None


def intent_dir(s2_code: int) -> int:
    return display_direction(s2_code)


def _flip_row(
    *,
    variant: str,
    index: int,
    snap: dict[str, Any],
    old_direction: int,
    new_direction: int,
    pending: PendingFlip,
    snaps: list[dict[str, Any]],
    s2_codes: list[int],
) -> dict[str, Any]:
    close = float(snap["close"])
    level = pending.level
    broke = (close > level) if new_direction > 0 else (close < level)
    dist = abs(close - level) / level * 100.0 if level else None
    # forward moves
    moves = {}
    for h, bars in ((4, 1), (8, 2), (12, 3), (24, 6)):
        j = index + bars
        if j < len(snaps):
            moves[f"max_move_after_flip_{h}h_pct"] = (float(snaps[j]["close"]) - close) / close * 100.0
        else:
            moves[f"max_move_after_flip_{h}h_pct"] = None

    # outcome vs subsequent price path (up to 6 bars / 24h)
    end_i = min(len(snaps) - 1, index + 6)
    future_closes = [float(snaps[k]["close"]) for k in range(index, end_i + 1)]
    returned_inside = False
    if new_direction > 0:
        # false if later close back below level
        returned_inside = any(c < level for c in future_closes[1:])
        progress = max(future_closes) - close
        adverse = close - min(future_closes)
    else:
        returned_inside = any(c > level for c in future_closes[1:])
        progress = close - min(future_closes)
        adverse = max(future_closes) - close

    # classify outcome
    if returned_inside and adverse >= progress:
        outcome = "false_flip"
        true_rev = False
        ctr = True
        false_flip = True
        delayed_valid = False
    elif returned_inside:
        outcome = "countertrend_recovery"
        true_rev = False
        ctr = True
        false_flip = False
        delayed_valid = False
    elif progress > adverse * 1.2:
        outcome = "true_reversal"
        true_rev = True
        ctr = False
        false_flip = False
        delayed_valid = False
    else:
        outcome = "unclear"
        true_rev = False
        ctr = False
        false_flip = False
        delayed_valid = False

    conf_bars = 0 if pending.break_index is None else max(0, index - pending.break_index + 1)

    return {
        "variant": variant,
        "flip_timestamp_utc": _iso(snap["decision_time"]),
        "flip_index": index,
        "old_direction": {1: "bullish", -1: "bearish", 0: "neutral"}[old_direction],
        "proposed_new_direction": {1: "bullish", -1: "bearish"}[new_direction],
        "last_relevant_swing_high": snap.get("last_swing_high"),
        "last_relevant_swing_low": snap.get("last_swing_low"),
        "last_lower_high": snap.get("last_lower_high"),
        "last_higher_low": snap.get("last_higher_low"),
        "protective_level": level,
        "close_broke_level": bool(broke or pending.broke),
        "break_distance_pct": dist,
        "confirmation_bars": conf_bars,
        "higher_low_after_break": pending.hl_after_break,
        "lower_high_after_break": pending.lh_after_break,
        "retest_held": pending.retest_held,
        "price_returned_inside_old_structure": returned_inside,
        **moves,
        "final_outcome": outcome,
        "true_reversal": true_rev,
        "countertrend_recovery": ctr,
        "false_flip": false_flip,
        "delayed_but_valid": delayed_valid,
        "structure_bias_at_flip": snap.get("structure_bias"),
        "s2_code_at_flip": s2_codes[index],
        "close": close,
    }


# ---------------------------------------------------------------------------
# Simpler R1-R4 engine rewrite for clarity/correctness
# ---------------------------------------------------------------------------


def run_gated_variant(
    *,
    variant: str,
    s2_codes: list[int],
    snaps: list[dict[str, Any]],
) -> tuple[list[int], list[dict[str, Any]]]:
    """Cleaner structure-gated macro state machine."""
    require_cont = variant in {"R2", "R4"}  # HL/LH after break
    require_two = variant == "R3"
    require_retest = variant == "R4"

    confirmed = 0
    pending: PendingFlip | None = None
    codes: list[int] = []
    flips: list[dict[str, Any]] = []
    flat_streak = 0

    def try_complete(i: int, snap: dict[str, Any]) -> bool:
        nonlocal confirmed, pending
        if pending is None:
            return False
        close = float(snap["close"])
        level = pending.level
        beyond = close > level if pending.target > 0 else close < level
        if beyond:
            if not pending.broke:
                pending.broke = True
                pending.break_index = i
            pending.closes_beyond += 1
        else:
            if require_two:
                pending.closes_beyond = 0

        if pending.target > 0:
            if pending.broke and pending.break_index is not None:
                br_t = snaps[pending.break_index]["decision_time"]
                if "higher_low" in snap.get("events", []) and _ts(snap["decision_time"]) > _ts(br_t):
                    pending.hl_after_break = True
                hl_ts = snap.get("last_higher_low_ts")
                if hl_ts is not None and _ts(hl_ts) > _ts(br_t):
                    pending.hl_after_break = True
                # Retest hold only on a later bar (not the break/choch bar itself)
                if _ts(snap["decision_time"]) > _ts(br_t) and snap.get("bullish_retest_holds"):
                    pending.retest_held = True
                # Synthetic retest: after break, a close returns near/below level then later closes above
                if (
                    _ts(snap["decision_time"]) > _ts(br_t)
                    and pending.closes_beyond >= 2
                    and beyond
                    and any(
                        float(snaps[k]["close"]) <= level
                        for k in range(pending.break_index + 1, i)
                    )
                ):
                    pending.retest_held = True
            if snap.get("bullish_choch"):
                pending.broke = True
                pending.break_index = pending.break_index if pending.break_index is not None else i
        else:
            if pending.broke and pending.break_index is not None:
                br_t = snaps[pending.break_index]["decision_time"]
                if "lower_high" in snap.get("events", []) and _ts(snap["decision_time"]) > _ts(br_t):
                    pending.lh_after_break = True
                lh_ts = snap.get("last_lower_high_ts")
                if lh_ts is not None and _ts(lh_ts) > _ts(br_t):
                    pending.lh_after_break = True
                if _ts(snap["decision_time"]) > _ts(br_t) and snap.get("bearish_retest_holds"):
                    pending.retest_held = True
                if (
                    _ts(snap["decision_time"]) > _ts(br_t)
                    and pending.closes_beyond >= 2
                    and beyond
                    and any(
                        float(snaps[k]["close"]) >= level
                        for k in range(pending.break_index + 1, i)
                    )
                ):
                    pending.retest_held = True
            if snap.get("bearish_choch"):
                pending.broke = True
                pending.break_index = pending.break_index if pending.break_index is not None else i

        ok = bool(pending.broke)
        if require_two:
            ok = ok and pending.closes_beyond >= 2
        if require_cont:
            ok = ok and (pending.hl_after_break if pending.target > 0 else pending.lh_after_break)
        if require_retest:
            ok = ok and pending.retest_held
        # R1: break alone on this bar
        if variant == "R1":
            ok = pending.broke and beyond

        if ok:
            old = confirmed
            confirmed = pending.target
            flips.append(
                _flip_row(
                    variant=variant,
                    index=i,
                    snap=snap,
                    old_direction=old,
                    new_direction=pending.target,
                    pending=pending,
                    snaps=snaps,
                    s2_codes=s2_codes,
                )
            )
            pending = None
            return True
        return False

    def start_pending(target: int, snap: dict[str, Any], i: int) -> None:
        nonlocal pending
        level = protective_for_bull_flip(snap) if target > 0 else protective_for_bear_flip(snap)
        if level is None:
            level = float(snap["high"] if target > 0 else snap["low"])
        close = float(snap["close"])
        beyond = close > level if target > 0 else close < level
        pending = PendingFlip(
            target=target,
            level=float(level),
            break_index=i if beyond else None,
            broke=beyond,
            closes_beyond=1 if beyond else 0,
        )

    for i, (s2, snap) in enumerate(zip(s2_codes, snaps)):
        intent = intent_dir(s2)
        completed = try_complete(i, snap)

        if confirmed == 0:
            if intent > 0:
                if pending is None or pending.target != 1:
                    start_pending(1, snap, i)
                completed = try_complete(i, snap) or completed
                if confirmed == 1:
                    codes.append(MACRO_BULL)
                else:
                    codes.append(POSSIBLE_BULL_REV if (pending and pending.broke) else BEAR_BULL_RECOVERY)
                    if codes[-1] == BEAR_BULL_RECOVERY and confirmed == 0:
                        codes[-1] = POSSIBLE_BULL_REV
            elif intent < 0:
                if pending is None or pending.target != -1:
                    start_pending(-1, snap, i)
                completed = try_complete(i, snap) or completed
                if confirmed == -1:
                    codes.append(MACRO_BEAR)
                else:
                    codes.append(POSSIBLE_BEAR_REV)
            else:
                pending = None
                flat_streak += 1
                codes.append(TRUE_RANGE_D if s2 == TRUE_RANGE else POSSIBLE_BULL_REV if s2 == POSSIBLE_REVERSAL else TRUE_RANGE_D)
            continue

        flat_streak = 0
        if confirmed > 0:
            if intent > 0 or s2 == BULL_TRENDING:
                pending = None if (pending and pending.target < 0 and intent > 0) else pending
                if intent > 0:
                    pending = None
                codes.append(MACRO_BULL)
            elif s2 == BULL_CONSOL or (intent == 0 and s2 != TRUE_RANGE and s2 != BEAR_TRENDING):
                codes.append(BULL_BEAR_PULLBACK)
            elif s2 == TRUE_RANGE:
                # allow clear after flat streak like S2 (4 bars) — approximate with immediate true range
                codes.append(TRUE_RANGE_D)
                confirmed = 0
                pending = None
            else:
                # opposite intent
                if pending is None or pending.target != -1:
                    start_pending(-1, snap, i)
                completed = try_complete(i, snap) or completed
                if confirmed < 0:
                    codes.append(MACRO_BEAR)
                else:
                    codes.append(
                        POSSIBLE_BEAR_REV if pending and pending.broke else BULL_BEAR_PULLBACK
                    )
        else:  # confirmed bear
            if intent < 0 or s2 == BEAR_TRENDING:
                if intent < 0:
                    pending = None
                codes.append(MACRO_BEAR)
            elif s2 == BEAR_CONSOL or (intent == 0 and s2 != TRUE_RANGE and s2 != BULL_TRENDING):
                codes.append(BEAR_BULL_RECOVERY)
            elif s2 == TRUE_RANGE:
                codes.append(TRUE_RANGE_D)
                confirmed = 0
                pending = None
            else:
                if pending is None or pending.target != 1:
                    start_pending(1, snap, i)
                completed = try_complete(i, snap) or completed
                if confirmed > 0:
                    codes.append(MACRO_BULL)
                else:
                    codes.append(
                        POSSIBLE_BULL_REV if pending and pending.broke else BEAR_BULL_RECOVERY
                    )

    return codes, flips


def extract_flips_from_codes(
    variant: str,
    codes: list[int],
    snaps: list[dict[str, Any]],
    s2_codes: list[int],
) -> list[dict[str, Any]]:
    """For R0: derive flip rows whenever macro_side changes."""
    flips: list[dict[str, Any]] = []
    last = 0
    for i, c in enumerate(codes):
        side = macro_side(c)
        if side != 0 and side != last:
            level = protective_for_bull_flip(snaps[i]) if side > 0 else protective_for_bear_flip(snaps[i])
            if level is None:
                level = float(snaps[i]["close"])
            pend = PendingFlip(target=side, level=float(level), break_index=i, broke=True, closes_beyond=1)
            # crude HL/LH flags from snap
            if side > 0:
                pend.hl_after_break = "higher_low" in snaps[i].get("events", [])
            else:
                pend.lh_after_break = "lower_high" in snaps[i].get("events", [])
            pend.retest_held = bool(
                snaps[i].get("bullish_retest_holds") or snaps[i].get("bearish_retest_holds")
            )
            flips.append(
                _flip_row(
                    variant=variant,
                    index=i,
                    snap=snaps[i],
                    old_direction=last,
                    new_direction=side,
                    pending=pend,
                    snaps=snaps,
                    s2_codes=s2_codes,
                )
            )
            last = side
        elif side != 0:
            last = side
        # keep last confirmed side through recovery states
        elif c in (BEAR_BULL_RECOVERY, POSSIBLE_BULL_REV) and last == -1:
            pass
        elif c in (BULL_BEAR_PULLBACK, POSSIBLE_BEAR_REV) and last == 1:
            pass
        elif c == TRUE_RANGE_D:
            last = 0
    return flips


def annotate_delayed(flips_by_variant: dict[str, list[dict[str, Any]]]) -> None:
    """Mark delayed_but_valid when R0 false/recovery but later variant true_reversal same side."""
    r0 = flips_by_variant.get("R0", [])
    for v, flips in flips_by_variant.items():
        if v == "R0":
            continue
        for fr in flips:
            if not fr.get("true_reversal"):
                continue
            # find earlier R0 flip same new direction within 10 bars before
            t = _ts(fr["flip_timestamp_utc"])
            for r0f in r0:
                if r0f["proposed_new_direction"] != fr["proposed_new_direction"]:
                    continue
                t0 = _ts(r0f["flip_timestamp_utc"])
                if t0 < t and (t - t0) <= pd.Timedelta(hours=48):
                    if r0f.get("false_flip") or r0f.get("countertrend_recovery"):
                        fr["delayed_but_valid"] = True


# ---------------------------------------------------------------------------
# Metrics / Pine
# ---------------------------------------------------------------------------


def collapse_intervals(snaps: list[dict[str, Any]], codes: list[int]) -> list[dict[str, Any]]:
    if not codes:
        return []
    out: list[dict[str, Any]] = []
    start = 0
    for i in range(1, len(codes) + 1):
        if i < len(codes) and codes[i] == codes[start]:
            continue
        chunk = snaps[start:i]
        code = codes[start]
        end_ts = (
            snaps[i]["decision_time"]
            if i < len(snaps)
            else _ts(chunk[-1]["decision_time"]) + pd.Timedelta(hours=4)
        )
        out.append(
            {
                "start_utc": _iso(chunk[0]["decision_time"]),
                "end_utc": _iso(end_ts),
                "display_code": code,
                "display_class": DISPLAY_NAMES[code],
                "n_4h_bars": len(chunk),
            }
        )
        start = i
    return out


def variant_metrics(variant: str, flips: list[dict[str, Any]], codes: list[int]) -> dict[str, Any]:
    n_false = sum(1 for f in flips if f.get("false_flip"))
    n_ctr = sum(1 for f in flips if f.get("countertrend_recovery"))
    n_true = sum(1 for f in flips if f.get("true_reversal"))
    n_held = sum(1 for c in codes if c in (BEAR_BULL_RECOVERY, BULL_BEAR_PULLBACK))
    delays = []
    # delay vs R0 filled later
    return {
        "variant": variant,
        "definition": VARIANT_DEFS[variant],
        "n_flips": len(flips),
        "n_false_flips": n_false,
        "n_countertrend_recovery": n_ctr,
        "n_true_reversal": n_true,
        "n_unclear": sum(1 for f in flips if f.get("final_outcome") == "unclear"),
        "recovery_pullback_bars": n_held,
        "possible_reversal_bars": sum(1 for c in codes if c in (POSSIBLE_BULL_REV, POSSIBLE_BEAR_REV)),
        "macro_bull_bars": sum(1 for c in codes if c == MACRO_BULL),
        "macro_bear_bars": sum(1 for c in codes if c == MACRO_BEAR),
        "class_counts": dict(Counter(DISPLAY_NAMES[c] for c in codes)),
    }


def delay_stats(r0_flips: list[dict], var_flips: list[dict]) -> dict[str, Any]:
    """For each R0 true_reversal, delay until variant confirms same direction (if ever)."""
    delays: list[float] = []
    missed = 0
    for r0 in r0_flips:
        if not r0.get("true_reversal") and r0.get("final_outcome") not in {"true_reversal"}:
            # also measure delay for R0 flips that were false — skip
            continue
        # Better: for R0 flips that eventually look like real regime change in long horizon
        pass
    # Alternate: pair by direction sequence
    for r0 in r0_flips:
        t0 = _ts(r0["flip_timestamp_utc"])
        direction = r0["proposed_new_direction"]
        match = None
        for vf in var_flips:
            if vf["proposed_new_direction"] != direction:
                continue
            tv = _ts(vf["flip_timestamp_utc"])
            if tv >= t0:
                match = tv
                break
        if match is None:
            if r0.get("true_reversal"):
                missed += 1
        else:
            delays.append((match - t0).total_seconds() / 3600.0)
    return {
        "n_paired_delays": len(delays),
        "avg_delay_hours": float(sum(delays) / len(delays)) if delays else None,
        "max_delay_hours": float(max(delays)) if delays else None,
        "missed_r0_true_reversals": missed,
    }


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

    title = f"Macro Flip Structure {variant} 2026-{month:02d}"
    return f"""//@version=6
indicator(
     "{pine_escape(title)}",
     overlay = true,
     max_labels_count = 500,
     max_lines_count = 100
)

// {variant}: {pine_escape(VARIANT_DEFS[variant])}
// Codes: 1 macro_bull 2 macro_bear 3 bear+bull_recovery 4 bull+bear_pullback
//        5 possible_bull_rev 6 possible_bear_rev 7 true_range
// UTC: start=decision_time, end=exclusive next run.

showMacroBackground = input.bool(true, "Show macro background")
showLabels = input.bool(false, "Show labels")
macroTransparency = input.int(85, "Macro transparency", minval = 70, maxval = 95)

f_ts(y, m, d, h, mi) =>
    timestamp("UTC", y, m, d, h, mi)

var int[] macroStarts = array.new_int()
var int[] macroEnds = array.new_int()
var int[] macroTypes = array.new_int()

{chr(10).join(helpers)}

if barstate.isfirst
{chr(10).join(calls)}

int activeType = 0
bool macroStartBar = false
if array.size(macroStarts) > 0
    for i = 0 to array.size(macroStarts) - 1
        int ms = array.get(macroStarts, i)
        int me = array.get(macroEnds, i)
        if time_close >= ms and time_close < me
            activeType := array.get(macroTypes, i)
        if time_close == ms
            macroStartBar := true
            activeType := array.get(macroTypes, i)

bgcolor(
     showMacroBackground ?
         (activeType == 1 ? color.new(color.green, macroTransparency) :
          activeType == 2 ? color.new(color.red, macroTransparency) :
          activeType == 3 ? color.new(#c5e1a5, math.min(macroTransparency + 3, 95)) :
          activeType == 4 ? color.new(#ffcc80, math.min(macroTransparency + 3, 95)) :
          activeType == 5 ? color.new(color.yellow, math.min(macroTransparency + 2, 95)) :
          activeType == 6 ? color.new(#ffd54f, math.min(macroTransparency + 2, 95)) :
          activeType == 7 ? color.new(color.gray, math.min(macroTransparency + 4, 95)) :
          na) :
     na
)

f_label() =>
    activeType == 1 ? "MACRO BULL" :
     activeType == 2 ? "MACRO BEAR" :
     activeType == 3 ? "BEAR+BULL REC" :
     activeType == 4 ? "BULL+BEAR PB" :
     activeType == 5 ? "POSS BULL REV" :
     activeType == 6 ? "POSS BEAR REV" :
     activeType == 7 ? "TRUE RANGE" :
     ""

if showLabels and macroStartBar and activeType != 0
    label.new(bar_index, high, f_label(), style = label.style_label_down, color = color.new(color.black, 40), textcolor = color.white, size = size.tiny)

// EOF
"""


def focus_slice(snaps, codes, start, end):
    a, b = _ts(start), _ts(end)
    rows = []
    for s, c in zip(snaps, codes):
        if a <= _ts(s["decision_time"]) <= b:
            rows.append(
                {
                    "decision_time": _iso(s["decision_time"]),
                    "close": s["close"],
                    "display_class": DISPLAY_NAMES[c],
                    "display_code": c,
                    "structure_bias": s.get("structure_bias"),
                    "last_lower_high": s.get("last_lower_high"),
                    "last_higher_low": s.get("last_higher_low"),
                    "protective_high": s.get("protective_high"),
                    "protective_low": s.get("protective_low"),
                    "events": "|".join(s.get("events") or []),
                }
            )
    return rows


def decide_letter(metrics: list[dict[str, Any]], jan_detail: dict[str, Any]) -> str:
    """J / N / U decision."""
    r0 = next(m for m in metrics if m["variant"] == "R0")
    gated = [m for m in metrics if m["variant"] != "R0"]
    jan_clean = [m for m in gated if not jan_detail[m["variant"]]["marks_macro_bull_jan13_15"]]
    faster_fewer_false = [
        m
        for m in gated
        if m["n_false_flips"] < r0["n_false_flips"] and (m.get("avg_delay_hours") or 0) <= 36
    ]
    slow_but_clean = [
        m
        for m in jan_clean
        if (m.get("avg_delay_hours") or 0) > 36 or (m.get("missed_r0_true_reversals") or 0) >= 2
    ]
    fast_and_clean = [
        m
        for m in jan_clean
        if (m.get("avg_delay_hours") or 0) <= 36 and (m.get("missed_r0_true_reversals") or 0) <= 1
    ]

    if fast_and_clean and fast_and_clean[0]["n_false_flips"] < r0["n_false_flips"]:
        return "J"
    # Reference fixed only by sluggish rules, while faster rules still false-flip Jan13-15
    if slow_but_clean and not fast_and_clean:
        if faster_fewer_false:
            return "U"
        return "N"
    if not jan_clean:
        return "U"
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

    print("Rebuilding 4h M2 + structure…")
    ind4, full_tl = rebuild_4h()
    struct_all = attach_structure(ind4)

    # Align timeline/structure on decision_time, filter audit window
    by_dt = {_ts(r["decision_time"]): r for r in full_tl}
    snaps: list[dict[str, Any]] = []
    tl_audit: list[dict[str, Any]] = []
    for s in struct_all:
        dt = _ts(s["decision_time"])
        if dt < _ts(AUDIT_START) or dt > _ts(AUDIT_END):
            continue
        if dt not in by_dt:
            continue
        row = {**s, "regime": by_dt[dt]["regime"]}
        snaps.append(row)
        tl_audit.append(by_dt[dt])

    s2_codes = apply_s2(tl_audit)
    assert len(s2_codes) == len(snaps)
    print(f"audit 4h bars: {len(snaps)}")

    codes_by: dict[str, list[int]] = {}
    flips_by: dict[str, list[dict[str, Any]]] = {}

    # R0
    codes_by["R0"] = map_s2_to_display(s2_codes)
    flips_by["R0"] = extract_flips_from_codes("R0", codes_by["R0"], snaps, s2_codes)

    for v in ("R1", "R2", "R3", "R4"):
        codes, flips = run_gated_variant(variant=v, s2_codes=s2_codes, snaps=snaps)
        codes_by[v] = codes
        flips_by[v] = flips
        print(f"{v}: flips={len(flips)} false={sum(1 for f in flips if f.get('false_flip'))}")

    annotate_delayed(flips_by)

    all_flips: list[dict[str, Any]] = []
    for v in ("R0", "R1", "R2", "R3", "R4"):
        all_flips.extend(flips_by[v])
    _write_csv(OUT / "all_macro_flips.csv", all_flips)
    _write_csv(
        OUT / "bullish_flip_cases.csv",
        [f for f in all_flips if f["proposed_new_direction"] == "bullish"],
    )
    _write_csv(
        OUT / "bearish_flip_cases.csv",
        [f for f in all_flips if f["proposed_new_direction"] == "bearish"],
    )
    _write_csv(OUT / "false_flip_cases.csv", [f for f in all_flips if f.get("false_flip")])
    _write_csv(OUT / "valid_reversal_cases.csv", [f for f in all_flips if f.get("true_reversal")])
    _write_csv(
        OUT / "countertrend_recovery_cases.csv",
        [f for f in all_flips if f.get("countertrend_recovery")],
    )

    metrics = []
    comparison = []
    intervals_by = {}
    for v in ("R0", "R1", "R2", "R3", "R4"):
        m = variant_metrics(v, flips_by[v], codes_by[v])
        dstat = delay_stats(flips_by["R0"], flips_by[v])
        m.update(dstat)
        metrics.append(m)
        comparison.append(
            {
                "variant": v,
                "n_flips": m["n_flips"],
                "n_false_flips": m["n_false_flips"],
                "n_countertrend_recovery": m["n_countertrend_recovery"],
                "n_true_reversal": m["n_true_reversal"],
                "recovery_pullback_bars": m["recovery_pullback_bars"],
                "avg_delay_hours": m.get("avg_delay_hours"),
                "max_delay_hours": m.get("max_delay_hours"),
                "missed_r0_true_reversals": m.get("missed_r0_true_reversals"),
            }
        )
        intervals_by[v] = collapse_intervals(snaps, codes_by[v])
        _write_csv(
            OUT / f"timeline_{v.lower()}.csv",
            [
                {
                    "variant": v,
                    "decision_time": _iso(s["decision_time"]),
                    "regime": s["regime"],
                    "display_class": DISPLAY_NAMES[c],
                    "display_code": c,
                    "close": s["close"],
                    "last_lower_high": s.get("last_lower_high"),
                    "last_higher_low": s.get("last_higher_low"),
                }
                for s, c in zip(snaps, codes_by[v])
            ],
        )

    _write_csv(OUT / "variant_comparison.csv", comparison)

    # Jan 13-15 detail
    jan_rows = []
    jan_flags = {}
    for v in ("R0", "R1", "R2", "R3", "R4"):
        rows = focus_slice(snaps, codes_by[v], *FOCUS["jan13_15"])
        for r in rows:
            jan_rows.append({"variant": v, **r})
        jan_flags[v] = {
            "marks_macro_bull_jan13_15": any(r["display_code"] == MACRO_BULL for r in rows),
            "marks_recovery_or_possible": any(
                r["display_code"] in (BEAR_BULL_RECOVERY, POSSIBLE_BULL_REV) for r in rows
            ),
            "n_macro_bull_bars": sum(1 for r in rows if r["display_code"] == MACRO_BULL),
        }
    _write_csv(OUT / "jan13_15_detail.csv", jan_rows)

    chart_review = []
    for name, window in FOCUS.items():
        for v in ("R0", "R1", "R2", "R3", "R4"):
            rows = focus_slice(snaps, codes_by[v], *window)
            chart_review.append(
                {
                    "window": name,
                    "variant": v,
                    "n_bars": len(rows),
                    "macro_bull_bars": sum(1 for r in rows if r["display_code"] == MACRO_BULL),
                    "macro_bear_bars": sum(1 for r in rows if r["display_code"] == MACRO_BEAR),
                    "recovery_bars": sum(1 for r in rows if r["display_code"] == BEAR_BULL_RECOVERY),
                    "pullback_bars": sum(1 for r in rows if r["display_code"] == BULL_BEAR_PULLBACK),
                    "possible_rev_bars": sum(
                        1 for r in rows if r["display_code"] in (POSSIBLE_BULL_REV, POSSIBLE_BEAR_REV)
                    ),
                    "classes": dict(Counter(r["display_class"] for r in rows)),
                }
            )
    _write_csv(OUT / "chart_review_flip_variants.csv", chart_review)

    pine_paths = {}
    for v in ("R0", "R1", "R2", "R3", "R4"):
        text = build_pine(v, intervals_by[v], month=1)
        path = OUT / f"market_regime_macro_flip_{v.lower()}_2026_01.pine"
        path.write_text(text, encoding="utf-8")
        pine_paths[v] = str(path)

    letter = decide_letter(metrics, jan_flags)
    letter_text = {
        "J": "Strukturgebundene Flip-Regel verhindert falsche Gegenrichtungen, ohne echte Reversals relevant zu spät zu erkennen.",
        "N": "Strukturgebundene Flip-Regel wird zu träge.",
        "U": "Keine Variante ist eindeutig überlegen.",
    }[letter]

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
        "baseline": "R0 = S2 sticky",
        "variant_definitions": VARIANT_DEFS,
        "htf_pivots": {"left": HTF_PIVOT_LEFT, "right": HTF_PIVOT_RIGHT},
        "n_4h_bars": len(snaps),
        "metrics": metrics,
        "comparison": comparison,
        "jan13_15_flags": jan_flags,
        "pine_files": pine_paths,
        "hashes": hashes_after,
        "market_regime_unchanged": True,
    }
    _write_json(OUT / "summary.json", summary)
    _write_json(
        OUT / "audit_metadata.json",
        {
            "audit": "market_regime_macro_flip_structure_audit",
            "read_only": True,
            "audit_window": {"start": AUDIT_START, "end": AUDIT_END},
            "symbol": "APTUSDT",
            "macro_tf": "4h_closed",
            "local_baseline": "S2",
            "structure_module": "trend_structure.update_market_structure (read-only)",
            "decision": letter,
        },
    )
    (OUT / "final_recommendation.md").write_text(
        f"# Macro flip structure audit\n\n**Decision: {letter}** — {letter_text}\n\n"
        "No variant adopted. Compare January Pine R0 vs R1–R4 on APTUSDT.\n",
        encoding="utf-8",
    )

    print(json.dumps({"decision": letter, "jan13_15": jan_flags, "comparison": comparison}, indent=2))
    print("Wrote", OUT)


if __name__ == "__main__":
    main()
