#!/usr/bin/env python3
"""Phase A multilevel market structure audit (read-only, isolated).

No production / policy / state-machine changes. No staging or commits.
"""
from __future__ import annotations

import csv
import hashlib
import json
import resource
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.data_loader import feather_path_for_symbol, load_symbol_candles
from research.regime_scanner.market_regime_macro_context_audit import aggregate_closed_htf
from research.regime_scanner.multilevel_market_structure import (
    annotate_event_outcomes,
    run_multilevel_structure,
)
from research.regime_scanner.point_audit import json_safe

OUT = Path("research/regime_scanner/results/multilevel_market_structure_audit")
ROOT = Path("research/regime_scanner")

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

SWING_LENGTHS = (20, 30, 50)
PRIMARY_SWING = 50
INTERNAL_SIZE = 5

FOCUS = {
    "jan13_15": ("2026-01-13", "2026-01-15"),
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


def filter_audit(rows: list[dict[str, Any]], key: str = "decision_timestamp_utc") -> list[dict[str, Any]]:
    a, b = _ts(AUDIT_START), _ts(AUDIT_END)
    return [r for r in rows if a <= _ts(r[key]) <= b]


def layer_timeline(rows: list[dict[str, Any]], layer: str) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        out.append(
            {
                "timestamp_utc": r["timestamp_utc"],
                "decision_timestamp_utc": r["decision_timestamp_utc"],
                "close": r["close"],
                "bias": r[f"{layer}_bias"],
                "leg": r[f"{layer}_leg"],
                "point_type": r[f"{layer}_point_type"],
                "pivot_high": r[f"{layer}_pivot_high"],
                "pivot_low": r[f"{layer}_pivot_low"],
                "pivot_extreme_timestamp": r[f"{layer}_pivot_extreme_timestamp"],
                "pivot_confirmation_timestamp": r[f"{layer}_pivot_confirmation_timestamp"],
                "pivot_available_from": r[f"{layer}_pivot_available_from"],
                "active_high": r[f"{layer}_active_high"],
                "active_low": r[f"{layer}_active_low"],
                "active_high_id": r[f"{layer}_active_high_id"],
                "active_low_id": r[f"{layer}_active_low_id"],
                "bullish_bos": r[f"{layer}_bullish_bos"],
                "bearish_bos": r[f"{layer}_bearish_bos"],
                "bullish_choch": r[f"{layer}_bullish_choch"],
                "bearish_choch": r[f"{layer}_bearish_choch"],
                "wick_cross_high": r[f"{layer}_wick_cross_high"],
                "wick_cross_low": r[f"{layer}_wick_cross_low"],
                "close_cross_high": r[f"{layer}_close_cross_high"],
                "close_cross_low": r[f"{layer}_close_cross_low"],
            }
        )
    return out


def event_counts(rows: list[dict[str, Any]], layer: str) -> dict[str, int]:
    return {
        "bullish_bos": sum(1 for r in rows if r.get(f"{layer}_bullish_bos")),
        "bearish_bos": sum(1 for r in rows if r.get(f"{layer}_bearish_bos")),
        "bullish_choch": sum(1 for r in rows if r.get(f"{layer}_bullish_choch")),
        "bearish_choch": sum(1 for r in rows if r.get(f"{layer}_bearish_choch")),
        "pivot_high": sum(1 for r in rows if r.get(f"{layer}_pivot_high")),
        "pivot_low": sum(1 for r in rows if r.get(f"{layer}_pivot_low")),
        "recovery_bars": sum(
            1 for r in rows if r.get("combined_primary_label") == "bullish_recovery_inside_bearish_swing"
        ),
        "confirmed_bull_rev_bars": sum(
            1 for r in rows if r.get("combined_primary_label") == "confirmed_bullish_swing_reversal"
        ),
    }


def jan13_15_answers(rows: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    a, b = _ts("2026-01-13"), _ts("2026-01-15 23:59:59")
    win = [r for r in rows if a <= _ts(r["decision_timestamp_utc"]) <= b]
    recovery_bars = [r for r in win if r.get("combined_primary_label") == "bullish_recovery_inside_bearish_swing"]
    confirmed_bull = [
        r for r in win if r.get("combined_primary_label") == "confirmed_bullish_swing_reversal"
    ]
    possible_rev = [r for r in win if str(r.get("combined_primary_label", "")).startswith("possible_")]
    earliest_possible = None
    for r in win:
        if r.get("ctx_possible_bullish_swing_reversal") or r.get("swing_bullish_choch"):
            earliest_possible = r["decision_timestamp_utc"]
            break
    split = [
        r
        for r in win
        if int(r.get("internal_bias") or 0) == 1 and int(r.get("swing_bias") or 0) == -1
    ]
    return {
        "n_bars": len(win),
        "n_recovery_bars": len(recovery_bars),
        "n_internal_bull_swing_bear_bars": len(split),
        "n_confirmed_bullish_reversal_bars": len(confirmed_bull),
        "n_confirmed_reversal_bars": len(confirmed_bull),
        "n_possible_reversal_bars": len(possible_rev),
        "earliest_possible_swing_reversal_decision": earliest_possible,
        "bullish_move_appears_as_recovery_not_macro_uptrend": len(recovery_bars) > 0
        and len(confirmed_bull) == 0,
        "sample_labels": sorted({str(r.get("combined_primary_label")) for r in win}),
        "internal_events_in_window": [
            e
            for e in events
            if e.get("structure_level") == "internal" and a <= _ts(e["event_decision_timestamp"]) <= b
        ],
        "swing_events_in_window": [
            e
            for e in events
            if e.get("structure_level") == "swing" and a <= _ts(e["event_decision_timestamp"]) <= b
        ],
    }


def build_pine(month: int, rows: list[dict[str, Any]], pivots: list[dict[str, Any]], events: list[dict[str, Any]]) -> str:
    a = _ts(f"2026-{month:02d}-01")
    b = _ts("2027-01-01") if month == 12 else _ts(f"2026-{month + 1:02d}-01")
    month_rows = [r for r in rows if a <= _ts(r["decision_timestamp_utc"]) < b]
    month_piv = [p for p in pivots if a <= _ts(p["available_from_timestamp_utc"]) < b]
    month_ev = [e for e in events if a <= _ts(e["event_decision_timestamp"]) < b]

    lines = [
        "//@version=6",
        f'indicator("Multilevel Structure Review 2026-{month:02d}", overlay=true, max_labels_count=300, max_lines_count=100)',
        "",
        "// Precomputed Phase-A multilevel structure — no OBs/FVGs/EQH, no policy.",
        "// Pivot labels use extreme / confirmed / available_from (UTC decision times).",
        "",
        'showSwingBg = input.bool(true, "Swing bias background")',
        'colorInternal = input.bool(false, "Color candles by internal bias")',
        'showSwingLevels = input.bool(true, "Show active swing levels")',
        'showInternalLevels = input.bool(false, "Show active internal levels")',
        'showSwingEvents = input.bool(true, "Show swing BOS/CHoCH")',
        'showInternalEvents = input.bool(false, "Show internal BOS/CHoCH")',
        'showPivotLabels = input.bool(false, "Show pivot labels")',
        'bgTransp = input.int(92, "Background transparency", minval=80, maxval=98)',
        "",
    ]

    # bias bgcolor via decision stamps of swing bias changes / continuous? Use per-bar plotshape sparse.
    # Encode swing bias segments roughly by plotting bgcolor on bars matching decision minute.
    for r in month_rows:
        if int(r.get("swing_bias") or 0) == 0:
            continue
        t = _ts(r["decision_timestamp_utc"])
        # decision is bar close = open+30m; on 30m chart time is open. Use open timestamp.
        ot = _ts(r["timestamp_utc"])
        cond = (
            f"(year=={ot.year} and month=={ot.month} and dayofmonth=={ot.day} "
            f"and hour=={ot.hour} and minute=={ot.minute})"
        )
        col = "color.new(color.green, bgTransp)" if int(r["swing_bias"]) > 0 else "color.new(color.red, bgTransp)"
        lines.append(f"bgcolor(showSwingBg and {cond} ? {col} : na)")

    # sparse: only emit level lines at activation bars (horizontal ray via line.new once)
    lines += [
        "var line[] swingHighLines = array.new_line()",
        "var line[] swingLowLines = array.new_line()",
        "",
    ]

    for p in month_piv:
        if p["structure_level"] != "swing":
            continue
        t = _ts(p["extreme_timestamp_utc"])
        cond = (
            f"(year=={t.year} and month=={t.month} and dayofmonth=={t.day} "
            f"and hour=={t.hour} and minute=={t.minute})"
        )
        price = float(p["price"])
        side = p["side"]
        label = p.get("point_type") or side
        txt = (
            f"{label}\\next={p['extreme_timestamp_utc'][:16]}\\n"
            f"conf={p['confirmation_timestamp_utc'][:16]}\\n"
            f"avail={p['available_from_timestamp_utc'][:16]}"
        )
        if side == "high":
            lines.append(
                f"plotshape(showSwingLevels and {cond}, title=\"SH\", style=shape.triangledown, "
                f"location=location.abovebar, color=color.new(color.red, 30), size=size.tiny)"
            )
            lines.append(f"if showPivotLabels and {cond}")
            lines.append(
                f'    label.new(bar_index, high, "{txt}", style=label.style_label_down, '
                f"color=color.new(color.red, 70), textcolor=color.white, size=size.tiny)"
            )
        else:
            lines.append(
                f"plotshape(showSwingLevels and {cond}, title=\"SL\", style=shape.triangleup, "
                f"location=location.belowbar, color=color.new(color.green, 30), size=size.tiny)"
            )
            lines.append(f"if showPivotLabels and {cond}")
            lines.append(
                f'    label.new(bar_index, low, "{txt}", style=label.style_label_up, '
                f"color=color.new(color.green, 70), textcolor=color.white, size=size.tiny)"
            )
        # draw level line from available_from open approx using extreme bar
        lines.append(f"if showSwingLevels and {cond}")
        if side == "high":
            lines.append(
                f"    array.push(swingHighLines, line.new(bar_index, {price}, bar_index + 20, {price}, "
                f"color=color.new(color.red, 50), width=1))"
            )
        else:
            lines.append(
                f"    array.push(swingLowLines, line.new(bar_index, {price}, bar_index + 20, {price}, "
                f"color=color.new(color.green, 50), width=1))"
            )

    for e in month_ev:
        t = _ts(e["event_decision_timestamp"])
        # map decision to prior open for 30m: decision = open+30m
        ot = t - pd.Timedelta(minutes=30)
        cond = (
            f"(year=={ot.year} and month=={ot.month} and dayofmonth=={ot.day} "
            f"and hour=={ot.hour} and minute=={ot.minute})"
        )
        layer = e["structure_level"]
        gate = "showSwingEvents" if layer == "swing" else "showInternalEvents"
        bull = e["direction"] == "bullish"
        choch = e["event_type"] == "choch"
        col = "color.lime" if bull else "color.red"
        shape = "shape.diamond" if choch else "shape.circle"
        loc = "location.belowbar" if bull else "location.abovebar"
        lines.append(
            f'plotshape({gate} and {cond}, title="{layer}_{e["event_type"]}", style={shape}, '
            f"location={loc}, color={col}, size=size.tiny)"
        )

    # optional internal candle tint via plotcandle is heavy; skip body recolor — document input only
    lines.append("// Internal bias candle recolor left optional/off (TradingView plotcandle override avoided).")
    lines.append("barcolor(colorInternal ? na : na)")
    return "\n".join(lines) + "\n"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    hashes_before = {n: _md5(ROOT / n) for n in PROTECTED}
    for n, exp in PROTECTED.items():
        if hashes_before[n] != exp:
            raise SystemExit(f"protected hash mismatch before: {n} {hashes_before[n]} != {exp}")

    for n in ("trend_state_policy.py", "trend_state_machine.py"):
        if "multilevel_market_structure" in (ROOT / n).read_text(encoding="utf-8"):
            raise SystemExit(f"unexpected multilevel import in {n}")

    _p("load candles")
    raw = load_symbol_candles("APTUSDT")
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    path = feather_path_for_symbol("APTUSDT")
    end_wall = _ts(AUDIT_END)
    load_start = _ts(LOAD_START)
    sl = raw[(raw["timestamp"] >= load_start) & (raw["timestamp"] <= _ts("2026-03-16 23:55:00+00:00"))].copy()
    ohlcv5 = sl[["timestamp", "open", "high", "low", "close", "volume"]]

    _p("aggregate closed 30m (+ optional 4h context)")
    agg30 = aggregate_closed_htf(ohlcv5, 30, end_wall)
    agg4 = aggregate_closed_htf(ohlcv5, 240, end_wall)

    variant_stats: list[dict[str, Any]] = []
    engines: dict[int, Any] = {}
    timelines: dict[int, list[dict[str, Any]]] = {}

    for swing_len in SWING_LENGTHS:
        _p(f"run multilevel internal={INTERNAL_SIZE} swing={swing_len}")
        rows, eng = run_multilevel_structure(
            agg30, internal_size=INTERNAL_SIZE, swing_size=swing_len, timeframe="30m"
        )
        # determinism
        rows2, _ = run_multilevel_structure(
            agg30, internal_size=INTERNAL_SIZE, swing_size=swing_len, timeframe="30m"
        )
        if rows != rows2:
            raise SystemExit(f"non-deterministic for swing={swing_len}")

        audit_rows = filter_audit(rows)
        annotate_event_outcomes(eng.all_events, pd.DataFrame(rows))
        engines[swing_len] = eng
        timelines[swing_len] = audit_rows

        if swing_len == PRIMARY_SWING:
            _write_csv(OUT / "combined_structure_timeline.csv", audit_rows)
            _write_csv(OUT / "internal_structure_timeline.csv", layer_timeline(audit_rows, "internal"))
        _write_csv(
            OUT / f"swing_structure_timeline_len{swing_len}.csv",
            layer_timeline(audit_rows, "swing"),
        )

        ic = event_counts(audit_rows, "internal")
        sc = event_counts(audit_rows, "swing")
        variant_stats.append(
            {
                "variant": f"i{INTERNAL_SIZE}_s{swing_len}",
                "swing_length": swing_len,
                "n_bars": len(audit_rows),
                "internal_bullish_bos": ic["bullish_bos"],
                "internal_bearish_bos": ic["bearish_bos"],
                "internal_bullish_choch": ic["bullish_choch"],
                "internal_bearish_choch": ic["bearish_choch"],
                "swing_bullish_bos": sc["bullish_bos"],
                "swing_bearish_bos": sc["bearish_bos"],
                "swing_bullish_choch": sc["bullish_choch"],
                "swing_bearish_choch": sc["bearish_choch"],
                "internal_pivots": ic["pivot_high"] + ic["pivot_low"],
                "swing_pivots": sc["pivot_high"] + sc["pivot_low"],
                "recovery_bars": sum(
                    1
                    for r in audit_rows
                    if r.get("combined_primary_label") == "bullish_recovery_inside_bearish_swing"
                ),
                "pullback_bars": sum(
                    1
                    for r in audit_rows
                    if r.get("combined_primary_label") == "bearish_pullback_inside_bullish_swing"
                ),
                "confirmed_bull_rev_bars": sum(
                    1
                    for r in audit_rows
                    if r.get("combined_primary_label") == "confirmed_bullish_swing_reversal"
                ),
                "possible_bull_rev_bars": sum(
                    1
                    for r in audit_rows
                    if r.get("combined_primary_label") == "possible_bullish_swing_reversal"
                ),
            }
        )

    primary = timelines[PRIMARY_SWING]
    eng = engines[PRIMARY_SWING]
    a0, a1 = _ts(AUDIT_START), _ts(AUDIT_END)

    pivots = [p.to_dict() for p in eng.all_pivots if a0 <= _ts(p.available_from_timestamp_utc) <= a1]
    levels = [lv.to_dict() for lv in eng.all_levels if a0 <= _ts(lv.activated_timestamp) <= a1]
    events = [e.to_dict() for e in eng.all_events if a0 <= _ts(e.event_decision_timestamp) <= a1]

    _write_csv(OUT / "pivot_events.csv", pivots)
    _write_csv(OUT / "active_structure_levels.csv", levels)
    _write_csv(OUT / "structure_break_events.csv", events)

    wick_only = [
        lv
        for lv in levels
        if lv.get("crossed_by_wick") and not lv.get("crossed_by_close")
    ]
    close_breaks = [e for e in events]
    failed = [e for e in events if e.get("failed_break_later")]
    retest = [e for e in events if e.get("retest_held_later")]
    _write_csv(OUT / "wick_only_breaks.csv", wick_only)
    _write_csv(OUT / "close_breaks.csv", close_breaks)
    _write_csv(OUT / "failed_break_cases.csv", failed)
    _write_csv(OUT / "retest_hold_cases.csv", retest)

    disagreements = [
        r
        for r in primary
        if int(r.get("internal_bias") or 0) != 0
        and int(r.get("swing_bias") or 0) != 0
        and int(r["internal_bias"]) != int(r["swing_bias"])
    ]
    _write_csv(OUT / "internal_vs_swing_disagreements.csv", disagreements)
    _write_csv(OUT / "variant_comparison.csv", variant_stats)

    for name, (lo, hi) in FOCUS.items():
        lo_t = _ts(lo)
        hi_t = _ts(hi) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        focus = [r for r in primary if lo_t <= _ts(r["decision_timestamp_utc"]) <= hi_t]
        _write_csv(OUT / f"{name}_detail.csv", focus)

    jan = jan13_15_answers(primary, events)
    _write_json(OUT / "jan13_15_answers.json", jan)

    # optional 4h swing context (read-only, not wired into primary decisions)
    rows4, eng4 = run_multilevel_structure(agg4, internal_size=5, swing_size=20, timeframe="4h")
    rows4_audit = filter_audit(rows4)
    _write_csv(OUT / "optional_4h_swing20_timeline.csv", rows4_audit)

    for month in (1, 2, 3):
        (OUT / f"multilevel_structure_review_2026_{month:02d}.pine").write_text(
            build_pine(month, primary, pivots, events),
            encoding="utf-8",
        )

    hashes_after = {n: _md5(ROOT / n) for n in PROTECTED}
    for n, exp in PROTECTED.items():
        if hashes_after[n] != exp or hashes_after[n] != hashes_before[n]:
            raise SystemExit(f"protected hash changed: {n}")

    # Lookahead check sample: prefix equality
    prefix = agg30.iloc[:120].copy()
    p_rows, _ = run_multilevel_structure(prefix, internal_size=5, swing_size=50)
    full_pref = timelines[50]
    # compare overlapping decision times in audit after warmup
    ok_lookahead = True
    pref_map = {r["decision_timestamp_utc"]: r for r in p_rows}
    for r in primary[:50]:
        key = r["decision_timestamp_utc"]
        if key in pref_map:
            # only if that decision existed in prefix run
            pass
    # stronger: first 80 bars of full vs prefix
    full_all, _ = run_multilevel_structure(agg30, internal_size=5, swing_size=50)
    for a, b in zip(p_rows, full_all[: len(p_rows)]):
        if a["internal_bias"] != b["internal_bias"] or a["swing_bias"] != b["swing_bias"]:
            ok_lookahead = False
            break

    # Decision
    if ok_lookahead and jan.get("n_internal_bull_swing_bear_bars", 0) > 0:
        n_dis = len(disagreements)
        n_rec = sum(
            1
            for r in primary
            if r.get("combined_primary_label") == "bullish_recovery_inside_bearish_swing"
        )
        jan_rec = int(jan.get("n_recovery_bars") or 0)
        jan_confirmed_bull = int(jan.get("n_confirmed_reversal_bars") or 0)
        # treat only confirmed_bullish as macro-up false positive
        jan_confirmed_bull = sum(
            1
            for r in primary
            if _ts("2026-01-13") <= _ts(r["decision_timestamp_utc"]) <= _ts("2026-01-15 23:59:59")
            and r.get("combined_primary_label") == "confirmed_bullish_swing_reversal"
        )
        if jan_rec > 0 and jan_confirmed_bull == 0 and n_dis > 0 and n_rec > 0:
            decision = "J"
            reason = (
                "Internal/Swing stay causally separated; Jan 13–15 prints bullish recovery inside "
                "bearish swing rather than confirmed macro uptrend; no lookahead."
            )
            jan["bullish_move_appears_as_recovery_not_macro_uptrend"] = True
        elif jan_rec > 0 and jan_confirmed_bull == 0:
            decision = "J"
            reason = (
                "Jan 13–15 recovery classification works; disagreement coverage thinner but causal."
            )
            jan["bullish_move_appears_as_recovery_not_macro_uptrend"] = True
        else:
            decision = "U"
            reason = "Jan 13–15 recovery classification incomplete or unstable across swing lengths."
    elif not ok_lookahead:
        decision = "N"
        reason = "Lookahead / non-causal behavior detected."
    else:
        decision = "U"
        reason = "Jan 13–15 recovery classification incomplete or unstable across swing lengths."

    summary = {
        "decision": decision,
        "decision_reason": reason,
        "feather_path": str(path),
        "audit_window": {"start": AUDIT_START, "end": AUDIT_END},
        "warmup_from": LOAD_START,
        "primary_config": {"internal": INTERNAL_SIZE, "swing": PRIMARY_SWING},
        "variant_comparison": variant_stats,
        "jan13_15": jan,
        "event_totals_primary": {
            "pivots": len(pivots),
            "levels": len(levels),
            "breaks": len(events),
            "wick_only_levels": len(wick_only),
            "failed_breaks": len(failed),
            "retest_holds": len(retest),
            "internal_vs_swing_disagreement_bars": len(disagreements),
        },
        "lookahead_prefix_ok": ok_lookahead,
        "protected_hashes_before": hashes_before,
        "protected_hashes_after": hashes_after,
        "optional_4h_bars": len(rows4_audit),
    }
    _write_json(OUT / "summary.json", summary)
    _write_json(
        OUT / "audit_metadata.json",
        {
            "phase": "A",
            "module": "multilevel_market_structure.py",
            "closed_buckets_only": True,
            "no_lookahead": ok_lookahead,
            "pivot_timestamps": [
                "extreme_timestamp_utc",
                "confirmation_timestamp_utc",
                "available_from_timestamp_utc",
            ],
            "bos_choch_on_close_only": True,
            "one_event_per_level": True,
            "protected_hashes": hashes_after,
            "policy_wiring": False,
            "state_machine_wiring": False,
        },
    )

    readme = f"""# Multilevel market structure audit (Phase A)

Isolated read-only research. No policy / state-machine wiring.

## Decision

**{decision}** — {reason}

## Primary config

Internal length={INTERNAL_SIZE}, Swing length={PRIMARY_SWING}

## Jan 13–15

```json
{json.dumps(jan, indent=2, default=str)}
```

## Variant comparison

See `variant_comparison.csv`.

## Safety

- closed 30m buckets only
- decisions use `available_from` / `event_decision_timestamp` (never extreme alone)
- wick ≠ BOS/CHoCH
- protected hashes unchanged
"""
    (OUT / "README.md").write_text(readme, encoding="utf-8")
    _p(f"done decision={decision}")


if __name__ == "__main__":
    main()
