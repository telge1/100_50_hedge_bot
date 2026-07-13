#!/usr/bin/env python3
"""Read-only macro-stability audit on top of M2 (4h K2_H4) baseline.

Does not modify market_regime.py or any trading/policy modules.
S0 = raw M2 display mapping. S1–S4 = post-processors that add consolidating /
possible-reversal states to reduce spurious direction flips.

Example:
  PYTHONPATH=. python3 -u research/regime_scanner/market_regime_macro_stability_audit.py
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.market_regime_macro_context_audit import (
    aggregate_closed_htf,
    run_htf_regime_timeline,
)
from research.regime_scanner.point_audit import json_safe

OUT = Path("research/regime_scanner/results/market_regime_macro_stability_audit")
MARKET = Path("research/regime_scanner/market_regime.py")
STRUCTURE = Path("research/regime_scanner/trend_structure.py")
MACHINE = Path("research/regime_scanner/trend_state_machine.py")
POLICY = Path("research/regime_scanner/trend_state_policy.py")
ZONES = Path("research/regime_scanner/trend_zones.py")

LOAD_START = "2025-12-27T00:00:00+00:00"
AUDIT_START = "2026-01-06T00:00:00+00:00"
AUDIT_END = "2026-03-16T23:59:00+00:00"
FOCUS_JAN19_31 = ("2026-01-19T00:00:00+00:00", "2026-01-31T23:59:00+00:00")
FOCUS_JAN29_FEB11 = ("2026-01-29T00:00:00+00:00", "2026-02-11T23:59:00+00:00")
PINE_MONTH = 1  # January chart exports only (per request)

# Display class codes (Pine macroDirs / macroTypes)
BULL_TRENDING = 1
BEAR_TRENDING = 2
BULL_CONSOL = 3
BEAR_CONSOL = 4
TRUE_RANGE = 5
POSSIBLE_REVERSAL = 6

DISPLAY_NAMES = {
    BULL_TRENDING: "bullish_trending",
    BEAR_TRENDING: "bearish_trending",
    BULL_CONSOL: "bullish_consolidating",
    BEAR_CONSOL: "bearish_consolidating",
    TRUE_RANGE: "true_range",
    POSSIBLE_REVERSAL: "possible_reversal",
}

VARIANT_DEFS: dict[str, str] = {
    "S0": "Baseline: raw M2 4h K2_H4 mapped to display classes (no consolidating).",
    "S1": "Soft sticky: non-trend interruptions → consolidating; opposite needs 2 bars to flip; flat clears after 3.",
    "S2": "Strong sticky merge: opposite needs 3 bars; flat clears after 4; short counters stay consolidating.",
    "S3": "Reversal gate: opposite streak consolidating→possible_reversal→flip (3 bars); flat clears after 4.",
    "S4": "Price-confirmed: flip only after 3 opposite strong bars AND adverse close vs run extreme; else consolidating/possible_reversal.",
}


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object) -> str:
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
    cols = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def pine_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def raw_direction(regime: str) -> int:
    if regime == "strong_bullish_trend":
        return 1
    if regime == "strong_bearish_trend":
        return -1
    return 0


def display_direction(code: int) -> int:
    if code in (BULL_TRENDING, BULL_CONSOL):
        return 1
    if code in (BEAR_TRENDING, BEAR_CONSOL):
        return -1
    return 0


def consol_of(bias: int) -> int:
    return BULL_CONSOL if bias > 0 else BEAR_CONSOL


def trending_of(bias: int) -> int:
    return BULL_TRENDING if bias > 0 else BEAR_TRENDING


# ---------------------------------------------------------------------------
# Stability post-processors (bar-by-bar on closed 4h M2 timeline)
# ---------------------------------------------------------------------------


def apply_s0(timeline: list[dict[str, Any]]) -> list[int]:
    out: list[int] = []
    for row in timeline:
        r = row["regime"]
        if r == "strong_bullish_trend":
            out.append(BULL_TRENDING)
        elif r == "strong_bearish_trend":
            out.append(BEAR_TRENDING)
        elif r == "accumulation_range":
            out.append(TRUE_RANGE)
        else:
            out.append(POSSIBLE_REVERSAL)
    return out


@dataclass
class StickyParams:
    opposite_flip_bars: int
    flat_clear_bars: int
    use_possible_reversal_gate: bool = False  # S3: bar before flip = possible_reversal
    price_confirm: bool = False  # S4
    adverse_atr_mult: float = 0.5


def apply_sticky(timeline: list[dict[str, Any]], params: StickyParams) -> list[int]:
    """Shared sticky-direction engine for S1–S4."""
    out: list[int] = []
    bias = 0  # last accepted direction (+1/-1/0)
    opp_streak = 0
    flat_streak = 0
    run_extreme: float | None = None  # protective extreme: low for bull bias, high for bear bias
    atr_proxy: float | None = None

    for row in timeline:
        regime = row["regime"]
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        # crude ATR proxy from bar range (causal, no lookahead)
        bar_range = max(high - low, 1e-12)
        atr_proxy = bar_range if atr_proxy is None else (0.7 * atr_proxy + 0.3 * bar_range)
        rd = raw_direction(regime)

        if bias == 0:
            if rd > 0:
                bias = 1
                opp_streak = 0
                flat_streak = 0
                run_extreme = low
                out.append(BULL_TRENDING)
            elif rd < 0:
                bias = -1
                opp_streak = 0
                flat_streak = 0
                run_extreme = high
                out.append(BEAR_TRENDING)
            elif regime == "accumulation_range":
                flat_streak += 1
                opp_streak = 0
                out.append(TRUE_RANGE)
            else:
                flat_streak += 1
                opp_streak = 0
                out.append(POSSIBLE_REVERSAL)
            continue

        # bias != 0
        if rd == bias:
            opp_streak = 0
            flat_streak = 0
            if bias > 0:
                run_extreme = low if run_extreme is None else min(run_extreme, low)
            else:
                run_extreme = high if run_extreme is None else max(run_extreme, high)
            out.append(trending_of(bias))
            continue

        if rd == -bias:
            # opposite strong
            flat_streak = 0
            opp_streak += 1
            price_ok = True
            if params.price_confirm and run_extreme is not None and atr_proxy is not None:
                buf = params.adverse_atr_mult * atr_proxy
                if bias > 0:
                    # bullish bias breaks only on close through run low
                    price_ok = close < (run_extreme - buf)
                else:
                    # bearish bias breaks only on close through run high
                    price_ok = close > (run_extreme + buf)

            if opp_streak >= params.opposite_flip_bars and (not params.price_confirm or price_ok):
                bias = rd
                opp_streak = 0
                run_extreme = low if bias > 0 else high
                out.append(trending_of(bias))
            elif params.use_possible_reversal_gate and opp_streak >= max(1, params.opposite_flip_bars - 1):
                out.append(POSSIBLE_REVERSAL)
            elif params.price_confirm and opp_streak >= 2 and price_ok:
                out.append(POSSIBLE_REVERSAL)
            else:
                out.append(consol_of(bias))
            continue

        # flat / range / transition while biased
        opp_streak = 0
        flat_streak += 1
        if flat_streak >= params.flat_clear_bars:
            bias = 0
            run_extreme = None
            out.append(TRUE_RANGE if regime == "accumulation_range" else POSSIBLE_REVERSAL)
        else:
            out.append(consol_of(bias))

    return out


def apply_s1(timeline: list[dict[str, Any]]) -> list[int]:
    return apply_sticky(timeline, StickyParams(opposite_flip_bars=2, flat_clear_bars=3))


def apply_s2(timeline: list[dict[str, Any]]) -> list[int]:
    return apply_sticky(timeline, StickyParams(opposite_flip_bars=3, flat_clear_bars=4))


def apply_s3(timeline: list[dict[str, Any]]) -> list[int]:
    return apply_sticky(
        timeline,
        StickyParams(opposite_flip_bars=3, flat_clear_bars=4, use_possible_reversal_gate=True),
    )


def apply_s4(timeline: list[dict[str, Any]]) -> list[int]:
    return apply_sticky(
        timeline,
        StickyParams(
            opposite_flip_bars=3,
            flat_clear_bars=5,
            use_possible_reversal_gate=True,
            price_confirm=True,
            adverse_atr_mult=0.5,
        ),
    )


APPLIERS: dict[str, Callable[[list[dict[str, Any]]], list[int]]] = {
    "S0": apply_s0,
    "S1": apply_s1,
    "S2": apply_s2,
    "S3": apply_s3,
    "S4": apply_s4,
}


# ---------------------------------------------------------------------------
# Interval collapse + metrics
# ---------------------------------------------------------------------------


def collapse_intervals(
    timeline: list[dict[str, Any]],
    codes: list[int],
) -> list[dict[str, Any]]:
    assert len(timeline) == len(codes)
    if not codes:
        return []
    out: list[dict[str, Any]] = []
    start_i = 0
    for i in range(1, len(codes) + 1):
        if i < len(codes) and codes[i] == codes[start_i]:
            continue
        chunk_tl = timeline[start_i:i]
        code = codes[start_i]
        if i < len(timeline):
            end_ts = _ts(timeline[i]["decision_time"])
        else:
            end_ts = _ts(chunk_tl[-1]["decision_time"]) + pd.Timedelta(hours=4)
        out.append(
            {
                "start_utc": _iso(chunk_tl[0]["decision_time"]),
                "end_utc": _iso(end_ts),
                "display_code": code,
                "display_class": DISPLAY_NAMES[code],
                "direction": display_direction(code),
                "n_4h_bars": len(chunk_tl),
                "raw_regimes": "|".join(sorted({r["regime"] for r in chunk_tl})),
            }
        )
        start_i = i
    return out


def _window_mask(intervals: list[dict[str, Any]], start: str, end: str) -> list[dict[str, Any]]:
    a, b = _ts(start), _ts(end)
    return [r for r in intervals if _ts(r["end_utc"]) > a and _ts(r["start_utc"]) <= b]


def metrics_for(
    variant: str,
    timeline: list[dict[str, Any]],
    codes: list[int],
    intervals: list[dict[str, Any]],
    s0_codes: list[int],
) -> dict[str, Any]:
    dirs = [display_direction(c) for c in codes]

    # Direction change = last nonzero side flips when a new nonzero side appears
    # (bear → flat → bull counts as one direction change).
    direction_changes = 0
    last_nonzero: int | None = None
    for d in dirs:
        if d == 0:
            continue
        if last_nonzero is not None and d != last_nonzero:
            direction_changes += 1
        last_nonzero = d

    # Neutral/flat change = enter or leave direction==0
    neutral_flat_changes = sum(1 for a, b in zip(dirs, dirs[1:]) if (a == 0) != (b == 0))

    # Immediate opposite flips without intervening flat (diagnostic)
    immediate_opposite_flips = sum(1 for a, b in zip(dirs, dirs[1:]) if a * b == -1)

    # average directional run length (bars with same nonzero sign, flats break the run)
    dir_lens: list[int] = []
    i = 0
    while i < len(dirs):
        if dirs[i] == 0:
            i += 1
            continue
        j = i + 1
        while j < len(dirs) and dirs[j] == dirs[i]:
            j += 1
        dir_lens.append(j - i)
        i = j
    avg_dir_duration = float(sum(dir_lens) / len(dir_lens)) if dir_lens else 0.0

    s0_dirs = [display_direction(c) for c in s0_codes]

    # Merged continuations: S0 leaves a nonzero side (to flat or opposite), but variant
    # keeps the same nonzero side via consolidating/trending on that bar.
    merged = 0
    for i in range(1, len(s0_dirs)):
        if s0_dirs[i - 1] != 0 and s0_dirs[i] != s0_dirs[i - 1]:
            if dirs[i] == s0_dirs[i - 1]:
                merged += 1

    # Delayed true reversals: when S0's last-nonzero side flips at index i,
    # measure how many bars later the variant's last-nonzero side matches.
    delayed_rev = 0
    delay_bars: list[int] = []
    s0_last: int | None = None
    for i, d0 in enumerate(s0_dirs):
        if d0 == 0:
            continue
        if s0_last is not None and d0 != s0_last:
            # S0 flipped side at i; find variant acceptance
            k = i
            while k < len(dirs) and dirs[k] != d0:
                k += 1
            if k >= len(dirs):
                # suppressed entirely
                pass
            elif k > i:
                delayed_rev += 1
                delay_bars.append(k - i)
        s0_last = d0

    consol_bars = sum(1 for c in codes if c in (BULL_CONSOL, BEAR_CONSOL))
    rev_bars = sum(1 for c in codes if c == POSSIBLE_REVERSAL)
    class_counts = Counter(DISPLAY_NAMES[c] for c in codes)

    def focus_stats(label: str, window: tuple[str, str]) -> dict[str, Any]:
        sub = _window_mask(intervals, window[0], window[1])
        a, b = _ts(window[0]), _ts(window[1])
        idxs = [i for i, r in enumerate(timeline) if a <= _ts(r["decision_time"]) <= b]
        sub_dirs = [dirs[i] for i in idxs]
        dchg = 0
        last_nz: int | None = None
        for d in sub_dirs:
            if d == 0:
                continue
            if last_nz is not None and d != last_nz:
                dchg += 1
            last_nz = d
        nchg = sum(1 for x, y in zip(sub_dirs, sub_dirs[1:]) if (x == 0) != (y == 0))
        return {
            "label": label,
            "n_intervals": len(sub),
            "direction_changes": dchg,
            "neutral_flat_changes": nchg,
            "class_counts": dict(Counter(r["display_class"] for r in sub)),
            "intervals": [
                {
                    "start_utc": r["start_utc"],
                    "end_utc": r["end_utc"],
                    "display_class": r["display_class"],
                    "n_4h_bars": r["n_4h_bars"],
                }
                for r in sub
            ],
        }

    return {
        "variant": variant,
        "definition": VARIANT_DEFS[variant],
        "n_4h_bars": len(codes),
        "n_intervals": len(intervals),
        "direction_changes": direction_changes,
        "neutral_flat_changes": neutral_flat_changes,
        "immediate_opposite_flips": immediate_opposite_flips,
        "avg_direction_duration_4h_bars": avg_dir_duration,
        "merged_continuations_vs_s0": merged,
        "delayed_true_reversals_vs_s0": delayed_rev,
        "median_reversal_delay_4h_bars": float(pd.Series(delay_bars).median()) if delay_bars else None,
        "consolidating_bars": consol_bars,
        "possible_reversal_bars": rev_bars,
        "class_counts": dict(class_counts),
        "focus_jan19_31": focus_stats("jan19_31", FOCUS_JAN19_31),
        "focus_jan29_feb11": focus_stats("jan29_feb11", FOCUS_JAN29_FEB11),
    }


# ---------------------------------------------------------------------------
# Pine (macro background only)
# ---------------------------------------------------------------------------


def build_stability_pine(variant: str, intervals: list[dict[str, Any]], month: int = 1) -> str:
    start = _ts(f"2026-{month:02d}-01T00:00:00+00:00")
    end = start + pd.offsets.MonthBegin(1)
    month_iv = [
        r
        for r in intervals
        if _ts(r["end_utc"]) > start and _ts(r["start_utc"]) < end
    ]

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

    title = f"M2 Macro Stability {variant} 2026-{month:02d}"
    return f"""//@version=6
indicator(
     "{pine_escape(title)}",
     overlay = true,
     max_labels_count = 500,
     max_lines_count = 100
)

// Macro-stability display only (no local segments / bounce / aligned labels).
// Variant {variant}: {pine_escape(VARIANT_DEFS[variant])}
// Timestamp UTC: start = 4h decision_time; end = exclusive next-run decision_time.
// Display codes: 1 bull trending, 2 bear trending, 3 bull consol, 4 bear consol, 5 true range, 6 possible reversal.

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
          activeType == 3 ? color.new(#66bb6a, math.min(macroTransparency + 5, 95)) :
          activeType == 4 ? color.new(#ef9a9a, math.min(macroTransparency + 5, 95)) :
          activeType == 5 ? color.new(color.gray, math.min(macroTransparency + 4, 95)) :
          activeType == 6 ? color.new(color.orange, math.min(macroTransparency + 2, 95)) :
          na) :
     na
)

f_label() =>
    activeType == 1 ? "BULL TREND" :
     activeType == 2 ? "BEAR TREND" :
     activeType == 3 ? "BULL CONSOL" :
     activeType == 4 ? "BEAR CONSOL" :
     activeType == 5 ? "TRUE RANGE" :
     activeType == 6 ? "POSSIBLE REV" :
     ""

if showLabels and macroStartBar and activeType != 0
    label.new(
         bar_index,
         high,
         f_label(),
         style = label.style_label_down,
         color = color.new(color.black, 40),
         textcolor = color.white,
         size = size.tiny
     )

// EOF
"""


def rebuild_4h_timeline() -> list[dict[str, Any]]:
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
    return run_htf_regime_timeline(ind4)


def filter_audit_window(timeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    a, b = _ts(AUDIT_START), _ts(AUDIT_END)
    return [r for r in timeline if a <= _ts(r["decision_time"]) <= b]


def write_recommendation(metrics: list[dict[str, Any]]) -> str:
    # Prefer fewer direction flips without collapsing everything to flat;
    # score = -direction_changes + 0.3*merged - 0.2*delayed_penalty + avg_duration/10
    scored = []
    for m in metrics:
        if m["variant"] == "S0":
            continue
        delay_pen = m["delayed_true_reversals_vs_s0"] or 0
        score = (
            -m["direction_changes"]
            + 0.35 * m["merged_continuations_vs_s0"]
            + 0.15 * m["avg_direction_duration_4h_bars"]
            - 0.1 * delay_pen
        )
        scored.append((score, m))
    scored.sort(key=lambda x: x[0], reverse=True)
    best = scored[0][1] if scored else metrics[0]
    lines = [
        "# Macro stability audit (read-only)",
        "",
        "**No variant adopted.** Compare S0–S4 on chart before any policy wiring.",
        "",
        f"Lowest-flip candidate by heuristic score: **{best['variant']}** "
        f"(direction_changes={best['direction_changes']}, "
        f"avg_dir_duration={best['avg_direction_duration_4h_bars']:.2f}, "
        f"merged_vs_s0={best['merged_continuations_vs_s0']}).",
        "",
        "## Variant definitions",
        "",
    ]
    for k, v in VARIANT_DEFS.items():
        lines.append(f"- **{k}**: {v}")
    lines += [
        "",
        "## Comparison (full audit window)",
        "",
        "| Variant | Dir changes | Neutral/flat changes | Avg dir duration (4h bars) | Merged cont. | Delayed rev | Consol bars |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for m in metrics:
        lines.append(
            f"| {m['variant']} | {m['direction_changes']} | {m['neutral_flat_changes']} | "
            f"{m['avg_direction_duration_4h_bars']:.2f} | {m['merged_continuations_vs_s0']} | "
            f"{m['delayed_true_reversals_vs_s0']} | {m['consolidating_bars']} |"
        )
    lines += [
        "",
        "## Focus windows",
        "",
    ]
    for m in metrics:
        f1 = m["focus_jan19_31"]
        f2 = m["focus_jan29_feb11"]
        lines.append(
            f"- **{m['variant']}** Jan19–31: dir_chg={f1['direction_changes']}, "
            f"flat_chg={f1['neutral_flat_changes']}, intervals={f1['n_intervals']}; "
            f"Jan29–Feb11: dir_chg={f2['direction_changes']}, "
            f"flat_chg={f2['neutral_flat_changes']}, intervals={f2['n_intervals']}"
        )
    lines += [
        "",
        "## Pine",
        "",
        "Primary: January files `market_regime_macro_stability_s{0..4}_2026_01.pine` — macro bgcolor only, labels off by default.",
        "",
        "Start with S0 vs best candidate side-by-side on APTUSDT 30m UTC.",
        "",
    ]
    return "\n".join(lines) + "\n"


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

    print("Rebuilding closed 4h M2 timeline…")
    full_tl = rebuild_4h_timeline()
    timeline = filter_audit_window(full_tl)
    print(f"4h bars in audit window: {len(timeline)}")

    raw_rows = [
        {
            "decision_time": _iso(r["decision_time"]),
            "candle_open": _iso(r["candle_open"]),
            "regime": r["regime"],
            "close": r["close"],
            "high": r["high"],
            "low": r["low"],
        }
        for r in timeline
    ]
    _write_csv(OUT / "m2_4h_raw_timeline.csv", raw_rows)

    s0_codes = apply_s0(timeline)
    all_metrics: list[dict[str, Any]] = []
    pine_paths: dict[str, str] = {}
    comparison_rows: list[dict[str, Any]] = []

    for variant, applier in APPLIERS.items():
        codes = applier(timeline)
        assert len(codes) == len(timeline)
        intervals = collapse_intervals(timeline, codes)
        bar_rows = []
        for r, c in zip(timeline, codes):
            bar_rows.append(
                {
                    "variant": variant,
                    "decision_time": _iso(r["decision_time"]),
                    "raw_regime": r["regime"],
                    "display_code": c,
                    "display_class": DISPLAY_NAMES[c],
                    "direction": display_direction(c),
                    "close": r["close"],
                }
            )
        _write_csv(OUT / f"timeline_{variant.lower()}.csv", bar_rows)
        _write_csv(
            OUT / f"intervals_{variant.lower()}.csv",
            [{**iv, "variant": variant} for iv in intervals],
        )

        m = metrics_for(variant, timeline, codes, intervals, s0_codes)
        all_metrics.append(m)
        comparison_rows.append(
            {
                "variant": variant,
                "direction_changes": m["direction_changes"],
                "neutral_flat_changes": m["neutral_flat_changes"],
                "immediate_opposite_flips": m["immediate_opposite_flips"],
                "avg_direction_duration_4h_bars": m["avg_direction_duration_4h_bars"],
                "merged_continuations_vs_s0": m["merged_continuations_vs_s0"],
                "delayed_true_reversals_vs_s0": m["delayed_true_reversals_vs_s0"],
                "median_reversal_delay_4h_bars": m["median_reversal_delay_4h_bars"],
                "consolidating_bars": m["consolidating_bars"],
                "possible_reversal_bars": m["possible_reversal_bars"],
                "n_intervals": m["n_intervals"],
                "jan19_31_direction_changes": m["focus_jan19_31"]["direction_changes"],
                "jan19_31_neutral_flat_changes": m["focus_jan19_31"]["neutral_flat_changes"],
                "jan19_31_n_intervals": m["focus_jan19_31"]["n_intervals"],
                "jan29_feb11_direction_changes": m["focus_jan29_feb11"]["direction_changes"],
                "jan29_feb11_neutral_flat_changes": m["focus_jan29_feb11"]["neutral_flat_changes"],
                "jan29_feb11_n_intervals": m["focus_jan29_feb11"]["n_intervals"],
            }
        )

        pine = build_stability_pine(variant, intervals, month=PINE_MONTH)
        pine_path = OUT / f"market_regime_macro_stability_{variant.lower()}_2026_{PINE_MONTH:02d}.pine"
        pine_path.write_text(pine, encoding="utf-8")
        pine_paths[variant] = str(pine_path)
        assert pine.lstrip().startswith("//@version=6")
        assert "box.new" not in pine and "1.0e10" not in pine
        assert 'timestamp("UTC"' in pine
        assert "showLabels = input.bool(false" in pine
        print(f"{variant}: intervals={len(intervals)} dir_chg={m['direction_changes']} pine={pine_path.name}")

    _write_csv(OUT / "variant_comparison.csv", comparison_rows)
    _write_json(OUT / "metrics_by_variant.json", all_metrics)

    # Focus narrative CSVs
    focus_rows = []
    for m in all_metrics:
        for key in ("focus_jan19_31", "focus_jan29_feb11"):
            f = m[key]
            for iv in f["intervals"]:
                focus_rows.append({"variant": m["variant"], "window": f["label"], **iv})
    _write_csv(OUT / "focus_window_intervals.csv", focus_rows)

    rec = write_recommendation(all_metrics)
    (OUT / "final_recommendation.md").write_text(rec, encoding="utf-8")
    (OUT / "README.md").write_text(
        "# Market regime macro stability audit\n\n"
        "Read-only post-processing of M2 (closed 4h K2_H4) into stable display classes.\n\n"
        "Does **not** change `market_regime.py`. No variant adopted.\n\n"
        "Reproduce:\n\n"
        "```bash\nPYTHONPATH=. python3 -u research/regime_scanner/market_regime_macro_stability_audit.py\n```\n",
        encoding="utf-8",
    )

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
        "audit": "market_regime_macro_stability_audit",
        "baseline": "M2 closed 4h K2_H4 (S0)",
        "variants": list(APPLIERS),
        "variant_definitions": VARIANT_DEFS,
        "display_classes": DISPLAY_NAMES,
        "audit_window": {"start": AUDIT_START, "end": AUDIT_END},
        "n_4h_bars": len(timeline),
        "comparison": comparison_rows,
        "pine_files": pine_paths,
        "no_variant_adopted": True,
        "market_regime_unchanged": True,
        "hashes": hashes_after,
    }
    _write_json(OUT / "summary.json", summary)

    print(json.dumps({"comparison": comparison_rows, "pine": pine_paths}, indent=2))
    print("Wrote", OUT)
    print("hashes ok", hashes_before == hashes_after)


if __name__ == "__main__":
    main()
