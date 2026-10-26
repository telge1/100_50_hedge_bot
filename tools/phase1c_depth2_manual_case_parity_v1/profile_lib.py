"""Phase 1C landscape profile helpers (EMA, neighbors, S1 classification)."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEPTH1_PROBE = Path(__file__).resolve().parent / "depth1_probe.js"
EMA_PERIODS = (9, 20, 59, 200)


def parse_utc(s: str) -> datetime:
    s = s.strip().replace(" ", "T")
    if not s.endswith("Z"):
        s += "Z"
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ema_series(closes: list[float], period: int) -> list[float | None]:
    if not closes or period <= 0:
        return []
    out: list[float | None] = [None] * len(closes)
    k = 2.0 / (period + 1)
    if len(closes) < period:
        return out
    seed = sum(closes[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(closes)):
        prev = closes[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def slope_last(values: list[float | None], bars: int = 3) -> float | None:
    idx = [i for i, v in enumerate(values) if v is not None]
    if len(idx) < bars:
        return None
    tail = idx[-bars:]
    v0 = values[tail[0]]
    v1 = values[tail[-1]]
    if v0 is None or v1 is None or v0 == 0:
        return None
    return (v1 - v0) / v0 * 100.0


def closed_candles_before(
    prices: list[dict[str, Any]], as_of: str, slot_minutes: int = 5
) -> list[dict[str, Any]]:
    """Candles with close time <= as_of (5m grid)."""
    as_ms = int(parse_utc(as_of).timestamp() * 1000)
    slot_ms = slot_minutes * 60 * 1000
    rows = []
    for row in prices:
        t_ms = int(parse_utc(row["t"]).timestamp() * 1000)
        if t_ms <= as_ms:
            rows.append(row)
    rows.sort(key=lambda r: r["t"])
    return rows


def ema_context(
    prices: list[dict[str, Any]], as_of: str, slot_minutes: int = 5
) -> dict[str, Any]:
    candles = closed_candles_before(prices, as_of, slot_minutes)
    if len(candles) < max(EMA_PERIODS) + 1:
        return {
            "status": "NOT_VERIFIED",
            "reason": "insufficient_closed_5m_bars_for_ema200",
            "closed_bar_count": len(candles),
            "as_of_utc": as_of,
        }
    closes = [float(c["c"]) for c in candles]
    last = candles[-1]
    ref_close = float(last["c"])
    emas: dict[str, Any] = {}
    slopes: dict[str, Any] = {}
    for p in EMA_PERIODS:
        series = ema_series(closes, p)
        val = series[-1]
        emas[f"ema{p}"] = val
        slopes[f"ema{p}_slope_3bar_pct"] = slope_last(series, 3)
    return {
        "status": "VERIFIED",
        "as_of_utc": as_of,
        "last_closed_candle_utc": last["t"],
        "reference_close": ref_close,
        "emas": emas,
        "slopes_pct": slopes,
        "price_vs_emas": {
            f"above_ema{p}": ref_close > emas[f"ema{p}"]
            if emas[f"ema{p}"] is not None
            else None
            for p in EMA_PERIODS
        },
    }


def interval_distance(iv: dict[str, Any], price: float) -> dict[str, float]:
    lo = float(iv["bottom_price"])
    hi = float(iv["top_price"])
    if lo <= price <= hi:
        return {"inside": 0.0, "to_bottom": 0.0, "to_top": 0.0}
    if price < lo:
        return {
            "inside": price - lo,
            "to_bottom": price - lo,
            "to_top": price - hi,
        }
    return {
        "inside": price - hi,
        "to_bottom": price - lo,
        "to_top": price - hi,
    }


def nearest_upper_depth2(
    intervals: list[dict[str, Any]], price: float
) -> dict[str, Any]:
    uppers = [iv for iv in intervals if iv["side"] == "upper"]
    below = [iv for iv in uppers if float(iv["top_price"]) < price]
    above = [iv for iv in uppers if float(iv["bottom_price"]) > price]
    containing = [
        iv
        for iv in uppers
        if float(iv["bottom_price"]) <= price <= float(iv["top_price"])
    ]
    below.sort(key=lambda x: float(x["top_price"]), reverse=True)
    above.sort(key=lambda x: float(x["bottom_price"]))
    return {
        "contains_price": containing,
        "nearest_below": below[0] if below else None,
        "nearest_above": above[0] if above else None,
        "distance_nearest_below": (
            interval_distance(below[0], price) if below else None
        ),
        "distance_nearest_above": (
            interval_distance(above[0], price) if above else None
        ),
    }


def run_depth1_probe(
    pools: list[dict], as_of: str, price: float | None, depth2_upper: list[dict]
) -> dict[str, Any]:
    payload = {
        "pools": pools,
        "as_of_utc": as_of,
        "slot_minutes": 5,
        "price": price,
        "depth2_upper": depth2_upper,
    }
    proc = subprocess.run(
        ["node", str(DEPTH1_PROBE)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(DEPTH1_PROBE.parent),
    )
    if proc.returncode != 0:
        return {"status": "NOT_VERIFIED", "error": proc.stderr or proc.stdout}
    return json.loads(proc.stdout)


def classify_s1_upper_anchor(
    entry_hint: float,
    nearest: dict[str, Any],
    depth1_probe: dict[str, Any],
) -> dict[str, Any]:
    d2_contains = nearest.get("contains_price") or []
    d1_contains = depth1_probe.get("contains_upper_depth1") or []
    if d2_contains:
        hypothesis = "A_near_valid_depth2_upper"
        note = "Entry hint lies inside at least one visible Depth-2 upper band."
    elif d1_contains and not d2_contains:
        hypothesis = "B_depth1_only_at_entry_hint"
        note = (
            "No Depth-2 upper contains hint; Depth-1 upper zone(s) do — "
            "manual micro may map to min-depth-1 geometry only."
        )
    elif not d1_contains and not d2_contains:
        nearest_below = nearest.get("nearest_below")
        nearest_above = nearest.get("nearest_above")
        if nearest_below or nearest_above:
            hypothesis = "C_ambiguous_anchor_not_inside_depth2"
            note = (
                "Hint not inside Depth-2 or Depth-1 upper; nearest bands exist — "
                "check timestamp/entry rule vs pool bounds."
            )
        else:
            hypothesis = "NOT_VERIFIED"
            note = "No upper Depth-2 bands found near hint."
    else:
        hypothesis = "NOT_VERIFIED"
        note = "Unexpected classification state."

    return {
        "entry_hint": entry_hint,
        "hypothesis": hypothesis,
        "note": note,
        "depth2_contains_count": len(d2_contains),
        "depth1_contains_count": len(d1_contains),
        "not_compatible_with_depth2": hypothesis == "B_depth1_only_at_entry_hint",
    }


def enrich_intervals_with_distance(
    intervals: list[dict[str, Any]], ref_price: float
) -> list[dict[str, Any]]:
    out = []
    for iv in intervals:
        lo = float(iv["bottom_price"])
        hi = float(iv["top_price"])
        out.append(
            {
                **iv,
                "distance_to_ref_bottom": ref_price - hi,
                "distance_to_ref_top": lo - ref_price,
                "ref_inside": lo <= ref_price <= hi,
            }
        )
    out.sort(key=lambda x: float(x["bottom_price"]))
    return out


def next_5m_open(as_of: str) -> str:
    t = parse_utc(as_of)
    slot = 5
    minute = (t.minute // slot) * slot
    aligned = t.replace(minute=minute, second=0, microsecond=0)
    if aligned < t or (t.minute % slot == 0 and t.second == 0):
        nxt = aligned + timedelta(minutes=slot)
    else:
        nxt = aligned + timedelta(minutes=slot)
    return iso(nxt)


def origin_age_map(
    persistence_rows: list[dict[str, Any]], origin_ids: list[str]
) -> dict[str, dict[str, Any]]:
    by_origin = {r["origin"]: r for r in persistence_rows if r.get("origin")}
    out = {}
    for oid in origin_ids:
        row = by_origin.get(oid)
        if row:
            out[oid] = {
                "active_at_as_of": row.get("active_at_as_of"),
                "first_segment_start_utc": row.get("first_segment_start_utc"),
                "age_hours_at_as_of": row.get("age_hours_at_as_of"),
                "persistence_status": row.get("persistence_status"),
            }
        else:
            out[oid] = {"persistence_status": "NOT_VERIFIED"}
    return out
