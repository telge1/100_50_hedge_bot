"""Map QDH engine timeline → CanonicalWallFlowBucket rows (no QDH reimplementation)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from obfull_research_engine.drilldown.aggregation_100ms import _as_dt

from . import QUEUE_EXHAUSTED_EPS
from .near_zero import apply_queue_policy


def _parse_ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return _as_dt(value)


def map_timeline_to_buckets(
    *,
    event_id: str,
    wall_id: str,
    timeline: list[dict[str, Any]],
    trigger_ts: datetime,
    first_touch_ts: datetime,
    wall_side: str,
    queue_at_touch: float | None,
) -> list[dict[str, Any]]:
    """Build canonical bucket rows; forensic tail marked post_decision=True."""
    rows: list[dict[str, Any]] = []
    side = str(wall_side).lower()
    mid0 = None
    for raw in timeline:
        avail = _parse_ts(raw.get("feature_available_at") or raw.get("bucket_available_at"))
        bucket_ts = _parse_ts(raw.get("bucket_start") or raw.get("bucket_ts"))
        if avail is None:
            continue
        post = avail > trigger_ts
        q_rem = raw.get("wall_size_band")
        if q_rem is None:
            q_rem = raw.get("queue_band") or raw.get("queue_remaining")
        try:
            q_rem_f = float(q_rem) if q_rem is not None else None
        except (TypeError, ValueError):
            q_rem_f = None
        qdh = raw.get("qdh_base")
        try:
            qdh_f = float(qdh) if qdh is not None else None
        except (TypeError, ValueError):
            qdh_f = None
        runway = raw.get("queue_runway_seconds")
        try:
            runway_f = float(runway) if runway is not None else None
        except (TypeError, ValueError):
            runway_f = None

        pol = apply_queue_policy(
            queue_remaining_qty=q_rem_f,
            qdh_base=qdh_f,
            queue_runway_seconds=runway_f,
        )
        frac = None
        if q_rem_f is not None and queue_at_touch is not None and float(queue_at_touch) > QUEUE_EXHAUSTED_EPS:
            frac = float(q_rem_f) / float(queue_at_touch)

        mid = raw.get("midprice")
        if mid0 is None and mid is not None:
            try:
                mid0 = float(mid)
            except (TypeError, ValueError):
                mid0 = None
        # Engine emits progress_bps (attack direction); fall back to mid delta.
        attack_progress = raw.get("progress_bps")
        if attack_progress is None and mid is not None and mid0 is not None:
            try:
                d = float(mid) - float(mid0)
                attack_progress = (d if side == "ask" else -d)
            except (TypeError, ValueError):
                attack_progress = None

        hit_notional = float(raw.get("hit_notional") or 0.0)
        musd = hit_notional / 1_000_000.0 if hit_notional else 0.0
        # Prefer engine impact_efficiency (bps per million) when present.
        impact = raw.get("impact_efficiency")
        impact_valid = impact is not None
        impact_reason = None if impact_valid else "NO_ENGINE_IMPACT"
        if not impact_valid and musd > 0 and attack_progress is not None:
            try:
                impact = float(attack_progress) / musd
                impact_valid = True
                impact_reason = None
            except (TypeError, ValueError):
                impact_reason = "PROGRESS_INVALID"

        causal_ok = avail <= trigger_ts or post  # post allowed but flagged
        leakage = avail > trigger_ts and not post
        # Actually leakage for signal features is avail > trigger; post is intentional
        if post:
            causal_ok = True

        rows.append(
            {
                "event_id": event_id,
                "wall_id": wall_id,
                "bucket_ts": bucket_ts.isoformat() if bucket_ts else None,
                "bucket_available_at": avail.isoformat(),
                "post_decision": bool(post),
                "exact_price_queue_before": raw.get("wall_size_exact"),
                "exact_price_queue_after": raw.get("wall_size_exact"),
                "defended_band_queue_before": raw.get("wall_size_band"),
                "defended_band_queue_after": raw.get("wall_size_band"),
                "attributed_trade_count": raw.get("hit_trade_count") or raw.get("attributed_trade_count"),
                "unique_trade_count": raw.get("hit_trade_count") or raw.get("unique_trade_count"),
                "trade_ids_hash": raw.get("trade_ids_hash") or raw.get("attributed_trade_ids_hash"),
                "attributed_fill_qty": raw.get("hit_qty"),
                "attributed_fill_notional": hit_notional,
                "residual_pull_qty": raw.get("residual_pull_qty") or raw.get("pull_qty"),
                "refill_qty": raw.get("net_refill_qty") or raw.get("refill_qty"),
                "net_depletion_qty": raw.get("net_depletion_qty"),
                "attribution_unknown_qty": raw.get("attribution_unknown_qty") or 0.0,
                "attribution_confidence": raw.get("attribution_confidence") or raw.get("confidence"),
                "attack_rate_qty_per_s": raw.get("hit_rate") or raw.get("attack_rate_qty_per_s"),
                "attack_rate_notional_per_s": raw.get("attack_rate_notional_per_s"),
                "persistence_ratio": raw.get("persistence_ratio"),
                "queue_remaining_qty": q_rem_f,
                "queue_fraction_remaining": frac if frac is not None else pol.get("queue_fraction_remaining"),
                "canonical_qdh_base": pol.get("canonical_qdh_base"),
                "canonical_qdh_persistence_multiplier": raw.get("M_persistence") or raw.get("persistence_ratio"),
                "canonical_qdh_persistence_adjusted": raw.get("qdh_toxic_base_only"),
                "queue_runway_seconds": pol.get("queue_runway_seconds"),
                "queue_state": pol.get("queue_state"),
                "qdh_valid": pol.get("qdh_valid"),
                "qdh_invalid_reason": pol.get("qdh_invalid_reason"),
                "near_zero_queue_warning": pol.get("near_zero_queue_warning"),
                "midprice": raw.get("midprice"),
                "microprice": raw.get("microprice"),
                "microprice_source": "ENGINE_TIMELINE",
                "spread_bps": raw.get("spread_bps"),
                "attack_progress_pct": attack_progress,
                "defender_reclaim_pct": raw.get("defender_reclaim_pct") or raw.get("reclaim_ticks"),
                "impact_efficiency_pct_per_musd": impact,
                "impact_efficiency_valid": impact_valid,
                "impact_efficiency_invalid_reason": impact_reason,
                "normalized_impact_efficiency": "NOT_CALIBRATED",
                "absorption_ratio": "NOT_CALIBRATED",
                "vacuum_score": "NOT_CALIBRATED",
                "ofi_reclaim": "NOT_IMPLEMENTED",
                "max_input_available_at": raw.get("max_input_available_at"),
                "causal_valid": bool(avail <= trigger_ts) if not post else True,
                "leakage_flag": bool(leakage),
                "window_phase": _phase(avail, first_touch_ts, trigger_ts),
                # raw buy/sell (not direction-normalized)
                "raw_buy_hit_qty": raw.get("buy_hit_qty"),
                "raw_sell_hit_qty": raw.get("sell_hit_qty"),
                "attack_accelerating": raw.get("attack_accelerating"),
                "attack_decelerating": raw.get("attack_decelerating"),
            }
        )
    return rows


def _phase(avail: datetime, touch: datetime, trigger: datetime) -> str:
    if avail <= touch:
        return "BASELINE"
    if avail <= trigger:
        return "DECISION"
    return "FORENSIC_TAIL"


def decision_snapshot_from_buckets(
    *,
    event_id: str,
    wall_id: str,
    trigger_ts: datetime,
    buckets: list[dict[str, Any]],
    coverage_ok: bool,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Last pre-trigger bucket only (forensic excluded)."""
    pre = [b for b in buckets if not b.get("post_decision")]
    if not pre:
        return {
            "event_id": event_id,
            "wall_id": wall_id,
            "trigger_ts": trigger_ts.isoformat(),
            "feature_cutoff_ts": trigger_ts.isoformat(),
            "coverage_ok": coverage_ok,
            "leakage_check_passed": False,
            "blocker_reason": "NO_PRE_TRIGGER_BUCKETS",
            **(extra or {}),
        }
    last = pre[-1]
    max_avail = last.get("bucket_available_at")
    max_dt = _parse_ts(max_avail)
    leak_ok = max_dt is not None and max_dt <= trigger_ts
    snap = {
        "event_id": event_id,
        "wall_id": wall_id,
        "trigger_ts": trigger_ts.isoformat(),
        "feature_cutoff_ts": trigger_ts.isoformat(),
        "coverage_ok": coverage_ok,
        "attribution_confidence": last.get("attribution_confidence"),
        "qdh_valid": last.get("qdh_valid"),
        "max_signal_feature_available_at": max_avail,
        "leakage_check_passed": bool(leak_ok),
        "canonical_qdh_attributed_fill_qty": last.get("attributed_fill_qty"),
        "canonical_qdh_residual_pull_qty": last.get("residual_pull_qty"),
        "canonical_qdh_refill_qty": last.get("refill_qty"),
        "canonical_qdh_net_depletion_qty": last.get("net_depletion_qty"),
        "canonical_qdh_base": last.get("canonical_qdh_base"),
        "canonical_qdh_queue_remaining": last.get("queue_remaining_qty"),
        "canonical_qdh_persistence_ratio": last.get("persistence_ratio"),
        "canonical_qdh_persistence_multiplier": last.get("canonical_qdh_persistence_multiplier"),
        "canonical_qdh_persistence_adjusted": last.get("canonical_qdh_persistence_adjusted"),
        "queue_runway_seconds": last.get("queue_runway_seconds"),
        "queue_state": last.get("queue_state"),
        "queue_fraction_remaining": last.get("queue_fraction_remaining"),
        "midprice": last.get("midprice"),
        "microprice": last.get("microprice"),
        "attack_progress_pct": last.get("attack_progress_pct"),
        "impact_efficiency_pct_per_musd": last.get("impact_efficiency_pct_per_musd"),
        "persistence_ratio": last.get("persistence_ratio"),
        "attack_rate_qty_per_s": last.get("attack_rate_qty_per_s"),
        "near_zero_queue_warning": last.get("near_zero_queue_warning"),
        **(extra or {}),
    }
    return snap
