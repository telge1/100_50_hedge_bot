#!/usr/bin/env python3
"""Export TradingView Pine scripts for M2 (4h macro) chart review.

Read-only. Uses existing macro-context audit CSVs + rebuilt closed 4h timeline
for continuous macro backgrounds. Does not modify market_regime.py.

Example:
  PYTHONPATH=. python3 -u research/regime_scanner/market_regime_m2_pine_export.py
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
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
from research.regime_scanner.point_audit import json_safe

OUT = Path("research/regime_scanner/results/market_regime_macro_context_audit")
SRC_CSV = OUT / "strong_segments_with_macro_context.csv"
MARKET = Path("research/regime_scanner/market_regime.py")
STRUCTURE = Path("research/regime_scanner/trend_structure.py")
MACHINE = Path("research/regime_scanner/trend_state_machine.py")
POLICY = Path("research/regime_scanner/trend_state_policy.py")
ZONES = Path("research/regime_scanner/trend_zones.py")

LOAD_START = "2025-12-27T00:00:00+00:00"
AUDIT_END = "2026-03-16T23:59:00+00:00"
SMOKE_IDS = ("REVIEW_0148", "REVIEW_0170", "REVIEW_0336", "REVIEW_0340")

# localTypes in Pine
LT_ALIGNED_UP = 1
LT_ALIGNED_DOWN = 2
LT_BULL_BOUNCE = 3
LT_BEAR_BOUNCE = 4
LT_REVERSAL = 5
# Smoke / reference-only overlays (range_impulse under flat macro — not bounce)
LT_OTHER_UP = 6
LT_OTHER_DOWN = 7


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


def load_m2_locals() -> list[dict[str, Any]]:
    rows = list(csv.DictReader(SRC_CSV.open()))
    m2 = [r for r in rows if r.get("variant") == "M2"]
    if not m2:
        raise SystemExit("No M2 rows in strong_segments_with_macro_context.csv")
    # Only plot types requested for local overlay; range_impulse/unclear still contribute to counts elsewhere
    return sorted(m2, key=lambda r: _ts(r["local_start_utc"]))


def local_type(row: dict[str, Any], *, include_other: bool = False) -> int | None:
    cls = row["classification"]
    direction = row["local_direction"]
    if cls == "possible_macro_reversal":
        return LT_REVERSAL
    if cls == "countertrend_bounce":
        return LT_BULL_BOUNCE if direction == "UPTREND" else LT_BEAR_BOUNCE
    if cls in {"aligned_breakout", "aligned_continuation"}:
        return LT_ALIGNED_UP if direction == "UPTREND" else LT_ALIGNED_DOWN
    if include_other:
        return LT_OTHER_UP if direction == "UPTREND" else LT_OTHER_DOWN
    return None  # range_impulse / unclear → no local overlay on monthly charts


def macro_dir(regime: str) -> int:
    if regime == "strong_bullish_trend":
        return 1
    if regime == "strong_bearish_trend":
        return -1
    return 0


def build_macro_intervals(timeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse contiguous 4h classifier regimes into intervals.

    start = decision_time of first HTF bar in run (confirmed close)
    end   = decision_time of first bar of the *next* run (exclusive upper bound),
            or last decision_time + 4h for the final run.

    This keeps the macro bgcolor continuous on the 30m chart until the next
    closed 4h bar confirms a regime change (no lookahead).
    """
    if not timeline:
        return []
    out: list[dict[str, Any]] = []
    start_i = 0
    for i in range(1, len(timeline) + 1):
        if i < len(timeline) and timeline[i]["regime"] == timeline[start_i]["regime"]:
            continue
        chunk = timeline[start_i:i]
        regime = chunk[0]["regime"]
        if i < len(timeline):
            end_ts = _ts(timeline[i]["decision_time"])
        else:
            end_ts = _ts(chunk[-1]["decision_time"]) + pd.Timedelta(hours=4)
        out.append(
            {
                "start_utc": _iso(chunk[0]["decision_time"]),
                "end_utc": _iso(end_ts),
                "regime": regime,
                "macro_dir": macro_dir(regime),
                "n_4h_bars": len(chunk),
                "end_exclusive": True,
            }
        )
        start_i = i
    return out


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


def pine_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _chunk_pushes(
    name_prefix: str,
    items: list[tuple[str, ...]],
    push_lines_fn,
    chunk_size: int = 10,
) -> tuple[list[str], list[str]]:
    helpers: list[str] = []
    calls: list[str] = []
    if not items:
        helpers.append(f"{name_prefix}_00() =>\n    true")
        calls.append(f"    {name_prefix}_00()")
        return helpers, calls
    for i in range(0, len(items), chunk_size):
        chunk = items[i : i + chunk_size]
        fn = f"{name_prefix}_{i // chunk_size:02d}"
        body = [f"{fn}() =>"] + push_lines_fn(chunk)
        helpers.append("\n".join(body))
        calls.append(f"    {fn}()")
    return helpers, calls


def build_m2_pine(
    *,
    title: str,
    macros: list[dict[str, Any]],
    locals_: list[dict[str, Any]],
    include_other_locals: bool = False,
) -> str:
    """Pine v6: macro bgcolor + local plotshape/labels. No giant boxes."""

    macro_items = [
        (
            _ts(m["start_utc"]),
            _ts(m["end_utc"]),
            int(m["macro_dir"]),
        )
        for m in macros
    ]
    local_items = []
    for r in locals_:
        lt = local_type(r, include_other=include_other_locals)
        if lt is None:
            continue
        local_items.append(
            (
                _ts(r["local_start_utc"]),
                _ts(r["local_end_utc"]),
                lt,
                r["review_id"],
            )
        )

    def macro_pushes(chunk):
        lines = []
        for s, e, d in chunk:
            lines.append(
                f"    array.push(macroStarts, f_ts({s.year}, {s.month}, {s.day}, {s.hour}, {s.minute}))"
            )
            lines.append(
                f"    array.push(macroEnds, f_ts({e.year}, {e.month}, {e.day}, {e.hour}, {e.minute}))"
            )
            lines.append(f"    array.push(macroDirs, {d})")
        return lines

    def local_pushes(chunk):
        lines = []
        for s, e, lt, rid in chunk:
            lines.append(
                f"    array.push(localStarts, f_ts({s.year}, {s.month}, {s.day}, {s.hour}, {s.minute}))"
            )
            lines.append(
                f"    array.push(localEnds, f_ts({e.year}, {e.month}, {e.day}, {e.hour}, {e.minute}))"
            )
            lines.append(f"    array.push(localTypes, {lt})")
            lines.append(f'    array.push(reviewIds, "{pine_escape(rid)}")')
        return lines

    mh, mc = _chunk_pushes("f_load_macro", macro_items, macro_pushes, 8)
    lh, lc = _chunk_pushes("f_load_local", local_items, local_pushes, 8)

    return f"""//@version=6
indicator(
     "{pine_escape(title)}",
     overlay = true,
     max_labels_count = 500,
     max_lines_count = 100
)

// K2_H4 local strong + M2 4H macro context (read-only audit visualization).
// No regime calculation in Pine — arrays copied from audit CSVs / closed 4h timeline.
//
// Timestamp semantics (UTC):
//   macroStarts       = 4h classifier decision_time (= close of first 4h bar in run)
//   macroEnds         = exclusive upper bound = next run's first decision_time
//                       (or last decision + 4h). Paint: time_close >= start and < end.
//   localStarts       = local_start_utc  (= 30m decision_time / strong start)
//   localEnds         = local_end_utc    (= end_candle_close_utc of last 30m bar)
// Chart: APTUSDT 30m preferred. Exchange/chart timezone should be UTC.

showMacroBackground = input.bool(true, "Show 4H macro background")
showAlignedLocal = input.bool(true, "Show aligned local trends")
showCountertrendBounces = input.bool(true, "Show countertrend bounces")
showLabels = input.bool(true, "Show labels")
showReviewIds = input.bool(true, "Show review IDs")
macroTransparency = input.int(88, "Macro transparency", minval = 70, maxval = 95)
localTransparency = input.int(70, "Local transparency", minval = 50, maxval = 95)

f_ts(y, m, d, h, mi) =>
    timestamp("UTC", y, m, d, h, mi)

var int[] macroStarts = array.new_int()
var int[] macroEnds = array.new_int()
var int[] macroDirs = array.new_int()

var int[] localStarts = array.new_int()
var int[] localEnds = array.new_int()
var int[] localTypes = array.new_int()
var string[] reviewIds = array.new_string()

{chr(10).join(mh)}

{chr(10).join(lh)}

if barstate.isfirst
{chr(10).join(mc)}
{chr(10).join(lc)}

// --- Active macro (closed-bar semantics via time_close; end exclusive) ---
int activeMacroDir = 0
bool macroStartBar = false
if array.size(macroStarts) > 0
    for i = 0 to array.size(macroStarts) - 1
        int ms = array.get(macroStarts, i)
        int me = array.get(macroEnds, i)
        if time_close >= ms and time_close < me
            activeMacroDir := array.get(macroDirs, i)
        if time_close == ms
            macroStartBar := true
            activeMacroDir := array.get(macroDirs, i)

bgcolor(
     showMacroBackground ?
         (activeMacroDir == 1 ? color.new(color.green, macroTransparency) :
          activeMacroDir == -1 ? color.new(color.red, macroTransparency) :
          activeMacroDir == 0 ? color.new(color.gray, math.min(macroTransparency + 4, 95)) :
          na) :
     na
)

// --- Active local overlay type ---
int activeLocalType = 0
string activeReviewId = ""
bool localStartBar = false
if array.size(localStarts) > 0
    for i = 0 to array.size(localStarts) - 1
        int ls = array.get(localStarts, i)
        int le = array.get(localEnds, i)
        int lt = array.get(localTypes, i)
        bool showThis =
             ((lt == 1 or lt == 2) and showAlignedLocal) or
             ((lt == 3 or lt == 4) and showCountertrendBounces) or
             (lt == 5) or
             (lt == 6 or lt == 7)
        if showThis and time_close >= ls and time_close <= le
            activeLocalType := lt
            activeReviewId := array.get(reviewIds, i)
        if showThis and time_close == ls
            localStartBar := true
            activeLocalType := lt
            activeReviewId := array.get(reviewIds, i)

// Local candle tint (does not replace macro bgcolor — overlays via barcolor)
barcolor(
     activeLocalType == 1 ? color.new(color.lime, localTransparency) :
     activeLocalType == 2 ? color.new(color.maroon, localTransparency) :
     activeLocalType == 3 ? color.new(color.yellow, localTransparency) :
     activeLocalType == 4 ? color.new(color.orange, localTransparency) :
     activeLocalType == 5 ? color.new(color.aqua, localTransparency) :
     activeLocalType == 6 ? color.new(color.teal, localTransparency) :
     activeLocalType == 7 ? color.new(color.fuchsia, localTransparency) :
     na
)

plotshape(
     localStartBar and activeLocalType == 1,
     title = "Aligned UP start",
     style = shape.triangleup,
     location = location.belowbar,
     color = color.new(color.lime, 0),
     size = size.tiny
 )
plotshape(
     localStartBar and activeLocalType == 2,
     title = "Aligned DOWN start",
     style = shape.triangledown,
     location = location.abovebar,
     color = color.new(color.maroon, 0),
     size = size.tiny
 )
plotshape(
     localStartBar and activeLocalType == 3,
     title = "Bull bounce start",
     style = shape.circle,
     location = location.belowbar,
     color = color.new(color.yellow, 0),
     size = size.tiny
 )
plotshape(
     localStartBar and activeLocalType == 4,
     title = "Bear bounce start",
     style = shape.circle,
     location = location.abovebar,
     color = color.new(color.orange, 0),
     size = size.tiny
 )
plotshape(
     localStartBar and activeLocalType == 5,
     title = "Possible reversal",
     style = shape.diamond,
     location = location.abovebar,
     color = color.new(color.aqua, 0),
     size = size.small
 )
plotshape(
     localStartBar and activeLocalType == 6,
     title = "Other local UP",
     style = shape.xcross,
     location = location.belowbar,
     color = color.new(color.teal, 0),
     size = size.tiny
 )
plotshape(
     localStartBar and activeLocalType == 7,
     title = "Other local DOWN",
     style = shape.xcross,
     location = location.abovebar,
     color = color.new(color.fuchsia, 0),
     size = size.tiny
 )

f_label_text() =>
    string base =
         activeLocalType == 1 ? "ALIGNED UP" :
         activeLocalType == 2 ? "ALIGNED DOWN" :
         activeLocalType == 3 ? "BULL BOUNCE" :
         activeLocalType == 4 ? "BEAR BOUNCE" :
         activeLocalType == 5 ? "POSSIBLE REVERSAL" :
         activeLocalType == 6 ? "LOCAL UP" :
         activeLocalType == 7 ? "LOCAL DOWN" :
         ""
    showReviewIds and str.length(activeReviewId) > 0 ? base + " " + activeReviewId : base

if showLabels and localStartBar and activeLocalType != 0
    label.new(
         bar_index,
         activeLocalType == 1 or activeLocalType == 3 or activeLocalType == 6 ? low : high,
         f_label_text(),
         style = activeLocalType == 1 or activeLocalType == 3 or activeLocalType == 6 ? label.style_label_up : label.style_label_down,
         color = color.new(color.black, 25),
         textcolor = color.white,
         size = size.tiny
     )

if showLabels and macroStartBar and showMacroBackground
    label.new(
         bar_index,
         high,
         activeMacroDir == 1 ? "MACRO UP" : activeMacroDir == -1 ? "MACRO DOWN" : "MACRO FLAT",
         style = label.style_label_down,
         color = color.new(color.gray, 40),
         textcolor = color.white,
         size = size.tiny
     )

if localStartBar and activeLocalType == 5
    line.new(bar_index, low * 0.999, bar_index, high * 1.001, extend = extend.both, color = color.new(color.aqua, 40), style = line.style_dotted)

// EOF
"""


def filter_month(rows: list[dict[str, Any]], month: int, key: str) -> list[dict[str, Any]]:
    return [r for r in rows if _ts(r[key]).month == month and _ts(r[key]).year == 2026]


def validate(
    macros: list[dict[str, Any]],
    m2: list[dict[str, Any]],
    pine_texts: dict[str, str],
) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    checks["only_m2_source"] = all(r.get("variant") == "M2" for r in m2)

    def find(rid: str) -> dict[str, Any]:
        return next(r for r in m2 if r["review_id"] == rid)

    r148 = find("REVIEW_0148")
    r170 = find("REVIEW_0170")
    r336 = find("REVIEW_0336")
    r340 = find("REVIEW_0340")
    checks["review_0148_bull_bounce_bear_macro"] = (
        r148["classification"] == "countertrend_bounce"
        and r148["local_direction"] == "UPTREND"
        and r148["macro_regime"] == "strong_bearish_trend"
    )
    checks["review_0170_bull_bounce_bear_macro"] = (
        r170["classification"] == "countertrend_bounce"
        and r170["local_direction"] == "UPTREND"
        and r170["macro_regime"] == "strong_bearish_trend"
    )
    checks["review_0336_start"] = r336["local_start_utc"] == "2026-03-05T17:30:00+00:00"
    checks["review_0340_start"] = r340["local_start_utc"] == "2026-03-06T14:30:00+00:00"

    # macro intervals non-overlapping / sorted
    macros_sorted = sorted(macros, key=lambda m: _ts(m["start_utc"]))
    ok_order = True
    ok_overlap = True
    for a, b in zip(macros_sorted, macros_sorted[1:]):
        if _ts(a["start_utc"]) > _ts(b["start_utc"]):
            ok_order = False
        # abutting end==next.start is OK (end exclusive); overlap only if end > next.start
        if _ts(a["end_utc"]) > _ts(b["start_utc"]):
            ok_overlap = False
    checks["macro_sorted"] = ok_order
    checks["macro_no_overlap"] = ok_overlap
    checks["macro_continuous_abut"] = all(
        _ts(a["end_utc"]) == _ts(b["start_utc"])
        for a, b in zip(macros_sorted, macros_sorted[1:])
    )

    for name, text in pine_texts.items():
        checks[f"{name}_version6"] = text.lstrip().startswith("//@version=6")
        checks[f"{name}_utc"] = 'timestamp("UTC"' in text
        checks[f"{name}_no_box_1e10"] = "1.0e10" not in text and "box.new" not in text
        checks[f"{name}_has_bgcolor"] = "bgcolor(" in text

    smoke = pine_texts["smoke"]
    checks["smoke_has_0148"] = "REVIEW_0148" in smoke
    checks["smoke_has_0170"] = "REVIEW_0170" in smoke
    checks["smoke_has_0336"] = "REVIEW_0336" in smoke and "f_ts(2026, 3, 5, 17, 30)" in smoke
    checks["smoke_has_0340"] = "REVIEW_0340" in smoke and "f_ts(2026, 3, 6, 14, 30)" in smoke

    for month, key in ((1, "jan"), (2, "feb"), (3, "mar")):
        text = pine_texts[key]
        # local pushes months
        starts = re.findall(
            r"array\.push\(localStarts, f_ts\((\d+), (\d+),",
            text,
        )
        checks[f"{key}_locals_month_only"] = all(int(y) == 2026 and int(m) == month for y, m in starts) if starts else True

    checks["all_ok"] = all(bool(v) for k, v in checks.items() if k != "all_ok")
    return checks


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    hashes_before = {
        "market_regime.py": _md5(MARKET),
        "trend_structure.py": _md5(STRUCTURE),
        "trend_state_machine.py": _md5(MACHINE),
        "trend_state_policy.py": _md5(POLICY),
        "trend_zones.py": _md5(ZONES),
    }
    _write_json(OUT / "m2_pine_hashes_before.json", hashes_before)

    m2 = load_m2_locals()
    print(f"M2 local strong segments: {len(m2)}")

    tl4 = rebuild_4h_timeline()
    macros = build_macro_intervals(tl4)
    _write_csv(OUT / "m2_macro_intervals_4h.csv", macros)
    print(f"4h macro intervals: {len(macros)}")

    # Locals for plotting (all M2; pine skips None types)
    plot_locals = m2

    by_month = {
        1: filter_month(plot_locals, 1, "local_start_utc"),
        2: filter_month(plot_locals, 2, "local_start_utc"),
        3: filter_month(plot_locals, 3, "local_start_utc"),
    }
    macros_by_month = {
        1: filter_month(macros, 1, "start_utc"),
        2: filter_month(macros, 2, "start_utc"),
        3: filter_month(macros, 3, "start_utc"),
    }
    # Extend macro intervals that span month boundaries into each month file if they overlap
    # Simpler: include any macro whose [start,end] intersects the month
    def macros_overlapping_month(month: int) -> list[dict[str, Any]]:
        start = _ts(f"2026-{month:02d}-01T00:00:00+00:00")
        end = start + pd.offsets.MonthBegin(1)
        out = []
        for m in macros:
            ms, me = _ts(m["start_utc"]), _ts(m["end_utc"])
            if me >= start and ms < end:
                out.append(m)
        return out

    pine_paths = {}
    pine_texts = {}
    for month, key in ((1, "jan"), (2, "feb"), (3, "mar")):
        path = OUT / f"market_regime_m2_chart_review_2026_{month:02d}.pine"
        text = build_m2_pine(
            title=f"K2_H4 + M2 4H Macro Regime Review 2026-{month:02d}",
            macros=macros_overlapping_month(month),
            locals_=by_month[month],
        )
        path.write_text(text, encoding="utf-8")
        pine_paths[key] = str(path)
        pine_texts[key] = text

    smoke_rows = [next(r for r in m2 if r["review_id"] == rid) for rid in SMOKE_IDS]
    # macro intervals covering smoke locals
    smoke_macros = []
    for r in smoke_rows:
        t0, t1 = _ts(r["local_start_utc"]), _ts(r["local_end_utc"])
        for m in macros:
            if _ts(m["start_utc"]) <= t1 and _ts(m["end_utc"]) > t0:
                if m not in smoke_macros:
                    smoke_macros.append(m)
    smoke_path = OUT / "market_regime_m2_smoke_test.pine"
    smoke_text = build_m2_pine(
        title="K2_H4 + M2 4H Macro Regime Review Smoke",
        macros=smoke_macros,
        locals_=smoke_rows,
        include_other_locals=True,
    )
    smoke_path.write_text(smoke_text, encoding="utf-8")
    pine_paths["smoke"] = str(smoke_path)
    pine_texts["smoke"] = smoke_text

    checks = validate(macros, m2, pine_texts)

    # counts for report
    typed = [(r, local_type(r)) for r in m2]
    n_aligned = sum(1 for _, t in typed if t in (LT_ALIGNED_UP, LT_ALIGNED_DOWN))
    n_bounce = sum(1 for _, t in typed if t in (LT_BULL_BOUNCE, LT_BEAR_BOUNCE))
    n_rev = sum(1 for _, t in typed if t == LT_REVERSAL)

    hashes_after = {
        "market_regime.py": _md5(MARKET),
        "trend_structure.py": _md5(STRUCTURE),
        "trend_state_machine.py": _md5(MACHINE),
        "trend_state_policy.py": _md5(POLICY),
        "trend_zones.py": _md5(ZONES),
    }
    assert hashes_before == hashes_after

    summary = {
        "variant": "M2",
        "source_csv": str(SRC_CSV),
        "timestamp_columns": {
            "local_start": "local_start_utc (30m decision_time)",
            "local_end": "local_end_utc (end_candle_close_utc)",
            "macro_start": "4h classifier decision_time of first bar in contiguous run",
            "macro_end": "exclusive: next run first decision_time (or last+4h)",
        },
        "pine_version": 6,
        "macro_intervals_total": len(macros),
        "macro_intervals_by_month": {
            "2026-01": len(macros_overlapping_month(1)),
            "2026-02": len(macros_overlapping_month(2)),
            "2026-03": len(macros_overlapping_month(3)),
        },
        "local_m2_total": len(m2),
        "n_aligned_local": n_aligned,
        "n_countertrend_bounce": n_bounce,
        "n_possible_reversal": n_rev,
        "classification_counts": dict(Counter(r["classification"] for r in m2)),
        "files": pine_paths,
        "validation": checks,
        "smoke_first": str(smoke_path),
        "hashes": hashes_after,
        "market_regime_unchanged": True,
    }
    _write_json(OUT / "m2_pine_export_summary.json", summary)
    _write_json(OUT / "m2_pine_hashes_after.json", hashes_after)

    print(json.dumps({k: summary[k] for k in (
        "macro_intervals_by_month", "n_aligned_local", "n_countertrend_bounce",
        "n_possible_reversal", "smoke_first", "validation"
    )}, indent=2))
    print("all_ok", checks["all_ok"])
    print("FILES:")
    for p in pine_paths.values():
        print(" ", p)


if __name__ == "__main__":
    main()
