"""Entry order lifecycle — no next-open fill as substitute for pool level."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class FillResult:
    status: str
    fill_utc: str | None
    fill_price: float | None
    fill_bar_open: float | None
    ambiguous_entry_exit_same_bar: bool
    rule_id: str
    note: str


def bar_times_sorted(prices: dict[str, dict]) -> list[str]:
    return sorted(prices.keys())


def simulate_limit_touch_v1(
    direction: str,
    entry_level: float,
    order_active_from_utc: str,
    signal_close_utc: str,
    prices: dict[str, dict],
    pilot_to: str,
) -> FillResult:
    """
    DIAGNOSTIC_LIMIT_TOUCH_V1 (not user-approved final execution):
    Limit at pool level; active from order_active_from_utc (typically next 5m open after signal).
    Fill on first bar where H/L bracket entry_level, at entry_level price.
    """
    times = bar_times_sorted(prices)
    started = False
    for t in times:
        if t < order_active_from_utc:
            continue
        if t > pilot_to:
            break
        started = True
        bar = prices[t]
        lo, hi = bar["l"], bar["h"]
        if lo <= entry_level <= hi:
            amb = t == signal_close_utc
            return FillResult(
                status="ORDER_FILLED",
                fill_utc=t,
                fill_price=entry_level,
                fill_bar_open=bar["o"],
                ambiguous_entry_exit_same_bar=amb,
                rule_id="EXEC-DIAG-LIMIT-TOUCH-V1",
                note="Diagnostic only; entry_execution_user_approved=false",
            )
    if not started:
        return FillResult(
            status="ORDER_NOT_ACTIVE_IN_WINDOW",
            fill_utc=None,
            fill_price=None,
            fill_bar_open=None,
            ambiguous_entry_exit_same_bar=False,
            rule_id="EXEC-DIAG-LIMIT-TOUCH-V1",
            note="No bars at/after activation in pilot window",
        )
    return FillResult(
        status="ORDER_NOT_FILLED",
        fill_utc=None,
        fill_price=None,
        fill_bar_open=None,
        ambiguous_entry_exit_same_bar=False,
        rule_id="EXEC-DIAG-LIMIT-TOUCH-V1",
        note="Pool level not touched before pilot end",
    )


def simulate_entry(
    cfg: dict[str, Any],
    direction: str,
    entry_level: float,
    order_active_from_utc: str,
    signal_close_utc: str,
    prices: dict[str, dict],
    *,
    pool_lower: float,
    pool_upper: float,
    pool_identity: str,
    lifecycle=None,
) -> FillResult:
    variant = cfg.get("entry_execution_variant") or ""
    if variant == "USER_PENDING_LIMIT_V1" and lifecycle is not None:
        from pending_limit_simulator import simulate_user_pending_limit

        r = simulate_user_pending_limit(
            direction,
            entry_level,
            pool_lower,
            pool_upper,
            pool_identity,
            order_active_from_utc,
            prices,
            cfg["pilot_to_utc"],
            lifecycle,
        )
        return FillResult(
            status=r.status,
            fill_utc=r.fill_utc,
            fill_price=r.fill_price,
            fill_bar_open=None,
            ambiguous_entry_exit_same_bar=r.status == "AMBIGUOUS_POOL_FILL_SEQUENCE",
            rule_id=r.rule_id,
            note=r.note,
        )
    if not cfg.get("entry_execution_user_approved"):
        if variant == "DIAGNOSTIC_LIMIT_TOUCH_V1":
            return simulate_limit_touch_v1(
                direction,
                entry_level,
                order_active_from_utc,
                signal_close_utc,
                prices,
                cfg["pilot_to_utc"],
            )
        return FillResult(
            status="ENTRY_EXECUTION_UNRESOLVED",
            fill_utc=None,
            fill_price=None,
            fill_bar_open=None,
            ambiguous_entry_exit_same_bar=False,
            rule_id="EXEC-UNRESOLVED",
            note="No approved entry execution variant",
        )
    return simulate_limit_touch_v1(
        direction,
        entry_level,
        order_active_from_utc,
        signal_close_utc,
        prices,
        cfg["pilot_to_utc"],
    )
