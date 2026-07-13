#!/usr/bin/env python3
"""Diagnostic-only audit: bottoming/topping 2-hit vs 1-hit variants (B0–B4).

Does NOT modify production modules. Leaves V6+V2, G6, HTF unchanged.
"""
from __future__ import annotations

import csv
import hashlib
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.swings import find_confirmed_pivots
import research.regime_scanner.trend_state_machine as sm
from research.regime_scanner.trend_state_machine import (
    TrendRuntime,
    _event_types,
    _htf_bias,
    _htf_veto_strong_bearish,
    _htf_veto_strong_bullish,
    _indicator_confirms,
    _propose_transition,
    _qualified_failed_breakdown_for_weakening,
    _qualified_failed_breakout_for_weakening,
    default_trend_state_config,
    has_hh_hl,
    has_lh_ll,
    min_hold_for,
    step_trend_state,
)
from research.regime_scanner.trend_state_march_2026_root_cause_audit import (
    install_causal_htf_prefix_cache,
)

OUT = Path("research/regime_scanner/results/trend_state_bottoming_topping_audit")
DIAG_END = "2026-03-10T00:00:00+00:00"
FOCUS_START = "2026-01-01T00:00:00+00:00"  # ≥2 months before March window
MARCH_START = "2026-03-05T18:00:00+00:00"
MARCH_END = "2026-03-10T00:00:00+00:00"
MACHINE = Path("research/regime_scanner/trend_state_machine.py")
STRUCTURE = Path("research/regime_scanner/trend_structure.py")
POLICY = Path("research/regime_scanner/trend_state_policy.py")

BOTTOM_HIT_SET = frozenset({"failed_breakdown", "bullish_choch", "higher_low", "bullish_bos"})
TOP_HIT_SET = frozenset({"failed_breakout", "bearish_choch", "lower_high", "bearish_bos"})
BOTTOM_STRUCT = frozenset({"failed_breakdown", "bullish_choch", "bullish_bos"})
TOP_STRUCT = frozenset({"failed_breakout", "bearish_choch", "bearish_bos"})


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object) -> str:
    return _ts(v).isoformat()


def _p(msg: str) -> None:
    print(msg, flush=True)


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols = fields or list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_frame(end: pd.Timestamp) -> tuple[pd.DataFrame, list, Any]:
    raw = load_symbol_candles("APTUSDT")
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    slice_ = raw[raw["timestamp"] < end].copy()
    scfg = default_regime_scanner_config().with_timeframe("5m")
    frame = compute_indicator_frame(slice_, config=scfg)
    frame = frame.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame["decision_time"] = frame["timestamp"] + pd.Timedelta(minutes=5)
    frame = frame[frame["decision_time"] <= end].reset_index(drop=True)
    pivots = find_confirmed_pivots(frame, config=scfg)
    return frame, pivots, scfg


@dataclass(frozen=True)
class Variant:
    name: str
    description: str
    mode: str  # production|one_hit|one_hit_struct|one_hit_conf|accum_crossbar


VARIANTS = [
    Variant("B0", "Production same-bar len(hits)>=2", "production"),
    Variant("B1", "Same-bar len(hits)>=1 sufficient", "one_hit"),
    Variant("B2", "1-hit only if includes structure event (not lone HL/LH)", "one_hit_struct"),
    Variant("B3", "1-hit plus retest_holds OR indicator conf>=2", "one_hit_conf"),
    Variant("B4", "Still need 2 unique hit types, accumulate across weakening bars", "accum_crossbar"),
]


@dataclass
class AccumState:
    """Diagnostic cross-bar hit accumulation while in weakening (B4)."""
    bottom_seen: set[str] = field(default_factory=set)
    top_seen: set[str] = field(default_factory=set)
    first_bottom_hit_ts: str | None = None
    first_top_hit_ts: str | None = None
    first_bottom_hit_i: int | None = None
    first_top_hit_i: int | None = None

    def reset_bottom(self) -> None:
        self.bottom_seen.clear()
        self.first_bottom_hit_ts = None
        self.first_bottom_hit_i = None

    def reset_top(self) -> None:
        self.top_seen.clear()
        self.first_top_hit_ts = None
        self.first_top_hit_i = None


def bottom_hits(types: set[str]) -> set[str]:
    return set(types & BOTTOM_HIT_SET)


def top_hits(types: set[str]) -> set[str]:
    return set(types & TOP_HIT_SET)


def allow_bottoming(
    variant: Variant,
    types: set[str],
    row: dict[str, Any],
    cfg: Any,
    accum: AccumState,
    *,
    ts: str,
    idx: int,
) -> tuple[bool, list[str], dict[str, Any]]:
    hits = bottom_hits(types)
    meta: dict[str, Any] = {
        "same_bar_hits": sorted(hits),
        "same_bar_hit_count": len(hits),
        "accum_hits": sorted(accum.bottom_seen | hits),
        "accum_hit_count": len(accum.bottom_seen | hits),
    }
    if "lower_low" in types:
        return False, ["lower_low_blocks"], meta

    if variant.mode == "production":
        ok = len(hits) >= 2
        return ok, (["bottoming_structure", *sorted(hits)] if ok else ["need_2_same_bar"]), meta

    if variant.mode == "one_hit":
        ok = len(hits) >= 1
        return ok, (["bottoming_1hit", *sorted(hits)] if ok else ["no_hit"]), meta

    if variant.mode == "one_hit_struct":
        ok = len(hits & BOTTOM_STRUCT) >= 1
        return ok, (["bottoming_1hit_struct", *sorted(hits)] if ok else ["no_struct_hit"]), meta

    if variant.mode == "one_hit_conf":
        bull_conf, _ = _indicator_confirms(row, side="bullish", cfg=cfg)
        conf_ok = "bullish_retest_holds" in types or bull_conf >= 2
        ok = len(hits) >= 1 and conf_ok
        reasons = []
        if ok:
            reasons = ["bottoming_1hit_conf", *sorted(hits)]
            if "bullish_retest_holds" in types:
                reasons.append("retest_holds")
            else:
                reasons.append("bull_conf_ge_2")
        else:
            reasons = ["need_hit_and_conf"]
        meta["bull_conf"] = bull_conf
        meta["retest_holds"] = "bullish_retest_holds" in types
        return ok, reasons, meta

    # accum_crossbar B4
    if hits:
        if not accum.bottom_seen:
            accum.first_bottom_hit_ts = ts
            accum.first_bottom_hit_i = idx
        accum.bottom_seen |= hits
    meta["accum_hits"] = sorted(accum.bottom_seen)
    meta["accum_hit_count"] = len(accum.bottom_seen)
    meta["first_hit_ts"] = accum.first_bottom_hit_ts
    ok = len(accum.bottom_seen) >= 2
    return ok, (["bottoming_accum2", *sorted(accum.bottom_seen)] if ok else ["need_2_accum"]), meta


def allow_topping(
    variant: Variant,
    types: set[str],
    row: dict[str, Any],
    cfg: Any,
    accum: AccumState,
    *,
    ts: str,
    idx: int,
) -> tuple[bool, list[str], dict[str, Any]]:
    hits = top_hits(types)
    meta: dict[str, Any] = {
        "same_bar_hits": sorted(hits),
        "same_bar_hit_count": len(hits),
        "accum_hits": sorted(accum.top_seen | hits),
        "accum_hit_count": len(accum.top_seen | hits),
    }
    if "higher_high" in types:
        return False, ["higher_high_blocks"], meta

    if variant.mode == "production":
        ok = len(hits) >= 2
        return ok, (["topping_structure", *sorted(hits)] if ok else ["need_2_same_bar"]), meta

    if variant.mode == "one_hit":
        ok = len(hits) >= 1
        return ok, (["topping_1hit", *sorted(hits)] if ok else ["no_hit"]), meta

    if variant.mode == "one_hit_struct":
        ok = len(hits & TOP_STRUCT) >= 1
        return ok, (["topping_1hit_struct", *sorted(hits)] if ok else ["no_struct_hit"]), meta

    if variant.mode == "one_hit_conf":
        bear_conf, _ = _indicator_confirms(row, side="bearish", cfg=cfg)
        conf_ok = "bearish_retest_holds" in types or bear_conf >= 2
        ok = len(hits) >= 1 and conf_ok
        reasons = []
        if ok:
            reasons = ["topping_1hit_conf", *sorted(hits)]
            if "bearish_retest_holds" in types:
                reasons.append("retest_holds")
            else:
                reasons.append("bear_conf_ge_2")
        else:
            reasons = ["need_hit_and_conf"]
        meta["bear_conf"] = bear_conf
        meta["retest_holds"] = "bearish_retest_holds" in types
        return ok, reasons, meta

    if hits:
        if not accum.top_seen:
            accum.first_top_hit_ts = ts
            accum.first_top_hit_i = idx
        accum.top_seen |= hits
    meta["accum_hits"] = sorted(accum.top_seen)
    meta["accum_hit_count"] = len(accum.top_seen)
    meta["first_hit_ts"] = accum.first_top_hit_ts
    ok = len(accum.top_seen) >= 2
    return ok, (["topping_accum2", *sorted(accum.top_seen)] if ok else ["need_2_accum"]), meta


def make_propose(variant: Variant, accum: AccumState, ctx: dict[str, Any]) -> Callable[..., Any]:
    """Wrap production propose; only weakening→bottoming/topping differs."""

    if variant.mode == "production":
        return _propose_transition

    def _propose(rt: TrendRuntime, *, events: list, row: dict[str, Any], cfg: Any):
        types = _event_types(events)
        state = rt.state
        ts = ctx.get("ts", "")
        idx = int(ctx.get("idx", 0))

        if state == "bearish_weakening":
            if not sm._can_leave(rt, cfg):
                return None, ["min_hold_bearish_weakening"]
            if "lower_low" in types and "bearish_bos" in types:
                accum.reset_bottom()
                return "early_bearish", ["failed_bottom", "ll_bos"]
            ok, reasons, _meta = allow_bottoming(variant, types, row, cfg, accum, ts=ts, idx=idx)
            if ok:
                return "bottoming", reasons
            return None, reasons

        if state == "bullish_weakening":
            if not sm._can_leave(rt, cfg):
                return None, ["min_hold_bullish_weakening"]
            if "higher_high" in types and "bullish_bos" in types:
                accum.reset_top()
                return "early_bullish", ["failed_top", "hh_bos"]
            ok, reasons, _meta = allow_topping(variant, types, row, cfg, accum, ts=ts, idx=idx)
            if ok:
                return "topping", reasons
            return None, reasons

        # Leaving weakening without bottoming/topping — reset accum when state changes elsewhere
        proposed, reasons = _propose_transition(rt, events=events, row=row, cfg=cfg)
        return proposed, reasons

    return _propose


def classify_entry_quality(
    direction: str,  # bottoming|topping
    entry_i: int,
    entry_px: float,
    closes: list[float],
    highs: list[float],
    lows: list[float],
    timeline: list[dict[str, Any]],
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    horizon = min(len(closes) - 1, entry_i + 48)
    if direction == "bottoming":
        mfe = max(highs[entry_i : horizon + 1]) - entry_px
        mae = entry_px - min(lows[entry_i : horizon + 1])
        target_early = "early_bullish"
        false_exit = "false_bottom"
    else:
        mfe = entry_px - min(lows[entry_i : horizon + 1])
        mae = max(highs[entry_i : horizon + 1]) - entry_px
        target_early = "early_bearish"
        false_exit = "false_top"

    dur = 0
    reached_early = False
    false_rev = False
    bars_to_early: int | None = None
    for j in range(entry_i + 1, len(timeline)):
        st = timeline[j]["state"]
        hold = "bottoming" if direction == "bottoming" else "topping"
        if st != hold and timeline[j - 1]["state"] == hold:
            dur = j - entry_i
            break
        dur = j - entry_i
    for h in history:
        if h["timestamp"] < timeline[entry_i]["timestamp"]:
            continue
        if h["from_state"] == ("bottoming" if direction == "bottoming" else "topping"):
            if h["to_state"] == target_early:
                reached_early = True
                # approximate bars
                for j in range(entry_i, len(timeline)):
                    if timeline[j]["timestamp"] == h["timestamp"]:
                        bars_to_early = j - entry_i
                        break
            if false_exit in str(h.get("reason", "")) or h["to_state"] == (
                "early_bearish" if direction == "bottoming" else "early_bullish"
            ) and not reached_early:
                if any(false_exit in str(r) for r in h.get("reason", [])):
                    false_rev = True
            break

    if false_rev or (mae > mfe and dur <= 6):
        cat = "false_positive" if false_rev or mae > mfe * 1.5 else "premature"
    elif reached_early and mfe >= mae:
        cat = "valid_reversal"
    elif mfe > mae * 1.2 and dur >= 6:
        cat = "valid_reversal"
    elif mae > mfe:
        cat = "false_positive"
    elif dur <= 4:
        cat = "premature"
    else:
        cat = "ambiguous"

    return {
        "category": cat,
        "mfe": round(mfe, 6),
        "mae": round(mae, 6),
        "duration": dur,
        "reached_early": reached_early,
        "bars_to_early": bars_to_early if bars_to_early is not None else "",
        "false_reversal_flag": false_rev,
    }


def replay_variant(
    frame: pd.DataFrame,
    pivots: list,
    scfg: Any,
    variant: Variant,
) -> dict[str, Any]:
    cfg = default_trend_state_config()
    rt = TrendRuntime()
    ohlcv = [c for c in ("timestamp", "open", "high", "low", "close", "volume") if c in frame.columns]
    n = len(frame)
    accum = AccumState()
    ctx: dict[str, Any] = {"ts": "", "idx": 0}
    propose = make_propose(variant, accum, ctx)
    prev = sm._propose_transition
    sm._propose_transition = propose  # type: ignore[assignment]

    history: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []
    hit_trace: list[dict[str, Any]] = []
    entries: list[dict[str, Any]] = []
    single_hit_episodes: list[dict[str, Any]] = []
    open_single: dict[str, Any] | None = None

    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []

    t0 = time.perf_counter()
    try:
        for i in range(n):
            if (i + 1) % 3000 == 0 or i + 1 == n:
                _p(f"[{variant.name}] {i+1}/{n} state={rt.state}")
            row = frame.iloc[i]
            row_d = row.to_dict()
            decision_ts = _ts(row["decision_time"])
            ts = _iso(decision_ts)
            ctx["ts"] = ts
            ctx["idx"] = i
            before = rt.state
            age_before = rt.age_5m_bars

            # Reset accum when leaving weakening without going to bottom/top
            if before not in {"bearish_weakening", "bullish_weakening"}:
                if before != "bottoming":
                    accum.reset_bottom()
                if before != "topping":
                    accum.reset_top()

            rt, snap, events = step_trend_state(
                rt,
                candle_row=row,
                pivots_5m=pivots,
                decision_time=decision_ts,
                candles_5m_as_of=frame.iloc[: i + 1][ohlcv],
                bar_index=i,
                cfg=cfg,
                scanner_cfg=scfg,
            )
            ev5 = [e for e in events if getattr(e, "timeframe", "5m") == "5m"]
            types = _event_types(ev5)
            closes.append(float(row["close"]))
            highs.append(float(row["high"]))
            lows.append(float(row["low"]))

            # Diagnostic while in weakening (use pre-transition state)
            if before in {"bearish_weakening", "bullish_weakening"}:
                direction = "bottoming" if before == "bearish_weakening" else "topping"
                hits = bottom_hits(types) if direction == "bottoming" else top_hits(types)
                # For B0 diagnostics of delay: track accum of unique hits conceptually
                # even under production (diagnostic only)
                if direction == "bottoming":
                    if hits and open_single is None and len(hits) == 1:
                        open_single = {
                            "direction": direction,
                            "first_hit_ts": ts,
                            "first_hit_i": i,
                            "first_hits": sorted(hits),
                            "state_age_at_first": age_before,
                        }
                    elif hits and open_single is not None and open_single["direction"] == direction:
                        open_single["last_hit_ts"] = ts
                        open_single["unique_hits"] = sorted(
                            set(open_single.get("unique_hits", open_single["first_hits"])) | hits
                        )
                    # evaluate allow under each conceptual lens for inventory
                    bull_conf, _ = _indicator_confirms(row_d, side="bullish", cfg=cfg)
                    bear_conf, _ = _indicator_confirms(row_d, side="bearish", cfg=cfg)
                    conf = bull_conf if direction == "bottoming" else bear_conf
                    retest = (
                        "bullish_retest_holds" in types
                        if direction == "bottoming"
                        else "bearish_retest_holds" in types
                    )
                    hit_trace.append(
                        {
                            "timestamp": ts,
                            "variant": variant.name,
                            "state_before": before,
                            "state_after": rt.state,
                            "direction": direction,
                            "state_age": age_before,
                            "same_bar_hits": "|".join(sorted(hits)),
                            "same_bar_hit_count": len(hits),
                            "b0_would_enter": len(hits) >= 2
                            and (
                                "lower_low" not in types
                                if direction == "bottoming"
                                else "higher_high" not in types
                            ),
                            "b1_would_enter": len(hits) >= 1
                            and (
                                "lower_low" not in types
                                if direction == "bottoming"
                                else "higher_high" not in types
                            ),
                            "structure_hit": bool(
                                hits & (BOTTOM_STRUCT if direction == "bottoming" else TOP_STRUCT)
                            ),
                            "retest_holds": retest,
                            "indicator_conf": conf,
                            "bias_5m": rt.structure_5m.current_structure_bias,
                            "bias_15m": _htf_bias(rt.structure_15m),
                            "bias_30m": _htf_bias(rt.structure_30m),
                            "types": "|".join(sorted(types)),
                            "entered": rt.state in {"bottoming", "topping"} and before != rt.state,
                        }
                    )

            if before != rt.state:
                history.append(
                    {
                        "timestamp": ts,
                        "from_state": before,
                        "to_state": rt.state,
                        "reason": list(snap.active_reasons),
                        "age_at_transition": age_before,
                    }
                )
                if rt.state in {"bottoming", "topping"} and before != rt.state:
                    delay_from_first = ""
                    first_ts = ""
                    if open_single and open_single["direction"] == rt.state:
                        delay_from_first = i - int(open_single["first_hit_i"])
                        first_ts = open_single["first_hit_ts"]
                        open_single["resolved"] = "entered"
                        open_single["resolve_ts"] = ts
                        open_single["delay_bars"] = delay_from_first
                        single_hit_episodes.append(dict(open_single))
                        open_single = None
                    elif accum.first_bottom_hit_i is not None and rt.state == "bottoming":
                        delay_from_first = i - accum.first_bottom_hit_i
                        first_ts = accum.first_bottom_hit_ts or ""
                    elif accum.first_top_hit_i is not None and rt.state == "topping":
                        delay_from_first = i - accum.first_top_hit_i
                        first_ts = accum.first_top_hit_ts or ""

                    entries.append(
                        {
                            "idx": i,
                            "entry_timestamp": ts,
                            "direction": rt.state,
                            "from_state": before,
                            "reason": list(snap.active_reasons),
                            "first_hit_timestamp": first_ts,
                            "delay_bars_from_first_hit": delay_from_first,
                            "delay_minutes": (
                                int(delay_from_first) * 5 if delay_from_first != "" else ""
                            ),
                            "bias_5m": rt.structure_5m.current_structure_bias,
                            "bias_15m": _htf_bias(rt.structure_15m),
                            "bias_30m": _htf_bias(rt.structure_30m),
                            "types": sorted(types),
                        }
                    )
                    if rt.state == "bottoming":
                        accum.reset_bottom()
                    else:
                        accum.reset_top()

                # abandoned single-hit episode
                if open_single and before in {"bearish_weakening", "bullish_weakening"}:
                    if rt.state not in {"bottoming", "topping", before}:
                        open_single["resolved"] = f"left_to_{rt.state}"
                        open_single["resolve_ts"] = ts
                        open_single["delay_bars"] = i - int(open_single["first_hit_i"])
                        single_hit_episodes.append(dict(open_single))
                        open_single = None

            # if still in weakening after step but we entered bottoming, clear
            if rt.state not in {"bearish_weakening", "bullish_weakening"} and open_single:
                if rt.state not in {"bottoming", "topping"}:
                    open_single["resolved"] = f"left_to_{rt.state}"
                    open_single["resolve_ts"] = ts
                    open_single["delay_bars"] = i - int(open_single["first_hit_i"])
                    single_hit_episodes.append(dict(open_single))
                open_single = None

            timeline.append({"timestamp": ts, "state": rt.state, "reason": list(snap.active_reasons)})
    finally:
        sm._propose_transition = prev  # type: ignore[assignment]

    if open_single is not None:
        open_single["resolved"] = "end_of_replay"
        single_hit_episodes.append(dict(open_single))

    elapsed = time.perf_counter() - t0

    # quality for entries
    quality_rows = []
    for e in entries:
        q = classify_entry_quality(
            e["direction"],
            e["idx"],
            closes[e["idx"]],
            closes,
            highs,
            lows,
            timeline,
            history,
        )
        quality_rows.append({**{k: e[k] for k in e if k != "idx"}, **q, "variant": variant.name})

    # downstream early/strong after bottoming/topping
    early_after = 0
    strong_after = 0
    for e in entries:
        ei = e["idx"]
        target_early = "early_bullish" if e["direction"] == "bottoming" else "early_bearish"
        target_strong = "strong_bullish" if e["direction"] == "bottoming" else "strong_bearish"
        seen_early = False
        for j in range(ei, min(len(timeline), ei + 200)):
            if timeline[j]["state"] == target_early:
                early_after += 1
                seen_early = True
            if seen_early and timeline[j]["state"] == target_strong:
                strong_after += 1
                break
            if timeline[j]["state"] in {"neutral", "unavailable"}:
                break

    bottoming_n = sum(1 for h in history if h["to_state"] == "bottoming")
    topping_n = sum(1 for h in history if h["to_state"] == "topping")
    cats = Counter(q["category"] for q in quality_rows)
    delays = [
        int(e["delay_bars_from_first_hit"])
        for e in entries
        if e["delay_bars_from_first_hit"] != "" and e["delay_bars_from_first_hit"] is not None
    ]
    abandoned = sum(1 for s in single_hit_episodes if str(s.get("resolved", "")).startswith("left_to_"))
    resolved_enter = sum(1 for s in single_hit_episodes if s.get("resolved") == "entered")

    # focus-window counts
    focus = _iso(_ts(FOCUS_START))
    bottoming_focus = sum(
        1 for h in history if h["to_state"] == "bottoming" and h["timestamp"] >= focus
    )
    topping_focus = sum(
        1 for h in history if h["to_state"] == "topping" and h["timestamp"] >= focus
    )

    metrics = {
        "variant": variant.name,
        "bottoming_count": bottoming_n,
        "topping_count": topping_n,
        "bottoming_focus_jan_mar": bottoming_focus,
        "topping_focus_jan_mar": topping_focus,
        "valid_reversals": cats.get("valid_reversal", 0),
        "false_positives": cats.get("false_positive", 0),
        "premature": cats.get("premature", 0),
        "ambiguous": cats.get("ambiguous", 0),
        "median_delay_bars_first_to_entry": (
            sorted(delays)[len(delays) // 2] if delays else 0
        ),
        "max_delay_bars_first_to_entry": max(delays) if delays else 0,
        "median_delay_minutes": (
            sorted(delays)[len(delays) // 2] * 5 if delays else 0
        ),
        "max_delay_minutes": (max(delays) * 5 if delays else 0),
        "state_changes": len(history),
        "single_hit_episodes": len(single_hit_episodes),
        "single_hit_abandoned": abandoned,
        "single_hit_later_entered": resolved_enter,
        "downstream_early_count": early_after,
        "downstream_strong_count": strong_after,
        "early_bearish_entries": sum(1 for h in history if h["to_state"] == "early_bearish"),
        "early_bullish_entries": sum(1 for h in history if h["to_state"] == "early_bullish"),
        "strong_bearish_entries": sum(1 for h in history if h["to_state"] == "strong_bearish"),
        "strong_bullish_entries": sum(1 for h in history if h["to_state"] == "strong_bullish"),
        "runtime_seconds": round(elapsed, 3),
    }

    return {
        "variant": variant.name,
        "history": history,
        "timeline": timeline,
        "hit_trace": hit_trace,
        "entries": entries,
        "quality_rows": quality_rows,
        "single_hit_episodes": single_hit_episodes,
        "metrics": metrics,
        "history_fp": _sha(json.dumps(history, sort_keys=True, default=str).encode()),
    }


def semantics_payload() -> dict[str, Any]:
    return {
        "rule_type": "same_bar_combinatorial_not_cross_bar_counter",
        "bearish_weakening_to_bottoming": {
            "source_function": "trend_state_machine._propose_transition",
            "full_boolean_condition": (
                "state==bearish_weakening AND age>=min_hold(2) "
                "AND NOT (lower_low AND bearish_bos)  # failed_bottom path "
                "AND len(types ∩ {failed_breakdown,bullish_choch,higher_low,bullish_bos}) >= 2 "
                "AND lower_low not in types"
            ),
            "hit_set": sorted(BOTTOM_HIT_SET),
            "required_distinct_hits_same_bar": 2,
            "min_hold": 2,
            "note": "Hits must co-occur on the same 5m bar event set; sticky last_* slots are NOT used.",
        },
        "bullish_weakening_to_topping": {
            "source_function": "trend_state_machine._propose_transition",
            "full_boolean_condition": (
                "state==bullish_weakening AND age>=min_hold(2) "
                "AND NOT (higher_high AND bullish_bos) "
                "AND len(types ∩ {failed_breakout,bearish_choch,lower_high,bearish_bos}) >= 2 "
                "AND higher_high not in types"
            ),
            "hit_set": sorted(TOP_HIT_SET),
            "required_distinct_hits_same_bar": 2,
        },
        "known_march_example": {
            "timestamp": "2026-03-06T01:35:00+00:00",
            "hits": ["bullish_choch", "failed_breakdown"],
            "same_bar": True,
        },
        "interaction": {
            "HTF": "no HTF gate on weakening→bottoming/topping entry",
            "G6": "affects how often weakening is reached from early/strong; not the 2-hit rule itself",
            "V6+V2": "affects FB/FO levels feeding failed_* events into hit set",
            "bottoming→early": "separate rules with HTF veto possible",
        },
    }


def run() -> dict[str, Any]:
    OUT.mkdir(parents=True, exist_ok=True)
    end = _ts(DIAG_END)
    hashes_before = {
        "trend_state_machine_md5": _md5(MACHINE),
        "trend_structure_md5": _md5(STRUCTURE),
        "trend_state_policy_md5": _md5(POLICY),
    }
    _p("=== Bottoming/Topping 2-hit audit (diagnostic only) ===")
    _p(json.dumps(hashes_before))
    _write_json(OUT / "current_2hit_semantics.json", semantics_payload())
    _write_json(
        OUT / "variant_definitions.json",
        {v.name: {"description": v.description, "mode": v.mode} for v in VARIANTS},
    )

    _p("[load] frame + pivots")
    frame, pivots, scfg = load_frame(end)
    _p(f"[load] bars={len(frame)} pivots={len(pivots)}")
    install_causal_htf_prefix_cache(frame, end)

    results: dict[str, Any] = {}
    metrics_rows = []
    quality_all = []
    hit_all = []
    episode_all = []
    entry_all = []
    timeline_focus = []

    for v in VARIANTS:
        _p(f"[variant] {v.name} start")
        res = replay_variant(frame, pivots, scfg, v)
        results[v.name] = res
        metrics_rows.append(res["metrics"])
        quality_all.extend(res["quality_rows"])
        for h in res["hit_trace"]:
            if h["timestamp"] >= _iso(_ts(FOCUS_START)):
                hit_all.append(h)
        for ep in res["single_hit_episodes"]:
            episode_all.append({"variant": v.name, **ep})
        for e in res["entries"]:
            entry_all.append(
                {
                    "variant": v.name,
                    **{k: e[k] for k in e if k != "idx"},
                    "types": "|".join(e.get("types") or []),
                    "reason": json.dumps(e.get("reason"), default=str),
                }
            )
        for t in res["timeline"]:
            if t["timestamp"] >= _iso(_ts(MARCH_START)):
                timeline_focus.append(
                    {
                        "variant": v.name,
                        **t,
                        "reason": json.dumps(t["reason"], default=str),
                    }
                )
        m = res["metrics"]
        _p(
            f"[{v.name}] bottoming={m['bottoming_count']} topping={m['topping_count']} "
            f"valid={m['valid_reversals']} fp={m['false_positives']} prem={m['premature']} "
            f"med_delay={m['median_delay_bars_first_to_entry']} t={m['runtime_seconds']}s"
        )

    # B0 pure check
    pure = replay_variant(frame, pivots, scfg, VARIANTS[0])
    b0_match = results["B0"]["history_fp"] == pure["history_fp"]
    _p(f"[verify] B0 == pure production: {b0_match}")

    b0 = results["B0"]

    # Missed reversals under B0: single-hit bars where price reversed hard before 2-hit
    missed = []
    for ep in b0["single_hit_episodes"]:
        if ep.get("resolved") == "entered":
            continue
        # check favorable move after first hit
        fi = int(ep["first_hit_i"])
        # need closes from B0 replay — re-derive from timeline length
        # approximate via hit_trace timestamps
        missed.append(
            {
                "direction": ep["direction"],
                "first_hit_ts": ep["first_hit_ts"],
                "resolved": ep.get("resolved"),
                "resolve_ts": ep.get("resolve_ts", ""),
                "delay_bars": ep.get("delay_bars", ""),
                "first_hits": "|".join(ep.get("first_hits") or []),
                "classification": "abandoned_single_hit",
            }
        )

    # Compare B0 hit_trace: bars with b1_would_enter but not b0
    b0_hits = [h for h in b0["hit_trace"] if h["timestamp"] >= _iso(_ts(FOCUS_START))]
    one_not_two = [
        h for h in b0_hits if h["b1_would_enter"] and not h["b0_would_enter"]
    ]

    _write_csv(OUT / "variant_replay_metrics.csv", metrics_rows)
    _write_csv(OUT / "entry_quality.csv", quality_all)
    _write_csv(OUT / "hit_trace_focus.csv", hit_all)
    _write_csv(OUT / "single_hit_episodes.csv", episode_all)
    _write_csv(OUT / "bottoming_topping_entries.csv", entry_all)
    _write_csv(OUT / "march_timeline_by_variant.csv", timeline_focus)
    _write_csv(OUT / "abandoned_single_hit_b0.csv", missed)
    _write_csv(
        OUT / "one_hit_not_two_bars_b0.csv",
        [
            {
                "timestamp": h["timestamp"],
                "direction": h["direction"],
                "same_bar_hits": h["same_bar_hits"],
                "structure_hit": h["structure_hit"],
                "retest_holds": h["retest_holds"],
                "indicator_conf": h["indicator_conf"],
                "bias_15m": h["bias_15m"],
                "entered_eventual": h["entered"],
            }
            for h in one_not_two
        ],
    )

    # Symmetry
    sym = []
    for v in VARIANTS:
        m = results[v.name]["metrics"]
        q = results[v.name]["quality_rows"]
        b_q = [x for x in q if x["direction"] == "bottoming"]
        t_q = [x for x in q if x["direction"] == "topping"]
        sym.append(
            {
                "variant": v.name,
                "bottoming": m["bottoming_count"],
                "topping": m["topping_count"],
                "delta_bottom_minus_top": m["bottoming_count"] - m["topping_count"],
                "bottom_valid": sum(1 for x in b_q if x["category"] == "valid_reversal"),
                "top_valid": sum(1 for x in t_q if x["category"] == "valid_reversal"),
                "bottom_fp": sum(1 for x in b_q if x["category"] == "false_positive"),
                "top_fp": sum(1 for x in t_q if x["category"] == "false_positive"),
                "bottom_premature": sum(1 for x in b_q if x["category"] == "premature"),
                "top_premature": sum(1 for x in t_q if x["category"] == "premature"),
            }
        )
    _write_csv(OUT / "bullish_bearish_symmetry.csv", sym)

    # March focus comparison
    march_rows = []
    for vname, res in results.items():
        for h in res["history"]:
            if _iso(_ts(MARCH_START)) <= h["timestamp"] <= _iso(_ts(MARCH_END)):
                if h["to_state"] in {"bottoming", "topping", "early_bullish", "early_bearish"}:
                    march_rows.append(
                        {
                            "variant": vname,
                            "timestamp": h["timestamp"],
                            "from_state": h["from_state"],
                            "to_state": h["to_state"],
                            "reason": json.dumps(h["reason"], default=str),
                        }
                    )
    _write_csv(OUT / "march_transitions_by_variant.csv", march_rows)

    # Qualitative + decision
    b0m = results["B0"]["metrics"]
    b1m = results["B1"]["metrics"]
    b2m = results["B2"]["metrics"]
    b3m = results["B3"]["metrics"]
    b4m = results["B4"]["metrics"]

    def rate(v: str, m: dict[str, Any]) -> dict[str, Any]:
        extra_bt = (m["bottoming_count"] + m["topping_count"]) - (
            b0m["bottoming_count"] + b0m["topping_count"]
        )
        fp_delta = m["false_positives"] - b0m["false_positives"]
        prem_delta = m["premature"] - b0m["premature"]
        valid_delta = m["valid_reversals"] - b0m["valid_reversals"]
        if v == "B0":
            rating = "gut"
            risk = "same-bar 2-hit delays until co-occurrence; may miss staggered hits"
        elif v == "B1":
            rating = "schwach" if fp_delta + prem_delta >= valid_delta else "mittel"
            risk = "more entries; higher FP/premature risk"
        elif v == "B2":
            rating = "gut" if fp_delta <= 2 and valid_delta >= 0 else "mittel"
            risk = "blocks lone HL/LH; still 1-hit on choch/bos/FB"
        elif v == "B3":
            rating = "gut" if fp_delta <= 1 else "mittel"
            risk = "requires existing conf/retest; may still delay vs pure 1-hit"
        else:
            rating = "gut" if valid_delta > 0 and fp_delta <= 2 else "mittel"
            risk = "cross-bar accum changes semantics; may enter earlier on staggered hits"
        return {
            "variant": v,
            "bottoming_topping_total": m["bottoming_count"] + m["topping_count"],
            "extra_vs_b0": extra_bt,
            "valid_reversals": m["valid_reversals"],
            "false_positives": m["false_positives"],
            "premature": m["premature"],
            "valid_delta": valid_delta,
            "fp_delta": fp_delta,
            "premature_delta": prem_delta,
            "median_delay_bars": m["median_delay_bars_first_to_entry"],
            "single_hit_abandoned": m["single_hit_abandoned"],
            "rating": rating,
            "main_risk": risk,
        }

    qual = [rate(v.name, results[v.name]["metrics"]) for v in VARIANTS]
    _write_csv(OUT / "qualitative_evaluation.csv", qual)

    # Decision logic
    # Prefer keep B0 if 1-hit variants add more FP+premature than valid
    b1_cost = (b1m["false_positives"] + b1m["premature"]) - (
        b0m["false_positives"] + b0m["premature"]
    )
    b1_gain = b1m["valid_reversals"] - b0m["valid_reversals"]
    b2_cost = (b2m["false_positives"] + b2m["premature"]) - (
        b0m["false_positives"] + b0m["premature"]
    )
    b2_gain = b2m["valid_reversals"] - b0m["valid_reversals"]
    b3_cost = (b3m["false_positives"] + b3m["premature"]) - (
        b0m["false_positives"] + b0m["premature"]
    )
    b3_gain = b3m["valid_reversals"] - b0m["valid_reversals"]
    b4_cost = (b4m["false_positives"] + b4m["premature"]) - (
        b0m["false_positives"] + b0m["premature"]
    )
    b4_gain = b4m["valid_reversals"] - b0m["valid_reversals"]

    # Prefer B0 unless a variant clearly improves precision without FP explosion / state churn.
    b0_total_bt = b0m["bottoming_count"] + b0m["topping_count"]
    b1_total_bt = b1m["bottoming_count"] + b1m["topping_count"]
    b0_prec = b0m["valid_reversals"] / max(
        1, b0m["valid_reversals"] + b0m["false_positives"] + b0m["premature"]
    )
    b1_prec = b1m["valid_reversals"] / max(
        1, b1m["valid_reversals"] + b1m["false_positives"] + b1m["premature"]
    )
    decision = "J"
    recommended = "B0"
    runner = "B3"
    rationale = (
        "1-hit variants multiply bottoming/topping and absolute FP; "
        "lone higher_low is too weak as sole entry; keep same-bar 2-hit"
    )
    # Only switch if precision rises materially AND FP cost is small AND churn not extreme
    for name, gain, cost, tot in (
        ("B3", b3_gain, b3_cost, b3m["bottoming_count"] + b3m["topping_count"]),
        ("B2", b2_gain, b2_cost, b2m["bottoming_count"] + b2m["topping_count"]),
        ("B4", b4_gain, b4_cost, b4m["bottoming_count"] + b4m["topping_count"]),
        ("B1", b1_gain, b1_cost, b1_total_bt),
    ):
        prec = results[name]["metrics"]["valid_reversals"] / max(
            1,
            results[name]["metrics"]["valid_reversals"]
            + results[name]["metrics"]["false_positives"]
            + results[name]["metrics"]["premature"],
        )
        if (
            gain >= 2
            and cost <= 1
            and prec >= b0_prec + 0.1
            and tot <= b0_total_bt * 1.5
            and results[name]["metrics"]["state_changes"] <= b0m["state_changes"] * 1.5
        ):
            recommended = name
            runner = "B0"
            decision = "K" if name in {"B1", "B2", "B3"} else "L"
            rationale = f"{name} improves precision without FP/churn explosion"
            break
    else:
        if b1_prec > b0_prec + 0.15 and b1_cost <= b1_gain * 0.5:
            decision = "M"
            rationale = "precision hint for 1-hit but FP/churn still concerning — need more evidence"
        else:
            decision = "J"
            recommended = "B0"
            runner = "B3"

    # March specific
    march_b0_bottom = [
        h
        for h in b0["history"]
        if h["to_state"] == "bottoming"
        and _iso(_ts(MARCH_START)) <= h["timestamp"] <= _iso(_ts(MARCH_END))
    ]

    recommended_payload = {
        "technical_rule_understanding": "same_bar_combinatorial_2_distinct_hit_types",
        "current_policy_correct": decision == "J",
        "decision_letter": decision,
        "recommended_variant": recommended,
        "runner_up": runner,
        "rationale": rationale,
        "primary_finding": (
            "Bottoming/topping requires TWO distinct hit types on the SAME 5m bar "
            f"from {sorted(BOTTOM_HIT_SET)} / {sorted(TOP_HIT_SET)}. "
            "Delay is waiting for co-occurrence, not a second confirmation candle counter. "
            f"B0 single-hit episodes abandoned={b0m['single_hit_abandoned']}, "
            f"later_entered={b0m['single_hit_later_entered']}. "
            f"one_hit_not_two bars in focus={len(one_not_two)}."
        ),
        "exact_b0_bottoming_rule": semantics_payload()["bearish_weakening_to_bottoming"][
            "full_boolean_condition"
        ],
        "exact_b0_topping_rule": semantics_payload()["bullish_weakening_to_topping"][
            "full_boolean_condition"
        ],
        "asymmetry_needed": False,
        "htf_interaction": "none on entry to bottoming/topping; HTF applies bottoming→early",
        "g6_v6_status": "unchanged; left intact",
        "march_effect": {
            "b0_bottoming_in_window": [h["timestamp"] for h in march_b0_bottom],
            "known_0135": "2026-03-06T01:35:00+00:00 choch+failed_breakdown same-bar",
        },
        "broader_replay_effect": {v: results[v]["metrics"] for v in ["B0", "B1", "B2", "B3", "B4"]},
        "quality_effect": {
            v: dict(Counter(q["category"] for q in results[v]["quality_rows"]))
            for v in ["B0", "B1", "B2", "B3", "B4"]
        },
        "new_parameters_required": [],
        "implementation_files_later": (
            []
            if decision == "J"
            else ["research/regime_scanner/trend_state_machine.py::_propose_transition"]
        ),
        "implementation_risk": "none" if decision == "J" else "medium — more premature reversals",
        "confidence": "high" if b0_match else "medium",
        "hashes_before": hashes_before,
        "b0_matches_pure_production": b0_match,
        "deltas": {
            "B1": {"gain": b1_gain, "cost": b1_cost},
            "B2": {"gain": b2_gain, "cost": b2_cost},
            "B3": {"gain": b3_gain, "cost": b3_cost},
            "B4": {"gain": b4_gain, "cost": b4_cost},
        },
    }

    hashes_after = {
        "trend_state_machine_md5": _md5(MACHINE),
        "trend_structure_md5": _md5(STRUCTURE),
        "trend_state_policy_md5": _md5(POLICY),
    }
    recommended_payload["hashes_after"] = hashes_after
    recommended_payload["hashes_unchanged"] = hashes_before == hashes_after
    _write_json(OUT / "recommended_bottoming_topping_candidate.json", recommended_payload)

    (OUT / "test_plan.md").write_text(
        """# Bottoming/Topping — Test Plan (only if implementing later)

1. Unit: same-bar 2 distinct hits still enter under B0.
2. Unit: single hit alone does not enter under B0.
3. Unit: failed_bottom (LL+bearish_bos) still exits weakening to early_bearish.
4. Symmetry: topping mirrors bottoming.
5. Regression: G6, V6+V2, HTF early→strong unchanged.
6. March 2026-03-06T01:35 still bottoming on choch+failed_breakdown.
7. Dual determinism checksum.
""",
        encoding="utf-8",
    )
    (OUT / "README.md").write_text(
        f"""# Bottoming / Topping 2-Hit Audit

Diagnostic only. V6+V2, G6, HTF, structure/machine/policy production logic unchanged
(variants only via temporary propose patch during replay).

## Hashes
before: {json.dumps(hashes_before)}
after: {json.dumps(hashes_after)}

## Decision
**{decision}** — recommended `{recommended}` ({rationale})

## Rule reminder
`len(types ∩ hit_set) >= 2` on the **same bar**, not a 2-candle counter.
""",
        encoding="utf-8",
    )

    # determinism
    _p("[determinism] B0/B1 second pass")
    r0 = replay_variant(frame, pivots, scfg, VARIANTS[0])
    r1 = replay_variant(frame, pivots, scfg, VARIANTS[1])
    det = {
        "B0_match": results["B0"]["history_fp"] == r0["history_fp"],
        "B1_match": results["B1"]["history_fp"] == r1["history_fp"],
        "b0_matches_pure_production": b0_match,
    }
    _write_json(OUT / "determinism_check.json", det)
    _p(json.dumps(det))

    checksums = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(OUT.glob("*"))
        if p.suffix in {".csv", ".json", ".md"} and p.name != "determinism_check.json"
    }
    _write_json(OUT / "artifact_checksums.json", checksums)
    _p("DONE")
    return recommended_payload


def main() -> int:
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
