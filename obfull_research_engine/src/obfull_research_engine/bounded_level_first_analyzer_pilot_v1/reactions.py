"""Fixed causal price-reaction rules. Not a trading signal. No outcome inputs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..timeparse import format_utc_z
from . import REACTION_HORIZON_S
from .bins import point_in_zone
from .episodes import candle_complete_at, candle_fully_outside, parse_utc, side_of_zone


def _low_inside_zone(low: float, zone_low: float, zone_high: float) -> bool:
    return point_in_zone(low, zone_low, zone_high)


def _high_inside_zone(high: float, zone_low: float, zone_high: float) -> bool:
    return point_in_zone(high, zone_low, zone_high)


def causal_candles_after(
    candles: list[dict[str, Any]],
    *,
    after_ts: datetime,
    before_or_at: datetime,
) -> list[dict[str, Any]]:
    out = []
    for c in candles:
        open_ts = parse_utc(c["open_time"])
        close_ts = candle_complete_at(open_ts)
        if close_ts <= after_ts:
            continue
        if close_ts > before_or_at:
            continue
        row = dict(c)
        row["close_ts"] = close_ts
        out.append(row)
    out.sort(key=lambda r: r["close_ts"])
    return out


def count_side_changes(
    *,
    prices: list[tuple[datetime, float]],
    zone_low: float,
    zone_high: float,
    start: datetime,
    end: datetime,
) -> tuple[int, datetime | None]:
    last_out: str | None = None
    crosses = 0
    third_ts: datetime | None = None
    for ts, price in prices:
        if ts < start or ts > end:
            continue
        side = side_of_zone(price, zone_low, zone_high)
        if side == "INSIDE":
            continue
        if last_out is None:
            last_out = side
            continue
        if side != last_out:
            crosses += 1
            if crosses == 3:
                third_ts = ts
            last_out = side
    return crosses, third_ts


def resistance_context(
    *,
    lld_type: str,
    approach_side: str,
    member_tpo_types: list[str] | None = None,
) -> bool:
    """Rejection-down needs resistance semantics, not every below-approach."""
    types = {str(t) for t in (member_tpo_types or [])}
    if lld_type == "MP_LLD_DIRECTION_CONFLICT":
        return False
    if lld_type == "MP_WITH_LLD_RESISTANCE":
        return approach_side in {"BELOW", "INSIDE", "INSIDE_OR_UNKNOWN"}
    if lld_type == "MP_WITH_MULTIPLE_LLD":
        return False
    if "TPO_VAH" in types and lld_type in {"MP_ONLY", "MP_WITH_LLD_RESISTANCE"}:
        return approach_side in {"BELOW", "INSIDE", "INSIDE_OR_UNKNOWN"}
    return False


def support_context(
    *,
    lld_type: str,
    approach_side: str,
    member_tpo_types: list[str] | None = None,
) -> bool:
    types = {str(t) for t in (member_tpo_types or [])}
    if lld_type == "MP_LLD_DIRECTION_CONFLICT":
        return False
    if lld_type == "MP_WITH_LLD_SUPPORT":
        return approach_side in {"ABOVE", "INSIDE", "INSIDE_OR_UNKNOWN"}
    if lld_type == "MP_WITH_MULTIPLE_LLD":
        return False
    if "TPO_VAL" in types and lld_type in {"MP_ONLY", "MP_WITH_LLD_SUPPORT"}:
        return approach_side in {"ABOVE", "INSIDE", "INSIDE_OR_UNKNOWN"}
    return False


def genuine_break_down(*, approach_side: str, first_exit_dir: str | None) -> bool:
    """Reclaim-up requires a prior downside leave that is not a bounce back below."""
    if first_exit_dir != "DOWN":
        return False
    return approach_side != "BELOW"


def genuine_break_up(*, approach_side: str, first_exit_dir: str | None) -> bool:
    if first_exit_dir != "UP":
        return False
    return approach_side != "ABOVE"


def reaction_direction(reaction_class: str, *, lld_type: str) -> str | None:
    if lld_type == "MP_LLD_DIRECTION_CONFLICT":
        return None
    if reaction_class in {
        "REJECTED_DOWN_FROM_RESISTANCE_CONTEXT",
        "RECLAIMED_DOWN",
        "ACCEPTED_BELOW",
    }:
        return "BEARISH"
    if reaction_class in {
        "REJECTED_UP_FROM_SUPPORT_CONTEXT",
        "RECLAIMED_UP",
        "ACCEPTED_ABOVE",
    }:
        return "BULLISH"
    return None


def classify_reaction(
    *,
    episode: dict[str, Any],
    cluster: dict[str, Any],
    candles_1m: list[dict[str, Any]],
    price_marks: list[tuple[datetime, float]],
    lld_type: str,
    window_end: datetime,
) -> dict[str, Any]:
    """Classify using only completed causal 1m candles and the 15-minute cap."""
    touch = parse_utc(episode["first_touch_ts"])
    zone_low = float(cluster["cluster_price_low"])
    zone_high = float(cluster["cluster_price_high"])
    horizon_end = min(touch + timedelta(seconds=REACTION_HORIZON_S), window_end)
    candles = causal_candles_after(candles_1m, after_ts=touch, before_or_at=horizon_end)
    approach = str(episode.get("approach_side") or "INSIDE_OR_UNKNOWN")
    first_exit = episode.get("first_exit_ts")
    first_exit_dir = episode.get("first_exit_direction")
    first_exit_dt = parse_utc(first_exit) if first_exit else None
    member_tpo_types = list(cluster.get("member_level_types") or [])

    lifecycle: list[dict[str, Any]] = [
        {
            "episode_id": episode["episode_id"],
            "status": "FIRST_TOUCH",
            "ts": episode["first_touch_ts"],
        }
    ]

    def result(
        reaction_class: str,
        *,
        detection: datetime | None,
        reason: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        det = detection
        if reaction_class != "DATA_NOT_COMPLETE" and det is not None and det < touch:
            raise RuntimeError("detection before first touch")
        row = {
            "episode_id": episode["episode_id"],
            "level_cluster_id": cluster["level_cluster_id"],
            "reaction_class": reaction_class,
            "detection_available_at": None if det is None else format_utc_z(det),
            "observation_end": format_utc_z(horizon_end),
            "reason": reason,
            "lld_confluence_type": lld_type,
            "approach_side": approach,
            "break_is_not_acceptance": True,
            "is_trading_signal": False,
            "reaction_direction": reaction_direction(reaction_class, lld_type=lld_type),
            "n_causal_1m_candles": len(candles),
        }
        if extra:
            row.update(extra)
        return row

    if not candles and first_exit_dt is None:
        return result(
            "DATA_NOT_COMPLETE",
            detection=horizon_end,
            reason="NO_COMPLETED_1M_CANDLE_AFTER_TOUCH_IN_WINDOW",
        )

    accepted_above_at: datetime | None = None
    accepted_below_at: datetime | None = None
    for i in range(len(candles) - 1):
        a, b = candles[i], candles[i + 1]
        if float(a["close"]) > zone_high and float(b["close"]) > zone_high:
            both_lows_inside = _low_inside_zone(float(a["low"]), zone_low, zone_high) and _low_inside_zone(
                float(b["low"]), zone_low, zone_high
            )
            if not both_lows_inside:
                accepted_above_at = b["close_ts"]
                break
        if float(a["close"]) < zone_low and float(b["close"]) < zone_low:
            both_highs_inside = _high_inside_zone(float(a["high"]), zone_low, zone_high) and _high_inside_zone(
                float(b["high"]), zone_low, zone_high
            )
            if not both_highs_inside:
                accepted_below_at = b["close_ts"]
                break

    reclaimed_up_at: datetime | None = None
    reclaimed_down_at: datetime | None = None
    broke_down = genuine_break_down(approach_side=approach, first_exit_dir=first_exit_dir)
    broke_up = genuine_break_up(approach_side=approach, first_exit_dir=first_exit_dir)
    if broke_down:
        for c in candles:
            if first_exit_dt is not None and c["close_ts"] <= first_exit_dt:
                continue
            if float(c["close"]) > zone_high:
                reclaimed_up_at = c["close_ts"]
                break
    if broke_up:
        for c in candles:
            if first_exit_dt is not None and c["close_ts"] <= first_exit_dt:
                continue
            if float(c["close"]) < zone_low:
                reclaimed_down_at = c["close_ts"]
                break

    resisted = resistance_context(
        lld_type=lld_type, approach_side=approach, member_tpo_types=member_tpo_types
    )
    supported = support_context(
        lld_type=lld_type, approach_side=approach, member_tpo_types=member_tpo_types
    )
    rejected_down_at: datetime | None = None
    rejected_up_at: datetime | None = None
    if resisted and accepted_above_at is None:
        for c in candles:
            if float(c["close"]) < zone_low:
                rejected_down_at = c["close_ts"]
                break
    if supported and accepted_below_at is None:
        for c in candles:
            if float(c["close"]) > zone_high:
                rejected_up_at = c["close_ts"]
                break

    crosses, third_ts = count_side_changes(
        prices=price_marks,
        zone_low=zone_low,
        zone_high=zone_high,
        start=touch,
        end=horizon_end,
    )
    chop_true = crosses >= 3 and accepted_above_at is None and accepted_below_at is None

    directed: list[tuple[datetime, str, str]] = []
    if accepted_above_at is not None:
        directed.append((accepted_above_at, "ACCEPTED_ABOVE", "TWO_CLOSED_1M_CLOSES_ABOVE_CLUSTER_HIGH"))
    if accepted_below_at is not None:
        directed.append((accepted_below_at, "ACCEPTED_BELOW", "TWO_CLOSED_1M_CLOSES_BELOW_CLUSTER_LOW"))
    if reclaimed_up_at is not None:
        directed.append((reclaimed_up_at, "RECLAIMED_UP", "LEFT_DOWN_THEN_CLOSED_1M_ABOVE_CLUSTER_HIGH"))
    if reclaimed_down_at is not None:
        directed.append((reclaimed_down_at, "RECLAIMED_DOWN", "LEFT_UP_THEN_CLOSED_1M_BELOW_CLUSTER_LOW"))
    if rejected_down_at is not None and accepted_above_at is None:
        directed.append(
            (
                rejected_down_at,
                "REJECTED_DOWN_FROM_RESISTANCE_CONTEXT",
                "RESISTANCE_CONTEXT_THEN_CLOSED_1M_BELOW_CLUSTER_LOW",
            )
        )
    if rejected_up_at is not None and accepted_below_at is None:
        directed.append(
            (
                rejected_up_at,
                "REJECTED_UP_FROM_SUPPORT_CONTEXT",
                "SUPPORT_CONTEXT_THEN_CLOSED_1M_ABOVE_CLUSTER_HIGH",
            )
        )
    directed = [c for c in directed if c[0] <= horizon_end]
    simultaneous = sorted({c[1] for c in directed})
    predicate_extra = {
        "lifecycle": lifecycle,
        "side_changes": crosses,
        "acceptance_predicate_1": accepted_above_at is not None or accepted_below_at is not None,
        "reclaim_requires_genuine_prior_break": True,
        "rejection_requires_side_semantics": True,
        "simultaneous_directed_classes": simultaneous,
        "chop_true": chop_true,
        "priority_rule": "UNIQUE_DIRECTED_CLASS_ELSE_UNRESOLVED_OR_CHOP",
        "does_not_force_direction_after_15m": True,
        "used_post_detection_candles": False,
    }

    if len(simultaneous) > 1 or (chop_true and simultaneous):
        det = min((c[0] for c in directed), default=third_ts or horizon_end)
        lifecycle.append(
            {"episode_id": episode["episode_id"], "status": "TOUCH_UNRESOLVED", "ts": format_utc_z(det)}
        )
        return result(
            "TOUCH_UNRESOLVED",
            detection=det,
            reason="REACTION_CLASS_NOT_UNIQUE",
            extra=predicate_extra,
        )

    if len(simultaneous) == 1:
        det, klass, reason = min(directed, key=lambda x: x[0])
        lifecycle.append({"episode_id": episode["episode_id"], "status": klass, "ts": format_utc_z(det)})
        return result(klass, detection=det, reason=reason, extra=predicate_extra)

    if chop_true:
        det = third_ts or horizon_end
        lifecycle.append({"episode_id": episode["episode_id"], "status": "CHOPPED_AROUND_LEVEL", "ts": format_utc_z(det)})
        return result(
            "CHOPPED_AROUND_LEVEL",
            detection=det,
            reason="THREE_OR_MORE_SIDE_CHANGES_NO_ACCEPTANCE",
            extra=predicate_extra,
        )

    if first_exit_dt is not None and first_exit_dt <= horizon_end:
        klass = "BROKE_UP" if first_exit_dir == "UP" else "BROKE_DOWN"
        lifecycle.append({"episode_id": episode["episode_id"], "status": klass, "ts": format_utc_z(first_exit_dt)})
        return result(
            klass,
            detection=first_exit_dt,
            reason="FULL_EXIT_WITHOUT_ACCEPTANCE_OR_RECLAIM_OR_REJECTION",
            extra={"lifecycle": lifecycle},
        )

    return result(
        "TOUCH_UNRESOLVED",
        detection=horizon_end,
        reason="NO_UNIQUE_ACCEPT_RECLAIM_REJECT_OR_CHOP_WITHIN_15M",
        extra={"lifecycle": lifecycle, "side_changes": crosses},
    )
