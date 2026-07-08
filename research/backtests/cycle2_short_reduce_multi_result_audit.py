from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from research.backtests.candle_loader import (
    DEFAULT_DATA_DIR,
    load_candles_for_symbol,
)
from research.backtests import cycle2_short_reduce_shadow_audit as single


REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = REPO_ROOT / "research" / "backtests" / "results"

OUT_DIR = (
    REPO_ROOT
    / "research"
    / "backtests"
    / "results"
    / "cycle2_short_reduce_multi_result_audit"
)


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _iter_candidate_files(root: Path) -> Iterable[Path]:
    """Yield possible full-result JSON files under results/."""
    for path in root.rglob("*.json"):
        name = path.name
        if name.endswith("_full.json"):
            yield path


def _has_c2_short_reduce(result: Dict[str, Any]) -> bool:
    for ev in result.get("fill_log") or []:
        purpose = str(ev.get("purpose") or ev.get("purpose_original") or "")
        if purpose == "CYCLE_2_SHORT_REDUCE":
            return True
    return False


def _load_result(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _make_trade_key(result: Dict[str, Any]) -> Tuple[Any, ...]:
    symbol = result.get("symbol")
    direction = result.get("direction")
    start_time = result.get("start_time") or result.get("source_candle_timestamp")
    candles = int(result.get("candles_processed") or 0)
    start_index = int(result.get("start_index") or result.get("requested_start_index") or 0)
    cfg_diag = result.get("config_diagnostics") or {}
    cfg_path = cfg_diag.get("config_path") or ""
    cfg_hash = cfg_diag.get("config_hash") or ""
    return (symbol, direction, start_index, start_time, candles, cfg_path, cfg_hash)


@dataclass
class MultiTradeRow:
    source_result_path: str
    trade_id: str
    symbol: str
    direction: str
    start_index: int
    c2_candle_index: int
    c2_timestamp: Any
    long_qty_at_c2: float
    short_qty_at_c2: float
    net_qty_at_c2: float
    ratio_at_c2: float
    long_avg_price_at_c2: float
    short_avg_price_at_c2: float
    realized_pnl_at_c2: float
    unrealized_pnl_at_c2: float
    overall_pnl_at_c2: float
    position_notional_at_c2: float
    cycle3_long_created: bool
    cycle3_long_filled: bool
    cycle3_long_fill_candle: Optional[int]
    cycle3_long_qty: Optional[float]
    cycle3_short_created: bool
    cycle3_short_filled: bool
    cycle3_short_fill_candle: Optional[int]
    cycle3_complete: bool
    candles_from_c2_to_c3_long: Optional[int]
    candles_from_c3_long_to_c3_short: Optional[int]
    further_complete_cycles_after_c2: int
    baseline_closed: bool
    baseline_exit_reason: str
    baseline_final_realized_pnl: float
    baseline_final_unrealized_pnl: float
    baseline_final_overall_pnl: float
    baseline_max_drawdown_after_c2: float
    baseline_best_pnl_after_c2: float
    baseline_candles_after_c2: int
    maximum_position_notional_after_c2: float
    classification: str
    classification_reason: str
    STUCK_AFTER_C2: bool
    CYCLE3_REQUIRED_FOR_EXIT: bool
    CYCLE3_REQUIRED_FOR_POSITIVE_EXIT: bool
    GOOD_TRADE_AT_RISK_IF_BLOCKED: bool
    NO_CYCLE3_ACTIVITY: bool
    INSUFFICIENT_DATA: bool


@dataclass
class MultiShadowRow:
    source_result_path: str
    trade_id: str
    symbol: str
    direction: str
    start_index: int
    c2_candle_index: int
    shadow_max_drawdown_after_c2: float
    shadow_best_pnl_after_c2: float
    shadow_pnl_at_baseline_exit_candle: float
    shadow_pnl_at_series_end: float
    shadow_break_even_reached: bool
    shadow_break_even_candle: Optional[int]
    shadow_return_to_long_avg_reached: bool
    shadow_return_to_long_avg_candle: Optional[int]
    baseline_final_overall_pnl: float
    baseline_minus_shadow_final_pnl: float
    baseline_minus_shadow_drawdown: float
    classification: str
    classification_reason: str


def _load_window_from_result(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Eigenes Window-Loading, analog zum Single-Audit, aber explizit."""
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


def _compute_additional_metrics(
    result: Dict[str, Any],
    window: List[Dict[str, Any]],
    candle_states: List[Any],
    audit_row: Any,
    shadow_row: Any,
) -> Tuple[MultiTradeRow, MultiShadowRow]:
    """Enrich single-trade audit + shadow with multi-result fields."""
    c2_ci = int(audit_row.c2_candle_index)
    n_candles = len(window)

    # Baseline best PnL + max position notional nach C2.
    best_pnl = audit_row.overall_pnl_at_c2
    max_notional = audit_row.position_notional_after_c2
    for ci in range(c2_ci, n_candles):
        st = candle_states[ci]
        if st is None:
            continue
        if st.overall_pnl > best_pnl:
            best_pnl = st.overall_pnl
        if st.position_notional > max_notional:
            max_notional = st.position_notional

    # Cycle-3-Fill-Details.
    fill_log: List[Dict[str, Any]] = list(result.get("fill_log") or [])
    intent_log: List[Dict[str, Any]] = list(result.get("intent_log") or [])

    cycle3_long_fill_ci: Optional[int] = None
    cycle3_long_qty: Optional[float] = None
    cycle3_short_fill_ci: Optional[int] = None

    cycle3_short_created = False

    for ev in intent_log:
        ci = int(ev.get("candle_index") or 0)
        if ci <= c2_ci:
            continue
        purpose = str(ev.get("purpose") or ev.get("purpose_original") or "")
        if purpose == "CYCLE_3_SHORT_REDUCE":
            cycle3_short_created = True
            break

    for ev in fill_log:
        ci = int(ev.get("candle_index") or 0)
        if ci <= c2_ci:
            continue
        purpose = str(ev.get("purpose") or ev.get("purpose_original") or "")
        if purpose == "CYCLE_3_LONG_ADD" and cycle3_long_fill_ci is None:
            cycle3_long_fill_ci = ci
            cycle3_long_qty = _safe_float(ev.get("qty"))
        if purpose == "CYCLE_3_SHORT_REDUCE" and cycle3_short_fill_ci is None:
            cycle3_short_fill_ci = ci

    candles_from_c2_to_c3_long: Optional[int] = None
    candles_from_c3_long_to_c3_short: Optional[int] = None
    if cycle3_long_fill_ci is not None:
        candles_from_c2_to_c3_long = cycle3_long_fill_ci - c2_ci
    if cycle3_long_fill_ci is not None and cycle3_short_fill_ci is not None:
        candles_from_c3_long_to_c3_short = cycle3_short_fill_ci - cycle3_long_fill_ci

    # Weitere vollständige Cycles nach C2.
    further_complete_cycles = 0
    cycles: Dict[int, Dict[str, bool]] = {}
    for ev in fill_log:
        ci = int(ev.get("candle_index") or 0)
        if ci <= c2_ci:
            continue
        purpose = str(ev.get("purpose") or ev.get("purpose_original") or "")
        cycle_index = int(ev.get("cycle_index") or 0)
        if cycle_index <= 2:
            continue
        if not purpose:
            continue
        entry = cycles.setdefault(cycle_index, {"long": False, "short": False})
        if f"CYCLE_{cycle_index}_LONG_ADD" == purpose:
            entry["long"] = True
        if f"CYCLE_{cycle_index}_SHORT_REDUCE" == purpose:
            entry["short"] = True
    for entry in cycles.values():
        if entry["long"] and entry["short"]:
            further_complete_cycles += 1

    # Flags.
    stuck_after_c2 = (not audit_row.baseline_closed) and (
        abs(audit_row.net_long_qty_after_c2) > 1e-9
    )
    no_cycle3_activity = (not audit_row.cycle3_long_filled) and (not audit_row.cycle3_short_filled)
    cycle3_required_for_exit = audit_row.baseline_closed and audit_row.cycle3_complete
    cycle3_required_for_positive_exit = (
        audit_row.baseline_closed
        and audit_row.baseline_final_overall_pnl > 0.0
        and audit_row.cycle3_complete
        and shadow_row.classification == "A"
    )
    good_trade_at_risk_if_blocked = (
        audit_row.baseline_closed
        and audit_row.baseline_final_overall_pnl > 0.0
        and shadow_row.classification in {"A", "B"}
        and shadow_row.baseline_minus_shadow_final_pnl > 0.0
    )

    multi_trade = MultiTradeRow(
        source_result_path=str(result.get("_source_path") or ""),
        trade_id=audit_row.trade_id,
        symbol=audit_row.symbol,
        direction=audit_row.direction,
        start_index=int(audit_row.start_index),
        c2_candle_index=int(audit_row.c2_candle_index),
        c2_timestamp=audit_row.c2_timestamp,
        long_qty_at_c2=audit_row.long_qty_after_c2,
        short_qty_at_c2=audit_row.short_qty_after_c2,
        net_qty_at_c2=audit_row.net_long_qty_after_c2,
        ratio_at_c2=audit_row.ratio_after_c2,
        long_avg_price_at_c2=audit_row.long_avg_price_at_c2,
        short_avg_price_at_c2=audit_row.short_avg_price_at_c2,
        realized_pnl_at_c2=audit_row.realized_pnl_at_c2,
        unrealized_pnl_at_c2=audit_row.unrealized_pnl_at_c2,
        overall_pnl_at_c2=audit_row.overall_pnl_at_c2,
        position_notional_at_c2=audit_row.position_notional_after_c2,
        cycle3_long_created=audit_row.cycle3_long_created,
        cycle3_long_filled=audit_row.cycle3_long_filled,
        cycle3_long_fill_candle=cycle3_long_fill_ci,
        cycle3_long_qty=cycle3_long_qty,
        cycle3_short_created=cycle3_short_created,
        cycle3_short_filled=audit_row.cycle3_short_filled,
        cycle3_short_fill_candle=cycle3_short_fill_ci,
        cycle3_complete=audit_row.cycle3_complete,
        candles_from_c2_to_c3_long=candles_from_c2_to_c3_long,
        candles_from_c3_long_to_c3_short=candles_from_c3_long_to_c3_short,
        further_complete_cycles_after_c2=further_complete_cycles,
        baseline_closed=audit_row.baseline_closed,
        baseline_exit_reason=audit_row.baseline_exit_reason,
        baseline_final_realized_pnl=audit_row.baseline_final_realized_pnl,
        baseline_final_unrealized_pnl=audit_row.baseline_final_unrealized_pnl,
        baseline_final_overall_pnl=audit_row.baseline_final_overall_pnl,
        baseline_max_drawdown_after_c2=audit_row.baseline_max_drawdown_after_c2,
        baseline_best_pnl_after_c2=best_pnl,
        baseline_candles_after_c2=audit_row.baseline_candles_after_c2,
        maximum_position_notional_after_c2=max_notional,
        classification=shadow_row.classification,
        classification_reason=shadow_row.classification_reason,
        STUCK_AFTER_C2=stuck_after_c2,
        CYCLE3_REQUIRED_FOR_EXIT=cycle3_required_for_exit,
        CYCLE3_REQUIRED_FOR_POSITIVE_EXIT=cycle3_required_for_positive_exit,
        GOOD_TRADE_AT_RISK_IF_BLOCKED=good_trade_at_risk_if_blocked,
        NO_CYCLE3_ACTIVITY=no_cycle3_activity,
        INSUFFICIENT_DATA=False,
    )

    multi_shadow = MultiShadowRow(
        source_result_path=str(result.get("_source_path") or ""),
        trade_id=audit_row.trade_id,
        symbol=audit_row.symbol,
        direction=audit_row.direction,
        start_index=int(audit_row.start_index),
        c2_candle_index=int(audit_row.c2_candle_index),
        shadow_max_drawdown_after_c2=shadow_row.shadow_max_drawdown_after_c2,
        shadow_best_pnl_after_c2=shadow_row.shadow_best_pnl_after_c2,
        shadow_pnl_at_baseline_exit_candle=shadow_row.shadow_pnl_at_baseline_exit_candle,
        shadow_pnl_at_series_end=shadow_row.shadow_pnl_at_series_end,
        shadow_break_even_reached=shadow_row.shadow_break_even_reached,
        shadow_break_even_candle=shadow_row.shadow_break_even_candle_index,
        shadow_return_to_long_avg_reached=shadow_row.shadow_return_to_long_avg_reached,
        shadow_return_to_long_avg_candle=shadow_row.shadow_return_to_long_avg_candle_index,
        baseline_final_overall_pnl=audit_row.baseline_final_overall_pnl,
        baseline_minus_shadow_final_pnl=shadow_row.pnl_difference_baseline_minus_shadow,
        baseline_minus_shadow_drawdown=shadow_row.drawdown_difference,
        classification=shadow_row.classification,
        classification_reason=shadow_row.classification_reason,
    )

    return multi_trade, multi_shadow


def _write_csv(path: Path, rows: List[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write("")
        return
    fieldnames = [field.name for field in rows[0].__dataclass_fields__.values()]  # type: ignore[attr-defined]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r.__dict__)


def _summarize(
    trades: List[MultiTradeRow],
    shadows: List[MultiShadowRow],
    insufficient: List[Dict[str, Any]],
    summary_path: Path,
    md_path: Path,
) -> None:
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    total = len(trades)
    closed = sum(1 for t in trades if t.baseline_closed)
    positive = sum(1 for t in trades if t.baseline_closed and t.baseline_final_overall_pnl > 0.0)
    with_cycle3 = sum(1 for t in trades if t.cycle3_complete)
    cycle3_required_exit = sum(1 for t in trades if t.CYCLE3_REQUIRED_FOR_EXIT)
    cycle3_required_positive = sum(1 for t in trades if t.CYCLE3_REQUIRED_FOR_POSITIVE_EXIT)
    better_with_freeze = sum(
        1 for s in shadows if s.baseline_minus_shadow_final_pnl < -1e-3
    )
    stuck = sum(1 for t in trades if t.STUCK_AFTER_C2)
    good_at_risk = sum(1 for t in trades if t.GOOD_TRADE_AT_RISK_IF_BLOCKED)
    insufficient_count = len(insufficient)

    classification_counts: Dict[str, int] = {"A": 0, "B": 0, "C": 0, "D": 0}
    for t in trades:
        classification_counts[t.classification] = classification_counts.get(t.classification, 0) + 1

    summary = {
        "total_trades": total,
        "classification_counts": classification_counts,
        "baseline_closed_trades": closed,
        "baseline_positive_closes": positive,
        "trades_with_cycle3_complete": with_cycle3,
        "trades_where_cycle3_required_for_exit": cycle3_required_exit,
        "trades_where_cycle3_required_for_positive_exit": cycle3_required_positive,
        "trades_better_with_freeze_after_c2": better_with_freeze,
        "stuck_trades_after_c2": stuck,
        "good_trades_at_risk_if_c3_blocked": good_at_risk,
        "insufficient_data_trades": insufficient_count,
    }
    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    with md_path.open("w", encoding="utf-8") as handle:
        handle.write("# CYCLE_2_SHORT_REDUCE Multi-Result Audit\n\n")
        handle.write(f"- Eindeutige Trades mit `CYCLE_2_SHORT_REDUCE`: {total}\n")
        handle.write(
            f"- Klassifikation A/B/C/D: {classification_counts['A']}/"
            f"{classification_counts['B']}/{classification_counts['C']}/"
            f"{classification_counts['D']}\n"
        )
        handle.write(f"- Sauber geschlossene Trades: {closed}\n")
        handle.write(f"- Davon mit positivem End-PnL: {positive}\n")
        handle.write(
            f"- Trades mit komplettem Cycle 3: {with_cycle3} "
            f"(davon Cycle3 für Exit nötig: {cycle3_required_exit}, "
            f"für positiven Exit nötig: {cycle3_required_positive})\n"
        )
        handle.write(
            "- Trades, bei denen Freeze nach C2 besser gewesen wäre "
            f"(Baseline deutlich schlechter als Shadow): {better_with_freeze}\n"
        )
        handle.write(f"- Stuck-Trades nach Cycle 2: {stuck}\n")
        handle.write(
            "- Gute Trades, die bei pauschaler Blockierung von `CYCLE_3_LONG_ADD` "
            f"gefährdet wären: {good_at_risk}\n"
        )
        handle.write(f"- Trades mit unzureichenden Daten: {insufficient_count}\n")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    primary_by_key: Dict[Tuple[Any, ...], Path] = {}
    duplicates: List[Dict[str, Any]] = []
    source_rows: List[Dict[str, Any]] = []

    multi_trades: List[MultiTradeRow] = []
    multi_shadows: List[MultiShadowRow] = []
    insufficient: List[Dict[str, Any]] = []

    for path in _iter_candidate_files(RESULTS_ROOT):
        try:
            result = _load_result(path)
        except Exception:
            continue
        if "fill_log" not in result or "candles_processed" not in result:
            continue
        if not _has_c2_short_reduce(result):
            continue

        key = _make_trade_key(result)
        if key in primary_by_key:
            duplicates.append(
                {
                    "symbol": key[0],
                    "direction": key[1],
                    "start_index": key[2],
                    "start_time": key[3],
                    "candles_processed": key[4],
                    "config_path": key[5],
                    "config_hash": key[6],
                    "duplicate_result_path": str(path),
                    "primary_result_path": str(primary_by_key[key]),
                }
            )
            continue

        primary_by_key[key] = path
        source_rows.append(
            {
                "symbol": key[0],
                "direction": key[1],
                "start_index": key[2],
                "start_time": key[3],
                "candles_processed": key[4],
                "config_path": key[5],
                "config_hash": key[6],
                "source_result_path": str(path),
            }
        )

        # Pro primärem Result eine vollständige Audit-/Shadow-Berechnung.
        try:
            result = _load_result(path)
            result["_source_path"] = str(path)
            window = _load_window_from_result(result)
            candle_states = single._build_candle_states(result, window)
            audits = single._build_trade_audit_rows(result, window, candle_states)
            if not audits:
                continue

            # Identity-Regel für Freeze-Fälle.
            single._assert_freeze_shadow_identity(result, window, candle_states, audits)

            for audit_row in audits:
                shadow_row = single._build_shadow_row(audit_row, window)
                multi_trade, multi_shadow = _compute_additional_metrics(
                    result, window, candle_states, audit_row, shadow_row
                )
                multi_trades.append(multi_trade)
                multi_shadows.append(multi_shadow)
        except Exception as exc:  # pragma: no cover - reine Diagnose
            insufficient.append(
                {
                    "result_path": str(path),
                    "error": str(exc),
                }
            )
            continue

    # CSV-Ausgaben.
    _write_csv(OUT_DIR / "cycle2_multi_trade_audit.csv", multi_trades)
    _write_csv(OUT_DIR / "cycle2_multi_shadow_comparison.csv", multi_shadows)

    # Klassifikations-CSV (abgeleitet aus MultiTradeRow).
    classification_rows: List[Dict[str, Any]] = []
    for t in multi_trades:
        classification_rows.append(
            {
                "source_result_path": t.source_result_path,
                "trade_id": t.trade_id,
                "symbol": t.symbol,
                "direction": t.direction,
                "start_index": t.start_index,
                "classification": t.classification,
                "classification_reason": t.classification_reason,
                "STUCK_AFTER_C2": t.STUCK_AFTER_C2,
                "CYCLE3_REQUIRED_FOR_EXIT": t.CYCLE3_REQUIRED_FOR_EXIT,
                "CYCLE3_REQUIRED_FOR_POSITIVE_EXIT": t.CYCLE3_REQUIRED_FOR_POSITIVE_EXIT,
                "GOOD_TRADE_AT_RISK_IF_BLOCKED": t.GOOD_TRADE_AT_RISK_IF_BLOCKED,
                "NO_CYCLE3_ACTIVITY": t.NO_CYCLE3_ACTIVITY,
                "INSUFFICIENT_DATA": t.INSUFFICIENT_DATA,
            }
        )
    class_path = OUT_DIR / "cycle2_multi_classification.csv"
    if classification_rows:
        with class_path.open("w", encoding="utf-8", newline="") as handle:
            fieldnames = list(classification_rows[0].keys())
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for r in classification_rows:
                writer.writerow(r)
    else:
        class_path.write_text("", encoding="utf-8")

    # Source- und Duplicate-Listen.
    source_path = OUT_DIR / "cycle2_source_files.csv"
    if source_rows:
        with source_path.open("w", encoding="utf-8", newline="") as handle:
            fieldnames = list(source_rows[0].keys())
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for r in source_rows:
                writer.writerow(r)
    else:
        source_path.write_text("", encoding="utf-8")

    dup_path = OUT_DIR / "cycle2_duplicates_skipped.csv"
    if duplicates:
        with dup_path.open("w", encoding="utf-8", newline="") as handle:
            fieldnames = list(duplicates[0].keys())
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for r in duplicates:
                writer.writerow(r)
    else:
        dup_path.write_text("", encoding="utf-8")

    # Zusammenfassung + Diagnosis.
    _summarize(
        multi_trades,
        multi_shadows,
        insufficient,
        OUT_DIR / "cycle2_multi_summary.json",
        OUT_DIR / "cycle2_multi_diagnosis.md",
    )

    # Zusätzlich: Liste der insufficient-Datensätze speichern.
    (OUT_DIR / "cycle2_insufficient_data.json").write_text(
        json.dumps(insufficient, indent=2, default=str),
        encoding="utf-8",
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

