"""Explicit Emergency-Lock unlock / re-lock / break-even state machine.

States
------
PRE_EMERGENCY, FULL_LOCK, PARTIAL_UNLOCK, RELOCKED,
CLOSED_BREAK_EVEN, STOPPED_TIMEOUT, STOPPED_MAX_ADDED_LOSS, OPEN_AT_DATA_END

Conservative intrabar order (Phase B baseline)
----------------------------------------------
On each post-lock bar, after updating ``post_lock_low`` from ``candle.low``:

1. Decrement cooldown if active.
2. Check ``STOPPED_MAX_ADDED_LOSS`` / ``STOPPED_TIMEOUT`` (no forced flatten).
3. If an open unlock tranche can re-lock (``low <= relock_trigger``) **and**
   cooldown is zero: execute **re-lock first** (adverse vs unlocking).
4. Else if the next unlock stage is eligible: execute **at most one** unlock.
5. Never unlock and re-lock on the same bar.
6. Never chain multiple unlock stages on the same bar even if several
   rebound thresholds are cleared by the same high.
7. After the single action (or none), evaluate basket break-even using
   projected closing costs at ``close``; exit only if the projection clears
   the target (and mark basket clears target + buffer).

Stage / attempt semantics
-------------------------
``next_unlock_stage`` is the next open stage index. A successful unlock of
stage ``i`` advances it to ``i+1``. A re-lock of the last removed tranche
restores ``next_unlock_stage = i`` so the same stage can be retried after
cooldown. ``failed_unlocks`` counts re-locks; ``unlock_attempt_count``
counts every unlock execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .config import EmergencyLockRecoveryConfig
from .cost_model import (
    conservative_emergency_short_fill_price,
    conservative_long_close_fill_price,
    conservative_relock_short_fill_price,
    conservative_short_close_fill_price,
)
from .position_ledger import PositionLedger
from .signals import (
    distance_to_short_avg_pct,
    net_long_fraction,
    open_short_profit_usdt,
    relock_signal_touched,
    relock_trigger_price,
    tranche_qty_from_full_lock,
    unlock_reference_price,
    unlock_signal_touched,
)

TERMINAL_STATES = frozenset(
    {
        "CLOSED_BREAK_EVEN",
        "STOPPED_TIMEOUT",
        "STOPPED_MAX_ADDED_LOSS",
        "OPEN_AT_DATA_END",
    }
)


@dataclass
class StateTransition:
    timestamp: str | None
    state_from: str
    state_to: str
    action: str
    reason: str
    reference_price: float | None = None
    fill_price: float | None = None
    qty: float | None = None
    unlock_stage: int | None = None
    basket_pnl_before: float | None = None
    basket_pnl_after: float | None = None
    fees: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "state_from": self.state_from,
            "state_to": self.state_to,
            "action": self.action,
            "reason": self.reason,
            "reference_price": self.reference_price,
            "fill_price": self.fill_price,
            "qty": self.qty,
            "unlock_stage": self.unlock_stage,
            "basket_pnl_before": self.basket_pnl_before,
            "basket_pnl_after": self.basket_pnl_after,
            "fees": self.fees,
        }


@dataclass
class ActionRecord:
    timestamp: str | None
    action: str
    reason: str
    stage: int | None
    attempt: int | None
    reference_price: float | None
    fill_price: float | None
    qty: float | None
    long_qty_after: float
    short_qty_after: float
    short_avg_after: float
    realized_short_pnl_delta: float
    fee_delta: float
    basket_pnl_before: float
    basket_pnl_after: float
    added_loss_after_lock: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "action": self.action,
            "reason": self.reason,
            "stage": self.stage,
            "attempt": self.attempt,
            "reference_price": self.reference_price,
            "fill_price": self.fill_price,
            "qty": self.qty,
            "long_qty_after": self.long_qty_after,
            "short_qty_after": self.short_qty_after,
            "short_avg_after": self.short_avg_after,
            "realized_short_pnl_delta": self.realized_short_pnl_delta,
            "fee_delta": self.fee_delta,
            "basket_pnl_before": self.basket_pnl_before,
            "basket_pnl_after": self.basket_pnl_after,
            "added_loss_after_lock": self.added_loss_after_lock,
        }


@dataclass
class EmergencyLockStateMachine:
    cfg: EmergencyLockRecoveryConfig
    ledger: PositionLedger

    state: str = "PRE_EMERGENCY"
    emergency_trigger: float | None = None
    full_lock_short_qty: float = 0.0
    post_lock_low: float | None = None
    next_unlock_stage: int = 0
    unlock_count: int = 0
    unlock_attempt_count: int = 0
    relock_count: int = 0
    failed_unlocks: int = 0
    last_unlock_fill: float | None = None
    last_unlock_qty: float | None = None
    last_unlock_stage: int | None = None
    last_unlock_reference: float | None = None
    relock_trigger: float | None = None
    has_open_unlock_tranche: bool = False
    cooldown_bars_remaining: int = 0
    bars_since_lock: int = 0
    lock_bar_index: int | None = None
    basket_pnl_at_lock: float | None = None
    frozen_deficit_usdt: float | None = None
    lock_timestamp: str | None = None
    lock_price: float | None = None
    max_added_loss_after_lock: float = 0.0
    minimum_basket_pnl_after_lock: float | None = None
    unlock_confirm_streak: int = 0
    relock_confirm_streak: int = 0
    pending_unlock_stage: int | None = None
    pending_unlock_delay: int = 0
    break_even_timestamp: str | None = None
    bars_from_lock_to_break_even: int | None = None
    basket_pnl_before_exit: float | None = None
    projected_closing_fees: float | None = None
    projected_exit_slippage: float | None = None
    final_realized_net_pnl: float | None = None
    transitions: list[StateTransition] = field(default_factory=list)
    actions: list[ActionRecord] = field(default_factory=list)

    # Phase D optional hooks (None preserves Phase B rebound behaviour).
    unlock_signal: Any | None = None
    relock_mode_variant: str = "common_pct"  # common_pct | signal_invalidation
    simulation_candles: list[dict[str, Any]] | None = None
    simulation_index: int | None = None
    post_lock_start_index: int | None = None
    bars_since_last_unlock: int | None = None
    signal_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    signal_trigger_count: int = 0

    def _transition(
        self,
        *,
        timestamp: str | None,
        state_to: str,
        action: str,
        reason: str,
        reference_price: float | None = None,
        fill_price: float | None = None,
        qty: float | None = None,
        unlock_stage: int | None = None,
        basket_pnl_before: float | None = None,
        basket_pnl_after: float | None = None,
        fees: float | None = None,
    ) -> None:
        tr = StateTransition(
            timestamp=timestamp,
            state_from=self.state,
            state_to=state_to,
            action=action,
            reason=reason,
            reference_price=reference_price,
            fill_price=fill_price,
            qty=qty,
            unlock_stage=unlock_stage,
            basket_pnl_before=basket_pnl_before,
            basket_pnl_after=basket_pnl_after,
            fees=fees,
        )
        self.transitions.append(tr)
        self.state = state_to

    def _record_action(
        self,
        *,
        timestamp: str | None,
        action: str,
        reason: str,
        stage: int | None,
        attempt: int | None,
        reference_price: float | None,
        fill_price: float | None,
        qty: float | None,
        basket_pnl_before: float,
        basket_pnl_after: float,
        fee_delta: float,
        realized_short_pnl_delta: float,
    ) -> None:
        added = None
        if self.basket_pnl_at_lock is not None:
            added = max(float(self.basket_pnl_at_lock) - float(basket_pnl_after), 0.0)
        self.actions.append(
            ActionRecord(
                timestamp=timestamp,
                action=action,
                reason=reason,
                stage=stage,
                attempt=attempt,
                reference_price=reference_price,
                fill_price=fill_price,
                qty=qty,
                long_qty_after=float(self.ledger.long_qty),
                short_qty_after=float(self.ledger.short_qty),
                short_avg_after=float(self.ledger.short_avg),
                realized_short_pnl_delta=float(realized_short_pnl_delta),
                fee_delta=float(fee_delta),
                basket_pnl_before=float(basket_pnl_before),
                basket_pnl_after=float(basket_pnl_after),
                added_loss_after_lock=added,
            )
        )

    def planned_unlock_qty(self, stage: int) -> float:
        return tranche_qty_from_full_lock(
            full_lock_short_qty=self.full_lock_short_qty,
            unlock_step_fraction=float(self.cfg.unlock_steps[stage]),
        )

    def current_unlock_reference(self) -> float | None:
        if self.post_lock_low is None:
            return None
        if self.next_unlock_stage >= len(self.cfg.unlock_rebound_pcts):
            return None
        return unlock_reference_price(
            post_lock_low=self.post_lock_low,
            rebound_pct=float(self.cfg.unlock_rebound_pcts[self.next_unlock_stage]),
        )

    def metrics_at_mark(self, mark: float) -> dict[str, float | None]:
        unlock_ref = self.current_unlock_reference()
        ref_for_guards = unlock_ref if unlock_ref is not None else mark
        frac = net_long_fraction(
            long_qty=self.ledger.long_qty,
            short_qty=self.ledger.short_qty,
            full_lock_short_qty=self.full_lock_short_qty or 1.0,
        )
        added = None
        if self.basket_pnl_at_lock is not None:
            basket = self.ledger.basket_net_pnl(mark)
            added = max(float(self.basket_pnl_at_lock) - float(basket), 0.0)
        proj = self.ledger.project_full_close_net_pnl(
            reference_price=mark,
            fee_rate=self.cfg.fee_rate,
            slippage_bps=self.cfg.slippage_bps,
        )
        return {
            "unlock_reference": unlock_ref,
            "open_short_profit_usdt": open_short_profit_usdt(
                short_qty=self.ledger.short_qty,
                short_avg=self.ledger.short_avg,
                reference_price=ref_for_guards,
            ),
            "distance_to_short_avg_pct": distance_to_short_avg_pct(
                short_avg=self.ledger.short_avg,
                unlock_fill_reference=ref_for_guards,
            ),
            "net_long_qty": max(self.ledger.long_qty - self.ledger.short_qty, 0.0),
            "net_long_fraction": frac,
            "added_loss_after_lock": added,
            "projected_final_net_pnl_after_closing_costs": proj[
                "projected_final_net_pnl_after_closing_costs"
            ],
            "projected_closing_fees": proj["projected_closing_fees"],
            "projected_exit_slippage": proj["projected_exit_slippage"],
            "basket_pnl_before_exit": proj["basket_pnl_before_exit"],
        }

    def _update_post_lock_low(self, candle_low: float) -> None:
        low = float(candle_low)
        if self.post_lock_low is None:
            self.post_lock_low = low
        else:
            self.post_lock_low = min(float(self.post_lock_low), low)

    def _update_loss_trackers(self, mark: float) -> None:
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

    def enter_full_lock(
        self,
        *,
        timestamp: str | None,
        candle: dict[str, Any],
        fill_price: float,
        mark: float,
    ) -> None:
        before = self.ledger.basket_net_pnl(mark)
        self.full_lock_short_qty = float(self.ledger.short_qty)
        self.post_lock_low = float(candle["low"])
        self.basket_pnl_at_lock = self.ledger.basket_net_pnl(mark)
        self.frozen_deficit_usdt = (
            self.ledger.unrealized_long_pnl(mark)
            + self.ledger.unrealized_short_pnl(mark)
            + self.ledger.realized_long_pnl
            + self.ledger.realized_short_pnl
        )
        self.lock_timestamp = timestamp
        self.lock_price = float(fill_price)
        self.bars_since_lock = 0
        self.minimum_basket_pnl_after_lock = float(self.basket_pnl_at_lock)
        self.max_added_loss_after_lock = 0.0
        self._transition(
            timestamp=timestamp,
            state_to="FULL_LOCK",
            action="emergency_lock",
            reason="emergency_trigger",
            reference_price=self.emergency_trigger,
            fill_price=fill_price,
            qty=self.full_lock_short_qty,
            basket_pnl_before=before,
            basket_pnl_after=self.basket_pnl_at_lock,
            fees=float(self.ledger.lock_fees),
        )

    def _unlock_guards_ok(self, *, unlock_reference: float, qty: float) -> tuple[bool, str]:
        cfg = self.cfg
        # Cap tranche to remaining short.
        if qty > self.ledger.short_qty + cfg.qty_tolerance:
            return False, "tranche_exceeds_short_qty"
        if qty <= 0.0:
            return False, "non_positive_qty"

        projected_short = self.ledger.short_qty - qty
        frac = net_long_fraction(
            long_qty=self.ledger.long_qty,
            short_qty=projected_short,
            full_lock_short_qty=self.full_lock_short_qty,
        )
        if frac - float(cfg.maximum_net_long_fraction) > 1e-12:
            return False, "maximum_net_long_fraction"

        profit = open_short_profit_usdt(
            short_qty=self.ledger.short_qty,
            short_avg=self.ledger.short_avg,
            reference_price=unlock_reference,
        )
        # Default 0.0 disables the guard (do not require non-negative profit).
        if float(cfg.minimum_short_profit_buffer_usdt) > 0.0:
            if profit + 1e-12 < float(cfg.minimum_short_profit_buffer_usdt):
                return False, "minimum_short_profit_buffer"

        dist = distance_to_short_avg_pct(
            short_avg=self.ledger.short_avg,
            unlock_fill_reference=unlock_reference,
        )
        if float(cfg.minimum_distance_to_short_avg_pct) > 0.0:
            if dist + 1e-12 < float(cfg.minimum_distance_to_short_avg_pct):
                return False, "minimum_distance_to_short_avg"

        if self.failed_unlocks >= int(cfg.max_failed_unlocks):
            return False, "max_failed_unlocks"

        if self.cooldown_bars_remaining > 0:
            return False, "cooldown_active"

        return True, "ok"

    def _execute_unlock(
        self,
        *,
        timestamp: str | None,
        candle: dict[str, Any],
        stage: int,
        unlock_reference: float,
        mark: float,
    ) -> bool:
        cfg = self.cfg
        qty = self.planned_unlock_qty(stage)
        qty = min(qty, float(self.ledger.short_qty))
        ok, reason = self._unlock_guards_ok(unlock_reference=unlock_reference, qty=qty)
        if not ok:
            return False

        fill = conservative_short_close_fill_price(
            trigger_price=unlock_reference,
            candle_high=float(candle["high"]),
            slippage_bps=cfg.slippage_bps,
        )
        before = self.ledger.basket_net_pnl(mark)
        fees_before = float(self.ledger.total_fees)
        realized_before = float(self.ledger.realized_short_pnl)
        ev = self.ledger.close_short(
            qty=qty,
            fill_price=fill,
            fee_rate=cfg.fee_rate,
            reference_price=unlock_reference,
            fee_bucket="unlock_closing",
        )
        after = self.ledger.basket_net_pnl(mark)
        self.unlock_count += 1
        self.unlock_attempt_count += 1
        self.last_unlock_fill = fill
        self.last_unlock_qty = float(ev["qty"])
        self.last_unlock_stage = stage
        self.last_unlock_reference = unlock_reference
        self.relock_trigger = relock_trigger_price(
            last_unlock_fill=fill,
            relock_distance_pct=cfg.relock_distance_pct,
        )
        self.has_open_unlock_tranche = True
        self.next_unlock_stage = stage + 1
        self.unlock_confirm_streak = 0
        self.pending_unlock_stage = None
        self.pending_unlock_delay = 0
        self.bars_since_last_unlock = 0
        if self.unlock_signal is not None:
            from .phase_d_signals import SignalContext

            ctx = self._build_signal_context(mark=mark)
            if ctx is not None:
                # Reconstruct a decision-like object for note_unlock
                from .phase_d_signals import SignalDecision

                decision = SignalDecision(
                    True,
                    getattr(self.unlock_signal, "name", "signal"),
                    unlock_reference,
                    None,
                    "unlock",
                )
                self.unlock_signal.note_unlock(ctx, decision)

        self._transition(
            timestamp=timestamp,
            state_to="PARTIAL_UNLOCK",
            action="unlock_short",
            reason=f"rebound_stage_{stage}",
            reference_price=unlock_reference,
            fill_price=fill,
            qty=float(ev["qty"]),
            unlock_stage=stage,
            basket_pnl_before=before,
            basket_pnl_after=after,
            fees=float(ev["fee"]),
        )
        self._record_action(
            timestamp=timestamp,
            action="unlock_short",
            reason=reason if reason == "ok" else f"rebound_stage_{stage}",
            stage=stage,
            attempt=self.unlock_attempt_count,
            reference_price=unlock_reference,
            fill_price=fill,
            qty=float(ev["qty"]),
            basket_pnl_before=before,
            basket_pnl_after=after,
            fee_delta=float(self.ledger.total_fees) - fees_before,
            realized_short_pnl_delta=float(self.ledger.realized_short_pnl) - realized_before,
        )
        return True

    def _execute_relock(
        self,
        *,
        timestamp: str | None,
        candle: dict[str, Any],
        mark: float,
    ) -> bool:
        cfg = self.cfg
        if not self.has_open_unlock_tranche:
            return False
        if not cfg.relock_last_removed_tranche_only:
            raise NotImplementedError("only last-tranche relock is supported in Phase B")
        qty = float(self.last_unlock_qty or 0.0)
        trigger = float(self.relock_trigger or 0.0)
        if qty <= 0.0 or trigger <= 0.0:
            return False

        # No overhedge: short may not exceed long.
        max_add = max(float(self.ledger.long_qty) - float(self.ledger.short_qty), 0.0)
        qty = min(qty, max_add)
        if qty <= cfg.qty_tolerance:
            return False

        fill = conservative_relock_short_fill_price(
            trigger_price=trigger,
            candle_low=float(candle["low"]),
            slippage_bps=cfg.slippage_bps,
        )
        before = self.ledger.basket_net_pnl(mark)
        fees_before = float(self.ledger.total_fees)
        stage = self.last_unlock_stage
        ev = self.ledger.open_short(
            qty=qty,
            fill_price=fill,
            fee_rate=cfg.fee_rate,
            reference_price=trigger,
            fee_bucket="relock",
        )
        if self.ledger.short_qty > self.ledger.long_qty + cfg.qty_tolerance:
            raise RuntimeError("short overhedge after relock")

        after = self.ledger.basket_net_pnl(mark)
        self.relock_count += 1
        self.failed_unlocks += 1
        # Same stage becomes next open stage again.
        if stage is not None:
            self.next_unlock_stage = int(stage)
        self.has_open_unlock_tranche = False
        self.last_unlock_fill = None
        self.last_unlock_qty = None
        self.relock_trigger = None
        self.cooldown_bars_remaining = int(cfg.cooldown_bars_after_relock)
        self.relock_confirm_streak = 0
        self.unlock_confirm_streak = 0

        fully_locked = abs(self.ledger.long_qty - self.ledger.short_qty) <= cfg.qty_tolerance
        state_to = "RELOCKED" if fully_locked else "PARTIAL_UNLOCK"
        self._transition(
            timestamp=timestamp,
            state_to=state_to,
            action="relock_short",
            reason="relock_last_tranche",
            reference_price=trigger,
            fill_price=fill,
            qty=float(ev["qty"]),
            unlock_stage=stage,
            basket_pnl_before=before,
            basket_pnl_after=after,
            fees=float(ev["fee"]),
        )
        self._record_action(
            timestamp=timestamp,
            action="relock_short",
            reason="relock_last_tranche",
            stage=stage,
            attempt=self.unlock_attempt_count,
            reference_price=trigger,
            fill_price=fill,
            qty=float(ev["qty"]),
            basket_pnl_before=before,
            basket_pnl_after=after,
            fee_delta=float(self.ledger.total_fees) - fees_before,
            realized_short_pnl_delta=0.0,
        )
        return True

    def _try_basket_exit(self, *, timestamp: str | None, mark: float) -> bool:
        cfg = self.cfg
        if self.ledger.long_qty <= cfg.qty_tolerance and self.ledger.short_qty <= cfg.qty_tolerance:
            return False
        proj = self.ledger.project_full_close_net_pnl(
            reference_price=mark,
            fee_rate=cfg.fee_rate,
            slippage_bps=cfg.slippage_bps,
        )
        basket = float(proj["basket_pnl_before_exit"])
        projected = float(proj["projected_final_net_pnl_after_closing_costs"])
        target = float(cfg.basket_exit_target_usdt)
        buffer = float(cfg.basket_exit_buffer_usdt)

        # Soft mark check + hard projected check (no false BE before costs).
        if basket + 1e-12 < target + buffer:
            return False
        if projected + 1e-12 < target:
            return False

        before = basket
        fees_before = float(self.ledger.total_fees)
        long_qty = float(self.ledger.long_qty)
        short_qty = float(self.ledger.short_qty)
        long_fill = float(proj["long_fill"])
        short_fill = float(proj["short_fill"])

        if long_qty > cfg.qty_tolerance:
            self.ledger.close_long(
                qty=long_qty,
                fill_price=long_fill,
                fee_rate=cfg.fee_rate,
                reference_price=mark,
                fee_bucket="final_exit",
            )
        if short_qty > cfg.qty_tolerance:
            self.ledger.close_short(
                qty=short_qty,
                fill_price=short_fill,
                fee_rate=cfg.fee_rate,
                reference_price=mark,
                fee_bucket="final_exit",
            )

        final_pnl = self.ledger.basket_net_pnl(mark)
        if final_pnl + 1e-9 < target:
            # Should be rare (projection mismatch); still mark closed but not BE.
            pass

        self.basket_pnl_before_exit = before
        self.projected_closing_fees = float(proj["projected_closing_fees"])
        self.projected_exit_slippage = float(proj["projected_exit_slippage"])
        self.final_realized_net_pnl = final_pnl
        self.break_even_timestamp = timestamp
        self.bars_from_lock_to_break_even = int(self.bars_since_lock)
        self.has_open_unlock_tranche = False
        self.relock_trigger = None

        self._transition(
            timestamp=timestamp,
            state_to="CLOSED_BREAK_EVEN",
            action="basket_break_even_exit",
            reason="projected_net_clears_target",
            reference_price=mark,
            fill_price=None,
            qty=long_qty + short_qty,
            basket_pnl_before=before,
            basket_pnl_after=final_pnl,
            fees=float(self.ledger.total_fees) - fees_before,
        )
        self._record_action(
            timestamp=timestamp,
            action="basket_break_even_exit",
            reason="projected_net_clears_target",
            stage=None,
            attempt=None,
            reference_price=mark,
            fill_price=None,
            qty=long_qty + short_qty,
            basket_pnl_before=before,
            basket_pnl_after=final_pnl,
            fee_delta=float(self.ledger.total_fees) - fees_before,
            realized_short_pnl_delta=0.0,
        )
        return True

    def _build_signal_context(self, *, mark: float):
        if self.simulation_candles is None or self.simulation_index is None:
            return None
        if self.post_lock_start_index is None:
            return None
        from .phase_d_signals import SignalContext

        return SignalContext(
            candles=self.simulation_candles[: self.simulation_index + 1],
            index=self.simulation_index,
            post_lock_start_index=self.post_lock_start_index,
            long_avg=float(self.ledger.long_avg),
            short_avg=float(self.ledger.short_avg),
            long_qty=float(self.ledger.long_qty),
            short_qty=float(self.ledger.short_qty),
            next_unlock_stage=int(self.next_unlock_stage),
            last_unlock_fill=self.last_unlock_fill,
            last_unlock_reference=self.last_unlock_reference,
            bars_since_last_unlock=self.bars_since_last_unlock,
            post_lock_low=self.post_lock_low,
            unlock_rebound_pcts=tuple(self.cfg.unlock_rebound_pcts),
            full_lock_short_qty=float(self.full_lock_short_qty),
        )

    def process_post_lock_bar(
        self,
        *,
        timestamp: str | None,
        candle: dict[str, Any],
        mark: float,
        is_last_bar: bool,
        simulation_index: int | None = None,
    ) -> None:
        if self.state in TERMINAL_STATES:
            return

        if simulation_index is not None:
            self.simulation_index = int(simulation_index)
        if self.bars_since_last_unlock is not None:
            self.bars_since_last_unlock += 1

        self.bars_since_lock += 1
        self._update_post_lock_low(float(candle["low"]))
        self._update_loss_trackers(mark)

        if self.cooldown_bars_remaining > 0:
            self.cooldown_bars_remaining -= 1

        # Max added loss stop (no forced flatten).
        if self.cfg.max_added_loss_after_lock_usdt is not None:
            added = max(
                float(self.basket_pnl_at_lock or 0.0) - self.ledger.basket_net_pnl(mark),
                0.0,
            )
            if added > float(self.cfg.max_added_loss_after_lock_usdt) + 1e-12:
                self._transition(
                    timestamp=timestamp,
                    state_to="STOPPED_MAX_ADDED_LOSS",
                    action="stop",
                    reason="max_added_loss_after_lock",
                    basket_pnl_before=self.ledger.basket_net_pnl(mark),
                    basket_pnl_after=self.ledger.basket_net_pnl(mark),
                )
                return

        if self.bars_since_lock >= int(self.cfg.max_post_lock_bars):
            self._transition(
                timestamp=timestamp,
                state_to="STOPPED_TIMEOUT",
                action="stop",
                reason="max_post_lock_bars",
                basket_pnl_before=self.ledger.basket_net_pnl(mark),
                basket_pnl_after=self.ledger.basket_net_pnl(mark),
            )
            return

        acted = False
        unlock_enabled = bool(getattr(self.cfg, "enable_unlock", True))

        # --- Re-lock ---
        use_signal_relock = (
            self.relock_mode_variant == "signal_invalidation"
            and self.unlock_signal is not None
            and self.has_open_unlock_tranche
        )
        can_relock_pct = (
            unlock_enabled
            and self.has_open_unlock_tranche
            and self.relock_trigger is not None
            and self.cooldown_bars_remaining == 0
            and self.relock_mode_variant == "common_pct"
        )
        if can_relock_pct and relock_signal_touched(
            candle_low=float(candle["low"]),
            relock_trigger=float(self.relock_trigger),
        ):
            self.relock_confirm_streak += 1
        elif not use_signal_relock:
            self.relock_confirm_streak = 0

        if (
            can_relock_pct
            and self.relock_confirm_streak >= int(self.cfg.relock_confirmation_bars)
            and int(self.cfg.relock_execution_delay_bars) == 0
        ):
            acted = self._execute_relock(timestamp=timestamp, candle=candle, mark=mark)

        if (
            not acted
            and use_signal_relock
            and unlock_enabled
            and self.cooldown_bars_remaining == 0
        ):
            ctx = self._build_signal_context(mark=mark)
            if ctx is not None:
                inv = self.unlock_signal.invalidation(ctx)
                if inv.triggered:
                    # Map invalidation to last-tranche relock using common fill rules
                    # but trigger price = invalidation reference.
                    if inv.reference_price is not None:
                        self.relock_trigger = float(inv.reference_price)
                    acted = self._execute_relock(
                        timestamp=timestamp, candle=candle, mark=mark
                    )

        # --- Unlock via Phase-D signal or Phase-B rebound ---
        if (
            unlock_enabled
            and not acted
            and self.next_unlock_stage < len(self.cfg.unlock_steps)
        ):
            decision_triggered = False
            unlock_ref: float | None = None
            decision_meta: dict[str, Any] = {}
            decision_reason = ""

            if self.unlock_signal is not None:
                ctx = self._build_signal_context(mark=mark)
                if ctx is not None:
                    decision = self.unlock_signal.evaluate(ctx)
                    decision_triggered = bool(decision.triggered)
                    unlock_ref = decision.reference_price
                    decision_meta = dict(decision.metadata)
                    decision_reason = decision.reason
                    metrics = self.metrics_at_mark(mark)
                    self.signal_diagnostics.append(
                        {
                            "timestamp": timestamp,
                            "signal_name": decision.signal_name,
                            "triggered": decision.triggered,
                            "reason": decision.reason,
                            "reference_price": decision.reference_price,
                            "invalidation_price": decision.invalidation_price,
                            "unlock_stage": self.next_unlock_stage,
                            "short_profit_buffer": metrics.get("open_short_profit_usdt"),
                            "distance_to_short_avg": metrics.get(
                                "distance_to_short_avg_pct"
                            ),
                            "basket_pnl": self.ledger.basket_net_pnl(mark),
                            **decision_meta,
                        }
                    )
                    if decision.triggered:
                        self.signal_trigger_count += 1
            else:
                unlock_ref = self.current_unlock_reference()
                assert unlock_ref is not None
                decision_triggered = unlock_signal_touched(
                    candle_high=float(candle["high"]),
                    unlock_reference=unlock_ref,
                )
                decision_reason = "rebound_high_touch" if decision_triggered else "waiting"

            # Same-bar unlock+relock touch: never unlock when relock also plausible.
            relock_touch = (
                self.has_open_unlock_tranche
                and self.relock_trigger is not None
                and self.relock_mode_variant == "common_pct"
                and relock_signal_touched(
                    candle_low=float(candle["low"]),
                    relock_trigger=float(self.relock_trigger),
                )
            )
            if relock_touch and decision_triggered and not acted:
                decision_triggered = False

            if (
                decision_triggered
                and self.cooldown_bars_remaining == 0
                and self.failed_unlocks < int(self.cfg.max_failed_unlocks)
            ):
                self.unlock_confirm_streak += 1
            else:
                self.unlock_confirm_streak = 0

            if (
                not acted
                and decision_triggered
                and unlock_ref is not None
                and self.unlock_confirm_streak >= int(self.cfg.unlock_confirmation_bars)
                and int(self.cfg.unlock_execution_delay_bars) == 0
                and self.cooldown_bars_remaining == 0
            ):
                stage = self.next_unlock_stage
                qty = self.planned_unlock_qty(stage)
                ok, _reason = self._unlock_guards_ok(
                    unlock_reference=float(unlock_ref),
                    qty=min(qty, self.ledger.short_qty),
                )
                if ok:
                    acted = self._execute_unlock(
                        timestamp=timestamp,
                        candle=candle,
                        stage=stage,
                        unlock_reference=float(unlock_ref),
                        mark=mark,
                    )

        # After any action (or none), cost-aware basket exit.
        if self.state not in TERMINAL_STATES:
            self._try_basket_exit(timestamp=timestamp, mark=mark)

        if is_last_bar and self.state not in TERMINAL_STATES:
            self._transition(
                timestamp=timestamp,
                state_to="OPEN_AT_DATA_END",
                action="stop",
                reason="data_end",
                basket_pnl_before=self.ledger.basket_net_pnl(mark),
                basket_pnl_after=self.ledger.basket_net_pnl(mark),
            )
