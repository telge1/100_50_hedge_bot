"""Feature extraction for v2 (causal; time-normalized QDH AUC; depth baseline)."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from . import (
    ABSORPTION_RATIO_STATUS,
    CAUSAL_SNAPSHOT_S,
    PRE_TOUCH_S,
    VACUUM_SCORE_STATUS,
)


def _as_dt(x: Any) -> datetime:
    if isinstance(x, datetime):
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(x).replace("Z", "+00:00"))


def _finite(xs: list) -> list[float]:
    out = []
    for x in xs:
        if x is None:
            continue
        try:
            v = float(x)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            out.append(v)
    return out


def _median(xs: list[float]) -> float | None:
    ys = sorted(_finite(xs))
    if not ys:
        return None
    m = len(ys) // 2
    return ys[m] if len(ys) % 2 else 0.5 * (ys[m - 1] + ys[m])


def _mean(xs: list[float]) -> float | None:
    ys = _finite(xs)
    return sum(ys) / len(ys) if ys else None


def pre_touch_depth_baseline(flow_100ms: list[dict[str, Any]]) -> float | None:
    vals = [
        float(r["same_side_depth_2bps"])
        for r in flow_100ms
        if r.get("phase") == "PRE_TOUCH"
        and not r.get("post_decision")
        and r.get("same_side_depth_2bps") is not None
    ]
    return _median(vals)


def extract_features_v2(
    *,
    flow_100ms: list[dict[str, Any]],
    funnel: dict[str, Any] | None,
    touch_at: datetime,
    decision_at: datetime,
    trade_side: str,
    flow_meta: dict[str, Any],
    coverage: dict[str, Any],
) -> dict[str, Any]:
    causal = [r for r in flow_100ms if not r.get("post_decision")]
    touch_to_dec = [r for r in causal if r.get("phase") == "TOUCH_TO_TRIGGER"]
    horizon = max(float(flow_meta.get("decision_horizon_s") or (decision_at - touch_at).total_seconds()), 1e-9)

    def _sum(rows: list[dict[str, Any]], key: str) -> float:
        return sum(float(r.get(key) or 0) for r in rows)

    fill = _sum(causal, "attributed_fill")
    fill_raw = _sum(causal, "attributed_fill_raw")
    pull = _sum(causal, "residual_pull")
    refill = _sum(causal, "refill")
    unk = _sum(causal, "unknown")
    book_dec = _sum(causal, "book_decrease")
    net = _sum(causal, "net_depletion")
    excess = _sum(causal, "fill_excess_over_decrease")

    # QDH valid series only
    qdh_pts = []
    for r in causal:
        if not r.get("qdh_valid"):
            continue
        v = r.get("qdh_ewma")
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(fv):
            qdh_pts.append((_as_dt(r["bucket_start"]), fv, r.get("qdh_invalid_reason")))

    qdh_vals = [v for _, v, _ in qdh_pts]
    qdh_auc = None
    qdh_auc_per_decision_s = None
    qdh_auc_per_valid_s = None
    qdh_valid_seconds = 0.0
    if len(qdh_pts) >= 2:
        qdh_auc = 0.0
        for i in range(1, len(qdh_pts)):
            dt = (qdh_pts[i][0] - qdh_pts[i - 1][0]).total_seconds()
            if dt > 0:
                qdh_auc += 0.5 * (qdh_pts[i][1] + qdh_pts[i - 1][1]) * dt
                qdh_valid_seconds += dt
        if horizon > 0:
            qdh_auc_per_decision_s = qdh_auc / horizon
        if qdh_valid_seconds > 0:
            qdh_auc_per_valid_s = qdh_auc / qdh_valid_seconds
    elif len(qdh_pts) == 1:
        qdh_valid_seconds = 0.1  # single 100ms observation; AUC undefined
    qdh_valid_fraction = (qdh_valid_seconds / horizon) if horizon > 0 else None

    # slope over valid series
    qdh_slope = None
    if len(qdh_pts) >= 2 and qdh_valid_seconds > 0:
        qdh_slope = (qdh_pts[-1][1] - qdh_pts[0][1]) / qdh_valid_seconds

    def at_or_before(ts: datetime) -> dict[str, Any] | None:
        chosen = None
        for r in causal:
            avail = _as_dt(r.get("bucket_available_at") or r["bucket_end"])
            if avail <= ts:
                chosen = r
        return chosen

    r_touch = at_or_before(touch_at)
    r_dec = at_or_before(decision_at)

    def qdh_field(row: dict[str, Any] | None, key: str = "qdh_ewma"):
        if row is None:
            return None, "NO_BUCKET"
        if not row.get("qdh_valid"):
            return None, row.get("qdh_invalid_reason") or "QDH_INVALID"
        return row.get(key), None

    qdh_touch, qdh_touch_na = qdh_field(r_touch)
    qdh_dec, qdh_dec_na = qdh_field(r_dec)

    # persistence at decision — NA when too few attributed trades (do not fake weak=0)
    trade_n = funnel.get("attributed_trade_count")
    if trade_n is None:
        trade_n = funnel.get("total_unique_trade_count")
    try:
        trade_n_i = int(trade_n) if trade_n is not None else 0
    except (TypeError, ValueError):
        trade_n_i = 0
    pers_dec = (r_dec or {}).get("persistence_ratio")
    pers_status = "OK"
    if trade_n_i < 2:
        pers_dec = None
        pers_status = "PERSISTENCE_NOT_AVAILABLE"

    # IE: never keep epsilon-inflated values; require positive hit notional
    ie_dec = (r_dec or {}).get("impact_efficiency_bps_per_million")
    ie_status = (r_dec or {}).get("impact_efficiency_status") or "OK"
    hit_notional = (r_dec or {}).get("attributed_hit_notional_usdt")
    progress_dec = (r_dec or {}).get("progress_bps")
    try:
        hit_n_f = float(hit_notional) if hit_notional is not None else None
    except (TypeError, ValueError):
        hit_n_f = None
    if hit_n_f is None or hit_n_f <= 0.0:
        ie_dec = None
        ie_status = "NOT_AVAILABLE"
    elif ie_dec is not None:
        try:
            ie_f = float(ie_dec)
            if (not math.isfinite(ie_f)) or abs(ie_f) > 1e9:
                ie_dec = None
                ie_status = "NOT_AVAILABLE_EXTREME"
        except (TypeError, ValueError):
            ie_dec = None
            ie_status = "NOT_AVAILABLE"

    qdh_pers_adj = None
    if qdh_dec is not None and pers_dec is not None:
        try:
            qdh_pers_adj = float(qdh_dec) * float(pers_dec)
        except (TypeError, ValueError):
            qdh_pers_adj = None

    baseline = pre_touch_depth_baseline(flow_100ms)
    depth_dec = (r_dec or {}).get("same_side_depth_2bps")
    depth_touch = (r_touch or {}).get("same_side_depth_2bps")
    depth_norm_dec = None
    depth_norm_touch = None
    if baseline is not None and baseline > 1e-12 and depth_dec is not None:
        depth_norm_dec = float(depth_dec) / baseline - 1.0
    if baseline is not None and baseline > 1e-12 and depth_touch is not None:
        depth_norm_touch = float(depth_touch) / baseline - 1.0

    long = str(trade_side).upper() == "LONG"
    mid_touch = (r_touch or {}).get("mid")
    mid_dec = (r_dec or {}).get("mid")
    mid_change = None
    if mid_touch is not None and mid_dec is not None:
        mid_change = float(mid_dec) - float(mid_touch)
    favorable = None if mid_change is None else (mid_change if long else -mid_change)

    snaps = {}
    for s in CAUSAL_SNAPSHOT_S:
        ts = touch_at.timestamp() + s
        if ts > decision_at.timestamp() + 1e-9:
            snaps[f"snapshot_{s}s"] = "NOT_AVAILABLE_AT_DECISION"
            continue
        chosen = None
        for r in causal:
            avail = _as_dt(r.get("bucket_available_at") or r["bucket_end"])
            if avail.timestamp() <= ts:
                chosen = r
        if chosen is None:
            snaps[f"snapshot_{s}s"] = "NOT_AVAILABLE_AT_DECISION"
        else:
            qv, reason = qdh_field(chosen)
            snaps[f"snapshot_{s}s_qdh"] = qv
            snaps[f"snapshot_{s}s_qdh_na_reason"] = reason
            snaps[f"snapshot_{s}s_persistence"] = chosen.get("persistence_ratio")

    funnel = funnel or {}
    return {
        "unique_trade_count": funnel.get("total_unique_trade_count"),
        "eligible_in_band_trade_qty": funnel.get("in_band_trade_qty"),
        "correct_aggressor_trade_qty": funnel.get("correct_aggressor_trade_qty"),
        "attributed_fill_qty": fill,
        "attributed_fill_qty_raw": fill_raw,
        "fill_excess_over_decrease": excess,
        "residual_pull_qty": pull,
        "refill_qty": refill,
        "unknown_qty": unk,
        "book_decrease_qty": book_dec,
        "net_depletion_qty": net,
        "fill_share_of_book_decrease": (fill / book_dec) if book_dec > 1e-12 else None,
        "pull_share_of_book_decrease": (pull / book_dec) if book_dec > 1e-12 else None,
        "unknown_share_of_book_decrease": (unk / book_dec) if book_dec > 1e-12 else None,
        "queue_at_touch": flow_meta.get("queue_at_touch"),
        "linkage_present_flag": None,  # set by caller
        "qdh_at_touch": qdh_touch,
        "qdh_at_touch_na_reason": qdh_touch_na,
        "qdh_at_decision": qdh_dec,
        "qdh_at_decision_na_reason": qdh_dec_na,
        "qdh_mean": _mean(qdh_vals),
        "qdh_median": _median(qdh_vals),
        "qdh_max": max(qdh_vals) if qdh_vals else None,
        "qdh_min": min(qdh_vals) if qdh_vals else None,
        "qdh_auc": qdh_auc,
        "qdh_auc_raw": qdh_auc,
        "qdh_auc_per_decision_second": qdh_auc_per_decision_s,
        "qdh_auc_per_valid_second": qdh_auc_per_valid_s,
        "qdh_valid_seconds": qdh_valid_seconds if qdh_vals else 0.0,
        "qdh_valid_fraction": qdh_valid_fraction,
        "qdh_slope_per_second": qdh_slope,
        "decision_horizon_s": horizon,
        "seconds_qdh_valid": qdh_valid_seconds if qdh_vals else 0.0,
        "persistence_ratio_at_decision": pers_dec,
        "persistence_status": pers_status,
        "qdh_toxic_at_decision": (r_dec or {}).get("qdh_toxic_base_only"),
        "qdh_persistence_adjusted_research": qdh_pers_adj,
        "impact_efficiency_bps_per_million": ie_dec,
        "impact_efficiency_status": ie_status,
        "attributed_hit_notional_usdt": hit_n_f,
        "progress_bps_attack_direction": progress_dec,
        "absorption_ratio": None,
        "absorption_ratio_status": ABSORPTION_RATIO_STATUS,
        "vacuum_score": None,
        "vacuum_score_status": VACUUM_SCORE_STATUS,
        "same_side_depth_2bps_at_touch_raw": depth_touch,
        "same_side_depth_2bps_at_decision_raw": depth_dec,
        "same_side_depth_2bps_pre_touch_baseline": baseline,
        "same_side_depth_2bps_at_touch_norm": depth_norm_touch,
        "same_side_depth_2bps_at_decision_norm": depth_norm_dec,
        "depth_unit": "USDT_notional_within_near_bps",
        "depth_baseline_window_s": PRE_TOUCH_S,
        "mid_at_touch": mid_touch,
        "mid_at_decision": mid_dec,
        "mid_change": mid_change,
        "mid_change_favorable_signed": favorable,
        "coverage_pass": coverage.get("pass"),
        "coverage_blockers": "|".join(coverage.get("blockers") or []),
        "feature_available_at": decision_at.isoformat().replace("+00:00", "Z"),
        "decision_cutoff": decision_at.isoformat().replace("+00:00", "Z"),
        "causal_valid": True,
        "post_decision_forensic": False,
        **snaps,
    }
