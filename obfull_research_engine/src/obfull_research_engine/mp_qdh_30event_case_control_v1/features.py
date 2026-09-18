"""Extract causal flow / QDH / microprice features from audit flow series."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from . import CAUSAL_SNAPSHOT_S


def _as_dt(x: Any) -> datetime:
    if isinstance(x, datetime):
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    s = str(x).replace("Z", "+00:00")
    return datetime.fromisoformat(s)


def _finite(xs: list[float]) -> list[float]:
    return [x for x in xs if x is not None and isinstance(x, (int, float)) and math.isfinite(float(x))]


def _median(xs: list[float]) -> float | None:
    ys = sorted(_finite(xs))
    if not ys:
        return None
    m = len(ys) // 2
    return ys[m] if len(ys) % 2 else 0.5 * (ys[m - 1] + ys[m])


def _mean(xs: list[float]) -> float | None:
    ys = _finite(xs)
    return sum(ys) / len(ys) if ys else None


def _std(xs: list[float]) -> float | None:
    ys = _finite(xs)
    if len(ys) < 2:
        return None
    mu = sum(ys) / len(ys)
    return math.sqrt(sum((x - mu) ** 2 for x in ys) / (len(ys) - 1))


def _slope(xs: list[tuple[float, float]]) -> float | None:
    """Simple OLS slope of y vs t (seconds)."""
    if len(xs) < 2:
        return None
    ts = [t for t, _ in xs]
    ys = [y for _, y in xs]
    t0 = ts[0]
    t = [x - t0 for x in ts]
    n = len(t)
    mt = sum(t) / n
    my = sum(ys) / n
    den = sum((a - mt) ** 2 for a in t)
    if den <= 1e-18:
        return 0.0
    return sum((a - mt) * (b - my) for a, b in zip(t, ys)) / den


NA = "NOT_AVAILABLE"


def extract_flow_features(
    *,
    flow_100ms: list[dict[str, Any]],
    funnel: dict[str, Any] | None,
    touch_at: datetime,
    decision_at: datetime,
    trade_side: str,
) -> dict[str, Any]:
    """Causal features: PRE_TOUCH + TOUCH_TO_TRIGGER only (exclude post_decision)."""
    causal = [r for r in flow_100ms if not r.get("post_decision")]
    touch_to_dec = [r for r in causal if r.get("phase") == "TOUCH_TO_TRIGGER"]
    pre = [r for r in causal if r.get("phase") == "PRE_TOUCH"]

    def _sum(rows: list[dict[str, Any]], key: str) -> float:
        return sum(float(r.get(key) or 0) for r in rows)

    fill = _sum(causal, "attributed_fill")
    pull = _sum(causal, "residual_pull")
    refill = _sum(causal, "refill")
    book_dec = _sum(causal, "book_decrease")
    net = _sum(causal, "net_depletion")
    dur = 0.0
    if causal:
        dur = max(
            (_as_dt(causal[-1]["bucket_end"]) - _as_dt(causal[0]["bucket_start"])).total_seconds(),
            1e-9,
        )

    # Sequence timestamps (first positive)
    def first_pos(key: str) -> datetime | None:
        for r in causal:
            if float(r.get(key) or 0) > 0:
                return _as_dt(r["bucket_start"])
        return None

    def max_rate_time(key: str) -> datetime | None:
        best_t = None
        best_v = -1.0
        for r in causal:
            v = float(r.get(key) or 0)
            if v > best_v:
                best_v = v
                best_t = _as_dt(r["bucket_start"])
        return best_t if best_v > 0 else None

    t_fill = first_pos("attributed_fill")
    t_pull = first_pos("residual_pull")
    t_refill = first_pos("refill")
    t_max_fill = max_rate_time("attributed_fill")
    t_max_pull = max_rate_time("residual_pull")
    t_max_refill = max_rate_time("refill")

    # Queue min
    q_rows = [( _as_dt(r["bucket_start"]), r.get("queue_end")) for r in causal if r.get("queue_end") is not None]
    q_min_t = None
    q_min_v = None
    q0 = None
    if q_rows:
        q0 = float(q_rows[0][1])
        for t, q in q_rows:
            qf = float(q)
            if q_min_v is None or qf < q_min_v:
                q_min_v = qf
                q_min_t = t
    q_end = float(q_rows[-1][1]) if q_rows else None

    def recovery_after(seconds: float) -> float | None:
        if q_min_t is None or q_min_v is None or q0 is None or q0 <= 1e-12:
            return None
        target = q_min_t.timestamp() + seconds
        after = [float(q) for t, q in q_rows if t.timestamp() >= target]
        if not after:
            return None
        return after[0] / q0

    # QDH trajectory causal
    qdh_pts = []
    for r in causal:
        v = r.get("qdh_ewma")
        if v is None:
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(fv):
            qdh_pts.append((_as_dt(r["bucket_start"]), fv))

    qdh_vals = [v for _, v in qdh_pts]
    qdh_peak_t = None
    qdh_max = None
    if qdh_pts:
        qdh_peak_t, qdh_max = max(qdh_pts, key=lambda x: x[1])

    exhausted = [r for r in causal if r.get("queue_exhausted")]
    t_exh = _as_dt(exhausted[0]["bucket_start"]) if exhausted else None

    # Snapshots
    snaps: dict[str, Any] = {}
    for s in CAUSAL_SNAPSHOT_S:
        ts = touch_at.timestamp() + s
        if ts > decision_at.timestamp() + 1e-9:
            snaps[f"snapshot_{s}s"] = "NOT_AVAILABLE_AT_DECISION"
            continue
        # last causal row with available_at <= snapshot
        chosen = None
        for r in causal:
            avail = _as_dt(r.get("bucket_available_at") or r["bucket_end"])
            if avail.timestamp() <= ts:
                chosen = r
        if chosen is None:
            snaps[f"snapshot_{s}s"] = "NOT_AVAILABLE_AT_DECISION"
        else:
            snaps[f"snapshot_{s}s_qdh"] = chosen.get("qdh_ewma")
            snaps[f"snapshot_{s}s_queue"] = chosen.get("queue_end")
            snaps[f"snapshot_{s}s_cum_fill"] = chosen.get("cumulative_fill")
            snaps[f"snapshot_{s}s_cum_pull"] = chosen.get("cumulative_pull")

    # Microprice / mid at touch & decision
    def at_or_before(ts: datetime) -> dict[str, Any] | None:
        chosen = None
        for r in causal:
            avail = _as_dt(r.get("bucket_available_at") or r["bucket_end"])
            if avail <= ts:
                chosen = r
        return chosen

    r_touch = at_or_before(touch_at)
    r_dec = at_or_before(decision_at)
    mid_touch = r_touch.get("mid") if r_touch else None
    mid_dec = r_dec.get("mid") if r_dec else None
    micro_touch = r_touch.get("microprice") if r_touch else None
    micro_dec = r_dec.get("microprice") if r_dec else None

    long = str(trade_side).upper() == "LONG"

    def signed_move(delta: float | None) -> float | None:
        if delta is None:
            return None
        return float(delta) if long else -float(delta)

    mid_change = None
    if mid_touch is not None and mid_dec is not None:
        mid_change = float(mid_dec) - float(mid_touch)
    micro_change = None
    if micro_touch is not None and micro_dec is not None:
        micro_change = float(micro_dec) - float(micro_touch)

    favorable = signed_move(mid_change)
    adverse = None if favorable is None else -favorable

    pull_refill_peak_gap = None
    if t_max_pull and t_max_refill:
        pull_refill_peak_gap = (t_max_refill - t_max_pull).total_seconds()
    fill_qmin_gap = None
    if t_max_fill and q_min_t:
        fill_qmin_gap = (q_min_t - t_max_fill).total_seconds()

    # net depletion before/after refill peak
    net_before_refill_peak = net_after_refill_peak = None
    if t_max_refill:
        net_before_refill_peak = sum(
            float(r.get("net_depletion") or 0)
            for r in causal
            if _as_dt(r["bucket_start"]) <= t_max_refill
        )
        net_after_refill_peak = sum(
            float(r.get("net_depletion") or 0)
            for r in causal
            if _as_dt(r["bucket_start"]) > t_max_refill
        )

    trade_sizes = []
    # funnel may not have per-trade; leave median/max from funnel if present
    funnel = funnel or {}

    out: dict[str, Any] = {
        "unique_trade_count": funnel.get("total_unique_trade_count"),
        "eligible_in_band_trade_count": funnel.get("in_band_trade_count"),
        "eligible_in_band_trade_qty": funnel.get("in_band_trade_qty"),
        "correct_aggressor_trade_count": funnel.get("correct_aggressor_trade_count"),
        "correct_aggressor_trade_qty": funnel.get("correct_aggressor_trade_qty"),
        "attributed_trade_count": funnel.get("attributed_trade_count"),
        "attributed_fill_qty": fill,
        "fill_share_of_book_decrease": (fill / book_dec) if book_dec > 1e-12 else None,
        "fill_rate_qty_per_second": fill / dur,
        "aggressive_trade_pace": (funnel.get("correct_aggressor_trade_count") or 0) / dur,
        "median_trade_size": funnel.get("median_attributed_trade_size"),  # may be None
        "max_trade_size": funnel.get("max_attributed_trade_size"),
        "book_decrease_qty": book_dec,
        "residual_pull_qty": pull,
        "refill_qty": refill,
        "pull_rate_qty_per_second": pull / dur,
        "refill_rate_qty_per_second": refill / dur,
        "net_depletion_qty": net,
        "cumulative_pull": pull,
        "cumulative_refill": refill,
        "cumulative_net_depletion": net,
        "gross_book_churn": pull + refill,
        "churn_ratio": ((pull + refill) / abs(net)) if net and abs(net) > 1e-12 else None,
        "pull_share_of_book_decrease": (pull / book_dec) if book_dec > 1e-12 else None,
        "refill_to_pull_ratio": (refill / pull) if pull > 1e-12 else None,
        "queue_recovery_fraction": (q_end / q0) if q0 and q0 > 1e-12 and q_end is not None else None,
        "queue_min_fraction": (q_min_v / q0) if q0 and q0 > 1e-12 and q_min_v is not None else None,
        "queue_end_fraction": (q_end / q0) if q0 and q0 > 1e-12 and q_end is not None else None,
        "timestamp_first_material_fill": t_fill.isoformat().replace("+00:00", "Z") if t_fill else None,
        "timestamp_first_pull": t_pull.isoformat().replace("+00:00", "Z") if t_pull else None,
        "timestamp_first_refill": t_refill.isoformat().replace("+00:00", "Z") if t_refill else None,
        "timestamp_max_fill_rate": t_max_fill.isoformat().replace("+00:00", "Z") if t_max_fill else None,
        "timestamp_max_pull_rate": t_max_pull.isoformat().replace("+00:00", "Z") if t_max_pull else None,
        "timestamp_max_refill_rate": t_max_refill.isoformat().replace("+00:00", "Z") if t_max_refill else None,
        "fill_before_pull": (t_fill < t_pull) if t_fill and t_pull else None,
        "pull_before_refill": (t_pull < t_refill) if t_pull and t_refill else None,
        "refill_before_pull": (t_refill < t_pull) if t_pull and t_refill else None,
        "seconds_pull_peak_to_refill_peak": pull_refill_peak_gap,
        "seconds_fill_peak_to_queue_min": fill_qmin_gap,
        "queue_recovery_5s_after_min": recovery_after(5),
        "queue_recovery_15s_after_min": recovery_after(15),
        "queue_recovery_30s_after_min": recovery_after(30),
        "net_depletion_before_refill_peak": net_before_refill_peak,
        "net_depletion_after_refill_peak": net_after_refill_peak,
        "qdh_at_touch": (r_touch or {}).get("qdh_ewma"),
        "qdh_at_decision": (r_dec or {}).get("qdh_ewma"),
        "qdh_mean": _mean(qdh_vals),
        "qdh_median": _median(qdh_vals),
        "qdh_max": qdh_max,
        "qdh_min": min(qdh_vals) if qdh_vals else None,
        "qdh_std": _std(qdh_vals),
        "qdh_slope": _slope([(t.timestamp(), v) for t, v in qdh_pts]),
        "qdh_auc": None,
        "seconds_qdh_valid": len(qdh_vals) * 0.1,
        "qdh_peak_time_relative_to_touch": (
            (qdh_peak_t - touch_at).total_seconds() if qdh_peak_t else None
        ),
        "qdh_peak_before_or_after_queue_min": (
            None
            if qdh_peak_t is None or q_min_t is None
            else ("BEFORE" if qdh_peak_t <= q_min_t else "AFTER")
        ),
        "queue_exhausted": bool(exhausted),
        "timestamp_queue_exhausted": t_exh.isoformat().replace("+00:00", "Z") if t_exh else None,
        "mid_at_touch": mid_touch,
        "mid_at_decision": mid_dec,
        "microprice_at_touch": micro_touch,
        "microprice_at_decision": micro_dec,
        "microprice_minus_mid_at_touch": (
            float(micro_touch) - float(mid_touch)
            if micro_touch is not None and mid_touch is not None
            else None
        ),
        "microprice_minus_mid_at_decision": (
            float(micro_dec) - float(mid_dec)
            if micro_dec is not None and mid_dec is not None
            else None
        ),
        "microprice_change": micro_change,
        "mid_change": mid_change,
        "mid_change_favorable_signed": favorable,
        "adverse_price_response_after_aggression": adverse if adverse is not None and adverse > 0 else (0.0 if adverse is not None else None),
        "favorable_price_response_after_aggression": favorable if favorable is not None and favorable > 0 else (0.0 if favorable is not None else None),
        "price_impact_per_attributed_fill": (mid_change / fill) if fill and mid_change is not None and abs(fill) > 1e-12 else None,
        "price_impact_per_net_depletion": (mid_change / net) if net and mid_change is not None and abs(net) > 1e-12 else None,
        "pre_touch_fill": _sum(pre, "attributed_fill"),
        "pre_touch_pull": _sum(pre, "residual_pull"),
        "decision_fill": _sum(touch_to_dec, "attributed_fill"),
        "decision_pull": _sum(touch_to_dec, "residual_pull"),
        "decision_refill": _sum(touch_to_dec, "refill"),
        "feature_available_at": decision_at.isoformat().replace("+00:00", "Z"),
        "decision_cutoff": decision_at.isoformat().replace("+00:00", "Z"),
        "causal_valid": True,
        "phase": "TOUCH_TO_DECISION",
        "post_decision_forensic": False,
        "material_fill_definition": "first_positive_attributed_fill_bucket",
        **snaps,
    }
    # AUC trapezoid
    if len(qdh_pts) >= 2:
        auc = 0.0
        for i in range(1, len(qdh_pts)):
            dt = (qdh_pts[i][0] - qdh_pts[i - 1][0]).total_seconds()
            auc += 0.5 * (qdh_pts[i][1] + qdh_pts[i - 1][1]) * dt
        out["qdh_auc"] = auc
    return out
