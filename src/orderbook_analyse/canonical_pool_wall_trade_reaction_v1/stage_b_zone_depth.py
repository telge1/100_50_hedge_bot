"""Stage B V2 — decide from aggregate zone liquidity (not single wall).

CLEAR_POOL_SELECTION_RULE_V1 Stage B language:
  ZONE_HELD   — zone liquidity holds / refreshes while mid tests
  ZONE_EATEN  — zone liquidity shrinks with aggressive trades into zone
  ZONE_PULLED — zone liquidity vanishes with little trade notional
  ZONE_UNKNOWN — insufficient book/trade data
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.aggressor_efficiency_flip.trade_loader import load_trades_clickhouse
from orderbook_analyse.canonical_pool_wall_trade_reaction_v1.selection_rule_v1 import (
    RULE_ID,
    STAGE_B_LABELS,
)
from orderbook_analyse.canonical_pool_wall_trade_reaction_v1.stage_a import (
    zone_fill_from_levels,
)
from orderbook_analyse.liquidity_pool_six_case_wall_trade_reaction_sample_v1 import (
    PRE_START_S,
)
from orderbook_analyse.liquidity_pool_six_case_wall_trade_reaction_sample_v1.audit_case import (
    iter_ob_1s,
)

STAGE_B_VERSION = "zone_depth_v2_contact"
POST_DECISION_S = 90
POST_CHART_S = 300

# V1 thresholds (selection only — change → bump version)
DROP_HELD_MAX = 0.35  # end retains >=65% of touch notional → hold path
DROP_MATERIAL = 0.50  # need ≥50% drop for eat/pull
TRADE_COVER_EATEN = 0.40  # trades cover ≥40% of notional drop
TRADE_COVER_PULLED_MAX = 0.15  # trades cover <15% of drop → pulled
MIN_ZONE_NOTIONAL_AT_TOUCH = 1_000.0  # USD; below → unknown
MIN_TRADE_ROWS = 3
MIN_CONTACT_S = 5  # need enough in-band seconds for a zone decision
NEAR_FRONT_FRAC = 0.25  # contact if within 25% zone-height of front (outside band)


def _utc(ts: str | datetime | pd.Timestamp) -> datetime:
    if isinstance(ts, pd.Timestamp):
        ts = ts.to_pydatetime()
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def _front_back(side: str, lower: float, upper: float) -> tuple[float, float]:
    if str(side).upper() == "BID":
        return float(upper), float(lower)
    return float(lower), float(upper)


def _in_contact(mid: float, *, side: str, lower: float, upper: float) -> bool:
    """True when mid is inside the band or near the front (OB200 still sees the zone)."""
    lo, hi = float(lower), float(upper)
    if lo <= mid <= hi:
        return True
    zh = max(hi - lo, 1e-9)
    front, _back = _front_back(side, lo, hi)
    return abs(mid - front) <= NEAR_FRONT_FRAC * zh


def classify_zone_depth(
    *,
    zone_n0: float,
    zone_n_end: float,
    zone_n_min: float,
    zone_n_max_post: float,
    trade_into: float,
    mid_reclaimed: bool,
    mid_accepted_beyond: bool,
    contact_seconds: int,
) -> tuple[str, str]:
    """Return (zone_label, reason). Metrics must be contact-window only."""
    if contact_seconds < MIN_CONTACT_S:
        return "ZONE_UNKNOWN", "insufficient_contact_seconds"
    if zone_n0 is None or zone_n0 < MIN_ZONE_NOTIONAL_AT_TOUCH:
        return "ZONE_UNKNOWN", "zone_notional_at_touch_too_small"

    drop_end = max(0.0, zone_n0 - zone_n_end)
    drop_min = max(0.0, zone_n0 - zone_n_min)
    drop = max(drop_end, drop_min)
    drop_frac_end = drop_end / zone_n0 if zone_n0 > 0 else 0.0
    drop_frac = drop / zone_n0 if zone_n0 > 0 else 0.0
    cover = (trade_into / drop) if drop > 0 else 0.0
    refreshed = zone_n_max_post >= zone_n0 * 0.9 and zone_n_end >= zone_n0 * 0.7

    # Hold / refresh path (while still in contact)
    if drop_frac_end <= DROP_HELD_MAX or refreshed:
        if mid_accepted_beyond and drop_frac >= DROP_MATERIAL and cover >= TRADE_COVER_EATEN:
            return "ZONE_EATEN", "accepted_beyond_with_trade_covered_drop"
        return "ZONE_HELD", "zone_retained_or_refreshed_in_contact"

    # Material depletion while mid still sees the zone
    if drop_frac >= DROP_MATERIAL:
        if cover >= TRADE_COVER_EATEN:
            return "ZONE_EATEN", "zone_drop_trade_covered_in_contact"
        if cover <= TRADE_COVER_PULLED_MAX:
            return "ZONE_PULLED", "zone_drop_little_trade_in_contact"
        if mid_accepted_beyond:
            return "ZONE_EATEN", "mixed_cover_but_accepted_beyond"
        if mid_reclaimed:
            return "ZONE_HELD", "mixed_cover_but_mid_reclaimed"
        return "ZONE_UNKNOWN", "mixed_drop_ambiguous_cover"

    if mid_accepted_beyond:
        return "ZONE_EATEN", "mild_drop_but_accepted_beyond"
    if mid_reclaimed:
        return "ZONE_HELD", "mild_drop_mid_reclaimed"
    return "ZONE_UNKNOWN", "mild_drop_no_clear_price_cue"


def audit_zone_depth_case(
    row: dict[str, Any],
    *,
    case_id: str,
    raw_root: Path,
    decision_s: int = POST_DECISION_S,
    chart_s: int = POST_CHART_S,
) -> dict[str, Any]:
    side = str(row["side"]).upper()
    lo = float(row["lower"])
    hi = float(row["upper"])
    touch = _utc(row["first_touch_ts"])
    front, back = _front_back(side, lo, hi)
    load_start = touch - timedelta(seconds=PRE_START_S)
    chart_end = touch + timedelta(seconds=chart_s)
    decision_end = touch + timedelta(seconds=decision_s)

    timeline: list[dict[str, Any]] = []
    for bucket, genuine, bb, ba, mid, bids, asks in iter_ob_1s(raw_root, load_start, chart_end):
        levels = asks if side == "ASK" else bids
        fill = zone_fill_from_levels(levels, lower=lo, upper=hi)
        ts = datetime.fromtimestamp(bucket / 1000.0, tz=timezone.utc)
        phase = "PRE" if ts < touch else ("DECISION" if ts <= decision_end else "POST")
        timeline.append(
            {
                "case_id": case_id,
                "second": _iso(ts),
                "second_ms": int(bucket),
                "coverage": "COMPLETE" if genuine else "SOURCE_GAP",
                "mid": float(mid),
                "best_bid": float(bb),
                "best_ask": float(ba),
                "zone_level_count": fill["zone_level_count"],
                "zone_qty": fill["zone_qty"],
                "zone_notional": fill["zone_notional"],
                "phase": phase,
            }
        )

    if not timeline:
        return {
            "summary": {
                "case_id": case_id,
                "pool_id": row["pool_id"],
                "timeframe": row.get("timeframe"),
                "side": side,
                "cluster_start_ts": _iso(touch),
                "component_lower_edge": lo,
                "component_upper_edge": hi,
                "zone_label": "ZONE_UNKNOWN",
                "label_reason": "no_raw_book",
                "evidence_class": "INSUFFICIENT_DATA",
                "stage_b_version": STAGE_B_VERSION,
                "select_reason": RULE_ID,
            },
            "timeline": [],
        }

    tdf = pd.DataFrame(timeline)
    tdf["ts"] = pd.to_datetime(tdf["second"], utc=True)
    dec = tdf[(tdf["ts"] >= touch) & (tdf["ts"] <= decision_end) & (tdf["coverage"] == "COMPLETE")].copy()
    if dec.empty:
        return {
            "summary": {
                "case_id": case_id,
                "pool_id": row["pool_id"],
                "timeframe": row.get("timeframe"),
                "side": side,
                "cluster_start_ts": _iso(touch),
                "component_lower_edge": lo,
                "component_upper_edge": hi,
                "zone_label": "ZONE_UNKNOWN",
                "label_reason": "no_decision_window_book",
                "evidence_class": "INSUFFICIENT_DATA",
                "stage_b_version": STAGE_B_VERSION,
                "select_reason": RULE_ID,
            },
            "timeline": timeline,
        }

    dec["in_contact"] = [
        _in_contact(float(m), side=side, lower=lo, upper=hi) for m in dec["mid"].tolist()
    ]
    contact = dec[dec["in_contact"]]
    contact_seconds = int(len(contact))

    # Seed from first contact second (or first decision second if no contact flag yet)
    seed = contact.head(1) if not contact.empty else dec.head(1)
    zone_n0 = float(seed.iloc[0]["zone_notional"])
    zone_l0 = int(seed.iloc[0]["zone_level_count"])

    if contact_seconds > 0:
        zone_n_end = float(contact.iloc[-1]["zone_notional"])
        zone_n_min = float(contact["zone_notional"].min())
        zone_n_max_post = float(contact["zone_notional"].max())
        mid_series = contact["mid"].astype(float)
    else:
        zone_n_end = float(dec.iloc[-1]["zone_notional"])
        zone_n_min = float(dec["zone_notional"].min())
        zone_n_max_post = float(dec["zone_notional"].max())
        mid_series = dec["mid"].astype(float)

    if side == "BID":
        mid_reclaimed = bool((mid_series > front).any() and float(dec["mid"].iloc[-1]) > front)
        mid_accepted_beyond = bool((dec["mid"] < back).sum() >= 5)
    else:
        mid_reclaimed = bool((mid_series < front).any() and float(dec["mid"].iloc[-1]) < front)
        mid_accepted_beyond = bool((dec["mid"] > back).sum() >= 5)

    # Trades into the zone band (aggressor side) during decision window
    trades, _pre = load_trades_clickhouse(symbol="BTCUSDT", start=touch, end=decision_end)
    agg_side = "Sell" if side == "BID" else "Buy"
    into = 0.0
    into_n = 0
    for t in trades:
        if t.side != agg_side:
            continue
        if lo <= t.price <= hi:
            into += float(t.notional)
            into_n += 1

    label, reason = classify_zone_depth(
        zone_n0=zone_n0,
        zone_n_end=zone_n_end,
        zone_n_min=zone_n_min,
        zone_n_max_post=zone_n_max_post,
        trade_into=into,
        mid_reclaimed=mid_reclaimed,
        mid_accepted_beyond=mid_accepted_beyond,
        contact_seconds=contact_seconds,
    )
    if label not in STAGE_B_LABELS:
        label = "ZONE_UNKNOWN"

    drop = max(0.0, zone_n0 - min(zone_n_end, zone_n_min))
    cover = (into / drop) if drop > 0 else 0.0

    summary = {
        "case_id": case_id,
        "pool_id": row["pool_id"],
        "timeframe": row.get("timeframe"),
        "side": side,
        "cluster_start_ts": _iso(touch),
        "component_lower_edge": lo,
        "component_upper_edge": hi,
        "member_pool_count": int(row.get("maximum_P") or 0),
        "zone_label": label,
        "label_reason": reason,
        "evidence_class": reason,
        "stage_b_version": STAGE_B_VERSION,
        "select_reason": RULE_ID,
        "decision_window_s": decision_s,
        "contact_seconds": contact_seconds,
        "zone_level_count_at_touch": zone_l0,
        "zone_notional_at_touch": zone_n0,
        "zone_notional_end": zone_n_end,
        "zone_notional_min": zone_n_min,
        "zone_notional_max_post": zone_n_max_post,
        "zone_drop_frac_end": (zone_n0 - zone_n_end) / zone_n0 if zone_n0 else None,
        "zone_drop_frac_min": (zone_n0 - zone_n_min) / zone_n0 if zone_n0 else None,
        "trade_into_zone_notional": into,
        "trade_into_zone_count": into_n,
        "trade_cover_of_drop": cover,
        "mid_reclaimed": mid_reclaimed,
        "mid_accepted_beyond": mid_accepted_beyond,
        "mid_end": float(dec["mid"].iloc[-1]),
        "a7_zone_level_count": row.get("a7_zone_level_count"),
        "a7_zone_notional": row.get("a7_zone_notional"),
        "reaction_1s_prior": row.get("reaction"),
        "wall_in_pool_1s_proxy": row.get("wall_in_pool_1s_proxy") or row.get("wall_in_pool"),
        "metrics_scope": "in_contact_only",
    }
    # annotate timeline with contact flag for charts
    touch_ms = int(touch.timestamp() * 1000)
    end_ms = int(decision_end.timestamp() * 1000)
    for rec in timeline:
        if touch_ms <= rec["second_ms"] <= end_ms:
            rec["in_contact"] = _in_contact(
                float(rec["mid"]), side=side, lower=lo, upper=hi
            )
        else:
            rec["in_contact"] = False
    return {"summary": summary, "timeline": timeline}


def run_stage_b_zone_depth(
    candidates: pd.DataFrame,
    *,
    raw_root: Path,
    decision_s: int = POST_DECISION_S,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    summaries: list[dict[str, Any]] = []
    timelines: list[dict[str, Any]] = []
    for i, row in enumerate(candidates.to_dict(orient="records"), start=1):
        case_id = f"ZD_{i:03d}"
        print(f"stage_b_zone {case_id} {row['pool_id']} {row['first_touch_ts']}…", flush=True)
        res = audit_zone_depth_case(
            row, case_id=case_id, raw_root=raw_root, decision_s=decision_s
        )
        summaries.append(res["summary"])
        timelines.extend(res["timeline"])
    return summaries, timelines
