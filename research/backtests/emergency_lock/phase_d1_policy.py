"""Phase D.1 defensive micro-unlock policy (offline research only).

Stage-1 break-even confirmation (conservative)
----------------------------------------------
After unlocking short qty ``Q`` at fill ``F`` (buy-to-close), the unlocked
exposure is economically a net-long of size ``Q``. Confirmation marks that
net-long at an *adverse* exit (sell slippage) and subtracts round-trip fees:

    mark_exit = F-reference close with short-side (sell) slippage
    stage_1_gross_pnl = Q * (mark_exit - F)
    stage_1_fee_cost  = unlock_fee_paid + fee(mark_exit, Q)
    stage_1_net_pnl   = stage_1_gross_pnl - stage_1_fee_cost
    stage_1_break_even_confirmed <=> stage_1_net_pnl >= 0

No mid-price / best-case marks. EMA and swing values are causal only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .cost_model import (
    conservative_relock_short_fill_price,
    conservative_short_close_fill_price,
    fee_usdt,
    apply_short_open_slippage,
)
from .phase_d_signals import (
    SignalContext,
    SwingBreakWithEmaSignal,
    causal_ema_series,
    confirmed_swing_lows,
)
from .position_ledger import PositionLedger
from .signals import tranche_qty_from_full_lock


POLICY_LOCKED = "LOCKED"
POLICY_MICRO_STAGE_1_OPEN = "MICRO_STAGE_1_OPEN"
POLICY_MICRO_STAGE_1_CONFIRMED = "MICRO_STAGE_1_CONFIRMED"
POLICY_MICRO_STAGE_2_OPEN = "MICRO_STAGE_2_OPEN"
POLICY_RELOCKED = "RELOCKED"
POLICY_DONE = "DONE"

TERMINAL_BASKET = frozenset(
    {
        "CLOSED_BREAK_EVEN",
        "STOPPED_TIMEOUT",
        "STOPPED_MAX_ADDED_LOSS",
        "OPEN_AT_DATA_END",
    }
)


@dataclass(frozen=True)
class MicroUnlockConfig:
    """Explicit D.1 policy parameters — no magic numbers in the engine."""

    variant_name: str
    stage_1_unlock_pct: float = 0.10
    stage_2_unlock_pct: float = 0.0
    max_total_unlock_pct: float = 0.10
    max_unlock_stages: int = 1
    minimum_bars_before_stage_2: int = 6
    minimum_bars_after_relock: int = 12
    require_stage_1_break_even_after_fees: bool = True
    require_new_higher_swing_for_stage_2: bool = True
    require_close_above_ema20: bool = True
    require_ema9_gte_ema20: bool = True
    relock_on_break_level_loss: bool = True
    relock_on_two_closes_below_ema20: bool = True
    relock_on_invalidation_low: bool = True
    max_unlock_attempts_after_relock: int = 1
    ema_fast: int = 9
    ema_slow: int = 20
    swing_left_bars: int = 3
    swing_right_bars: int = 3

    def as_public_dict(self) -> dict[str, Any]:
        return asdict(self)


def micro_unlock_configs() -> dict[str, MicroUnlockConfig]:
    return {
        "micro_unlock_10": MicroUnlockConfig(
            variant_name="micro_unlock_10",
            stage_1_unlock_pct=0.10,
            stage_2_unlock_pct=0.0,
            max_total_unlock_pct=0.10,
            max_unlock_stages=1,
        ),
        "micro_unlock_10_10": MicroUnlockConfig(
            variant_name="micro_unlock_10_10",
            stage_1_unlock_pct=0.10,
            stage_2_unlock_pct=0.10,
            max_total_unlock_pct=0.20,
            max_unlock_stages=2,
        ),
        "micro_unlock_10_15": MicroUnlockConfig(
            variant_name="micro_unlock_10_15",
            stage_1_unlock_pct=0.10,
            stage_2_unlock_pct=0.15,
            max_total_unlock_pct=0.25,
            max_unlock_stages=2,
        ),
    }


def stage_1_mark_pnl(
    *,
    unlock_fill: float,
    qty: float,
    mark_close: float,
    fee_rate: float,
    slippage_bps: float,
    unlock_fee_paid: float,
) -> dict[str, float | bool]:
    """Conservative stage-1 confirmation metrics (see module docstring)."""
    mark_exit = apply_short_open_slippage(
        reference_price=float(mark_close), slippage_bps=slippage_bps
    )
    gross = float(qty) * (float(mark_exit) - float(unlock_fill))
    exit_fee = fee_usdt(fill_price=mark_exit, qty=qty, fee_rate=fee_rate)
    fee_cost = float(unlock_fee_paid) + float(exit_fee)
    net = gross - fee_cost
    return {
        "stage_1_gross_pnl": gross,
        "stage_1_fee_cost": fee_cost,
        "stage_1_net_pnl": net,
        "stage_1_break_even_confirmed": bool(net >= -1e-12),
        "stage_1_mark_exit": float(mark_exit),
    }


@dataclass
class StageSnapshot:
    stage: int
    unlock_pct: float
    cumulative_unlock_pct: float
    fill_price: float
    break_level: float
    invalidation_low: float | None
    ema9: float | None
    ema20: float | None
    swing_high: float | None
    bar_index: int
    timestamp: str | None
    qty: float
    unlock_fee: float


@dataclass
class MicroUnlockEngine:
    """Causal micro-unlock state machine driven by ``swing_break_with_ema``."""

    policy: MicroUnlockConfig
    ledger: PositionLedger
    fee_rate: float
    slippage_bps: float
    qty_tolerance: float = 1e-12
    max_post_lock_bars: int = 5000
    basket_exit_target_usdt: float = 0.0
    basket_exit_buffer_usdt: float = 0.05

    policy_state: str = POLICY_LOCKED
    basket_state: str = "FULL_LOCK"
    full_lock_short_qty: float = 0.0
    post_lock_start_index: int | None = None
    bars_since_lock: int = 0
    cooldown_bars_remaining: int = 0
    bars_since_stage_1: int | None = None
    below_ema20_streak: int = 0
    unlock_attempt_cycles: int = 0  # completed cycle starts; post-relock limited
    post_relock_attempts_used: int = 0
    stage_1: StageSnapshot | None = None
    stage_2: StageSnapshot | None = None
    cumulative_unlock_pct: float = 0.0
    max_open_unlock_pct: float = 0.0
    open_unlock_qty: float = 0.0
    active_break_level: float | None = None
    active_invalidation_low: float | None = None
    used_swing_highs: list[float] = field(default_factory=list)
    stage_1_be_ever: bool = False
    stage_1_pnl_last: dict[str, float | bool] | None = None
    transitions: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    signal: SwingBreakWithEmaSignal = field(default_factory=SwingBreakWithEmaSignal)
    basket_pnl_at_lock: float | None = None
    minimum_basket_pnl_after_lock: float | None = None
    max_added_loss_after_lock: float = 0.0
    break_even_reached: bool = False
    break_even_timestamp: str | None = None
    bars_lock_to_break_even: int | None = None
    final_realized_net_pnl: float | None = None
    relock_count: int = 0
    unlock_count: int = 0
    stage_1_unlock_count: int = 0
    stage_2_unlock_count: int = 0
    last_relock_reason: str | None = None
    last_relock_bar: int | None = None
    last_relock_price: float | None = None
    bars_to_relock_from_stage_1: int | None = None
    stage_2_trigger_reason: str | None = None
    bars_to_stage_2: int | None = None
    signal_bar_stage_1: int | None = None

    def __post_init__(self) -> None:
        self.signal = SwingBreakWithEmaSignal(
            ema_fast=self.policy.ema_fast,
            ema_slow=self.policy.ema_slow,
        )
        self.signal.swing.left = self.policy.swing_left_bars
        self.signal.swing.right = self.policy.swing_right_bars
        self.signal.swing.minimum_bars_between_unlock_stages = (
            self.policy.minimum_bars_before_stage_2
        )
        self.signal.reset()

    def _record_transition(
        self,
        *,
        timestamp: str | None,
        bar_index: int,
        action: str,
        reason: str,
        **extra: Any,
    ) -> None:
        self.transitions.append(
            {
                "timestamp": timestamp,
                "bar_index": bar_index,
                "policy_state_from": extra.pop("state_from", None),
                "policy_state_to": self.policy_state,
                "basket_state": self.basket_state,
                "action": action,
                "reason": reason,
                **extra,
            }
        )

    def _update_loss(self, mark: float) -> None:
        if self.basket_pnl_at_lock is None:
            return
        basket = self.ledger.basket_net_pnl(mark)
        if self.minimum_basket_pnl_after_lock is None:
            self.minimum_basket_pnl_after_lock = basket
        else:
            self.minimum_basket_pnl_after_lock = min(
                float(self.minimum_basket_pnl_after_lock), basket
            )
        added = max(float(self.basket_pnl_at_lock) - basket, 0.0)
        self.max_added_loss_after_lock = max(self.max_added_loss_after_lock, added)

    def _ema(self, candles: list[dict[str, Any]], index: int) -> tuple[float | None, float | None]:
        closes = [float(c["close"]) for c in candles[: index + 1]]
        fast = causal_ema_series(closes, self.policy.ema_fast)
        slow = causal_ema_series(closes, self.policy.ema_slow)
        return fast[index], slow[index]

    def _signal_context(
        self, candles: list[dict[str, Any]], index: int
    ) -> SignalContext:
        assert self.post_lock_start_index is not None
        return SignalContext(
            candles=candles[: index + 1],
            index=index,
            post_lock_start_index=self.post_lock_start_index,
            long_avg=float(self.ledger.long_avg),
            short_avg=float(self.ledger.short_avg),
            long_qty=float(self.ledger.long_qty),
            short_qty=float(self.ledger.short_qty),
            next_unlock_stage=0 if self.stage_1 is None else 1,
            last_unlock_fill=None if self.stage_1 is None else self.stage_1.fill_price,
            last_unlock_reference=(
                None if self.stage_1 is None else self.stage_1.break_level
            ),
            bars_since_last_unlock=self.bars_since_stage_1,
            post_lock_low=None,
            unlock_rebound_pcts=(),
            full_lock_short_qty=float(self.full_lock_short_qty),
        )

    def enter_lock(
        self,
        *,
        timestamp: str | None,
        bar_index: int,
        mark: float,
    ) -> None:
        self.full_lock_short_qty = float(self.ledger.short_qty)
        self.post_lock_start_index = bar_index
        self.basket_pnl_at_lock = self.ledger.basket_net_pnl(mark)
        self.minimum_basket_pnl_after_lock = self.basket_pnl_at_lock
        self.policy_state = POLICY_LOCKED
        self.basket_state = "FULL_LOCK"
        self._record_transition(
            timestamp=timestamp,
            bar_index=bar_index,
            action="enter_full_lock",
            reason="emergency_lock",
            state_from="PRE_EMERGENCY",
        )

    def _update_stage_1_pnl(
        self,
        mark_close: float,
        *,
        timestamp: str | None = None,
        bar_index: int | None = None,
    ) -> dict[str, float | bool] | None:
        if self.stage_1 is None or self.open_unlock_qty <= self.qty_tolerance:
            return None
        metrics = stage_1_mark_pnl(
            unlock_fill=self.stage_1.fill_price,
            qty=self.open_unlock_qty,
            mark_close=mark_close,
            fee_rate=self.fee_rate,
            slippage_bps=self.slippage_bps,
            unlock_fee_paid=self.stage_1.unlock_fee,
        )
        self.stage_1_pnl_last = metrics
        if metrics["stage_1_break_even_confirmed"]:
            self.stage_1_be_ever = True
            if self.policy_state == POLICY_MICRO_STAGE_1_OPEN:
                prev = self.policy_state
                self.policy_state = POLICY_MICRO_STAGE_1_CONFIRMED
                self._record_transition(
                    timestamp=timestamp,
                    bar_index=bar_index if bar_index is not None else -1,
                    action="stage_1_confirmed",
                    reason="stage_1_break_even_after_fees",
                    state_from=prev,
                    **{k: metrics[k] for k in metrics},
                )
        return metrics

    def _relock_conditions(
        self, *, close: float, ema20: float | None, prev_close: float | None, prev_ema20: float | None
    ) -> str | None:
        if self.open_unlock_qty <= self.qty_tolerance:
            return None
        if (
            self.policy.relock_on_break_level_loss
            and self.active_break_level is not None
            and close < float(self.active_break_level)
        ):
            return "close_below_break_level"
        if self.policy.relock_on_invalidation_low and self.active_invalidation_low is not None:
            if close < float(self.active_invalidation_low):
                return "close_below_invalidation_low"
        if self.policy.relock_on_two_closes_below_ema20 and ema20 is not None:
            if close < float(ema20):
                self.below_ema20_streak += 1
            else:
                self.below_ema20_streak = 0
            if self.below_ema20_streak >= 2:
                return "two_closes_below_ema20"
        else:
            # Still reset streak if feature disabled but we track for diagnostics
            if ema20 is not None and close >= float(ema20):
                self.below_ema20_streak = 0
        _ = (prev_close, prev_ema20)
        return None

    def _execute_relock(
        self,
        *,
        timestamp: str | None,
        candle: dict[str, Any],
        bar_index: int,
        mark: float,
        reason: str,
    ) -> None:
        qty = float(self.open_unlock_qty)
        if qty <= self.qty_tolerance:
            return
        # Cap so short never exceeds long.
        max_add = max(float(self.ledger.long_qty) - float(self.ledger.short_qty), 0.0)
        qty = min(qty, max_add)
        if qty <= self.qty_tolerance:
            return
        trigger = float(self.active_break_level or mark)
        fill = conservative_relock_short_fill_price(
            trigger_price=trigger,
            candle_low=float(candle["low"]),
            slippage_bps=self.slippage_bps,
        )
        before = self.ledger.basket_net_pnl(mark)
        fees_before = float(self.ledger.total_fees)
        self.ledger.open_short(
            qty=qty,
            fill_price=fill,
            fee_rate=self.fee_rate,
            reference_price=trigger,
            fee_bucket="relock",
        )
        after = self.ledger.basket_net_pnl(mark)
        self.relock_count += 1
        self.last_relock_reason = reason
        self.last_relock_bar = bar_index
        self.last_relock_price = fill
        if self.bars_to_relock_from_stage_1 is None and self.bars_since_stage_1 is not None:
            self.bars_to_relock_from_stage_1 = int(self.bars_since_stage_1)
        self.open_unlock_qty = 0.0
        self.cumulative_unlock_pct = 0.0
        self.active_break_level = None
        self.active_invalidation_low = None
        self.below_ema20_streak = 0
        self.stage_1 = None
        self.stage_2 = None
        self.bars_since_stage_1 = None
        self.cooldown_bars_remaining = int(self.policy.minimum_bars_after_relock)
        # Preserve used swing highs so they cannot be reused.
        floor = max(self.used_swing_highs) if self.used_swing_highs else None
        self.signal.reset()
        if floor is not None:
            self.signal.swing._last_used_swing_high = float(floor)
        prev = self.policy_state
        # If the post-relock attempt budget is already exhausted, freeze policy.
        if self.post_relock_attempts_used >= int(self.policy.max_unlock_attempts_after_relock):
            self.policy_state = POLICY_DONE
            self.basket_state = "RELOCKED"
            done_reason = "max_unlock_attempts_after_relock"
        else:
            self.policy_state = POLICY_RELOCKED
            self.basket_state = "RELOCKED"
            done_reason = None
        self.actions.append(
            {
                "timestamp": timestamp,
                "bar_index": bar_index,
                "action": "relock_short",
                "reason": reason,
                "fill_price": fill,
                "qty": qty,
                "basket_pnl_before": before,
                "basket_pnl_after": after,
                "fee_delta": float(self.ledger.total_fees) - fees_before,
            }
        )
        self._record_transition(
            timestamp=timestamp,
            bar_index=bar_index,
            action="relock_short",
            reason=reason,
            state_from=prev,
            fill_price=fill,
            qty=qty,
        )
        if done_reason is not None:
            self._record_transition(
                timestamp=timestamp,
                bar_index=bar_index,
                action="policy_done",
                reason=done_reason,
                state_from=POLICY_RELOCKED,
            )

    def _execute_unlock(
        self,
        *,
        timestamp: str | None,
        candle: dict[str, Any],
        bar_index: int,
        mark: float,
        stage: int,
        unlock_pct: float,
        break_level: float,
        swing_high: float | None,
        ema9: float | None,
        ema20: float | None,
        invalidation_low: float | None,
        reason: str,
    ) -> bool:
        projected = self.cumulative_unlock_pct + float(unlock_pct)
        if projected - float(self.policy.max_total_unlock_pct) > 1e-12:
            return False
        qty = tranche_qty_from_full_lock(
            full_lock_short_qty=self.full_lock_short_qty,
            unlock_step_fraction=unlock_pct,
        )
        qty = min(qty, float(self.ledger.short_qty))
        if qty <= self.qty_tolerance:
            return False
        fill = conservative_short_close_fill_price(
            trigger_price=float(break_level),
            candle_high=float(candle["high"]),
            slippage_bps=self.slippage_bps,
        )
        before = self.ledger.basket_net_pnl(mark)
        fees_before = float(self.ledger.total_fees)
        ev = self.ledger.close_short(
            qty=qty,
            fill_price=fill,
            fee_rate=self.fee_rate,
            reference_price=float(break_level),
            fee_bucket="unlock_closing",
        )
        unlock_fee = float(ev["fee"])
        after = self.ledger.basket_net_pnl(mark)
        snap = StageSnapshot(
            stage=stage,
            unlock_pct=float(unlock_pct),
            cumulative_unlock_pct=projected,
            fill_price=fill,
            break_level=float(break_level),
            invalidation_low=invalidation_low,
            ema9=ema9,
            ema20=ema20,
            swing_high=swing_high,
            bar_index=bar_index,
            timestamp=timestamp,
            qty=float(ev["qty"]),
            unlock_fee=unlock_fee,
        )
        self.cumulative_unlock_pct = projected
        self.max_open_unlock_pct = max(self.max_open_unlock_pct, projected)
        self.open_unlock_qty += float(ev["qty"])
        self.active_break_level = float(break_level)
        if invalidation_low is not None:
            self.active_invalidation_low = float(invalidation_low)
        if swing_high is not None:
            self.used_swing_highs.append(float(swing_high))
        self.unlock_count += 1
        prev = self.policy_state
        if stage == 1:
            if self.relock_count > 0:
                self.post_relock_attempts_used += 1
            self.stage_1 = snap
            self.stage_1_unlock_count += 1
            self.bars_since_stage_1 = 0
            self.signal_bar_stage_1 = bar_index
            self.policy_state = POLICY_MICRO_STAGE_1_OPEN
            self.basket_state = "PARTIAL_UNLOCK"
            self.unlock_attempt_cycles += 1
        else:
            self.stage_2 = snap
            self.stage_2_unlock_count += 1
            self.bars_to_stage_2 = self.bars_since_stage_1
            self.stage_2_trigger_reason = reason
            self.policy_state = POLICY_MICRO_STAGE_2_OPEN
            self.basket_state = "PARTIAL_UNLOCK"
        self.actions.append(
            {
                "timestamp": timestamp,
                "bar_index": bar_index,
                "action": "unlock_short",
                "reason": reason,
                "stage": stage,
                "unlock_pct": unlock_pct,
                "cumulative_unlock_pct": projected,
                "fill_price": fill,
                "qty": float(ev["qty"]),
                "break_level": break_level,
                "swing_high": swing_high,
                "basket_pnl_before": before,
                "basket_pnl_after": after,
                "fee_delta": float(self.ledger.total_fees) - fees_before,
            }
        )
        self._record_transition(
            timestamp=timestamp,
            bar_index=bar_index,
            action="unlock_short",
            reason=reason,
            state_from=prev,
            stage=stage,
            unlock_pct=unlock_pct,
            fill_price=fill,
            qty=float(ev["qty"]),
        )
        return True

    def _try_basket_exit(self, *, timestamp: str | None, mark: float, bar_index: int) -> bool:
        proj = self.ledger.project_full_close_net_pnl(
            reference_price=mark,
            fee_rate=self.fee_rate,
            slippage_bps=self.slippage_bps,
        )
        basket = float(proj["basket_pnl_before_exit"])
        projected = float(proj["projected_final_net_pnl_after_closing_costs"])
        if basket + 1e-12 < self.basket_exit_target_usdt + self.basket_exit_buffer_usdt:
            return False
        if projected + 1e-12 < self.basket_exit_target_usdt:
            return False
        long_qty = float(self.ledger.long_qty)
        short_qty = float(self.ledger.short_qty)
        if long_qty > self.qty_tolerance:
            self.ledger.close_long(
                qty=long_qty,
                fill_price=float(proj["long_fill"]),
                fee_rate=self.fee_rate,
                reference_price=mark,
                fee_bucket="final_exit",
            )
        if short_qty > self.qty_tolerance:
            self.ledger.close_short(
                qty=short_qty,
                fill_price=float(proj["short_fill"]),
                fee_rate=self.fee_rate,
                reference_price=mark,
                fee_bucket="final_exit",
            )
        self.final_realized_net_pnl = float(self.ledger.basket_net_pnl(mark))
        self.break_even_reached = True
        self.break_even_timestamp = timestamp
        self.bars_lock_to_break_even = self.bars_since_lock
        self.basket_state = "CLOSED_BREAK_EVEN"
        self.policy_state = POLICY_DONE
        self.open_unlock_qty = 0.0
        self._record_transition(
            timestamp=timestamp,
            bar_index=bar_index,
            action="basket_exit",
            reason="break_even",
            state_from=POLICY_MICRO_STAGE_1_OPEN,
        )
        return True

    def process_bar(
        self,
        *,
        timestamp: str | None,
        candle: dict[str, Any],
        candles: list[dict[str, Any]],
        bar_index: int,
        mark: float,
        is_last_bar: bool,
    ) -> None:
        if self.basket_state in TERMINAL_BASKET:
            return

        self.bars_since_lock += 1
        if self.bars_since_stage_1 is not None:
            self.bars_since_stage_1 += 1
        if self.cooldown_bars_remaining > 0:
            self.cooldown_bars_remaining -= 1
            if (
                self.cooldown_bars_remaining == 0
                and self.policy_state == POLICY_RELOCKED
            ):
                self.policy_state = POLICY_LOCKED
                self.basket_state = "FULL_LOCK"
                self._record_transition(
                    timestamp=timestamp,
                    bar_index=bar_index,
                    action="cooldown_complete",
                    reason="ready_for_new_signal",
                    state_from=POLICY_RELOCKED,
                )

        self._update_loss(mark)
        if self.bars_since_lock >= int(self.max_post_lock_bars):
            self.basket_state = "STOPPED_TIMEOUT"
            self.policy_state = POLICY_DONE
            return

        ema9, ema20 = self._ema(candles, bar_index)
        close = float(candle["close"])
        prev_close = float(candles[bar_index - 1]["close"]) if bar_index > 0 else None
        prev_ema20 = None
        if bar_index > 0:
            _, prev_ema20 = self._ema(candles, bar_index - 1)

        acted = False
        # --- Re-lock first ---
        if self.open_unlock_qty > self.qty_tolerance and self.policy_state not in {
            POLICY_DONE,
            POLICY_RELOCKED,
        }:
            # Update BE metrics while open
            be = self._update_stage_1_pnl(
                close, timestamp=timestamp, bar_index=bar_index
            )
            relock_reason = self._relock_conditions(
                close=close, ema20=ema20, prev_close=prev_close, prev_ema20=prev_ema20
            )
            if relock_reason is not None:
                self._execute_relock(
                    timestamp=timestamp,
                    candle=candle,
                    bar_index=bar_index,
                    mark=mark,
                    reason=relock_reason,
                )
                acted = True
            elif be is not None:
                self.diagnostics.append(
                    {
                        "timestamp": timestamp,
                        "bar_index": bar_index,
                        "policy_state": self.policy_state,
                        "ema_9": ema9,
                        "ema_20": ema20,
                        **be,
                    }
                )

        # --- Unlock ---
        can_unlock = (
            not acted
            and self.cooldown_bars_remaining == 0
            and self.policy_state
            in {POLICY_LOCKED, POLICY_MICRO_STAGE_1_CONFIRMED}
        )

        if can_unlock and self.policy_state == POLICY_LOCKED:
            ctx = self._signal_context(candles, bar_index)
            decision = self.signal.evaluate(ctx)
            swing = decision.metadata.get("swing_high")
            reused = swing is not None and any(
                abs(float(swing) - float(u)) < 1e-12 for u in self.used_swing_highs
            )
            self.diagnostics.append(
                {
                    "timestamp": timestamp,
                    "bar_index": bar_index,
                    "policy_state": self.policy_state,
                    "triggered": decision.triggered,
                    "reason": decision.reason,
                    "swing_high": swing,
                    "ema_9": decision.metadata.get("ema_9"),
                    "ema_20": decision.metadata.get("ema_20"),
                    "break_level": decision.reference_price,
                    "reused_swing_blocked": reused,
                }
            )
            if (
                decision.triggered
                and decision.reference_price is not None
                and not reused
            ):
                inv = decision.invalidation_price
                if inv is None:
                    lows = confirmed_swing_lows(
                        candles,
                        asof_index=bar_index,
                        left=self.policy.swing_left_bars,
                        right=self.policy.swing_right_bars,
                        start_index=int(self.post_lock_start_index or 0),
                    )
                    inv = float(lows[-1][1]) if lows else None
                ok = self._execute_unlock(
                    timestamp=timestamp,
                    candle=candle,
                    bar_index=bar_index,
                    mark=mark,
                    stage=1,
                    unlock_pct=self.policy.stage_1_unlock_pct,
                    break_level=float(decision.reference_price),
                    swing_high=float(swing) if swing is not None else None,
                    ema9=float(ema9) if ema9 is not None else None,
                    ema20=float(ema20) if ema20 is not None else None,
                    invalidation_low=float(inv) if inv is not None else None,
                    reason="micro_stage_1_swing_break_with_ema",
                )
                if ok:
                    self.signal.note_unlock(ctx, decision)
                    acted = True

        elif can_unlock and self.policy_state == POLICY_MICRO_STAGE_1_CONFIRMED:
            # Stage 2 gates
            if int(self.policy.max_unlock_stages) < 2:
                pass
            elif float(self.policy.stage_2_unlock_pct) <= 0.0:
                pass
            elif self.bars_since_stage_1 is None or self.bars_since_stage_1 < int(
                self.policy.minimum_bars_before_stage_2
            ):
                pass
            elif (
                self.policy.require_stage_1_break_even_after_fees
                and not self.stage_1_be_ever
            ):
                pass
            else:
                ctx = self._signal_context(candles, bar_index)
                decision = self.signal.evaluate(ctx)
                swing = decision.metadata.get("swing_high")
                break_level = decision.reference_price
                higher = True
                if self.policy.require_new_higher_swing_for_stage_2:
                    if swing is None or self.stage_1 is None or self.stage_1.swing_high is None:
                        higher = False
                    else:
                        higher = float(swing) > float(self.stage_1.swing_high) + 1e-12
                        if self.stage_1.break_level is not None and break_level is not None:
                            higher = higher and (
                                float(break_level)
                                > float(self.stage_1.break_level) + 1e-12
                            )
                ema_ok = True
                if self.policy.require_close_above_ema20:
                    ema_ok = ema20 is not None and close > float(ema20)
                if self.policy.require_ema9_gte_ema20:
                    ema_ok = ema_ok and (
                        ema9 is not None
                        and ema20 is not None
                        and float(ema9) >= float(ema20)
                    )
                self.diagnostics.append(
                    {
                        "timestamp": timestamp,
                        "bar_index": bar_index,
                        "policy_state": self.policy_state,
                        "triggered": decision.triggered,
                        "reason": decision.reason,
                        "new_higher_swing": higher,
                        "swing_high": swing,
                        "ema_9": ema9,
                        "ema_20": ema20,
                        "bars_since_stage_1": self.bars_since_stage_1,
                        "stage_1_be": self.stage_1_be_ever,
                    }
                )
                if (
                    decision.triggered
                    and higher
                    and ema_ok
                    and break_level is not None
                ):
                    inv = decision.invalidation_price
                    ok = self._execute_unlock(
                        timestamp=timestamp,
                        candle=candle,
                        bar_index=bar_index,
                        mark=mark,
                        stage=2,
                        unlock_pct=self.policy.stage_2_unlock_pct,
                        break_level=float(break_level),
                        swing_high=float(swing) if swing is not None else None,
                        ema9=float(ema9) if ema9 is not None else None,
                        ema20=float(ema20) if ema20 is not None else None,
                        invalidation_low=float(inv) if inv is not None else None,
                        reason="micro_stage_2_new_higher_swing_break_with_ema",
                    )
                    if ok:
                        self.signal.note_unlock(ctx, decision)
                        acted = True

        if self.basket_state not in TERMINAL_BASKET:
            if self._try_basket_exit(timestamp=timestamp, mark=mark, bar_index=bar_index):
                return

        if is_last_bar and self.basket_state not in TERMINAL_BASKET:
            self.basket_state = "OPEN_AT_DATA_END"
            if self.policy_state not in {POLICY_DONE}:
                # leave policy state as-is for diagnostics
                pass

    def assert_exposure_caps(self) -> None:
        assert self.max_open_unlock_pct <= float(self.policy.max_total_unlock_pct) + 1e-12
        assert self.cumulative_unlock_pct <= float(self.policy.max_total_unlock_pct) + 1e-12
        total_steps = float(self.policy.stage_1_unlock_pct) + float(
            self.policy.stage_2_unlock_pct
        )
        assert total_steps <= float(self.policy.max_total_unlock_pct) + 1e-12
