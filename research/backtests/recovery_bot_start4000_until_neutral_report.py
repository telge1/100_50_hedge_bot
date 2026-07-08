from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from research.backtests.recovery_bot.calculations import compute_net_long_qty


REPO_ROOT = Path(__file__).resolve().parents[2]
FULL_RESULT_PATH = (
    REPO_ROOT
    / "research"
    / "backtests"
    / "results"
    / "recovery_bot_start4000_validated_no_budget"
    / "APTUSDT_start4000_full_validated_no_budget.json"
)
OUT_DIR = (
    REPO_ROOT
    / "research"
    / "backtests"
    / "results"
    / "recovery_bot_start4000_validated_no_budget"
)


@dataclass
class TimelineRow:
    index: int
    candle_index: Optional[int]
    timestamp: Optional[str]
    phase: str
    event_type: str
    purpose: Optional[str]
    side: Optional[str]
    action_kind: str
    fill_price: Optional[float]
    fill_qty: Optional[float]
    order_notional_usdt: Optional[float]
    realized_pnl_this_fill: float
    cumulative_realized_pnl: float
    long_qty_before: Optional[float]
    long_qty_after: Optional[float]
    short_qty_before: Optional[float]
    short_qty_after: Optional[float]
    net_long_qty_after: Optional[float]
    long_avg_before: Optional[float]
    long_avg_after: Optional[float]
    short_avg_before: Optional[float]
    short_avg_after: Optional[float]
    explanation: str


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_full_result() -> Dict[str, Any]:
    return json.loads(FULL_RESULT_PATH.read_text(encoding="utf-8"))


def _build_event_timeline(result: Dict[str, Any]) -> List[TimelineRow]:
    fill_log: List[Dict[str, Any]] = list(result.get("fill_log") or [])
    trace: List[Dict[str, Any]] = list(result.get("recovery_trace") or [])

    combined: List[Dict[str, Any]] = []

    for idx, ev in enumerate(fill_log):
        ci = ev.get("candle_index")
        if isinstance(ci, int) and ci <= 57:
            combined.append(
                {
                    "source": "fill_log",
                    "source_index": idx,
                    "raw": ev,
                    "candle_index": ci,
                    "timestamp": ev.get("timestamp"),
                }
            )

    for idx, ev in enumerate(trace):
        ci = ev.get("candle_index")
        if ci is None or not isinstance(ci, int) or ci > 57:
            continue
        action = str(ev.get("action") or "")
        if action not in {
            "RECOVERY_TRIGGER_OBSERVED",
            "RECOVERY_TRIGGERED",
            "NEUTRALIZATION_SUBMITTED",
            "NEUTRALIZATION_FILLED",
        }:
            continue
        combined.append(
            {
                "source": "recovery_trace",
                "source_index": idx,
                "raw": ev,
                "candle_index": ci,
                "timestamp": ev.get("timestamp"),
            }
        )

    def _sort_key(item: Dict[str, Any]) -> Tuple[int, str, int]:
        ci = int(item.get("candle_index") or 0)
        ts = str(item.get("timestamp") or "")
        source = str(item.get("source") or "")
        prio = {"recovery_trace": 0, "fill_log": 1}.get(source, 2)
        return ci, ts, prio

    combined.sort(key=_sort_key)

    rows: List[TimelineRow] = []
    cum_realized = 0.0
    long_qty: Optional[float] = None
    short_qty: Optional[float] = None
    long_avg: Optional[float] = None
    short_avg: Optional[float] = None

    index = 0

    for item in combined:
        src = item["source"]
        ev = item["raw"]
        ci = ev.get("candle_index")
        ts = ev.get("timestamp")

        long_before = long_qty
        short_before = short_qty
        long_avg_before = long_avg
        short_avg_before = short_avg

        phase = "NORMAL_CYCLE"
        event_type = ""
        action_kind = ""
        purpose: Optional[str] = None
        side: Optional[str] = None
        fill_price: Optional[float] = None
        fill_qty: Optional[float] = None
        notional: Optional[float] = None
        realized_this = 0.0
        explanation = ""

        if src == "fill_log":
            event_type = "FILL"
            purpose = str(ev.get("purpose") or ev.get("purpose_original") or "")
            side = str(ev.get("side") or "")
            fill_price = _safe_float(ev.get("fill_price"))
            fill_qty = _safe_float(ev.get("qty"))
            if fill_price is not None and fill_qty is not None:
                notional = abs(fill_price * fill_qty)

            if purpose in {"INITIAL_LONG_ENTRY", "INITIAL_SHORT_ENTRY"}:
                phase = "INITIAL"
                action_kind = "OPEN"
                explanation = f"Initial {side} entry"
            elif purpose.startswith("CYCLE_"):
                phase = "NORMAL_CYCLE"
                if "LONG_ADD" in purpose:
                    action_kind = "ADD"
                elif "SHORT_REDUCE" in purpose:
                    action_kind = "REDUCE"
                else:
                    action_kind = "ADD"
                explanation = f"Cycle fill: {purpose}"
            elif "REFILL" in purpose:
                phase = "REFILL"
                action_kind = "REFILL"
                explanation = f"Refill fill: {purpose}"
            elif purpose.startswith("RECOVERY_NEUTRALIZE"):
                phase = "RECOVERY_NEUTRALIZATION"
                action_kind = "NEUTRALIZE"
                explanation = "Recovery neutralization fill"
            elif "TP_EXIT" in purpose:
                phase = "NORMAL_CYCLE"
                action_kind = "TP"
                explanation = f"Take-profit exit: {purpose}"
            else:
                action_kind = "ADD"
                explanation = f"Fill: {purpose}"

            closed_pnl = _safe_float(ev.get("closed_pnl")) or 0.0
            realized_this = closed_pnl
            cum_realized += realized_this

            long_after = _safe_float(ev.get("long_qty_after"))
            short_after = _safe_float(ev.get("short_qty_after"))
            long_avg_after = _safe_float(ev.get("long_avg_after"))
            short_avg_after = _safe_float(ev.get("short_avg_after"))

            long_qty = long_after if long_after is not None else long_qty
            short_qty = short_after if short_after is not None else short_qty
            long_avg = long_avg_after if long_avg_after is not None else long_avg
            short_avg = short_avg_after if short_avg_after is not None else short_avg

        else:  # recovery_trace
            event_type = "RECOVERY"
            action = str(ev.get("action") or "")
            purpose = action
            side = None
            fill_price = _safe_float(ev.get("current_price"))
            fill_qty = None
            notional = None

            if action == "RECOVERY_TRIGGER_OBSERVED":
                phase = "RECOVERY_TRIGGER"
                action_kind = "RECOVERY_TRIGGER"
                explanation = (
                    f"Recovery trigger observed on purpose={ev.get('reason')}"
                )
            elif action == "RECOVERY_TRIGGERED":
                phase = "RECOVERY_TRIGGER"
                action_kind = "RECOVERY_TRIGGER"
                explanation = "Recovery activated (state TRIGGER_OBSERVED -> NEUTRALIZING)"
            elif action == "NEUTRALIZATION_SUBMITTED":
                phase = "RECOVERY_NEUTRALIZATION"
                action_kind = "NEUTRALIZE"
                explanation = "Neutralization order submitted"
            elif action == "NEUTRALIZATION_FILLED":
                phase = "RECOVERY_NEUTRALIZATION"
                action_kind = "NEUTRALIZE"
                explanation = "Neutralization reported as filled in trace"

            long_trace = _safe_float(ev.get("long_qty"))
            short_trace = _safe_float(ev.get("short_qty"))
            long_avg_trace = _safe_float(ev.get("long_avg"))
            short_avg_trace = _safe_float(ev.get("short_avg"))
            if long_trace is not None:
                long_qty = long_trace
            if short_trace is not None:
                short_qty = short_trace
            if long_avg_trace is not None:
                long_avg = long_avg_trace
            if short_avg_trace is not None:
                short_avg = short_avg_trace

        net_after: Optional[float] = None
        if long_qty is not None and short_qty is not None:
            net_after = compute_net_long_qty(long_qty, short_qty)

        index += 1
        row = TimelineRow(
            index=index,
            candle_index=ci if isinstance(ci, int) else None,
            timestamp=ts,
            phase=phase,
            event_type=event_type,
            purpose=purpose,
            side=side,
            action_kind=action_kind,
            fill_price=fill_price,
            fill_qty=fill_qty,
            order_notional_usdt=notional,
            realized_pnl_this_fill=realized_this,
            cumulative_realized_pnl=cum_realized,
            long_qty_before=long_before,
            long_qty_after=long_qty,
            short_qty_before=short_before,
            short_qty_after=short_qty,
            net_long_qty_after=net_after,
            long_avg_before=long_avg_before,
            long_avg_after=long_avg,
            short_avg_before=short_avg_before,
            short_avg_after=short_avg,
            explanation=explanation,
        )
        rows.append(row)

    return rows


def _write_csv(path: Path, rows: List[TimelineRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "index",
        "candle_index",
        "timestamp",
        "phase",
        "event_type",
        "purpose",
        "side",
        "action",
        "fill_price",
        "fill_qty",
        "order_notional_usdt",
        "realized_pnl_this_fill",
        "cumulative_realized_pnl",
        "long_qty_before",
        "long_qty_after",
        "short_qty_before",
        "short_qty_after",
        "net_long_qty_after",
        "long_avg_price_before",
        "long_avg_price_after",
        "short_avg_price_before",
        "short_avg_price_after",
        "explanation",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(
                {
                    "index": r.index,
                    "candle_index": r.candle_index,
                    "timestamp": r.timestamp,
                    "phase": r.phase,
                    "event_type": r.event_type,
                    "purpose": r.purpose,
                    "side": r.side,
                    "action": r.action_kind,
                    "fill_price": r.fill_price,
                    "fill_qty": r.fill_qty,
                    "order_notional_usdt": r.order_notional_usdt,
                    "realized_pnl_this_fill": r.realized_pnl_this_fill,
                    "cumulative_realized_pnl": r.cumulative_realized_pnl,
                    "long_qty_before": r.long_qty_before,
                    "long_qty_after": r.long_qty_after,
                    "short_qty_before": r.short_qty_before,
                    "short_qty_after": r.short_qty_after,
                    "net_long_qty_after": r.net_long_qty_after,
                    "long_avg_price_before": r.long_avg_before,
                    "long_avg_price_after": r.long_avg_after,
                    "short_avg_price_before": r.short_avg_before,
                    "short_avg_price_after": r.short_avg_after,
                    "explanation": r.explanation,
                }
            )


def _write_markdown(path: Path, result: Dict[str, Any], rows: List[TimelineRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fills: List[Dict[str, Any]] = list(result.get("fill_log") or [])
    summary: Dict[str, Any] = dict(result.get("recovery_summary") or {})

    initial_long = next(
        (f for f in fills if str(f.get("purpose") or "") == "INITIAL_LONG_ENTRY"),
        None,
    )
    initial_short = next(
        (f for f in fills if str(f.get("purpose") or "") == "INITIAL_SHORT_ENTRY"),
        None,
    )

    with path.open("w", encoding="utf-8") as handle:
        handle.write("# APTUSDT Start 4000 – Buchführung bis Netto-Neutralität\n\n")

        handle.write("## Chronologische Ereignisliste (bis Candle 57)\n\n")
        for r in rows:
            handle.write(
                f"- #{r.index} | candle_index={r.candle_index}, timestamp={r.timestamp}, "
                f"phase={r.phase}, event_type={r.event_type}, purpose={r.purpose}, side={r.side}, "
                f"action={r.action_kind}, fill_price={r.fill_price}, fill_qty={r.fill_qty}, "
                f"notional={r.order_notional_usdt}, realized_pnl_this_fill={r.realized_pnl_this_fill}, "
                f"cum_realized_pnl={r.cumulative_realized_pnl}, "
                f"long_qty_before={r.long_qty_before}, long_qty_after={r.long_qty_after}, "
                f"short_qty_before={r.short_qty_before}, short_qty_after={r.short_qty_after}, "
                f"net_long_qty_after={r.net_long_qty_after}, "
                f"long_avg_before={r.long_avg_before}, long_avg_after={r.long_avg_after}, "
                f"short_avg_before={r.short_avg_before}, short_avg_after={r.short_avg_after} "
                f"– {r.explanation}\n"
            )

        handle.write("\n## Ausgangszustand\n\n")
        if initial_long and initial_short:
            handle.write(
                f"- Initial Long: qty={initial_long.get('qty')}, price={initial_long.get('fill_price')}\n"
            )
            handle.write(
                f"- Initial Short: qty={initial_short.get('qty')}, price={initial_short.get('fill_price')}\n"
            )
            il_notional = (
                (_safe_float(initial_long.get("qty")) or 0.0)
                * (_safe_float(initial_long.get("fill_price")) or 0.0)
            )
            is_notional = (
                (_safe_float(initial_short.get("qty")) or 0.0)
                * (_safe_float(initial_short.get("fill_price")) or 0.0)
            )
            handle.write(
                f"- Initiales Notional: long≈{il_notional:.6f} USDT, short≈{is_notional:.6f} USDT\n"
            )
        else:
            handle.write("- Initial Entries nicht vollständig im Result gespeichert.\n")

        handle.write("\n## Normale Bot-Phase (vor Recovery)\n\n")
        cycle1_long = next(
            (f for f in fills if str(f.get("purpose") or "") == "CYCLE_1_LONG_ADD"),
            None,
        )
        cycle1_short = next(
            (f for f in fills if str(f.get("purpose") or "") == "CYCLE_1_SHORT_REDUCE"),
            None,
        )
        cycle2_long = next(
            (f for f in fills if str(f.get("purpose") or "") == "CYCLE_2_LONG_ADD"),
            None,
        )
        cycle2_short = next(
            (f for f in fills if str(f.get("purpose") or "") == "CYCLE_2_SHORT_REDUCE"),
            None,
        )

        handle.write("### Cycle 1\n\n")
        if cycle1_long and cycle1_short:
            c1_long_pnl = _safe_float(cycle1_long.get("closed_pnl")) or 0.0
            c1_short_pnl = _safe_float(cycle1_short.get("closed_pnl")) or 0.0
            handle.write(
                f"- Long-Reduce: candle_index={cycle1_long.get('candle_index')}, "
                f"price={cycle1_long.get('fill_price')}, qty={cycle1_long.get('qty')}, "
                f"realized_pnl={c1_long_pnl}\n"
            )
            handle.write(
                f"- Short-TP: candle_index={cycle1_short.get('candle_index')}, "
                f"price={cycle1_short.get('fill_price')}, qty={cycle1_short.get('qty')}, "
                f"realized_pnl={c1_short_pnl}\n"
            )
            handle.write(
                f"- Netto-Ergebnis Cycle 1: {c1_long_pnl + c1_short_pnl}\n"
            )
            handle.write(
                f"- Position danach: long_qty={cycle1_short.get('long_qty_after')}, "
                f"short_qty={cycle1_short.get('short_qty_after')}\n"
            )
        else:
            handle.write("- Cycle 1 nicht vollständig im Result gespeichert.\n")

        handle.write("\n### Cycle 2\n\n")
        if cycle2_long and cycle2_short:
            c2_long_pnl = _safe_float(cycle2_long.get("closed_pnl")) or 0.0
            c2_short_pnl = _safe_float(cycle2_short.get("closed_pnl")) or 0.0
            handle.write(
                f"- Long-Reduce: candle_index={cycle2_long.get('candle_index')}, "
                f"price={cycle2_long.get('fill_price')}, qty={cycle2_long.get('qty')}, "
                f"realized_pnl={c2_long_pnl}\n"
            )
            handle.write(
                f"- Short-TP: candle_index={cycle2_short.get('candle_index')}, "
                f"price={cycle2_short.get('fill_price')}, qty={cycle2_short.get('qty')}, "
                f"realized_pnl={c2_short_pnl}\n"
            )
            handle.write(
                f"- Netto-Ergebnis Cycle 2: {c2_long_pnl + c2_short_pnl}\n"
            )
            handle.write(
                f"- Position danach: long_qty={cycle2_short.get('long_qty_after')}, "
                f"short_qty={cycle2_short.get('short_qty_after')}\n"
            )
        else:
            handle.write("- Cycle 2 nicht vollständig im Result gespeichert.\n")

        handle.write("\n## Recovery-Aktivierung\n\n")
        handle.write(
            f"- Trigger-Candle: {summary.get('start_candle_index')} "
            f"(laut recovery_summary)\n"
        )

        handle.write("\n## Neutralisierung\n\n")
        handle.write(
            f"- Anzahl Schritte: {summary.get('neutralization_count')}\n"
        )
        handle.write(
            f"- Gesamter Recovery-Verlust: {summary.get('recovery_realized_pnl')}\n"
        )

        handle.write("\n## Netto-neutraler Punkt (Candle 57)\n\n")
        handle.write(
            f"- Candle 57: long_qty={summary.get('remaining_long_qty')}, "
            f"short_qty={summary.get('remaining_short_qty')}, "
            f"netto≈{(summary.get('remaining_long_qty') or 0.0) - (summary.get('remaining_short_qty') or 0.0)}\n"
        )
        handle.write(
            f"- final_state={summary.get('final_state')}, "
            f"blocked_reason={summary.get('blocked_reason')}\n"
        )


def main() -> int:
    result = _load_full_result()
    rows = _build_event_timeline(result)

    csv_path = OUT_DIR / "APTUSDT_start4000_until_neutral.csv"
    _write_csv(csv_path, rows)

    md_path = OUT_DIR / "APTUSDT_start4000_until_neutral.md"
    _write_markdown(md_path, result, rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

