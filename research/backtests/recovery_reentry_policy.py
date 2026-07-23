"""Research-only recovery/re-entry policy math for the inventory_mtm<-1 blocker audit.

Pure dataclasses + helper functions consumed by ``continuous_reentry_backtest.py``
and ``run_inventory_mtm_neg1_recovery_reentry_audit.py``. No live config, runtime,
or strategy default is ever touched by this module, and causal fills are never
altered here -- these helpers only decide *when* a fresh (already causal) backtest
trade should be started, never how any individual candle fills within a trade.

Variants
--------
``B0``: continuous re-entry unchanged (control).
``B1``: stop for good after the first recovered flat of the target blocker trade.
``B2``: cooldown window after the first recovered flat (skip re-entries inside it).
``B3``: only re-enter on the first later "fresh pullback" signal candle.
``B4``: only re-enter immediately if the recovered flat left a strictly clean book
    (``flat_no_active_orders`` + zero qty + no active orders).
``B5``: continuous like B0; the staged inventory-MTM freeze (A2 exposure freeze,
    escalating to an A1-style cycle freeze) is applied via ``InventoryMtmFreezeConfig``
    rather than by this module.

Only the FIRST flat of the coin's baseline blocker trade (``target_blocker_trade_number``)
ever branches away from plain continuous (B0) behaviour. Every other trade -- before
that recovery, or after it for B0/B5, or once recovery has already happened for any
variant -- continues exactly like the unmodified continuous loop.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .inventory_mtm_freeze import InventoryMtmFreezeConfig

RECOVERY_VARIANTS: tuple[str, ...] = ("B0", "B1", "B2", "B3", "B4", "B5")

_FLAT_QTY_EPS = 1e-9
_FLAT_EXIT_REASONS = frozenset({"flat_no_active_orders", "recovery_joint_exit"})


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class RecoveryReentryConfig:
    """Configuration for one recovery/re-entry variant run, scoped to one coin."""

    variant: str
    target_blocker_trade_number: int  # baseline blocker trade_number for THIS coin
    cooldown_candles: int = 500
    # long: reentry only accepted once close <= flat_mark * (1 - pct/100).
    fresh_pullback_pct: float = 0.5
    # B5 secondary conditions live on InventoryMtmFreezeConfig, but are mirrored
    # here (defaults kept in sync) so callers can build both configs from one place.
    secondary_hold_candles_below_threshold: int = 100
    secondary_mtm_threshold_usdt: float = -2.0
    secondary_exit_increase_count: int = 2

    def __post_init__(self) -> None:
        if self.variant not in RECOVERY_VARIANTS:
            raise ValueError(f"unknown recovery re-entry variant: {self.variant!r}")


def freeze_config_for_variant(variant: str) -> InventoryMtmFreezeConfig | None:
    """The inventory-MTM freeze config paired with one recovery re-entry variant."""
    if variant in {"B0", "B1", "B2", "B3", "B4"}:
        return InventoryMtmFreezeConfig(variant="A1")
    if variant == "B5":
        return InventoryMtmFreezeConfig(
            variant="A2",
            staged_cycle_freeze=True,
            secondary_hold_candles_below_threshold=100,
            secondary_mtm_threshold_usdt=-2.0,
            secondary_exit_increase_count=2,
        )
    return None


# ---------------------------------------------------------------------------
# Flat / trigger detection helpers
# ---------------------------------------------------------------------------


def _qty_is_flat(value: object) -> bool:
    return abs(safe_float(value, 0.0)) <= _FLAT_QTY_EPS


def is_fully_flat_result(result: Any) -> bool:
    """True iff ``result`` closed via a recognised flat exit with ~0 residual qty."""
    exit_reason = str(getattr(result, "exit_reason", "") or "")
    if exit_reason not in _FLAT_EXIT_REASONS:
        return False
    return _qty_is_flat(getattr(result, "final_long_qty", None)) and _qty_is_flat(
        getattr(result, "final_short_qty", None)
    )


def previous_trade_is_clean_flat(result: Any) -> bool:
    """B4 clean-state gate: strict ``flat_no_active_orders`` + zero qty + no active orders."""
    if str(getattr(result, "exit_reason", "") or "") != "flat_no_active_orders":
        return False
    if not _qty_is_flat(getattr(result, "final_long_qty", None)):
        return False
    if not _qty_is_flat(getattr(result, "final_short_qty", None)):
        return False
    active_orders = getattr(result, "final_active_orders", None) or []
    return len(active_orders) == 0


def trigger_fired_for_result(result: Any) -> bool:
    excerpt = dict(getattr(result, "final_strategy_state_excerpt", None) or {})
    return bool(excerpt.get("inventory_mtm_trigger_event"))


def is_target_blocker_first_flat(
    *, result: Any, target_blocker_trade_number: int, already_recovered: bool
) -> bool:
    """True iff ``result`` is the (as-yet unseen) first recovered flat of the target blocker trade."""
    if already_recovered:
        return False
    if int(target_blocker_trade_number) < 0:
        return False
    if int(getattr(result, "trade_number", 0) or 0) != int(target_blocker_trade_number):
        return False
    if not trigger_fired_for_result(result):
        return False
    return is_fully_flat_result(result)


# ---------------------------------------------------------------------------
# Baseline blockers (read-only)
# ---------------------------------------------------------------------------


def load_baseline_blockers(path: str | Path) -> list[dict[str, Any]]:
    """Read the baseline ``blocker_trades.csv`` rows (coin, trade_number, mtm_pnl, ...)."""
    path_obj = Path(path)
    if not path_obj.exists():
        raise FileNotFoundError(f"baseline blocker_trades.csv not found: {path_obj}")
    with path_obj.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def baseline_blocker_trade_number_by_coin(rows: list[dict[str, Any]]) -> dict[str, int]:
    """coin -> baseline blocker trade_number (one blocker row expected per coin)."""
    mapping: dict[str, int] = {}
    for row in rows:
        coin = str(row.get("coin") or "").strip().upper()
        if not coin:
            continue
        mapping[coin] = int(safe_float(row.get("trade_number"), -1))
    return mapping


# ---------------------------------------------------------------------------
# Reentry gap-skip / fresh-signal / clean-state helpers (pure, unit-testable)
# ---------------------------------------------------------------------------


def min_next_start_index(end_index: int) -> int:
    """No same-candle reopen: the earliest legal next start is ``end_index + 1``."""
    return int(end_index) + 1


def resolve_cooldown_start_index(*, candidate_start_index: int, cooldown_until_index: int | None) -> int:
    """B2: jump straight past the cooldown window instead of starting a trade inside it."""
    if cooldown_until_index is None:
        return int(candidate_start_index)
    if int(candidate_start_index) <= int(cooldown_until_index):
        return int(cooldown_until_index) + 1
    return int(candidate_start_index)


def _candle_close(candle: Any) -> float | None:
    if candle is None:
        return None
    value = getattr(candle, "close", None)
    if value is None and isinstance(candle, dict):
        value = candle.get("close")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def resolve_flat_mark_price(
    candle_list: list[Any] | None, end_index: int | None, *, fallback: float | None = None
) -> float | None:
    """Best-effort close price at the absolute flat candle (``end_index``)."""
    if candle_list and end_index is not None and 0 <= int(end_index) < len(candle_list):
        close = _candle_close(candle_list[int(end_index)])
        if close is not None:
            return close
    return fallback


def find_fresh_pullback_start_index(
    *,
    candle_list: list[Any],
    from_index: int,
    flat_mark_price: float | None,
    fresh_pullback_pct: float,
) -> int | None:
    """B3: first absolute candle index >= ``from_index`` with a fresh long pullback.

    long: ``close <= flat_mark_price * (1 - fresh_pullback_pct / 100)``.
    """
    if not candle_list or flat_mark_price is None or float(flat_mark_price) <= 0:
        return None
    threshold = float(flat_mark_price) * (1.0 - float(fresh_pullback_pct) / 100.0)
    start = max(0, int(from_index))
    for index in range(start, len(candle_list)):
        close = _candle_close(candle_list[index])
        if close is not None and close <= threshold:
            return index
    return None


# ---------------------------------------------------------------------------
# Runtime state + orchestration (called once per finished trade)
# ---------------------------------------------------------------------------


@dataclass
class RecoveryReentryRuntimeState:
    recovered: bool = False
    recovered_trade_number: int | None = None
    cooldown_until_index: int | None = None
    reentry_events: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ReentryOutcome:
    should_break: bool
    next_start_index: int | None
    event: dict[str, Any] | None = None


def apply_recovery_policy_after_trade(
    *,
    result: Any,
    config: RecoveryReentryConfig,
    state: RecoveryReentryRuntimeState,
    candle_list: list[Any],
    default_next_start_index: int,
) -> ReentryOutcome:
    """Annotate ``result`` in place and decide the next loop step for one finished trade.

    Only the FIRST flat of ``config.target_blocker_trade_number`` ever branches away
    from plain continuous (B0) behaviour; see module docstring.
    """
    excerpt = dict(getattr(result, "final_strategy_state_excerpt", None) or {})

    if state.recovered:
        excerpt["post_recovery_trade"] = True
        result.final_strategy_state_excerpt = excerpt
        next_start = int(default_next_start_index)
        if config.variant == "B2":
            next_start = resolve_cooldown_start_index(
                candidate_start_index=next_start, cooldown_until_index=state.cooldown_until_index
            )
        return ReentryOutcome(should_break=False, next_start_index=next_start)

    if not is_target_blocker_first_flat(
        result=result,
        target_blocker_trade_number=config.target_blocker_trade_number,
        already_recovered=state.recovered,
    ):
        result.final_strategy_state_excerpt = excerpt
        return ReentryOutcome(should_break=False, next_start_index=int(default_next_start_index))

    end_index = int(getattr(result, "end_index", 0) or 0)
    flat_mark_price = resolve_flat_mark_price(
        candle_list, end_index, fallback=getattr(result, "final_price", None)
    )
    trade_number = int(getattr(result, "trade_number", 0) or 0)

    excerpt["recovered_flat_of_target_blocker"] = True
    excerpt["first_flat_candle_absolute"] = end_index
    excerpt["flat_mark_price"] = flat_mark_price
    state.recovered = True
    state.recovered_trade_number = trade_number

    variant = config.variant

    if variant in ("B0", "B5"):
        result.final_strategy_state_excerpt = excerpt
        return ReentryOutcome(should_break=False, next_start_index=int(default_next_start_index))

    if variant == "B1":
        excerpt["research_terminal_reason"] = "recovered_flat_terminal"
        result.final_strategy_state_excerpt = excerpt
        event = {
            "type": "recovered_flat_terminal",
            "variant": variant,
            "trade_number": trade_number,
            "end_index": end_index,
        }
        state.reentry_events.append(event)
        return ReentryOutcome(should_break=True, next_start_index=None, event=event)

    if variant == "B2":
        state.cooldown_until_index = end_index + int(config.cooldown_candles)
        next_start = resolve_cooldown_start_index(
            candidate_start_index=int(default_next_start_index),
            cooldown_until_index=state.cooldown_until_index,
        )
        event = {
            "type": "reentry_event",
            "reason": "cooldown_start",
            "variant": variant,
            "trade_number": trade_number,
            "end_index": end_index,
            "cooldown_until_index": state.cooldown_until_index,
            "resolved_next_start_index": next_start,
        }
        excerpt["reentry_event"] = event
        state.reentry_events.append(event)
        result.final_strategy_state_excerpt = excerpt
        return ReentryOutcome(should_break=False, next_start_index=next_start, event=event)

    if variant == "B3":
        found = find_fresh_pullback_start_index(
            candle_list=candle_list,
            from_index=min_next_start_index(end_index),
            flat_mark_price=flat_mark_price,
            fresh_pullback_pct=config.fresh_pullback_pct,
        )
        if found is None:
            excerpt["research_terminal_reason"] = "no_fresh_pullback_signal"
            result.final_strategy_state_excerpt = excerpt
            return ReentryOutcome(should_break=True, next_start_index=None)
        event = {
            "type": "fresh_signal_reentry",
            "variant": variant,
            "trade_number": trade_number,
            "end_index": end_index,
            "flat_mark_price": flat_mark_price,
            "fresh_pullback_pct": config.fresh_pullback_pct,
            "found_start_index": found,
        }
        excerpt["reentry_event"] = event
        state.reentry_events.append(event)
        result.final_strategy_state_excerpt = excerpt
        return ReentryOutcome(should_break=False, next_start_index=found, event=event)

    if variant == "B4":
        if not previous_trade_is_clean_flat(result):
            excerpt["research_terminal_reason"] = "not_clean_flat_state"
            result.final_strategy_state_excerpt = excerpt
            return ReentryOutcome(should_break=True, next_start_index=None)
        event = {
            "type": "state_reset_ok",
            "variant": variant,
            "trade_number": trade_number,
            "end_index": end_index,
        }
        excerpt["reentry_event"] = event
        state.reentry_events.append(event)
        result.final_strategy_state_excerpt = excerpt
        return ReentryOutcome(
            should_break=False, next_start_index=int(default_next_start_index), event=event
        )

    result.final_strategy_state_excerpt = excerpt
    return ReentryOutcome(should_break=False, next_start_index=int(default_next_start_index))


# ---------------------------------------------------------------------------
# Metric helpers for recovery PnL splits (used by the audit runner + tests)
# ---------------------------------------------------------------------------


def series_mtm_if_stopped_at_first_recovered_flat(
    *,
    trade_rows: list[dict[str, Any]],
    target_blocker_trade_number: int,
    recovered: bool,
) -> float:
    """Sum of ``mtm_pnl`` up to/including the target trade if recovered, else full series."""
    if not recovered:
        return sum(safe_float(row.get("mtm_pnl")) for row in trade_rows)
    return sum(
        safe_float(row.get("mtm_pnl"))
        for row in trade_rows
        if int(safe_float(row.get("trade_number"), 0)) <= int(target_blocker_trade_number)
    )


def post_recovery_trade_pnl(*, series_mtm: float, series_mtm_if_stopped: float) -> float:
    return float(series_mtm) - float(series_mtm_if_stopped)


def count_new_blockers_after_recovery(
    *, trade_rows: list[dict[str, Any]], target_blocker_trade_number: int
) -> int:
    """Open (blocker) trades with ``trade_number > target``, i.e. new blockers post-recovery."""
    return sum(
        1
        for row in trade_rows
        if int(safe_float(row.get("trade_number"), 0)) > int(target_blocker_trade_number)
        and int(safe_float(row.get("is_blocker"), 0))
    )
