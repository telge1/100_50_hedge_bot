#!/usr/bin/env python3
"""
Phase L1 — isolated pilot: fixed SL 1.20 %, TP per upper channel / upper D2 pools.
Read-only; does not modify strategies, drawings, or dashboard code.

Pilot mode: RETROSPECTIVE_DRAWINGS (line anchors not proven causally available at touch time).
"""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

PROBE_DIR = Path(__file__).resolve().parent
ROOT = PROBE_DIR.parents[1]
REPO = PROBE_DIR.parents[2]
_TOUCH_DIR = ROOT / "research" / "manual_line_touch_probe_v1"
for p in (str(ROOT), str(_TOUCH_DIR), str(REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

from line_geometry import (  # noqa: E402
    BAR_SEC_5M,
    LineDef,
    classify_candle_interval,
    fmt_utc,
    utc_ts,
)

OUT_DIR = PROBE_DIR
DRAWINGS_PATH = ROOT / "research_charts" / "user_data" / "drawings.json"

WINDOW_START = utc_ts(datetime(2026, 8, 25, 0, 0, 0, tzinfo=timezone.utc))
WINDOW_END = utc_ts(datetime(2026, 8, 29, 0, 0, 0, tzinfo=timezone.utc))
SYMBOL = "DOGEUSDT"
TIMEFRAME = "5m"
CANDLE_LOAD_LIMIT = 4000

SL_PCT = 1.20
MIN_POOL_DIST_PCT = 0.50
CHANNEL_NEAR_PCT = 2.0
MAX_CHANNEL_UPPER_SPAN_PCT = 12.0
TOUCH_ZONE_MAX_DIST_PCT = 0.20
TOUCH_RESET_ABOVE_LINE_PCT = 0.30
CONFIRM_VARIANT = "C1"
POOL_TIMEFRAME = "5m"
POOL_DETAIL = 1
PILOT_LABEL = "RETROSPECTIVE_DRAWINGS_PILOT"
# Upper main channel — exit target only; never a long touch trigger (Long rule 1).
UPPER_MAIN_CHANNEL_LINE_ID = "d-22f8d9f720ce"
APPROACH_EPSILON_PCT = 0.05
LOWER_CHANNEL_STRICT_EPS = 1e-9
# Explicit retrospective pilot allowlist (research release — not auto-detected final role).
PILOT_ALLOWED_LOWER_TREND_IDS: frozenset[str] = frozenset(
    {
        "d-03350098db64",
        "d-0e5e7004dccd",
        "d-1ff495203465",
        "d-248d74050cc8",
        "d-a6d9a05da2f1",
    }
)


def _bootstrap_import_paths() -> None:
    trading = Path("/home/telgenbuescher/projects/trading_research_platform")
    pools = Path("/home/telgenbuescher/projects/pools+ob+delta_bot")
    for p in (trading, pools):
        text = str(p)
        if p.is_dir() and text not in sys.path:
            sys.path.insert(0, text)


def line_from_drawing(d: dict[str, Any]) -> LineDef | None:
    if str(d.get("symbol") or "").upper() != SYMBOL:
        return None
    kind = str(d.get("drawing_type") or "")
    if kind not in ("trend", "hline"):
        return None
    if d.get("visible") is False:
        return None
    meta = d.get("metadata") or {}
    did = str(d.get("drawing_id") or "")
    ext = bool(meta.get("extend_right"))
    if kind == "hline":
        if d.get("price") is None:
            return None
        return LineDef(
            drawing_id=did,
            drawing_type="hline",
            symbol=SYMBOL,
            extend_right=False,
            price=float(d["price"]),
        )
    for key in ("start_timestamp", "start_price", "end_timestamp", "end_price"):
        if d.get(key) is None:
            return None
    t1, t2 = float(d["start_timestamp"]), float(d["end_timestamp"])
    if t1 <= 0 or t2 <= 0:
        return None
    return LineDef(
        drawing_id=did,
        drawing_type="trend",
        symbol=SYMBOL,
        extend_right=ext,
        t1=t1,
        p1=float(d["start_price"]),
        t2=t2,
        p2=float(d["end_price"]),
    )


def load_candles(
    *,
    start: int | None = None,
    end: int | None = None,
) -> list[dict[str, Any]]:
    from research_charts.service import candle_objects

    ws = WINDOW_START if start is None else start
    we = WINDOW_END if end is None else end
    rows = candle_objects(
        SYMBOL,
        TIMEFRAME,
        start=ws,
        end=we,
        limit=CANDLE_LOAD_LIMIT,
        allow_stale=True,
    )
    out: list[dict[str, Any]] = []
    for c in rows:
        ts = getattr(c, "timestamp", None) or getattr(c, "time", None)
        if ts is None:
            continue
        if hasattr(ts, "timestamp"):
            t = int(ts.timestamp())
        else:
            t = int(ts)
        if t < ws or t >= we:
            continue
        out.append(
            {
                "time": t,
                "open": float(c.open),
                "high": float(c.high),
                "low": float(c.low),
                "close": float(c.close),
            }
        )
    out.sort(key=lambda x: x["time"])
    return out


def in_contact_zone(line: LineDef, candle: dict[str, Any]) -> tuple[bool, float]:
    cls, lp, _dist, method, _touch = classify_candle_interval(
        line, float(candle["time"]), candle["high"], candle["low"]
    )
    if method == "inactive":
        return False, lp
    return cls in ("TOUCH", "NEAR_0_10", "NEAR_0_20"), lp


def price_reset_above_line(line: LineDef, candle: dict[str, Any]) -> bool:
    lp = line.price_at(float(candle["time"]))
    if lp is None or lp <= 0:
        return False
    return float(candle["close"]) >= float(lp) * (1.0 + TOUCH_RESET_ABOVE_LINE_PCT / 100.0)


@dataclass
class TouchEvent:
    touch_event_id: str
    line: LineDef
    start_idx: int
    end_idx: int
    first_touch_line_price: float


def build_touch_events(line: LineDef, candles: list[dict[str, Any]]) -> list[TouchEvent]:
    """Consecutive in-zone bars = one event; re-arm after close >= line + reset %."""
    events: list[TouchEvent] = []
    armed = True
    in_phase = False
    seq = 0
    current: TouchEvent | None = None

    for i, candle in enumerate(candles):
        if not line.active_for_candle(float(candle["time"])):
            if in_phase:
                in_phase = False
                current = None
            continue
        contact, lp = in_contact_zone(line, candle)
        if price_reset_above_line(line, candle):
            armed = True
        if contact and armed:
            seq += 1
            current = TouchEvent(
                touch_event_id=f"te-{line.drawing_id}-{seq:04d}",
                line=line,
                start_idx=i,
                end_idx=i,
                first_touch_line_price=float(lp),
            )
            events.append(current)
            in_phase = True
            armed = False
        elif contact and in_phase and current is not None:
            current.end_idx = i
        elif not contact and in_phase:
            in_phase = False
            current = None
    return events


def certify_lower_parallel_trends(
    lines: list[LineDef],
    upper: LineDef,
    candles: list[dict[str, Any]],
) -> tuple[dict[str, str], frozenset[str]]:
    """
    Drawing-only check: trend strictly below main upper channel on every overlapping bar.
    No entry-price inference. Metadata is not used (currently empty in drawings.json).
    """
    roles: dict[str, str] = {}
    lower_parallel: set[str] = set()
    for ln in lines:
        if ln.drawing_type != "trend":
            continue
        if ln.drawing_id == upper.drawing_id:
            roles[ln.drawing_id] = "UPPER_MAIN_CHANNEL"
            continue
        samples = 0
        always_below = True
        overlap = False
        for c in candles:
            t = float(c["time"])
            if not ln.active_for_candle(t) or not upper.active_for_candle(t):
                continue
            overlap = True
            lp = ln.price_at(t)
            up = upper.price_at(t)
            if lp is None or up is None:
                always_below = False
                break
            samples += 1
            if float(lp) >= float(up) - LOWER_CHANNEL_STRICT_EPS:
                always_below = False
                break
        if not overlap:
            roles[ln.drawing_id] = "NO_OVERLAP_WITH_MAIN_UPPER"
        elif always_below and samples > 0:
            roles[ln.drawing_id] = "LOWER_PARALLEL_CANDIDATE"
            lower_parallel.add(ln.drawing_id)
        else:
            roles[ln.drawing_id] = "NOT_ALWAYS_BELOW_MAIN_UPPER"
    for ln in lines:
        if ln.drawing_type == "hline":
            roles[ln.drawing_id] = "HLINE_DIAGNOSTIC_ONLY"
    return roles, frozenset(lower_parallel)


def trend_support_from_above(
    line: LineDef,
    candles: list[dict[str, Any]],
    contact_start_idx: int,
) -> tuple[str, str | None]:
    if contact_start_idx < 1:
        return "TREND", "NO_PRIOR_BAR_FOR_APPROACH"
    prior = candles[contact_start_idx - 1]
    t_prior = float(prior["time"])
    if not line.active_for_candle(t_prior):
        return "TREND", "INACTIVE_LINE_BEFORE_CONTACT"
    lp = line.price_at(t_prior)
    if lp is None or lp <= 0:
        return "TREND", "INVALID_TREND_PRICE"
    pc = float(prior["close"])
    band = float(lp) * APPROACH_EPSILON_PCT / 100.0
    if pc > float(lp) + band:
        return "TREND_SUPPORT_FROM_ABOVE", None
    if pc < float(lp) - band:
        return "TREND_RESISTANCE_FROM_BELOW", "TREND_RESISTANCE_FROM_BELOW"
    return "TREND_APPROACH_ON_LINE", "TREND_APPROACH_NOT_FROM_ABOVE"


def assess_long_touch_eligibility(
    line: LineDef,
    candles: list[dict[str, Any]],
    contact_start_idx: int,
    *,
    upper: LineDef | None,
    line_selection_mode: str = "OLD_ALLOWLIST",
) -> tuple[str, str | None]:
    """
    Long triggers: pilot allowlist (OLD) or dynamic diagonal support (V3I).
    Hlines are recorded but never traded. Upper main channel is exit-only.
    """
    from support_line_selection import (
        LINE_SELECTION_DYNAMIC,
        LINE_SELECTION_INDEPENDENT,
        LINE_SELECTION_OLD,
    )

    did = line.drawing_id
    if did == UPPER_MAIN_CHANNEL_LINE_ID:
        return "UPPER_MAIN_CHANNEL", "UPPER_CHANNEL_NOT_A_LONG_TRIGGER"
    if line.drawing_type == "hline":
        return "HLINE", "HLINE_NOT_A_LONG_TRIGGER"
    if line.drawing_type != "trend":
        return "UNKNOWN_DRAWING", "UNSUPPORTED_DRAWING_TYPE"

    if line_selection_mode == LINE_SELECTION_OLD:
        if did not in PILOT_ALLOWED_LOWER_TREND_IDS:
            return "TREND_NOT_IN_PILOT_ALLOWLIST", "TREND_NOT_IN_PILOT_ALLOWLIST"
    elif line_selection_mode not in (LINE_SELECTION_DYNAMIC, LINE_SELECTION_INDEPENDENT):
        return "UNKNOWN_LINE_MODE", "UNKNOWN_LINE_SELECTION_MODE"

    approach_class, approach_reject = trend_support_from_above(line, candles, contact_start_idx)
    if approach_reject is not None:
        return approach_class, approach_reject

    independent = line_selection_mode == LINE_SELECTION_INDEPENDENT
    ok_class = (
        "INDEPENDENT_SUPPORT_TREND"
        if independent
        else (
            "DYNAMIC_SUPPORT_TREND"
            if line_selection_mode == LINE_SELECTION_DYNAMIC
            else "PILOT_ALLOWED_LOWER_TREND"
        )
    )
    touch = candles[contact_start_idx]
    if not line.active_for_candle(float(touch["time"])):
        return ok_class, "INACTIVE_AT_TOUCH"
    lp_touch = line.price_at(float(touch["time"]))
    if lp_touch is None:
        return ok_class, "INACTIVE_AT_TOUCH"

    if not independent:
        if upper is None:
            return ok_class, "MISSING_MAIN_UPPER_REFERENCE"
        up_touch = upper.price_at(float(touch["time"]))
        if up_touch is None:
            return ok_class, "INACTIVE_AT_TOUCH"
        if float(lp_touch) >= float(up_touch) - LOWER_CHANNEL_STRICT_EPS:
            return "TREND_AT_OR_ABOVE_MAIN_UPPER", "TREND_NOT_BELOW_MAIN_UPPER_AT_TOUCH"

    return ok_class, None


def c1_confirm_indices(event: TouchEvent, candles: list[dict[str, Any]]) -> int | None:
    """Single C1 attempt from first touch bar of the event (next-bar open entry)."""
    i = event.start_idx
    if i + 2 >= len(candles):
        return None
    confirm = candles[i + 1]
    line = event.line
    _, lp_confirm, _, _, _ = classify_candle_interval(
        line, float(confirm["time"]), confirm["high"], confirm["low"]
    )
    if confirm["close"] <= confirm["open"]:
        return None
    if confirm["close"] <= lp_confirm:
        return None
    return i + 2


def channel_upper_at(
    lines: list[LineDef],
    t: int,
    entry: float,
    *,
    exclude_drawing_ids: frozenset[str] | None = None,
) -> tuple[float | None, str | None]:
    """
    Hauptkanal-Obergrenze: aktive Trendlinien oberhalb Entry, innerhalb einer Pilot-Spanne.
    Entfernte Hlines (> span) nicht als Kanaldeckel werten (retrospektiver Pilot).
    """
    cap = entry * (1.0 + MAX_CHANNEL_UPPER_SPAN_PCT / 100.0)
    trends: list[tuple[float, str]] = []
    hlines: list[tuple[float, str]] = []
    skip = exclude_drawing_ids or frozenset()
    for ln in lines:
        if ln.drawing_id in skip:
            continue
        if not ln.active_for_candle(float(t)):
            continue
        p = ln.price_at(float(t))
        if p is None or p <= entry or float(p) > cap:
            continue
        if ln.drawing_type == "trend":
            trends.append((float(p), ln.drawing_id))
        elif ln.drawing_type == "hline":
            hlines.append((float(p), ln.drawing_id))
    if trends:
        best_p, best_id = max(trends, key=lambda x: x[0])
        return best_p, best_id
    if hlines:
        best_p, best_id = max(hlines, key=lambda x: x[0])
        return best_p, best_id
    return None, None


@dataclass
class PoolPick:
    tp_price: float | None
    tp_mode: str
    tp_pool_id: str | None
    tp_channel_line_id: str | None
    tp_dist_pct: float | None
    gate_count: int
    gate_ok: bool
    block_reason: str | None


def qualify_upper_pools(members: list[dict], entry: float) -> list[dict]:
    out = []
    for m in members:
        if str(m.get("side", "")).lower() != "upper":
            continue
        bottom = float(m.get("lower_bound", 0))
        if bottom <= entry:
            continue
        dist_pct = (bottom - entry) / entry * 100.0
        if dist_pct < MIN_POOL_DIST_PCT - 1e-12:
            continue
        out.append(m)
    return out


def pick_tp(
    entry: float,
    qualified: list[dict],
    channel_upper: float | None,
    channel_line_id: str | None,
    *,
    independent_support: bool = False,
) -> PoolPick:
    gate_ok = len(qualified) >= 1
    if not gate_ok:
        block = (
            "NO_QUALIFIED_UPPER_D2_TP"
            if independent_support and channel_upper is None
            else "POOL_GATE_LT1_QUALIFIED_UPPER"
        )
        return PoolPick(
            None,
            "NONE",
            None,
            None,
            None,
            0,
            False,
            block,
        )
    if channel_upper is not None and channel_upper > entry:
        near = [
            m
            for m in qualified
            if float(m["lower_bound"]) <= channel_upper
            and (channel_upper - float(m["lower_bound"])) / channel_upper * 100.0 <= CHANNEL_NEAR_PCT
        ]
        if near:
            best = max(near, key=lambda m: float(m["lower_bound"]))
            tp = float(best["lower_bound"])
            dist = (tp - entry) / entry * 100.0
            return PoolPick(
                tp,
                "POOL_BOTTOM_NEAR_CHANNEL",
                str(best["pool_id"]),
                channel_line_id,
                dist,
                len(qualified),
                True,
                None,
            )
        if (channel_upper - entry) / entry * 100.0 >= MIN_POOL_DIST_PCT:
            dist = (channel_upper - entry) / entry * 100.0
            return PoolPick(
                channel_upper,
                "CHANNEL_UPPER_LINE",
                None,
                channel_line_id,
                dist,
                len(qualified),
                True,
                None,
            )
        return PoolPick(
            None,
            "UNDETERMINED",
            None,
            channel_line_id,
            None,
            len(qualified),
            True,
            "NO_TP_CHANNEL_TOO_CLOSE",
        )
    if qualified:
        best = max(qualified, key=lambda m: float(m["lower_bound"]))
        tp = float(best["lower_bound"])
        dist = (tp - entry) / entry * 100.0
        mode = "UPPER_D2_POOL_ONLY" if independent_support else "POOL_BOTTOM_NO_CLEAR_CHANNEL"
        return PoolPick(
            tp,
            mode,
            str(best["pool_id"]),
            None,
            dist,
            len(qualified),
            True,
            None,
        )
    block = "NO_QUALIFIED_UPPER_D2_TP" if independent_support else "NO_TP_UNDETERMINED"
    return PoolPick(
        None,
        "UNDETERMINED",
        None,
        None,
        None,
        len(qualified),
        False,
        block,
    )


ExitKind = Literal["TP", "CHANNEL_UPPER", "SL", "AMBIGUOUS", "OPEN_AT_WINDOW_END", "NO_TRADE"]


def pool_tp_for_exit(pick: PoolPick) -> float | None:
    if pick.tp_price is None:
        return None
    if pick.tp_pool_id and pick.tp_price is not None:
        return float(pick.tp_price)
    if pick.tp_mode in ("POOL_BOTTOM_NO_CLEAR_CHANNEL", "UPPER_D2_POOL_ONLY"):
        return float(pick.tp_price)
    return None


def channel_line_at_bar(channel: LineDef | None, candle: dict[str, Any]) -> float | None:
    if channel is None:
        return None
    t = float(candle["time"])
    if not channel.active_for_candle(t):
        return None
    p = channel.price_at(t)
    if p is None:
        return None
    return float(p)


def simulate_exit(
    candles: list[dict],
    start_idx: int,
    entry: float,
    sl: float,
    pool_tp: float | None,
    channel_line: LineDef | None,
) -> tuple[ExitKind, int | None, float | None, float | None, int | None]:
    """Pool TP (fixed at entry) plus dynamic upper channel line when assigned at entry."""
    if pool_tp is not None and pool_tp <= entry:
        pool_tp = None
    has_channel = channel_line is not None

    if pool_tp is None and not has_channel:
        return "NO_TRADE", None, None, None, None

    for j in range(start_idx, len(candles)):
        c = candles[j]
        channel_px = channel_line_at_bar(channel_line, c)
        hit_sl = c["low"] <= sl
        profit_targets: list[tuple[ExitKind, float]] = []
        if pool_tp is not None and c["high"] >= pool_tp:
            profit_targets.append(("TP", float(pool_tp)))
        if channel_px is not None and c["high"] >= channel_px:
            profit_targets.append(("CHANNEL_UPPER", channel_px))
        hit_profit = bool(profit_targets)

        if hit_sl and hit_profit:
            return "AMBIGUOUS", c["time"], sl, (sl - entry) / entry * 100.0, j
        if hit_sl:
            pnl = (sl - entry) / entry * 100.0
            return "SL", c["time"], sl, pnl, j
        if profit_targets:
            kind, exit_px = min(profit_targets, key=lambda x: x[1])
            pnl = (exit_px - entry) / entry * 100.0
            return kind, c["time"], exit_px, pnl, j
    last = candles[-1]
    pnl = (last["close"] - entry) / entry * 100.0
    return "OPEN_AT_WINDOW_END", last["time"], last["close"], pnl, len(candles) - 1


def load_pool_snapshot(decision_close: datetime) -> tuple[list[dict] | None, str | None]:
    from research_charts.historical_pool_state_v1 import (
        HistoricalPoolStateError,
        load_historical_pool_state_at_close,
    )

    try:
        st = load_historical_pool_state_at_close(
            decision_close,
            timeframe=POOL_TIMEFRAME,
            pool_detail=POOL_DETAIL,
            include_remaining=False,
        )
        return list(st.active_members), None
    except HistoricalPoolStateError as exc:
        return None, f"POOL_SSOT_{exc.code}"
    except Exception as exc:
        return None, f"POOL_SSOT_{type(exc).__name__}"


_pool_cache: dict[str, tuple[list[dict] | None, str | None]] = {}


def pools_at_confirm_open(confirm_open_ts: int) -> tuple[list[dict] | None, str | None]:
    from pool_scan.clock import bar_close

    open_dt = datetime.fromtimestamp(confirm_open_ts, tz=timezone.utc)
    dc = bar_close(open_dt, TIMEFRAME)
    key = dc.isoformat()
    if key not in _pool_cache:
        _pool_cache[key] = load_pool_snapshot(dc)
    return _pool_cache[key]


def build_pilot_context(
    *,
    line_selection_mode: str = "OLD_ALLOWLIST",
    window_start: int | None = None,
    window_end: int | None = None,
    simulation_end: int | None = None,
) -> dict[str, Any] | None:
    """Load drawings, candles, touch events, and enriched pending candidates (no execution)."""
    from support_line_selection import LINE_SELECTION_INDEPENDENT, line_causality_at_touch

    independent = line_selection_mode == LINE_SELECTION_INDEPENDENT
    ws = window_start if window_start is not None else WINDOW_START
    we = window_end if window_end is not None else WINDOW_END
    sim_end = simulation_end if simulation_end is not None else we

    if not DRAWINGS_PATH.is_file():
        print(f"MISSING_DRAWINGS {DRAWINGS_PATH}")
        return None

    payload = json.loads(DRAWINGS_PATH.read_text(encoding="utf-8"))
    lines = [ln for ln in (line_from_drawing(d) for d in payload.get("drawings") or []) if ln]
    lines_by_id = {ln.drawing_id: ln for ln in lines}
    upper_line = lines_by_id.get(UPPER_MAIN_CHANNEL_LINE_ID)
    load_end = max(we, sim_end)
    candles = load_candles(start=ws, end=load_end)
    if len(candles) < 5:
        print("INSUFFICIENT_CANDLES")
        return None

    trend_roles, lower_parallel_ids = certify_lower_parallel_trends(
        lines, upper_line, candles
    ) if upper_line else ({}, frozenset())
    role_report = [
        "# L1 trend line roles (pilot window)",
        "",
        f"- Main upper channel (exit only): `{UPPER_MAIN_CHANNEL_LINE_ID}`",
        f"- Drawing metadata roles: **none** (all `metadata` empty — no stored support/upper tags)",
        f"- Lower-parallel candidates (strictly below upper on every overlapping bar): "
        f"{len(lower_parallel_ids)}",
        f"- **Pilot long allowlist** (explicit research release): "
        + ", ".join(f"`{x}`" for x in sorted(PILOT_ALLOWED_LOWER_TREND_IDS)),
        "",
    ]
    if lower_parallel_ids:
        role_report.append(
            "- Certified lower-parallel IDs: " + ", ".join(f"`{x}`" for x in sorted(lower_parallel_ids))
        )
    missing_cert = sorted(PILOT_ALLOWED_LOWER_TREND_IDS - lower_parallel_ids)
    if missing_cert:
        role_report.append(
            "- Allowlist IDs not certified lower-parallel in window: "
            + ", ".join(f"`{x}`" for x in missing_cert)
        )
    role_report.extend(["", "## Per drawing_id", ""])
    for did in sorted(trend_roles):
        role_report.append(f"- `{did}`: {trend_roles[did]}")
    (OUT_DIR / "l1_trend_line_roles.md").write_text("\n".join(role_report) + "\n", encoding="utf-8")

    all_events: list[TouchEvent] = []
    for line in lines:
        all_events.extend(build_touch_events(line, candles))

    touch_stats = {
        "touch_events_total": len(all_events),
        "long_triggers_allowed": 0,
        "hline_diagnostic_only": 0,
        "excluded_upper_or_resistance": 0,
        "unclassified_or_ambiguous_trend": 0,
        "pilot_allowlist_ids": sorted(PILOT_ALLOWED_LOWER_TREND_IDS),
    }
    for event in all_events:
        structure_class, struct_reject = assess_long_touch_eligibility(
            event.line,
            candles,
            event.start_idx,
            upper=upper_line,
            line_selection_mode=line_selection_mode,
        )
        if struct_reject is None:
            touch_stats["long_triggers_allowed"] += 1
        elif event.line.drawing_type == "hline" or struct_reject == "HLINE_NOT_A_LONG_TRIGGER":
            touch_stats["hline_diagnostic_only"] += 1
        elif structure_class in (
            "UPPER_MAIN_CHANNEL",
            "TREND_RESISTANCE_FROM_BELOW",
        ) or struct_reject in (
            "UPPER_CHANNEL_NOT_A_LONG_TRIGGER",
            "TREND_RESISTANCE_FROM_BELOW",
        ):
            touch_stats["excluded_upper_or_resistance"] += 1
        else:
            touch_stats["unclassified_or_ambiguous_trend"] += 1

    pending: list[dict[str, Any]] = []
    for event in all_events:
        line = event.line
        i0 = event.start_idx
        touch_utc = fmt_utc(int(candles[i0]["time"]))
        contact_end_utc = fmt_utc(int(candles[event.end_idx]["time"]))
        structure_class, struct_reject = assess_long_touch_eligibility(
            line,
            candles,
            i0,
            upper=upper_line,
            line_selection_mode=line_selection_mode,
        )
        touch_ts = float(candles[i0]["time"])
        causality = line_causality_at_touch(line, touch_ts)

        row_base = {
            "pilot_mode": PILOT_LABEL,
            "line_selection_mode": line_selection_mode,
            "drawing_causality": causality.label,
            "drawing_geometry_active": "yes" if causality.geometry_active else "no",
            "drawing_anchor_status": causality.anchor_status,
            "symbol": SYMBOL,
            "touch_event_id": event.touch_event_id,
            "touch_utc": touch_utc,
            "contact_phase_end_utc": contact_end_utc,
            "touch_drawing_id": line.drawing_id,
            "touch_line_price": f"{event.first_touch_line_price:.10f}",
            "structure_class": structure_class,
            "long_trigger_allowed": "yes" if struct_reject is None else "no",
            "confirm_variant": CONFIRM_VARIANT,
            "confirm_utc": "",
            "entry_utc": "",
            "entry_idx": "",
            "entry_price": "",
            "sl_price": "",
            "sl_pct": f"{SL_PCT:.2f}",
            "tp_mode": "",
            "tp_price": "",
            "tp_pool_id": "",
            "tp_channel_line_id": "",
            "tp_target_dist_pct": "",
            "pool_gate_qualified_count": "",
            "pool_ssot": f"{POOL_TIMEFRAME}_PD{POOL_DETAIL}",
            "exit_kind": "NO_TRADE",
            "exit_utc": "",
            "exit_price": "",
            "gross_pnl_pct": "",
            "dynamic_channel_exit": "",
            "reject_reason": "",
            "channel_price_at_entry": "",
            "channel_room_pct": "",
            "effective_tp_at_entry": "",
            "potential_profit_pct": "",
        }

        if struct_reject is not None:
            row = dict(row_base)
            row["reject_reason"] = struct_reject
            pending.append(row)
            continue

        entry_idx = c1_confirm_indices(event, candles)
        if entry_idx is None:
            row = dict(row_base)
            row["reject_reason"] = "NO_C1_CONFIRM_AFTER_FIRST_TOUCH"
            pending.append(row)
            continue

        confirm = candles[entry_idx - 1]
        row_base["confirm_utc"] = fmt_utc(int(confirm["time"]))
        entry_ts = candles[entry_idx]["time"]
        entry = float(candles[entry_idx]["open"])
        sl = entry * (1.0 - SL_PCT / 100.0)
        row_base["entry_utc"] = fmt_utc(entry_ts)
        row_base["entry_idx"] = str(entry_idx)
        row_base["entry_price"] = f"{entry:.10f}"
        row_base["sl_price"] = f"{sl:.10f}"

        from pool_scan.clock import bar_close

        confirm_open_dt = datetime.fromtimestamp(int(confirm["time"]), tz=timezone.utc)
        pool_decision_close = bar_close(confirm_open_dt, TIMEFRAME)
        members, pool_err = pools_at_confirm_open(int(confirm["time"]))
        channel_upper, channel_id = channel_upper_at(
            lines,
            entry_ts,
            entry,
            exclude_drawing_ids=frozenset({line.drawing_id}),
        )

        if members is None:
            pick = PoolPick(
                None,
                "NONE",
                None,
                channel_id,
                None,
                0,
                False,
                pool_err or "POOL_SSOT_FAIL",
            )
            qualified: list[dict] = []
        else:
            qualified = qualify_upper_pools(members, entry)
            pick = pick_tp(
                entry,
                qualified,
                channel_upper,
                channel_id,
                independent_support=independent and channel_upper is None,
            )

        row = dict(row_base)
        row["tp_mode"] = pick.tp_mode
        row["tp_price"] = f"{pick.tp_price:.10f}" if pick.tp_price is not None else ""
        row["tp_pool_id"] = pick.tp_pool_id or ""
        row["tp_channel_line_id"] = pick.tp_channel_line_id or ""
        row["tp_target_dist_pct"] = f"{pick.tp_dist_pct:.4f}" if pick.tp_dist_pct is not None else ""
        row["pool_gate_qualified_count"] = str(pick.gate_count)
        row["reject_reason"] = pick.block_reason or ""
        row["_entry_idx"] = entry_idx
        row["_sl"] = sl
        row["_pool_tp"] = pool_tp_for_exit(pick)
        ch_id = pick.tp_channel_line_id
        row["_channel_line"] = lines_by_id.get(ch_id) if ch_id else None
        row["dynamic_channel_exit"] = "yes" if row["_channel_line"] is not None else "no"
        row["_pick_ok"] = (
            pick.gate_ok
            and not pick.block_reason
            and (row["_pool_tp"] is not None or row["_channel_line"] is not None)
        )
        row["_touch_start_idx"] = i0
        row["_entry_ts"] = entry_ts
        row["_pool_members"] = members
        row["_pool_decision_close"] = fmt_utc(int(pool_decision_close.timestamp()))
        row["_independent_support_mode"] = independent
        row["_channel_deckel_at_entry"] = channel_upper
        row["_channel_deckel_line_id"] = channel_id
        pending.append(row)

    import pilot_engine as pe

    pe.enrich_pending_metrics(pending, upper_line, sys.modules[__name__], independent=independent)
    pe.enrich_pool_landscape(pending)
    return {
        "pending": pending,
        "candles": candles,
        "upper_line": upper_line,
        "lines_by_id": lines_by_id,
        "lines": lines,
        "touch_stats": touch_stats,
        "all_events": all_events,
        "line_selection_mode": line_selection_mode,
        "window_start_ts": ws,
        "window_end_ts": we,
        "simulation_end_ts": sim_end,
        "events_per_line": {
            did: [e for e in all_events if e.line.drawing_id == did]
            for did in PILOT_ALLOWED_LOWER_TREND_IDS
        },
    }


def main() -> int:
    _bootstrap_import_paths()
    ctx = build_pilot_context()
    if ctx is None:
        return 2

    pending = ctx["pending"]
    candles = ctx["candles"]
    upper_line = ctx["upper_line"]
    touch_stats = ctx["touch_stats"]
    all_events = ctx["all_events"]
    events_per_line = ctx["events_per_line"]

    import pilot_engine as pe
    from config import VARIANT_A

    trades = pe.execute_variant(
        VARIANT_A, pending, candles, upper_line=upper_line, l1_module=sys.modules[__name__]
    )

    events_per_line = {
        did: [e for e in all_events if e.line.drawing_id == did] for did in PILOT_ALLOWED_LOWER_TREND_IDS
    }
    per_line_lines = [
        "# L1 — per allowlist trend line",
        "",
        "Retrospective research pilot (not a causal backtest).",
        "",
        "| drawing_id | touch_events | long_trigger_ok | entry_attempts | executed_trades | sum_pnl_% |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    ref_lo = datetime(2026, 8, 26, 15, 0, tzinfo=timezone.utc)
    ref_hi = datetime(2026, 8, 26, 16, 50, tzinfo=timezone.utc)
    ref_section: list[str] = [
        "",
        "## 2026-08-26 15:00–16:50 UTC (reference window)",
        "",
    ]
    for did in sorted(PILOT_ALLOWED_LOWER_TREND_IDS):
        rows = [r for r in trades if r["touch_drawing_id"] == did]
        n_touch = len(events_per_line.get(did, []))
        n_ok = sum(1 for r in rows if r.get("long_trigger_allowed") == "yes")
        n_entry = sum(1 for r in rows if r.get("entry_utc"))
        executed = [r for r in rows if r.get("exit_kind") not in ("NO_TRADE", "")]
        pnl_sum = sum(float(r["gross_pnl_pct"]) for r in executed if r.get("gross_pnl_pct"))
        per_line_lines.append(
            f"| `{did}` | {n_touch} | {n_ok} | {n_entry} | {len(executed)} | {pnl_sum:.4f} |"
        )
        ref_rows = [
            r
            for r in rows
            if ref_lo
            <= datetime.strptime(r["touch_utc"], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
            <= ref_hi
        ]
        if ref_rows:
            ref_section.append(f"### `{did}`")
            for r in ref_rows:
                ref_section.append(
                    f"- `{r['touch_event_id']}` touch {r['touch_utc']} | long_ok={r['long_trigger_allowed']} | "
                    f"reject={r['reject_reason'] or '—'} | entry={r['entry_utc'] or '—'} | exit={r['exit_kind']}"
                )
        else:
            ref_section.append(f"- `{did}`: no touch events in reference window")
    block_rows = [
        r
        for r in trades
        if r.get("long_trigger_allowed") == "no" and r["touch_drawing_id"] in PILOT_ALLOWED_LOWER_TREND_IDS
    ]
    if block_rows:
        from collections import Counter

        per_line_lines.extend(["", "## Blocked on allowlist lines (reject reason counts)", ""])
        for did in sorted(PILOT_ALLOWED_LOWER_TREND_IDS):
            sub = [r for r in block_rows if r["touch_drawing_id"] == did]
            if not sub:
                continue
            per_line_lines.append(f"### `{did}`")
            for reason, cnt in sorted(Counter(r["reject_reason"] for r in sub).items()):
                per_line_lines.append(f"- {reason}: {cnt}")
    per_line_lines.extend(ref_section)
    (OUT_DIR / "l1_pilot_per_line.md").write_text("\n".join(per_line_lines) + "\n", encoding="utf-8")

    csv_path = OUT_DIR / "l1_pilot_trades.csv"
    fields = sorted({k for t in trades for k in t}) if trades else []
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        if trades:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(trades)

    closed = [t for t in trades if t["exit_kind"] in ("TP", "CHANNEL_UPPER", "SL", "AMBIGUOUS")]
    md_lines = [
        "# Phase L1 pilot — Manual Channel + ACTIVE D2 Pool LONG",
        "",
        f"- **Pilot mode**: {PILOT_LABEL} (not a fully causal backtest)",
        f"- **Window**: {fmt_utc(WINDOW_START)} .. {fmt_utc(WINDOW_END)} (exclusive end)",
        f"- **Instrument**: {SYMBOL} {TIMEFRAME}",
        f"- **SL**: fixed {SL_PCT:.2f} % below entry (gross, no fees/slippage)",
        f"- **Confirmation**: {CONFIRM_VARIANT} only",
        f"- **Pool SSOT**: {POOL_TIMEFRAME} PD{POOL_DETAIL} ACTIVE at confirm-bar close",
        f"- **Qualified pool min distance**: {MIN_POOL_DIST_PCT:.2f} % entry → pool bottom",
        f"- **Touch zone tolerance**: ≤ {TOUCH_ZONE_MAX_DIST_PCT:.2f} % (TOUCH / NEAR_0_10 / NEAR_0_20)",
        f"- **Touch re-arm reset**: close ≥ line + {TOUCH_RESET_ABOVE_LINE_PCT:.2f} %",
        f"- **Max one trade per touch_event_id** and **max one global open long**",
        f"- **Exits**: fixed pool TP at entry + **dynamic** upper channel line (`tp_channel_line_id`) when assigned",
        f"- **Structure gate**: pilot allowlist lower trends ({len(PILOT_ALLOWED_LOWER_TREND_IDS)} IDs); hlines diagnostic only",
        f"- **Touch events total**: {touch_stats['touch_events_total']} | long allowed: {touch_stats['long_triggers_allowed']} | "
        f"hline diagnostic: {touch_stats['hline_diagnostic_only']} | upper/resistance: {touch_stats['excluded_upper_or_resistance']} | "
        f"other trend excluded: {touch_stats['unclassified_or_ambiguous_trend']}",
        f"- **Allowlist**: {', '.join(touch_stats['pilot_allowlist_ids'])}",
        f"- **Candidate rows**: {len(trades)}",
        f"- **Simulated exits (TP/CHANNEL_UPPER/SL/AMBIGUOUS)**: {len(closed)}",
        "",
        "## Notes",
        "",
        "- Upper channel second anchor (e.g. Sep 2026): August results remain **retrospective research**, not causal.",
        "- Drawings read from current `drawings.json`; causal availability of anchors not proven.",
        "- Catalog exposes 5m PD1 `micro` upper bands; exact unique-origin count is not in the ACTIVE row schema used here.",
        f"- Upper channel proxy: highest active **trend** within {MAX_CHANNEL_UPPER_SPAN_PCT:.1f} % above entry (pilot heuristic).",
        "- No changes to production strategies.",
        "",
        "## Trade list",
        "",
        "| touch_event_id | Touch UTC | Entry UTC | Entry | SL | TP mode | TP | Exit UTC | Gross PnL % | Reject |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for t in trades:
        md_lines.append(
            f"| `{t['touch_event_id']}` | {t['touch_utc']} | {t['entry_utc']} | {t['entry_price']} | {t['sl_price']} "
            f"| {t['tp_mode']} | {t['tp_price']} | {t['exit_utc']} | {t['gross_pnl_pct']} | {t['reject_reason']} |"
        )

    (OUT_DIR / "l1_pilot_report.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    prev_path = OUT_DIR / "l1_pilot_trades_prev.csv"
    compare_lines = ["# L1 pilot compare (channel dynamic exit)", ""]
    if prev_path.is_file():
        from collections import Counter

        def _summ(rows: list[dict]) -> dict[str, Any]:
            executed = [r for r in rows if r.get("exit_kind") not in ("NO_TRADE", "")]
            return {
                "rows": len(rows),
                "exit": dict(Counter(r.get("exit_kind", "") for r in rows)),
                "reject": dict(Counter(r.get("reject_reason", "") for r in rows if r.get("reject_reason"))),
                "executed": len(executed),
                "pnl_sum": sum(float(r["gross_pnl_pct"]) for r in executed if r.get("gross_pnl_pct")),
            }

        before = list(csv.DictReader(prev_path.open(encoding="utf-8")))
        after = trades
        sb, sa = _summ(before), _summ(after)
        compare_lines.extend(
            [
                "| Metric | Before | After |",
                "| --- | --- | --- |",
                f"| Rows | {sb['rows']} | {sa['rows']} |",
                f"| Executed trades | {sb['executed']} | {sa['executed']} |",
                f"| Sum gross PnL % (executed) | {sb['pnl_sum']:.4f} | {sa['pnl_sum']:.4f} |",
                "",
                "## Touch structure (after run only)",
                "",
                f"- Total touch events: {touch_stats['touch_events_total']}",
                f"- Long triggers allowed: {touch_stats['long_triggers_allowed']}",
                f"- Hline touches (diagnostic only): {touch_stats['hline_diagnostic_only']}",
                f"- Excluded upper / resistance: {touch_stats['excluded_upper_or_resistance']}",
                f"- Unclassified / ambiguous trend: {touch_stats['unclassified_or_ambiguous_trend']}",
                f"- Pilot allowlist IDs: {touch_stats['pilot_allowlist_ids']}",
                "",
                "**Exit kinds before**: " + ", ".join(f"{k}={v}" for k, v in sorted(sb["exit"].items())),
                "",
                "**Exit kinds after**: " + ", ".join(f"{k}={v}" for k, v in sorted(sa["exit"].items())),
                "",
                "**GLOBAL_MAX_ONE_OPEN_LONG before**: "
                + str(sb["reject"].get("GLOBAL_MAX_ONE_OPEN_LONG", 0)),
                f"**after**: " + str(sa["reject"].get("GLOBAL_MAX_ONE_OPEN_LONG", 0)),
                "",
            ]
        )
        struct_rejects = Counter(
            r.get("reject_reason", "")
            for r in trades
            if r.get("long_trigger_allowed") == "no" and r.get("reject_reason")
        )
        if struct_rejects:
            compare_lines.append("**Structural reject counts (after)**:")
            for k, v in sorted(struct_rejects.items()):
                compare_lines.append(f"- {k}: {v}")
            compare_lines.append("")
        newly_exec = [
            r["touch_event_id"]
            for r in after
            if r.get("exit_kind") not in ("NO_TRADE", "")
            and not any(
                b.get("touch_event_id") == r.get("touch_event_id")
                and b.get("exit_kind") not in ("NO_TRADE", "")
                for b in before
            )
        ]
        freed = [
            r["touch_event_id"]
            for r in after
            if r.get("reject_reason") == ""
            and r.get("exit_kind") not in ("NO_TRADE", "")
            and any(
                b.get("touch_event_id") == r.get("touch_event_id")
                and b.get("reject_reason") == "GLOBAL_MAX_ONE_OPEN_LONG"
                for b in before
            )
        ]
        compare_lines.append("**Newly executed (not traded before)**: " + (", ".join(newly_exec) or "—"))
        compare_lines.append("**Previously blocked by GLOBAL_MAX_ONE_OPEN_LONG, now executed**: " + (", ".join(freed) or "—"))
    else:
        compare_lines.append("_No l1_pilot_trades_prev.csv — copy current CSV before re-run for diff._")
    (OUT_DIR / "l1_pilot_compare.md").write_text("\n".join(compare_lines) + "\n", encoding="utf-8")

    print(f"Wrote {csv_path} ({len(trades)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
