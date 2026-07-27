"""Phase 6: causal forward outcomes and statistical evaluation of pattern candidates.

Research-only. No trading signals, no DB writes, no lookahead.
"""

from __future__ import annotations

import bisect
import csv
import hashlib
import logging
import math
import random
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import orjson

logger = logging.getLogger(__name__)

MAPPING_VERSION = "phase6_direction_v1"
PHASE6_VERSION = "phase6_v1"

DIRECTION_LONG = "LONG"
DIRECTION_SHORT = "SHORT"
DIRECTION_NEUTRAL = "NEUTRAL"
DIRECTION_UNKNOWN = "UNKNOWN"

BASELINE_HL_VARIANTS = frozenset({"P0", "P1", "P2"})
ARMED_HL_VARIANTS = frozenset({f"P{i}" for i in range(3, 12)})


class PatternOutcomeError(ValueError):
    pass


@dataclass
class OutcomeParams:
    horizons_seconds: tuple[int, ...] = (60, 300, 900, 1800, 3600, 7200)
    targets_bps: tuple[float, ...] = (10.0, 25.0, 50.0, 100.0)
    stops_bps: tuple[float, ...] = (25.0, 50.0, 100.0)
    price_source: str = "mid"
    min_samples: int = 30
    bootstrap_iterations: int = 1000
    random_seed: int = 42


def parse_int_list(raw: str | None, *, default: Sequence[int]) -> tuple[int, ...]:
    if raw is None or str(raw).strip() == "":
        return tuple(int(x) for x in default)
    vals = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        v = int(part)
        if v <= 0:
            raise PatternOutcomeError(f"horizon/list values must be > 0, got {v}")
        vals.append(v)
    if not vals:
        raise PatternOutcomeError("empty numeric list")
    return tuple(vals)


def parse_float_list(raw: str | None, *, default: Sequence[float]) -> tuple[float, ...]:
    if raw is None or str(raw).strip() == "":
        return tuple(float(x) for x in default)
    vals = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        v = float(part)
        if not math.isfinite(v) or v <= 0:
            raise PatternOutcomeError(f"bps values must be finite and > 0, got {v}")
        vals.append(v)
    if not vals:
        raise PatternOutcomeError("empty bps list")
    return tuple(vals)


def validate_outcome_params(params: OutcomeParams) -> OutcomeParams:
    if params.price_source not in {"mid", "close", "high_low"}:
        raise PatternOutcomeError(
            f"unsupported outcome-price-source {params.price_source!r}; "
            "supported: mid,close,high_low"
        )
    if params.min_samples < 1:
        raise PatternOutcomeError("outcome-min-samples must be >= 1")
    if params.bootstrap_iterations < 0:
        raise PatternOutcomeError("outcome-bootstrap-iterations must be >= 0")
    if not params.horizons_seconds:
        raise PatternOutcomeError("outcome-horizons-seconds must be non-empty")
    if any(h <= 0 for h in params.horizons_seconds):
        raise PatternOutcomeError("horizons must be > 0")
    return params


def _parse_dt(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def _safe_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes", "y"}


def write_csv_headered(
    path: Path, rows: Sequence[Mapping[str, Any]], headers: Sequence[str]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(headers), extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({h: row.get(h) for h in headers})


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------------------
# Direction mapping
# ---------------------------------------------------------------------------

def _dir(ptype: str, family: str, direction: str, reason: str) -> dict[str, str]:
    return {
        "pattern_type": ptype,
        "pattern_family": family,
        "expected_direction": direction,
        "mapping_reason": reason,
        "mapping_version": MAPPING_VERSION,
    }


def build_direction_mapping() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    # Explicit LONG
    for p in (
        "BID_WALL_WITH_SELL_PRESSURE",
        "BID_WALL_GROWING_WITH_SELL_PRESSURE",
        "BID_WALL_PERSISTENT_PRICE_NOT_FALLING",
        "BID_WALL_TESTED_WITH_SELL_PRESSURE",
        "BID_ABSORPTION_CANDIDATE",
        "BID_WALL_PERSISTENT",
        "BID_WALL_GREW",
        "BID_WALL_TESTED",
        "BID_WALL_MOVED_TOWARD_PRICE",
    ):
        rows.append(_dir(p, "WALL_FLOW" if "PRESSURE" in p or "GROWING" in p or "PERSISTENT_PRICE" in p else (
            "ABSORPTION_CANDIDATE" if "ABSORPTION" in p else "WALL_LIFECYCLE"
        ), DIRECTION_LONG, "bid-side support / absorption candidate"))
    # Explicit SHORT
    for p in (
        "ASK_WALL_WITH_BUY_PRESSURE",
        "ASK_WALL_GROWING_WITH_BUY_PRESSURE",
        "ASK_WALL_PERSISTENT_PRICE_NOT_RISING",
        "ASK_WALL_TESTED_WITH_BUY_PRESSURE",
        "ASK_ABSORPTION_CANDIDATE",
        "ASK_WALL_PERSISTENT",
        "ASK_WALL_GREW",
        "ASK_WALL_TESTED",
        "ASK_WALL_MOVED_TOWARD_PRICE",
    ):
        fam = (
            "ABSORPTION_CANDIDATE"
            if "ABSORPTION" in p
            else ("WALL_FLOW" if ("PRESSURE" in p or "GROWING" in p or "PERSISTENT_PRICE" in p) else "WALL_LIFECYCLE")
        )
        rows.append(_dir(p, fam, DIRECTION_SHORT, "ask-side resistance / absorption candidate"))
    # Failures: break of bid → SHORT bias; break of ask → LONG bias
    rows.append(_dir("BID_WALL_FAILURE_CANDIDATE", "WALL_FAILURE_CANDIDATE", DIRECTION_SHORT, "confirmed bid wall failure"))
    rows.append(_dir("ASK_WALL_FAILURE_CANDIDATE", "WALL_FAILURE_CANDIDATE", DIRECTION_LONG, "confirmed ask wall failure"))
    rows.append(_dir("BID_WALL_CONFIRMED_BREAK", "WALL_LIFECYCLE", DIRECTION_SHORT, "bid wall confirmed break"))
    rows.append(_dir("ASK_WALL_CONFIRMED_BREAK", "WALL_LIFECYCLE", DIRECTION_LONG, "ask wall confirmed break"))
    rows.append(_dir("BID_WALL_BREAK_WITH_SELL_PRESSURE", "WALL_FLOW", DIRECTION_SHORT, "bid break with sell pressure"))
    rows.append(_dir("ASK_WALL_BREAK_WITH_BUY_PRESSURE", "WALL_FLOW", DIRECTION_LONG, "ask break with buy pressure"))
    rows.append(_dir("BID_WALL_TRADED_THROUGH", "WALL_LIFECYCLE", DIRECTION_SHORT, "bid traded through"))
    rows.append(_dir("ASK_WALL_TRADED_THROUGH", "WALL_LIFECYCLE", DIRECTION_LONG, "ask traded through"))
    # Pulling: removal may precede continuation — UNKNOWN (no invent)
    rows.append(_dir("BID_WALL_PULLING_CANDIDATE", "WALL_PULLING_CANDIDATE", DIRECTION_UNKNOWN, "cancel vs execution unknown"))
    rows.append(_dir("ASK_WALL_PULLING_CANDIDATE", "WALL_PULLING_CANDIDATE", DIRECTION_UNKNOWN, "cancel vs execution unknown"))
    # Context-only price/OI / delta: NEUTRAL (not automatic trade direction)
    for p in (
        "PRICE_DOWN_DELTA_POSITIVE",
        "PRICE_UP_DELTA_NEGATIVE",
        "PRICE_FLAT_DELTA_POSITIVE",
        "PRICE_FLAT_DELTA_NEGATIVE",
        "PRICE_UP_OI_UP",
        "PRICE_UP_OI_DOWN",
        "PRICE_DOWN_OI_UP",
        "PRICE_DOWN_OI_DOWN",
        "PRICE_FLAT_OI_UP",
        "PRICE_FLAT_OI_DOWN",
    ):
        fam = "PRICE_DELTA_DIVERGENCE" if "DELTA" in p else "PRICE_OI"
        rows.append(_dir(p, fam, DIRECTION_NEUTRAL, "context constellation only; no automatic direction"))
    # Imbalance / liquidation / appear / shrink / move away / dominance: NEUTRAL or UNKNOWN
    for p, fam, d, reason in (
        ("BID_WALL_DOMINANCE", "WALL_IMBALANCE", DIRECTION_NEUTRAL, "liquidity imbalance only"),
        ("ASK_WALL_DOMINANCE", "WALL_IMBALANCE", DIRECTION_NEUTRAL, "liquidity imbalance only"),
        ("BALANCED_WALL_LIQUIDITY", "WALL_IMBALANCE", DIRECTION_NEUTRAL, "balanced liquidity"),
        ("BUY_LIQUIDATION_CLUSTER", "LIQUIDATION", DIRECTION_NEUTRAL, "liquidation context only"),
        ("SELL_LIQUIDATION_CLUSTER", "LIQUIDATION", DIRECTION_NEUTRAL, "liquidation context only"),
        ("WALL_TEST_WITH_LIQUIDATIONS", "LIQUIDATION", DIRECTION_NEUTRAL, "liquidation context only"),
        ("WALL_BREAK_WITH_LIQUIDATIONS", "LIQUIDATION", DIRECTION_NEUTRAL, "liquidation context only"),
        ("BID_WALL_APPEARED", "WALL_LIFECYCLE", DIRECTION_NEUTRAL, "lifecycle appear only"),
        ("ASK_WALL_APPEARED", "WALL_LIFECYCLE", DIRECTION_NEUTRAL, "lifecycle appear only"),
        ("BID_WALL_SHRANK", "WALL_LIFECYCLE", DIRECTION_UNKNOWN, "shrink without proven direction"),
        ("ASK_WALL_SHRANK", "WALL_LIFECYCLE", DIRECTION_UNKNOWN, "shrink without proven direction"),
        ("BID_WALL_MOVED_AWAY_FROM_PRICE", "WALL_LIFECYCLE", DIRECTION_NEUTRAL, "lifecycle move only"),
        ("ASK_WALL_MOVED_AWAY_FROM_PRICE", "WALL_LIFECYCLE", DIRECTION_NEUTRAL, "lifecycle move only"),
        ("BID_WALL_DISAPPEARED_UNTESTED", "WALL_LIFECYCLE", DIRECTION_UNKNOWN, "disappear untested"),
        ("ASK_WALL_DISAPPEARED_UNTESTED", "WALL_LIFECYCLE", DIRECTION_UNKNOWN, "disappear untested"),
        ("WALL_REPLACEMENT_LOWER", "WALL_REPLACEMENT", DIRECTION_UNKNOWN, "replacement descriptive only"),
        ("WALL_REPLACEMENT_HIGHER", "WALL_REPLACEMENT", DIRECTION_UNKNOWN, "replacement descriptive only"),
        ("BID_WALL_WITH_BUY_PRESSURE", "WALL_FLOW", DIRECTION_UNKNOWN, "bid+buy ambiguous"),
        ("ASK_WALL_WITH_SELL_PRESSURE", "WALL_FLOW", DIRECTION_UNKNOWN, "ask+sell ambiguous"),
    ):
        rows.append(_dir(p, fam, d, reason))
    # Higher-low armed variants
    for i in range(3, 12):
        rows.append(_dir(f"HL_P{i}", "HIGHER_LOW_ARMED_ACTION", DIRECTION_LONG, "higher-low long-to-ceiling armed action"))
        rows.append(_dir(f"P{i}", "HIGHER_LOW_ARMED_ACTION", DIRECTION_LONG, "higher-low variant alias"))
    for i in range(0, 3):
        rows.append(_dir(f"HL_P{i}", "HIGHER_LOW_BASELINE", DIRECTION_NEUTRAL, "baseline P0-P2 not armed action"))
        rows.append(_dir(f"P{i}", "HIGHER_LOW_BASELINE", DIRECTION_NEUTRAL, "baseline P0-P2 not armed action"))
    # de-dupe by pattern_type keep first
    seen: set[str] = set()
    out = []
    for r in rows:
        if r["pattern_type"] in seen:
            continue
        seen.add(r["pattern_type"])
        out.append(r)
    return sorted(out, key=lambda r: r["pattern_type"])


def direction_lookup() -> dict[str, dict[str, str]]:
    return {r["pattern_type"]: r for r in build_direction_mapping()}


def expected_direction_for(
    pattern_type: str,
    *,
    pattern_family: str | None = None,
    variant: str | None = None,
    source_family: str | None = None,
) -> tuple[str, str]:
    """Return (expected_direction, reason)."""
    lookup = direction_lookup()
    ptype = str(pattern_type or "")
    variant = str(variant or "")
    source_family = str(source_family or "")
    if source_family == "HIGHER_LOW_ARMED_ACTION" or ptype.startswith("HL_"):
        v = variant or ptype.replace("HL_", "")
        if v in BASELINE_HL_VARIANTS:
            return DIRECTION_NEUTRAL, "baseline P0-P2 not armed action"
        if v in ARMED_HL_VARIANTS or (ptype.startswith("HL_P") and ptype[4:].isdigit()):
            return DIRECTION_LONG, "higher-low armed action"
    if ptype in lookup:
        return lookup[ptype]["expected_direction"], lookup[ptype]["mapping_reason"]
    # Heuristics for unlisted types — conservative
    up = ptype.upper()
    if "ABSORPTION" in up and "BID" in up:
        return DIRECTION_LONG, "heuristic bid absorption"
    if "ABSORPTION" in up and "ASK" in up:
        return DIRECTION_SHORT, "heuristic ask absorption"
    if "BID_WALL_FAILURE" in up or "BID_WALL_CONFIRMED_BREAK" in up:
        return DIRECTION_SHORT, "heuristic bid failure"
    if "ASK_WALL_FAILURE" in up or "ASK_WALL_CONFIRMED_BREAK" in up:
        return DIRECTION_LONG, "heuristic ask failure"
    if pattern_family in {"PRICE_DELTA_DIVERGENCE", "PRICE_OI", "LIQUIDATION", "WALL_IMBALANCE"}:
        return DIRECTION_NEUTRAL, "context family default"
    return DIRECTION_UNKNOWN, "unmapped pattern type"


# ---------------------------------------------------------------------------
# Price path loading
# ---------------------------------------------------------------------------

@dataclass
class PricePoint:
    ts: datetime
    mid: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None


@dataclass
class SegmentPath:
    segment_id: str
    start: datetime
    end: datetime
    points: list[PricePoint] = field(default_factory=list)
    times: list[datetime] = field(default_factory=list)
    sample_interval_seconds: float = 60.0


def load_gaps(path: Path) -> list[tuple[datetime, datetime]]:
    gaps = []
    for r in read_csv_rows(path):
        a = _parse_dt(r.get("gap_start_ts"))
        b = _parse_dt(r.get("gap_end_ts"))
        if a and b:
            gaps.append((a, b))
    return sorted(gaps, key=lambda x: x[0])


def _in_gap(ts: datetime, gaps: Sequence[tuple[datetime, datetime]]) -> bool:
    for a, b in gaps:
        if a <= ts <= b:
            return True
        if a > ts:
            break
    return False


def _gap_overlaps_window(
    start_exclusive: datetime,
    end_inclusive: datetime,
    gaps: Sequence[tuple[datetime, datetime]],
) -> bool:
    """True if any gap intersects (start_exclusive, end_inclusive]."""
    if end_inclusive <= start_exclusive:
        return False
    for a, b in gaps:
        if b <= start_exclusive:
            continue
        if a > end_inclusive:
            break
        # overlap of (start_exclusive, end_inclusive] with [a, b]
        if a <= end_inclusive and b > start_exclusive:
            return True
    return False


def estimate_sample_interval_seconds(
    times: Sequence[datetime], *, default: float = 60.0
) -> float:
    """Robust sample interval = median of positive consecutive deltas."""
    deltas: list[float] = []
    for i in range(1, len(times)):
        d = (times[i] - times[i - 1]).total_seconds()
        if d > 0:
            deltas.append(d)
    if not deltas:
        return float(default)
    med = _median(deltas)
    return float(med) if med is not None else float(default)


def coverage_tolerance_seconds(sample_interval_seconds: float) -> float:
    """Allowed shortfall of last sample before exact horizon_end for rastered paths.

    coverage_tolerance_seconds = max(interval * 1.1, interval + 1e-6)
    A sample need not land exactly on horizon_end.
    """
    iv = max(float(sample_interval_seconds), 0.0)
    return max(iv * 1.1, iv + 1e-6)


def load_segment_paths(
    *,
    full_history_dir: Path,
    price_source: str,
    segments: Sequence[Mapping[str, Any]],
) -> dict[str, SegmentPath]:
    gaps = load_gaps(full_history_dir / "replay_gaps.csv")
    out: dict[str, SegmentPath] = {}
    for seg in segments:
        sid = str(seg.get("segment_id") or "")
        if not sid:
            continue
        start = _parse_dt(seg.get("segment_start_ts"))
        end = _parse_dt(seg.get("segment_end_ts"))
        if start is None or end is None:
            continue
        out[sid] = SegmentPath(segment_id=sid, start=start, end=end)

    default_interval = 60.0
    if price_source == "mid":
        for r in read_csv_rows(full_history_dir / "segment_replay_samples.csv"):
            sid = str(r.get("segment_id") or "")
            path = out.get(sid)
            if path is None:
                continue
            ts = _parse_dt(r.get("sample_ts"))
            mid = _safe_float(r.get("mid_price"))
            if ts is None or mid is None:
                continue
            if ts < path.start or ts > path.end:
                continue
            if _in_gap(ts, gaps):
                continue
            path.points.append(PricePoint(ts=ts, mid=mid, close=mid, high=mid, low=mid, open=mid))
    else:
        # Bars: usable only after bucket_end (bar complete)
        for r in read_csv_rows(full_history_dir / "price_bars_1m.csv"):
            # bars may lack segment_id — assign by time overlap
            ts = _parse_dt(r.get("bucket_end"))
            if ts is None:
                continue
            o = _safe_float(r.get("open_price"))
            h = _safe_float(r.get("high_price"))
            lo = _safe_float(r.get("low_price"))
            c = _safe_float(r.get("close_price"))
            if c is None:
                continue
            for path in out.values():
                if path.start <= ts <= path.end and not _in_gap(ts, gaps):
                    path.points.append(
                        PricePoint(ts=ts, mid=c, open=o, high=h, low=lo, close=c)
                    )
                    break

    for path in out.values():
        path.points.sort(key=lambda p: p.ts)
        # de-dupe identical timestamps keep last
        dedup: dict[datetime, PricePoint] = {}
        for p in path.points:
            dedup[p.ts] = p
        path.points = [dedup[t] for t in sorted(dedup)]
        path.times = [p.ts for p in path.points]
        path.sample_interval_seconds = estimate_sample_interval_seconds(
            path.times, default=default_interval
        )
    return out


# ---------------------------------------------------------------------------
# Forward outcome computation
# ---------------------------------------------------------------------------

def _bps(change: float, start: float) -> float:
    if start == 0:
        return 0.0
    return change / abs(start) * 10000.0


def _classify_forward_completeness(
    *,
    event_time: datetime,
    horizon_end: datetime,
    path: SegmentPath,
    last_ts: datetime,
    sample_count: int,
    aborted_reason: str | None,
    gaps: Sequence[tuple[datetime, datetime]],
) -> tuple[bool, str, float, float]:
    """Return (complete, end_reason, seconds_available, seconds_to_horizon_end).

    Mid/bar raster semantics: no sample required exactly at horizon_end.
    Complete when horizon fits in segment, no gap in window, samples exist, and
    0 <= horizon_end - last_forward_time <= coverage_tolerance.
    NO_FORWARD_DATA only when sample_count == 0 (caller handles empty path).
    """
    seconds_available = (last_ts - event_time).total_seconds()
    seconds_to_horizon = (horizon_end - last_ts).total_seconds()
    if sample_count <= 0:
        return False, "NO_FORWARD_DATA", seconds_available, seconds_to_horizon

    if aborted_reason == "DATA_GAP":
        return False, "DATA_GAP", seconds_available, seconds_to_horizon

    # Gap anywhere in the required forward window (no tolerance across gaps)
    window_end = min(horizon_end, path.end)
    if _gap_overlaps_window(event_time, window_end, gaps):
        return False, "DATA_GAP", seconds_available, seconds_to_horizon
    if _gap_overlaps_window(last_ts, window_end, gaps):
        return False, "DATA_GAP", seconds_available, seconds_to_horizon

    if horizon_end > path.end:
        return False, "SEGMENT_END", seconds_available, seconds_to_horizon

    if aborted_reason == "SEGMENT_END":
        return False, "SEGMENT_END", seconds_available, seconds_to_horizon

    tol = coverage_tolerance_seconds(path.sample_interval_seconds)
    if 0.0 <= seconds_to_horizon <= tol:
        return True, "HORIZON_COMPLETE", seconds_available, seconds_to_horizon

    return False, "INSUFFICIENT_SAMPLE_COVERAGE", seconds_available, seconds_to_horizon


def compute_forward_outcome(
    *,
    event_time: datetime,
    start_price: float,
    direction: str,
    path: SegmentPath,
    horizon_sec: int,
    targets_bps: Sequence[float],
    stops_bps: Sequence[float],
    price_source: str,
    gaps: Sequence[tuple[datetime, datetime]],
) -> dict[str, Any]:
    """Compute one horizon outcome. Strictly uses samples with event_time < ts <= horizon_end."""
    horizon_end = event_time + timedelta(seconds=horizon_sec)
    # first index with ts > event_time
    i0 = bisect.bisect_right(path.times, event_time)
    samples: list[PricePoint] = []
    aborted_reason: str | None = None
    prev_ts = event_time
    for i in range(i0, len(path.points)):
        p = path.points[i]
        if p.ts <= event_time:
            continue
        if p.ts > horizon_end:
            break
        if p.ts > path.end:
            aborted_reason = "SEGMENT_END"
            break
        # Stop before crossing a gap; do not include post-gap samples
        if _in_gap(p.ts, gaps) or _gap_overlaps_window(prev_ts, p.ts, gaps):
            aborted_reason = "DATA_GAP"
            break
        samples.append(p)
        prev_ts = p.ts

    if not samples:
        # Distinguish no samples vs horizon beyond segment with empty path after event
        if horizon_end > path.end and (path.end - event_time).total_seconds() > 0:
            # still no forward samples after event → NO_FORWARD_DATA takes precedence
            # when count==0 (spec: NO_FORWARD_DATA only iff count==0)
            pass
        return {
            "forward_data_complete": False,
            "forward_end_reason": "NO_FORWARD_DATA",
            "forward_sample_count": 0,
            "first_forward_time": None,
            "last_forward_time": None,
            "start_price": start_price,
            "end_price": None,
            "return_bps": None,
            "mfe_bps": None,
            "mae_bps": None,
            "time_to_mfe_seconds": None,
            "time_to_mae_seconds": None,
            "seconds_available": 0.0,
            "seconds_to_horizon_end": (horizon_end - event_time).total_seconds(),
            "insufficient_forward_reason": "NO_FORWARD_DATA",
            "first_move_direction": None,
            "first_move_bps": None,
            "first_move_time_seconds": None,
            "max_favourable_price": None,
            "max_adverse_price": None,
            "targets": {},
            "stops": {},
            "target_before_stop": {},
        }

    last_ts = samples[-1].ts
    complete, end_reason, available, seconds_to_horizon = _classify_forward_completeness(
        event_time=event_time,
        horizon_end=horizon_end,
        path=path,
        last_ts=last_ts,
        sample_count=len(samples),
        aborted_reason=aborted_reason,
        gaps=gaps,
    )

    # Build path of favourable/adverse depending on direction & price_source
    fav_ext = 0.0
    adv_ext = 0.0
    fav_price = start_price
    adv_price = start_price
    t_mfe = None
    t_mae = None
    end_price = samples[-1].close if samples[-1].close is not None else samples[-1].mid

    first_move_dir = None
    first_move_bps = None
    first_move_t = None

    # Track target/stop first hit times
    target_hit: dict[float, float | None] = {t: None for t in targets_bps}
    stop_hit: dict[float, float | None] = {s: None for s in stops_bps}
    # For same-bar ambiguity tracking per (target, stop)
    tbs_status: dict[tuple[float, float], str | bool | None] = {}

    def _upd_extrema(up_bps: float, down_bps: float, ts: datetime, high_p: float, low_p: float) -> None:
        nonlocal fav_ext, adv_ext, fav_price, adv_price, t_mfe, t_mae
        if direction == DIRECTION_LONG:
            if up_bps > fav_ext:
                fav_ext = up_bps
                fav_price = high_p
                t_mfe = (ts - event_time).total_seconds()
            if down_bps > adv_ext:
                adv_ext = down_bps
                adv_price = low_p
                t_mae = (ts - event_time).total_seconds()
        elif direction == DIRECTION_SHORT:
            if down_bps > fav_ext:
                fav_ext = down_bps
                fav_price = low_p
                t_mfe = (ts - event_time).total_seconds()
            if up_bps > adv_ext:
                adv_ext = up_bps
                adv_price = high_p
                t_mae = (ts - event_time).total_seconds()
        else:
            # NEUTRAL/UNKNOWN: track absolute up/down as fav=up, adv=down for raw stats
            if up_bps > fav_ext:
                fav_ext = up_bps
                fav_price = high_p
                t_mfe = (ts - event_time).total_seconds()
            if down_bps > adv_ext:
                adv_ext = down_bps
                adv_price = low_p
                t_mae = (ts - event_time).total_seconds()

    for p in samples:
        if price_source == "high_low" and p.high is not None and p.low is not None:
            high_p, low_p = p.high, p.low
            close_p = p.close if p.close is not None else p.mid
        else:
            px = p.mid if price_source == "mid" else (p.close if p.close is not None else p.mid)
            if px is None:
                continue
            high_p = low_p = close_p = px

        up_bps = _bps(high_p - start_price, start_price)
        down_bps = _bps(start_price - low_p, start_price)
        dt = (p.ts - event_time).total_seconds()
        if first_move_dir is None and close_p is not None and close_p != start_price:
            move = _bps(close_p - start_price, start_price)
            first_move_bps = move
            first_move_t = dt
            first_move_dir = "UP" if move > 0 else "DOWN"

        _upd_extrema(up_bps, down_bps, p.ts, high_p, low_p)

        # Target / stop hits
        same_bar_both: dict[tuple[float, float], bool] = {}
        for t_bps in targets_bps:
            if target_hit[t_bps] is not None:
                continue
            hit = False
            if direction == DIRECTION_LONG and up_bps >= t_bps:
                hit = True
            elif direction == DIRECTION_SHORT and down_bps >= t_bps:
                hit = True
            if hit:
                target_hit[t_bps] = dt
        for s_bps in stops_bps:
            if stop_hit[s_bps] is not None:
                continue
            hit = False
            if direction == DIRECTION_LONG and down_bps >= s_bps:
                hit = True
            elif direction == DIRECTION_SHORT and up_bps >= s_bps:
                hit = True
            if hit:
                stop_hit[s_bps] = dt

        # Same-bar ambiguity for high_low: if both target and stop appear first time on this bar
        if price_source == "high_low" and direction in {DIRECTION_LONG, DIRECTION_SHORT}:
            for t_bps in targets_bps:
                for s_bps in stops_bps:
                    key = (t_bps, s_bps)
                    if key in tbs_status:
                        continue
                    t_just = target_hit[t_bps] == dt
                    s_just = stop_hit[s_bps] == dt
                    if t_just and s_just:
                        tbs_status[key] = "AMBIGUOUS_SAME_BAR"
                    elif t_just and (stop_hit[s_bps] is None or stop_hit[s_bps] > dt):
                        tbs_status[key] = True
                    elif s_just and (target_hit[t_bps] is None or target_hit[t_bps] > dt):
                        tbs_status[key] = False

    # Finalize target_before_stop for mid/close (ordered by time)
    if price_source != "high_low" or direction not in {DIRECTION_LONG, DIRECTION_SHORT}:
        for t_bps in targets_bps:
            for s_bps in stops_bps:
                key = (t_bps, s_bps)
                tt, st = target_hit[t_bps], stop_hit[s_bps]
                if tt is None and st is None:
                    tbs_status[key] = None
                elif tt is not None and (st is None or tt < st):
                    tbs_status[key] = True
                elif st is not None and (tt is None or st < tt):
                    tbs_status[key] = False
                elif tt is not None and st is not None and abs(tt - st) < 1e-12:
                    tbs_status[key] = "AMBIGUOUS_SAME_BAR"
                else:
                    tbs_status[key] = None
    else:
        for t_bps in targets_bps:
            for s_bps in stops_bps:
                key = (t_bps, s_bps)
                if key in tbs_status:
                    continue
                tt, st = target_hit[t_bps], stop_hit[s_bps]
                if tt is None and st is None:
                    tbs_status[key] = None
                elif tt is not None and (st is None or tt < st):
                    tbs_status[key] = True
                elif st is not None and (tt is None or st < tt):
                    tbs_status[key] = False
                else:
                    tbs_status[key] = "AMBIGUOUS_SAME_BAR"

    ret_bps = None
    if end_price is not None:
        if direction == DIRECTION_SHORT:
            ret_bps = _bps(start_price - end_price, start_price)
        elif direction == DIRECTION_LONG:
            ret_bps = _bps(end_price - start_price, start_price)
        else:
            ret_bps = _bps(end_price - start_price, start_price)

    return {
        "forward_data_complete": complete,
        "forward_end_reason": end_reason,
        "forward_sample_count": len(samples),
        "first_forward_time": samples[0].ts.isoformat(),
        "last_forward_time": samples[-1].ts.isoformat(),
        "start_price": start_price,
        "end_price": end_price,
        "return_bps": ret_bps,
        "mfe_bps": fav_ext if direction in {DIRECTION_LONG, DIRECTION_SHORT, DIRECTION_NEUTRAL, DIRECTION_UNKNOWN} else None,
        "mae_bps": adv_ext if direction in {DIRECTION_LONG, DIRECTION_SHORT, DIRECTION_NEUTRAL, DIRECTION_UNKNOWN} else None,
        "time_to_mfe_seconds": t_mfe,
        "time_to_mae_seconds": t_mae,
        "seconds_available": available,
        "seconds_to_horizon_end": seconds_to_horizon,
        "insufficient_forward_reason": None if complete else end_reason,
        "first_move_direction": first_move_dir,
        "first_move_bps": first_move_bps,
        "first_move_time_seconds": first_move_t,
        "max_favourable_price": fav_price,
        "max_adverse_price": adv_price,
        "targets": target_hit,
        "stops": stop_hit,
        "target_before_stop": tbs_status,
        "directional": direction in {DIRECTION_LONG, DIRECTION_SHORT},
    }


def _armed_window_from_path(source_output_dir: str) -> int | None:
    m = re.search(r"armed_(\d+)s", str(source_output_dir or ""))
    if not m:
        return None
    return int(m.group(1))


def event_stable_key(row: Mapping[str, Any]) -> str:
    source = str(row.get("source_family") or "")
    if source == "HIGHER_LOW_ARMED_ACTION":
        armed_w = row.get("armed_window_seconds")
        if armed_w in (None, ""):
            armed_w = _armed_window_from_path(str(row.get("source_output_dir") or ""))
        parts = [
            str(row.get("segment_id") or ""),
            str(row.get("armed_pair_id") or ""),
            str(row.get("variant") or ""),
            str(armed_w or ""),
            str(row.get("action_time") or ""),
        ]
        return "HL|" + "|".join(parts)
    return "PC|" + str(row.get("event_id") or "")


def cluster_id_for(
    *,
    symbol: str,
    segment_id: str,
    event_time: str,
    direction: str,
    subject_key: str,
    pattern_family: str,
) -> str:
    raw = f"{symbol}|{segment_id}|{event_time}|{direction}|{subject_key or pattern_family}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"C|{digest}"


def outcome_row_headers(params: OutcomeParams) -> list[str]:
    base = [
        "symbol", "segment_id", "source_family", "pattern_family", "pattern_type", "variant",
        "event_id", "event_time", "event_price", "expected_direction", "armed_pair_id",
        "armed_window_seconds", "action_time", "data_complete", "forward_data_complete",
        "forward_end_reason", "price_source", "horizon_seconds", "forward_sample_count",
        "first_forward_time", "last_forward_time", "start_price", "end_price", "return_bps",
        "mfe_bps", "mae_bps", "time_to_mfe_seconds", "time_to_mae_seconds",
    ]
    for t in params.targets_bps:
        ti = int(t) if float(t).is_integer() else t
        base += [f"target_{ti}bps_hit", f"target_{ti}bps_time_seconds"]
    for s in params.stops_bps:
        si = int(s) if float(s).is_integer() else s
        base += [f"stop_{si}bps_hit", f"stop_{si}bps_time_seconds"]
    # named pairs from defaults + any overlapping
    for t, s in ((10, 25), (25, 25), (50, 50), (100, 100)):
        if t in params.targets_bps and s in params.stops_bps:
            base.append(f"target_before_stop_{t}_{s}")
    base += [
        "first_move_direction", "first_move_bps", "first_move_time_seconds",
        "max_favourable_price", "max_adverse_price", "segment_end_time",
        "seconds_available", "insufficient_forward_reason", "stable_event_key", "cluster_id",
    ]
    return base


def _fmt_tbs(v: Any) -> Any:
    if v is True:
        return True
    if v is False:
        return False
    if v is None:
        return ""
    return str(v)


def build_outcome_row(
    *,
    event: Mapping[str, Any],
    direction: str,
    horizon: int,
    outcome: Mapping[str, Any],
    params: OutcomeParams,
    cluster_id: str,
) -> dict[str, Any]:
    directional = direction in {DIRECTION_LONG, DIRECTION_SHORT}
    ep = _safe_float(event.get("event_price"))
    row: dict[str, Any] = {
        "symbol": event.get("symbol"),
        "segment_id": event.get("segment_id"),
        "source_family": event.get("source_family"),
        "pattern_family": event.get("pattern_family") or event.get("source_family"),
        "pattern_type": event.get("pattern_type"),
        "variant": event.get("variant") or "",
        "event_id": event.get("event_id"),
        "event_time": event.get("event_time"),
        "event_price": ep,
        "expected_direction": direction,
        "armed_pair_id": event.get("armed_pair_id") or "",
        "armed_window_seconds": event.get("armed_window_seconds") or "",
        "action_time": event.get("action_time") or "",
        "data_complete": event.get("data_complete"),
        "forward_data_complete": bool(outcome.get("forward_data_complete")),
        "forward_end_reason": outcome.get("forward_end_reason"),
        "price_source": params.price_source,
        "horizon_seconds": horizon,
        "forward_sample_count": outcome.get("forward_sample_count"),
        "first_forward_time": outcome.get("first_forward_time"),
        "last_forward_time": outcome.get("last_forward_time"),
        "start_price": outcome.get("start_price"),
        "end_price": outcome.get("end_price"),
        "return_bps": outcome.get("return_bps") if directional or direction in {DIRECTION_NEUTRAL, DIRECTION_UNKNOWN} else None,
        "mfe_bps": outcome.get("mfe_bps"),
        "mae_bps": outcome.get("mae_bps"),
        "time_to_mfe_seconds": outcome.get("time_to_mfe_seconds"),
        "time_to_mae_seconds": outcome.get("time_to_mae_seconds"),
        "first_move_direction": outcome.get("first_move_direction"),
        "first_move_bps": outcome.get("first_move_bps"),
        "first_move_time_seconds": outcome.get("first_move_time_seconds"),
        "max_favourable_price": outcome.get("max_favourable_price"),
        "max_adverse_price": outcome.get("max_adverse_price"),
        "segment_end_time": event.get("segment_end_time"),
        "seconds_available": outcome.get("seconds_available"),
        "insufficient_forward_reason": outcome.get("insufficient_forward_reason"),
        "stable_event_key": event_stable_key(event),
        "cluster_id": cluster_id,
    }
    targets = outcome.get("targets") or {}
    stops = outcome.get("stops") or {}
    tbs = outcome.get("target_before_stop") or {}
    for t in params.targets_bps:
        ti = int(t) if float(t).is_integer() else t
        hit_t = targets.get(t)
        if directional:
            row[f"target_{ti}bps_hit"] = hit_t is not None
            row[f"target_{ti}bps_time_seconds"] = hit_t
        else:
            row[f"target_{ti}bps_hit"] = ""
            row[f"target_{ti}bps_time_seconds"] = ""
    for s in params.stops_bps:
        si = int(s) if float(s).is_integer() else s
        hit_s = stops.get(s)
        if directional:
            row[f"stop_{si}bps_hit"] = hit_s is not None
            row[f"stop_{si}bps_time_seconds"] = hit_s
        else:
            row[f"stop_{si}bps_hit"] = ""
            row[f"stop_{si}bps_time_seconds"] = ""
    for t, s in ((10, 25), (25, 25), (50, 50), (100, 100)):
        if t in params.targets_bps and s in params.stops_bps:
            key = f"target_before_stop_{t}_{s}"
            if directional:
                row[key] = _fmt_tbs(tbs.get((float(t), float(s))))
            else:
                row[key] = ""
    return row


# ---------------------------------------------------------------------------
# Aggregations / bootstrap / baselines / ranking
# ---------------------------------------------------------------------------

def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    n = len(ys)
    mid = n // 2
    if n % 2:
        return ys[mid]
    return 0.5 * (ys[mid - 1] + ys[mid])


def _mean(xs: list[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _percentile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    if len(ys) == 1:
        return ys[0]
    pos = (len(ys) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return ys[lo]
    return ys[lo] * (hi - pos) + ys[hi] * (pos - lo)


def _rate(flags: list[bool]) -> float | None:
    if not flags:
        return None
    return sum(1 for f in flags if f) / len(flags)


def summarize_group(
    rows: Sequence[Mapping[str, Any]],
    *,
    group_key: str,
    horizon: int,
    params: OutcomeParams,
    expected_direction: str | None = None,
) -> dict[str, Any]:
    complete = [r for r in rows if _truthy(r.get("forward_data_complete"))]
    incomplete = [r for r in rows if not _truthy(r.get("forward_data_complete"))]
    directional = expected_direction in {DIRECTION_LONG, DIRECTION_SHORT} if expected_direction else True

    def nums(key: str, only_complete: bool = True) -> list[float]:
        src = complete if only_complete else rows
        out = []
        for r in src:
            v = _safe_float(r.get(key))
            if v is not None:
                out.append(v)
        return out

    def bools(key: str) -> list[bool]:
        if not directional:
            return []
        out = []
        for r in complete:
            v = r.get(key)
            if v == "" or v is None:
                continue
            out.append(_truthy(v) if not isinstance(v, bool) else v)
        return out

    mfe = nums("mfe_bps")
    mae = nums("mae_bps")
    rets = nums("return_bps")
    summary = {
        "group_key": group_key,
        "horizon_seconds": horizon,
        "sample_count_raw": len(rows),
        "sample_count_complete": len(complete),
        "sample_count_incomplete": len(incomplete),
        "segment_count": len({str(r.get("segment_id")) for r in complete}),
        "symbol_count": len({str(r.get("symbol")) for r in complete}),
        "expected_direction": expected_direction,
        "median_return_bps": _median(rets),
        "mean_return_bps": _mean(rets),
        "median_mfe_bps": _median(mfe),
        "mean_mfe_bps": _mean(mfe),
        "median_mae_bps": _median(mae),
        "mean_mae_bps": _mean(mae),
        "p25_mfe_bps": _percentile(mfe, 0.25),
        "p75_mfe_bps": _percentile(mfe, 0.75),
        "p25_mae_bps": _percentile(mae, 0.25),
        "p75_mae_bps": _percentile(mae, 0.75),
        "positive_return_rate": _rate([r > 0 for r in rets]) if directional else None,
        "insufficient_sample": len(complete) < params.min_samples,
    }
    for t in params.targets_bps:
        ti = int(t) if float(t).is_integer() else t
        summary[f"target_{ti}bps_hit_rate"] = _rate(bools(f"target_{ti}bps_hit")) if directional else None
        times = nums(f"target_{ti}bps_time_seconds")
        summary[f"median_time_to_target_{ti}bps"] = _median(times) if directional else None
    for s in params.stops_bps:
        si = int(s) if float(s).is_integer() else s
        summary[f"stop_{si}bps_hit_rate"] = _rate(bools(f"stop_{si}bps_hit")) if directional else None
        times = nums(f"stop_{si}bps_time_seconds")
        summary[f"median_time_to_stop_{si}bps"] = _median(times) if directional else None
    for t, s in ((10, 25), (25, 25), (50, 50), (100, 100)):
        if t in params.targets_bps and s in params.stops_bps:
            key = f"target_before_stop_{t}_{s}"
            flags = []
            for r in complete:
                v = r.get(key)
                if v in ("", None, "AMBIGUOUS_SAME_BAR"):
                    continue
                flags.append(_truthy(v) if not isinstance(v, bool) else bool(v))
            summary[f"{key}_rate"] = _rate(flags) if directional else None
    return summary


def bootstrap_ci(
    values: Sequence[float],
    *,
    iterations: int,
    seed: int,
    statistic: str = "median",
) -> tuple[float | None, float | None, float | None]:
    if not values or iterations <= 0:
        return None, None, None
    rng = random.Random(seed)
    n = len(values)
    estimates = []
    for i in range(iterations):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        if statistic == "median":
            estimates.append(_median(sample) or 0.0)
        elif statistic == "mean":
            estimates.append(_mean(sample) or 0.0)
        elif statistic == "rate":
            estimates.append(sum(sample) / n)
        else:
            estimates.append(_median(sample) or 0.0)
    estimates.sort()
    lo = estimates[int(0.025 * (iterations - 1))]
    hi = estimates[int(0.975 * (iterations - 1))]
    est = _median(list(values)) if statistic == "median" else (
        _mean(list(values)) if statistic != "rate" else sum(values) / len(values)
    )
    return est, lo, hi


def research_score(row: Mapping[str, Any]) -> float:
    """Transparent exploratory score — not a profitability claim."""
    score = 0.0
    lift_tbs = _safe_float(row.get("baseline_target_before_stop_lift")) or 0.0
    lift_hit = _safe_float(row.get("baseline_target_25_lift")) or 0.0
    mfe = _safe_float(row.get("median_mfe_bps")) or 0.0
    mae = _safe_float(row.get("median_mae_bps")) or 0.0
    cons = _safe_float(row.get("segment_consistency_rate")) or 0.0
    n = int(row.get("sample_count_complete") or 0)
    segs = int(row.get("segment_count") or 0)
    score += 2.0 * lift_tbs
    score += 1.5 * lift_hit
    score += 0.02 * mfe
    score -= 0.03 * mae
    score += 20.0 * cons
    if n < 50:
        score -= 5.0
    if segs < 2:
        score -= 20.0
    if _truthy(row.get("single_segment_only")):
        score -= 15.0
    return score


def label_from_score(row: Mapping[str, Any], score: float) -> tuple[str, str]:
    flags = []
    n = int(row.get("sample_count_complete") or 0)
    segs = int(row.get("segment_count") or 0)
    cons = _safe_float(row.get("segment_consistency_rate"))
    if n < int(row.get("min_samples") or 30):
        return "INSUFFICIENT_DATA", "below_min_samples"
    if segs < 2 or _truthy(row.get("single_segment_only")):
        return "UNSTABLE_ACROSS_SEGMENTS", "single_segment_or_lt2"
    if cons is not None and cons < 0.5:
        flags.append("low_segment_consistency")
        return "UNSTABLE_ACROSS_SEGMENTS", ",".join(flags)
    lift = _safe_float(row.get("baseline_target_before_stop_lift")) or 0.0
    if score >= 5 and lift > 0:
        return "PROMISING_FOR_OOS", ",".join(flags) if flags else ""
    if score >= 0:
        return "WEAK_EVIDENCE", ",".join(flags) if flags else ""
    return "NO_CLEAR_EDGE", ",".join(flags) if flags else ""


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

PHASE6_OUTPUT_FILES = [
    "PATTERN_OUTCOME_REPORT.md",
    "pattern_outcome_summary.json",
    "pattern_outcome_integrity.json",
    "pattern_outcome_errors.csv",
    "pattern_direction_mapping.csv",
    "pattern_forward_outcomes.csv",
    "pattern_event_clusters.csv",
    "pattern_cluster_forward_outcomes.csv",
    "pattern_outcome_summary_by_type.csv",
    "pattern_outcome_summary_by_family.csv",
    "pattern_outcome_summary_by_segment.csv",
    "pattern_outcome_summary_by_direction.csv",
    "pattern_outcome_summary_by_context.csv",
    "pattern_outcome_summary_by_cluster.csv",
    "pattern_outcome_summary_by_horizon.csv",
    "pattern_outcome_summary_by_target_stop.csv",
    "pattern_baseline_outcomes.csv",
    "pattern_baseline_comparison.csv",
    "pattern_outcome_confidence_intervals.csv",
    "pattern_segment_stability.csv",
    "pattern_research_ranking.csv",
]


@dataclass
class PatternOutcomeResult:
    params: OutcomeParams
    output_dir: Path
    decision: str = "PATTERN_OUTCOMES_FAILED"
    summary: dict[str, Any] = field(default_factory=dict)
    integrity: dict[str, Any] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)
    ok: bool = False
    runtime_sec: float = 0.0


def decide_phase6(
    *,
    ok: bool,
    gap_incomplete: bool,
    warnings: bool,
    insufficient: bool,
) -> str:
    if not ok:
        return "PATTERN_OUTCOMES_FAILED"
    if insufficient:
        return "PATTERN_OUTCOMES_DATA_INSUFFICIENT"
    if warnings:
        return "PATTERN_OUTCOMES_COMPLETE_WITH_WARNINGS"
    if gap_incomplete:
        return "PATTERN_OUTCOMES_COMPLETE_WITH_GAPS"
    return "PATTERN_OUTCOMES_COMPLETE"


def check_outcome_integrity(
    *,
    outcomes: Sequence[Mapping[str, Any]],
    clusters: Sequence[Mapping[str, Any]],
    eval_rows: Sequence[Mapping[str, Any]],
    segment_paths: Mapping[str, SegmentPath],
    forward_samples_processed: int = 0,
    horizons_seconds: Sequence[int] | None = None,
) -> dict[str, Any]:
    errs: list[str] = []
    warns: list[str] = []
    seen = set()
    for r in outcomes:
        key = (str(r.get("stable_event_key")), int(r.get("horizon_seconds") or 0))
        if key in seen:
            errs.append(f"duplicate outcome {key}")
        seen.add(key)
        et = _parse_dt(r.get("event_time"))
        fft = _parse_dt(r.get("first_forward_time"))
        if et and fft and not (fft > et):
            errs.append(f"lookahead first_forward_time for {r.get('event_id')}")
        at = _parse_dt(r.get("action_time"))
        if str(r.get("source_family")) == "HIGHER_LOW_ARMED_ACTION" and at and et and et < at:
            # event_time should be action_time for HL
            pass
        if str(r.get("variant")) in BASELINE_HL_VARIANTS and str(r.get("source_family")) == "HIGHER_LOW_ARMED_ACTION":
            errs.append(f"baseline variant as armed action {r.get('event_id')}")
        for k in ("time_to_mfe_seconds", "time_to_mae_seconds"):
            v = _safe_float(r.get(k))
            if v is not None and v < 0:
                errs.append(f"negative {k}")
        sid = str(r.get("segment_id") or "")
        path = segment_paths.get(sid)
        if path and fft and (fft < path.start or fft > path.end):
            errs.append(f"forward sample outside segment {r.get('event_id')}")
        # NO_FORWARD_DATA must mean zero samples
        n_fwd = int(r.get("forward_sample_count") or 0)
        if str(r.get("forward_end_reason")) == "NO_FORWARD_DATA" and n_fwd > 0:
            errs.append(f"NO_FORWARD_DATA_WITH_SAMPLES event={r.get('event_id')} h={r.get('horizon_seconds')}")
    # HL armed uniqueness across eval
    hl_keys = set()
    for r in eval_rows:
        if str(r.get("source_family")) != "HIGHER_LOW_ARMED_ACTION":
            continue
        if str(r.get("variant")) in BASELINE_HL_VARIANTS:
            errs.append("P0-P2 marked as armed action in eval input")
            continue
        k = (
            str(r.get("segment_id")),
            str(r.get("armed_pair_id")),
            str(r.get("variant")),
            str(r.get("armed_window_seconds") or _armed_window_from_path(str(r.get("source_output_dir") or ""))),
        )
        if k in hl_keys:
            errs.append(f"duplicate HL armed key {k}")
        hl_keys.add(k)
    cluster_ids = {c.get("cluster_id") for c in clusters}
    for r in outcomes:
        cid = r.get("cluster_id")
        if cid and cid not in cluster_ids and clusters:
            warns.append(f"outcome cluster missing {cid}")

    complete_n = sum(1 for r in outcomes if _truthy(r.get("forward_data_complete")))
    # Complete counts by horizon must be weakly decreasing for longer horizons
    horizons = list(horizons_seconds) if horizons_seconds else sorted(
        {int(r.get("horizon_seconds") or 0) for r in outcomes}
    )
    complete_by_h: dict[int, int] = {}
    for h in horizons:
        complete_by_h[int(h)] = sum(
            1
            for r in outcomes
            if int(r.get("horizon_seconds") or 0) == int(h)
            and _truthy(r.get("forward_data_complete"))
        )
    sorted_h = sorted(complete_by_h)
    for i in range(1, len(sorted_h)):
        prev_h, cur_h = sorted_h[i - 1], sorted_h[i]
        if complete_by_h[cur_h] > complete_by_h[prev_h]:
            errs.append(
                f"complete_count_non_monotonic h={prev_h}:{complete_by_h[prev_h]} "
                f"< h={cur_h}:{complete_by_h[cur_h]}"
            )

    # Implausible: samples processed and segment duration supports shortest horizon
    # but zero complete outcomes.
    if outcomes and forward_samples_processed > 0 and complete_n == 0:
        shortest = min(horizons) if horizons else None
        supports = False
        if shortest is not None:
            for path in segment_paths.values():
                if not path.points:
                    continue
                if (path.end - path.start).total_seconds() >= float(shortest):
                    supports = True
                    break
        if supports:
            errs.append("ZERO_COMPLETE_OUTCOMES_IMPLAUSIBLE")
            if shortest is not None and complete_by_h.get(int(shortest), 0) == 0:
                errs.append(
                    f"ZERO_COMPLETE_AT_SHORTEST_HORIZON_{int(shortest)}"
                )

    return {"ok": len(errs) == 0, "errors": errs, "warnings": warns}


def run_pattern_outcome_evaluation(
    *,
    general_output_dir: Path,
    full_history_dir: Path | None = None,
    eval_input_path: Path | None = None,
    output_dir: Path | None = None,
    params: OutcomeParams | None = None,
) -> PatternOutcomeResult:
    t0 = time.perf_counter()
    params = validate_outcome_params(params or OutcomeParams())
    general_output_dir = Path(general_output_dir)
    fh_dir = Path(full_history_dir) if full_history_dir else general_output_dir / "full_history"
    eval_path = Path(eval_input_path) if eval_input_path else general_output_dir / "general_pattern_evaluation_input.csv"
    out_dir = Path(output_dir) if output_dir else general_output_dir / "pattern_outcomes"
    out_dir.mkdir(parents=True, exist_ok=True)
    result = PatternOutcomeResult(params=params, output_dir=out_dir)
    errors: list[dict[str, Any]] = []

    # Always emit mapping
    mapping = build_direction_mapping()
    write_csv_headered(
        out_dir / "pattern_direction_mapping.csv",
        mapping,
        ["pattern_type", "pattern_family", "expected_direction", "mapping_reason", "mapping_version"],
    )

    eval_rows = read_csv_rows(eval_path)
    # Enrich armed_window_seconds
    for r in eval_rows:
        if not r.get("armed_window_seconds"):
            aw = _armed_window_from_path(str(r.get("source_output_dir") or ""))
            if aw is not None:
                r["armed_window_seconds"] = str(aw)
        # HL event time = action_time
        if str(r.get("source_family")) == "HIGHER_LOW_ARMED_ACTION" and r.get("action_time"):
            r["event_time"] = r.get("action_time")

    headers = outcome_row_headers(params)
    if not eval_rows:
        for name in PHASE6_OUTPUT_FILES:
            if name.endswith(".csv") and name != "pattern_direction_mapping.csv":
                # minimal headers
                if name == "pattern_forward_outcomes.csv":
                    write_csv_headered(out_dir / name, [], headers)
                elif name == "pattern_outcome_errors.csv":
                    write_csv_headered(out_dir / name, [], ["phase", "error_type", "error_message", "details"])
                else:
                    write_csv_headered(out_dir / name, [], ["group_key"])
            elif name.endswith(".json"):
                (out_dir / name).write_bytes(orjson.dumps({}, option=orjson.OPT_INDENT_2))
            elif name.endswith(".md"):
                (out_dir / name).write_text("# Pattern Outcome Report\n\nNo evaluation input rows.\n", encoding="utf-8")
        result.decision = "PATTERN_OUTCOMES_DATA_INSUFFICIENT"
        result.summary = {
            "pattern_outcome_event_count": 0,
            "pattern_outcome_complete_count": 0,
            "decision": result.decision,
        }
        result.ok = True
        result.integrity = {"ok": True, "errors": [], "warnings": ["empty input"]}
        result.runtime_sec = time.perf_counter() - t0
        (out_dir / "pattern_outcome_summary.json").write_bytes(
            orjson.dumps(result.summary, option=orjson.OPT_INDENT_2)
        )
        (out_dir / "pattern_outcome_integrity.json").write_bytes(
            orjson.dumps(result.integrity, option=orjson.OPT_INDENT_2)
        )
        return result

    segments = read_csv_rows(fh_dir / "replay_segments.csv")
    segment_paths = load_segment_paths(
        full_history_dir=fh_dir, price_source=params.price_source, segments=segments
    )
    gaps = load_gaps(fh_dir / "replay_gaps.csv")

    # Attach segment end times
    for r in eval_rows:
        sid = str(r.get("segment_id") or "")
        path = segment_paths.get(sid)
        if path:
            r["segment_end_time"] = path.end.isoformat()

    # Clusters
    cluster_members: dict[str, list[dict[str, Any]]] = defaultdict(list)
    event_meta: list[dict[str, Any]] = []
    for r in eval_rows:
        # skip baseline armed mislabels
        if str(r.get("source_family")) == "HIGHER_LOW_ARMED_ACTION" and str(r.get("variant")) in BASELINE_HL_VARIANTS:
            errors.append(
                {
                    "phase": "pattern_outcomes",
                    "error_type": "BASELINE_AS_ARMED",
                    "error_message": f"skipped baseline variant {r.get('variant')}",
                    "details": str(r.get("event_id")),
                }
            )
            continue
        direction, reason = expected_direction_for(
            str(r.get("pattern_type") or ""),
            pattern_family=str(r.get("source_family") or ""),
            variant=str(r.get("variant") or ""),
            source_family=str(r.get("source_family") or ""),
        )
        subject = str(r.get("sequence_id") or r.get("armed_pair_id") or "")
        family = str(r.get("source_family") or r.get("pattern_family") or "")
        cid = cluster_id_for(
            symbol=str(r.get("symbol") or ""),
            segment_id=str(r.get("segment_id") or ""),
            event_time=str(r.get("event_time") or ""),
            direction=direction,
            subject_key=subject,
            pattern_family=family,
        )
        meta = dict(r)
        meta["expected_direction"] = direction
        meta["mapping_reason"] = reason
        meta["cluster_id"] = cid
        meta["stable_event_key"] = event_stable_key(r)
        event_meta.append(meta)
        cluster_members[cid].append(meta)

    cluster_rows = []
    for cid, members in sorted(cluster_members.items(), key=lambda x: x[0]):
        m0 = members[0]
        cluster_rows.append(
            {
                "cluster_id": cid,
                "symbol": m0.get("symbol"),
                "segment_id": m0.get("segment_id"),
                "cluster_time": m0.get("event_time"),
                "expected_direction": m0.get("expected_direction"),
                "member_count": len(members),
                "pattern_types": "|".join(sorted({str(m.get("pattern_type")) for m in members})),
                "pattern_families": "|".join(sorted({str(m.get("source_family")) for m in members})),
                "sequence_ids": "|".join(sorted({str(m.get("sequence_id") or "") for m in members if m.get("sequence_id")})),
                "subject_keys": "|".join(
                    sorted({str(m.get("sequence_id") or m.get("armed_pair_id") or "") for m in members})
                ),
                "data_complete": all(_truthy(m.get("data_complete")) for m in members),
            }
        )
    write_csv_headered(
        out_dir / "pattern_event_clusters.csv",
        cluster_rows,
        [
            "cluster_id", "symbol", "segment_id", "cluster_time", "expected_direction",
            "member_count", "pattern_types", "pattern_families", "sequence_ids",
            "subject_keys", "data_complete",
        ],
    )

    # Forward outcomes
    outcomes: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, int]] = set()
    forward_samples_processed = 0
    for meta in event_meta:
        et = _parse_dt(meta.get("event_time"))
        sid = str(meta.get("segment_id") or "")
        path = segment_paths.get(sid)
        direction = str(meta.get("expected_direction"))
        start_price = _safe_float(meta.get("event_price"))
        if et is None or path is None:
            for h in params.horizons_seconds:
                sk = (meta["stable_event_key"], h)
                if sk in seen_keys:
                    continue
                seen_keys.add(sk)
                dummy = {
                    "forward_data_complete": False,
                    "forward_end_reason": "INCOMPLETE_SOURCE_DATA",
                    "forward_sample_count": 0,
                    "start_price": start_price,
                    "targets": {t: None for t in params.targets_bps},
                    "stops": {s: None for s in params.stops_bps},
                    "target_before_stop": {},
                    "seconds_available": 0,
                    "insufficient_forward_reason": "INCOMPLETE_SOURCE_DATA",
                }
                outcomes.append(
                    build_outcome_row(
                        event=meta, direction=direction, horizon=h, outcome=dummy,
                        params=params, cluster_id=str(meta.get("cluster_id")),
                    )
                )
            continue
        if start_price is None:
            # try nearest path price at/before event (not after)
            i = bisect.bisect_right(path.times, et) - 1
            if i >= 0:
                start_price = path.points[i].mid or path.points[i].close
            meta["event_price"] = start_price
        if start_price is None:
            continue
        for h in params.horizons_seconds:
            sk = (meta["stable_event_key"], h)
            if sk in seen_keys:
                continue
            seen_keys.add(sk)
            oc = compute_forward_outcome(
                event_time=et,
                start_price=float(start_price),
                direction=direction,
                path=path,
                horizon_sec=h,
                targets_bps=params.targets_bps,
                stops_bps=params.stops_bps,
                price_source=params.price_source,
                gaps=gaps,
            )
            forward_samples_processed += int(oc.get("forward_sample_count") or 0)
            # Neutral/unknown: keep raw mfe/mae but blank directional hit fields via build_outcome_row
            if direction not in {DIRECTION_LONG, DIRECTION_SHORT}:
                oc = dict(oc)
                # keep forward_data_complete for availability, but build_outcome_row blanks hits
            outcomes.append(
                build_outcome_row(
                    event=meta,
                    direction=direction,
                    horizon=h,
                    outcome=oc,
                    params=params,
                    cluster_id=str(meta.get("cluster_id")),
                )
            )

    outcomes.sort(
        key=lambda r: (
            str(r.get("event_time") or ""),
            str(r.get("stable_event_key") or ""),
            int(r.get("horizon_seconds") or 0),
        )
    )
    write_csv_headered(out_dir / "pattern_forward_outcomes.csv", outcomes, headers)

    # Cluster outcomes: one representative event per cluster (first by event_id)
    cluster_rep: dict[str, dict[str, Any]] = {}
    for meta in event_meta:
        cid = str(meta.get("cluster_id"))
        if cid not in cluster_rep:
            cluster_rep[cid] = meta
    cluster_outcomes = [
        r for r in outcomes if str(r.get("stable_event_key")) == event_stable_key(cluster_rep.get(str(r.get("cluster_id")), {}))
        or str(r.get("event_id")) == str(cluster_rep.get(str(r.get("cluster_id")), {}).get("event_id"))
    ]
    # Simpler: filter outcomes whose event is the rep
    rep_keys = {event_stable_key(m) for m in cluster_rep.values()}
    cluster_outcomes = [r for r in outcomes if str(r.get("stable_event_key")) in rep_keys]
    write_csv_headered(out_dir / "pattern_cluster_forward_outcomes.csv", cluster_outcomes, headers)

    # Summaries
    def group_and_write(path_name: str, key_fn, rows_src=outcomes):
        groups: dict[tuple[Any, ...], list] = defaultdict(list)
        for r in rows_src:
            groups[key_fn(r)].append(r)
        out_rows = []
        for key, items in sorted(groups.items(), key=lambda x: [str(k) for k in x[0]]):
            horizon = int(items[0].get("horizon_seconds") or 0)
            gkey = "|".join(str(k) for k in key if k != horizon)
            direction = str(items[0].get("expected_direction") or "")
            # if key includes mixed directions, leave expected_direction from first
            out_rows.append(
                summarize_group(
                    items, group_key=gkey, horizon=horizon, params=params, expected_direction=direction
                )
            )
        # headers from first row keys
        if out_rows:
            hdrs = list(out_rows[0].keys())
        else:
            hdrs = ["group_key", "horizon_seconds", "sample_count_complete", "insufficient_sample"]
        write_csv_headered(out_dir / path_name, out_rows, hdrs)
        return out_rows

    by_type = group_and_write(
        "pattern_outcome_summary_by_type.csv",
        lambda r: (r.get("pattern_type"), int(r.get("horizon_seconds") or 0)),
    )
    by_family = group_and_write(
        "pattern_outcome_summary_by_family.csv",
        lambda r: (r.get("pattern_family") or r.get("source_family"), int(r.get("horizon_seconds") or 0)),
    )
    by_segment = group_and_write(
        "pattern_outcome_summary_by_segment.csv",
        lambda r: (r.get("segment_id"), int(r.get("horizon_seconds") or 0)),
    )
    by_direction = group_and_write(
        "pattern_outcome_summary_by_direction.csv",
        lambda r: (r.get("expected_direction"), int(r.get("horizon_seconds") or 0)),
    )
    by_context = group_and_write(
        "pattern_outcome_summary_by_context.csv",
        lambda r: (r.get("source_family"), r.get("expected_direction"), int(r.get("horizon_seconds") or 0)),
    )
    by_cluster = group_and_write(
        "pattern_outcome_summary_by_cluster.csv",
        lambda r: (r.get("cluster_id"), int(r.get("horizon_seconds") or 0)),
        rows_src=cluster_outcomes,
    )
    by_horizon = group_and_write(
        "pattern_outcome_summary_by_horizon.csv",
        lambda r: (int(r.get("horizon_seconds") or 0),),
    )
    # target/stop summary: reuse by_type as proxy grouped by horizon only for named pairs
    by_ts = []
    for h in params.horizons_seconds:
        items = [r for r in outcomes if int(r.get("horizon_seconds") or 0) == h and r.get("expected_direction") in {DIRECTION_LONG, DIRECTION_SHORT}]
        s = summarize_group(items, group_key=f"ALL_DIRECTIONAL|{h}", horizon=h, params=params, expected_direction="MIXED")
        by_ts.append(s)
    write_csv_headered(
        out_dir / "pattern_outcome_summary_by_target_stop.csv",
        by_ts,
        list(by_ts[0].keys()) if by_ts else ["group_key", "horizon_seconds"],
    )

    # Baselines: TIME_MATCHED / BUCKET_MATCHED / DIRECTION_MATCHED
    baseline_outcomes: list[dict[str, Any]] = []
    baseline_compare: list[dict[str, Any]] = []
    rng = random.Random(params.random_seed)

    def _eligible_pool(h: int) -> list[tuple[str, datetime, float]]:
        pool: list[tuple[str, datetime, float]] = []
        for sid, path in segment_paths.items():
            for p in path.points:
                if p.mid is None and p.close is None:
                    continue
                if (path.end - p.ts).total_seconds() < h:
                    continue
                if _in_gap(p.ts, gaps):
                    continue
                pool.append((sid, p.ts, float(p.mid if p.mid is not None else p.close)))
        return pool

    def _unique_sample(
        pool: list[tuple[str, datetime, float]], n: int, rng_local: random.Random
    ) -> list[tuple[str, datetime, float]]:
        if n <= 0 or not pool:
            return []
        if len(pool) <= n:
            # shuffle copy for determinism even when taking all
            out = list(pool)
            rng_local.shuffle(out)
            return out
        idxs = list(range(len(pool)))
        rng_local.shuffle(idxs)
        return [pool[i] for i in idxs[:n]]

    def _emit_baseline_rows(
        *,
        chosen: list[tuple[str, datetime, float]],
        direction: str,
        baseline_type: str,
        h: int,
        symbol: str,
    ) -> list[dict[str, Any]]:
        rows_out: list[dict[str, Any]] = []
        for sid, ts, px in chosen:
            path = segment_paths[sid]
            oc = compute_forward_outcome(
                event_time=ts,
                start_price=px,
                direction=direction,
                path=path,
                horizon_sec=h,
                targets_bps=params.targets_bps,
                stops_bps=params.stops_bps,
                price_source=params.price_source,
                gaps=gaps,
            )
            ev = {
                "symbol": symbol,
                "segment_id": sid,
                "source_family": f"BASELINE_{baseline_type}",
                "pattern_family": "BASELINE",
                "pattern_type": baseline_type,
                "variant": "",
                "event_id": f"BL|{baseline_type}|{direction}|{sid}|{ts.isoformat()}|{h}",
                "event_time": ts.isoformat(),
                "event_price": px,
                "armed_pair_id": "",
                "armed_window_seconds": "",
                "action_time": "",
                "data_complete": True,
                "segment_end_time": path.end.isoformat(),
            }
            row = build_outcome_row(
                event=ev,
                direction=direction,
                horizon=h,
                outcome=oc,
                params=params,
                cluster_id="",
            )
            row["baseline_type"] = baseline_type
            rows_out.append(row)
            baseline_outcomes.append(row)
        return rows_out

    def _compare_row(
        *,
        pattern_group: str,
        baseline_type: str,
        h: int,
        pattern_rows: list[dict[str, Any]],
        baseline_rows: list[dict[str, Any]],
        expected_direction: str,
    ) -> dict[str, Any]:
        psum = summarize_group(
            pattern_rows,
            group_key=f"PATTERN|{pattern_group}|{h}",
            horizon=h,
            params=params,
            expected_direction=expected_direction,
        )
        bsum = summarize_group(
            baseline_rows,
            group_key=f"BASELINE|{baseline_type}|{h}",
            horizon=h,
            params=params,
            expected_direction=expected_direction,
        )
        return {
            "pattern_group": pattern_group,
            "baseline_type": baseline_type,
            "horizon_seconds": h,
            "pattern_sample_count": psum["sample_count_complete"],
            "baseline_sample_count": bsum["sample_count_complete"],
            "pattern_median_mfe_bps": psum["median_mfe_bps"],
            "baseline_median_mfe_bps": bsum["median_mfe_bps"],
            "mfe_lift_bps": (
                None
                if psum["median_mfe_bps"] is None or bsum["median_mfe_bps"] is None
                else psum["median_mfe_bps"] - bsum["median_mfe_bps"]
            ),
            "pattern_median_mae_bps": psum["median_mae_bps"],
            "baseline_median_mae_bps": bsum["median_mae_bps"],
            "mae_improvement_bps": (
                None
                if psum["median_mae_bps"] is None or bsum["median_mae_bps"] is None
                else bsum["median_mae_bps"] - psum["median_mae_bps"]
            ),
            "pattern_target_25bps_hit_rate": psum.get("target_25bps_hit_rate"),
            "baseline_target_25bps_hit_rate": bsum.get("target_25bps_hit_rate"),
            "target_25bps_hit_rate_lift": (
                None
                if psum.get("target_25bps_hit_rate") is None or bsum.get("target_25bps_hit_rate") is None
                else psum["target_25bps_hit_rate"] - bsum["target_25bps_hit_rate"]
            ),
            "pattern_target_before_stop_25_25_rate": psum.get("target_before_stop_25_25_rate"),
            "baseline_target_before_stop_25_25_rate": bsum.get("target_before_stop_25_25_rate"),
            "target_before_stop_lift": (
                None
                if psum.get("target_before_stop_25_25_rate") is None
                or bsum.get("target_before_stop_25_25_rate") is None
                else psum["target_before_stop_25_25_rate"] - bsum["target_before_stop_25_25_rate"]
            ),
        }

    for h in params.horizons_seconds:
        dir_rows = [
            r
            for r in outcomes
            if int(r.get("horizon_seconds") or 0) == h
            and r.get("expected_direction") in {DIRECTION_LONG, DIRECTION_SHORT}
            and _truthy(r.get("forward_data_complete"))
        ]
        n = len(dir_rows)
        if n == 0:
            continue
        symbol = str(dir_rows[0].get("symbol") or "")
        pool = _eligible_pool(h)
        # TIME_MATCHED_RANDOM
        bl_time = _emit_baseline_rows(
            chosen=_unique_sample(pool, n, rng),
            direction=DIRECTION_LONG,
            baseline_type="TIME_MATCHED_RANDOM",
            h=h,
            symbol=symbol,
        )
        baseline_compare.append(
            _compare_row(
                pattern_group="ALL_DIRECTIONAL",
                baseline_type="TIME_MATCHED_RANDOM",
                h=h,
                pattern_rows=dir_rows,
                baseline_rows=bl_time,
                expected_direction=DIRECTION_LONG,
            )
        )
        # BUCKET_MATCHED_RANDOM: same UTC hour-of-day as pattern events
        hour_counts: dict[int, int] = defaultdict(int)
        for r in dir_rows:
            et = _parse_dt(r.get("event_time"))
            if et is not None:
                hour_counts[et.hour] += 1
        bucket_chosen: list[tuple[str, datetime, float]] = []
        used_ts: set[tuple[str, datetime]] = set()
        for hour, cnt in sorted(hour_counts.items()):
            sub = [(sid, ts, px) for sid, ts, px in pool if ts.hour == hour and (sid, ts) not in used_ts]
            picked = _unique_sample(sub, cnt, rng)
            for sid, ts, px in picked:
                used_ts.add((sid, ts))
                bucket_chosen.append((sid, ts, px))
        # top up if some hours had insufficient pool
        if len(bucket_chosen) < n:
            rest = [(sid, ts, px) for sid, ts, px in pool if (sid, ts) not in used_ts]
            bucket_chosen.extend(_unique_sample(rest, n - len(bucket_chosen), rng))
        bl_bucket = _emit_baseline_rows(
            chosen=bucket_chosen[:n],
            direction=DIRECTION_LONG,
            baseline_type="BUCKET_MATCHED_RANDOM",
            h=h,
            symbol=symbol,
        )
        baseline_compare.append(
            _compare_row(
                pattern_group="ALL_DIRECTIONAL",
                baseline_type="BUCKET_MATCHED_RANDOM",
                h=h,
                pattern_rows=dir_rows,
                baseline_rows=bl_bucket,
                expected_direction=DIRECTION_LONG,
            )
        )
        # DIRECTION_MATCHED_RANDOM: per expected direction
        for direction in (DIRECTION_LONG, DIRECTION_SHORT):
            drows = [r for r in dir_rows if r.get("expected_direction") == direction]
            if not drows:
                continue
            bl_dir = _emit_baseline_rows(
                chosen=_unique_sample(pool, len(drows), rng),
                direction=direction,
                baseline_type="DIRECTION_MATCHED_RANDOM",
                h=h,
                symbol=symbol,
            )
            baseline_compare.append(
                _compare_row(
                    pattern_group=f"DIRECTION_{direction}",
                    baseline_type="DIRECTION_MATCHED_RANDOM",
                    h=h,
                    pattern_rows=drows,
                    baseline_rows=bl_dir,
                    expected_direction=direction,
                )
            )
    write_csv_headered(
        out_dir / "pattern_baseline_outcomes.csv",
        baseline_outcomes,
        list(headers) + ["baseline_type"] if baseline_outcomes else headers + ["baseline_type"],
    )
    write_csv_headered(
        out_dir / "pattern_baseline_comparison.csv",
        baseline_compare,
        list(baseline_compare[0].keys()) if baseline_compare else [
            "pattern_group", "baseline_type", "horizon_seconds", "pattern_sample_count"
        ],
    )

    # Confidence intervals for by_type groups with enough samples
    ci_rows = []
    for s in by_type:
        if s.get("insufficient_sample"):
            continue
        if s.get("expected_direction") not in {DIRECTION_LONG, DIRECTION_SHORT}:
            continue
        h = int(s["horizon_seconds"])
        gkey = s["group_key"]
        items = [
            r for r in outcomes
            if str(r.get("pattern_type")) == gkey
            and int(r.get("horizon_seconds") or 0) == h
            and _truthy(r.get("forward_data_complete"))
        ]
        for metric, getter, stat in (
            ("median_mfe_bps", lambda r: _safe_float(r.get("mfe_bps")), "median"),
            ("median_mae_bps", lambda r: _safe_float(r.get("mae_bps")), "median"),
            ("target_25bps_hit_rate", lambda r: 1.0 if _truthy(r.get("target_25bps_hit")) else 0.0, "rate"),
            ("target_50bps_hit_rate", lambda r: 1.0 if _truthy(r.get("target_50bps_hit")) else 0.0, "rate"),
            ("target_before_stop_25_25_rate", lambda r: 1.0 if r.get("target_before_stop_25_25") is True or r.get("target_before_stop_25_25") == "True" else (0.0 if r.get("target_before_stop_25_25") in (False, "False") else None), "rate"),
            ("positive_return_rate", lambda r: 1.0 if (_safe_float(r.get("return_bps")) or 0) > 0 else 0.0, "rate"),
        ):
            vals = []
            for r in items:
                v = getter(r)
                if v is None:
                    continue
                vals.append(float(v))
            if len(vals) < params.min_samples:
                continue
            est, lo, hi = bootstrap_ci(
                vals, iterations=params.bootstrap_iterations, seed=params.random_seed, statistic=stat
            )
            ci_rows.append(
                {
                    "group_type": "pattern_type",
                    "group_key": gkey,
                    "horizon_seconds": h,
                    "metric": metric,
                    "sample_count": len(vals),
                    "estimate": est,
                    "ci_low_95": lo,
                    "ci_high_95": hi,
                    "bootstrap_iterations": params.bootstrap_iterations,
                    "random_seed": params.random_seed,
                    "insufficient_sample": False,
                }
            )
    write_csv_headered(
        out_dir / "pattern_outcome_confidence_intervals.csv",
        ci_rows,
        [
            "group_type", "group_key", "horizon_seconds", "metric", "sample_count",
            "estimate", "ci_low_95", "ci_high_95", "bootstrap_iterations", "random_seed",
            "insufficient_sample",
        ],
    )

    # Segment stability vs baseline lift proxy using return sign per segment
    stability_rows = []
    compare_by_h = {
        int(r["horizon_seconds"]): r
        for r in baseline_compare
        if r.get("baseline_type") == "TIME_MATCHED_RANDOM" and r.get("pattern_group") == "ALL_DIRECTIONAL"
    }
    # fallback: any ALL_DIRECTIONAL
    for r in baseline_compare:
        h = int(r["horizon_seconds"])
        if h not in compare_by_h and r.get("pattern_group") == "ALL_DIRECTIONAL":
            compare_by_h[h] = r
    for s in by_type:
        h = int(s["horizon_seconds"])
        gkey = s["group_key"]
        if s.get("expected_direction") not in {DIRECTION_LONG, DIRECTION_SHORT}:
            continue
        items = [
            r for r in outcomes
            if str(r.get("pattern_type")) == gkey
            and int(r.get("horizon_seconds") or 0) == h
            and _truthy(r.get("forward_data_complete"))
        ]
        by_seg: dict[str, list] = defaultdict(list)
        for r in items:
            by_seg[str(r.get("segment_id"))].append(r)
        lifts = []
        pos = neg = 0
        for sid, rows_s in by_seg.items():
            rate = _rate([_truthy(r.get("target_25bps_hit")) for r in rows_s])
            base = (compare_by_h.get(h) or {}).get("baseline_target_25bps_hit_rate")
            if rate is None or base is None:
                continue
            lift = rate - base
            lifts.append(lift)
            if lift > 0:
                pos += 1
            elif lift < 0:
                neg += 1
        stability_rows.append(
            {
                "group_key": gkey,
                "horizon_seconds": h,
                "segments_with_samples": len(by_seg),
                "segments_positive_lift": pos,
                "segments_negative_lift": neg,
                "median_segment_target_25_lift": _median(lifts),
                "min_segment_target_25_lift": min(lifts) if lifts else None,
                "max_segment_target_25_lift": max(lifts) if lifts else None,
                "segment_consistency_rate": (pos / len(by_seg)) if by_seg else None,
                "single_segment_only": len(by_seg) <= 1,
            }
        )
    write_csv_headered(
        out_dir / "pattern_segment_stability.csv",
        stability_rows,
        [
            "group_key", "horizon_seconds", "segments_with_samples", "segments_positive_lift",
            "segments_negative_lift", "median_segment_target_25_lift", "min_segment_target_25_lift",
            "max_segment_target_25_lift", "segment_consistency_rate", "single_segment_only",
        ],
    )

    # Ranking
    stab_idx = {(r["group_key"], int(r["horizon_seconds"])): r for r in stability_rows}
    ranking = []
    for s in by_type:
        if s.get("insufficient_sample"):
            continue
        if int(s.get("segment_count") or 0) < 2:
            continue
        if s.get("expected_direction") not in {DIRECTION_LONG, DIRECTION_SHORT}:
            continue
        h = int(s["horizon_seconds"])
        st = stab_idx.get((s["group_key"], h), {})
        base = compare_by_h.get(h, {})
        row = {
            "group_type": "pattern_type",
            "group_key": s["group_key"],
            "horizon_seconds": h,
            "sample_count_complete": s["sample_count_complete"],
            "segment_count": s["segment_count"],
            "median_mfe_bps": s["median_mfe_bps"],
            "median_mae_bps": s["median_mae_bps"],
            "target_25bps_hit_rate": s.get("target_25bps_hit_rate"),
            "target_before_stop_25_25_rate": s.get("target_before_stop_25_25_rate"),
            "baseline_target_25_lift": base.get("target_25bps_hit_rate_lift"),
            "baseline_target_before_stop_lift": base.get("target_before_stop_lift"),
            "segment_consistency_rate": st.get("segment_consistency_rate"),
            "single_segment_only": st.get("single_segment_only"),
            "ci_low_target_25_hit_rate": None,
            "min_samples": params.min_samples,
        }
        for ci in ci_rows:
            if ci["group_key"] == s["group_key"] and int(ci["horizon_seconds"]) == h and ci["metric"] == "target_25bps_hit_rate":
                row["ci_low_target_25_hit_rate"] = ci["ci_low_95"]
        score = research_score(row)
        label, flags = label_from_score(row, score)
        row["research_score"] = score
        row["research_label"] = label
        row["warning_flags"] = flags
        # ban live labels
        assert label not in {"PROFITABLE", "GUARANTEED", "TRADING_SIGNAL", "READY_FOR_LIVE"}
        ranking.append(row)
    ranking.sort(key=lambda r: (-float(r["research_score"]), str(r["group_key"]), int(r["horizon_seconds"])))
    for i, r in enumerate(ranking, start=1):
        r["rank"] = i
    write_csv_headered(
        out_dir / "pattern_research_ranking.csv",
        ranking,
        [
            "rank", "group_type", "group_key", "horizon_seconds", "sample_count_complete",
            "segment_count", "median_mfe_bps", "median_mae_bps", "target_25bps_hit_rate",
            "target_before_stop_25_25_rate", "baseline_target_25_lift",
            "baseline_target_before_stop_lift", "segment_consistency_rate",
            "ci_low_target_25_hit_rate", "research_score", "research_label", "warning_flags",
        ],
    )

    integ = check_outcome_integrity(
        outcomes=outcomes,
        clusters=cluster_rows,
        eval_rows=eval_rows,
        segment_paths=segment_paths,
        forward_samples_processed=forward_samples_processed,
        horizons_seconds=params.horizons_seconds,
    )
    write_csv_headered(
        out_dir / "pattern_outcome_errors.csv",
        errors + [
            {"phase": "integrity", "error_type": "INTEGRITY", "error_message": e, "details": ""}
            for e in integ.get("errors") or []
        ],
        ["phase", "error_type", "error_message", "details"],
    )

    complete_n = sum(1 for r in outcomes if _truthy(r.get("forward_data_complete")))
    incomplete_n = len(outcomes) - complete_n
    dir_counts = defaultdict(int)
    for m in event_meta:
        dir_counts[str(m.get("expected_direction"))] += 1
    promising = sum(1 for r in ranking if r.get("research_label") == "PROMISING_FOR_OOS")
    groups_tested = len(by_type)
    groups_sufficient = sum(1 for s in by_type if not s.get("insufficient_sample"))
    gap_incomplete = incomplete_n > 0
    insufficient = complete_n == 0
    decision = decide_phase6(
        ok=bool(integ.get("ok")),
        gap_incomplete=gap_incomplete,
        warnings=bool(integ.get("warnings")) or bool(errors),
        insufficient=insufficient,
    )
    runtime = time.perf_counter() - t0
    events_n = len(event_meta)
    summary = {
        "phase6_version": PHASE6_VERSION,
        "decision": decision,
        "price_source": params.price_source,
        "horizons_seconds": list(params.horizons_seconds),
        "targets_bps": list(params.targets_bps),
        "stops_bps": list(params.stops_bps),
        "min_samples": params.min_samples,
        "bootstrap_iterations": params.bootstrap_iterations,
        "random_seed": params.random_seed,
        "pattern_outcome_event_count": events_n,
        "pattern_outcome_row_count": len(outcomes),
        "pattern_outcome_complete_count": complete_n,
        "pattern_outcome_incomplete_count": incomplete_n,
        "pattern_cluster_count": len(cluster_rows),
        "pattern_directional_event_count": dir_counts[DIRECTION_LONG] + dir_counts[DIRECTION_SHORT],
        "pattern_neutral_event_count": dir_counts[DIRECTION_NEUTRAL],
        "pattern_unknown_event_count": dir_counts[DIRECTION_UNKNOWN],
        "pattern_groups_tested": groups_tested,
        "pattern_groups_sufficient": groups_sufficient,
        "pattern_promising_for_oos_count": promising,
        "pattern_outcome_integrity_error_count": len(integ.get("errors") or []),
        "segments_processed": sum(1 for p in segment_paths.values() if p.points),
        "forward_samples_processed": forward_samples_processed,
        "runtime_sec": runtime,
        "events_per_second": (events_n / runtime) if runtime > 0 else None,
        "research_score_formula": (
            "2*tbs_lift + 1.5*hit25_lift + 0.02*mfe - 0.03*mae + 20*segment_consistency "
            "- 5 if n<50 - 20 if segments<2 - 15 if single_segment_only"
        ),
        "limitations": [
            "No fees, slippage, or execution model.",
            "No profitability or live-readiness claim.",
            "Unknown/neutral patterns excluded from directional hit rates.",
            "Same-bar high/low target/stop order may be AMBIGUOUS_SAME_BAR.",
            "Multiple testing: ranking is exploratory only.",
            "Forward samples strictly after event_time (never same unfinished bar).",
            "Rastered mid/bar paths: HORIZON_COMPLETE if last sample within "
            "coverage_tolerance of horizon_end (no exact stamp required).",
            "NO_FORWARD_DATA only when forward_sample_count==0; else "
            "INSUFFICIENT_SAMPLE_COVERAGE when coverage shortfall exceeds tolerance.",
        ],
    }
    (out_dir / "pattern_outcome_summary.json").write_bytes(
        orjson.dumps(summary, option=orjson.OPT_INDENT_2)
    )
    (out_dir / "pattern_outcome_integrity.json").write_bytes(
        orjson.dumps(integ, option=orjson.OPT_INDENT_2)
    )

    top = ranking[:10]
    weak = list(reversed(ranking[-10:])) if ranking else []
    report = [
        "# Pattern Outcome Report (Phase 6)",
        "",
        f"**Decision:** `{decision}`",
        "",
        "## 1. Datenbasis",
        f"- Events: {events_n}",
        f"- Outcome rows: {len(outcomes)}",
        f"- Clusters: {len(cluster_rows)}",
        f"- Complete/incomplete outcome rows: {complete_n} / {incomplete_n}",
        f"- Price source: `{params.price_source}`",
        f"- Horizons (s): {list(params.horizons_seconds)}",
        "",
        "## 2–4. Richtung / Familien",
        f"- LONG+SHORT events: {summary['pattern_directional_event_count']}",
        f"- NEUTRAL: {summary['pattern_neutral_event_count']}",
        f"- UNKNOWN: {summary['pattern_unknown_event_count']}",
        "",
        "## 5. Forward-Vollständigkeit",
        f"- End reasons dominated by incomplete={incomplete_n}",
        "",
        "## 7–8. Baseline / Top explorative Gruppen",
        f"- Baseline comparisons: {len(baseline_compare)}",
        f"- Promising_for_oos: {promising}",
        "",
        "### Top ranks",
    ]
    for r in top:
        report.append(
            f"- #{r['rank']} `{r['group_key']}` h={r['horizon_seconds']} "
            f"score={r['research_score']:.2f} label={r['research_label']}"
        )
    report += ["", "### Weakest ranks", ""]
    for r in weak:
        report.append(
            f"- #{r['rank']} `{r['group_key']}` h={r['horizon_seconds']} "
            f"score={r['research_score']:.2f} label={r['research_label']}"
        )
    report += [
        "",
        "## 9–10. Segment-Stabilität",
        f"- Stability groups: {len(stability_rows)}",
        f"- Single-segment-only groups: {sum(1 for r in stability_rows if r.get('single_segment_only'))}",
        "",
        "## 11. Higher-Low (P3–P11) separat",
        f"- HL armed events: {sum(1 for m in event_meta if m.get('source_family') == 'HIGHER_LOW_ARMED_ACTION')}",
        "",
        "## 12. Neutral / Unknown separat",
        f"- NEUTRAL events: {summary['pattern_neutral_event_count']}",
        f"- UNKNOWN events: {summary['pattern_unknown_event_count']}",
        "- Directional hit-rates are blank for NEUTRAL/UNKNOWN.",
        "",
        "## 13. Einschränkungen",
        "- Keine Fees/Slippage/Execution.",
        "- Keine PROFITABLE/LIVE Labels.",
        "- Exploratory ranking only; multiple-testing risk (viele Gruppen).",
        f"- Groups tested / sufficient: {groups_tested} / {groups_sufficient}.",
        "- Forward starts strictly after event_time / action_time.",
        "- Bar high/low usable only after bucket_end (completed bar).",
        "",
        "## 14. Kausalitätsaudit",
        f"- Integrity ok: {integ.get('ok')}",
        f"- Errors: {len(integ.get('errors') or [])}",
        "- forward_ts > event_time (strict); window event_time < ts <= horizon_end.",
        "- No cross-segment / gap forward-fill; gaps never covered by sampling tolerance.",
        "- Raster completeness: last sample within coverage_tolerance of horizon_end.",
        "- Same-bar target+stop → AMBIGUOUS_SAME_BAR (no target-first guess).",
        "- NO_FORWARD_DATA only if forward_sample_count==0.",
        "",
        f"## 15. Entscheidung: `{decision}`",
        "",
        "COMPLETE means technical completeness only — not a trading edge.",
        "",
        f"Runtime: {runtime:.3f}s; events/s: {summary.get('events_per_second')}",
        "",
    ]
    (out_dir / "PATTERN_OUTCOME_REPORT.md").write_text("\n".join(report), encoding="utf-8")

    result.decision = decision
    result.summary = summary
    result.integrity = integ
    result.errors = errors
    result.ok = bool(integ.get("ok"))
    result.runtime_sec = runtime
    return result
