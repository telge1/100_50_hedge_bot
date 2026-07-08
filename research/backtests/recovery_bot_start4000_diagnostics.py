from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

from research.backtests.recovery_bot.calculations import compute_net_long_qty


REPO_ROOT = Path(__file__).resolve().parents[2]
RESULT_DIR = (
    REPO_ROOT / "research" / "backtests" / "results" / "recovery_bot_current_three_audit"
)
LIFECYCLE_DIR = RESULT_DIR / "lifecycle_audit"


@dataclass
class EventRow:
    candle_index: int | None
    timestamp: str | None
    market_price: float | None
    event_type: str
    action: str | None
    reason: str | None
    recovery_state_before: str | None
    recovery_state_after: str | None
    long_qty: float | None
    short_qty: float | None
    net_long_qty: float | None
    long_avg_price: float | None
    short_avg_price: float | None
    realized_pnl: float | None
    unrealized_pnl: float | None
    overall_pnl: float | None
    recovery_realized_pnl: float | None
    loss_budget_usdt: float | None
    loss_budget_used_usdt: float | None
    loss_budget_remaining_usdt: float | None
    active_orders_count: int
    active_orders_repr: str
    neutralization_count: int
    pair_reduction_count: int
    reload_count: int
    minimum_pair_reached: bool
    final_exit_attempted: bool
    blocked_reason: str | None


def _load_full_result() -> Dict[str, Any]:
    path = RESULT_DIR / "APTUSDT_start4000_full.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _build_events(result: Dict[str, Any]) -> List[EventRow]:
    fill_log: List[Dict[str, Any]] = list(result.get("fill_log") or [])
    order_log: List[Dict[str, Any]] = list(result.get("order_log") or [])
    trace: List[Dict[str, Any]] = list(result.get("recovery_trace") or [])

    combined: List[Dict[str, Any]] = []

    for idx, ev in enumerate(fill_log):
        combined.append(
            {
                "source": "fill_log",
                "source_index": idx,
                "raw": ev,
                "candle_index": ev.get("candle_index"),
                "timestamp": ev.get("timestamp"),
            }
        )
    for idx, ev in enumerate(order_log):
        combined.append(
            {
                "source": "order_log",
                "source_index": idx,
                "raw": ev,
                "candle_index": ev.get("candle_index"),
                "timestamp": ev.get("timestamp"),
            }
        )
    for idx, ev in enumerate(trace):
        combined.append(
            {
                "source": "recovery_trace",
                "source_index": idx,
                "raw": ev,
                "candle_index": ev.get("candle_index"),
                "timestamp": ev.get("timestamp"),
            }
        )

    def _sort_key(item: Dict[str, Any]) -> Tuple[int, str, int]:
        ci = item.get("candle_index")
        ci_int = int(ci) if isinstance(ci, int) else 10**9
        ts = str(item.get("timestamp") or "")
        source = str(item.get("source") or "")
        # recovery_trace vor order_log vor fill_log
        prio = {"recovery_trace": 0, "order_log": 1, "fill_log": 2}.get(source, 3)
        return ci_int, ts, prio

    combined.sort(key=_sort_key)

    events: List[EventRow] = []

    long_qty: float | None = None
    short_qty: float | None = None
    long_avg: float | None = None
    short_avg: float | None = None
    realized_pnl_cum: float = 0.0
    recovery_realized_pnl: float = 0.0
    loss_budget_usdt: float | None = None
    loss_budget_used: float | None = None
    loss_budget_remaining: float | None = None
    recovery_state: str | None = None
    neutralization_count = 0
    pair_reduction_count = 0
    reload_count = 0
    minimum_pair_reached = False
    final_exit_attempted = False
    blocked_reason: str | None = None

    orders: Dict[str, Dict[str, Any]] = {}

    for item in combined:
        src = item["source"]
        ev = item["raw"]
        ci = ev.get("candle_index")
        ts = ev.get("timestamp")
        market_price: float | None = None
        event_type = ""
        action: str | None = None
        reason: str | None = None

        state_before = recovery_state
        state_after = recovery_state

        # Update from recovery_trace first (State, Budget, Position).
        if src == "recovery_trace":
            event_type = "recovery"
            action = str(ev.get("action") or "")
            reason = ev.get("reason")
            market_price = _safe_float(ev.get("current_price"))
            loss_budget_usdt = _safe_float(ev.get("loss_budget_usdt"))
            loss_budget_used = _safe_float(ev.get("loss_budget_used_usdt"))
            # bevorzugt explizites remaining_loss_budget_usdt
            loss_budget_remaining = _safe_float(ev.get("remaining_loss_budget_usdt"))
            if loss_budget_remaining is None and (
                loss_budget_usdt is not None and loss_budget_used is not None
            ):
                loss_budget_remaining = max(loss_budget_usdt - loss_budget_used, 0.0)
            long_qty = _safe_float(ev.get("long_qty")) or long_qty
            short_qty = _safe_float(ev.get("short_qty")) or short_qty
            long_avg = _safe_float(ev.get("long_avg")) or long_avg
            short_avg = _safe_float(ev.get("short_avg")) or short_avg
            state_before = str(ev.get("state_before") or recovery_state or "")
            state_after = str(ev.get("state_after") or state_before)
            recovery_state = state_after
            if action == "NEUTRALIZATION_FILLED":
                neutralization_count += 1
            if action == "PAIR_REDUCTION_FILLED":
                pair_reduction_count += 1
            if action == "RELOAD_FILLED":
                reload_count += 1
            if action == "MINIMUM_PAIR_REACHED":
                minimum_pair_reached = True
            if action in {"FINAL_EXIT_EVALUATED", "FINAL_EXIT_SUBMITTED"}:
                final_exit_attempted = True
            if "BLOCK" in action or "FAILED" in action:
                blocked_reason = reason or blocked_reason

        elif src == "fill_log":
            event_type = "fill"
            action = str(ev.get("purpose") or ev.get("purpose_original") or "")
            market_price = _safe_float(ev.get("candle_close")) or _safe_float(
                ev.get("fill_price")
            )
            # Positions- und Durchschnitte aus dem Fill übernehmen.
            long_after = _safe_float(ev.get("long_qty_after"))
            short_after = _safe_float(ev.get("short_qty_after"))
            long_avg_after = _safe_float(ev.get("long_avg_after"))
            short_avg_after = _safe_float(ev.get("short_avg_after"))
            long_qty = long_after if long_after is not None else long_qty
            short_qty = short_after if short_after is not None else short_qty
            long_avg = long_avg_after if long_avg_after is not None else long_avg
            short_avg = short_avg_after if short_avg_after is not None else short_avg
            closed_pnl = _safe_float(ev.get("closed_pnl")) or 0.0
            realized_pnl_cum += closed_pnl
            if action.startswith("RECOVERY_"):
                recovery_realized_pnl += closed_pnl

        elif src == "order_log":
            et = str(ev.get("event_type") or "").lower()
            if et == "submitted":
                event_type = "order_submitted"
            elif et == "cancelled":
                event_type = "order_cancelled"
            else:
                event_type = f"order_{et or 'event'}"
            action = str(ev.get("purpose") or ev.get("purpose_original") or "")
            market_price = None
            oid = str(ev.get("order_id") or "")
            if oid:
                orders.setdefault(oid, {}).update(
                    {
                        "order_id": oid,
                        "side": ev.get("side"),
                        "qty": _safe_float(ev.get("qty")),
                        "price": _safe_float(ev.get("price")),
                        "trigger_price": _safe_float(ev.get("trigger_price")),
                        "reduce_only": bool(ev.get("reduce_only")),
                        "status": ev.get("status"),
                        "purpose": action,
                    }
                )

        # Net-Qty und Ordersnapshot vorbereiten.
        net_long_qty = None
        if long_qty is not None and short_qty is not None:
            net_long_qty = compute_net_long_qty(long_qty, short_qty)

        active_orders: List[Dict[str, Any]] = []
        for order in orders.values():
            status = str(order.get("status") or "").upper()
            if status in {"CANCELED", "FILLED"}:
                continue
            active_orders.append(order)
        active_orders_count = len(active_orders)
        active_orders_repr = "|".join(
            f"{o.get('order_id')}:{o.get('purpose')}:{o.get('side')}:{o.get('qty')}:{o.get('price')}:{o.get('reduce_only')}"
            for o in active_orders
        )

        row = EventRow(
            candle_index=ci if isinstance(ci, int) else None,
            timestamp=ts,
            market_price=market_price,
            event_type=event_type or src,
            action=action,
            reason=reason,
            recovery_state_before=state_before,
            recovery_state_after=state_after,
            long_qty=long_qty,
            short_qty=short_qty,
            net_long_qty=net_long_qty,
            long_avg_price=long_avg,
            short_avg_price=short_avg,
            realized_pnl=realized_pnl_cum,
            unrealized_pnl=None,
            overall_pnl=None,
            recovery_realized_pnl=recovery_realized_pnl,
            loss_budget_usdt=loss_budget_usdt,
            loss_budget_used_usdt=loss_budget_used,
            loss_budget_remaining_usdt=loss_budget_remaining,
            active_orders_count=active_orders_count,
            active_orders_repr=active_orders_repr,
            neutralization_count=neutralization_count,
            pair_reduction_count=pair_reduction_count,
            reload_count=reload_count,
            minimum_pair_reached=minimum_pair_reached,
            final_exit_attempted=final_exit_attempted,
            blocked_reason=blocked_reason,
        )
        events.append(row)

    # Periodische Snapshots alle 250 Candles.
    candles_processed = int(result.get("candles_processed") or 0)
    snapshot_indices = list(range(0, max(candles_processed, 1), 250))
    if snapshot_indices and snapshot_indices[-1] != candles_processed - 1:
        snapshot_indices.append(candles_processed - 1)

    def _state_at_or_before(candle_idx: int) -> EventRow | None:
        candidate: EventRow | None = None
        for ev in events:
            if ev.candle_index is None:
                continue
            if ev.candle_index <= candle_idx:
                candidate = ev
            else:
                break
        return candidate

    for s_idx in snapshot_indices:
        base = _state_at_or_before(s_idx)
        if base is None:
            continue
        events.append(
            EventRow(
                candle_index=s_idx,
                timestamp=base.timestamp,
                market_price=base.market_price,
                event_type="snapshot",
                action=None,
                reason=None,
                recovery_state_before=base.recovery_state_after,
                recovery_state_after=base.recovery_state_after,
                long_qty=base.long_qty,
                short_qty=base.short_qty,
                net_long_qty=base.net_long_qty,
                long_avg_price=base.long_avg_price,
                short_avg_price=base.short_avg_price,
                realized_pnl=base.realized_pnl,
                unrealized_pnl=base.unrealized_pnl,
                overall_pnl=base.overall_pnl,
                recovery_realized_pnl=base.recovery_realized_pnl,
                loss_budget_usdt=base.loss_budget_usdt,
                loss_budget_used_usdt=base.loss_budget_used_usdt,
                loss_budget_remaining_usdt=base.loss_budget_remaining_usdt,
                active_orders_count=base.active_orders_count,
                active_orders_repr=base.active_orders_repr,
                neutralization_count=base.neutralization_count,
                pair_reduction_count=base.pair_reduction_count,
                reload_count=base.reload_count,
                minimum_pair_reached=base.minimum_pair_reached,
                final_exit_attempted=base.final_exit_attempted,
                blocked_reason=base.blocked_reason,
            )
        )

    events.sort(key=lambda e: (e.candle_index if e.candle_index is not None else 10**9, e.timestamp or ""))
    return events


def _write_event_log_csv(path: Path, events: List[EventRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "candle_index",
        "timestamp",
        "market_price",
        "event_type",
        "action",
        "reason",
        "recovery_state_before",
        "recovery_state_after",
        "long_qty",
        "short_qty",
        "net_long_qty",
        "long_avg_price",
        "short_avg_price",
        "realized_pnl",
        "unrealized_pnl",
        "overall_pnl",
        "recovery_realized_pnl",
        "loss_budget_usdt",
        "loss_budget_used_usdt",
        "loss_budget_remaining_usdt",
        "active_orders_count",
        "active_orders",
        "neutralization_count",
        "pair_reduction_count",
        "reload_count",
        "minimum_pair_reached",
        "final_exit_attempted",
        "blocked_reason",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for e in events:
            writer.writerow(
                {
                    "candle_index": e.candle_index,
                    "timestamp": e.timestamp,
                    "market_price": e.market_price,
                    "event_type": e.event_type,
                    "action": e.action,
                    "reason": e.reason,
                    "recovery_state_before": e.recovery_state_before,
                    "recovery_state_after": e.recovery_state_after,
                    "long_qty": e.long_qty,
                    "short_qty": e.short_qty,
                    "net_long_qty": e.net_long_qty,
                    "long_avg_price": e.long_avg_price,
                    "short_avg_price": e.short_avg_price,
                    "realized_pnl": e.realized_pnl,
                    "unrealized_pnl": e.unrealized_pnl,
                    "overall_pnl": e.overall_pnl,
                    "recovery_realized_pnl": e.recovery_realized_pnl,
                    "loss_budget_usdt": e.loss_budget_usdt,
                    "loss_budget_used_usdt": e.loss_budget_used_usdt,
                    "loss_budget_remaining_usdt": e.loss_budget_remaining_usdt,
                    "active_orders_count": e.active_orders_count,
                    "active_orders": e.active_orders_repr,
                    "neutralization_count": e.neutralization_count,
                    "pair_reduction_count": e.pair_reduction_count,
                    "reload_count": e.reload_count,
                    "minimum_pair_reached": e.minimum_pair_reached,
                    "final_exit_attempted": e.final_exit_attempted,
                    "blocked_reason": e.blocked_reason,
                }
            )


def _write_timeline_csv(path: Path, events: List[EventRow]) -> None:
    """Kompakte Timeline mit nur wesentlichen Events."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "candle_index",
        "timestamp",
        "event_type",
        "action",
        "reason",
        "recovery_state_before",
        "recovery_state_after",
        "long_qty",
        "short_qty",
        "net_long_qty",
        "loss_budget_usdt",
        "loss_budget_used_usdt",
        "loss_budget_remaining_usdt",
        "neutralization_count",
        "pair_reduction_count",
        "reload_count",
        "blocked_reason",
    ]
    key_actions = {
        "recovery",
        "NEUTRALIZATION_SUBMITTED",
        "NEUTRALIZATION_FILLED",
        "NEUTRALIZATION_BLOCKED",
        "PAIR_REDUCTION_SUBMITTED",
        "PAIR_REDUCTION_FILLED",
        "RELOAD_WAITING",
        "RELOAD_FILLED",
        "FINAL_EXIT_EVALUATED",
        "FINAL_EXIT_SUBMITTED",
        "FINAL_EXIT_FILLED",
        "RECOVERY_FAILED",
    }
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for e in events:
            if e.event_type == "snapshot":
                continue
            if e.event_type == "recovery" or (e.action in key_actions):
                writer.writerow(
                    {
                        "candle_index": e.candle_index,
                        "timestamp": e.timestamp,
                        "event_type": e.event_type,
                        "action": e.action,
                        "reason": e.reason,
                        "recovery_state_before": e.recovery_state_before,
                        "recovery_state_after": e.recovery_state_after,
                        "long_qty": e.long_qty,
                        "short_qty": e.short_qty,
                        "net_long_qty": e.net_long_qty,
                        "loss_budget_usdt": e.loss_budget_usdt,
                        "loss_budget_used_usdt": e.loss_budget_used_usdt,
                        "loss_budget_remaining_usdt": e.loss_budget_remaining_usdt,
                        "neutralization_count": e.neutralization_count,
                        "pair_reduction_count": e.pair_reduction_count,
                        "reload_count": e.reload_count,
                        "blocked_reason": e.blocked_reason,
                    }
                )


def _write_markdown_summary(path: Path, result: Dict[str, Any], events: List[EventRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    trace: List[Dict[str, Any]] = list(result.get("recovery_trace") or [])
    fills: List[Dict[str, Any]] = list(result.get("fill_log") or [])
    summary: Dict[str, Any] = dict(result.get("recovery_summary") or {})

    def _first_trace(action: str) -> Dict[str, Any] | None:
        for e in trace:
            if str(e.get("action") or "") == action:
                return e
        return None

    initial_fill = next(
        (f for f in fills if str(f.get("purpose") or "") == "INITIAL_LONG_ENTRY"),
        fills[0] if fills else None,
    )
    triggered = _first_trace("RECOVERY_TRIGGERED")

    with path.open("w", encoding="utf-8") as handle:
        handle.write("# Start 4000 – Recovery-Diagnose\n\n")

        handle.write("## 1. Initial Entry und Konfiguration\n\n")
        if initial_fill:
            handle.write(
                f"- **Initialer Long-Entry**: candle_index={initial_fill.get('candle_index')}, "
                f"timestamp={initial_fill.get('timestamp')}, "
                f"qty={initial_fill.get('qty')}, "
                f"fill_price={initial_fill.get('fill_price')}\n"
            )
        cfg = dict(result.get("config_diagnostics") or {})
        handle.write(
            f"- **Config-Quelle**: config_source={cfg.get('config_source')}, "
            f"config_path={cfg.get('config_path')}\n"
        )
        handle.write(
            f"- **Recovery-Loss-Budget laut Summary**: "
            f"loss_budget_usdt={summary.get('loss_budget_usdt')}, "
            f"loss_budget_used_usdt={summary.get('loss_budget_used_usdt')}\n\n"
        )

        handle.write("## 2. Recovery-Aktivierung\n\n")
        if triggered:
            handle.write(
                f"- **RECOVERY_TRIGGERED**: candle_index={triggered.get('candle_index')}, "
                f"timestamp={triggered.get('timestamp')}, "
                f"price={triggered.get('current_price')}\n\n"
            )
        else:
            handle.write("- Kein `RECOVERY_TRIGGERED` im Trace gefunden.\n\n")

        handle.write("## 3. State-Wechsel\n\n")
        last_state = None
        for e in trace:
            before = str(e.get("state_before") or "")
            after = str(e.get("state_after") or "")
            action = str(e.get("action") or "")
            if before != after or action in {
                "RECOVERY_TRIGGERED",
                "MINIMUM_PAIR_REACHED",
                "FINAL_EXIT_EVALUATED",
            }:
                handle.write(
                    f"- action={action}: {before} → {after} "
                    f"(candle_index={e.get('candle_index')}, timestamp={e.get('timestamp')})\n"
                )
                last_state = after
        handle.write("\n")

        handle.write("## 4. Neutralisierungen, Pair Reduction, Reloads\n\n")
        for action in ["NEUTRALIZATION_FILLED", "PAIR_REDUCTION_FILLED", "RELOAD_FILLED"]:
            entries = [e for e in trace if str(e.get("action") or "") == action]
            handle.write(f"### {action}\n\n")
            if not entries:
                handle.write("- keine Einträge\n\n")
                continue
            for e in entries:
                handle.write(
                    f"- candle_index={e.get('candle_index')}, timestamp={e.get('timestamp')}, "
                    f"price={e.get('current_price')}, "
                    f"long_qty={e.get('long_qty')}, short_qty={e.get('short_qty')}\n"
                )
            handle.write("\n")

        handle.write("## 5. Entwicklung von Position und Verlustbudget\n\n")
        for e in trace:
            if str(e.get("action") or "") in {
                "RECOVERY_TRIGGERED",
                "NEUTRALIZATION_FILLED",
                "NEUTRALIZATION_BLOCKED",
            }:
                handle.write(
                    f"- {e.get('action')}: candle_index={e.get('candle_index')}, "
                    f"price={e.get('current_price')}, "
                    f"long_qty={e.get('long_qty')}, short_qty={e.get('short_qty')}, "
                    f"loss_budget={e.get('loss_budget_usdt')}, "
                    f"loss_budget_used={e.get('loss_budget_used_usdt')}, "
                    f"remaining_loss_budget={e.get('remaining_loss_budget_usdt')}\n"
                )
        handle.write("\n")

        handle.write("## 6. Letzter erfolgreicher Schritt und erster dauerhafter Block\n\n")
        last_success: Dict[str, Any] | None = None
        first_block: Dict[str, Any] | None = None
        for e in trace:
            action = str(e.get("action") or "")
            if "BLOCKED" in action or "FAILED" in action:
                if first_block is None:
                    first_block = e
            else:
                last_success = e
        if last_success:
            handle.write(
                f"- **Letzter erfolgreicher Schritt**: action={last_success.get('action')}, "
                f"candle_index={last_success.get('candle_index')}, "
                f"timestamp={last_success.get('timestamp')}\n"
            )
        if first_block:
            handle.write(
                f"- **Erste dauerhafte Blockierung**: action={first_block.get('action')}, "
                f"reason={first_block.get('reason')}, "
                f"candle_index={first_block.get('candle_index')}, "
                f"timestamp={first_block.get('timestamp')}\n"
            )
        handle.write("\n")

        handle.write("## 7. Finaler Zustand\n\n")
        handle.write(
            f"- final_state={summary.get('final_state')}, "
            f"blocked_reason={summary.get('blocked_reason')}, "
            f"remaining_long_qty={summary.get('remaining_long_qty')}, "
            f"remaining_short_qty={summary.get('remaining_short_qty')}, "
            f"active_orders_remaining={summary.get('active_orders_remaining')}\n\n"
        )


def main() -> int:
    result = _load_full_result()
    events = _build_events(result)

    # Vollständiges Event-Log.
    event_log_path = LIFECYCLE_DIR / "APTUSDT_start4000_event_log.csv"
    _write_event_log_csv(event_log_path, events)

    # Kompakte Timeline.
    timeline_path = LIFECYCLE_DIR / "APTUSDT_start4000_timeline.csv"
    _write_timeline_csv(timeline_path, events)

    # Markdown-Diagnose.
    md_path = LIFECYCLE_DIR / "APTUSDT_start4000_diagnostics.md"
    _write_markdown_summary(md_path, result, events)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

