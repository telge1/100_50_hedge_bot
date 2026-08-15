"""Generate TradingView Pine v6 overlay from Python level inventory (parity-safe).

Pine does NOT recompute pivots live. It embeds scanner levels as arrays so
visuals match level_preview_expected.csv exactly. No lookahead_on, no extend.both.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

import pandas as pd

from research.regime_scanner.htf_pivot_level_preview.config import (
    AUDIT_VERSION,
    CORE_HTF_SOURCE_TYPES,
    DENSE_HTF_SOURCE_TYPES,
    HTF_PIVOT_SPECS,
    HTF_SOURCE_TYPES,
    HtfPivotPreviewConfig,
    is_htf_source,
)
from research.regime_scanner.trend_pine_export import (
    build_pine_header,
    escape_pine_string,
    validate_pine_script,
)


def _ts_parts(ts: Any) -> tuple[int, int, int, int, int]:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return int(t.year), int(t.month), int(t.day), int(t.hour), int(t.minute)


def _pine_ts(ts: Any) -> str:
    if ts is None or (isinstance(ts, float) and pd.isna(ts)) or str(ts).strip() in {"", "None", "nan"}:
        return "na"
    y, m, d, h, mi = _ts_parts(ts)
    # Explicit UTC: bare timestamp() uses exchange TZ and can shift bar_time lines off-chart.
    return f'timestamp("UTC", {y}, {m}, {d}, {h}, {mi})'


def _pine_float(v: Any) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "na"
    if not pd.notna(x):
        return "na"
    return repr(float(x))


def _pine_int(v: Any, default: int = 0) -> str:
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return str(default)
        return str(int(v))
    except (TypeError, ValueError):
        return str(default)


def _array_floats(name: str, values: Sequence[Any]) -> str:
    return f"{name} = array.from({', '.join(_pine_float(v) for v in values)})"


def _array_ints(name: str, values: Sequence[Any]) -> str:
    return f"{name} = array.from({', '.join(_pine_int(v) for v in values)})"


def _array_times(name: str, values: Sequence[Any]) -> str:
    return f"{name} = array.from({', '.join(_pine_ts(v) for v in values)})"


def _array_strings(name: str, values: Sequence[Any]) -> str:
    body = ", ".join(f'"{escape_pine_string(str(v))}"' for v in values)
    return f"{name} = array.from({body})"


def _source_code(source_type: str) -> int:
    mapping = {
        "htf_pivot_5m": 7,
        "htf_pivot_15m": 6,
        "htf_pivot_1h": 8,
        "htf_pivot_4h": 1,
        "htf_pivot_12h": 2,
        "htf_pivot_1d": 3,
        "htf_pivot_1D": 3,
        "external_swing": 4,
        "protected": 5,
    }
    return mapping.get(str(source_type), 0)


def _inv_code(reason: Any) -> int:
    r = str(reason or "")
    if r == "close_break":
        return 1
    if r == "replacement":
        return 2
    if r:
        return 3
    return 0


def filter_htf_only_levels(levels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep preview pivot families (5m / 15m / 1h / 4h / 12h / 1D); drop external/protected."""
    return [r for r in levels if is_htf_source(r.get("source_type"))]


def select_levels_for_pine(
    levels: list[dict[str, Any]],
    cfg: HtfPivotPreviewConfig,
    *,
    reference_price: float | None = None,
) -> list[dict[str, Any]]:
    """Select levels for Pine arrays.

    Preview mode (htf_only + embed_all_htf_levels):
      - drop external/protected
      - always embed all core HTF (4h/12h/1D)
      - embed dense TFs (5m/15m/1h) fully if under pine_max_lines; else prefer
        active + nearest to reference_price (last close) so near-price review stays dense
    """
    rows = list(levels)
    if cfg.htf_only:
        rows = filter_htf_only_levels(rows)
        for r in rows:
            if not is_htf_source(r.get("source_type")):
                raise ValueError(f"non-HTF source leaked into HTF-only selection: {r.get('source_type')}")
        if cfg.embed_all_htf_levels:
            core = [r for r in rows if str(r.get("source_type")) in CORE_HTF_SOURCE_TYPES]
            denser_src = [
                r for r in rows if str(r.get("source_type")) in DENSE_HTF_SOURCE_TYPES
            ]
            other = [
                r
                for r in rows
                if str(r.get("source_type")) not in CORE_HTF_SOURCE_TYPES
                and str(r.get("source_type")) not in DENSE_HTF_SOURCE_TYPES
            ]
            ref = float(reference_price) if reference_price is not None else None
            if ref is None or not pd.notna(ref):
                px_all = [float(r["level_price"]) for r in rows if r.get("level_price") is not None]
                ref = float(sorted(px_all)[len(px_all) // 2]) if px_all else 0.0

            def _near_key(r: dict[str, Any]) -> tuple:
                price = float(r["level_price"])
                return (
                    0 if r.get("active") else 1,
                    abs(price - ref) / max(abs(ref), 1e-12),
                    str(r.get("visible_from_timestamp") or ""),
                    str(r.get("level_id") or ""),
                )

            def _within(r: dict[str, Any], band: float) -> bool:
                price = float(r["level_price"])
                return abs(price - ref) / max(abs(ref), 1e-12) <= band

            # Keep chart-usable inventory tightly around reference close so 5m Y-scale stays sane.
            core_keep = [r for r in core if r.get("active") or _within(r, 0.15)]
            if len(core_keep) < 8:
                core_keep = sorted(core, key=_near_key)[: min(len(core), 24)]
            denser = [r for r in (denser_src + other) if r.get("active") or _within(r, 0.12)]
            denser = sorted(denser, key=_near_key)
            picked = list(core_keep)
            budget = min(int(cfg.pine_max_lines), 120) - len(picked)
            if budget < 0:
                picked = sorted(picked, key=_near_key)[:120]
            else:
                picked.extend(denser[:budget])
            return sorted(picked, key=lambda r: (str(r["visible_from_timestamp"]), str(r["level_id"])))

    active = [r for r in rows if r.get("active")]
    hist = [r for r in rows if not r.get("active")]
    active_sorted = sorted(active, key=lambda r: str(r["visible_from_timestamp"]), reverse=True)
    hist_sorted = sorted(hist, key=lambda r: str(r["visible_from_timestamp"]), reverse=True)
    picked = active_sorted[: cfg.max_active_levels] + hist_sorted[: cfg.max_historical_levels]
    picked = picked[: cfg.pine_max_visible_levels]
    return sorted(picked, key=lambda r: (str(r["visible_from_timestamp"]), str(r["level_id"])))


def count_nlevels_in_pine(pine: str) -> int:
    m = re.search(r"^nLevels\s*=\s*(\d+)\s*$", pine, flags=re.MULTILINE)
    if not m:
        return 0
    return int(m.group(1))


def pine_array_lengths(pine: str) -> dict[str, int]:
    """Parse array.from(...) element counts for parity checks."""
    out: dict[str, int] = {}
    for name in (
        "seqArr",
        "priceArr",
        "sideArr",
        "srcArr",
        "activeArr",
        "touchArr",
        "invReasonArr",
        "visArr",
        "pivotArr",
        "invArr",
        "firstTouchArr",
        "idArr",
        "tfArr",
        "labelArr",
    ):
        m = re.search(rf"{name}\s*=\s*array\.from\((.*)\)\s*$", pine, flags=re.MULTILINE)
        if not m:
            continue
        body = m.group(1).strip()
        if not body:
            out[name] = 0
            continue
        # timestamps/strings contain commas inside; split carefully is hard —
        # count top-level commas via simple state machine
        depth = 0
        in_str = False
        parts = 1
        for ch in body:
            if ch == '"' and not in_str:
                in_str = True
            elif ch == '"' and in_str:
                in_str = False
            elif not in_str:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                elif ch == "," and depth == 0:
                    parts += 1
        out[name] = parts
    return out


def build_htf_pivot_preview_pine(
    levels: list[dict[str, Any]],
    *,
    symbol: str,
    cfg: HtfPivotPreviewConfig,
    title: str | None = None,
    reference_price: float | None = None,
) -> str:
    selected = select_levels_for_pine(levels, cfg, reference_price=reference_price)
    if cfg.htf_only:
        bad = [r for r in selected if _source_code(str(r["source_type"])) in (4, 5)]
        if bad:
            raise ValueError("HTF-only pine must not embed external/protected levels")

    n = len(selected)
    tfs_cfg = tuple(cfg.htf_timeframes) or ("5m", "15m", "1h", "4h", "12h", "1D")
    specs_doc = ", ".join(
        f"{tf}: left={HTF_PIVOT_SPECS[tf]['left']} right={HTF_PIVOT_SPECS[tf]['right']}"
        for tf in tfs_cfg
        if tf in HTF_PIVOT_SPECS
    )
    lifecycle = str(cfg.lifecycle_mode)
    mode_doc = f"lifecycle={lifecycle}; invalidation={cfg.invalidation_mode}"

    default_title = f"HTF Pivot Levels {symbol} [{lifecycle}/v3]"
    if n == 0:
        lines = [
            *build_pine_header(title or default_title),
            f"// {AUDIT_VERSION} — empty inventory for {escape_pine_string(symbol)}",
            f"// Pivot specs: {specs_doc}",
            f"// {mode_doc}",
            f"// htf_only={cfg.htf_only}; embed_all_htf_levels={cfg.embed_all_htf_levels}",
            "// Selection: all HTF pivot levels sorted by visible_from, level_id.",
            "// Python is source of truth; Pine overlays embedded arrays only.",
            "// Forbidden: security lookahead, bidirectional line extend, retroactive line starts.",
            'label.new(bar_index, high, "no levels", style=label.style_label_down, size=size.tiny)',
            "",
        ]
        text = "\n".join(lines) + "\n"
        validate_pine_script(text)
        return text

    prices = [r["level_price"] for r in selected]
    vis = [r["visible_from_timestamp"] for r in selected]
    piv = [r["pivot_timestamp"] for r in selected]
    inv = [r.get("invalidated_at") for r in selected]
    first_touch = [r.get("first_touch_timestamp") for r in selected]
    ids = [str(r.get("level_id") or "") for r in selected]
    sides = [1 if r["side"] == "support" else -1 for r in selected]
    srcs = [_source_code(r["source_type"]) for r in selected]
    if cfg.htf_only and any(s in (4, 5) for s in srcs):
        raise ValueError("srcArr contains 4/5 in HTF-only pine")
    active = [1 if r.get("active") else 0 for r in selected]
    touches = [int(r.get("touch_count") or 0) for r in selected]
    inv_reasons = [_inv_code(r.get("invalidation_reason")) for r in selected]
    seq_nums = list(range(1, n + 1))
    tfs = [str(r.get("timeframe") or "") for r in selected]
    labels = []
    for r in selected:
        side = "LOW" if r["side"] == "support" else "HIGH"
        src = str(r["source_type"]).replace("htf_pivot_", "").upper()
        ft = r.get("first_touch_timestamp") or "-"
        labels.append(
            f"{src} {side}\\nid: {r['level_id']}\\npivot: {r['pivot_timestamp']}"
            f"\\nvisible: {r['visible_from_timestamp']}\\ntouch: {ft}"
        )

    # Array length parity
    lens = {
        "seq": len(seq_nums),
        "price": len(prices),
        "side": len(sides),
        "src": len(srcs),
        "active": len(active),
        "touch": len(touches),
        "invReason": len(inv_reasons),
        "vis": len(vis),
        "pivot": len(piv),
        "inv": len(inv),
        "firstTouch": len(first_touch),
        "id": len(ids),
        "tf": len(tfs),
        "label": len(labels),
    }
    if len(set(lens.values())) != 1:
        raise ValueError(f"pine array length mismatch: {lens}")

    max_draw_default = n if cfg.htf_only and cfg.embed_all_htf_levels else min(n, cfg.pine_max_visible_levels)

    body: list[str] = [
        *build_pine_header(title or default_title),
        f"// RESEARCH ONLY — {AUDIT_VERSION}",
        f"// Symbol: {escape_pine_string(symbol)}",
        f"// Pivot specs: {specs_doc}",
        f"// {mode_doc}",
        f"// htf_only={cfg.htf_only}; embed_all_htf_levels={cfg.embed_all_htf_levels}; nLevels={n}",
        "// Selection rule: pivot sources 5m/15m/1h/4h/12h/1D; sorted by visible_from ASC, level_id ASC;",
        "// core HTF fully embedded; dense TFs trimmed to pine_max_lines by active+nearest-to-close if needed.",
        "// Python scanner is source of truth (embedded arrays).",
        "// Lines start at visible_from (= confirming bar CLOSE), never at pivot open.",
        "// T marker only at first_touch_timestamp (never at visible_from).",
        "// Segments span LEFT through history via bar_index mapping (multi-TF safe).",
        "// Active extend.right is OFF by default (no future-only stubs). Never extend.both. Never security lookahead.",
        "// Historical segments keep t1=visible_from, t2=invalidated_at.",
        "",
        f"nLevels = {n}",
        'show5m = input.bool(true, "Show 5m pivots")',
        'show15m = input.bool(true, "Show 15m pivots")',
        'show1h = input.bool(true, "Show 1h pivots")',
        'show4h = input.bool(true, "Show 4h pivots")',
        'show12h = input.bool(true, "Show 12h pivots")',
        'show1d = input.bool(true, "Show 1D pivots")',
        'showSupport = input.bool(true, "Show support")',
        'showResistance = input.bool(true, "Show resistance")',
        'showActive = input.bool(true, "Show active levels")',
        # Default OFF historically cluttered; ON for near-price review so recently broken
        # supports/resistances under/over live price still appear (banded by maxDistPct).
        'showInvalidated = input.bool(true, "Show invalidated history")',
        'showTouchMarkers = input.bool(false, "Touch markers (T) at first touch")',
        # Markers/labels default OFF — heavy labels can obscure lines and hit object limits.
        'showPivotOrigin = input.bool(false, "Pivot origin markers (P)")',
        'showConfirmMarkers = input.bool(false, "Confirmation markers (C)")',
        'showInvalidateMarkers = input.bool(false, "Invalidation markers (X/R)")',
        'showLabels = input.bool(false, "Level labels")',
        # Always xloc.bar_time — bar_index line.new throws on 5m deep history
        # ("Bar index value of x1 ... too far from the current bar index").
        'extendActiveRight = input.bool(false, "Extend active levels into future (right)")',
        # Visual focus: only nearest levels around live price (above + below).
        'onlyNearestToPrice = input.bool(true, "Only nearest levels around price")',
        'nearAbove = input.int(4, "Nearest above price", minval=0, maxval=20)',
        'nearBelow = input.int(4, "Nearest below price", minval=0, maxval=20)',
        'maxDistPct = input.float(8.0, "Max |level-close| % (scale guard)", minval=1.0, maxval=100.0, step=0.5)',
        f'maxDraw = input.int({min(max_draw_default, 24)}, "Max drawn levels", minval=1, maxval={cfg.pine_max_lines})',
        'confirmOnClose = input.bool(false, "Only draw on last confirmed history bar")',
        "",
        "// --- embedded scanner levels (identical array lengths) ---",
        _array_ints("seqArr", seq_nums),
        _array_floats("priceArr", prices),
        _array_ints("sideArr", sides),
        _array_ints("srcArr", srcs),
        _array_ints("activeArr", active),
        _array_ints("touchArr", touches),
        _array_ints("invReasonArr", inv_reasons),
        _array_times("visArr", vis),
        _array_times("pivotArr", piv),
        _array_times("invArr", inv),
        _array_times("firstTouchArr", first_touch),
        _array_strings("idArr", ids),
        _array_strings("tfArr", tfs),
        _array_strings("labelArr", labels),
        "",
        "var line[] lines = array.new_line()",
        "var label[] labs = array.new_label()",
        "var label[] markers = array.new_label()",
        "",
        "srcEnabled(int src) =>",
        "    src == 7 ? show5m : src == 6 ? show15m : src == 8 ? show1h : src == 1 ? show4h : src == 2 ? show12h : src == 3 ? show1d : false",
        "",
        "sideEnabled(int side) =>",
        "    side == 1 ? showSupport : showResistance",
        "",
        "lineColor(int src, int side, int isActive) =>",
        "    color base = src == 7 ? color.new(color.gray, 0) : src == 6 ? color.new(color.orange, 0) : src == 8 ? color.new(color.aqua, 0) : src == 1 ? color.new(color.teal, 0) : src == 2 ? color.new(color.blue, 0) : color.new(color.purple, 0)",
        "    color c = side == 1 ? base : color.new(color.red, 0)",
        "    isActive == 1 ? c : color.new(c, 55)",
        "",
        "lineStyle(int src) =>",
        "    src == 7 ? line.style_solid : src == 6 ? line.style_solid : src == 8 ? line.style_solid : src == 1 ? line.style_solid : src == 2 ? line.style_dashed : line.style_dotted",
        "",
        "f_clear() =>",
        "    if array.size(lines) > 0",
        "        for i = 0 to array.size(lines) - 1",
        "            line.delete(array.get(lines, i))",
        "        array.clear(lines)",
        "    if array.size(labs) > 0",
        "        for i = 0 to array.size(labs) - 1",
        "            label.delete(array.get(labs, i))",
        "        array.clear(labs)",
        "    if array.size(markers) > 0",
        "        for i = 0 to array.size(markers) - 1",
        "            label.delete(array.get(markers, i))",
        "        array.clear(markers)",
        "",
        "// Draw gate: same pattern as working exit-levels overlays.",
        "// Default confirmOnClose=false → draw on live last bar every tick (after clear).",
        "// If confirmOnClose=true → only draw on last confirmed history bar.",
        "bool drawNow = barstate.islastconfirmedhistory or (not confirmOnClose and barstate.islast)",
        "",
        "// DRAW_MODE=bar_time_only — never xloc.bar_index (TV 5m distance limit).",
        "levelPassesFilters(int i) =>",
        "    int src = array.get(srcArr, i)",
        "    int side = array.get(sideArr, i)",
        "    int isAct = array.get(activeArr, i)",
        "    bool ok = srcEnabled(src) and sideEnabled(side)",
        "    ok := ok and not (isAct == 1 and not showActive)",
        "    ok := ok and not (isAct == 0 and not showInvalidated)",
        "    ok := ok and not na(array.get(visArr, i)) and not na(array.get(priceArr, i))",
        "    ok",
        "",
        "// O(n·k) top-k by |px-close| — avoids bubble-sort timeouts on 5m with large embeds.",
        "pickNearestSide(bool wantAbove, int k) =>",
        "    int[] chosen = array.new_int()",
        "    if k > 0",
        "        bool[] used = array.new_bool(nLevels, false)",
        "        for take = 1 to k",
        "            int best = -1",
        "            float bestD = 1e20",
        "            for i = 0 to nLevels - 1",
        "                if array.get(used, i)",
        "                    continue",
        "                if not levelPassesFilters(i)",
        "                    continue",
        "                float px = array.get(priceArr, i)",
        "                bool isAbove = px >= close",
        "                if isAbove != wantAbove",
        "                    continue",
        "                float dPct = math.abs(px - close) / math.max(math.abs(close), 1e-10) * 100.0",
        "                if dPct > maxDistPct",
        "                    continue",
        "                if dPct < bestD",
        "                    bestD := dPct",
        "                    best := i",
        "            if best < 0",
        "                break",
        "            array.set(used, best, true)",
        "            array.push(chosen, best)",
        "    chosen",
        "",
        "if drawNow",
        "    f_clear()",
        "    int drawnN = 0",
        "    int skippedOffscreen = 0",
        "    int skippedFar = 0",
        "    float drawnMin = na",
        "    float drawnMax = na",
        "    int tfMs = math.max(1, timeframe.in_seconds() * 1000)",
        "    int firstBarTime = time - bar_index * tfMs",
        "    bool[] drawMask = array.new_bool(nLevels, onlyNearestToPrice ? false : true)",
        "    if onlyNearestToPrice",
        "        int[] abovePick = pickNearestSide(true, nearAbove)",
        "        int[] belowPick = pickNearestSide(false, nearBelow)",
        "        if array.size(abovePick) > 0",
        "            for k = 0 to array.size(abovePick) - 1",
        "                array.set(drawMask, array.get(abovePick, k), true)",
        "        if array.size(belowPick) > 0",
        "            for k = 0 to array.size(belowPick) - 1",
        "                array.set(drawMask, array.get(belowPick, k), true)",
        "    for i = 0 to nLevels - 1",
        "        if drawnN >= maxDraw",
        "            break",
        "        if not levelPassesFilters(i)",
        "            continue",
        "        if onlyNearestToPrice and not array.get(drawMask, i)",
        "            skippedFar += 1",
        "            continue",
        "        float px = array.get(priceArr, i)",
        "        float dPct = math.abs(px - close) / math.max(math.abs(close), 1e-10) * 100.0",
        "        if dPct > maxDistPct",
        "            skippedFar += 1",
        "            continue",
        "        int src = array.get(srcArr, i)",
        "        int side = array.get(sideArr, i)",
        "        int isAct = array.get(activeArr, i)",
        "        int xVis = array.get(visArr, i)",
        "        int xPivot = array.get(pivotArr, i)",
        "        int xInv = array.get(invArr, i)",
        "        int xTouch = array.get(firstTouchArr, i)",
        "        int t1 = xVis",
        "        int t2 = isAct == 1 ? time : (na(xInv) ? time : xInv)",
        "        if isAct == 0 and not na(xInv) and xInv <= xVis",
        "            t2 := xVis + tfMs",
        "        if t2 < firstBarTime",
        "            skippedOffscreen += 1",
        "            continue",
        "        if t1 < firstBarTime",
        "            t1 := firstBarTime",
        "        if t2 > time",
        "            t2 := time",
        "        if isAct == 0 and t2 <= t1",
        "            skippedOffscreen += 1",
        "            continue",
        "        bool extR = isAct == 1 and extendActiveRight",
        "        int x1 = t1",
        "        int x2 = t2",
        "        if x2 <= x1",
        "            x2 := x1 + tfMs",
        "        line ln = line.new(x1, px, x2, px, xloc=xloc.bar_time, extend=extR ? extend.right : extend.none, color=lineColor(src, side, isAct), style=lineStyle(src), width=isAct == 1 ? 2 : 1)",
        "        array.push(lines, ln)",
        "        if showLabels",
        "            label lb = label.new(x1, px, array.get(labelArr, i), xloc=xloc.bar_time, style=side == 1 ? label.style_label_up : label.style_label_down, color=color.new(color.black, 80), textcolor=color.white, size=size.tiny)",
        "            array.push(labs, lb)",
        "        if showPivotOrigin and not na(xPivot) and xPivot >= firstBarTime",
        "            label mp = label.new(xPivot, px, \"P\", xloc=xloc.bar_time, style=label.style_label_center, color=color.new(color.gray, 30), textcolor=color.white, size=size.tiny)",
        "            array.push(markers, mp)",
        "        if showConfirmMarkers and xVis >= firstBarTime",
        "            label mc = label.new(xVis, px, \"C\", xloc=xloc.bar_time, style=label.style_label_center, color=color.new(color.green, 20), textcolor=color.white, size=size.tiny)",
        "            array.push(markers, mc)",
        "        if showInvalidateMarkers and isAct == 0 and not na(xInv) and xInv >= firstBarTime",
        "            string mark = array.get(invReasonArr, i) == 2 ? \"R\" : \"X\"",
        "            label mx = label.new(xInv, px, mark, xloc=xloc.bar_time, style=label.style_label_center, color=color.new(color.red, 20), textcolor=color.white, size=size.tiny)",
        "            array.push(markers, mx)",
        "        if showTouchMarkers and not na(xTouch) and array.get(touchArr, i) > 0 and xTouch >= firstBarTime",
        "            label mt = label.new(xTouch, px, \"T\" + str.tostring(array.get(touchArr, i)), xloc=xloc.bar_time, style=label.style_label_left, color=color.new(color.yellow, 40), textcolor=color.black, size=size.tiny)",
        "            array.push(markers, mt)",
        "        drawnMin := na(drawnMin) ? px : math.min(drawnMin, px)",
        "        drawnMax := na(drawnMax) ? px : math.max(drawnMax, px)",
        "        drawnN += 1",
        '    label.new(bar_index, close, "HTF preview OK v3-bartime\\ndrawn=" + str.tostring(drawnN) + "/" + str.tostring(nLevels) + "\\nskipFar=" + str.tostring(skippedFar) + " off=" + str.tostring(skippedOffscreen) + "\\nclose=" + str.tostring(close, "#.####") + "\\ndrawnPx " + (na(drawnMin) ? "-" : str.tostring(drawnMin, "#.####")) + ".." + (na(drawnMax) ? "-" : str.tostring(drawnMax, "#.####")), style=label.style_label_left, color=color.new(color.black, 20), textcolor=color.lime, size=size.small)',
        "",
        "// Debug table (parity checklist vs CSV)",
        "var table dbg = table.new(position.bottom_right, 8, math.min(nLevels, 12) + 1)",
        "if barstate.islast",
        '    table.cell(dbg, 0, 0, "seq")',
        '    table.cell(dbg, 1, 0, "px")',
        '    table.cell(dbg, 2, 0, "tf")',
        '    table.cell(dbg, 3, 0, "side")',
        '    table.cell(dbg, 4, 0, "vis")',
        '    table.cell(dbg, 5, 0, "touch")',
        '    table.cell(dbg, 6, 0, "inv")',
        '    table.cell(dbg, 7, 0, "id")',
        "    int rows = math.min(nLevels, 12)",
        "    for i = 0 to rows - 1",
        "        table.cell(dbg, 0, i + 1, str.tostring(array.get(seqArr, i)))",
        "        table.cell(dbg, 1, i + 1, str.tostring(array.get(priceArr, i), format.mintick))",
        "        table.cell(dbg, 2, i + 1, array.get(tfArr, i))",
        '        table.cell(dbg, 3, i + 1, array.get(sideArr, i) == 1 ? "S" : "R")',
        "        table.cell(dbg, 4, i + 1, str.format_time(array.get(visArr, i), \"yyyy-MM-dd HH:mm\", \"UTC\"))",
        "        int xt = array.get(firstTouchArr, i)",
        '        table.cell(dbg, 5, i + 1, na(xt) ? "-" : str.format_time(xt, "yyyy-MM-dd HH:mm", "UTC"))',
        "        int xi = array.get(invArr, i)",
        '        table.cell(dbg, 6, i + 1, na(xi) ? "-" : str.format_time(xi, "yyyy-MM-dd HH:mm", "UTC"))',
        "        table.cell(dbg, 7, i + 1, array.get(idArr, i))",
        "",
    ]
    text = "\n".join(body) + "\n"
    if "barmerge.lookahead_on" in text or "lookahead=barmerge.lookahead_on" in text:
        raise ValueError("generated pine must not enable lookahead_on")
    if "extend=extend.both" in text or "extend.both)" in text:
        raise ValueError("generated pine must not contain extend.both")
    # Touch must use firstTouchArr, never place T at visArr/x1
    if 'label.new(x1, px, "T' in text:
        raise ValueError("T marker must not use visible_from (x1)")
    if 'label.new(xTouch, px, "T' not in text:
        raise ValueError("T marker must use first_touch timestamp (xTouch)")
    validate_pine_script(text)
    return text
