"""Bar-walk touch/rejection market entry engine (isolated Phase 4M)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from touch_rejection_logic import (
    invalid_long_entry_geometry,
    invalid_short_entry_geometry,
    long_pool_touch,
    long_rejection_confirmed,
    short_pool_touch,
    short_rejection_confirmed,
)


@dataclass
class WatchSpec:
    watch_key: str
    candidate_id: str
    direction: str
    watch_from_utc: str
    pool_lower: float
    pool_upper: float
    pool_identity: str
    landscape_intervals: list[dict]
    upper_tp_rows: list[dict]


@dataclass
class WatchState:
    spec: WatchSpec
    episode: int = 1
    phase: str = "IDLE"  # IDLE | TOUCHED | DONE | ABORTED
    touch_utc: str | None = None
    touch_bar_index: int | None = None


@dataclass
class PendingFill:
    intent_id: str
    direction: str
    candidate_id: str
    confirm_close_utc: str
    fill_bar_utc: str
    pool_lower: float
    pool_upper: float
    pool_identity: str
    landscape_intervals: list[dict]
    upper_tp_rows: list[dict]
    strategy_eligibility: str = "STRATEGY_ELIGIBILITY_UNRESOLVED"


@dataclass
class OpenPosition:
    trade_id: str
    intent_id: str
    direction: str
    candidate_id: str
    fill_utc: str
    entry_price: float
    sl_price: float
    tp_price: float
    exit_utc: str | None = None
    exit_reason: str = ""


def run_market_backtest(
    cfg: dict[str, Any],
    watches: list[WatchSpec],
    prices: dict[str, dict],
    lifecycle: Any,
    plan_short_fn: Any,
    plan_long_fn: Any,
    simulate_exit_fn: Any,
    zone_status_fn: Any,
    anchor_from_signal_fn: Any,
) -> dict[str, list[dict]]:
    times = sorted(prices.keys())
    pilot_from = cfg["pilot_from_utc"]
    pilot_to = cfg["pilot_to_utc"]
    times = [t for t in times if pilot_from <= t <= pilot_to]
    time_index = {t: i for i, t in enumerate(times)}

    touch_events: list[dict] = []
    rejections: list[dict] = []
    intents: list[dict] = []
    trades: list[dict] = []
    unresolved: list[dict] = []
    aborted: list[dict] = []

    states: list[WatchState] = [WatchState(spec=w) for w in watches]
    pending_fills: list[PendingFill] = []
    active: OpenPosition | None = None
    active_exit_utc: str | None = None
    trade_seq = 0
    intent_seq = 0
    touch_seq = 0
    fees = float(cfg["roundtrip_fees_pct"])

    def pool_ok_at_bar(spec: WatchSpec, bar_utc: str) -> tuple[bool, str]:
        anchor = anchor_from_signal_fn(
            spec.direction, spec.pool_lower, spec.pool_upper, spec.pool_identity
        )
        zs = zone_status_fn(anchor, bar_utc, lifecycle)
        if zs.state == "IDENTITY_AMBIGUOUS":
            return False, "POOL_IDENTITY_UNRESOLVED"
        if zs.state != "ACTIVE":
            return False, "ENTRY_POOL_DEPTH2_LOST"
        return True, ""

    for i, t in enumerate(times):
        bar = prices[t]

        # --- Market fill at this bar's open (after prior bar's confirm close) ---
        if active and active_exit_utc and t >= active_exit_utc:
            active = None
            active_exit_utc = None

        due = [p for p in pending_fills if p.fill_bar_utc == t]
        pending_fills = [p for p in pending_fills if p.fill_bar_utc != t]

        if due:
            shorts = [p for p in due if p.direction == "SHORT"]
            longs = [p for p in due if p.direction == "LONG"]
            blocked_simultaneous = bool(shorts and longs)
            if blocked_simultaneous:
                for p in due:
                    intents.append(
                        {
                            "intent_id": p.intent_id,
                            "candidate_id": p.candidate_id,
                            "direction": p.direction,
                            "rejection_confirm_utc": p.confirm_close_utc,
                            "scheduled_fill_bar_utc": p.fill_bar_utc,
                            "lifecycle": "SIMULTANEOUS_OPPOSING_MARKET_SIGNALS",
                            "strategy_eligibility": p.strategy_eligibility,
                            "market_entry_intent": "BLOCKED",
                        }
                    )
                    unresolved.append(
                        {
                            "candidate_id": p.candidate_id,
                            "bar_utc": t,
                            "reason": "SIMULTANEOUS_OPPOSING_MARKET_SIGNALS",
                            "detail": "No direction priority without user rule",
                        }
                    )
            else:
                due_sorted = sorted(due, key=lambda p: (p.confirm_close_utc, p.intent_id))
                opened_this_bar = False
                for p in due_sorted:
                    intents.append(
                        {
                            "intent_id": p.intent_id,
                            "candidate_id": p.candidate_id,
                            "direction": p.direction,
                            "rejection_confirm_utc": p.confirm_close_utc,
                            "scheduled_fill_bar_utc": p.fill_bar_utc,
                            "lifecycle": "MARKET_ENTRY_INTENT",
                            "strategy_eligibility": p.strategy_eligibility,
                            "market_entry_intent": "PENDING_FILL",
                        }
                    )
                    if active and (active_exit_utc is None or active_exit_utc == "" or t < active_exit_utc):
                        intents[-1]["market_entry_intent"] = "SUPPRESSED_POSITION_OPEN"
                        intents[-1]["lifecycle"] = "MARKET_ENTRY_SUPPRESSED"
                        continue
                    if opened_this_bar:
                        intents[-1]["market_entry_intent"] = "SUPPRESSED_SLOT_BUSY"
                        intents[-1]["lifecycle"] = "MARKET_ENTRY_SUPPRESSED"
                        continue

                    fill_price = bar["o"]
                    if p.direction == "SHORT":
                        plan = plan_short_fn(
                            p.pool_lower,
                            p.pool_upper,
                            p.landscape_intervals,
                            fill_price,
                            cfg,
                            quality_gate=False,
                        )
                        geom = invalid_short_entry_geometry(fill_price, plan.sl_price, plan.tp_price)
                    else:
                        plan = plan_long_fn(
                            p.pool_lower,
                            p.pool_upper,
                            p.upper_tp_rows,
                            fill_price,
                            cfg,
                            quality_gate=False,
                        )
                        geom = invalid_long_entry_geometry(fill_price, plan.sl_price, plan.tp_price)

                    if geom or plan.unresolved:
                        reason = geom or plan.unresolved or "INVALID_ENTRY_GEOMETRY"
                        intents[-1]["market_entry_intent"] = reason
                        intents[-1]["lifecycle"] = "MARKET_FILL_SKIPPED"
                        unresolved.append(
                            {
                                "candidate_id": p.candidate_id,
                                "bar_utc": t,
                                "reason": reason,
                                "fill_price": fill_price,
                            }
                        )
                        continue

                    trade_seq += 1
                    tid = f"T4M_{trade_seq:03d}"
                    ex = simulate_exit_fn(
                        p.direction,
                        t,
                        fill_price,
                        plan.sl_price,
                        plan.tp_price,
                        prices,
                        pilot_to,
                        fees,
                    )
                    intents[-1]["market_entry_intent"] = "MARKET_FILL"
                    intents[-1]["fill_price"] = fill_price
                    intents[-1]["sl_price"] = plan.sl_price
                    intents[-1]["tp_price"] = plan.tp_price

                    pos = OpenPosition(
                        trade_id=tid,
                        intent_id=p.intent_id,
                        direction=p.direction,
                        candidate_id=p.candidate_id,
                        fill_utc=t,
                        entry_price=fill_price,
                        sl_price=plan.sl_price,
                        tp_price=plan.tp_price,
                        exit_utc=ex.exit_utc,
                        exit_reason=ex.exit_reason,
                    )
                    active = pos
                    active_exit_utc = ex.exit_utc or ""
                    opened_this_bar = True

                    trades.append(
                        {
                            "trade_id": tid,
                            "intent_id": p.intent_id,
                            "candidate_id": p.candidate_id,
                            "direction": p.direction,
                            "rejection_confirm_utc": p.confirm_close_utc,
                            "fill_utc": t,
                            "entry_price": fill_price,
                            "sl_price": plan.sl_price,
                            "tp_price": plan.tp_price,
                            "fill_model": "NEXT_5M_OPEN_DIAGNOSTIC",
                            "slippage_modeled": cfg.get("slippage_modeled", False),
                            "strategy_eligibility": p.strategy_eligibility,
                            "performance_validated": False,
                            "exit_utc": ex.exit_utc or "",
                            "exit_reason": ex.exit_reason,
                            "exit_price": ex.exit_price if ex.exit_price is not None else "",
                            "gross_pnl_pct": ex.gross_pnl_pct if ex.gross_pnl_pct is not None else "",
                            "net_pnl_pct": ex.net_pnl_pct if ex.net_pnl_pct is not None else "",
                            "entry_mode": "TOUCH_REJECTION_MARKET_V1",
                        }
                    )
                    if ex.exit_reason in ("AMBIGUOUS_INTRABAR_EXIT", "AMBIGUOUS_ENTRY_EXIT_SEQUENCE"):
                        unresolved.append(
                            {
                                "candidate_id": p.candidate_id,
                                "bar_utc": t,
                                "reason": ex.exit_reason,
                                "trade_id": tid,
                            }
                        )

        # --- Watch updates at bar close ---
        for ws in states:
            spec = ws.spec
            if t < spec.watch_from_utc:
                continue
            if ws.phase == "DONE":
                continue

            ok, pool_reason = pool_ok_at_bar(spec, t)
            if ws.phase == "TOUCHED" and not ok:
                aborted.append(
                    {
                        "candidate_id": spec.candidate_id,
                        "direction": spec.direction,
                        "episode": ws.episode,
                        "touch_utc": ws.touch_utc,
                        "abort_utc": t,
                        "reason": pool_reason,
                    }
                )
                ws.phase = "IDLE"
                ws.touch_utc = None
                ws.touch_bar_index = None
                ws.episode += 1
                continue

            if ws.phase == "IDLE":
                touched = (
                    short_pool_touch(bar, spec.pool_lower)
                    if spec.direction == "SHORT"
                    else long_pool_touch(bar, spec.pool_upper)
                )
                if touched:
                    touch_seq += 1
                    ws.phase = "TOUCHED"
                    ws.touch_utc = t
                    ws.touch_bar_index = i
                    touch_events.append(
                        {
                            "touch_event_id": f"TOUCH_{touch_seq:04d}",
                            "candidate_id": spec.candidate_id,
                            "direction": spec.direction,
                            "episode": ws.episode,
                            "touch_candle_utc": t,
                            "pool_lower": spec.pool_lower,
                            "pool_upper": spec.pool_upper,
                            "pool_identity": spec.pool_identity,
                            "lifecycle": "RAW_POOL_TOUCH",
                            "strategy_eligibility": "STRATEGY_ELIGIBILITY_UNRESOLVED",
                        }
                    )
                continue

            if ws.phase == "TOUCHED" and ws.touch_bar_index is not None and i > ws.touch_bar_index:
                confirmed = (
                    short_rejection_confirmed(bar, spec.pool_lower)
                    if spec.direction == "SHORT"
                    else long_rejection_confirmed(bar, spec.pool_upper)
                )
                if confirmed:
                    if not ok:
                        aborted.append(
                            {
                                "candidate_id": spec.candidate_id,
                                "direction": spec.direction,
                                "episode": ws.episode,
                                "touch_utc": ws.touch_utc,
                                "abort_utc": t,
                                "reason": pool_reason,
                            }
                        )
                        ws.phase = "IDLE"
                        ws.touch_utc = None
                        ws.touch_bar_index = None
                        ws.episode += 1
                        continue

                    intent_seq += 1
                    iid = f"INTENT_{intent_seq:04d}"
                    fill_bar = times[i + 1] if i + 1 < len(times) else ""
                    rejections.append(
                        {
                            "confirmation_id": iid,
                            "candidate_id": spec.candidate_id,
                            "direction": spec.direction,
                            "episode": ws.episode,
                            "touch_candle_utc": ws.touch_utc,
                            "confirm_candle_utc": t,
                            "pool_lower": spec.pool_lower,
                            "pool_upper": spec.pool_upper,
                            "lifecycle": "REJECTION_CONFIRMED",
                            "market_signal": f"{spec.direction}_MARKET_SIGNAL",
                        }
                    )
                    ws.phase = "DONE"
                    if not fill_bar:
                        unresolved.append(
                            {
                                "candidate_id": spec.candidate_id,
                                "bar_utc": t,
                                "reason": "NO_FILL_DATA",
                                "detail": "No next 5m bar after confirmation",
                            }
                        )
                    else:
                        pending_fills.append(
                            PendingFill(
                                intent_id=iid,
                                direction=spec.direction,
                                candidate_id=spec.candidate_id,
                                confirm_close_utc=t,
                                fill_bar_utc=fill_bar,
                                pool_lower=spec.pool_lower,
                                pool_upper=spec.pool_upper,
                                pool_identity=spec.pool_identity,
                                landscape_intervals=spec.landscape_intervals,
                                upper_tp_rows=spec.upper_tp_rows,
                            )
                        )

    return {
        "pool_touch_events": touch_events,
        "rejection_confirmations": rejections,
        "market_entry_intents": intents,
        "simulated_market_trades": trades,
        "pool_watch_aborts": aborted,
        "unresolved_strategy_decisions": unresolved,
    }
