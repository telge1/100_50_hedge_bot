"""Overlay short tranche tracking and fee-aware individual TP triggers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from fixed_cycle_hedge_bot.math_utils import calculate_pnl

from research.backtests.emergency_lock.cost_model import BPS_DIVISOR, fee_usdt

from .config import CoberturaConfig, IndividualTpStep
from .economics import adverse_short_exit_price
from .ledger import round_price, round_qty

TrancheStatus = Literal["open", "partial", "closed"]


@dataclass
class OverlayTranche:
    tranche_id: str
    round_id: int
    entry_timestamp: str
    entry_price_raw: float
    entry_price_filled: float
    initial_qty: float
    remaining_qty: float
    open_fee_usdt: float
    tp_pct: float
    tp_trigger_price: float
    close_timestamp: str | None = None
    close_price: float | None = None
    close_fee_usdt: float = 0.0
    realized_pnl_usdt: float = 0.0
    status: TrancheStatus = "open"
    # Scaled TP bookkeeping
    steps_completed: int = 0
    side: str = "short"
    level: int | None = None
    active_from_next_bar: bool = True
    tp_active: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def short_tp_optical_trigger(entry_filled: float, tp_pct: float) -> float:
    """Direction check: short TP is below entry."""
    return float(entry_filled) * (1.0 - float(tp_pct))


def solve_short_tp_trigger(
    *,
    entry_filled: float,
    qty: float,
    open_fee_allocated: float,
    tp_pct: float,
    cfg: CoberturaConfig,
    fee_buffer_usdt: float,
) -> float:
    """Solve trigger so net PnL after costs >= qty * entry * tp_pct.

    Short close fill = trigger * (1 + slip_close). Lower trigger => more profit.
    """
    qty_f = float(qty)
    entry = float(entry_filled)
    if qty_f <= 0.0 or entry <= 0.0:
        raise ValueError("qty and entry must be positive")
    target_net = qty_f * entry * float(tp_pct)
    fee_close = float(cfg.fee_rate_close)
    slip = float(cfg.slippage_bps_close) / BPS_DIVISOR
    buffer = float(fee_buffer_usdt)

    # qty*(entry - fill) - open_fee_alloc - fee_close*fill*qty - buffer >= target
    # qty*entry - fill*qty*(1+fee_close) >= target + open_fee_alloc + buffer
    rhs = qty_f * entry - target_net - float(open_fee_allocated) - buffer
    denom = qty_f * (1.0 + fee_close)
    fill = rhs / denom
    if fill <= 0.0:
        raise ValueError("non-positive TP fill solved")
    trigger = fill / (1.0 + slip) if slip > -1.0 else fill
    tick = float(cfg.tick_size)
    # Floor: ensure economics still meet target after rounding.
    trigger = max(tick, (int(trigger / tick)) * tick)
    for _ in range(50):
        net = _short_tp_net_at_trigger(
            entry_filled=entry,
            qty=qty_f,
            open_fee_allocated=open_fee_allocated,
            trigger=trigger,
            cfg=cfg,
            fee_buffer_usdt=buffer,
        )
        if net + 1e-9 >= target_net:
            return trigger
        trigger = round_price(trigger - tick, cfg.tick_size)
        if trigger <= 0.0:
            break
    raise ValueError("unable to solve fee-aware short TP trigger")


def _short_tp_net_at_trigger(
    *,
    entry_filled: float,
    qty: float,
    open_fee_allocated: float,
    trigger: float,
    cfg: CoberturaConfig,
    fee_buffer_usdt: float,
) -> float:
    fill = adverse_short_exit_price(trigger, cfg.slippage_bps_close)
    gross = calculate_pnl(entry_filled, fill, qty, "short")
    close_fee = fee_usdt(fill_price=fill, qty=qty, fee_rate=cfg.fee_rate_close)
    return gross - float(open_fee_allocated) - close_fee - float(fee_buffer_usdt)


@dataclass
class TrancheBook:
    """Open/closed overlay short tranches for individual TP policies."""

    tranches: list[OverlayTranche] = field(default_factory=list)
    _seq: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)

    def next_id(self, round_id: int) -> str:
        self._seq += 1
        return f"R{round_id}-T{self._seq}"

    def open_tranches(self) -> list[OverlayTranche]:
        return [t for t in self.tranches if t.remaining_qty > 1e-12]

    def remaining_qty(self) -> float:
        return sum(t.remaining_qty for t in self.open_tranches())

    def activate_pending(self) -> None:
        for t in self.open_tranches():
            if t.active_from_next_bar:
                t.tp_active = True
                t.active_from_next_bar = False

    def create_short_tranche(
        self,
        *,
        cfg: CoberturaConfig,
        round_id: int,
        timestamp: str,
        entry_price_raw: float,
        entry_price_filled: float,
        qty: float,
        open_fee_usdt: float,
        level: int | None,
    ) -> OverlayTranche:
        qty_f = float(qty)
        tp_pct = 0.0
        trigger = 0.0
        optical = 0.0
        if cfg.overlay_exit_policy in ("individual_tp", "individual_tp_scaled"):
            tp_pct = float(cfg.individual_tp_pct)
            if cfg.overlay_exit_policy == "individual_tp_scaled":
                if not cfg.individual_tp_steps:
                    raise ValueError("individual_tp_scaled requires individual_tp_steps")
                tp_pct = float(cfg.individual_tp_steps[0].move_pct)

            close_qty_for_solve = qty_f
            if cfg.overlay_exit_policy == "individual_tp":
                close_qty_for_solve = round_qty(
                    qty_f * float(cfg.individual_tp_close_fraction), cfg.qty_step
                )
                if close_qty_for_solve <= 0.0:
                    close_qty_for_solve = qty_f
            else:
                frac = float(cfg.individual_tp_steps[0].close_fraction)
                close_qty_for_solve = round_qty(qty_f * frac, cfg.qty_step)
                if close_qty_for_solve <= 0.0:
                    close_qty_for_solve = qty_f

            open_fee_alloc = float(open_fee_usdt) * (close_qty_for_solve / qty_f)
            buffer = float(cfg.individual_tp_fee_buffer_usdt)
            trigger = solve_short_tp_trigger(
                entry_filled=entry_price_filled,
                qty=close_qty_for_solve,
                open_fee_allocated=open_fee_alloc,
                tp_pct=tp_pct,
                cfg=cfg,
                fee_buffer_usdt=buffer,
            )
            optical = short_tp_optical_trigger(entry_price_filled, tp_pct)
            if trigger >= float(entry_price_filled):
                raise ValueError("short TP trigger must be below entry")

        tranche = OverlayTranche(
            tranche_id=self.next_id(round_id),
            round_id=round_id,
            entry_timestamp=timestamp,
            entry_price_raw=float(entry_price_raw),
            entry_price_filled=float(entry_price_filled),
            initial_qty=qty_f,
            remaining_qty=qty_f,
            open_fee_usdt=float(open_fee_usdt),
            tp_pct=tp_pct,
            tp_trigger_price=trigger,
            status="open",
            level=level,
            active_from_next_bar=cfg.overlay_exit_policy
            in ("individual_tp", "individual_tp_scaled"),
            tp_active=False,
        )
        self.tranches.append(tranche)
        self.events.append(
            {
                "event": "tranche_open",
                "timestamp": timestamp,
                "tranche_id": tranche.tranche_id,
                "round_id": round_id,
                "qty": qty_f,
                "entry_price_raw": entry_price_raw,
                "entry_price_filled": entry_price_filled,
                "tp_pct": tp_pct,
                "tp_trigger_price": trigger,
                "optical_tp_trigger": optical,
                "open_fee_usdt": open_fee_usdt,
                "level": level,
                "overlay_exit_policy": cfg.overlay_exit_policy,
            }
        )
        return tranche

    def current_step(self, cfg: CoberturaConfig, tranche: OverlayTranche) -> IndividualTpStep | None:
        if cfg.overlay_exit_policy != "individual_tp_scaled":
            return None
        steps = cfg.individual_tp_steps
        if tranche.steps_completed >= len(steps):
            return None
        return steps[tranche.steps_completed]

    def refresh_trigger(self, cfg: CoberturaConfig, tranche: OverlayTranche) -> None:
        if tranche.remaining_qty <= 1e-12:
            return
        if cfg.overlay_exit_policy == "individual_tp":
            frac = float(cfg.individual_tp_close_fraction)
            close_qty = round_qty(tranche.remaining_qty * frac, cfg.qty_step)
            if close_qty <= 0.0 or close_qty > tranche.remaining_qty + 1e-12:
                close_qty = tranche.remaining_qty
            tp_pct = float(cfg.individual_tp_pct)
        elif cfg.overlay_exit_policy == "individual_tp_scaled":
            step = self.current_step(cfg, tranche)
            if step is None:
                return
            tp_pct = float(step.move_pct)
            close_qty = round_qty(
                tranche.initial_qty * float(step.close_fraction), cfg.qty_step
            )
            close_qty = min(close_qty, tranche.remaining_qty)
            if close_qty <= 0.0:
                close_qty = tranche.remaining_qty
        else:
            return

        open_fee_alloc = float(tranche.open_fee_usdt) * (
            close_qty / float(tranche.initial_qty)
        )
        tranche.tp_pct = tp_pct
        tranche.tp_trigger_price = solve_short_tp_trigger(
            entry_filled=tranche.entry_price_filled,
            qty=close_qty,
            open_fee_allocated=open_fee_alloc,
            tp_pct=tp_pct,
            cfg=cfg,
            fee_buffer_usdt=float(cfg.individual_tp_fee_buffer_usdt),
        )

    def planned_close_qty(self, cfg: CoberturaConfig, tranche: OverlayTranche) -> float:
        if cfg.overlay_exit_policy == "individual_tp":
            qty = round_qty(
                tranche.remaining_qty * float(cfg.individual_tp_close_fraction),
                cfg.qty_step,
            )
            if qty <= 0.0 or abs(qty - tranche.remaining_qty) <= 1e-12:
                return tranche.remaining_qty
            return min(qty, tranche.remaining_qty)
        if cfg.overlay_exit_policy == "individual_tp_scaled":
            step = self.current_step(cfg, tranche)
            if step is None:
                return 0.0
            qty = round_qty(
                tranche.initial_qty * float(step.close_fraction), cfg.qty_step
            )
            return min(max(qty, 0.0), tranche.remaining_qty)
        return tranche.remaining_qty

    def apply_close(
        self,
        *,
        tranche: OverlayTranche,
        qty: float,
        fill_price: float,
        close_fee: float,
        realized_pnl: float,
        timestamp: str,
        partial: bool,
        cfg: CoberturaConfig,
    ) -> None:
        q = float(qty)
        if q <= 0.0:
            raise ValueError("close qty must be positive")
        if q - tranche.remaining_qty > 1e-9:
            raise ValueError("over-close tranche")
        tranche.remaining_qty = max(0.0, tranche.remaining_qty - q)
        tranche.close_fee_usdt += float(close_fee)
        tranche.realized_pnl_usdt += float(realized_pnl)
        tranche.close_timestamp = timestamp
        tranche.close_price = float(fill_price)
        if tranche.remaining_qty <= 1e-12:
            tranche.remaining_qty = 0.0
            tranche.status = "closed"
            tranche.tp_active = False
        else:
            tranche.status = "partial"
            if cfg.overlay_exit_policy == "individual_tp_scaled":
                tranche.steps_completed += 1
                if self.current_step(cfg, tranche) is None:
                    # No further scaled steps: leave remainder open without TP,
                    # or refresh if somehow leftover — mark inactive until new step.
                    tranche.tp_active = False
                    tranche.active_from_next_bar = False
                else:
                    self.refresh_trigger(cfg, tranche)
                    tranche.active_from_next_bar = True
                    tranche.tp_active = False
            elif cfg.overlay_exit_policy == "individual_tp":
                # Remaining after partial: re-arm TP on leftover for next bar.
                self.refresh_trigger(cfg, tranche)
                tranche.active_from_next_bar = True
                tranche.tp_active = False

        self.events.append(
            {
                "event": "tranche_tp_partial" if partial else "tranche_tp_close",
                "timestamp": timestamp,
                "tranche_id": tranche.tranche_id,
                "round_id": tranche.round_id,
                "qty": q,
                "fill_price": fill_price,
                "close_fee_usdt": close_fee,
                "realized_pnl_usdt": realized_pnl,
                "remaining_qty": tranche.remaining_qty,
                "status": tranche.status,
                "tp_trigger_price": tranche.tp_trigger_price,
                "steps_completed": tranche.steps_completed,
            }
        )

    def close_all_for_full_exit(
        self,
        *,
        timestamp: str,
        fill_price: float,
        close_fee_by_qty: float,
        realized_by_qty: float,
    ) -> None:
        """Mark remaining open tranches closed on full trade exit (fees/pnl booked elsewhere)."""
        for t in self.open_tranches():
            q = t.remaining_qty
            fee = close_fee_by_qty * q  # caller usually books aggregate; keep 0 here
            _ = fee
            t.remaining_qty = 0.0
            t.status = "closed"
            t.tp_active = False
            t.close_timestamp = timestamp
            t.close_price = float(fill_price)
            t.realized_pnl_usdt += float(realized_by_qty) * 0.0
            self.events.append(
                {
                    "event": "tranche_full_exit_close",
                    "timestamp": timestamp,
                    "tranche_id": t.tranche_id,
                    "round_id": t.round_id,
                    "qty": q,
                    "fill_price": fill_price,
                    "status": "closed",
                }
            )
