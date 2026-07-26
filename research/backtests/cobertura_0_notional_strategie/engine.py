"""Bar-by-bar Cobertura recovery engine (isolated research semantics).

Per-candle event order (deterministic):
1. Activate pending overlay exits / equalization triggers from prior bar
2. Arm recovery round from WAITING_MOVE if activation level touched
3. Process exits / adds depending on policy:
   - shared_be / individual_tp*: overlay exits first, then short adds
   - dynamic_long_equalization: short adds first, then long eq (never same-bar as add)
4. Full-exit gate at close using total_exit_economics
   - legacy: after adds (fingerprint-stable)
   - net_be: after overlay exits / before adds (no same-candle add→full-exit)
5. EQUALIZED_LOCKED: no further adds; only economics / full-exit until end
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from research.backtests.emergency_lock.cost_model import fee_usdt

from .config import CoberturaConfig
from .economics import (
    adverse_short_exit_price,
    compute_total_exit_economics,
    distance_pct,
    gap_aware_long_close_fill_price,
    gap_aware_short_close_fill_price,
    gap_aware_short_open_fill_price,
    long_open_fill_price,
    overlay_open_profit_usdt,
    overlay_short_be_trigger_price,
    short_open_fill_price,
)
from .equalization import (
    compute_equalization_plan,
    locked_spread_pct,
)
from .ledger import CoberturaLedger, round_price, round_qty
from .start_distance import resolve_post_add_qty
from .tranches import TrancheBook

State = Literal[
    "WAITING_MOVE",
    "OVERLAY_ACTIVE",
    "EQUALIZED_LOCKED",
    "RECOVERED",
    "RECOVERED_BE",
    "STOPPED",
    "DATA_END_OPEN",
]


def _parse_ts(value: object) -> datetime:
    if isinstance(value, datetime):
        ts = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        ts = datetime.fromisoformat(text)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _ts_iso(value: object | None) -> str | None:
    if value is None:
        return None
    return _parse_ts(value).isoformat()


@dataclass
class EngineResult:
    cfg: CoberturaConfig
    ledger: CoberturaLedger
    state: State
    exit_reason: str | None
    recovery_rounds: int
    bars_processed: int
    start_index: int
    recovery_reference_price: float
    locked_spread_loss: float
    per_bar_trace: list[dict[str, Any]] = field(default_factory=list)
    order_events: list[dict[str, Any]] = field(default_factory=list)
    fill_events: list[dict[str, Any]] = field(default_factory=list)
    overlay_rounds: list[dict[str, Any]] = field(default_factory=list)
    overlay_average_timeline: list[dict[str, Any]] = field(default_factory=list)
    overlay_be_timeline: list[dict[str, Any]] = field(default_factory=list)
    total_exit_economics_timeline: list[dict[str, Any]] = field(default_factory=list)
    failure_reasons: list[dict[str, Any]] = field(default_factory=list)
    integrity: dict[str, Any] = field(default_factory=dict)
    tranche_events: list[dict[str, Any]] = field(default_factory=list)
    tranches_final: list[dict[str, Any]] = field(default_factory=list)
    equalization_events: list[dict[str, Any]] = field(default_factory=list)
    first_net_be_touch: dict[str, Any] | None = None
    post_add_guard_events: list[dict[str, Any]] = field(default_factory=list)
    gap_fill_events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def fills_events(self) -> list[dict[str, Any]]:
        """Alias used by older comparison runners / metrics."""
        return self.fill_events


class CoberturaEngine:
    def __init__(self, cfg: CoberturaConfig) -> None:
        cfg.validate()
        self.cfg = cfg
        self.ledger = CoberturaLedger()
        self.ledger.seed_core(
            long_qty=cfg.core_long_qty,
            long_avg=cfg.core_long_avg,
            short_qty=cfg.core_short_qty,
            short_avg=cfg.core_short_avg,
        )
        self._core_freeze = self.ledger.core_snapshot()
        self.state: State = "WAITING_MOVE"
        self.exit_reason: str | None = None
        self.recovery_rounds = 0
        self.recovery_reference_price = float(cfg.start_price)
        self.active_direction: str | None = None
        self.next_add_index = 0
        self.filled_levels: set[int] = set()
        self.overlay_be_price: float | None = None
        self._be_active: float | None = None
        self.bars_since_start = 0
        self._round_adds = 0
        self.tranche_book = TrancheBook()
        self.safety_violation_count = 0
        self.equalization_events: list[dict[str, Any]] = []
        self._eq_plan = None
        self._eq_trigger_active: float | None = None
        self._eq_trigger_pending: float | None = None
        self._eq_seen_below = False
        self._eq_add_qty_pending: float | None = None
        self._short_add_count_total = 0
        self._first_net_be_touch: dict[str, Any] | None = None
        self.post_add_guard_events: list[dict[str, Any]] = []
        self._bar_open: float | None = None
        self.gap_fill_events: list[dict[str, Any]] = []

        self.per_bar_trace: list[dict[str, Any]] = []
        self.order_events: list[dict[str, Any]] = []
        self.fills: list[dict[str, Any]] = []
        self.overlay_rounds: list[dict[str, Any]] = []
        self.overlay_average_timeline: list[dict[str, Any]] = []
        self.overlay_be_timeline: list[dict[str, Any]] = []
        self.econ_timeline: list[dict[str, Any]] = []
        self.failure_reasons: list[dict[str, Any]] = []

    def _uses_individual_tp(self) -> bool:
        return self.cfg.overlay_exit_policy in (
            "individual_tp",
            "individual_tp_scaled",
        )

    def _uses_shared_be(self) -> bool:
        return self.cfg.overlay_exit_policy == "shared_be"

    def _uses_equalization(self) -> bool:
        return self.cfg.overlay_exit_policy == "dynamic_long_equalization"

    def _uses_net_be(self) -> bool:
        return str(self.cfg.full_exit_target_mode) == "net_be"

    def _record_first_net_be_touch(
        self, *, ts: str, econ: Any, close: float, deferred: bool
    ) -> None:
        if self._first_net_be_touch is not None:
            return
        self._first_net_be_touch = {
            "timestamp": ts,
            "bar_index": self.bars_since_start - 1,
            "close": float(close),
            "total_exit_economics": float(econ.total_exit_economics),
            "overlay_short_qty": float(self.ledger.overlay_short.qty),
            "overlay_long_qty": float(self.ledger.overlay_long.qty),
            "gross_notional": float(self.ledger.gross_notional(close))
            if hasattr(self.ledger, "gross_notional")
            else float(
                (self.ledger.core_long.qty + self.ledger.overlay_long.qty) * close
                + (self.ledger.core_short.qty + self.ledger.overlay_short.qty) * close
            ),
            "deferred_same_candle_add": bool(deferred),
            "estimated_remaining_close_fees": float(
                econ.estimated_remaining_close_fees
            ),
            "estimated_exit_slippage": float(econ.estimated_exit_slippage),
        }

    def _maybe_full_exit(
        self, *, ref_price: float, ts: str, econ: Any, added_this_bar: bool
    ) -> bool:
        """Attempt full exit when economics allow. Returns True if exited."""
        if not econ.exit_allowed or self.ledger.core_long.qty <= 0:
            return False
        if self._uses_net_be():
            deferred = bool(added_this_bar)
            self._record_first_net_be_touch(
                ts=ts, econ=econ, close=ref_price, deferred=deferred
            )
            if deferred:
                # Causal: new add this bar → full-exit earliest next bar.
                return False
            self._execute_full_exit(ref_price=ref_price, ts=ts, reason="recovered_net_be")
            return True
        # Legacy / research next-bar exit: defer only when same-bar adds created
        # the newly satisfied gate; prior-bar entitlement still exits immediately.
        if bool(self.cfg.defer_full_exit_after_same_bar_adds) and bool(added_this_bar):
            return False
        label = (
            "recovered_profit"
            if econ.total_exit_economics > float(self.cfg.pnl_tolerance_usdt)
            else "recovered_break_even"
        )
        self._execute_full_exit(ref_price=ref_price, ts=ts, reason=label)
        return True

    def _locked_spread_loss(self) -> float:
        return float(self.cfg.core_long_qty) * (
            float(self.cfg.core_long_avg) - float(self.cfg.core_short_avg)
        )

    def _add_qty(self) -> float:
        qty = round_qty(self.cfg.overlay_add_qty_raw(), self.cfg.qty_step)
        if qty <= 0.0:
            raise ValueError("add qty rounds to zero")
        return qty

    def _level_move_pct(self, add_index: int) -> float:
        return float(self.cfg.first_add_move_pct) + float(add_index) * float(
            self.cfg.add_step_pct
        )

    def _short_add_level(self, add_index: int) -> float:
        move = self._level_move_pct(add_index)
        return round_price(
            self.recovery_reference_price * (1.0 - move), self.cfg.tick_size
        )

    def _activation_down(self) -> float:
        return round_price(
            self.recovery_reference_price * (1.0 - float(self.cfg.activation_move_pct)),
            self.cfg.tick_size,
        )

    def _exposure_blocks_add(self, add_qty: float, mark: float) -> str | None:
        ov_qty = self.ledger.overlay_short.qty + self.ledger.overlay_long.qty
        if self.cfg.max_overlay_qty_multiple is not None:
            cap = float(self.cfg.core_qty()) * float(self.cfg.max_overlay_qty_multiple)
            if ov_qty + add_qty > cap + 1e-12:
                return "max_overlay_qty_multiple"
        if self.cfg.max_total_gross_notional is not None:
            gross = (
                self.ledger.total_long_qty() + self.ledger.total_short_qty() + add_qty
            ) * float(mark)
            if gross > float(self.cfg.max_total_gross_notional) + 1e-9:
                return "max_total_gross_notional"
        if self.cfg.max_net_notional is not None:
            net_qty = self.ledger.net_qty() - add_qty
            if abs(net_qty) * float(mark) > float(self.cfg.max_net_notional) + 1e-9:
                return "max_net_notional"
        if add_qty * float(mark) + 1e-12 < float(self.cfg.min_notional):
            return "min_notional"
        return None

    def _note_safety(self, reason: str, ts: str, **extra: Any) -> None:
        self.safety_violation_count += 1
        row = {"reason": reason, "timestamp": ts}
        row.update(extra)
        self.failure_reasons.append(row)

    def _recompute_overlay_be(self) -> float | None:
        if self._uses_individual_tp() or self._uses_equalization():
            self.overlay_be_price = None
            return None
        if not self.cfg.overlay_be_enabled:
            self.overlay_be_price = None
            return None
        self.overlay_be_price = overlay_short_be_trigger_price(self.ledger, self.cfg)
        return self.overlay_be_price

    def _arm_equalization_pending(self, *, ts: str, reason: str) -> None:
        if not self._uses_equalization():
            return
        if self._short_add_count_total < 1:
            return
        plan = compute_equalization_plan(self.ledger, self.cfg)
        self._eq_plan = plan
        self._eq_trigger_active = None
        self._eq_seen_below = False
        if plan is None:
            self._eq_trigger_pending = None
            self._eq_add_qty_pending = None
            self.equalization_events.append(
                {
                    "event": "equalization_plan_unavailable",
                    "timestamp": ts,
                    "reason": reason,
                }
            )
            return
        self._eq_trigger_pending = float(plan.max_long_add_fill_price)
        self._eq_add_qty_pending = float(plan.add_qty)
        self.equalization_events.append(
            {
                "event": "equalization_trigger_armed_pending",
                "timestamp": ts,
                "reason": reason,
                "add_qty": plan.add_qty,
                "trigger_price": plan.max_long_add_fill_price,
                "max_fill_raw": plan.max_long_add_fill_price_raw,
                "target_long_avg": plan.target_long_avg,
                "current_long_avg": plan.current_long_avg,
                "current_short_avg": plan.current_short_avg,
                "active_next_bar": True,
            }
        )

    def _equalization_exposure_ok(self, add_qty: float, mark: float) -> str | None:
        new_long = self.ledger.total_long_qty() + add_qty
        if self.cfg.max_long_qty_to_initial_core_ratio is not None:
            cap = float(self.cfg.core_qty()) * float(
                self.cfg.max_long_qty_to_initial_core_ratio
            )
            if new_long > cap + 1e-12:
                return "max_long_qty_to_initial_core_ratio"
        gross = (
            self.ledger.total_long_qty()
            + add_qty
            + self.ledger.total_short_qty()
        ) * float(mark)
        cap_g = self.cfg.max_total_gross_notional
        if cap_g is not None and gross > float(cap_g) + 1e-9:
            return "max_total_gross_notional"
        if add_qty * float(mark) + 1e-12 < float(self.cfg.min_notional):
            return "min_notional"
        return None

    def _try_equalization_fill(
        self, *, o: float, h: float, low: float, c: float, ts: str
    ) -> bool:
        if not self._uses_equalization():
            return False
        if self.state != "OVERLAY_ACTIVE":
            return False
        if self._eq_trigger_active is None or self._eq_plan is None:
            return False
        if self._short_add_count_total < 1:
            return False
        trigger = float(self._eq_trigger_active)
        add_qty = float(self._eq_add_qty_pending or self._eq_plan.add_qty)
        if add_qty <= 0.0:
            return False

        if low + 1e-12 < trigger:
            self._eq_seen_below = True

        if self.cfg.long_equalization_require_recovery and not self._eq_seen_below:
            return False
        if h + 1e-12 < trigger:
            return False

        # Recompute live plan for qty sync (shorts unchanged since arm).
        plan = compute_equalization_plan(self.ledger, self.cfg)
        if plan is None or plan.add_qty <= 0.0:
            return False
        add_qty = float(plan.add_qty)
        # Fill at min(trigger, high path): conservative long fill uses trigger
        # with adverse slippage — never better than trigger.
        fill_px = round_price(
            long_open_fill_price(trigger, self.cfg.slippage_bps_open),
            self.cfg.tick_size,
        )
        if fill_px - trigger > 1e-12 and self.cfg.slippage_bps_open <= 0.0:
            fill_px = round_price(trigger, self.cfg.tick_size)

        block = self._equalization_exposure_ok(add_qty, fill_px)
        if block is not None:
            self._note_safety(block, ts)
            return False

        short_before = (
            self.ledger.core_short.qty,
            self.ledger.core_short.avg,
            self.ledger.overlay_short.qty,
            self.ledger.overlay_short.avg,
        )
        long_avg_before = self.ledger.total_long_avg()
        long_qty_before = self.ledger.total_long_qty()
        core_before = self.ledger.core_snapshot()

        meta = self.ledger.open_overlay_long(
            qty=add_qty,
            fill_price=fill_px,
            reference_price=trigger,
            fee_rate_open=self.cfg.fee_rate_open,
        )
        # Core must stay frozen; shorts unchanged.
        self.ledger.assert_core_unchanged(core_before)
        short_after = (
            self.ledger.core_short.qty,
            self.ledger.core_short.avg,
            self.ledger.overlay_short.qty,
            self.ledger.overlay_short.avg,
        )
        if short_before != short_after:
            raise AssertionError("equalization mutated short inventory")

        new_avg = self.ledger.total_long_avg()
        spread_pct = locked_spread_pct(new_avg, self.ledger.total_short_avg())
        fill_path = {
            "open_le_trigger": o <= trigger + 1e-12,
            "high_ge_trigger": h + 1e-12 >= trigger,
            "low_lt_trigger": low + 1e-12 < trigger,
            "close_le_trigger": c <= trigger + 1e-12,
            "conservative_fill_model": "trigger_with_open_slippage",
        }
        self.fills.append(
            {
                "timestamp": ts,
                "kind": "long_equalization",
                "level": None,
                "trigger": trigger,
                "fill_price": fill_px,
                "qty": add_qty,
                "fee": meta["fee"],
                "slippage_cost": meta["slippage_cost"],
                "side": "long",
            }
        )
        self.equalization_events.append(
            {
                "event": "long_equalization_fill",
                "timestamp": ts,
                "trigger_price": trigger,
                "fill_price": fill_px,
                "qty": add_qty,
                "fee": meta["fee"],
                "long_avg_before": long_avg_before,
                "long_qty_before": long_qty_before,
                "long_avg_after": new_avg,
                "long_qty_after": self.ledger.total_long_qty(),
                "short_qty_after": self.ledger.total_short_qty(),
                "short_avg": self.ledger.total_short_avg(),
                "locked_spread_pct_after": spread_pct,
                "target_long_avg": plan.target_long_avg,
                "fill_path": fill_path,
            }
        )
        self._record_avg_timeline(ts=ts, price=fill_px)
        self.state = "EQUALIZED_LOCKED"
        self.exit_reason = "equalized_locked"
        self.active_direction = None
        self._eq_trigger_active = None
        self._eq_trigger_pending = None
        self._eq_plan = None
        self.order_events.append(
            {
                "timestamp": ts,
                "event": "equalized_locked",
                "long_qty": self.ledger.total_long_qty(),
                "short_qty": self.ledger.total_short_qty(),
                "locked_spread_pct": spread_pct,
            }
        )
        return True

    def _reset_round(self, new_reference: float, *, reason: str, ts: str) -> None:
        self.ledger.assert_core_unchanged(self._core_freeze)
        self.overlay_rounds.append(
            {
                "round": self.recovery_rounds,
                "end_reason": reason,
                "end_timestamp": ts,
                "adds": self._round_adds,
                "reference_price_end": new_reference,
            }
        )
        self.active_direction = None
        self.next_add_index = 0
        self.filled_levels.clear()
        self.overlay_be_price = None
        self._be_active = None
        self._round_adds = 0
        if self.cfg.reset_reference_after_overlay_be:
            self.recovery_reference_price = float(new_reference)
        self.state = "WAITING_MOVE"
        if self.cfg.max_recovery_rounds is not None and self.recovery_rounds >= int(
            self.cfg.max_recovery_rounds
        ):
            self.state = "STOPPED"
            self.exit_reason = "max_recovery_rounds"
            self._note_safety("max_recovery_rounds", ts)

    def _start_round(self, direction: str, ts: str) -> None:
        self.recovery_rounds += 1
        self.active_direction = direction
        self.next_add_index = 0
        self.filled_levels.clear()
        self._round_adds = 0
        self._be_active = None
        self.overlay_be_price = None
        self.state = "OVERLAY_ACTIVE"
        self.order_events.append(
            {
                "timestamp": ts,
                "event": "round_armed",
                "direction": direction,
                "round": self.recovery_rounds,
                "recovery_reference_price": self.recovery_reference_price,
                "overlay_exit_policy": self.cfg.overlay_exit_policy,
            }
        )

    def _record_avg_timeline(self, *, ts: str, price: float) -> None:
        self.overlay_average_timeline.append(
            {
                "timestamp": ts,
                "price": price,
                "overlay_short_qty": self.ledger.overlay_short.qty,
                "overlay_short_avg": self.ledger.overlay_short.avg,
                "overlay_long_qty": self.ledger.overlay_long.qty,
                "overlay_long_avg": self.ledger.overlay_long.avg,
                "total_short_qty": self.ledger.total_short_qty(),
                "total_short_avg": self.ledger.total_short_avg(),
                "core_short_avg": self.ledger.core_short.avg,
                "distance_price_to_overlay_avg_pct": distance_pct(
                    price, self.ledger.overlay_short.avg
                )
                if self.ledger.overlay_short.qty > 0
                else None,
                "distance_price_to_total_short_avg_pct": distance_pct(
                    price, self.ledger.total_short_avg()
                )
                if self.ledger.total_short_qty() > 0
                else None,
                "distance_price_to_core_short_avg_pct": distance_pct(
                    price, self.ledger.core_short.avg
                ),
            }
        )

    def _assert_tranche_qty_sync(self) -> None:
        book_qty = self.tranche_book.remaining_qty()
        led_qty = self.ledger.overlay_short.qty
        if abs(book_qty - led_qty) > 1e-6:
            raise AssertionError(
                f"tranche/ledger qty mismatch: book={book_qty} ledger={led_qty}"
            )

    def _fill_short_add(self, *, level: int, trigger: float, ts: str) -> bool:
        configured_qty = self._add_qty()
        candle_open = float(self._bar_open if self._bar_open is not None else trigger)
        fill_raw, gap_ref, gap_adj = gap_aware_short_open_fill_price(
            trigger=trigger,
            candle_open=candle_open,
            slippage_bps_open=self.cfg.slippage_bps_open,
            enabled=bool(self.cfg.gap_through_trigger_fills),
        )
        fill_px = round_price(fill_raw, self.cfg.tick_size)
        if gap_adj:
            self.gap_fill_events.append(
                {
                    "timestamp": ts,
                    "kind": "overlay_short_add",
                    "side": "short",
                    "trigger": trigger,
                    "candle_open": candle_open,
                    "raw_reference": gap_ref,
                    "fill_price": fill_px,
                    "gap_adjusted": True,
                }
            )
        cur_short_qty = float(self.ledger.total_short_qty())
        cur_short_avg = float(self.ledger.total_short_avg()) if cur_short_qty > 0 else 0.0
        decision = resolve_post_add_qty(
            configured_candidate_add_qty=configured_qty,
            current_total_short_qty=cur_short_qty,
            current_total_short_avg=cur_short_avg if cur_short_qty > 0 else fill_px,
            current_overlay_qty=float(
                self.ledger.overlay_short.qty + self.ledger.overlay_long.qty
            ),
            core_qty=float(self.cfg.core_qty()),
            candidate_fill_price=fill_px,
            current_price=float(trigger),
            minimum_post_add_distance_pct=self.cfg.minimum_post_add_distance_pct,
            post_add_distance_policy=self.cfg.post_add_distance_policy,
            max_overlay_qty_multiple=self.cfg.max_overlay_qty_multiple,
            qty_step=float(self.cfg.qty_step),
            min_notional=float(self.cfg.min_notional),
        )
        guard_row = {
            "timestamp": ts,
            "level": level,
            "trigger": trigger,
            "fill_price": fill_px,
            **decision,
        }
        self.post_add_guard_events.append(guard_row)

        if decision["action"] == "skip" or float(decision["actual_add_qty"]) <= 0.0:
            self.order_events.append(
                {
                    "timestamp": ts,
                    "event": "overlay_short_add_skipped",
                    "level": level,
                    "trigger": trigger,
                    "reason": decision.get("reason"),
                    "configured_qty": configured_qty,
                }
            )
            return False

        qty = float(decision["actual_add_qty"])
        block = self._exposure_blocks_add(qty, trigger)
        if block is not None:
            self._note_safety(block, ts, level=level)
            return False
        meta = self.ledger.open_overlay_short(
            qty=qty,
            fill_price=fill_px,
            reference_price=trigger,
            fee_rate_open=self.cfg.fee_rate_open,
        )
        self.filled_levels.add(level)
        self.next_add_index = level + 1
        self._round_adds += 1
        self._short_add_count_total += 1
        self.order_events.append(
            {
                "timestamp": ts,
                "event": "overlay_short_add_order",
                "level": level,
                "trigger": trigger,
                "qty": qty,
                "configured_qty": configured_qty,
                "post_add_action": decision.get("action"),
            }
        )
        self.fills.append(
            {
                "timestamp": ts,
                "kind": "overlay_short_add",
                "level": level,
                "trigger": trigger,
                "fill_price": fill_px,
                "qty": qty,
                "configured_qty": configured_qty,
                "fee": meta["fee"],
                "slippage_cost": meta["slippage_cost"],
                "side": "short",
                "projected_post_add_distance_pct": decision.get(
                    "projected_post_add_distance_pct"
                ),
                "projected_total_short_avg": decision.get("projected_total_short_avg"),
                "post_add_action": decision.get("action"),
            }
        )
        # Always record tranche for audit; TP only for individual policies.
        self.tranche_book.create_short_tranche(
            cfg=self.cfg,
            round_id=self.recovery_rounds,
            timestamp=ts,
            entry_price_raw=trigger,
            entry_price_filled=fill_px,
            qty=qty,
            open_fee_usdt=meta["fee"],
            level=level,
        )

        self.ledger.assert_core_unchanged(self._core_freeze)
        self._record_avg_timeline(ts=ts, price=fill_px)
        be = self._recompute_overlay_be()
        self.overlay_be_timeline.append(
            {
                "timestamp": ts,
                "overlay_be_price": be,
                "overlay_short_qty": self.ledger.overlay_short.qty,
                "overlay_short_avg": self.ledger.overlay_short.avg,
                "overlay_entry_fees": self.ledger.overlay_entry_fees,
                "active_next_bar": True,
            }
        )
        if self._uses_equalization():
            self._arm_equalization_pending(ts=ts, reason="after_short_add")
        self._assert_tranche_qty_sync()
        return True

    def _close_overlay_short_be(self, *, trigger: float, ts: str) -> None:
        core_before = self.ledger.core_snapshot()
        candle_open = float(self._bar_open if self._bar_open is not None else trigger)
        fill_raw, gap_ref, gap_adj = gap_aware_short_close_fill_price(
            trigger=trigger,
            candle_open=candle_open,
            slippage_bps_close=self.cfg.slippage_bps_close,
            enabled=bool(self.cfg.gap_through_trigger_fills),
        )
        fill_px = round_price(fill_raw, self.cfg.tick_size)
        if gap_adj:
            self.gap_fill_events.append(
                {
                    "timestamp": ts,
                    "kind": "overlay_be_close",
                    "side": "buy",
                    "trigger": trigger,
                    "candle_open": candle_open,
                    "raw_reference": gap_ref,
                    "fill_price": fill_px,
                    "gap_adjusted": True,
                }
            )
        meta = self.ledger.close_all_overlay_short(
            fill_price=fill_px,
            reference_price=trigger,
            fee_rate_close=self.cfg.fee_rate_close,
        )
        self.ledger.assert_core_unchanged(core_before)
        if self.ledger.overlay_short.qty != 0.0:
            raise AssertionError("overlay short not flat after BE close")
        # Close all open tranches for audit consistency.
        for t in list(self.tranche_book.open_tranches()):
            t.remaining_qty = 0.0
            t.status = "closed"
            t.tp_active = False
            t.close_timestamp = ts
            t.close_price = fill_px
            self.tranche_book.events.append(
                {
                    "event": "tranche_shared_be_close",
                    "timestamp": ts,
                    "tranche_id": t.tranche_id,
                    "round_id": t.round_id,
                    "qty": t.initial_qty,
                    "fill_price": fill_px,
                    "status": "closed",
                }
            )
        self.fills.append(
            {
                "timestamp": ts,
                "kind": "overlay_be_close",
                "level": None,
                "trigger": trigger,
                "fill_price": fill_px,
                "qty": meta["qty"],
                "fee": meta["fee"],
                "slippage_cost": meta["slippage_cost"],
                "side": "long",
                "realized_pnl_delta": meta["realized_pnl_delta"],
            }
        )
        self._record_avg_timeline(ts=ts, price=fill_px)
        self._be_active = None
        self.overlay_be_price = None
        self._reset_round(fill_px, reason="overlay_be_close", ts=ts)

    def _process_individual_tps(self, *, low: float, ts: str) -> bool:
        """Process active short TPs. Returns True if overlay became flat (round reset).

        Conservative multi-TP order: highest trigger first (nearest / least deep),
        each fill at its own trigger (not candle low).
        """
        any_close = False
        # Snapshot eligible at bar open (already activated).
        eligible = [
            t
            for t in self.tranche_book.open_tranches()
            if t.tp_active and t.remaining_qty > 1e-12 and t.tp_trigger_price > 0.0
        ]
        eligible.sort(key=lambda t: (-float(t.tp_trigger_price), t.tranche_id))

        for tranche in eligible:
            if tranche.remaining_qty <= 1e-12:
                continue
            if low > float(tranche.tp_trigger_price) + 1e-12:
                continue
            close_qty = self.tranche_book.planned_close_qty(self.cfg, tranche)
            if close_qty <= 0.0:
                continue
            trigger = float(tranche.tp_trigger_price)
            fill_px = round_price(
                adverse_short_exit_price(trigger, self.cfg.slippage_bps_close),
                self.cfg.tick_size,
            )
            open_fee_alloc = float(tranche.open_fee_usdt) * (
                close_qty / float(tranche.initial_qty)
            )
            core_before = self.ledger.core_snapshot()
            meta = self.ledger.close_overlay_short_qty(
                qty=close_qty,
                fill_price=fill_px,
                reference_price=trigger,
                fee_rate_close=self.cfg.fee_rate_close,
                open_fee_release=open_fee_alloc,
            )
            self.ledger.assert_core_unchanged(core_before)
            partial = close_qty + 1e-12 < tranche.remaining_qty or (
                tranche.remaining_qty - close_qty > 1e-12
            )
            # remaining before apply
            rem_before = tranche.remaining_qty
            partial = close_qty + 1e-12 < rem_before
            self.tranche_book.apply_close(
                tranche=tranche,
                qty=close_qty,
                fill_price=fill_px,
                close_fee=meta["fee"],
                realized_pnl=meta["realized_pnl_delta"],
                timestamp=ts,
                partial=partial,
                cfg=self.cfg,
            )
            self.fills.append(
                {
                    "timestamp": ts,
                    "kind": "overlay_tp_partial" if partial else "overlay_tp_close",
                    "level": tranche.level,
                    "trigger": trigger,
                    "fill_price": fill_px,
                    "qty": close_qty,
                    "fee": meta["fee"],
                    "slippage_cost": meta["slippage_cost"],
                    "side": "long",
                    "realized_pnl_delta": meta["realized_pnl_delta"],
                    "tranche_id": tranche.tranche_id,
                }
            )
            self._record_avg_timeline(ts=ts, price=fill_px)
            any_close = True
            self._assert_tranche_qty_sync()

        if any_close and self.ledger.overlay_short.qty <= 1e-12:
            # Flat overlay → new recovery round wait (same as BE reset).
            ref = float(self.fills[-1]["fill_price"])
            self._reset_round(ref, reason="overlay_tp_flat", ts=ts)
            return True
        return False

    def _execute_full_exit(self, *, ref_price: float, ts: str, reason: str) -> None:
        econ = compute_total_exit_economics(
            self.ledger, self.cfg, reference_exit_price=ref_price
        )
        candle_open = float(self._bar_open if self._bar_open is not None else ref_price)
        long_px = float(econ.long_exit_price)
        short_px = float(econ.short_exit_price)
        if bool(self.cfg.gap_through_trigger_fills):
            long_raw, long_ref, long_adj = gap_aware_long_close_fill_price(
                trigger=ref_price,
                candle_open=candle_open,
                slippage_bps_close=self.cfg.slippage_bps_close,
                enabled=True,
            )
            short_raw, short_ref, short_adj = gap_aware_short_close_fill_price(
                trigger=ref_price,
                candle_open=candle_open,
                slippage_bps_close=self.cfg.slippage_bps_close,
                enabled=True,
            )
            long_px = round_price(long_raw, self.cfg.tick_size)
            short_px = round_price(short_raw, self.cfg.tick_size)
            if long_adj or short_adj:
                self.gap_fill_events.append(
                    {
                        "timestamp": ts,
                        "kind": "full_exit",
                        "side": "both",
                        "trigger": ref_price,
                        "candle_open": candle_open,
                        "raw_reference_long": long_ref,
                        "raw_reference_short": short_ref,
                        "fill_price_long": long_px,
                        "fill_price_short": short_px,
                        "gap_adjusted": True,
                    }
                )
        for pos, side, px, is_overlay in (
            (self.ledger.overlay_long, "long", long_px, True),
            (self.ledger.overlay_short, "short", short_px, True),
            (self.ledger.core_long, "long", long_px, False),
            (self.ledger.core_short, "short", short_px, False),
        ):
            qty = pos.qty
            if qty <= 0.0:
                continue
            pnl = pos.close_all(px, side)
            if is_overlay:
                self.ledger.realized_overlay_pnl += pnl
            fee = fee_usdt(fill_price=px, qty=qty, fee_rate=self.cfg.fee_rate_close)
            self.ledger.cumulative_close_fees += fee
            self.fills.append(
                {
                    "timestamp": ts,
                    "kind": "full_exit",
                    "level": None,
                    "trigger": ref_price,
                    "fill_price": px,
                    "qty": qty,
                    "fee": fee,
                    "slippage_cost": 0.0,
                    "side": "sell" if side == "long" else "buy",
                    "realized_pnl_delta": pnl,
                    "leg": "overlay" if is_overlay else "core",
                    "position_side": side,
                }
            )
        self.tranche_book.close_all_for_full_exit(
            timestamp=ts,
            fill_price=short_px,
            close_fee_by_qty=0.0,
            realized_by_qty=0.0,
        )
        if self._uses_net_be() or reason == "recovered_net_be":
            self.state = "RECOVERED_BE"
        else:
            self.state = "RECOVERED"
        self.exit_reason = reason
        self.active_direction = None
        self.overlay_be_price = None
        self._be_active = None
        self.order_events.append(
            {
                "timestamp": ts,
                "event": "full_exit",
                "reason": reason,
                "total_exit_economics_pre": econ.total_exit_economics,
                "estimated_remaining_close_fees_pre": econ.estimated_remaining_close_fees,
                "estimated_exit_slippage_pre": econ.estimated_exit_slippage,
            }
        )

    def process_candle(self, candle: dict[str, Any]) -> None:
        if self.state in ("RECOVERED", "RECOVERED_BE", "STOPPED"):
            return

        ts = _ts_iso(candle["timestamp"]) or ""
        o = float(candle["open"])
        h = float(candle["high"])
        low = float(candle["low"])
        c = float(candle["close"])
        self._bar_open = o
        self.bars_since_start += 1
        added_this_bar = False

        if self.cfg.max_recovery_duration_bars is not None:
            if self.bars_since_start > int(self.cfg.max_recovery_duration_bars):
                if self.state != "EQUALIZED_LOCKED":
                    self.state = "STOPPED"
                    self.exit_reason = "max_recovery_duration_bars"
                    self._note_safety("max_recovery_duration_bars", ts)
                self._trace(ts, o, h, low, c)
                return

        # EQUALIZED_LOCKED: only economics / full-exit, no new positions.
        if self.state == "EQUALIZED_LOCKED":
            econ = compute_total_exit_economics(
                self.ledger, self.cfg, reference_exit_price=c
            )
            self._append_econ(ts, econ)
            self._maybe_full_exit(
                ref_price=c, ts=ts, econ=econ, added_this_bar=False
            )
            self._trace(ts, o, h, low, c)
            return

        # 1) Activate pending exits / equalization from prior bar.
        if self._uses_individual_tp():
            self.tranche_book.activate_pending()
        elif self._uses_shared_be():
            if self.overlay_be_price is not None and self.ledger.overlay_short.qty > 0:
                self._be_active = self.overlay_be_price
        elif self._uses_equalization():
            if self._eq_trigger_pending is not None:
                self._eq_trigger_active = self._eq_trigger_pending
                self._eq_trigger_pending = None
                self._eq_seen_below = False
                self.equalization_events.append(
                    {
                        "event": "equalization_trigger_active",
                        "timestamp": ts,
                        "trigger_price": self._eq_trigger_active,
                        "add_qty": self._eq_add_qty_pending,
                    }
                )
            if self._eq_trigger_active is not None and low + 1e-12 < float(
                self._eq_trigger_active
            ):
                self._eq_seen_below = True

        # 2) Arm short recovery after activation move.
        if self.state == "WAITING_MOVE" and self.cfg.direction_mode in (
            "short_only",
            "symmetric",
        ):
            if low <= self._activation_down():
                if self.cfg.max_recovery_rounds is not None and self.recovery_rounds >= int(
                    self.cfg.max_recovery_rounds
                ):
                    self.state = "STOPPED"
                    self.exit_reason = "max_recovery_rounds"
                    self._note_safety("max_recovery_rounds", ts)
                else:
                    self._start_round("short", ts)

        if self.state == "WAITING_MOVE" and self.cfg.direction_mode == "long_only":
            self._note_safety("long_overlay_not_implemented", ts)

        if self._uses_equalization():
            # Equalization: short adds first, then long eq (never same-bar as add).
            if self.state == "OVERLAY_ACTIVE" and self.active_direction == "short":
                adds_this_candle = 0
                while (
                    adds_this_candle < int(self.cfg.max_adds_per_candle)
                    and self.next_add_index < int(self.cfg.max_add_count)
                ):
                    level = self.next_add_index
                    if level in self.filled_levels:
                        self.next_add_index += 1
                        continue
                    trigger = self._short_add_level(level)
                    if low <= trigger + 1e-12:
                        ok = self._fill_short_add(level=level, trigger=trigger, ts=ts)
                        if not ok:
                            break
                        adds_this_candle += 1
                        added_this_bar = True
                    else:
                        break
            if not added_this_bar:
                self._try_equalization_fill(o=o, h=h, low=low, c=c, ts=ts)
        else:
            # Fingerprint-stable shared_be / individual_tp: exits before adds.
            if (
                self._uses_shared_be()
                and self.state == "OVERLAY_ACTIVE"
                and self.active_direction == "short"
                and self._be_active is not None
                and self.ledger.overlay_short.qty > 0
                and self.cfg.overlay_be_enabled
                and self.cfg.overlay_be_close_all
            ):
                be = float(self._be_active)
                open_profit = overlay_open_profit_usdt(self.ledger, be)
                if open_profit + 1e-12 >= float(
                    self.cfg.overlay_be_min_open_profit_usdt
                ):
                    if h + 1e-12 >= be:
                        self._close_overlay_short_be(trigger=be, ts=ts)
                        econ = compute_total_exit_economics(
                            self.ledger, self.cfg, reference_exit_price=c
                        )
                        self._append_econ(ts, econ)
                        if self._uses_net_be():
                            self._maybe_full_exit(
                                ref_price=c,
                                ts=ts,
                                econ=econ,
                                added_this_bar=False,
                            )
                        self._trace(ts, o, h, low, c)
                        return

            if (
                self._uses_individual_tp()
                and self.state == "OVERLAY_ACTIVE"
                and self.active_direction == "short"
                and self.ledger.overlay_short.qty > 0
            ):
                flattened = self._process_individual_tps(low=low, ts=ts)
                if flattened:
                    econ = compute_total_exit_economics(
                        self.ledger, self.cfg, reference_exit_price=c
                    )
                    self._append_econ(ts, econ)

            # net_be: full-exit before adds so a same-candle add cannot create the BE.
            # next_bar_exit (legacy): also evaluate prior entitlement before adds.
            if self.state not in (
                "RECOVERED",
                "RECOVERED_BE",
                "STOPPED",
            ) and (
                self._uses_net_be()
                or bool(self.cfg.defer_full_exit_after_same_bar_adds)
            ):
                econ = compute_total_exit_economics(
                    self.ledger, self.cfg, reference_exit_price=c
                )
                self._append_econ(ts, econ)
                if self._maybe_full_exit(
                    ref_price=c, ts=ts, econ=econ, added_this_bar=False
                ):
                    self._trace(ts, o, h, low, c)
                    return

            if self.state == "OVERLAY_ACTIVE" and self.active_direction == "short":
                adds_this_candle = 0
                while (
                    adds_this_candle < int(self.cfg.max_adds_per_candle)
                    and self.next_add_index < int(self.cfg.max_add_count)
                ):
                    level = self.next_add_index
                    if level in self.filled_levels:
                        self.next_add_index += 1
                        continue
                    trigger = self._short_add_level(level)
                    if low <= trigger + 1e-12:
                        ok = self._fill_short_add(level=level, trigger=trigger, ts=ts)
                        if not ok:
                            break
                        adds_this_candle += 1
                        added_this_bar = True
                    else:
                        break

            if added_this_bar and self._uses_shared_be():
                self._be_active = None

        # 5) Full-exit gate (legacy after adds; net_be / next_bar already handled pre-add)
        if self.state not in ("RECOVERED", "RECOVERED_BE", "STOPPED"):
            econ = compute_total_exit_economics(
                self.ledger, self.cfg, reference_exit_price=c
            )
            self._append_econ(ts, econ)
            if self._uses_net_be() or bool(self.cfg.defer_full_exit_after_same_bar_adds):
                # Pre-add gate already evaluated; do not allow post-add same-bar exit
                # when adds occurred. If no adds, still allow exit here.
                if added_this_bar:
                    exited = False
                else:
                    exited = self._maybe_full_exit(
                        ref_price=c, ts=ts, econ=econ, added_this_bar=False
                    )
            else:
                exited = self._maybe_full_exit(
                    ref_price=c, ts=ts, econ=econ, added_this_bar=added_this_bar
                )
            if (
                not exited
                and self.state != "EQUALIZED_LOCKED"
                and self.cfg.max_recovery_drawdown_usdt is not None
            ):
                if (-econ.total_exit_economics) > float(
                    self.cfg.max_recovery_drawdown_usdt
                ):
                    self.state = "STOPPED"
                    self.exit_reason = "max_recovery_drawdown_usdt"
                    self._note_safety("max_recovery_drawdown_usdt", ts)

        self._trace(ts, o, h, low, c)

    def _append_econ(self, ts: str, econ: Any) -> None:
        self.econ_timeline.append(
            {
                "timestamp": ts,
                "total_exit_economics": econ.total_exit_economics,
                "remaining_to_total_be": econ.remaining_to_total_be,
                "estimated_remaining_close_fees": econ.estimated_remaining_close_fees,
                "cumulative_entry_fees": econ.cumulative_entry_fees,
                "cumulative_close_fees_paid": econ.cumulative_close_fees_paid,
                "exit_allowed": econ.exit_allowed,
                "realized_overlay_pnl": econ.realized_overlay_pnl,
                "cumulative_slippage_costs": econ.cumulative_slippage_costs,
                "estimated_exit_slippage": econ.estimated_exit_slippage,
            }
        )

    def _trace(self, ts: str, o: float, h: float, low: float, c: float) -> None:
        econ = compute_total_exit_economics(
            self.ledger, self.cfg, reference_exit_price=c
        )
        cur_level = self.next_add_index - 1 if self.next_add_index > 0 else None
        next_level = None
        if (
            self.active_direction == "short"
            and self.next_add_index < int(self.cfg.max_add_count)
        ):
            next_level = self._short_add_level(self.next_add_index)
        ov_avg = (
            self.ledger.overlay_short.avg if self.ledger.overlay_short.qty > 0 else None
        )
        tot_s_avg = (
            self.ledger.total_short_avg() if self.ledger.total_short_qty() > 0 else None
        )
        open_pnls = self.ledger.open_pnl_at(c)
        self.per_bar_trace.append(
            {
                "timestamp": ts,
                "open": o,
                "high": h,
                "low": low,
                "close": c,
                "state": self.state,
                "recovery_round": self.recovery_rounds,
                "recovery_reference_price": self.recovery_reference_price,
                "active_direction": self.active_direction,
                "overlay_exit_policy": self.cfg.overlay_exit_policy,
                "core_long_qty": self.ledger.core_long.qty,
                "core_long_avg": self.ledger.core_long.avg,
                "core_short_qty": self.ledger.core_short.qty,
                "core_short_avg": self.ledger.core_short.avg,
                "overlay_long_qty": self.ledger.overlay_long.qty,
                "overlay_long_avg": self.ledger.overlay_long.avg,
                "overlay_short_qty": self.ledger.overlay_short.qty,
                "overlay_short_avg": self.ledger.overlay_short.avg,
                "total_long_qty": self.ledger.total_long_qty(),
                "total_long_avg": self.ledger.total_long_avg(),
                "total_short_qty": self.ledger.total_short_qty(),
                "total_short_avg": self.ledger.total_short_avg(),
                "net_qty": self.ledger.net_qty(),
                "gross_notional": (
                    self.ledger.total_long_qty() + self.ledger.total_short_qty()
                )
                * c,
                "net_notional": self.ledger.net_qty() * c,
                "current_overlay_level": cur_level,
                "next_add_level": next_level,
                "overlay_be_price": self.overlay_be_price,
                "overlay_be_active": self._be_active,
                "overlay_open_pnl": open_pnls["overlay_long_open_pnl"]
                + open_pnls["overlay_short_open_pnl"],
                "core_open_pnl": open_pnls["core_long_open_pnl"]
                + open_pnls["core_short_open_pnl"],
                "realized_overlay_pnl": self.ledger.realized_overlay_pnl,
                "cumulative_fees": self.ledger.cumulative_entry_fees
                + self.ledger.cumulative_close_fees,
                "estimated_close_fees": econ.estimated_remaining_close_fees,
                "total_exit_economics": econ.total_exit_economics,
                "remaining_to_total_be": econ.remaining_to_total_be,
                "distance_price_to_overlay_avg_pct": distance_pct(c, ov_avg)
                if ov_avg
                else None,
                "distance_price_to_total_short_avg_pct": distance_pct(c, tot_s_avg)
                if tot_s_avg
                else None,
                "exit_reason": self.exit_reason,
                "open_tranche_count": len(self.tranche_book.open_tranches()),
            }
        )

    def finalize(self, start_index: int) -> EngineResult:
        if self.state == "EQUALIZED_LOCKED":
            if self.exit_reason is None:
                self.exit_reason = "equalized_locked_data_end"
        elif self.state not in ("RECOVERED", "RECOVERED_BE", "STOPPED"):
            self.state = "DATA_END_OPEN"
            if self.exit_reason is None:
                self.exit_reason = "data_end_open"
                self.failure_reasons.append({"reason": "data_end_open"})

        core_ok = True
        if self.state not in ("RECOVERED", "RECOVERED_BE"):
            try:
                self.ledger.assert_core_unchanged(self._core_freeze)
            except AssertionError:
                core_ok = False

        try:
            self._assert_tranche_qty_sync()
            tranche_sync = True
        except AssertionError:
            tranche_sync = False

        integrity = {
            "core_unchanged_until_full_exit_or_still_frozen": core_ok
            or self.state in ("RECOVERED", "RECOVERED_BE"),
            "start_qty_neutral": abs(self.cfg.core_long_qty - self.cfg.core_short_qty)
            <= 1e-9,
            "no_negative_qty": all(
                q >= -1e-12
                for q in (
                    self.ledger.core_long.qty,
                    self.ledger.core_short.qty,
                    self.ledger.overlay_long.qty,
                    self.ledger.overlay_short.qty,
                )
            ),
            "tranche_ledger_qty_sync": tranche_sync,
            "locked_spread_loss": self._locked_spread_loss(),
            "fill_count": len(self.fills),
            "recovery_rounds": self.recovery_rounds,
            "final_state": self.state,
            "exit_reason": self.exit_reason,
            "overlay_exit_policy": self.cfg.overlay_exit_policy,
            "full_exit_target_mode": self.cfg.full_exit_target_mode,
            "safety_violation_count": self.safety_violation_count,
            "short_add_count_total": self._short_add_count_total,
            "equalization_fill_count": sum(
                1
                for e in self.equalization_events
                if e.get("event") == "long_equalization_fill"
            ),
            "flat_after_full_exit": (
                self.state in ("RECOVERED", "RECOVERED_BE")
                and abs(self.ledger.core_long.qty) <= 1e-12
                and abs(self.ledger.core_short.qty) <= 1e-12
                and abs(self.ledger.overlay_long.qty) <= 1e-12
                and abs(self.ledger.overlay_short.qty) <= 1e-12
            ),
        }
        return EngineResult(
            cfg=self.cfg,
            ledger=self.ledger,
            state=self.state,
            exit_reason=self.exit_reason,
            recovery_rounds=self.recovery_rounds,
            bars_processed=self.bars_since_start,
            start_index=start_index,
            recovery_reference_price=self.recovery_reference_price,
            locked_spread_loss=self._locked_spread_loss(),
            per_bar_trace=self.per_bar_trace,
            order_events=self.order_events,
            fill_events=self.fills,
            overlay_rounds=self.overlay_rounds,
            overlay_average_timeline=self.overlay_average_timeline,
            overlay_be_timeline=self.overlay_be_timeline,
            total_exit_economics_timeline=self.econ_timeline,
            failure_reasons=self.failure_reasons,
            integrity=integrity,
            tranche_events=list(self.tranche_book.events),
            tranches_final=[t.to_dict() for t in self.tranche_book.tranches],
            equalization_events=list(self.equalization_events),
            first_net_be_touch=self._first_net_be_touch,
            post_add_guard_events=self.post_add_guard_events,
            gap_fill_events=self.gap_fill_events,
        )
