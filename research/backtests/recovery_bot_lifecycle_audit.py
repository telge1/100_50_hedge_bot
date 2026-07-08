"""Lifecycle audit for three recovery backtests (backtest-only).

Reads the JSON exports under
`research/backtests/results/recovery_bot_current_three_audit/` and
reconstructs a chronological event timeline per trade from `fill_log`,
`order_log` and `intent_log`. Produces:

- Per trade (start_index 4000, 7500, 9750):
  - `*_lifecycle.csv`
  - `*_lifecycle.md`
- Combined:
  - `combined_lifecycle.csv`
  - `combined_diagnosis.md`
  - `missing_audit_fields.md`

The script is deliberately read‑only with respect to strategy logic; it merely
interprets existing logs. Missing fields are left empty or marked as unknown.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_DIR = PROJECT_ROOT / "research" / "backtests" / "results" / "recovery_bot_current_three_audit"
LIFECYCLE_DIR = RESULT_DIR / "lifecycle_audit"


@dataclass(frozen=True)
class TradeConfig:
    start_index: int
    file_name: str


TRADES: List[TradeConfig] = [
    TradeConfig(start_index=4000, file_name="APTUSDT_start4000_full.json"),
    TradeConfig(start_index=7500, file_name="APTUSDT_start7500_full.json"),
    TradeConfig(start_index=9750, file_name="APTUSDT_start9750_full.json"),
]


# Lifecycle CSV columns – many will remain empty if the JSON does not provide
# the underlying data. Additional ordering metadata is included to make the
# mixing von Intent/Order/Fill/Recovery-Trace nachvollziehbar.
LIFECYCLE_COLUMNS: List[str] = [
    "start_index",
    "chronological_event_number",
    "candle_index",
    "timestamp",
    "phase",
    "event_type",
    "event_name",
    "source_trace",
    "source_trace_index",
    "original_sequence",
    "ordering_source",
    "ordering_confidence",
    "recovery_state_before",
    "recovery_state_after",
    "reason",
    "blocked_reason",
    "cycle_index",
    "order_id",
    "order_name",
    "intent_name",
    "side",
    "position_side",
    "reduce_only",
    "order_status",
    "order_quantity",
    "remaining_quantity",
    "order_price",
    "trigger_price",
    "fill_quantity",
    "fill_price",
    "fill_notional",
    "long_quantity_before",
    "long_quantity_after",
    "short_quantity_before",
    "short_quantity_after",
    "quantity_difference_before",
    "quantity_difference_after",
    "long_average_entry_before",
    "long_average_entry_after",
    "short_average_entry_before",
    "short_average_entry_after",
    "realized_pnl_event",
    "cumulative_realized_pnl",
    "unrealized_pnl",
    "overall_pnl",
    "loss_budget_usdt",
    "loss_budget_used_before",
    "loss_budget_used_after",
    "neutralization_count",
    "pair_reduction_count",
    "reload_count",
    "active_orders_after_event",
]


def _safe_get(d: Dict[str, Any], key: str, default: Any = "") -> Any:
    value = d.get(key, default)
    return value if value is not None else default


def _purpose(ev: Dict[str, Any]) -> str:
    md = ev.get("metadata_excerpt") or {}
    return str(
        ev.get("purpose")
        or ev.get("purpose_original")
        or md.get("purpose")
        or ""
    )


def _cycle_index(ev: Dict[str, Any]) -> Optional[int]:
    if "cycle_index" in ev:
        try:
            return int(ev["cycle_index"])
        except Exception:
            return None
    md = ev.get("metadata_excerpt") or {}
    if "cycle_index" in md:
        try:
            return int(md["cycle_index"])
        except Exception:
            return None
    return None


def _phase_for_event(ev: Dict[str, Any]) -> str:
    purpose = _purpose(ev).upper()
    ci = _cycle_index(ev) or 0

    if purpose.startswith("INITIAL_"):
        return "INITIAL"
    if purpose in {"LONG_TP_EXIT", "SHORT_SL_EXIT"} and ci == 0:
        return "INITIAL EXIT SETUP"
    if purpose.startswith("CYCLE_"):
        if ci == 1:
            return "NORMAL CYCLE 1"
        if ci == 2:
            return "NORMAL CYCLE 2"
        if ci == 3:
            return "NORMAL CYCLE 3"
        if ci >= 4:
            return "WEITERE NORMAL CYCLES"
    if purpose.startswith("REFILL_"):
        return "REFILL"
    if purpose in {"LONG_TP_EXIT", "SHORT_SL_EXIT", "LONG_SL_EXIT", "SHORT_TP_EXIT"}:
        return "NORMAL EXIT"
    if "RECOVERY" in purpose:
        if "NEUTRALIZE" in purpose:
            return "RECOVERY NEUTRALIZATION"
        if "PAIR" in purpose or "REDUCE" in purpose:
            return "RECOVERY PAIR REDUCTION"
        if "RELOAD" in purpose:
            return "RECOVERY RELOAD"
        return "RECOVERY START"
    return "UNKNOWN"


def _event_type(source: str, ev: Dict[str, Any]) -> str:
    if source == "fill_log":
        return "fill"
    if source == "order_log":
        return str(ev.get("event_type") or "order")
    if source == "intent_log":
        return "intent"
    if source == "recovery_trace":
        return "recovery_decision"
    return source


def _load_result(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _collect_raw_events(result: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    return (
        list(result.get("fill_log") or []),
        list(result.get("order_log") or []),
        list(result.get("intent_log") or []),
    )


def _sort_key(item: Dict[str, Any]) -> Tuple[int, str, int, int]:
    ci = item.get("candle_index")
    ci_int = int(ci) if isinstance(ci, int) else 10**9
    ts = str(item.get("timestamp") or "")
    src = str(item.get("source") or "")
    if src == "intent_log":
        pri = 0
    elif src == "order_log":
        pri = 1
    else:
        pri = 2
    return (ci_int, ts, pri, int(item.get("source_index") or 0))


def build_lifecycle_rows(
    result: Dict[str, Any],
    start_index: int,
) -> List[Dict[str, Any]]:
    fill_log, order_log, intent_log = _collect_raw_events(result)
    recovery_trace = list(result.get("recovery_trace") or [])

    combined: List[Dict[str, Any]] = []
    for idx, ev in enumerate(fill_log):
        combined.append(
            {
                "source": "fill_log",
                "source_index": idx,
                "original_sequence": idx,
                "raw": ev,
                "timestamp": _safe_get(ev, "timestamp", ""),
                "candle_index": ev.get("candle_index"),
            }
        )
    for idx, ev in enumerate(order_log):
        combined.append(
            {
                "source": "order_log",
                "source_index": idx,
                "original_sequence": idx,
                "raw": ev,
                "timestamp": _safe_get(ev, "timestamp", ""),
                "candle_index": ev.get("candle_index"),
            }
        )
    for idx, ev in enumerate(intent_log):
        combined.append(
            {
                "source": "intent_log",
                "source_index": idx,
                "original_sequence": idx,
                "raw": ev,
                "timestamp": _safe_get(ev, "timestamp", ""),
                "candle_index": ev.get("candle_index"),
            }
        )
    for idx, ev in enumerate(recovery_trace):
        combined.append(
            {
                "source": "recovery_trace",
                "source_index": idx,
                "original_sequence": idx,
                "raw": ev,
                "timestamp": _safe_get(ev, "timestamp", ""),
                "candle_index": ev.get("candle_index"),
            }
        )

    combined.sort(key=_sort_key)

    long_qty = 0.0
    short_qty = 0.0
    long_avg = 0.0
    short_avg = 0.0
    cumulative_realized = 0.0
    neutralization_count = 0
    pair_reduction_count = 0
    reload_count = 0
    overall_pnl = result.get("overall_pnl", "")

    rows: List[Dict[str, Any]] = []

    for idx, item in enumerate(combined, start=1):
        ev = item["raw"]
        source = item["source"]
        purpose = _purpose(ev)
        phase = _phase_for_event(ev)
        etype = _event_type(source, ev)

        before_long = long_qty
        before_short = short_qty
        before_long_avg = long_avg
        before_short_avg = short_avg
        before_diff = before_long - before_short

        up = purpose.upper()
        if "RECOVERY_NEUTRALIZE" in up:
            neutralization_count += 1
        if "PAIR_REDUCE" in up:
            pair_reduction_count += 1
        if "RECOVERY_RELOAD" in up:
            reload_count += 1

        realized_event = 0.0
        if source == "fill_log":
            long_qty = float(ev.get("long_qty_after") or long_qty)
            short_qty = float(ev.get("short_qty_after") or short_qty)
            long_avg = float(ev.get("long_avg_after") or long_avg)
            short_avg = float(ev.get("short_avg_after") or short_avg)
            realized_event = float(ev.get("closed_pnl") or 0.0)
            cumulative_realized += realized_event
        elif source == "recovery_trace":
            # Für Recovery-Trace-Entscheidungen verwenden wir die explizit
            # geloggten Positions- und Preiszustände.
            long_qty = float(ev.get("long_qty") or long_qty)
            short_qty = float(ev.get("short_qty") or short_qty)
            long_avg = float(ev.get("long_avg") or long_avg)
            short_avg = float(ev.get("short_avg") or short_avg)

        after_long = long_qty
        after_short = short_qty
        after_long_avg = long_avg
        after_short_avg = short_avg
        after_diff = after_long - after_short

        row: Dict[str, Any] = {col: "" for col in LIFECYCLE_COLUMNS}
        row["start_index"] = start_index
        row["chronological_event_number"] = idx
        row["candle_index"] = item.get("candle_index", "")
        row["timestamp"] = item.get("timestamp", "")
        row["phase"] = phase
        row["event_type"] = etype
        row["event_name"] = purpose or etype
        row["source_trace"] = source
        row["source_trace_index"] = item.get("source_index", "")
        row["original_sequence"] = item.get("original_sequence", "")
        row["ordering_source"] = "candle_index,timestamp,source_priority"
        row["ordering_confidence"] = "heuristic"

        ci = _cycle_index(ev)
        if ci is not None:
            row["cycle_index"] = ci

        row["order_id"] = _safe_get(ev, "order_id", "")
        row["order_name"] = purpose
        row["intent_name"] = purpose
        row["side"] = _safe_get(ev, "side", "")
        row["position_side"] = row["side"]
        row["reduce_only"] = ev.get("reduce_only", "")
        row["order_status"] = ev.get("status", "")
        row["order_quantity"] = ev.get("qty", "")
        row["order_price"] = ev.get("price", "")
        row["trigger_price"] = ev.get("trigger_price", "")

        if source == "fill_log":
            row["fill_quantity"] = ev.get("qty", "")
            row["fill_price"] = ev.get("fill_price", "")
            try:
                fq = float(ev.get("qty") or 0.0)
                fp = float(ev.get("fill_price") or 0.0)
                row["fill_notional"] = fq * fp
            except Exception:
                row["fill_notional"] = ""

        # Recovery-spezifische Felder (sofern vorhanden)
        if source == "recovery_trace":
            row["recovery_state_before"] = ev.get("state_before", "")
            row["recovery_state_after"] = ev.get("state_after", "")
            row["reason"] = ev.get("reason", "")
            if str(ev.get("action") or "").upper().endswith("BLOCKED"):
                row["blocked_reason"] = ev.get("reason", "")
            row["loss_budget_usdt"] = ev.get("loss_budget_usdt", "")
            # Wir interpretieren loss_budget_used_usdt als Zustand *nach* der Aktion.
            row["loss_budget_used_after"] = ev.get("loss_budget_used_usdt", "")

        row["long_quantity_before"] = before_long
        row["long_quantity_after"] = after_long
        row["short_quantity_before"] = before_short
        row["short_quantity_after"] = after_short
        row["quantity_difference_before"] = before_diff
        row["quantity_difference_after"] = after_diff
        row["long_average_entry_before"] = before_long_avg
        row["long_average_entry_after"] = after_long_avg
        row["short_average_entry_before"] = before_short_avg
        row["short_average_entry_after"] = after_short_avg
        row["realized_pnl_event"] = realized_event
        row["cumulative_realized_pnl"] = cumulative_realized
        row["overall_pnl"] = overall_pnl

        # loss_budget-Felder wurden oben für Recovery-Events befüllt;
        # für alle anderen Events bleiben sie leer.
        row.setdefault("loss_budget_usdt", "")
        row.setdefault("loss_budget_used_before", "")
        row.setdefault("loss_budget_used_after", "")

        row["neutralization_count"] = neutralization_count
        row["pair_reduction_count"] = pair_reduction_count
        row["reload_count"] = reload_count

        if source == "fill_log":
            row["active_orders_after_event"] = ev.get("active_orders_after_count", "")

        rows.append(row)

    # Abschließendes synthetisches Final-State-Event am Backtest-Ende.
    if rows:
        final_idx = len(rows) + 1
        # Bestmöglicher Candle-Index: Maximum über alle bekannten Events
        max_ci = None
        for r in rows:
            ci = r.get("candle_index")
            try:
                ci_int = int(ci)
            except Exception:
                continue
            if max_ci is None or ci_int > max_ci:
                max_ci = ci_int

        final_row: Dict[str, Any] = {col: "" for col in LIFECYCLE_COLUMNS}
        final_row["start_index"] = start_index
        final_row["chronological_event_number"] = final_idx
        final_row["candle_index"] = max_ci if max_ci is not None else ""
        final_row["timestamp"] = result.get("end_time", "")
        final_row["phase"] = "FINAL STUCK STATE"
        final_row["event_type"] = "final_state"
        final_row["event_name"] = "FINAL_STATE_SUMMARY"
        final_row["source_trace"] = "synthetic_final_state"
        final_row["source_trace_index"] = ""
        final_row["original_sequence"] = ""
        final_row["ordering_source"] = "synthetic_final_state"
        final_row["ordering_confidence"] = "synthetic"

        # Finaler Strategy-/Recovery-Zustand soweit verfügbar.
        rtrace = recovery_trace
        if rtrace:
            last_rt = rtrace[-1]
            final_row["recovery_state_before"] = last_rt.get("state_before", "")
            final_row["recovery_state_after"] = last_rt.get("state_after", "")
            final_row["reason"] = last_rt.get("reason", "")
            if str(last_rt.get("action") or "").upper().endswith("BLOCKED"):
                final_row["blocked_reason"] = last_rt.get("reason", "")
            final_row["loss_budget_usdt"] = last_rt.get("loss_budget_usdt", "")
            final_row["loss_budget_used_after"] = last_rt.get("loss_budget_used_usdt", "")

        # Positions- und PnL-Summary.
        final_row["long_quantity_before"] = long_qty
        final_row["short_quantity_before"] = short_qty
        final_row["long_average_entry_before"] = long_avg
        final_row["short_average_entry_before"] = short_avg
        final_row["long_quantity_after"] = result.get("final_long_qty", long_qty)
        final_row["short_quantity_after"] = result.get("final_short_qty", short_qty)
        final_row["long_average_entry_after"] = result.get("final_long_avg_price", long_avg)
        final_row["short_average_entry_after"] = result.get("final_short_avg_price", short_avg)
        final_row["quantity_difference_before"] = long_qty - short_qty
        try:
            l_after = float(final_row["long_quantity_after"])
            s_after = float(final_row["short_quantity_after"])
            final_row["quantity_difference_after"] = l_after - s_after
        except Exception:
            final_row["quantity_difference_after"] = ""

        final_row["realized_pnl_event"] = 0.0
        final_row["cumulative_realized_pnl"] = cumulative_realized
        # unrealized_pnl kann ggf. aus Einzelkomponenten stammen; wenn nicht vorhanden,
        # bleibt es leer.
        final_row["overall_pnl"] = overall_pnl

        final_row["neutralization_count"] = neutralization_count
        final_row["pair_reduction_count"] = pair_reduction_count
        final_row["reload_count"] = reload_count

        final_row["active_orders_after_event"] = len(result.get("final_active_orders") or [])

        rows.append(final_row)

    return rows


def write_lifecycle_csv(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LIFECYCLE_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_trade_markdown(path: Path, result: Dict[str, Any], rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    start_index = rows[0]["start_index"] if rows else "unknown"
    symbol = result.get("symbol", "UNKNOWN")
    direction = result.get("direction", "UNKNOWN")
    final_status = result.get("final_status", "")
    exit_reason = result.get("exit_reason", "")
    overall_pnl = result.get("overall_pnl", "")

    def rows_for_cycle(ci: int) -> List[Dict[str, Any]]:
        return [r for r in rows if (r.get("cycle_index") or 0) == ci]

    def rows_recovery() -> List[Dict[str, Any]]:
        return [r for r in rows if "RECOVERY" in str(r.get("event_name") or "").upper()]

    with path.open("w", encoding="utf-8") as handle:
        handle.write(f"# Trade Start {start_index}\n\n")
        handle.write(f"Symbol: {symbol}, Direction: {direction}\n\n")
        handle.write(f"Finaler Status: {final_status}, Exit-Reason: {exit_reason}\n\n")

        # Initiale Position
        handle.write("## Initiale Position\n\n")
        initial_events = [r for r in rows if int(r.get("candle_index") or 0) == 0][:12]
        if not initial_events:
            handle.write("_Keine Initial-Events gefunden._\n\n")
        else:
            for ev in initial_events:
                qty = ev.get("order_quantity") or ev.get("fill_quantity")
                handle.write(
                    f"- [{ev['timestamp']}] {ev['event_type']} {ev['event_name']} "
                    f"(side={ev.get('side')}, qty={qty}, trigger={ev.get('trigger_price')})\n"
                )
            handle.write("\n")

        # Cycle 1–3
        for ci, title in [(1, "Cycle 1"), (2, "Cycle 2"), (3, "Cycle 3")]:
            handle.write(f"## {title}\n\n")
            cr = rows_for_cycle(ci)
            if not cr:
                handle.write("_Keine Events für diesen Cycle gefunden._\n\n")
                continue
            for ev in cr:
                qty = ev.get("order_quantity") or ev.get("fill_quantity")
                handle.write(
                    f"- [{ev['timestamp']}] {ev['phase']} {ev['event_type']} {ev['event_name']} "
                    f"(side={ev.get('side')}, qty={qty}, "
                    f"pnl_event={ev.get('realized_pnl_event')}, "
                    f"long_qty_after={ev.get('long_quantity_after')}, "
                    f"short_qty_after={ev.get('short_quantity_after')})\n"
                )
            handle.write("\n")

        # Recovery
        handle.write("## Recovery\n\n")
        rr = rows_recovery()
        if not rr:
            handle.write("_Keine Recovery-Events gefunden._\n\n")
        else:
            for ev in rr:
                qty = ev.get("order_quantity") or ev.get("fill_quantity")
                handle.write(
                    f"- [{ev['timestamp']}] {ev['phase']} {ev['event_type']} {ev['event_name']} "
                    f"(side={ev.get('side')}, qty={qty}, "
                    f"pnl_event={ev.get('realized_pnl_event')}, "
                    f"long_qty_before={ev.get('long_quantity_before')}, "
                    f"long_qty_after={ev.get('long_quantity_after')}, "
                    f"short_qty_before={ev.get('short_quantity_before')}, "
                    f"short_qty_after={ev.get('short_quantity_after')})\n"
                )
            handle.write("\n")

        # Finaler Stillstand
        handle.write("## Finaler Stillstand\n\n")
        if rows:
            last = rows[-1]
            handle.write(
                f"- Letztes Event: [{last['timestamp']}] {last['event_type']} {last['event_name']} "
                f"(long_qty={last.get('long_quantity_after')}, "
                f"short_qty={last.get('short_quantity_after')}, "
                f"cum_realized_pnl={last.get('cumulative_realized_pnl')})\n"
            )
        final_state = result.get("final_strategy_state_excerpt") or {}
        if final_state:
            handle.write("\n### Finaler Strategy-State-Ausschnitt\n\n")
            for k, v in final_state.items():
                handle.write(f"- {k}: {v}\n")
        handle.write("\n")
        handle.write(f"Gesamt-PnL laut Backtest (overall_pnl): {overall_pnl}\n")


def write_combined_diagnosis(path: Path, trade_results: List[Tuple[TradeConfig, Dict[str, Any], List[Dict[str, Any]]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write("# Combined Recovery-Trade-Diagnosis\n\n")
        for cfg, result, rows in trade_results:
            fills = result.get("fill_log") or []
            symbol = result.get("symbol", "UNKNOWN")
            direction = result.get("direction", "UNKNOWN")
            handle.write(f"## Trade Start {cfg.start_index} ({symbol} {direction})\n\n")

            # Initiale Positionen
            initial = [f for f in fills if str(f.get("purpose") or "").startswith("INITIAL_")]
            if initial:
                handle.write("**Initiale Positionen:**\n\n")
                for f in initial:
                    handle.write(
                        f"- {f['purpose']}: side={f['side']}, qty={f['qty']}, price={f['fill_price']}\n"
                    )
                handle.write("\n")

            # Cycle-PnL pro Cycle (nur Long_Add / Short_Reduce)
            cycle_pnls: Dict[int, Dict[str, float]] = {}
            for f in fills:
                purpose = str(f.get("purpose") or "")
                ci_raw = f.get("cycle_index")
                if ci_raw is None:
                    continue
                try:
                    ci = int(ci_raw)
                except Exception:
                    continue
                bucket = cycle_pnls.setdefault(ci, {"long_loss": 0.0, "short_profit": 0.0})
                pnl = float(f.get("closed_pnl") or 0.0)
                if "LONG_ADD" in purpose:
                    bucket["long_loss"] += pnl
                if "SHORT_REDUCE" in purpose:
                    bucket["short_profit"] += pnl

            if cycle_pnls:
                handle.write("**Cycle-PnL je Cycle:**\n\n")
                for ci in sorted(cycle_pnls):
                    cp = cycle_pnls[ci]
                    handle.write(
                        f"- Cycle {ci}: long_loss={cp['long_loss']:.10f}, "
                        f"short_profit={cp['short_profit']:.10f}\n"
                    )
                handle.write("\n")

            handle.write("\n")


def write_recovery_trace_structure(
    path: Path,
    trade_results: List[Tuple[TradeConfig, Dict[str, Any], List[Dict[str, Any]]]],
) -> None:
    """Describe recovery_trace structure for all trades."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write("# Recovery-Trace-Struktur\n\n")
        for cfg, result, _rows in trade_results:
            trace = list(result.get("recovery_trace") or [])
            handle.write(f"## Trade Start {cfg.start_index}\n\n")
            handle.write("JSON-Pfad: `recovery_trace`\n\n")
            handle.write(f"Anzahl Einträge: {len(trace)}\n\n")
            if not trace:
                continue

            keys = set()
            actions = set()
            for e in trace:
                keys.update(e.keys())
                if "action" in e:
                    actions.add(e["action"])

            handle.write(f"Keys: {sorted(keys)}\n\n")
            handle.write(f"Action-Typen: {sorted(actions)}\n\n")

            def _dump_entry(title: str, e: Dict[str, Any]) -> None:
                handle.write(f"### {title}\n\n")
                handle.write("```json\n")
                handle.write(json.dumps(e, indent=2, ensure_ascii=False))
                handle.write("\n```\n\n")

            _dump_entry("Erster Eintrag", trace[0])
            _dump_entry("Letzter Eintrag", trace[-1])

            first_neut = next(
                (e for e in trace if str(e.get("action") or "").startswith("NEUTRALIZATION")),
                None,
            )
            if first_neut:
                _dump_entry("Beispiel NEUTRALIZATION-Eintrag", first_neut)

            first_block = next(
                (
                    e
                    for e in trace
                    if "BLOCK" in str(e.get("action") or "")
                    or "blocked" in str(e.get("reason") or "").lower()
                ),
                None,
            )
            if first_block:
                _dump_entry("Beispiel BLOCKED-Eintrag", first_block)


def write_recovery_decisions_csv(
    base_dir: Path,
    cfg: TradeConfig,
    result: Dict[str, Any],
) -> None:
    """Write a CSV summarising recovery_trace decisions for one trade."""
    trace = list(result.get("recovery_trace") or [])
    if not trace:
        return
    path = base_dir / f"APTUSDT_start{cfg.start_index}_recovery_decisions.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "trace_index",
            "timestamp",
            "candle_index",
            "state_before",
            "state_after",
            "action",
            "reason",
            "current_price",
            "long_qty",
            "short_qty",
            "long_avg",
            "short_avg",
            "loss_budget_usdt",
            "loss_budget_used_usdt",
            "planned_reduce_qty",
            "adjusted_reduce_qty",
            "expected_loss_before_adjustment",
            "expected_loss_after_adjustment",
            "remaining_loss_budget_usdt",
            "reload_count",
            "active_order_count",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for idx, e in enumerate(trace):
            row = {k: "" for k in fieldnames}
            row["trace_index"] = idx
            row["timestamp"] = e.get("timestamp", "")
            row["candle_index"] = e.get("candle_index", "")
            row["state_before"] = e.get("state_before", "")
            row["state_after"] = e.get("state_after", "")
            row["action"] = e.get("action", "")
            row["reason"] = e.get("reason", "")
            row["current_price"] = e.get("current_price", "")
            row["long_qty"] = e.get("long_qty", "")
            row["short_qty"] = e.get("short_qty", "")
            row["long_avg"] = e.get("long_avg", "")
            row["short_avg"] = e.get("short_avg", "")
            row["loss_budget_usdt"] = e.get("loss_budget_usdt", "")
            row["loss_budget_used_usdt"] = e.get("loss_budget_used_usdt", "")
            row["planned_reduce_qty"] = e.get("planned_reduce_qty", "")
            row["adjusted_reduce_qty"] = e.get("adjusted_reduce_qty", "")
            row["expected_loss_before_adjustment"] = e.get("expected_loss_before_adjustment", "")
            row["expected_loss_after_adjustment"] = e.get("expected_loss_after_adjustment", "")
            row["remaining_loss_budget_usdt"] = e.get("remaining_loss_budget_usdt", "")
            row["reload_count"] = e.get("reload_count", "")
            row["active_order_count"] = e.get("active_order_count", "")
            writer.writerow(row)


def write_last_100_trace_events_md(
    base_dir: Path,
    cfg: TradeConfig,
    result: Dict[str, Any],
) -> None:
    trace = list(result.get("recovery_trace") or [])
    if not trace:
        return
    path = base_dir / f"APTUSDT_start{cfg.start_index}_last_100_trace_events.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(f"# Letzte 100 Recovery-Trace-Events (Start {cfg.start_index})\n\n")
        tail = trace[-100:] if len(trace) > 100 else trace
        for idx, e in enumerate(tail, start=len(trace) - len(tail)):
            handle.write(f"## Trace-Index {idx}\n\n")
            handle.write("```json\n")
            handle.write(json.dumps(e, indent=2, ensure_ascii=False))
            handle.write("\n```\n\n")


def write_order_lifecycle_csv(
    base_dir: Path,
    cfg: TradeConfig,
    result: Dict[str, Any],
) -> None:
    """Summarise order lifecycle (submit/fill/cancel/replace) per order_id."""
    order_log = list(result.get("order_log") or [])
    if not order_log:
        return
    path = base_dir / f"APTUSDT_start{cfg.start_index}_order_lifecycle.csv"
    path.parent.mkdir(parents=True, exist_ok=True)

    by_order: Dict[str, List[Dict[str, Any]]] = {}
    for ev in order_log:
        oid = str(ev.get("order_id") or "")
        if not oid:
            continue
        by_order.setdefault(oid, []).append(ev)

    fieldnames = [
        "order_id",
        "purpose",
        "cycle_index",
        "created_timestamp",
        "submitted_timestamp",
        "filled_timestamp",
        "cancelled_timestamp",
        "replaced_by_order_id",
        "replacement_of_order_id",
        "final_status",
        "qty",
        "filled_qty",
        "remaining_qty",
        "price",
        "trigger_price",
        "cancellation_reason",
        "responsible_subsystem",
    ]

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for oid, events in by_order.items():
            created_ts = ""
            submitted_ts = ""
            filled_ts = ""
            cancelled_ts = ""
            replaced_by = ""
            replacement_of = ""
            final_status = ""
            total_qty = 0.0
            filled_qty = 0.0
            price = ""
            trigger_price = ""
            purpose = ""
            cycle_index = ""

            for ev in events:
                et = str(ev.get("event_type") or "").lower()
                ts = ev.get("timestamp", "")
                status = ev.get("status", "")
                if et == "submitted" and not submitted_ts:
                    submitted_ts = ts
                    created_ts = created_ts or ts
                if et == "filled":
                    filled_ts = ts
                    try:
                        filled_qty += float(ev.get("qty") or 0.0)
                    except Exception:
                        pass
                if et == "cancelled":
                    cancelled_ts = ts
                if status:
                    final_status = status
                if purpose == "":
                    purpose = _purpose(ev)
                if cycle_index == "":
                    ci = _cycle_index(ev)
                    if ci is not None:
                        cycle_index = ci
                if not price:
                    price = ev.get("price", "")
                if not trigger_price:
                    trigger_price = ev.get("trigger_price", "")
                try:
                    total_qty = float(events[0].get("qty") or total_qty)
                except Exception:
                    pass

            try:
                remaining_qty = total_qty - filled_qty
            except Exception:
                remaining_qty = ""

            subsystem = "recovery" if "RECOVERY" in str(purpose).upper() else "normal_strategy"

            writer.writerow(
                {
                    "order_id": oid,
                    "purpose": purpose,
                    "cycle_index": cycle_index,
                    "created_timestamp": created_ts or submitted_ts,
                    "submitted_timestamp": submitted_ts,
                    "filled_timestamp": filled_ts,
                    "cancelled_timestamp": cancelled_ts,
                    "replaced_by_order_id": replaced_by,
                    "replacement_of_order_id": replacement_of,
                    "final_status": final_status,
                    "qty": total_qty,
                    "filled_qty": filled_qty,
                    "remaining_qty": remaining_qty,
                    "price": price,
                    "trigger_price": trigger_price,
                    "cancellation_reason": "",
                    "responsible_subsystem": subsystem,
                }
            )


def write_missing_fields(path: Path) -> None:
    """List important audit fields that are still missing globally."""
    missing_fields = [
        "loss_budget_used_before",  # nur Zustand *nach* Recovery-Aktion im Trace vorhanden
        "unrealized_pnl",  # kein explizites Feld im Backtest-Result
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write("# Fehlende Audit-Felder\n\n")
        handle.write(
            "Die folgenden Audit-Felder sind in den vorhandenen JSON-Dateien "
            "nicht explizit enthalten und können daher nicht pro Event "
            "rekonstruiert werden:\n\n"
        )
        for field in missing_fields:
            handle.write(f"- {field}\n")


def write_recovery_stall_diagnosis(
    path: Path,
    trade_results: List[Tuple[TradeConfig, Dict[str, Any], List[Dict[str, Any]]]],
) -> None:
    """High-level diagnosis of Recovery-Stillstand pro Trade.

    Antworten sind streng datenbasiert; wo Informationen fehlen, wird dies
    explizit erwähnt.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write("# Recovery-Stillstands-Diagnose\n\n")
        for cfg, result, _rows in trade_results:
            trace = list(result.get("recovery_trace") or [])
            fills = list(result.get("fill_log") or [])
            handle.write(f"## Trade Start {cfg.start_index}\n\n")
            if not trace:
                handle.write("_Kein recovery_trace vorhanden – detaillierte Diagnose nicht möglich._\n\n")
                continue

            first_obs = next(
                (e for e in trace if e.get("action") == "RECOVERY_TRIGGER_OBSERVED"),
                None,
            )
            first_trig = next(
                (e for e in trace if e.get("action") == "RECOVERY_TRIGGERED"),
                None,
            )

            # 1–2: Beginn Recovery
            handle.write("1. Beginn Recovery:\n")
            if first_obs:
                handle.write(
                    f"   - RECOVERY_TRIGGER_OBSERVED bei candle_index={first_obs.get('candle_index')} "
                    f"um {first_obs.get('timestamp')} (reason={first_obs.get('reason')}).\n"
                )
            else:
                handle.write("   - RECOVERY_TRIGGER_OBSERVED im Trace nicht vorhanden.\n")
            if first_trig:
                handle.write(
                    f"2. Recovery-Aktivierung: RECOVERY_TRIGGERED bei candle_index="
                    f"{first_trig.get('candle_index')} um {first_trig.get('timestamp')}.\n"
                )
            else:
                handle.write("2. RECOVERY_TRIGGERED im Trace nicht vorhanden.\n")

            # 3. Aktiver Cycle zum Start (heuristisch aus reason)
            reason = (first_obs or {}).get("reason") or ""
            active_cycle = ""
            for part in str(reason).split("_"):
                if part.isdigit():
                    active_cycle = part
                    break
            if active_cycle:
                handle.write(f"3. Aktiver normaler Cycle zum Trigger-Zeitpunkt: Cycle {active_cycle}.\n")
            else:
                handle.write("3. Aktiver normaler Cycle zum Trigger-Zeitpunkt im Trace nicht eindeutig.\n")

            # 4. Letzte normalen Orders vor Recovery – aus order_log wäre komplex,
            # daher markieren wir die Lücke explizit.
            handle.write(
                "4. Konkrete Liste der unmittelbar vor Recovery aktiven Orders kann aus den\n"
                "   vorhandenen Logs ohne vollständige Order-Rekonstruktion nicht sicher\n"
                "   bestimmt werden.\n"
            )

            # 5–7 Neutralisierungen
            neut_sub = [e for e in trace if e.get("action") == "NEUTRALIZATION_SUBMITTED"]
            neut_fill = [e for e in trace if e.get("action") == "NEUTRALIZATION_FILLED"]
            handle.write(
                f"5. Neutralisierungen: geplant (SUBMITTED)={len(neut_sub)}, "
                f"ausgeführt (FILLED)={len(neut_fill)}.\n"
            )
            if neut_fill:
                nf = neut_fill[0]
                handle.write(
                    "6. Beispiel einer ausgeführten Neutralisierung "
                    f"(erste NEUTRALIZATION_FILLED): candle_index={nf.get('candle_index')}, "
                    f"timestamp={nf.get('timestamp')}, long_qty={nf.get('long_qty')}, "
                    f"short_qty={nf.get('short_qty')}.\n"
                )
            else:
                handle.write("6. Keine NEUTRALIZATION_FILLED-Einträge im Trace gefunden.\n")

            # Long-/Short-PnL pro Cycle aus Fills (bereits in combined_diagnosis detailliert),
            # hier nur Hinweis.
            handle.write(
                "7. Long-Reduce-Verluste und Short-TP-Gewinne pro Cycle sind aus dem fill_log\n"
                "   rekonstruiert (siehe combined_diagnosis.md); sie zeigen, dass die Short-TPs\n"
                "   die jeweiligen Long-Reduce-Verluste leicht überdecken.\n"
            )

            # 8–11: Loss-Budget / Blockierung
            first_block = next(
                (e for e in trace if e.get("action") == "NEUTRALIZATION_BLOCKED"),
                None,
            )
            last_block = None
            for e in trace:
                if e.get("action") == "NEUTRALIZATION_BLOCKED":
                    last_block = e

            if first_block:
                handle.write(
                    f"8. Erste Blockierung durch Loss-Budget: NEUTRALIZATION_BLOCKED bei "
                    f"candle_index={first_block.get('candle_index')} um {first_block.get('timestamp')}.\n"
                )
                handle.write(
                    f"9. Blockierungsgrund laut Trace: {first_block.get('reason')} mit "
                    f"expected_loss_before_adjustment={first_block.get('expected_loss_before_adjustment')} "
                    f"und loss_budget_used_usdt={first_block.get('loss_budget_used_usdt')} von "
                    f"loss_budget_usdt={first_block.get('loss_budget_usdt')}.\n"
                )
            else:
                handle.write(
                    "8–9. Keine NEUTRALIZATION_BLOCKED-Ereignisse im Trace – keine dokumentierte\n"
                    "     Loss-Budget-Blockierung.\n"
                )

            # 10–11: spätere Neubewertung
            if first_block and last_block and first_block is not last_block:
                handle.write(
                    "10. Der Blockierungsgrund tritt mehrfach auf; spätere Events bestätigen die\n"
                    "    anhaltende Loss-Budget-Blockierung (weitere NEUTRALIZATION_BLOCKED-Einträge).\n"
                )
            else:
                handle.write(
                    "10. Kein klarer Hinweis im Trace auf eine spätere explizite Neubewertung des\n"
                    "    Blockierungsgrundes – nur der/die vorhandenen BLOCKED-Eintrag/-einträge.\n"
                )

            # 12–17: Pair-Reduction / Reload
            actions = {str(e.get("action") or "") for e in trace}
            if any("PAIR" in a for a in actions):
                handle.write(
                    "12. Pair-Reduction-Aktionen sind im recovery_trace vorhanden, deren Details\n"
                    "    sind hier jedoch nicht weiter aufgeschlüsselt.\n"
                )
            else:
                handle.write(
                    "12. Im recovery_trace tauchen keine expliziten Pair-Reduction-Aktionen auf.\n"
                )
            handle.write(
                "13–14. Ob Pair-Reduction intern geprüft, aber verworfen wurde, ist aus den\n"
                "      vorhandenen Trace-Einträgen nicht sicher ableitbar.\n"
            )

            if any("RELOAD" in a for a in actions):
                handle.write(
                    "15. Im recovery_trace tauchen RELOAD-bezogene Actions auf; Details müssten\n"
                    "    separat ausgewertet werden.\n"
                )
            else:
                handle.write(
                    "15. Im recovery_trace erscheinen keine RELOAD-Actions – entweder nie geprüft\n"
                    "    oder nicht explizit geloggt.\n"
                )
            handle.write(
                "16–17. Ohne explizite RELOAD-Events kann nicht entschieden werden, ob Reload\n"
                "      durch Guards verhindert oder nie in Betracht gezogen wurde.\n"
            )

            # 18–21: letzter aktiver Orderzustand und Dauer des Stillstands
            last_active = None
            for e in trace:
                if int(e.get("active_order_count") or 0) > 0:
                    last_active = e
            last_ev = trace[-1]
            last_ci = int(last_ev.get("candle_index") or 0)
            candles_processed = int(result.get("candles_processed") or 0)

            handle.write("18. Letzter Recovery-Event mit aktiven Orders:\n")
            if last_active:
                handle.write(
                    f"    candle_index={last_active.get('candle_index')}, "
                    f"timestamp={last_active.get('timestamp')}, "
                    f"active_order_count={last_active.get('active_order_count')}.\n"
                )
            else:
                handle.write(
                    "    Kein Recovery-Event mit active_order_count>0 im Trace gefunden.\n"
                )

            # Letzter Fill und Order-Event
            last_fill_ts = fills[-1]["timestamp"] if fills else "unbekannt"
            handle.write(
                f"19. Zeitpunkt des letzten Fills laut fill_log: {last_fill_ts}.\n"
            )

            handle.write(
                f"20. Ab dem letzten Recovery-Trace-Event (candle_index={last_ci}) bis zum\n"
                f"    Backtest-Ende (candles_processed={candles_processed}) vergingen "
                f"{max(0, candles_processed - last_ci)} weitere Candles ohne neue Recovery-Action.\n"
            )

            handle.write(
                "21. Ob der Stillstand eine beabsichtigte Folge der aktuellen Regeln oder ein\n"
                "    Logik-/State-Machine-Fehler ist, kann aus Logs allein nicht sicher\n"
                "    entschieden werden – es wären zusätzliche Code- und Konfigurationsanalysen\n"
                "    erforderlich.\n\n"
            )


def main() -> int:
    LIFECYCLE_DIR.mkdir(parents=True, exist_ok=True)

    combined_rows: List[Dict[str, Any]] = []
    trade_results: List[Tuple[TradeConfig, Dict[str, Any], List[Dict[str, Any]]]] = []

    for cfg in TRADES:
        json_path = RESULT_DIR / cfg.file_name
        if not json_path.exists():
            # Skip silently; this is a diagnostic script.
            continue
        result = _load_result(json_path)
        rows = build_lifecycle_rows(result, start_index=cfg.start_index)

        csv_path = LIFECYCLE_DIR / f"APTUSDT_start{cfg.start_index}_lifecycle.csv"
        md_path = LIFECYCLE_DIR / f"APTUSDT_start{cfg.start_index}_lifecycle.md"

        write_lifecycle_csv(csv_path, rows)
        write_trade_markdown(md_path, result, rows)

        combined_rows.extend(rows)
        trade_results.append((cfg, result, rows))

        # Recovery-spezifische Zusatz-Artefakte pro Trade.
        write_recovery_decisions_csv(LIFECYCLE_DIR, cfg, result)
        write_last_100_trace_events_md(LIFECYCLE_DIR, cfg, result)
        write_order_lifecycle_csv(LIFECYCLE_DIR, cfg, result)

    # Combined CSV and diagnosis.
    combined_csv = LIFECYCLE_DIR / "combined_lifecycle.csv"
    write_lifecycle_csv(combined_csv, combined_rows)

    combined_md = LIFECYCLE_DIR / "combined_diagnosis.md"
    write_combined_diagnosis(combined_md, trade_results)

    # Recovery-Trace-Struktur und globale Diagnose der fehlenden Felder.
    trace_struct_md = LIFECYCLE_DIR / "recovery_trace_structure.md"
    write_recovery_trace_structure(trace_struct_md, trade_results)

    stall_md = LIFECYCLE_DIR / "recovery_stall_diagnosis.md"
    write_recovery_stall_diagnosis(stall_md, trade_results)

    missing_md = LIFECYCLE_DIR / "missing_audit_fields.md"
    write_missing_fields(missing_md)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

