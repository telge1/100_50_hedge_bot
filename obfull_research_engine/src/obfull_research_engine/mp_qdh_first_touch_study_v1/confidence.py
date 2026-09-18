"""Split flow-attribution confidence from availability confidence."""

from __future__ import annotations

from typing import Any


FLOW_LEVELS = ("HIGH", "MEDIUM", "LOW", "BLOCKED")
AVAIL_LEVELS = (
    "RECEIVE_TIME_OBSERVED",
    "EVENT_AVAILABLE_AT_OBSERVED",
    "EXCHANGE_TIME_WITH_CAUSAL_PROXY",
    "RECEIVE_TIME_NOT_AVAILABLE",
    "AVAILABILITY_BLOCKED",
)


def classify_availability_confidence(
    *,
    coverage_pass: bool,
    receive_present: int,
    receive_missing: int,
    event_available_at: str | None = None,
    exchange_event_time: str | None = None,
    collector_received_at: str | None = None,
) -> dict[str, Any]:
    """Availability is independent of fill/pull attribution quality."""
    if not coverage_pass:
        level = "AVAILABILITY_BLOCKED"
    elif receive_present > 0 and receive_missing == 0:
        level = "RECEIVE_TIME_OBSERVED"
    elif receive_present > 0:
        level = "EVENT_AVAILABLE_AT_OBSERVED"
    elif receive_missing > 0:
        level = "RECEIVE_TIME_NOT_AVAILABLE"
    else:
        level = "EXCHANGE_TIME_WITH_CAUSAL_PROXY"

    proxy = level in (
        "EXCHANGE_TIME_WITH_CAUSAL_PROXY",
        "RECEIVE_TIME_NOT_AVAILABLE",
        "EVENT_AVAILABLE_AT_OBSERVED",
    )
    return {
        "availability_confidence": level,
        "exchange_event_time": exchange_event_time,
        "collector_received_at": collector_received_at,
        "event_available_at": event_available_at,
        "receive_time_is_proxy": proxy and level != "RECEIVE_TIME_OBSERVED",
        "receive_time_present_count": int(receive_present),
        "receive_time_missing_count": int(receive_missing),
        "source_age_ms": None,
        "availability_warning": (
            "NO_COLLECTOR_RECEIVE_TIME" if level == "RECEIVE_TIME_NOT_AVAILABLE" else None
        ),
        "usable_for_historical_research": level
        not in ("AVAILABILITY_BLOCKED",),
        "usable_for_live_latency_claim": level == "RECEIVE_TIME_OBSERVED",
    }


def classify_flow_attribution_confidence(
    *,
    coverage_pass: bool,
    coverage_blockers: list[str] | None,
    linkage_status: str | None,
    has_wall: bool,
    mass_balance_violations: int,
    seq_gaps: int,
    cross_epoch: int,
    lookahead_flags: int,
    attributed_trade_count: int,
    unmatched_fill: float,
    unknown_qty: float,
    book_decrease_qty: float,
    inferred_refill_qty: float,
    fill_qty: float,
) -> dict[str, Any]:
    """Flow attribution ignores receive-time; unknown_fraction is separate."""
    blockers = list(coverage_blockers or [])
    flags: list[str] = []
    book_dec = float(book_decrease_qty or 0.0)
    unk = float(unknown_qty or 0.0)
    unknown_fraction = (unk / book_dec) if book_dec > 1e-12 else None

    if not coverage_pass or blockers:
        return {
            "flow_attribution_confidence": "BLOCKED",
            "flow_attribution_flags": blockers or ["COVERAGE_FAIL"],
            "unknown_fraction": unknown_fraction,
            "receive_time_affects_flow_confidence": False,
        }
    if int(seq_gaps or 0) > 0:
        return {
            "flow_attribution_confidence": "BLOCKED",
            "flow_attribution_flags": ["BOOK_SEQUENCE_GAP"],
            "unknown_fraction": unknown_fraction,
            "receive_time_affects_flow_confidence": False,
        }
    if int(cross_epoch or 0) > 0:
        return {
            "flow_attribution_confidence": "BLOCKED",
            "flow_attribution_flags": ["REPLAY_EPOCH_MIX_OR_CHANGE"],
            "unknown_fraction": unknown_fraction,
            "receive_time_affects_flow_confidence": False,
        }
    if int(lookahead_flags or 0) > 0:
        return {
            "flow_attribution_confidence": "BLOCKED",
            "flow_attribution_flags": ["LOOKAHEAD"],
            "unknown_fraction": unknown_fraction,
            "receive_time_affects_flow_confidence": False,
        }
    if int(mass_balance_violations or 0) > 0:
        return {
            "flow_attribution_confidence": "BLOCKED",
            "flow_attribution_flags": ["MASS_BALANCE_VIOLATION"],
            "unknown_fraction": unknown_fraction,
            "receive_time_affects_flow_confidence": False,
        }
    if not has_wall or str(linkage_status or "") in ("", "NO_WALL", "ABSENT", "MISSING"):
        return {
            "flow_attribution_confidence": "LOW",
            "flow_attribution_flags": ["WEAK_OR_MISSING_WALL"],
            "unknown_fraction": unknown_fraction,
            "receive_time_affects_flow_confidence": False,
        }

    if float(unmatched_fill or 0.0) > 1e-9:
        flags.append("UNMATCHED_FILL")
    if attributed_trade_count <= 0 and unk > 1e-9:
        flags.append("NO_FILL_WITH_UNKNOWN")
    if str(linkage_status) == "MOVED_BEFORE_TOUCH":
        flags.append("WALL_MOVED_BEFORE_TOUCH")

    # No outcome-tuned unknown threshold — flags only
    if "UNMATCHED_FILL" in flags or "NO_FILL_WITH_UNKNOWN" in flags:
        level = "LOW"
    elif float(inferred_refill_qty or 0.0) > 1e-9 and float(fill_qty or 0.0) > 0:
        level = "MEDIUM"
        flags.append("INFERRED_REFILL_PRESENT")
    elif attributed_trade_count > 0 and float(fill_qty or 0.0) >= 0:
        level = "HIGH"
        flags.append("WALL_AND_TRADES_RESOLVED")
    else:
        level = "MEDIUM"
        flags.append("NET_BUCKET_OR_LIMITED_RESOLUTION")

    return {
        "flow_attribution_confidence": level,
        "flow_attribution_flags": flags,
        "unknown_fraction": unknown_fraction,
        "receive_time_affects_flow_confidence": False,
    }


def attach_confidence_fields(
    *,
    coverage: dict[str, Any],
    linkage_status: str | None,
    has_wall: bool,
    funnel: dict[str, Any] | None,
    attribution_stats: dict[str, Any] | None,
    features: dict[str, Any],
    flow_meta: dict[str, Any] | None,
    decision_at: str | None,
) -> dict[str, Any]:
    funnel = funnel or {}
    stats = attribution_stats or {}
    flow_meta = flow_meta or {}
    recv_p = int(funnel.get("receive_time_present_count") or 0)
    recv_m = int(funnel.get("receive_time_missing_count") or 0)
    avail = classify_availability_confidence(
        coverage_pass=bool(coverage.get("pass")),
        receive_present=recv_p,
        receive_missing=recv_m,
        event_available_at=decision_at,
    )
    flow = classify_flow_attribution_confidence(
        coverage_pass=bool(coverage.get("pass")),
        coverage_blockers=list(coverage.get("blockers") or []),
        linkage_status=linkage_status,
        has_wall=has_wall,
        mass_balance_violations=int(stats.get("mass_balance_violations") or 0),
        seq_gaps=int(coverage.get("seq_gaps") or stats.get("seq_gaps") or 0),
        cross_epoch=int(coverage.get("cross_epoch") or stats.get("cross_epoch") or 0),
        lookahead_flags=int(flow_meta.get("lookahead_flags") or 0),
        attributed_trade_count=int(funnel.get("attributed_trade_count") or features.get("unique_trade_count") or 0),
        unmatched_fill=float(features.get("fill_excess_over_decrease") or 0.0),
        unknown_qty=float(features.get("unknown_qty") or 0.0),
        book_decrease_qty=float(features.get("book_decrease_qty") or 0.0),
        inferred_refill_qty=float(features.get("refill_qty") or 0.0),
        fill_qty=float(features.get("attributed_fill_qty") or 0.0),
    )
    return {
        **avail,
        **flow,
        "coverage_ok": bool(coverage.get("pass")),
        "coverage_block_reason": "|".join(coverage.get("blockers") or []) or None,
        "replay_epoch_ok": int(coverage.get("cross_epoch") or 0) == 0,
        "sequence_complete": int(coverage.get("seq_gaps") or 0) == 0,
        "public_trades_complete": int(coverage.get("n_trades_raw") or 0) > 0,
    }
