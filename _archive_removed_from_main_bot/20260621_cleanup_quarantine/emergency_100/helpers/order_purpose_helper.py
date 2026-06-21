from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from emergency_100.final_hedge_strategy import PSRHStrategy


def is_spread_heal_short_order(
    strategy: PSRHStrategy, order: dict[str, Any] | None
) -> bool:
    purpose = strategy._normalize_purpose_name((order or {}).get("purpose"))
    return purpose == "spread_heal_short"


def is_spread_heal_long_order(
    strategy: PSRHStrategy, order: dict[str, Any] | None
) -> bool:
    purpose = strategy._normalize_purpose_name((order or {}).get("purpose"))
    return purpose == "spread_heal_long"


def is_paired_partial_sl_long_order(
    strategy: PSRHStrategy, order: dict[str, Any] | None
) -> bool:
    purpose = strategy._normalize_purpose_name((order or {}).get("purpose"))
    return purpose == "paired_partial_sl_long"


def is_paired_partial_sl_short_order(
    strategy: PSRHStrategy, order: dict[str, Any] | None
) -> bool:
    purpose = strategy._normalize_purpose_name((order or {}).get("purpose"))
    return purpose == "paired_partial_sl_short"


def is_paired_long_close_order(
    strategy: PSRHStrategy, order: dict[str, Any] | None
) -> bool:
    purpose = strategy._normalize_purpose_name((order or {}).get("purpose"))
    return purpose in {
        "paired_long_close",
        "paired_partial_sl_long",
        "paired_partial_sl_short",
    }
