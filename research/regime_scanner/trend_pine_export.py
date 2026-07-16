"""TradingView Pine v6 export for trend-state research audits (read-only).

Embeds audited state runs, transitions and optional event markers as static
arrays — no regime calculation inside Pine. Intended for APTUSDT 5m chart
review in UTC.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import pandas as pd

from research.regime_scanner.point_audit import json_safe

PINE_VERSION = 6
DEFAULT_TIMEFRAME = "5m"
CHUNK_SIZE = 24
PINE_MAX_TRANSITION_LABELS = 120
PINE_MAX_DISAGREEMENT_MARKERS = 400
AUDIT_ANCHOR_PLOT = 'plot(close, title="Audit anchor", display=display.none)'


class PineExportValidationError(ValueError):
    """Raised when generated Pine would fail TradingView CE10156 / CE10246 checks."""


def pine_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def escape_pine_string(value: str) -> str:
    return pine_escape(value)


# Stable integer codes for Pine switch/bgcolor (audit display only).
STATE_CODES: dict[str, int] = {
    "unavailable": 0,
    "neutral": 1,
    "strong_bullish": 2,
    "early_bullish": 3,
    "bullish_weakening": 4,
    "topping": 5,
    "bullish_warning": 6,
    "strong_bearish": 7,
    "early_bearish": 8,
    "bearish_weakening": 9,
    "bottoming": 10,
    "bearish_warning": 11,
}

_STATE_FROM_CODE = {v: k for k, v in STATE_CODES.items()}

# Phase C3 regime states (separate code space for Pine review).
C3_STATE_CODES: dict[str, int] = {
    "confirmed_uptrend": 20,
    "confirmed_downtrend": 21,
    "range_sideways": 22,
    "bullish_pullback": 23,
    "bearish_pullback": 24,
    "transition_up": 25,
    "transition_down": 26,
    "unclear": 27,
}


def c3_state_code(state: object) -> int:
    return int(C3_STATE_CODES.get(str(state or ""), C3_STATE_CODES["unclear"]))


def build_pine_header(title: str) -> list[str]:
    """Atomic Pine v6 header: version, closed indicator(...), then audit anchor."""
    return [
        "//@version=6",
        "indicator(",
        f'    "{escape_pine_string(title)}",',
        "    overlay=true,",
        "    max_labels_count=500,",
        "    max_lines_count=500",
        ")",
        "",
        AUDIT_ANCHOR_PLOT,
        "",
    ]


def _indicator_block_end(text: str) -> int:
    start = text.index("indicator(")
    depth = 0
    for idx in range(start, len(text)):
        ch = text[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return idx
    raise PineExportValidationError("indicator(...) block is not closed (CE10156 risk)")


def validate_pine_script(text: str) -> None:
    """Static checks before writing Pine to disk."""
    if not text.endswith("\n"):
        raise PineExportValidationError("pine script must end with a newline")

    lines = text.splitlines()
    if not lines or lines[0] != "//@version=6":
        raise PineExportValidationError("first code line must be //@version=6")

    if text.count("indicator(") != 1:
        raise PineExportValidationError("exactly one indicator( is required")

    if text.count(AUDIT_ANCHOR_PLOT) != 1:
        raise PineExportValidationError(
            f"exactly one audit anchor is required: {AUDIT_ANCHOR_PLOT}"
        )

    ind_start = text.index("indicator(")
    ind_end = _indicator_block_end(text)
    anchor_pos = text.index(AUDIT_ANCHOR_PLOT)

    if anchor_pos <= ind_end:
        raise PineExportValidationError(
            "audit anchor must appear after the closed indicator(...) block (CE10156)"
        )

    indicator_body = text[ind_start : ind_end + 1]
    if "plot(" in indicator_body:
        raise PineExportValidationError("plot must not appear inside indicator(...) (CE10156)")

    first_fn = re.search(r"^[A-Za-z_][\w]*\([^)]*\)\s*=>", text, flags=re.MULTILINE)
    if first_fn is not None and first_fn.start() <= anchor_pos:
        raise PineExportValidationError("audit anchor must appear before first function definition")

    for token in ("if barstate.isfirst",):
        pos = text.find(token)
        if pos != -1 and pos <= anchor_pos:
            raise PineExportValidationError(
                f"audit anchor must appear before {token!r}"
            )

    if lines[1] != "indicator(" or lines[6] != ")" or lines[8] != AUDIT_ANCHOR_PLOT:
        raise PineExportValidationError("pine header structure is invalid")


def _ts(value: object) -> pd.Timestamp:
    t = pd.Timestamp(value)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def state_code(state: object) -> int:
    return int(STATE_CODES.get(str(state or ""), 0))


def sanitize_variant_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")


PINE_VARIANT_SHORT: dict[str, str] = {
    "C3_A_conservative": "conservative",
    "C3_B_balanced": "balanced",
    "C3_C_responsive": "responsive",
}


def pine_variant_slug(variant_name: str) -> str:
    return PINE_VARIANT_SHORT.get(variant_name, sanitize_variant_name(variant_name))


def timeline_to_state_runs(
    timeline_rows: Sequence[Mapping[str, Any]],
    *,
    state_key: str = "state",
    code_fn: Callable[[object], int] | None = None,
) -> list[dict[str, Any]]:
    """Contiguous state episodes from per-bar timeline rows."""
    if not timeline_rows:
        return []
    encode = code_fn or state_code
    rows = list(timeline_rows)
    runs: list[dict[str, Any]] = []
    cur_state = str(rows[0].get(state_key) or "unavailable")
    start_ts = str(rows[0].get("decision_time") or "")
    prev_ts = start_ts
    for row in rows[1:]:
        ts = str(row.get("decision_time") or "")
        st = str(row.get(state_key) or "unavailable")
        if st != cur_state:
            runs.append(
                {
                    "start_time": start_ts,
                    "end_time": prev_ts,
                    "state": cur_state,
                    "state_code": encode(cur_state),
                }
            )
            cur_state = st
            start_ts = ts
        prev_ts = ts
    runs.append(
        {
            "start_time": start_ts,
            "end_time": prev_ts,
            "state": cur_state,
            "state_code": encode(cur_state),
        }
    )
    return runs


def limit_pine_transitions(
    transitions: Sequence[Mapping[str, Any]],
    *,
    max_count: int = PINE_MAX_TRANSITION_LABELS,
) -> list[dict[str, Any]]:
    """Cap transition labels embedded in Pine to stay under TradingView token limits."""
    items = [dict(t) for t in transitions]
    if len(items) <= max_count:
        return items
    if max_count <= 0:
        return []
    step = max(1, len(items) // max_count)
    out = [items[i] for i in range(0, len(items), step)][:max_count]
    if out and out[-1] != items[-1]:
        out[-1] = items[-1]
    return out


def extract_transitions(timeline_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in timeline_rows:
        if not row.get("transition") and row.get("previous_state") == row.get("state"):
            continue
        prev = str(row.get("previous_state") or "")
        new = str(row.get("state") or "")
        if prev == new:
            continue
        out.append(
            {
                "decision_time": str(row.get("decision_time") or ""),
                "previous_state": prev,
                "new_state": new,
                "previous_state_code": state_code(prev),
                "new_state_code": state_code(new),
                "reasons": str(row.get("reasons") or row.get("trigger_reasons") or ""),
                "close": row.get("close"),
            }
        )
    return out


def build_timeline_from_state_series(
    frame: pd.DataFrame,
    state_by_bar: Sequence[str],
    *,
    analyze_start: object,
    analyze_end: object,
) -> list[dict[str, Any]]:
    """Build analyze-window timeline rows from full-frame state replay."""
    a0 = _ts(analyze_start)
    a1 = _ts(analyze_end)
    rows: list[dict[str, Any]] = []
    n = min(len(frame), len(state_by_bar))
    for i in range(n):
        row = frame.iloc[i]
        decision_ts = _ts(row["decision_time"])
        if not (a0 <= decision_ts <= a1):
            continue
        prev = state_by_bar[i - 1] if i > 0 else "unavailable"
        cur = str(state_by_bar[i])
        rows.append(
            {
                "decision_time": decision_ts.isoformat(),
                "state": cur,
                "previous_state": prev,
                "close": float(row["close"]),
                "transition": cur != prev,
                "reasons": "",
            }
        )
    return rows


def _pine_ts_parts(ts: object) -> tuple[int, int, int, int, int]:
    t = _ts(ts)
    return int(t.year), int(t.month), int(t.day), int(t.hour), int(t.minute)


def _chunked_push_helpers(
    items: Sequence[Mapping[str, Any]],
    *,
    prefix: str,
    push_body: Callable[[list[str], Mapping[str, Any]], None],
) -> tuple[list[str], list[str]]:
    if not items:
        return [f"f_{prefix}_00() =>\n    true"], [f"    f_{prefix}_00()"]
    helpers: list[str] = []
    calls: list[str] = []
    for idx, chunk_start in enumerate(range(0, len(items), CHUNK_SIZE)):
        chunk = items[chunk_start : chunk_start + CHUNK_SIZE]
        name = f"f_{prefix}_{idx:02d}"
        body = [f"{name}() =>"]
        for item in chunk:
            push_body(body, item)
        helpers.append("\n".join(body))
        calls.append(f"    {name}()")
    return helpers, calls


def _push_run_lines(body: list[str], run: Mapping[str, Any]) -> None:
    y0, m0, d0, h0, mi0 = _pine_ts_parts(run["start_time"])
    y1, m1, d1, h1, mi1 = _pine_ts_parts(run["end_time"])
    code = int(run["state_code"])
    st = pine_escape(str(run["state"]))
    body.append(f"    array.push(runStarts, f_ts({y0}, {m0}, {d0}, {h0}, {mi0}))")
    body.append(f"    array.push(runEnds, f_ts({y1}, {m1}, {d1}, {h1}, {mi1}))")
    body.append(f"    array.push(runStates, {code})")
    body.append(f'    array.push(runNames, "{st}")')


def _push_transition_lines(body: list[str], tr: Mapping[str, Any]) -> None:
    y, m, d, h, mi = _pine_ts_parts(tr["decision_time"])
    from_c = int(tr["previous_state_code"])
    to_c = int(tr["new_state_code"])
    label = pine_escape(f"{tr['previous_state']}->{tr['new_state']}")
    body.append(f"    array.push(trTimes, f_ts({y}, {m}, {d}, {h}, {mi}))")
    body.append(f"    array.push(trFrom, {from_c})")
    body.append(f"    array.push(trTo, {to_c})")
    body.append(f'    array.push(trLabels, "{label}")')


def _push_marker_lines(body: list[str], mk: Mapping[str, Any]) -> None:
    y, m, d, h, mi = _pine_ts_parts(mk["decision_time"])
    code = int(mk.get("marker_code") or state_code(mk.get("new_state")))
    label = pine_escape(str(mk.get("label") or mk.get("new_state") or "event"))
    body.append(f"    array.push(evTimes, f_ts({y}, {m}, {d}, {h}, {mi}))")
    body.append(f"    array.push(evCodes, {code})")
    body.append(f'    array.push(evLabels, "{label}")')


def build_trend_state_pine(
    *,
    title: str,
    symbol: str,
    phase: str,
    variant: str,
    analyze_start: str,
    analyze_end: str,
    state_runs: Sequence[Mapping[str, Any]],
    transitions: Sequence[Mapping[str, Any]],
    markers: Sequence[Mapping[str, Any]] | None = None,
    timeframe: str = DEFAULT_TIMEFRAME,
) -> str:
    """Pine v6 overlay: state bgcolor runs + transition labels + optional markers."""
    markers = list(markers or [])
    run_helpers, run_calls = _chunked_push_helpers(state_runs, prefix="runs", push_body=_push_run_lines)
    tr_helpers, tr_calls = _chunked_push_helpers(
        transitions, prefix="tr", push_body=_push_transition_lines
    )
    mk_helpers, mk_calls = _chunked_push_helpers(markers, prefix="ev", push_body=_push_marker_lines)

    helper_block = "\n\n".join([*run_helpers, *tr_helpers, *mk_helpers])
    init_calls = "\n".join([*run_calls, *tr_calls, *mk_calls]) or "    true"

    lines = [
        *build_pine_header(title),
        "// READ-ONLY research chart review. No live logic. No signals.",
        f"// Phase: {escape_pine_string(phase)} | Variant: {escape_pine_string(variant)}",
        (
            f"// Symbol: {escape_pine_string(symbol)} | Timeframe: {escape_pine_string(timeframe)} "
            "(UTC decision_time = bar close)"
        ),
        f"// Analyze: {escape_pine_string(analyze_start)} .. {escape_pine_string(analyze_end)}",
        "",
        'showTransitions = input.bool(true, "Show transition labels")',
        'showEventMarkers = input.bool(true, "Show audit event markers")',
        'showStateLegend = input.bool(true, "Show active state (data window)")',
        "",
        "f_ts(y, m, d, h, mi) =>",
        '    timestamp("UTC", y, m, d, h, mi)',
        "",
        "stateColor(code) =>",
        "    switch code",
        "        2 => color.new(color.green, 88)",
        "        3 => color.new(color.green, 92)",
        "        4 => color.new(color.yellow, 86)",
        "        5 => color.new(color.orange, 82)",
        "        6 => color.new(color.yellow, 90)",
        "        7 => color.new(color.red, 88)",
        "        8 => color.new(color.red, 92)",
        "        9 => color.new(color.purple, 86)",
        "        10 => color.new(color.teal, 82)",
        "        11 => color.new(color.purple, 90)",
        "        1 => color.new(color.gray, 92)",
        "        => color.new(color.gray, 96)",
        "",
        "var int[] runStarts = array.new_int()",
        "var int[] runEnds = array.new_int()",
        "var int[] runStates = array.new_int()",
        "var string[] runNames = array.new_string()",
        "",
        "var int[] trTimes = array.new_int()",
        "var int[] trFrom = array.new_int()",
        "var int[] trTo = array.new_int()",
        "var string[] trLabels = array.new_string()",
        "",
        "var int[] evTimes = array.new_int()",
        "var int[] evCodes = array.new_int()",
        "var string[] evLabels = array.new_string()",
        "",
        helper_block,
        "",
        "if barstate.isfirst",
        init_calls,
        "",
        "int activeCode = 0",
        'string activeName = ""',
        "if array.size(runStarts) > 0",
        "    for i = 0 to array.size(runStarts) - 1",
        "        int s = array.get(runStarts, i)",
        "        int e = array.get(runEnds, i)",
        "        if time_close >= s and time_close <= e",
        "            activeCode := array.get(runStates, i)",
        "            activeName := array.get(runNames, i)",
        "",
        'bgcolor(showStateLegend ? stateColor(activeCode) : na, title = "Trend state run")',
        "",
        "if showTransitions and array.size(trTimes) > 0",
        "    for i = 0 to array.size(trTimes) - 1",
        "        if time_close == array.get(trTimes, i)",
        "            int toCode = array.get(trTo, i)",
        "            string lbl = array.get(trLabels, i)",
        "            label.new(",
        "                 bar_index,",
        "                 toCode == 5 or toCode == 7 or toCode == 9 ? high * 1.0003 : low * 0.9997,",
        "                 lbl,",
        "                 style = label.style_label_down,",
        "                 color = color.new(color.black, 25),",
        "                 textcolor = color.white,",
        "                 size = size.tiny",
        "             )",
        "",
        "if showEventMarkers and array.size(evTimes) > 0",
        "    for i = 0 to array.size(evTimes) - 1",
        "        if time_close == array.get(evTimes, i)",
        "            int evCode = array.get(evCodes, i)",
        "            string evLbl = array.get(evLabels, i)",
        "            label.new(",
        "                 bar_index,",
        "                 evCode == 5 or evCode == 7 or evCode == 9 ? high * 1.0006 : low * 0.9994,",
        "                 evLbl,",
        "                 style = label.style_label_left,",
        "                 color = color.new(stateColor(evCode), 35),",
        "                 textcolor = color.white,",
        "                 size = size.small",
        "             )",
        "",
        "// EOF",
    ]

    pine_text = "\n".join(lines) + "\n"
    validate_pine_script(pine_text)
    return pine_text


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    pd.DataFrame(rows).to_csv(path, index=False)


def export_variant_pine(
    *,
    output_dir: Path,
    phase: str,
    symbol: str,
    variant: str,
    analyze_start: str,
    analyze_end: str,
    timeline_rows: Sequence[Mapping[str, Any]],
    marker_rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write CSV + Pine for one audit variant."""
    safe = sanitize_variant_name(variant)
    runs = timeline_to_state_runs(timeline_rows)
    transitions = extract_transitions(timeline_rows)

    chart_csv = output_dir / f"trend_chart_review_{safe}.csv"
    trans_csv = output_dir / f"trend_transitions_{safe}.csv"
    pine_path = output_dir / f"trend_audit_{sanitize_variant_name(phase)}_{safe}.pine"

    _write_csv(chart_csv, [dict(r) for r in timeline_rows])
    _write_csv(trans_csv, transitions)

    title = f"{symbol} {phase} {variant}"
    pine_text = build_trend_state_pine(
        title=title,
        symbol=symbol,
        phase=phase,
        variant=variant,
        analyze_start=analyze_start,
        analyze_end=analyze_end,
        state_runs=runs,
        transitions=transitions,
        markers=marker_rows,
    )
    validate_pine_script(pine_text)
    pine_path.write_text(pine_text, encoding="utf-8")
    pine_hash = hashlib.sha256(pine_text.encode()).hexdigest()

    return {
        "variant": variant,
        "safe_name": safe,
        "pine_path": str(pine_path),
        "chart_review_csv": str(chart_csv),
        "transitions_csv": str(trans_csv),
        "n_timeline_bars": len(timeline_rows),
        "n_state_runs": len(runs),
        "n_transitions": len(transitions),
        "n_markers": len(list(marker_rows or [])),
        "pine_sha256": pine_hash,
        "pine_bytes": len(pine_text.encode()),
    }


def export_audit_pine_artifacts(
    *,
    output_dir: Path,
    phase: str,
    symbol: str,
    analyze_start: str,
    analyze_end: str,
    variants: Mapping[str, Mapping[str, Any]],
    recommended_variant: str | None = None,
) -> dict[str, Any]:
    """Export Pine bundle for all variants; copy recommended script if known."""
    output_dir.mkdir(parents=True, exist_ok=True)
    per_variant: dict[str, Any] = {}

    for variant_name, payload in variants.items():
        timeline_rows = list(payload.get("timeline_rows") or [])
        marker_rows = list(payload.get("marker_rows") or payload.get("markers") or [])
        per_variant[variant_name] = export_variant_pine(
            output_dir=output_dir,
            phase=phase,
            symbol=symbol,
            variant=variant_name,
            analyze_start=analyze_start,
            analyze_end=analyze_end,
            timeline_rows=timeline_rows,
            marker_rows=marker_rows,
        )

    recommended_pine: str | None = None
    if recommended_variant and recommended_variant in per_variant:
        src = Path(per_variant[recommended_variant]["pine_path"])
        dst = output_dir / f"trend_audit_{sanitize_variant_name(phase)}_recommended.pine"
        shutil.copyfile(src, dst)
        recommended_pine = str(dst)

    meta = {
        "phase": phase,
        "symbol": symbol,
        "analyze_start": analyze_start,
        "analyze_end": analyze_end,
        "pine_version": PINE_VERSION,
        "timeframe": DEFAULT_TIMEFRAME,
        "recommended_variant": recommended_variant,
        "recommended_pine": recommended_pine,
        "variants": per_variant,
        "state_codes": STATE_CODES,
        "note": "Research-only TradingView overlay; does not change production config.",
    }
    meta_path = output_dir / "trend_pine_export.json"
    meta_path.write_text(json.dumps(json_safe(meta), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    meta["metadata_path"] = str(meta_path)
    return meta


def marker_rows_from_events(
    events: Iterable[Mapping[str, Any]],
    *,
    label_field: str = "new_state",
    extra_suffix: str | None = None,
) -> list[dict[str, Any]]:
    """Convert forward-outcome / exit events to Pine marker rows."""
    rows: list[dict[str, Any]] = []
    for ev in events:
        ts = ev.get("timestamp") or ev.get("decision_time")
        if ts is None:
            continue
        new_state = str(ev.get("new_state") or ev.get("to_state") or "")
        label = str(ev.get(label_field) or new_state)
        if extra_suffix:
            label = f"{label}|{extra_suffix}"
        hit = ev.get("h12_direction_hit")
        if hit is True:
            label += " ✓"
        elif hit is False:
            label += " ✗"
        rows.append(
            {
                "decision_time": str(ts),
                "new_state": new_state,
                "marker_code": state_code(new_state),
                "label": label,
            }
        )
    return rows


def _push_c3_run_lines(body: list[str], run: Mapping[str, Any]) -> None:
    y0, m0, d0, h0, mi0 = _pine_ts_parts(run["start_time"])
    y1, m1, d1, h1, mi1 = _pine_ts_parts(run["end_time"])
    code = int(run.get("state_code") or c3_state_code(run.get("state")))
    st = pine_escape(str(run["state"]))
    body.append(f"    array.push(runStarts, f_ts({y0}, {m0}, {d0}, {h0}, {mi0}))")
    body.append(f"    array.push(runEnds, f_ts({y1}, {m1}, {d1}, {h1}, {mi1}))")
    body.append(f"    array.push(runStates, {code})")
    body.append(f'    array.push(runNames, "{st}")')


def _push_range_bound_lines(body: list[str], run: Mapping[str, Any]) -> None:
    y0, m0, d0, h0, mi0 = _pine_ts_parts(run["start_time"])
    y1, m1, d1, h1, mi1 = _pine_ts_parts(run["end_time"])
    hi = float(run.get("range_high") or 0.0)
    lo = float(run.get("range_low") or 0.0)
    body.append(f"    array.push(rngStarts, f_ts({y0}, {m0}, {d0}, {h0}, {mi0}))")
    body.append(f"    array.push(rngEnds, f_ts({y1}, {m1}, {d1}, {h1}, {mi1}))")
    body.append(f"    array.push(rngHighs, {hi})")
    body.append(f"    array.push(rngLows, {lo})")


def _push_range_event_lines(body: list[str], ev: Mapping[str, Any]) -> None:
    y, m, d, h, mi = _pine_ts_parts(ev["decision_time"])
    kind = int(ev.get("kind") or 1)
    label = pine_escape(str(ev.get("label") or "RNG"))
    score = float(ev.get("range_score") or 0.0)
    body.append(f"    array.push(reTimes, f_ts({y}, {m}, {d}, {h}, {mi}))")
    body.append(f"    array.push(reKinds, {kind})")
    body.append(f'    array.push(reLabels, "{label}")')
    body.append(f"    array.push(reScores, {score})")


def extract_range_bound_runs(timeline_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Contiguous confirmed-range episodes with stable high/low for Pine lines."""
    runs: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for row in timeline_rows:
        in_range = bool(row.get("range_confirmed") or row.get("state") == "range_sideways")
        ts = str(row.get("decision_time") or "")
        hi = row.get("range_high")
        lo = row.get("range_low")
        if in_range and hi is not None and lo is not None:
            if cur is None:
                cur = {
                    "start_time": ts,
                    "end_time": ts,
                    "range_high": float(hi),
                    "range_low": float(lo),
                }
            else:
                cur["end_time"] = ts
                # Keep first established bounds (stable); do not chase every update.
        else:
            if cur is not None:
                runs.append(cur)
                cur = None
    if cur is not None:
        runs.append(cur)
    return runs


def extract_range_events(timeline_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for row in timeline_rows:
        reasons = str(row.get("reasons") or "")
        ts = str(row.get("decision_time") or "")
        score = float(row.get("range_score") or 0.0)
        if "range_enter" in reasons or (
            row.get("transition")
            and row.get("state") == "range_sideways"
            and row.get("previous_state") != "range_sideways"
        ):
            events.append(
                {"decision_time": ts, "kind": 1, "label": "RNG_IN", "range_score": score}
            )
        if "range_exit" in reasons:
            events.append(
                {"decision_time": ts, "kind": 2, "label": "RNG_OUT", "range_score": score}
            )
        if row.get("failed_breakout_event"):
            events.append(
                {"decision_time": ts, "kind": 3, "label": "FAIL_BO", "range_score": score}
            )
    return limit_pine_transitions(events, max_count=150)


def build_c3_regime_pine(
    *,
    title: str,
    symbol: str,
    phase: str,
    variant: str,
    analyze_start: str,
    analyze_end: str,
    audit_hash: str,
    state_runs: Sequence[Mapping[str, Any]],
    transitions: Sequence[Mapping[str, Any]],
    markers: Sequence[Mapping[str, Any]] | None = None,
    timeline_rows: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    markers = list(markers or [])
    timeline_rows = list(timeline_rows or [])
    range_runs = extract_range_bound_runs(timeline_rows)
    range_events = extract_range_events(timeline_rows)

    run_helpers, run_calls = _chunked_push_helpers(
        [{**dict(r), "state_code": c3_state_code(r.get("state"))} for r in state_runs],
        prefix="runs",
        push_body=_push_c3_run_lines,
    )
    pine_transitions = limit_pine_transitions(transitions)
    tr_helpers, tr_calls = _chunked_push_helpers(
        pine_transitions, prefix="tr", push_body=_push_transition_lines
    )
    rng_helpers, rng_calls = _chunked_push_helpers(
        range_runs, prefix="rng", push_body=_push_range_bound_lines
    )
    re_helpers, re_calls = _chunked_push_helpers(
        range_events, prefix="re", push_body=_push_range_event_lines
    )
    helper_block = "\n\n".join([*run_helpers, *tr_helpers, *rng_helpers, *re_helpers])
    init_calls = "\n".join([*run_calls, *tr_calls, *rng_calls, *re_calls]) or "    true"
    lines = [
        *build_pine_header(title),
        f"// READ-ONLY C3.1 regime review | {escape_pine_string(phase)} | {escape_pine_string(variant)}",
        f"// Symbol: {escape_pine_string(symbol)} | Timeframe: 5m UTC | Analyze: {escape_pine_string(analyze_start)} .. {escape_pine_string(analyze_end)}",
        f"// Audit hash: {escape_pine_string(audit_hash)}",
        'expectedTf = input.string("5", "Expected chart timeframe (minutes)")',
        'showTfWarning = input.bool(true, "Show timeframe mismatch warning")',
        'showRangeBounds = input.bool(true, "Show range high/low")',
        'showRangeEvents = input.bool(true, "Show range enter/exit/fail markers")',
        'showRangeTable = input.bool(true, "Show range diagnostics table")',
        "",
        "c3Color(code) =>",
        "    switch code",
        "        20 => color.new(color.green, 84)",
        "        21 => color.new(color.red, 84)",
        "        22 => color.new(color.blue, 78)",
        "        23 => color.new(color.teal, 86)",
        "        24 => color.new(color.orange, 86)",
        "        25 => color.new(color.yellow, 84)",
        "        26 => color.new(color.purple, 84)",
        "        => color.new(color.silver, 90)",
        "",
        "f_ts(y, m, d, h, mi) =>",
        '    timestamp("UTC", y, m, d, h, mi)',
        "",
        "var int[] runStarts = array.new_int()",
        "var int[] runEnds = array.new_int()",
        "var int[] runStates = array.new_int()",
        "var string[] runNames = array.new_string()",
        "var int[] trTimes = array.new_int()",
        "var int[] trFrom = array.new_int()",
        "var int[] trTo = array.new_int()",
        "var string[] trLabels = array.new_string()",
        "var int[] rngStarts = array.new_int()",
        "var int[] rngEnds = array.new_int()",
        "var float[] rngHighs = array.new_float()",
        "var float[] rngLows = array.new_float()",
        "var int[] reTimes = array.new_int()",
        "var int[] reKinds = array.new_int()",
        "var string[] reLabels = array.new_string()",
        "var float[] reScores = array.new_float()",
        "",
        helper_block,
        "",
        "if barstate.isfirst",
        init_calls,
        "",
        'tfOk = timeframe.in_seconds(timeframe.period) == 300',
        "if showTfWarning and barstate.islast and not tfOk",
        '    label.new(bar_index, high, "WARNING: use APTUSDT 5m UTC", style=label.style_label_down, color=color.red, textcolor=color.white, size=size.normal)',
        "",
        "int activeCode = 0",
        'string activeName = ""',
        "if array.size(runStarts) > 0",
        "    for i = 0 to array.size(runStarts) - 1",
        "        int s = array.get(runStarts, i)",
        "        int e = array.get(runEnds, i)",
        "        if time_close >= s and time_close <= e",
        "            activeCode := array.get(runStates, i)",
        "            activeName := array.get(runNames, i)",
        "bgcolor(c3Color(activeCode))",
        "",
        "float plotHi = na",
        "float plotLo = na",
        "float curScore = na",
        "int barsInRange = 0",
        "bool rangeConfirmed = activeCode == 22",
        "if showRangeBounds and array.size(rngStarts) > 0",
        "    for i = 0 to array.size(rngStarts) - 1",
        "        int rs = array.get(rngStarts, i)",
        "        int re = array.get(rngEnds, i)",
        "        if time_close >= rs and time_close <= re",
        "            plotHi := array.get(rngHighs, i)",
        "            plotLo := array.get(rngLows, i)",
        "plot(plotHi, title=\"Range High\", color=color.new(color.blue, 0), linewidth=2)",
        "plot(plotLo, title=\"Range Low\", color=color.new(color.blue, 0), linewidth=2)",
        "",
        "if showRangeEvents and array.size(reTimes) > 0",
        "    for i = 0 to array.size(reTimes) - 1",
        "        if time_close == array.get(reTimes, i)",
        "            int k = array.get(reKinds, i)",
        "            curScore := array.get(reScores, i)",
        "            color mc = k == 1 ? color.blue : k == 2 ? color.fuchsia : color.gray",
        '            label.new(bar_index, k == 3 ? low : high, array.get(reLabels, i), style=k == 3 ? label.style_label_up : label.style_label_down, color=mc, textcolor=color.white, size=size.tiny)',
        "",
        "if array.size(trTimes) > 0",
        "    for i = 0 to array.size(trTimes) - 1",
        "        if time_close == array.get(trTimes, i)",
        "            label.new(bar_index, high, array.get(trLabels, i), style=label.style_label_down, size=size.tiny)",
        "",
        "if showRangeTable and barstate.islast",
        "    var table t = table.new(position.bottom_right, 2, 6, bgcolor=color.new(color.black, 30))",
        '    table.cell(t, 0, 0, "State", text_color=color.white)',
        "    table.cell(t, 1, 0, activeName, text_color=color.white)",
        '    table.cell(t, 0, 1, "Range?", text_color=color.white)',
        '    table.cell(t, 1, 1, rangeConfirmed ? "yes" : "no", text_color=color.white)',
        '    table.cell(t, 0, 2, "Score", text_color=color.white)',
        '    table.cell(t, 1, 2, str.tostring(curScore, "#.##"), text_color=color.white)',
        '    table.cell(t, 0, 3, "High", text_color=color.white)',
        '    table.cell(t, 1, 3, str.tostring(plotHi, "#.####"), text_color=color.white)',
        '    table.cell(t, 0, 4, "Low", text_color=color.white)',
        '    table.cell(t, 1, 4, str.tostring(plotLo, "#.####"), text_color=color.white)',
        '    table.cell(t, 0, 5, "TF", text_color=color.white)',
        '    table.cell(t, 1, 5, tfOk ? "5m ok" : "NOT 5m", text_color=color.white)',
        "",
        "// EOF",
    ]
    pine_text = "\n".join(lines) + "\n"
    validate_pine_script(pine_text)
    return pine_text


def _push_compare_run_lines(body: list[str], run: Mapping[str, Any], *, prefix: str) -> None:
    y0, m0, d0, h0, mi0 = _pine_ts_parts(run["start_time"])
    y1, m1, d1, h1, mi1 = _pine_ts_parts(run["end_time"])
    code = int(run["state_code"])
    body.append(f"    array.push({prefix}Starts, f_ts({y0}, {m0}, {d0}, {h0}, {mi0}))")
    body.append(f"    array.push({prefix}Ends, f_ts({y1}, {m1}, {d1}, {h1}, {mi1}))")
    body.append(f"    array.push({prefix}States, {code})")


def _push_disagreement_lines(body: list[str], row: Mapping[str, Any]) -> None:
    y, m, d, h, mi = _pine_ts_parts(row["decision_time"])
    kind = int(row.get("kind") or 1)
    label = pine_escape(str(row.get("label") or "C2vsC3"))
    body.append(f"    array.push(discTimes, f_ts({y}, {m}, {d}, {h}, {mi}))")
    body.append(f"    array.push(discKinds, {kind})")
    body.append(f'    array.push(discLabels, "{label}")')


def _comparison_agrees(c2_state: str, c3_state: str) -> bool:
    from research.regime_scanner.trend_regime_classifier import c2_direction, c3_direction

    c2d = c2_direction(c2_state)
    c3d = c3_direction(c3_state)
    return c2d == c3d or (
        c2d in {"transition_up", "transition_down"}
        and c3d in {"transition_up", "transition_down", "pullback_up", "pullback_down"}
    )


def _comparison_kind(c2_state: str, c3_state: str) -> int:
    from research.regime_scanner.trend_regime_classifier import c2_direction, c3_direction

    c2d = c2_direction(c2_state)
    c3d = c3_direction(c3_state)
    if c2d in {"up", "down"} and c3d == "range":
        return 2
    if c2d in {"up", "down"} and c3d.startswith("pullback"):
        return 3
    return 1


def build_c2_c3_comparison_payload(
    c2_timeline: Sequence[Mapping[str, Any]],
    c3_timeline: Sequence[Mapping[str, Any]],
    *,
    max_disagreement_markers: int = PINE_MAX_DISAGREEMENT_MARKERS,
) -> dict[str, Any]:
    """Compact C2-vs-C3 Pine payload using state runs (not per-bar arrays)."""
    c2_rows = [
        {"decision_time": r["decision_time"], "state": r.get("c2_state")}
        for r in c2_timeline
    ]
    c3_rows = [
        {"decision_time": r["decision_time"], "state": r.get("state")}
        for r in c3_timeline
    ]
    c2_runs = timeline_to_state_runs(c2_rows, state_key="state", code_fn=state_code)
    c3_runs = timeline_to_state_runs(c3_rows, state_key="state", code_fn=c3_state_code)

    c3_by = {str(r.get("decision_time")): r for r in c3_timeline}
    markers: list[dict[str, Any]] = []
    prev_agree = True
    for c2 in c2_timeline:
        ts = str(c2.get("decision_time"))
        c3 = c3_by.get(ts)
        if c3 is None:
            continue
        c2_st = str(c2.get("c2_state") or "")
        c3_st = str(c3.get("state") or "")
        agree = _comparison_agrees(c2_st, c3_st)
        if not agree:
            if prev_agree:
                kind = _comparison_kind(c2_st, c3_st)
                label = {1: "DIR", 2: "RNG", 3: "PB"}.get(kind, "C2vsC3")
                markers.append(
                    {
                        "decision_time": ts,
                        "kind": kind,
                        "label": label,
                        "c2_state": c2_st,
                        "c3_state": c3_st,
                    }
                )
            prev_agree = False
            if len(markers) >= max_disagreement_markers:
                break
        else:
            prev_agree = True

    return {
        "c2_runs": c2_runs,
        "c3_runs": c3_runs,
        "disagreement_markers": markers,
        "n_bars": min(len(c2_timeline), len(c3_timeline)),
        "n_disagreement_markers": len(markers),
    }


def build_c3_vs_c2_comparison_pine(
    *,
    title: str,
    symbol: str,
    analyze_start: str,
    analyze_end: str,
    audit_hash: str,
    comparison: Mapping[str, Any],
) -> str:
    c2_runs = list(comparison.get("c2_runs") or [])
    c3_runs = list(comparison.get("c3_runs") or [])
    markers = list(comparison.get("disagreement_markers") or [])

    c2_helpers, c2_calls = _chunked_push_helpers(
        c2_runs,
        prefix="c2",
        push_body=lambda body, run: _push_compare_run_lines(body, run, prefix="c2"),
    )
    c3_helpers, c3_calls = _chunked_push_helpers(
        c3_runs,
        prefix="c3",
        push_body=lambda body, run: _push_compare_run_lines(body, run, prefix="c3"),
    )
    disc_helpers, disc_calls = _chunked_push_helpers(
        markers, prefix="disc", push_body=_push_disagreement_lines
    )
    helper_block = "\n\n".join([*c2_helpers, *c3_helpers, *disc_helpers])
    init_calls = "\n".join([*c2_calls, *disc_calls, *c3_calls]) or "    true"
    lines = [
        *build_pine_header(title),
        f"// C2 baseline vs C3 regime overlay | {escape_pine_string(symbol)} 5m UTC",
        f"// Analyze: {escape_pine_string(analyze_start)} .. {escape_pine_string(analyze_end)}",
        f"// Audit hash: {escape_pine_string(audit_hash)}",
        "// Compact run-based export (state episodes, not per-bar arrays).",
        'showTfWarning = input.bool(true, "Show 5m timeframe warning")',
        'showDisagreements = input.bool(true, "Show C2/C3 disagreement markers")',
        'showSummaryTable = input.bool(true, "Show summary table (last bar)")',
        "",
        "f_ts(y, m, d, h, mi) =>",
        '    timestamp("UTC", y, m, d, h, mi)',
        "",
        "c2Color(code) =>",
        "    switch code",
        "        2 => color.new(color.teal, 92)",
        "        3 => color.new(color.teal, 92)",
        "        4 => color.new(color.teal, 92)",
        "        5 => color.new(color.orange, 90)",
        "        6 => color.new(color.orange, 90)",
        "        7 => color.new(color.red, 92)",
        "        8 => color.new(color.red, 92)",
        "        9 => color.new(color.red, 92)",
        "        10 => color.new(color.green, 90)",
        "        11 => color.new(color.green, 90)",
        "        => color.new(color.gray, 94)",
        "",
        "c3BarTint(code) =>",
        "    switch code",
        "        20 => color.new(color.green, 70)",
        "        21 => color.new(color.red, 70)",
        "        22 => color.new(color.gray, 80)",
        "        23 => color.new(color.teal, 75)",
        "        24 => color.new(color.orange, 75)",
        "        25 => color.new(color.teal, 75)",
        "        26 => color.new(color.orange, 75)",
        "        => color.new(color.silver, 85)",
        "",
        "c2Name(code) =>",
        "    switch code",
        '        2 => "strong_bullish"',
        '        5 => "topping"',
        '        7 => "strong_bearish"',
        '        10 => "bottoming"',
        '        1 => "neutral"',
        '        => "other"',
        "",
        "c3Name(code) =>",
        "    switch code",
        '        20 => "confirmed_uptrend"',
        '        21 => "confirmed_downtrend"',
        '        22 => "range_sideways"',
        '        23 => "bullish_pullback"',
        '        24 => "bearish_pullback"',
        '        25 => "transition_up"',
        '        26 => "transition_down"',
        '        => "unclear"',
        "",
        "var int[] c2Starts = array.new_int()",
        "var int[] c2Ends = array.new_int()",
        "var int[] c2States = array.new_int()",
        "var int[] c3Starts = array.new_int()",
        "var int[] c3Ends = array.new_int()",
        "var int[] c3States = array.new_int()",
        "var int[] discTimes = array.new_int()",
        "var int[] discKinds = array.new_int()",
        "var string[] discLabels = array.new_string()",
        "",
        helper_block,
        "",
        "if barstate.isfirst",
        init_calls,
        "",
        'tfOk = timeframe.in_seconds(timeframe.period) == 300',
        "if showTfWarning and barstate.islast and not tfOk",
        '    label.new(bar_index, high, "WARNING: use APTUSDT 5m UTC", color=color.red, textcolor=color.white)',
        "",
        "int c2Code = 0",
        "int c3Code = 0",
        "if array.size(c2Starts) > 0",
        "    for i = 0 to array.size(c2Starts) - 1",
        "        int s = array.get(c2Starts, i)",
        "        int e = array.get(c2Ends, i)",
        "        if time_close >= s and time_close <= e",
        "            c2Code := array.get(c2States, i)",
        "if array.size(c3Starts) > 0",
        "    for i = 0 to array.size(c3Starts) - 1",
        "        int s3 = array.get(c3Starts, i)",
        "        int e3 = array.get(c3Ends, i)",
        "        if time_close >= s3 and time_close <= e3",
        "            c3Code := array.get(c3States, i)",
        "",
        "bgcolor(c2Color(c2Code))",
        "barcolor(c3BarTint(c3Code))",
        "",
        "if showDisagreements and array.size(discTimes) > 0",
        "    for i = 0 to array.size(discTimes) - 1",
        "        if time_close == array.get(discTimes, i)",
        "            int k = array.get(discKinds, i)",
        "            color mc = k == 2 ? color.blue : k == 3 ? color.purple : color.red",
        '            label.new(bar_index, high, array.get(discLabels, i), style=label.style_label_down, color=mc, textcolor=color.white, size=size.tiny)',
        "",
        "bool inRange = c3Code == 22",
        'string parent = c3Code == 23 ? "up" : c3Code == 24 ? "down" : "—"',
        "bool agree = (c2Code == 2 and c3Code == 20) or (c2Code == 7 and c3Code == 21) or (c2Code == 5 and c3Code == 26) or (c2Code == 10 and c3Code == 25) or (c2Code == 1 and c3Code == 22)",
        "if showSummaryTable and barstate.islast",
        "    var table t = table.new(position.top_right, 2, 6, bgcolor=color.new(color.black, 35))",
        '    table.cell(t, 0, 0, "C2", text_color=color.white)',
        '    table.cell(t, 1, 0, c2Name(c2Code), text_color=color.white)',
        '    table.cell(t, 0, 1, "C3", text_color=color.white)',
        '    table.cell(t, 1, 1, c3Name(c3Code), text_color=color.white)',
        '    table.cell(t, 0, 2, "Parent", text_color=color.white)',
        '    table.cell(t, 1, 2, parent, text_color=color.white)',
        '    table.cell(t, 0, 3, "Range", text_color=color.white)',
        '    table.cell(t, 1, 3, inRange ? "yes" : "no", text_color=color.white)',
        '    table.cell(t, 0, 4, "Match", text_color=color.white)',
        '    table.cell(t, 1, 4, agree ? "yes" : "no", text_color=color.white)',
        '    table.cell(t, 0, 5, "TF", text_color=color.white)',
        '    table.cell(t, 1, 5, tfOk ? "5m ok" : "NOT 5m", text_color=color.white)',
        "",
        "// EOF",
    ]
    pine_text = "\n".join(lines) + "\n"
    validate_pine_script(pine_text)
    return pine_text


def export_c3_pine_bundle(
    *,
    output_dir: Path,
    symbol: str,
    analyze_start: str,
    analyze_end: str,
    audit_hash: str,
    variants: Mapping[str, Mapping[str, Any]],
    recommended_variant: str | None,
    comparison: Mapping[str, Any],
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    per_variant: dict[str, Any] = {}
    for variant_name, payload in variants.items():
        timeline = list(payload.get("timeline_rows") or [])
        runs = timeline_to_state_runs(timeline)
        transitions = limit_pine_transitions(extract_transitions(timeline))
        safe = pine_variant_slug(variant_name)
        pine_path = output_dir / f"trend_audit_c3_{safe}.pine"
        title = f"{symbol} C3 {variant_name}"
        pine_text = build_c3_regime_pine(
            title=title,
            symbol=symbol,
            phase="C3_1_range_calibration",
            variant=variant_name,
            analyze_start=analyze_start,
            analyze_end=analyze_end,
            audit_hash=audit_hash,
            state_runs=runs,
            transitions=transitions,
            markers=[],
            timeline_rows=timeline,
        )
        pine_path.write_text(pine_text, encoding="utf-8")
        _write_csv(output_dir / f"trend_chart_review_{safe}.csv", [dict(r) for r in timeline])
        _write_csv(output_dir / f"trend_transitions_{safe}.csv", transitions)
        per_variant[variant_name] = {
            "pine_path": str(pine_path),
            "pine_sha256": hashlib.sha256(pine_text.encode()).hexdigest(),
        }
    recommended_pine = None
    if recommended_variant and recommended_variant in per_variant:
        src = Path(per_variant[recommended_variant]["pine_path"])
        dst = output_dir / "trend_audit_c3_recommended.pine"
        shutil.copyfile(src, dst)
        recommended_pine = str(dst)
    cmp_path = output_dir / "trend_audit_c3_vs_c2_baseline.pine"
    cmp_text = build_c3_vs_c2_comparison_pine(
        title=f"{symbol} C3 vs C2 baseline",
        symbol=symbol,
        analyze_start=analyze_start,
        analyze_end=analyze_end,
        audit_hash=audit_hash,
        comparison=comparison,
    )
    cmp_path.write_text(cmp_text, encoding="utf-8")
    meta = {
        "phase": "C3_regime_classification",
        "symbol": symbol,
        "timeframe": DEFAULT_TIMEFRAME,
        "analyze_start": analyze_start,
        "analyze_end": analyze_end,
        "audit_hash": audit_hash,
        "recommended_variant": recommended_variant,
        "recommended_pine": recommended_pine,
        "comparison_pine": str(cmp_path),
        "comparison_compact": {
            "n_c2_runs": len(comparison.get("c2_runs") or []),
            "n_c3_runs": len(comparison.get("c3_runs") or []),
            "n_disagreement_markers": len(comparison.get("disagreement_markers") or []),
        },
        "variants": per_variant,
        "c3_state_codes": C3_STATE_CODES,
    }
    meta_path = output_dir / "trend_pine_export.json"
    meta_path.write_text(json.dumps(json_safe(meta), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    meta["metadata_path"] = str(meta_path)
    return meta
