from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from emergency_100.final_hedge_strategy import PSRHStrategy


def handle_post_fill_follow_up_hook(
    strategy: PSRHStrategy, client_order_id: str, purpose: str | None, source: str
) -> None:
    with strategy._order_lock:
        order = strategy.active_orders.get(client_order_id)
        if not order:
            return
        metadata = order.setdefault("metadata", {})
        if metadata.get("post_fill_follow_up_handled"):
            return
        metadata["post_fill_follow_up_handled"] = True
        reference_price = order.get("price")

    if not reference_price or reference_price <= 0:
        reference_price = strategy.last_price
    if not reference_price or reference_price <= 0:
        strategy.logger.warning(
            "Skipping fill-aware hedge follow-up: missing reference price",
            extra={
                "event": "follow_up_disabled",
                "client_order_id": client_order_id,
                "purpose": purpose,
                "source": source,
                "reason": "missing_reference_price",
                "result": "skipped",
            },
        )
        return

    strategy.logger.info(
        "Post-fill auto follow-up disabled under final strategy",
        extra={
            "event": "follow_up_disabled",
            "client_order_id": client_order_id,
            "purpose": purpose,
            "source": source,
            "reference_price": reference_price,
            "reason": "final_strategy_disabled",
            "result": "no_op",
        },
    )
    return
