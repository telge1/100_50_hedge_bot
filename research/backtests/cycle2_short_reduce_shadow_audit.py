from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from research.backtests.candle_loader import (
    DEFAULT_DATA_DIR,
    load_candles_for_symbol,
)


REPO_ROOT = Path(__file__).resolve().parents[2]

# Default: strenger No-Refill/No-Recovery-Start4000-Lauf als Basis
DEFAULT_RESULT_PATH = (
    REPO_ROOT
    / "research"
    / "backtests"
    / "results"
    / "start4000_strict_no_refill_no_recovery"
    / "APTUSDT_start4000_no_refill_no_recovery_full.json"
)

OUT_DIR = (
    REPO_ROOT
    / "research"
    / "backtests"
    / "results"
    / "cycle2_short_reduce_shadow_audit"
)


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_result(path: Path | str | None = None) -> Dict[str, Any]:
    target = Path(path) if path is not None else DEFAULT_RESULT_PATH
    return json.loads(target.read_text(encoding="utf-8"))


def _load_window_from_result(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Rekonstruiere Candle-Fenster anhand von start_time + candles_processed."""
    symbol = str(result.get("symbol") or "APTUSDT")
    start_time = str(result.get("start_time") or "")
    candles_processed = int(result.get("candles_processed") or 0)
    if not start_time or not candles_processed:
        raise ValueError("result missing start_time or candles_processed")

    rows = load_candles_for_symbol(
        symbol,
        timeframe="5m",
        data_dir=DEFAULT_DATA_DIR,
        limit=60000,
    )
    start_idx = None
    for idx, row in enumerate(rows):
        ts = row.get("timestamp")
        if ts is None:
            continue
        ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        if ts_str == start_time:
            start_idx = idx
            break
    if start_idx is None:
        raise ValueError(f"could not locate start_time={start_time!r} in candle series")
    end_idx = start_idx + candles_processed
    return rows[start_idx:end_idx]


@dataclass
class CandleState:
    candle_index: int
    timestamp: Any
    long_qty: float
    short_qty: float
    long_avg: float
    short_avg: float
    realized_pnl: float
    unrealized_pnl: float
    overall_pnl: float
    position_notional: float


def _build_candle_states(
    result: Dict[str, Any],
    window: List[Dict[str, Any]],
) -> List[Optional[CandleState]]:
    """Baue pro Candle einen mark-to-market State auf Basis der Fills."""
    fill_log: List[Dict[str, Any]] = list(result.get("fill_log") or [])
    if not window:
        return []

    n = len(window)
    per_candle: List[Optional[CandleState]] = [None] * n

    long_qty = 0.0
    short_qty = 0.0
    long_avg = 0.0
    short_avg = 0.0
    realized_cum = 0.0

    fills_sorted = sorted(
        fill_log,
        key=lambda ev: (int(ev.get("candle_index") or 0), str(ev.get("timestamp") or "")),
    )

    last_ci = -1
    for ev in fills_sorted:
        ci = int(ev.get("candle_index") or 0)
        if ci < 0 or ci >= n:
            continue
        closed = _safe_float(ev.get("closed_pnl")) or 0.0
        realized_cum += closed

        l_after = _safe_float(ev.get("long_qty_after"))
        s_after = _safe_float(ev.get("short_qty_after"))
        l_avg_after = _safe_float(ev.get("long_avg_after"))
        s_avg_after = _safe_float(ev.get("short_avg_after"))

        if l_after is not None:
            long_qty = l_after
        if s_after is not None:
            short_qty = s_after
        if l_avg_after is not None:
            long_avg = l_avg_after
        if s_avg_after is not None:
            short_avg = s_avg_after

        candle = window[ci]
        price = float(candle["close"])
        unrealized = (price - long_avg) * long_qty + (short_avg - price) * short_qty
        overall = realized_cum + unrealized
        notional = (abs(long_qty) + abs(short_qty)) * price

        per_candle[ci] = CandleState(
            candle_index=ci,
            timestamp=candle["timestamp"],
            long_qty=long_qty,
            short_qty=short_qty,
            long_avg=long_avg,
            short_avg=short_avg,
            realized_pnl=realized_cum,
            unrealized_pnl=unrealized,
            overall_pnl=overall,
            position_notional=notional,
        )
        last_ci = ci

    # Forward-fill Zustände über Candles ohne Fills.
    last_state: Optional[CandleState] = None
    for ci in range(n):
        if per_candle[ci] is None:
            if last_state is None:
                continue
            candle = window[ci]
            price = float(candle["close"])
            unrealized = (
                (price - last_state.long_avg) * last_state.long_qty
                + (last_state.short_avg - price) * last_state.short_qty
            )
            overall = last_state.realized_pnl + unrealized
            notional = (abs(last_state.long_qty) + abs(last_state.short_qty)) * price
            per_candle[ci] = CandleState(
                candle_index=ci,
                timestamp=candle["timestamp"],
                long_qty=last_state.long_qty,
                short_qty=last_state.short_qty,
                long_avg=last_state.long_avg,
                short_avg=last_state.short_avg,
                realized_pnl=last_state.realized_pnl,
                unrealized_pnl=unrealized,
                overall_pnl=overall,
                position_notional=notional,
            )
        last_state = per_candle[ci]

    return per_candle


def _iter_c2_short_reduce_fills(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    fills: List[Dict[str, Any]] = list(result.get("fill_log") or [])
    out: List[Dict[str, Any]] = []
    for ev in fills:
        purpose = str(ev.get("purpose") or ev.get("purpose_original") or "")
        if purpose == "CYCLE_2_SHORT_REDUCE":
            out.append(ev)
    return out


def _collect_active_orders_at_candle(
    result: Dict[str, Any],
    candle_index: int,
) -> List[Dict[str, Any]]:
    order_log: List[Dict[str, Any]] = list(result.get("order_log") or [])
    latest_per_order: Dict[str, Dict[str, Any]] = {}
    for ev in order_log:
        ci = int(ev.get("candle_index") or 0)
        if ci > candle_index:
            continue
        order_id = str(ev.get("order_id") or "")
        if not order_id:
            continue
        prev = latest_per_order.get(order_id)
        if prev is None or ci > int(prev.get("candle_index") or 0):
            latest_per_order[order_id] = ev
    active: List[Dict[str, Any]] = []
    for ev in latest_per_order.values():
        status = str(ev.get("status") or "").upper()
        if status not in {"FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "DEACTIVATED"}:
            active.append(ev)
    return active


@dataclass
class TradeAuditRow:
    trade_id: str
    start_index: int
    symbol: str
    direction: str
    c2_candle_index: int
    c2_timestamp: Any
    long_qty_after_c2: float
    short_qty_after_c2: float
    net_long_qty_after_c2: float
    ratio_after_c2: float
    position_notional_after_c2: float
    long_avg_price_at_c2: float
    short_avg_price_at_c2: float
    realized_pnl_at_c2: float
    unrealized_pnl_at_c2: float
    overall_pnl_at_c2: float
    baseline_closed: bool
    baseline_exit_reason: str
    baseline_final_realized_pnl: float
    baseline_final_unrealized_pnl: float
    baseline_final_overall_pnl: float
    baseline_max_drawdown_after_c2: float
    baseline_candles_after_c2: int
    cycle3_long_created: bool
    cycle3_long_filled: bool
    cycle3_short_filled: bool
    cycle3_complete: bool
    cycle3_long_create_candle_delta: Optional[int]
    cycle3_long_fill_candle_delta: Optional[int]
    cycle3_short_fill_candle_delta: Optional[int]
    cycle3_duration_candles: Optional[int]


@dataclass
class ShadowComparisonRow:
    trade_id: str
    c2_candle_index: int
    shadow_max_drawdown_after_c2: float
    shadow_best_pnl_after_c2: float
    shadow_pnl_at_baseline_exit_candle: float
    shadow_pnl_at_series_end: float
    shadow_break_even_reached: bool
    shadow_break_even_candle_index: Optional[int]
    shadow_return_to_long_avg_reached: bool
    shadow_return_to_long_avg_candle_index: Optional[int]
    pnl_difference_baseline_minus_shadow: float
    drawdown_difference: float
    holding_time_difference: int
    classification: str
    classification_reason: str


def _build_trade_audit_rows(
    result: Dict[str, Any],
    window: List[Dict[str, Any]],
    candle_states: List[Optional[CandleState]],
) -> List[TradeAuditRow]:
    c2_fills = _iter_c2_short_reduce_fills(result)
    if not c2_fills:
        return []

    symbol = str(result.get("symbol") or "APTUSDT")
    direction = str(result.get("direction") or "long")
    start_index = int(result.get("start_index") or result.get("requested_start_index") or 0)
    trade_id = str(result.get("trade_block_id") or "backtest_long_start0")

    final_status = str(result.get("final_status") or "").lower()
    baseline_closed = final_status in {"closed", "flat", "exited"}
    baseline_exit_reason = str(result.get("exit_reason") or "")
    final_realized = _safe_float(result.get("realized_pnl")) or 0.0
    final_unrealized = _safe_float(result.get("unrealized_pnl")) or 0.0
    final_overall = _safe_float(result.get("overall_pnl")) or (final_realized + final_unrealized)

    fill_log: List[Dict[str, Any]] = list(result.get("fill_log") or [])

    rows: List[TradeAuditRow] = []

    for c2_ev in c2_fills:
        c2_ci = int(c2_ev.get("candle_index") or 0)
        state_at_c2 = candle_states[c2_ci]
        if state_at_c2 is None:
            raise ValueError(f"missing candle state at c2 candle_index={c2_ci}")
        long_qty = float(state_at_c2.long_qty)
        short_qty = float(state_at_c2.short_qty)
        net = long_qty - short_qty
        ratio = long_qty / short_qty if abs(short_qty) > 1e-12 else 0.0
        notional = float(state_at_c2.position_notional)
        long_avg_c2 = float(state_at_c2.long_avg)
        short_avg_c2 = float(state_at_c2.short_avg)

        # Realized-PnL bis inkl. C2 aus den Fills vor/nach diesem Candle aggregieren.
        realized_at_c2 = 0.0
        for ev in fill_log:
            ci = int(ev.get("candle_index") or 0)
            if ci > c2_ci:
                continue
            closed = _safe_float(ev.get("closed_pnl")) or 0.0
            realized_at_c2 += closed

        unreal_c2 = float(state_at_c2.unrealized_pnl)
        overall_c2 = realized_at_c2 + unreal_c2

        # Baseline-Drawdown nach C2.
        max_drawdown = overall_c2
        n_candles = len(window)
        for ci in range(c2_ci, n_candles):
            st = candle_states[ci]
            if st is None:
                continue
            if st.overall_pnl < max_drawdown:
                max_drawdown = st.overall_pnl
        baseline_candles_after_c2 = n_candles - 1 - c2_ci

        # Cycle-3-Infos aus Logs.
        intent_log: List[Dict[str, Any]] = list(result.get("intent_log") or [])
        cycle3_long_created = False
        cycle3_long_fill_ci: Optional[int] = None
        cycle3_short_fill_ci: Optional[int] = None

        cycle3_long_create_ci: Optional[int] = None
        for ev in intent_log:
            ci = int(ev.get("candle_index") or 0)
            if ci <= c2_ci:
                continue
            purpose = str(ev.get("purpose") or ev.get("purpose_original") or "")
            if purpose == "CYCLE_3_LONG_ADD":
                cycle3_long_created = True
                cycle3_long_create_ci = ci
                break

        for ev in fill_log:
            ci = int(ev.get("candle_index") or 0)
            if ci <= c2_ci:
                continue
            purpose = str(ev.get("purpose") or ev.get("purpose_original") or "")
            if purpose == "CYCLE_3_LONG_ADD" and cycle3_long_fill_ci is None:
                cycle3_long_fill_ci = ci
            if purpose == "CYCLE_3_SHORT_REDUCE" and cycle3_short_fill_ci is None:
                cycle3_short_fill_ci = ci

        cycle3_long_filled = cycle3_long_fill_ci is not None
        cycle3_short_filled = cycle3_short_fill_ci is not None
        cycle3_complete = cycle3_long_filled and cycle3_short_filled

        long_create_delta = (
            cycle3_long_create_ci - c2_ci if cycle3_long_create_ci is not None else None
        )
        long_fill_delta = (
            cycle3_long_fill_ci - c2_ci if cycle3_long_fill_ci is not None else None
        )
        short_fill_delta = (
            cycle3_short_fill_ci - c2_ci if cycle3_short_fill_ci is not None else None
        )
        if cycle3_complete and cycle3_short_fill_ci is not None:
            cycle3_duration = cycle3_short_fill_ci - c2_ci
        else:
            cycle3_duration = None

        rows.append(
            TradeAuditRow(
                trade_id=trade_id,
                start_index=start_index,
                symbol=symbol,
                direction=direction,
                c2_candle_index=c2_ci,
                c2_timestamp=c2_ev.get("timestamp"),
                long_qty_after_c2=long_qty,
                short_qty_after_c2=short_qty,
                net_long_qty_after_c2=net,
                ratio_after_c2=ratio,
                position_notional_after_c2=notional,
                long_avg_price_at_c2=long_avg_c2,
                short_avg_price_at_c2=short_avg_c2,
                realized_pnl_at_c2=realized_at_c2,
                unrealized_pnl_at_c2=unreal_c2,
                overall_pnl_at_c2=overall_c2,
                baseline_closed=baseline_closed,
                baseline_exit_reason=baseline_exit_reason,
                baseline_final_realized_pnl=final_realized,
                baseline_final_unrealized_pnl=final_unrealized,
                baseline_final_overall_pnl=final_overall,
                baseline_max_drawdown_after_c2=max_drawdown,
                baseline_candles_after_c2=baseline_candles_after_c2,
                cycle3_long_created=cycle3_long_created,
                cycle3_long_filled=cycle3_long_filled,
                cycle3_short_filled=cycle3_short_filled,
                cycle3_complete=cycle3_complete,
                cycle3_long_create_candle_delta=long_create_delta,
                cycle3_long_fill_candle_delta=long_fill_delta,
                cycle3_short_fill_candle_delta=short_fill_delta,
                cycle3_duration_candles=cycle3_duration,
            )
        )

    return rows


def _build_shadow_row(
    audit: TradeAuditRow,
    window: List[Dict[str, Any]],
) -> ShadowComparisonRow:
    n_candles = len(window)
    c2_ci = audit.c2_candle_index

    long_qty = audit.long_qty_after_c2
    short_qty = audit.short_qty_after_c2
    long_avg = audit.long_avg_price_at_c2
    short_avg = audit.short_avg_price_at_c2

    realized_at_c2 = audit.realized_pnl_at_c2

    worst = audit.overall_pnl_at_c2
    best = audit.overall_pnl_at_c2
    pnl_at_exit = audit.overall_pnl_at_c2
    pnl_at_end = audit.overall_pnl_at_c2

    break_even_reached = False
    break_even_ci: Optional[int] = None

    return_to_long_avg_reached = False
    return_to_long_avg_ci: Optional[int] = None

    for ci in range(c2_ci, n_candles):
        candle = window[ci]
        price = float(candle["close"])
        unreal = (price - long_avg) * long_qty + (short_avg - price) * short_qty
        overall = realized_at_c2 + unreal
        if overall < worst:
            worst = overall
        if overall > best:
            best = overall
        if not break_even_reached and overall >= 0.0:
            break_even_reached = True
            break_even_ci = ci
        if not return_to_long_avg_reached and price >= long_avg:
            return_to_long_avg_reached = True
            return_to_long_avg_ci = ci
        pnl_at_end = overall

    baseline_end_ci = c2_ci + audit.baseline_candles_after_c2
    if baseline_end_ci >= n_candles:
        baseline_end_ci = n_candles - 1
    candle = window[baseline_end_ci]
    price = float(candle["close"])
    unreal_at_exit = (price - long_avg) * long_qty + (short_avg - price) * short_qty
    pnl_at_exit = realized_at_c2 + unreal_at_exit

    pnl_diff = audit.baseline_final_overall_pnl - pnl_at_exit
    drawdown_diff = audit.baseline_max_drawdown_after_c2 - worst
    holding_diff = audit.baseline_candles_after_c2 - (n_candles - 1 - c2_ci)

    # Einfache Heuristik für die Klassifikation.
    classification = "D"
    reason = "unclassified"
    eps = 1e-6

    if audit.baseline_final_overall_pnl > 0 and audit.cycle3_complete:
        if pnl_diff > 0.0 and worst >= audit.baseline_max_drawdown_after_c2 - 1e-3:
            classification = "A"
            reason = "cycle3 required for positive baseline exit vs non-positive shadow"
        elif pnl_diff >= -eps:
            classification = "B"
            reason = "cycle3 did not materially improve final pnl vs shadow"
        else:
            classification = "C"
            reason = "cycle3 reduced final pnl vs shadow"
    else:
        if pnl_diff < -eps:
            classification = "C"
            reason = "shadow outperforms baseline despite non-positive baseline"
        elif abs(pnl_diff) <= 1e-3:
            classification = "B"
            reason = "baseline and shadow roughly equal"
        else:
            classification = "D"

    # Wenn es keinerlei Cycle-3-Aktivität gibt, ist kein echter Vergleich möglich.
    if not audit.cycle3_long_filled and not audit.cycle3_short_filled:
        classification = "D"
        reason = "no_cycle3_activity_for_trade"

    return ShadowComparisonRow(
        trade_id=audit.trade_id,
        c2_candle_index=c2_ci,
        shadow_max_drawdown_after_c2=worst,
        shadow_best_pnl_after_c2=best,
        shadow_pnl_at_baseline_exit_candle=pnl_at_exit,
        shadow_pnl_at_series_end=pnl_at_end,
        shadow_break_even_reached=break_even_reached,
        shadow_break_even_candle_index=break_even_ci,
        shadow_return_to_long_avg_reached=return_to_long_avg_reached,
        shadow_return_to_long_avg_candle_index=return_to_long_avg_ci,
        pnl_difference_baseline_minus_shadow=pnl_diff,
        drawdown_difference=drawdown_diff,
        holding_time_difference=holding_diff,
        classification=classification,
        classification_reason=reason,
    )


def _write_trade_audit_csv(path: Path, rows: List[TradeAuditRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [field.name for field in TradeAuditRow.__dataclass_fields__.values()]  # type: ignore[attr-defined]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r.__dict__)


def _write_shadow_comparison_csv(path: Path, rows: List[ShadowComparisonRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [field.name for field in ShadowComparisonRow.__dataclass_fields__.values()]  # type: ignore[attr-defined]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r.__dict__)


def _write_summary_and_md(
    summary_path: Path,
    md_path: Path,
    audits: List[TradeAuditRow],
    shadows: List[ShadowComparisonRow],
) -> None:
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    total_trades = len(audits)
    c_by_id = {row.trade_id: row for row in shadows}

    classification_counts = {"A": 0, "B": 0, "C": 0, "D": 0}
    for row in shadows:
        classification_counts[row.classification] = classification_counts.get(row.classification, 0) + 1

    closed_count = sum(1 for a in audits if a.baseline_closed)
    positive_closes = sum(1 for a in audits if a.baseline_closed and a.baseline_final_overall_pnl > 0.0)
    require_cycle3_for_positive = sum(
        1
        for a in audits
        if a.baseline_closed
        and a.baseline_final_overall_pnl > 0.0
        and a.cycle3_complete
        and c_by_id[a.trade_id].classification == "A"
    )
    better_without_cycle3 = sum(1 for r in shadows if r.classification == "C")
    stuck_trades = sum(
        1
        for a in audits
        if not a.baseline_closed and abs(a.net_long_qty_after_c2) > 1e-6
    )

    summary = {
        "total_trades_reaching_c2_short_reduce": total_trades,
        "classification_counts": classification_counts,
        "baseline_trades_closed": closed_count,
        "baseline_positive_closes": positive_closes,
        "trades_where_cycle3_required_for_positive": require_cycle3_for_positive,
        "trades_better_or_equal_without_cycle3": better_without_cycle3,
        "stuck_trades_after_c2": stuck_trades,
    }
    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    with md_path.open("w", encoding="utf-8") as handle:
        handle.write("# CYCLE_2_SHORT_REDUCE Shadow-Audit\n\n")
        handle.write(f"- Trades mit `CYCLE_2_SHORT_REDUCE`: {total_trades}\n")
        handle.write(
            f"- Klassifikation A/B/C/D: {classification_counts['A']}/"
            f"{classification_counts['B']}/{classification_counts['C']}/"
            f"{classification_counts['D']}\n"
        )
        handle.write(f"- Baseline sauber geschlossen: {closed_count}\n")
        handle.write(f"- Davon mit positivem Final-PnL: {positive_closes}\n")
        handle.write(
            "- Trades, bei denen Cycle 3 für einen positiven Exit notwendig war "
            f"(Heuristik, Klasse A): {require_cycle3_for_positive}\n"
        )
        handle.write(
            "- Trades, bei denen der Shadow-Ansatz ohne Cycle 3 besser oder gleich gut war "
            f"(Klassen B/C insgesamt): {better_without_cycle3}\n"
        )
        handle.write(f"- Stuck-Trades nach Cycle 2: {stuck_trades}\n")
        handle.write(
            "- Hinweis: Für Trades ohne weitere Positionsänderung nach Cycle 2 "
            "sind Baseline- und Freeze-Shadow-PnL per Design identisch "
            "(Identity-Test im Audit verankert).\n"
        )


def _assert_freeze_shadow_identity(
    result: Dict[str, Any],
    window: List[Dict[str, Any]],
    candle_states: List[Optional[CandleState]],
    audits: List[TradeAuditRow],
) -> None:
    """Stelle sicher, dass Baseline- und Freeze-Shadow-PnL übereinstimmen,
    wenn es nach C2 keine weiteren Positionsänderungen gibt.
    """
    fill_log: List[Dict[str, Any]] = list(result.get("fill_log") or [])
    n_candles = len(window)

    for audit in audits:
        c2_ci = audit.c2_candle_index
        # Prüfen, ob nach C2 noch Fills auftreten, die die Position verändern.
        has_position_change = False
        for ev in fill_log:
            ci = int(ev.get("candle_index") or 0)
            if ci <= c2_ci:
                continue
            l_after = _safe_float(ev.get("long_qty_after"))
            s_after = _safe_float(ev.get("short_qty_after"))
            if l_after is None and s_after is None:
                continue
            if l_after is not None and abs(l_after - audit.long_qty_after_c2) > 1e-12:
                has_position_change = True
                break
            if s_after is not None and abs(s_after - audit.short_qty_after_c2) > 1e-12:
                has_position_change = True
                break
        if has_position_change:
            continue

        long_qty = audit.long_qty_after_c2
        short_qty = audit.short_qty_after_c2
        long_avg = audit.long_avg_price_at_c2
        short_avg = audit.short_avg_price_at_c2
        realized_at_c2 = audit.realized_pnl_at_c2

        for ci in range(c2_ci, n_candles):
            candle = window[ci]
            price = float(candle["close"])
            unreal = (price - long_avg) * long_qty + (short_avg - price) * short_qty
            shadow_overall = realized_at_c2 + unreal

            base_state = candle_states[ci]
            if base_state is None:
                continue
            baseline_overall = base_state.overall_pnl
            if abs(baseline_overall - shadow_overall) > 1e-9:
                raise AssertionError(
                    f"freeze-shadow identity failed at candle_index={ci}: "
                    f"baseline={baseline_overall}, shadow={shadow_overall}"
                )


def main() -> int:
    # Derzeit Single-Result-Audit basierend auf DEFAULT_RESULT_PATH.
    result = _load_result()
    window = _load_window_from_result(result)
    candle_states = _build_candle_states(result, window)

    audits = _build_trade_audit_rows(result, window, candle_states)
    if not audits:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / "cycle2_short_reduce_summary.json").write_text(
            json.dumps({"total_trades_reaching_c2_short_reduce": 0}, indent=2),
            encoding="utf-8",
        )
        return 0

    # Identity-Test für reine Freeze-Fälle (keine Positionsänderung nach C2).
    _assert_freeze_shadow_identity(result, window, candle_states, audits)

    shadow_rows = [_build_shadow_row(a, window) for a in audits]

    _write_trade_audit_csv(OUT_DIR / "cycle2_short_reduce_trade_audit.csv", audits)
    _write_shadow_comparison_csv(
        OUT_DIR / "cycle2_short_reduce_shadow_comparison.csv",
        shadow_rows,
    )
    _write_summary_and_md(
        OUT_DIR / "cycle2_short_reduce_summary.json",
        OUT_DIR / "cycle2_short_reduce_diagnosis.md",
        audits,
        shadow_rows,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

