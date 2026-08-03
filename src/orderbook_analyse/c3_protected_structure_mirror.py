"""Mathematical mirror: Protected-High decisions as Protected-Low on reflected ticks.

Does not modify confirmation thresholds in ``c3_protected_low_event_driven_decision``.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any, Sequence

from orderbook_analyse.c3_protected_low_event_driven_decision import (
    evaluate_breakdown_confirmed,
    evaluate_reclaim_confirmed,
    find_causal_decision,
)

# Low outcome / artefact → High label (for mirror_parity_audit.json)
MIRROR_PARITY_TABLE: dict[str, str] = {
    "BREAKDOWN_CONFIRMED": "BREAKOUT_CONFIRMED",
    "RECLAIM_CONFIRMED": "RECLAIM_DOWN_CONFIRMED",
    "UNRESOLVED_WITHIN_MAX_WINDOW": "UNRESOLVED_WITHIN_MAX_WINDOW",
    "EVENT_DATA_INVALID": "EVENT_DATA_INVALID",
    "close_break_protected_down": "close_break_protected_up",
    "protected_low": "protected_high",
    "bearish_choch": "bullish_choch",
    "PROTECTED_LOW_BREAKDOWN": "PROTECTED_HIGH_BREAKOUT",
    "PROTECTED_LOW_RECLAIM": "PROTECTED_HIGH_RECLAIM_DOWN",
    "SHORT (breakdown)": "LONG (breakout)",
    "LONG (reclaim)": "SHORT (reclaim_down)",
    "distance_below_level_bps": "distance_above_level_bps",
    "distance_above_level_bps": "distance_below_level_bps",
    "first_reclaim_ts": "first_reclaim_down_ts",
    "rebreak_below_pl": "reclaim_above_ph",
    "reclaim_above_pl": "rebreak_below_ph",
    "SUFFICIENT_RECLAIM_AND_BREAKDOWN_SAMPLE_FOUND": (
        "SUFFICIENT_BREAKOUT_AND_RECLAIM_DOWN_SAMPLE_FOUND"
    ),
    "SUFFICIENT_BREAKDOWN_SAMPLE_ONLY": "SUFFICIENT_BREAKOUT_SAMPLE_ONLY",
    "SUFFICIENT_RECLAIM_SAMPLE_ONLY": "SUFFICIENT_RECLAIM_DOWN_SAMPLE_ONLY",
    "PROTECTED_LOW_EVENTS_MOSTLY_BREAKDOWN": "PROTECTED_HIGH_EVENTS_MOSTLY_BREAKOUT",
    "PROTECTED_LOW_EVENTS_MOSTLY_RECLAIM": "PROTECTED_HIGH_EVENTS_MOSTLY_RECLAIM_DOWN",
    "PROTECTED_LOW_EVENTS_MOSTLY_UNRESOLVED": "PROTECTED_HIGH_EVENTS_MOSTLY_UNRESOLVED",
}

_OUTCOME_MAP = {
    "BREAKDOWN_CONFIRMED": "BREAKOUT_CONFIRMED",
    "RECLAIM_CONFIRMED": "RECLAIM_DOWN_CONFIRMED",
}

_OUTCOME_MAP_BACK = {
    "BREAKOUT_CONFIRMED": "BREAKDOWN_CONFIRMED",
    "RECLAIM_DOWN_CONFIRMED": "RECLAIM_CONFIRMED",
}

_SIZE_ATTRS = ("notional", "quantity", "qty", "size", "amount", "trade_id")


def _flip_aggressor_side(side: Any) -> Any:
    """Flip buy↔sell preserving common casing (buy/Buy/BUY)."""
    if side is None:
        return None
    s = str(side)
    low = s.lower()
    if low not in {"buy", "sell"}:
        return side
    flipped = "sell" if low == "buy" else "buy"
    if s.isupper():
        return flipped.upper()
    if s[0].isupper():
        return flipped.capitalize()
    return flipped


def mirror_ticks(ticks: Sequence[Any], level: float) -> list[SimpleNamespace]:
    """Reflect price around ``level`` and flip aggressor side.

    ``price' = 2*level - price``. Preserves trade_ts and size/notional/qty fields.
    Returns ``SimpleNamespace`` rows compatible with ``find_causal_decision``.
    """
    level = float(level)
    out: list[SimpleNamespace] = []
    for t in ticks:
        kwargs: dict[str, Any] = {
            "trade_ts": getattr(t, "trade_ts"),
            "price": 2.0 * level - float(t.price),
        }
        for attr in _SIZE_ATTRS:
            if hasattr(t, attr):
                kwargs[attr] = getattr(t, attr)
        if hasattr(t, "side"):
            kwargs["side"] = _flip_aggressor_side(getattr(t, "side"))
        if hasattr(t, "is_buyer_maker"):
            kwargs["is_buyer_maker"] = not bool(getattr(t, "is_buyer_maker"))
        if hasattr(t, "buyer_maker"):
            kwargs["buyer_maker"] = not bool(getattr(t, "buyer_maker"))
        # Default notional if absent (helpers tolerate missing via getattr patterns)
        if "notional" not in kwargs and hasattr(t, "notional"):
            kwargs["notional"] = t.notional
        out.append(SimpleNamespace(**kwargs))
    return out


def map_outcome_low_to_high(outcome: str | None) -> str | None:
    if outcome is None:
        return None
    return _OUTCOME_MAP.get(str(outcome), str(outcome))


def map_outcome_high_to_low(outcome: str | None) -> str | None:
    if outcome is None:
        return None
    return _OUTCOME_MAP_BACK.get(str(outcome), str(outcome))


def _map_decision_high(low_decision: dict[str, Any]) -> dict[str, Any]:
    """Map a low causal decision dict onto high labels."""
    out = dict(low_decision)
    outcome = str(low_decision.get("outcome") or "")
    out["outcome"] = map_outcome_low_to_high(outcome) or outcome
    if out.get("state_after") is not None:
        out["state_after"] = map_outcome_low_to_high(str(out["state_after"])) or out["state_after"]
    # first_reclaim_ts on mirrored path ≡ first reclaim-down on original
    if "first_reclaim_ts" in out:
        out["first_reclaim_down_ts"] = out.get("first_reclaim_ts")
    return out


def find_causal_decision_high(
    ticks: Sequence[Any],
    *,
    level: float,
    available_at: datetime,
    late_end: datetime,
    book_by_ts: dict[str, dict[str, Any]] | None = None,
    check_every_s: int = 1,
) -> dict[str, Any]:
    """Causal Protected-High decision via mirror → low ``find_causal_decision``.

    ``book_by_ts`` is ignored (always ``None`` on the mirrored path).
    """
    del book_by_ts
    mirrored = mirror_ticks(ticks, level)
    low_dec = find_causal_decision(
        mirrored,
        level=level,
        available_at=available_at,
        late_end=late_end,
        book_by_ts=None,
        check_every_s=check_every_s,
    )
    return _map_decision_high(low_dec)


def evaluate_breakout_confirmed(
    ticks: Sequence[Any],
    *,
    level: float,
    available_at: datetime,
    as_of: datetime,
) -> dict[str, Any]:
    """Thin wrapper: mirror then ``evaluate_breakdown_confirmed``."""
    mirrored = mirror_ticks(ticks, level)
    bd = evaluate_breakdown_confirmed(
        mirrored, level=level, available_at=available_at, as_of=as_of
    )
    out = dict(bd)
    out["high_label"] = "BREAKOUT_CONFIRMED" if bd.get("confirmed") else None
    return out


def evaluate_reclaim_down_confirmed(
    ticks: Sequence[Any],
    *,
    level: float,
    available_at: datetime,
    as_of: datetime,
    book_at: dict[str, Any] | None = None,
    closes_1m: list[tuple[datetime, float]] | None = None,
) -> dict[str, Any]:
    """Thin wrapper: mirror then ``evaluate_reclaim_confirmed``."""
    del book_at, closes_1m
    mirrored = mirror_ticks(ticks, level)
    rc = evaluate_reclaim_confirmed(
        mirrored,
        level=level,
        available_at=available_at,
        as_of=as_of,
        book_at=None,
        closes_1m=None,
    )
    out = dict(rc)
    if "first_reclaim_ts" in out:
        out["first_reclaim_down_ts"] = out.get("first_reclaim_ts")
    out["high_label"] = "RECLAIM_DOWN_CONFIRMED" if rc.get("confirmed") else None
    return out


def flip_candidate_side(side: str) -> str:
    s = str(side).upper()
    if s == "LONG":
        return "SHORT"
    if s == "SHORT":
        return "LONG"
    return s


__all__ = [
    "MIRROR_PARITY_TABLE",
    "mirror_ticks",
    "find_causal_decision_high",
    "evaluate_breakout_confirmed",
    "evaluate_reclaim_down_confirmed",
    "map_outcome_low_to_high",
    "map_outcome_high_to_low",
    "flip_candidate_side",
]
