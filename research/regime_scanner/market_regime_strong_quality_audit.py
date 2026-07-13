#!/usr/bin/env python3
"""Chart-review export for K2_H4 regime segments (read-only).

Builds human-checkable interval lists from the long-range audit segments.
Does not modify market_regime.py, policy, or state machine.

Example:
  PYTHONPATH=. python3 -u research/regime_scanner/market_regime_strong_quality_audit.py
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

from research.regime_scanner.point_audit import json_safe

SRC = Path("research/regime_scanner/results/market_regime_long_range_audit")
OUT = Path("research/regime_scanner/results/market_regime_strong_quality_audit")

STRUCTURE = Path("research/regime_scanner/trend_structure.py")
MACHINE = Path("research/regime_scanner/trend_state_machine.py")
POLICY = Path("research/regime_scanner/trend_state_policy.py")
ZONES = Path("research/regime_scanner/trend_zones.py")
MARKET_REGIME = Path("research/regime_scanner/market_regime.py")

SYMBOL = "APTUSDT"
TIMEFRAME = "30m"
BAR_MINUTES = 30
AUDIT_START = pd.Timestamp("2026-01-06T00:00:00+00:00")

DIRECTION = {
    "strong_bullish_trend": "UPTREND",
    "strong_bearish_trend": "DOWNTREND",
    "accumulation_range": "RANGE",
    "transition_unclear": "TRANSITION",
}

SHORT_DIR = {
    "UPTREND": "UP",
    "DOWNTREND": "DOWN",
    "RANGE": "RANGE",
    "TRANSITION": "TRANS",
}

MARCH_REF = (
    "2026-03-05T17:30:00+00:00",
    "2026-03-06T14:30:00+00:00",
)


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object) -> str:
    return _ts(v).isoformat()


def _fmt_human(v: object) -> str:
    t = _ts(v)
    return t.strftime("%Y-%m-%d %H:%M UTC")


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


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


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _f(v: object, default: float = 0.0) -> float:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if x != x:
        return default
    return x


def _b(v: object) -> bool:
    return str(v).lower() in {"1", "true", "yes"}


def classify_quality(seg: dict[str, Any], ev: dict[str, Any] | None) -> tuple[str, list[str]]:
    """Analytical quality tag for chart review (not fed to classifier)."""
    labels: list[str] = []
    regime = seg["regime"]
    bars = int(float(seg["duration_30m_bars"]))
    hours = _f(seg["duration_hours"])
    mfe = _f(seg["max_favorable_excursion_pct"])
    mae = abs(_f(seg["max_adverse_excursion_pct"]))
    chg = abs(_f(seg["price_change_pct"]))
    end_giveback = max(0.0, mfe - chg) if mfe > 0 else 0.0

    if ev:
        if _b(ev.get("possible_premature")):
            labels.append("possible_premature")
        if _b(ev.get("possible_late")):
            labels.append("possible_late")
        if _b(ev.get("possible_false_strong")):
            labels.append("possible_false_strong")
        tags = str(ev.get("analytic_tags") or "")
        if "very_short_strong" in tags:
            labels.append("very_short_strong")

    if regime in {"strong_bullish_trend", "strong_bearish_trend"}:
        if bars <= 2:
            labels.append("two_bar_strong")
            quality = "likely_noise_strong"
        elif chg < 0.35 and bars >= 3:
            labels.append("sideways_flat")
            quality = "sideways_suspicious_strong"
        elif ev and _b(ev.get("possible_late")):
            quality = "exhaustion_or_late_strong"
        elif ev and _b(ev.get("possible_false_strong")):
            quality = "possible_false_strong"
        elif ev and _b(ev.get("possible_premature")):
            quality = "possible_premature"
        elif end_giveback > max(1.0, 0.55 * mfe) and mfe >= 1.5:
            labels.append("high_mfe_giveback")
            quality = "exhaustion_or_late_strong"
        elif bars <= 4 or hours <= 2.5:
            quality = "useful_but_short_strong"
        elif seg.get("previous_regime") in {"accumulation_range", "transition_unclear"} and chg >= 1.0:
            quality = "breakout_strong"
        elif seg.get("previous_regime") in {"strong_bullish_trend", "strong_bearish_trend"}:
            quality = "continuation_strong"
        elif hours >= 4 and chg >= 1.0 and mae < mfe:
            quality = "clearly_valid_strong"
        else:
            quality = "continuation_strong"
    elif regime == "accumulation_range":
        quality = "range_segment"
    else:
        quality = "transition_segment"

    # de-dupe labels preserve order
    seen: set[str] = set()
    uniq = []
    for x in labels:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return quality, uniq


def review_priority(quality: str, labels: list[str], seg: dict[str, Any], ev: dict[str, Any] | None) -> int:
    bars = int(float(seg["duration_30m_bars"]))
    mfe = _f(seg["max_favorable_excursion_pct"])
    chg = abs(_f(seg["price_change_pct"]))
    mae = abs(_f(seg["max_adverse_excursion_pct"]))
    giveback = max(0.0, mfe - chg)

    p1 = {
        "likely_noise_strong",
        "sideways_suspicious_strong",
        "exhaustion_or_late_strong",
        "possible_false_strong",
        "possible_premature",
        "possible_late",
    }
    if quality in p1 or bars <= 2 or "possible_premature" in labels or "possible_late" in labels or "possible_false_strong" in labels:
        return 1
    if giveback > max(1.0, 0.55 * mfe) and mfe >= 1.5:
        return 1

    p2 = {"useful_but_short_strong", "continuation_strong", "breakout_strong"}
    if quality in p2:
        return 2
    if mae > max(1.5, mfe):
        return 2
    if seg.get("previous_regime") in {"accumulation_range", "transition_unclear"} and seg["regime"].startswith("strong_"):
        return 2
    if quality == "clearly_valid_strong":
        return 3
    return 3 if not seg["regime"].startswith("strong_") else 2


def confirmation_bars(regime: str) -> int:
    if regime in {"strong_bullish_trend", "strong_bearish_trend"}:
        return 2
    if regime == "accumulation_range":
        return 3
    return 2


def chart_note(direction: str, quality: str, start: str, end_close: str) -> str:
    if direction == "DOWNTREND":
        base = f"War ab {start} bis {end_close} im 30m-Chart ein klarer Abwärtstrend sichtbar?"
        if quality in {"useful_but_short_strong", "likely_noise_strong"}:
            return f"War dieser Abwärtstrend nur ein kurzer Bounce/Pull innerhalb eines größeren Kontexts? ({base})"
        if quality in {"sideways_suspicious_strong", "possible_false_strong"}:
            return f"Sieht dieses Fenster eher nach Seitwärts/Noise aus statt nach DOWNTREND? ({base})"
        if quality in {"exhaustion_or_late_strong", "possible_late"}:
            return f"Kam der Strong-Label erst, nachdem der Move schon weitgehend vorbei war? ({base})"
        return base
    if direction == "UPTREND":
        base = f"War ab {start} bis {end_close} im 30m-Chart ein klarer Aufwärtstrend sichtbar?"
        if quality in {"useful_but_short_strong", "likely_noise_strong"}:
            return f"War dieser Aufwärtstrend nur ein kurzer Bounce innerhalb eines größeren Abwärtstrends? ({base})"
        if quality in {"sideways_suspicious_strong", "possible_false_strong"}:
            return f"Sieht dieses Fenster eher nach Seitwärts/Noise aus statt nach UPTREND? ({base})"
        if quality in {"exhaustion_or_late_strong", "possible_late"}:
            return f"Kam der Strong-Label erst nach dem Großteil der Aufwärtsbewegung? ({base})"
        return base
    if direction == "RANGE":
        return f"War {start}–{end_close} im 30m-Chart klar seitwärts / ohne gerichteten Fortschritt?"
    return f"War {start}–{end_close} eher Übergang/unklar als stabiler Trend oder Range?"


def manual_trend_question(direction: str, quality: str, start: str, end_close: str) -> str:
    return chart_note(direction, quality, start, end_close)


def enrich_segments(
    segs: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    ev_by_id = {str(e["segment_id"]): e for e in events}
    rows: list[dict[str, Any]] = []
    for i, seg in enumerate(segs):
        # Timestamp semantics (classifier decision_time = close of 30m bar):
        # start_timestamp_utc = decision_time of first bar (regime effective after close)
        # end_timestamp_utc   = OPEN of last bar in segment
        # start_candle_open   = open of first bar
        # end_candle_close    = close of last bar (= decision_time of last bar)
        start_decision = _ts(seg["start_timestamp"])
        end_decision = _ts(seg["end_timestamp"])
        open_first = _ts(seg["candle_open_start"])
        open_last = _ts(seg["candle_open_end"])
        close_last = open_last + pd.Timedelta(minutes=BAR_MINUTES)
        # sanity: end_decision should equal close_last
        if abs((end_decision - close_last).total_seconds()) > 1:
            close_last = end_decision
            open_last = close_last - pd.Timedelta(minutes=BAR_MINUTES)

        regime = seg["regime"]
        direction = DIRECTION[regime]
        ev = ev_by_id.get(str(seg["segment_id"]))
        quality, labels = classify_quality(seg, ev)
        prio = review_priority(quality, labels, seg, ev)
        review_id = f"REVIEW_{i + 1:04d}"

        context_start = start_decision - pd.Timedelta(hours=12)
        context_end = close_last + pd.Timedelta(hours=12)

        rows.append(
            {
                "review_id": review_id,
                "segment_id": seg["segment_id"],
                "symbol": SYMBOL,
                "timeframe": TIMEFRAME,
                "regime": regime,
                "direction": direction,
                "start_timestamp_utc": _iso(start_decision),
                "end_timestamp_utc": _iso(open_last),
                "start_candle_open_utc": _iso(open_first),
                "end_candle_close_utc": _iso(close_last),
                "start_price": _f(seg["start_price"]),
                "end_price": _f(seg["end_price"]),
                "price_change_pct": _f(seg["price_change_pct"]),
                "duration_30m_bars": int(float(seg["duration_30m_bars"])),
                "duration_hours": _f(seg["duration_hours"]),
                "previous_regime": seg.get("previous_regime") or "",
                "next_regime": seg.get("next_regime") or "",
                "raw_candidate_at_start": regime,  # held regime at first emitted bar
                "confirmation_bars": confirmation_bars(regime),
                "strong_start_timestamp_utc": _iso(start_decision)
                if regime.startswith("strong_")
                else "",
                "strong_end_timestamp_utc": _iso(close_last) if regime.startswith("strong_") else "",
                "mfe_pct": _f(seg["max_favorable_excursion_pct"]),
                "mae_pct": _f(seg["max_adverse_excursion_pct"]),
                "existing_labels": "|".join(labels) if labels else "keine",
                "quality_classification": quality,
                "review_priority": prio,
                "chart_review_note": chart_note(
                    direction, quality, _fmt_human(start_decision), _fmt_human(close_last)
                ),
                "context_start_utc": _iso(context_start),
                "context_end_utc": _iso(context_end),
                "possible_premature": _b(ev.get("possible_premature")) if ev else False,
                "possible_late": _b(ev.get("possible_late")) if ev else False,
                "possible_false_strong": _b(ev.get("possible_false_strong")) if ev else False,
                "bars_before_confirmation": max(0, confirmation_bars(regime) - 1)
                if regime.startswith("strong_")
                else "",
            }
        )
    return rows


def build_trend_rows(intervals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in intervals:
        if r["regime"] not in {"strong_bullish_trend", "strong_bearish_trend"}:
            continue
        out.append(
            {
                "review_id": r["review_id"],
                "trend_direction": r["direction"],
                "trend_start_utc": r["start_timestamp_utc"],
                "trend_end_utc": r["end_timestamp_utc"],
                "trend_end_close_utc": r["end_candle_close_utc"],
                "duration": f"{r['duration_hours']}h / {r['duration_30m_bars']}×30m",
                "duration_hours": r["duration_hours"],
                "duration_30m_bars": r["duration_30m_bars"],
                "start_price": r["start_price"],
                "end_price": r["end_price"],
                "price_change_pct": r["price_change_pct"],
                "mfe_pct": r["mfe_pct"],
                "mae_pct": r["mae_pct"],
                "previous_regime": r["previous_regime"],
                "next_regime": r["next_regime"],
                "bars_before_confirmation": r["bars_before_confirmation"],
                "possible_premature": r["possible_premature"],
                "possible_late": r["possible_late"],
                "possible_false_strong": r["possible_false_strong"],
                "quality_classification": r["quality_classification"],
                "manual_chart_question": manual_trend_question(
                    r["direction"],
                    r["quality_classification"],
                    _fmt_human(r["start_timestamp_utc"]),
                    _fmt_human(r["end_candle_close_utc"]),
                ),
                "context_start_utc": r["context_start_utc"],
                "context_end_utc": r["context_end_utc"],
                "review_priority": r["review_priority"],
                "existing_labels": r["existing_labels"],
            }
        )
    return out


def build_timeline_txt(trends: list[dict[str, Any]]) -> str:
    blocks = []
    for r in trends:
        rid = r["review_id"].replace("REVIEW_", "")
        blocks.append(
            "\n".join(
                [
                    "============================================================",
                    f"REVIEW {rid}",
                    f"{r['trend_direction']} / { 'strong_bearish_trend' if r['trend_direction']=='DOWNTREND' else 'strong_bullish_trend' }",
                    f"Von: {_fmt_human(r['trend_start_utc'])}",
                    f"Bis: {_fmt_human(r['trend_end_close_utc'])}",
                    f"Dauer: {r['duration_hours']} Stunden / {r['duration_30m_bars']} × 30m-Bars",
                    f"Preis: {r['start_price']} -> {r['end_price']}",
                    f"Veränderung: {r['price_change_pct']:.2f} %",
                    f"MFE: {r['mfe_pct']:.2f} %",
                    f"MAE: {r['mae_pct']:.2f} %",
                    f"Vorher: {r['previous_regime'] or '—'}",
                    f"Danach: {r['next_regime'] or '—'}",
                    f"Bewertung: {r['quality_classification']}",
                    f"Auditlabels: {r['existing_labels']}",
                    f"Priorität: {r['review_priority']}",
                    f"Chart-Kontext: {_fmt_human(r['context_start_utc'])} -> {_fmt_human(r['context_end_utc'])}",
                    "Chart-Prüfung:",
                    r["manual_chart_question"],
                    "============================================================",
                ]
            )
        )
    return "\n\n".join(blocks) + "\n"


def build_markers(intervals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    markers = []
    for r in intervals:
        dshort = SHORT_DIR[r["direction"]]
        markers.append(
            {
                "timestamp_utc": r["start_timestamp_utc"],
                "marker_type": "START",
                "regime": r["regime"],
                "direction": r["direction"],
                "review_id": r["review_id"],
                "price": r["start_price"],
                "label": f"{dshort} START",
            }
        )
        markers.append(
            {
                "timestamp_utc": r["end_candle_close_utc"],
                "marker_type": "END",
                "regime": r["regime"],
                "direction": r["direction"],
                "review_id": r["review_id"],
                "price": r["end_price"],
                "label": f"{dshort} END",
            }
        )
    return markers


def strong_quality_cases(trends: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return list(trends)


def best_worst(trends: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    scored = []
    for r in trends:
        # higher absolute move + longer + priority 3 = better
        score = abs(_f(r["price_change_pct"])) + 0.3 * _f(r["duration_hours"]) - 2.0 * (4 - int(r["review_priority"]))
        if r["quality_classification"] == "clearly_valid_strong":
            score += 3
        if r["quality_classification"] in {
            "likely_noise_strong",
            "sideways_suspicious_strong",
            "possible_false_strong",
            "possible_premature",
        }:
            score -= 5
        scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    best = [r for _, r in scored[:25]]
    worst = [r for _, r in sorted(scored, key=lambda x: x[0])[:25]]
    return best, worst


def monthly_exports(trends: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_m: dict[str, list[dict[str, Any]]] = {"2026_01": [], "2026_02": [], "2026_03": []}
    for r in trends:
        key = _ts(r["trend_start_utc"]).strftime("%Y_%m")
        if key in by_m:
            by_m[key].append(r)
    return by_m


def monthly_text(trends: list[dict[str, Any]], month: int) -> str:
    names = {1: "JANUAR", 2: "FEBRUAR", 3: "MÄRZ"}
    lines = [f"{names[month]} 2026", ""]
    for r in trends:
        if _ts(r["trend_start_utc"]).month != month:
            continue
        tag = "UP  " if r["trend_direction"] == "UPTREND" else "DOWN"
        lines.append(
            f"{tag}  {_fmt_human(r['trend_start_utc'])} -> {_fmt_human(r['trend_end_close_utc'])}"
        )
    lines.append("")
    return "\n".join(lines)


def pine_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _pine_push_lines(
    trends: list[dict[str, Any]], chunk_size: int = 12
) -> tuple[list[str], list[str]]:
    """Split interval pushes into small helper functions (avoids huge local blocks)."""
    helpers: list[str] = []
    calls: list[str] = []
    if not trends:
        helpers.append("f_load_00() =>\n    true")
        calls.append("    f_load_00()")
        return helpers, calls
    for chunk_i in range(0, len(trends), chunk_size):
        chunk = trends[chunk_i : chunk_i + chunk_size]
        name = f"f_load_{chunk_i // chunk_size:02d}"
        body = [f"{name}() =>"]
        for r in chunk:
            t0 = _ts(r["trend_start_utc"])
            t1 = _ts(r["trend_end_close_utc"])
            d = 1 if r["trend_direction"] == "UPTREND" else -1
            rid = pine_escape(r["review_id"])
            body.append(
                f"    array.push(starts, f_ts({t0.year}, {t0.month}, {t0.day}, {t0.hour}, {t0.minute}))"
            )
            body.append(
                f"    array.push(ends, f_ts({t1.year}, {t1.month}, {t1.day}, {t1.hour}, {t1.minute}))"
            )
            body.append(f"    array.push(dirs, {d})")
            body.append(f'    array.push(ids, "{rid}")')
        helpers.append("\n".join(body))
        calls.append(f"    {name}()")
    return helpers, calls


def build_pine(trends: list[dict[str, Any]], title: str) -> str:
    """Compact Pine v6 review aid: arrays + bgcolor (no giant boxes).

    Timestamp semantics (must match CSV):
      starts[] = trend_start_utc = classifier decision_time = close of first 30m bar
      ends[]   = trend_end_close_utc = close of last 30m bar
    On a 30m chart, highlight bars with time_close in [start, end].
    Markers fire when time_close equals start/end.
    """
    helpers, calls = _pine_push_lines(trends, chunk_size=12)
    helper_block = "\n\n".join(helpers) if helpers else "f_load_00() =>\n    true"
    call_block = "\n".join(calls) if calls else "    f_load_00()"

    return f"""//@version=6
indicator(
     "{pine_escape(title)}",
     overlay = true,
     max_labels_count = 500
)

// READ-ONLY chart review aid. No regime calculation. No signals.
// Data: audited K2_H4 strong intervals from market_regime_strong_quality_audit.
//
// Timestamp semantics:
//   starts = trend_start_utc      = classifier decision_time (close of first 30m bar)
//   ends   = trend_end_close_utc  = close of last 30m bar in the segment
// Use on APTUSDT 30m. Prefer chart timezone UTC.

f_ts(y, m, d, h, mi) =>
    timestamp("UTC", y, m, d, h, mi)

var int[] starts = array.new_int()
var int[] ends = array.new_int()
var int[] dirs = array.new_int()
var string[] ids = array.new_string()

{helper_block}

if barstate.isfirst
{call_block}

// Active interval for current bar (30m): include bars whose close is inside [start, end].
int activeDir = 0
string activeId = ""
if array.size(starts) > 0
    for i = 0 to array.size(starts) - 1
        int startTs = array.get(starts, i)
        int endTs = array.get(ends, i)
        bool inside = time_close >= startTs and time_close <= endTs
        if inside
            activeDir := array.get(dirs, i)
            activeId := array.get(ids, i)

bgcolor(
     activeDir == 1 ? color.new(color.green, 85) :
     activeDir == -1 ? color.new(color.red, 85) :
     na
)

// Start / end markers only on the matching closed bar (time_close == exported UTC close).
bool isStart = false
bool isEnd = false
string markerId = ""
int markerDir = 0
if array.size(starts) > 0
    for i = 0 to array.size(starts) - 1
        int startTs = array.get(starts, i)
        int endTs = array.get(ends, i)
        if time_close == startTs
            isStart := true
            markerId := array.get(ids, i)
            markerDir := array.get(dirs, i)
        if time_close == endTs
            isEnd := true
            markerId := array.get(ids, i)
            markerDir := array.get(dirs, i)

if isStart
    label.new(
         bar_index,
         markerDir == 1 ? low : high,
         markerId + (markerDir == 1 ? " UP START" : " DOWN START"),
         style = markerDir == 1 ? label.style_label_up : label.style_label_down,
         color = color.new(color.black, 30),
         textcolor = color.white,
         size = size.tiny
     )

if isEnd
    label.new(
         bar_index,
         markerDir == 1 ? high : low,
         markerId + " END",
         style = markerDir == 1 ? label.style_label_down : label.style_label_up,
         color = color.new(color.black, 30),
         textcolor = color.white,
         size = size.tiny
     )

// EOF
"""


def parse_pine_intervals(pine_text: str) -> list[dict[str, Any]]:
    """Extract pushed intervals from generated Pine source."""
    # Match consecutive push quartets in helper bodies
    starts = re.findall(
        r'array\.push\(starts, f_ts\((\d+), (\d+), (\d+), (\d+), (\d+)\)\)',
        pine_text,
    )
    ends = re.findall(
        r'array\.push\(ends, f_ts\((\d+), (\d+), (\d+), (\d+), (\d+)\)\)',
        pine_text,
    )
    dirs = re.findall(r"array\.push\(dirs, (-?\d+)\)", pine_text)
    ids = re.findall(r'array\.push\(ids, "(REVIEW_\d+)"\)', pine_text)
    out = []
    for i, sid in enumerate(ids):
        y, m, d, h, mi = map(int, starts[i])
        y2, m2, d2, h2, mi2 = map(int, ends[i])
        start = pd.Timestamp(year=y, month=m, day=d, hour=h, minute=mi, tz="UTC")
        end = pd.Timestamp(year=y2, month=m2, day=d2, hour=h2, minute=mi2, tz="UTC")
        out.append(
            {
                "review_id": sid,
                "start": _iso(start),
                "end": _iso(end),
                "dir": int(dirs[i]),
            }
        )
    return out


def validate_pine_file(
    pine_path: Path,
    expected_trends: list[dict[str, Any]],
    *,
    month: int | None = None,
) -> dict[str, Any]:
    text = pine_path.read_text(encoding="utf-8")
    parsed = parse_pine_intervals(text)
    exp_ids = [r["review_id"] for r in expected_trends]
    got_ids = [p["review_id"] for p in parsed]
    checks = {
        "file": str(pine_path),
        "n_expected": len(expected_trends),
        "n_parsed": len(parsed),
        "count_match": len(parsed) == len(expected_trends),
        "ids_exact_once": sorted(got_ids) == sorted(exp_ids) and len(got_ids) == len(set(got_ids)),
        "no_box_new": "box.new" not in text,
        "no_scale_distorting_box": "1.0e10" not in text and "1e10" not in text,
        "uses_utc_timestamp": 'timestamp("UTC"' in text or "timestamp('UTC'" in text,
        "uses_bgcolor": "bgcolor(" in text,
        "version6": text.lstrip().startswith("//@version=6"),
        "no_giant_box_label_block": text.count("box.new") == 0 and text.count("label.new") <= 4,
    }
    start_ok = end_ok = True
    by_id = {p["review_id"]: p for p in parsed}
    for r in expected_trends:
        p = by_id.get(r["review_id"])
        if p is None:
            start_ok = False
            end_ok = False
            continue
        if p["start"] != _iso(_ts(r["trend_start_utc"])):
            start_ok = False
        if p["end"] != _iso(_ts(r["trend_end_close_utc"])):
            end_ok = False
        if month is not None and _ts(r["trend_start_utc"]).month != month:
            checks["month_filter_ok"] = False
    checks["starts_match_csv"] = start_ok
    checks["ends_match_end_candle_close"] = end_ok
    if month is not None:
        checks.setdefault("month_filter_ok", True)
        checks["month_filter_ok"] = checks["month_filter_ok"] and all(
            _ts(r["trend_start_utc"]).month == month for r in expected_trends
        ) and all(_ts(p["start"]).month == month for p in parsed)
    checks["all_ok"] = all(v for k, v in checks.items() if k not in {"file", "n_expected", "n_parsed"})
    return checks


def completeness(
    segs: list[dict[str, Any]],
    intervals: list[dict[str, Any]],
    trends: list[dict[str, Any]],
    markers: list[dict[str, Any]],
    timeline_txt: str,
    pine_mar: str,
) -> dict[str, Any]:
    checks = {}
    checks["segments_eq_intervals"] = len(segs) == len(intervals)
    checks["each_segment_once"] = len({r["segment_id"] for r in intervals}) == len(intervals)
    strong_segs = [s for s in segs if s["regime"].startswith("strong_")]
    checks["strong_eq_trends"] = len(strong_segs) == len(trends)
    # markers: 2 per interval
    checks["markers_2_per_interval"] = len(markers) == 2 * len(intervals)
    trend_ids = {r["review_id"] for r in trends}
    start_m = {m["review_id"] for m in markers if m["marker_type"] == "START" and m["regime"].startswith("strong_")}
    end_m = {m["review_id"] for m in markers if m["marker_type"] == "END" and m["regime"].startswith("strong_")}
    checks["each_trend_has_start_end_marker"] = trend_ids <= start_m and trend_ids <= end_m

    ordered = True
    non_overlap = True
    start_before_end = True
    for a, b in zip(intervals, intervals[1:]):
        if _ts(a["start_timestamp_utc"]) > _ts(b["start_timestamp_utc"]):
            ordered = False
        # abutting OK: previous close == next open; overlap if previous close > next open
        if _ts(a["end_candle_close_utc"]) > _ts(b["start_candle_open_utc"]):
            non_overlap = False
        if not (_ts(a["start_timestamp_utc"]) < _ts(a["end_candle_close_utc"])):
            start_before_end = False
    if intervals and not (_ts(intervals[-1]["start_timestamp_utc"]) < _ts(intervals[-1]["end_candle_close_utc"])):
        start_before_end = False

    checks["chronologically_sorted"] = ordered
    checks["no_overlap"] = non_overlap
    checks["start_before_end_close"] = start_before_end
    checks["all_utc"] = all("+" in r["start_timestamp_utc"] or "Z" in r["start_timestamp_utc"] for r in intervals)
    checks["no_warmup_before_jan6"] = all(_ts(r["start_timestamp_utc"]) >= AUDIT_START for r in intervals)

    trend_starts = {_iso(_ts(r["trend_start_utc"])) for r in trends}
    checks["march_ref_in_trends"] = set(MARCH_REF) <= trend_starts
    checks["march_ref_in_timeline_txt"] = (
        "2026-03-05 17:30 UTC" in timeline_txt and "2026-03-06 14:30 UTC" in timeline_txt
    )
    marker_ts = {_iso(_ts(m["timestamp_utc"])) for m in markers if m["marker_type"] == "START"}
    checks["march_ref_in_markers"] = set(MARCH_REF) <= marker_ts
    checks["march_ref_in_pine_march"] = (
        'f_ts(2026, 3, 5, 17, 30)' in pine_mar
        and 'f_ts(2026, 3, 6, 14, 30)' in pine_mar
        and "REVIEW_0336" in pine_mar
        and "REVIEW_0340" in pine_mar
    )
    checks["pine_no_box_new"] = "box.new" not in pine_mar
    checks["pine_uses_utc"] = 'timestamp("UTC"' in pine_mar

    checks["all_passed"] = all(checks.values())
    return checks


def preview(trends: list[dict[str, Any]]) -> str:
    ups = [r for r in trends if r["trend_direction"] == "UPTREND"][:10]
    downs = [r for r in trends if r["trend_direction"] == "DOWNTREND"][:10]
    top = sorted(trends, key=lambda r: (int(r["review_priority"]), -abs(_f(r["price_change_pct"]))))[:15]
    # priority 1 first
    top = sorted(trends, key=lambda r: (int(r["review_priority"]), -abs(_f(r["mfe_pct"]))))[:15]

    def line(r: dict[str, Any]) -> str:
        return (
            f"  {r['review_id']} {_fmt_human(r['trend_start_utc'])} -> {_fmt_human(r['trend_end_close_utc'])} "
            f"| {r['trend_direction']} | Δ={_f(r['price_change_pct']):+.2f}% | {r['quality_classification']}"
        )

    parts = ["ERSTE 10 UPTRENDS"]
    parts += [line(r) for r in ups] or ["  (keine)"]
    parts += ["", "ERSTE 10 DOWNTRENDS"]
    parts += [line(r) for r in downs] or ["  (keine)"]
    parts += ["", "15 HÖCHSTE REVIEW-PRIORITÄTEN"]
    parts += [
        f"  P{r['review_priority']} {r['review_id']} {_fmt_human(r['trend_start_utc'])} -> {_fmt_human(r['trend_end_close_utc'])} / {r['quality_classification']} / {r['existing_labels']}"
        for r in top
    ]
    parts += [
        "",
        "MÄRZ-REFERENZ",
        "  DOWNTREND Beginn: 2026-03-05 17:30 UTC",
        "  DOWNTREND Beginn: 2026-03-06 14:30 UTC",
    ]
    # verify present
    for ref in MARCH_REF:
        hit = next((r for r in trends if _iso(_ts(r["trend_start_utc"])) == ref), None)
        if hit:
            parts.append(
                f"  OK {ref} -> {_fmt_human(hit['trend_end_close_utc'])} Δ={_f(hit['price_change_pct']):+.2f}%"
            )
        else:
            parts.append(f"  MISSING {ref}")
    return "\n".join(parts) + "\n"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    hashes_before = {
        "trend_structure.py": _md5(STRUCTURE),
        "trend_state_machine.py": _md5(MACHINE),
        "trend_state_policy.py": _md5(POLICY),
        "trend_zones.py": _md5(ZONES),
        "market_regime.py": _md5(MARKET_REGIME),
    }
    _write_json(OUT / "hashes_before.json", hashes_before)

    segs = _read_csv(SRC / "regime_segments.csv")
    events = _read_csv(SRC / "strong_regime_events.csv")
    # ensure chronological
    segs = sorted(segs, key=lambda r: _ts(r["start_timestamp"]))

    intervals = enrich_segments(segs, events)
    trends = build_trend_rows(intervals)
    markers = build_markers(intervals)
    timeline_txt = build_timeline_txt(trends)
    cases = strong_quality_cases(trends)
    best, worst = best_worst(trends)
    by_m = monthly_exports(trends)

    _write_csv(OUT / "chart_review_intervals.csv", intervals)
    _write_csv(OUT / "trend_chart_review.csv", trends)
    _write_csv(OUT / "chart_regime_markers.csv", markers)
    _write_csv(OUT / "strong_quality_cases.csv", cases)
    _write_csv(OUT / "best_strong_cases.csv", best)
    _write_csv(OUT / "worst_strong_cases.csv", worst)
    (OUT / "chart_review_timeline.txt").write_text(timeline_txt, encoding="utf-8")

    for key, rows in by_m.items():
        _write_csv(OUT / f"chart_review_{key}.csv", rows)
    (OUT / "chart_review_months.txt").write_text(
        monthly_text(trends, 1) + "\n" + monthly_text(trends, 2) + "\n" + monthly_text(trends, 3),
        encoding="utf-8",
    )

    # Pine: monthly primary + full + smoke test
    pine_all = build_pine(trends, "K2_H4 Strong Regime Chart Review")
    (OUT / "market_regime_chart_review.pine").write_text(pine_all, encoding="utf-8")
    pine_files: dict[str, str] = {"all": str(OUT / "market_regime_chart_review.pine")}
    pine_validation: dict[str, Any] = {}
    for key, rows in by_m.items():
        y, m = key.split("_")
        name = f"market_regime_chart_review_{y}_{m}.pine"
        path = OUT / name
        path.write_text(build_pine(rows, f"K2_H4 Strong Review {y}-{m}"), encoding="utf-8")
        pine_files[key] = str(path)
        pine_validation[key] = validate_pine_file(path, rows, month=int(m))

    pine_validation["all"] = validate_pine_file(OUT / "market_regime_chart_review.pine", trends)

    smoke = [
        r
        for r in trends
        if r["review_id"] in {"REVIEW_0336", "REVIEW_0340"}
    ]
    smoke_path = OUT / "market_regime_chart_review_smoke_test.pine"
    smoke_path.write_text(
        build_pine(smoke, "K2_H4 Smoke Test March Refs"),
        encoding="utf-8",
    )
    pine_files["smoke"] = str(smoke_path)
    pine_validation["smoke"] = validate_pine_file(smoke_path, smoke, month=3)

    pine_mar = (OUT / "market_regime_chart_review_2026_03.pine").read_text(encoding="utf-8")
    checks = completeness(segs, intervals, trends, markers, timeline_txt, pine_mar)
    checks["pine_validation"] = pine_validation
    checks["pine_all_ok"] = all(v.get("all_ok") for v in pine_validation.values())
    checks["all_passed"] = bool(checks["all_passed"] and checks["pine_all_ok"])

    meta = {
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "source": str(SRC),
        "timestamp_semantics": {
            "classifier_timestamp": (
                "MarketRegimeClassifier uses decision_time = candle_open + 30m "
                "(close of the completed 30m bar)."
            ),
            "start_timestamp_utc": "decision_time / close of first segment bar (regime becomes active)",
            "end_timestamp_utc": "open of last segment bar",
            "start_candle_open_utc": "open of first segment bar",
            "end_candle_close_utc": "close of last segment bar (= last decision_time)",
            "example": {
                "start_timestamp_utc": "2026-03-05T17:30:00+00:00",
                "end_timestamp_utc": "open of last bar",
                "end_candle_close_utc": "open + 30m",
            },
        },
        "direction_mapping": DIRECTION,
        "n_intervals": len(intervals),
        "n_trends": len(trends),
        "priority_counts": dict(Counter(int(r["review_priority"]) for r in trends)),
        "quality_counts": dict(Counter(r["quality_classification"] for r in trends)),
        "completeness": checks,
        "march_references": list(MARCH_REF),
        "pine_files": pine_files,
        "pine_validation": pine_validation,
        "read_only": True,
        "market_regime_py_unchanged": True,
    }
    _write_json(OUT / "audit_metadata.json", meta)

    preview_txt = preview(trends)
    (OUT / "chart_review_preview.txt").write_text(preview_txt, encoding="utf-8")

    hashes_after = {
        "trend_structure.py": _md5(STRUCTURE),
        "trend_state_machine.py": _md5(MACHINE),
        "trend_state_policy.py": _md5(POLICY),
        "trend_zones.py": _md5(ZONES),
        "market_regime.py": _md5(MARKET_REGIME),
    }
    assert hashes_before == hashes_after
    _write_json(OUT / "hashes_after.json", hashes_after)

    files = sorted(str(p) for p in OUT.iterdir() if p.is_file())
    _write_json(
        OUT / "summary.json",
        {
            "completeness_all_passed": checks["all_passed"],
            "checks": checks,
            "n_intervals": len(intervals),
            "n_trends": len(trends),
            "files": files,
            "preview": preview_txt,
            "hashes_unchanged": True,
        },
    )

    (OUT / "README.md").write_text(
        f"""# Market Regime Strong Quality / Chart Review

Read-only chart checklist for K2_H4 segments from the long-range audit.

Primary file: `chart_review_intervals.csv`
Strong-only: `trend_chart_review.csv`
Readable: `chart_review_timeline.txt`
Markers: `chart_regime_markers.csv`
Pine: `market_regime_chart_review*.pine`

Completeness all_passed={checks['all_passed']}

## Timestamp semantics

Classifier decision_time = 30m candle close (open+30m).
See `audit_metadata.json`.
""",
        encoding="utf-8",
    )

    print(preview_txt)
    print(f"completeness_all_passed={checks['all_passed']}")
    print("FILES:")
    for f in files:
        print(f"  {f}")


if __name__ == "__main__":
    main()
