"""Outcome labeling: may use future post-break path; never used as a feature."""

from __future__ import annotations

from typing import Any

from research.orderbook.historical_post_break_acceptance_reclaim import (
    MECH_TO_COARSE,
    OUTCOME_ACCEPTED,
    OUTCOME_AMBIGUOUS,
    OUTCOME_RECLAIM,
)


def distance_beyond_bps(*, mid: float | None, level: float, direction: str) -> float | None:
    """Positive = further beyond the broken level (break direction)."""
    if mid is None or level <= 0:
        return None
    raw = (mid - level) / level * 1e4
    if direction == "bearish":
        return -raw  # below level → positive
    return raw  # above level → positive


def is_beyond(*, best_bid: float | None, best_ask: float | None, level: float, direction: str) -> bool:
    if direction == "bearish":
        return best_bid is not None and best_bid < level
    return best_ask is not None and best_ask > level


def label_from_post_path(
    samples: list[dict[str, Any]],
    *,
    break_ms: int,
    level: float,
    direction: str,
    reclaim_horizon_ms: int = 120_000,
    accept_confirm_ms: int = 60_000,
) -> dict[str, Any]:
    """Label using full post-break path (future OK for labels only).

    RECLAIM: BBO returns to safe side of level within reclaim_horizon after having broken.
    BREAK_ACCEPTED: still beyond at accept_confirm and at 2*accept_confirm without reclaim.
    """
    post = [s for s in samples if s["ts_ms"] >= break_ms]
    if not post:
        return {
            "outcome": OUTCOME_AMBIGUOUS,
            "outcome_reason": "no_post_samples",
            "first_reclaim_ts_ms": None,
            "uses_future_info": True,
        }

    # Confirm break shortly after first_break
    early = [s for s in post if s["ts_ms"] <= break_ms + 5_000]
    broke = any(
        is_beyond(
            best_bid=s.get("best_bid"),
            best_ask=s.get("best_ask"),
            level=level,
            direction=direction,
        )
        for s in early
    ) or is_beyond(
        best_bid=post[0].get("best_bid"),
        best_ask=post[0].get("best_ask"),
        level=level,
        direction=direction,
    )
    if not broke:
        return {
            "outcome": OUTCOME_AMBIGUOUS,
            "outcome_reason": "no_bbo_break_near_anchor",
            "first_reclaim_ts_ms": None,
            "uses_future_info": True,
        }

    reclaim_ms = None
    for s in post:
        if s["ts_ms"] > break_ms + reclaim_horizon_ms:
            break
        if s["ts_ms"] <= break_ms + 2_000:
            continue
        if not is_beyond(
            best_bid=s.get("best_bid"),
            best_ask=s.get("best_ask"),
            level=level,
            direction=direction,
        ):
            reclaim_ms = s["ts_ms"]
            break

    if reclaim_ms is not None:
        return {
            "outcome": OUTCOME_RECLAIM,
            "outcome_reason": "bbo_returned_safe_side_within_horizon",
            "first_reclaim_ts_ms": reclaim_ms,
            "seconds_to_reclaim": (reclaim_ms - break_ms) / 1000.0,
            "uses_future_info": True,
        }

    def beyond_at(offset_ms: int) -> bool | None:
        target = break_ms + offset_ms
        cand = [s for s in post if s["ts_ms"] <= target]
        if not cand:
            return None
        s = cand[-1]
        return is_beyond(
            best_bid=s.get("best_bid"),
            best_ask=s.get("best_ask"),
            level=level,
            direction=direction,
        )

    b60 = beyond_at(accept_confirm_ms)
    b120 = beyond_at(accept_confirm_ms * 2)
    if b60 is True and b120 is True:
        return {
            "outcome": OUTCOME_ACCEPTED,
            "outcome_reason": "still_beyond_at_60s_and_120s",
            "first_reclaim_ts_ms": None,
            "seconds_to_reclaim": None,
            "uses_future_info": True,
        }
    return {
        "outcome": OUTCOME_AMBIGUOUS,
        "outcome_reason": "unresolved_path",
        "first_reclaim_ts_ms": None,
        "seconds_to_reclaim": None,
        "uses_future_info": True,
    }


def map_event_outcome(
    *,
    ob_classification: str | None,
    path_label: dict[str, Any],
) -> dict[str, Any]:
    """Prefer existing deep-dive classification; else post-break BBO path."""
    path_out = path_label.get("outcome", OUTCOME_AMBIGUOUS)
    mech = (ob_classification or "").strip()
    mech_out = MECH_TO_COARSE.get(mech)

    if mech_out is not None:
        final = mech_out
        source = "legacy_ob_classification"
    elif path_out in {OUTCOME_ACCEPTED, OUTCOME_RECLAIM}:
        final = path_out
        source = "post_break_bbo_path"
    else:
        final = OUTCOME_AMBIGUOUS
        source = "ambiguous"

    return {
        "outcome": final,
        "outcome_source": source,
        "path_outcome": path_out,
        "path_reason": path_label.get("outcome_reason"),
        "legacy_ob_classification": mech or None,
        "first_reclaim_ts_ms": path_label.get("first_reclaim_ts_ms"),
        "seconds_to_reclaim": path_label.get("seconds_to_reclaim"),
        "uses_future_info": True,
    }
