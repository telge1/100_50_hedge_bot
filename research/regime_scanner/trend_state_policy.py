"""Research direction policy derived from trend states (no trade opening).

Counterfactual only. Does not wire into live or productive pipeline.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

TrendStateName = Literal[
    "neutral",
    "bearish_warning",
    "early_bearish",
    "strong_bearish",
    "bearish_weakening",
    "bottoming",
    "bullish_warning",
    "early_bullish",
    "strong_bullish",
    "bullish_weakening",
    "topping",
    "unavailable",
]


@dataclass(frozen=True)
class DirectionPolicy:
    state: str
    allow_long: bool
    allow_short: bool
    require_stricter_long_confirmation: bool
    require_stricter_short_confirmation: bool
    block_new_long_setup: bool
    block_new_short_setup: bool
    abort_running_long_pa: bool
    abort_running_short_pa: bool
    abort_running_long_momentum: bool
    abort_running_short_momentum: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _policy(
    state: str,
    *,
    allow_long: bool,
    allow_short: bool,
    stricter_long: bool = False,
    stricter_short: bool = False,
    block_new_long_setup: bool = False,
    block_new_short_setup: bool = False,
    abort_long_pa: bool = False,
    abort_short_pa: bool = False,
    abort_long_mom: bool = False,
    abort_short_mom: bool = False,
) -> DirectionPolicy:
    return DirectionPolicy(
        state=state,
        allow_long=allow_long,
        allow_short=allow_short,
        require_stricter_long_confirmation=stricter_long,
        require_stricter_short_confirmation=stricter_short,
        block_new_long_setup=block_new_long_setup,
        block_new_short_setup=block_new_short_setup,
        abort_running_long_pa=abort_long_pa,
        abort_running_short_pa=abort_short_pa,
        abort_running_long_momentum=abort_long_mom,
        abort_running_short_momentum=abort_short_mom,
    )


# Spec §9 — Bottoming policy B (shorts block, longs stricter allow).
_POLICY_TABLE: dict[str, DirectionPolicy] = {
    "neutral": _policy("neutral", allow_long=True, allow_short=True),
    "bearish_warning": _policy(
        "bearish_warning",
        allow_long=True,
        allow_short=True,
        stricter_long=True,
    ),
    "early_bearish": _policy(
        "early_bearish",
        allow_long=False,
        allow_short=True,
        block_new_long_setup=True,
        abort_long_pa=True,
        abort_long_mom=True,
    ),
    "strong_bearish": _policy(
        "strong_bearish",
        allow_long=False,
        allow_short=True,
        block_new_long_setup=True,
        abort_long_pa=True,
        abort_long_mom=True,
    ),
    "bearish_weakening": _policy(
        "bearish_weakening",
        allow_long=False,
        allow_short=True,
        stricter_long=True,
        stricter_short=True,
        block_new_long_setup=True,
    ),
    "bottoming": _policy(
        "bottoming",
        allow_long=True,
        allow_short=False,
        stricter_long=True,
        block_new_short_setup=True,
        abort_short_pa=True,
        abort_short_mom=True,
    ),
    "bullish_warning": _policy(
        "bullish_warning",
        allow_long=True,
        allow_short=True,
        stricter_short=True,
    ),
    "early_bullish": _policy(
        "early_bullish",
        allow_long=True,
        allow_short=False,
        block_new_short_setup=True,
        abort_short_pa=True,
        abort_short_mom=True,
    ),
    "strong_bullish": _policy(
        "strong_bullish",
        allow_long=True,
        allow_short=False,
        block_new_short_setup=True,
        abort_short_pa=True,
        abort_short_mom=True,
    ),
    "bullish_weakening": _policy(
        "bullish_weakening",
        allow_long=True,
        allow_short=False,
        stricter_long=True,
        stricter_short=True,
        block_new_short_setup=True,
    ),
    "topping": _policy(
        "topping",
        allow_long=False,
        allow_short=True,
        stricter_short=True,
        block_new_long_setup=True,
        abort_long_pa=True,
        abort_long_mom=True,
    ),
    "unavailable": _policy(
        "unavailable",
        allow_long=True,
        allow_short=True,
    ),
}


def policy_for_state(state: str) -> DirectionPolicy:
    key = str(state or "unavailable")
    if key not in _POLICY_TABLE:
        return _POLICY_TABLE["unavailable"]
    return _POLICY_TABLE[key]


def would_block_long(state: str) -> bool:
    p = policy_for_state(state)
    return (not p.allow_long) or p.block_new_long_setup


def would_block_short(state: str) -> bool:
    p = policy_for_state(state)
    return (not p.allow_short) or p.block_new_short_setup


def all_policies() -> dict[str, DirectionPolicy]:
    return dict(_POLICY_TABLE)


__all__ = [
    "TrendStateName",
    "DirectionPolicy",
    "policy_for_state",
    "would_block_long",
    "would_block_short",
    "all_policies",
]
