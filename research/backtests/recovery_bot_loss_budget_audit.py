from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from research.backtests.recovery_bot.calculations import (
    compute_net_long_qty,
)


BASE_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BASE_DIR / "results" / "recovery_bot_current_three_audit"
LIFECYCLE_DIR = RESULTS_DIR / "lifecycle_audit"


@dataclass
class BlockedNeutralization:
    trade_start_index: int
    trace_index: int
    candle_index: int | None
    timestamp: str | None
    current_price: float | None
    loss_budget_usdt: float | None
    loss_budget_used_usdt: float
    remaining_loss_budget_usdt: float | None
    planned_reduce_qty: float | None
    expected_loss_before: float | None
    long_qty: float | None
    short_qty: float | None
    long_avg: float | None
    short_avg: float | None


def _to_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _load_full_result(start_index: int) -> dict[str, Any]:
    path = RESULTS_DIR / f"APTUSDT_start{start_index}_full.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return data


def _blocked_neutralizations(
    start_index: int,
    result: dict[str, Any],
) -> list[BlockedNeutralization]:
    trace: list[dict[str, Any]] = list(result.get("recovery_trace") or [])
    blocked: list[BlockedNeutralization] = []
    for idx, entry in enumerate(trace):
        if str(entry.get("action") or "") != "NEUTRALIZATION_BLOCKED":
            continue
        loss_budget_usdt = _to_float(entry.get("loss_budget_usdt"))
        loss_budget_used = _to_float(entry.get("loss_budget_used_usdt")) or 0.0
        remaining = _to_float(entry.get("remaining_loss_budget_usdt"))
        planned = _to_float(entry.get("planned_reduce_qty"))
        expected_loss_before = _to_float(entry.get("expected_loss_before_adjustment"))
        blocked.append(
            BlockedNeutralization(
                trade_start_index=start_index,
                trace_index=idx,
                candle_index=entry.get("candle_index"),
                timestamp=entry.get("timestamp"),
                current_price=_to_float(entry.get("current_price")),
                loss_budget_usdt=loss_budget_usdt,
                loss_budget_used_usdt=loss_budget_used,
                remaining_loss_budget_usdt=remaining,
                planned_reduce_qty=planned,
                expected_loss_before=expected_loss_before,
                long_qty=_to_float(entry.get("long_qty")),
                short_qty=_to_float(entry.get("short_qty")),
                long_avg=_to_float(entry.get("long_avg")),
                short_avg=_to_float(entry.get("short_avg")),
            )
        )
    return blocked


def _first(iterable: Iterable[BlockedNeutralization]) -> BlockedNeutralization | None:
    for item in iterable:
        return item
    return None


def _middle(items: list[BlockedNeutralization]) -> BlockedNeutralization | None:
    if not items:
        return None
    return items[len(items) // 2]


def _last(items: list[BlockedNeutralization]) -> BlockedNeutralization | None:
    if not items:
        return None
    return items[-1]


def _write_per_trade_steps_csv(
    path: Path,
    blocks: list[BlockedNeutralization],
) -> None:
    if not blocks:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "trace_index",
        "candle_index",
        "timestamp",
        "current_price",
        "loss_budget_usdt",
        "loss_budget_used_usdt",
        "remaining_loss_budget_usdt",
        "planned_reduce_qty",
        "expected_loss_before",
        "net_long_qty",
        "required_total_budget_for_step",
        "additional_budget_vs_1_50",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for b in blocks:
            net_long = None
            if b.long_qty is not None and b.short_qty is not None:
                net_long = compute_net_long_qty(b.long_qty, b.short_qty)
            required_total = None
            additional_vs_1_5 = None
            if b.expected_loss_before is not None:
                required_total = b.loss_budget_used_usdt + b.expected_loss_before
                additional_vs_1_5 = required_total - 1.5
            writer.writerow(
                {
                    "trace_index": b.trace_index,
                    "candle_index": b.candle_index,
                    "timestamp": b.timestamp,
                    "current_price": b.current_price,
                    "loss_budget_usdt": b.loss_budget_usdt,
                    "loss_budget_used_usdt": b.loss_budget_used_usdt,
                    "remaining_loss_budget_usdt": b.remaining_loss_budget_usdt,
                    "planned_reduce_qty": b.planned_reduce_qty,
                    "expected_loss_before": b.expected_loss_before,
                    "net_long_qty": net_long,
                    "required_total_budget_for_step": required_total,
                    "additional_budget_vs_1_50": additional_vs_1_5,
                }
            )


def _markdown_header_for_trade(start_index: int) -> str:
    return f"## Trade Start {start_index}\n\n"


def _format_float(value: float | None, digits: int = 6) -> str:
    if value is None:
        return ""
    return f"{value:.{digits}f}"


def _build_trade_markdown(
    start_index: int,
    result: dict[str, Any],
    blocks: list[BlockedNeutralization],
) -> str:
    out: list[str] = []
    out.append(_markdown_header_for_trade(start_index))

    # Ausgangslage: Recovery-Start und erste Blockierung.
    trace: list[dict[str, Any]] = list(result.get("recovery_trace") or [])
    start_entry = next(
        (e for e in trace if str(e.get("action") or "") == "RECOVERY_TRIGGERED"),
        trace[0] if trace else None,
    )
    first_block = _first(blocks)

    out.append("### Ausgangslage\n")
    if start_entry:
        out.append("- **Recovery-Start**:\n")
        out.append(
            f"  - action=`{start_entry.get('action')}`, "
            f"candle_index={start_entry.get('candle_index')}, "
            f"timestamp={start_entry.get('timestamp')}\n"
        )
    if first_block:
        out.append("- **Erste Blockierung (NEUTRALIZATION_BLOCKED)**:\n")
        out.append(
            f"  - candle_index={first_block.candle_index}, "
            f"timestamp={first_block.timestamp}, "
            f"current_price={_format_float(first_block.current_price, 6)}\n"
        )
        out.append(
            f"  - loss_budget_usdt={_format_float(first_block.loss_budget_usdt)}, "
            f"loss_budget_used_usdt={_format_float(first_block.loss_budget_used_usdt)}, "
            f"remaining_loss_budget_usdt={_format_float(first_block.remaining_loss_budget_usdt)}\n"
        )
        out.append(
            f"  - long_qty={_format_float(first_block.long_qty)}, "
            f"short_qty={_format_float(first_block.short_qty)}, "
            f"long_avg={_format_float(first_block.long_avg)}, "
            f"short_avg={_format_float(first_block.short_avg)}\n"
        )
        if (
            first_block.long_qty is not None
            and first_block.short_qty is not None
        ):
            net = compute_net_long_qty(first_block.long_qty, first_block.short_qty)
            out.append(f"  - netto_long_qty={_format_float(net)}\n")
    out.append("\n")

    # Nächste Neutralisierung (Variante A: frühestmögliche Weiterführung).
    out.append("### Nächste Neutralisierung (Variante A – erste Blockierung)\n\n")
    if first_block and first_block.expected_loss_before is not None:
        required_total = (
            first_block.loss_budget_used_usdt + first_block.expected_loss_before
        )
        additional_vs_1_5 = required_total - 1.5
        out.append(
            f"- **geplante Qty**: {_format_float(first_block.planned_reduce_qty)}\n"
        )
        out.append(
            f"- **erwarteter Verlust**: {_format_float(first_block.expected_loss_before)} USDT\n"
        )
        out.append(
            f"- **erforderliches Gesamtbudget**: {_format_float(required_total)} USDT\n"
        )
        out.append(
            f"- **zusätzlich benötigtes Budget gegenüber 1.50 USDT**: "
            f"{_format_float(additional_vs_1_5)} USDT\n"
        )
    else:
        out.append(
            "- Für die erste Blockierung liegen im Trace keine vollständigen "
            "Budget-Felddaten vor (planned_reduce_qty/expected_loss_before_adjustment), "
            "daher kann das erforderliche Budget nicht exakt berechnet werden.\n"
        )
    out.append("\n")

    # Bis Pair-Neutralität / Pair-Reduction: mit den vorhandenen Daten nicht exakt berechenbar.
    out.append("### Bis Pair-Neutralität / Pair Reduction\n\n")
    out.append(
        "Die weiteren Neutralisierungsschritte bis zur Pair-Neutralität können mit den "
        "vorhandenen Trace-Daten und ohne erneute Simulation der gesamten "
        "State-Machine nicht **exakt** rekonstruiert werden. Insbesondere fehlen:\n"
    )
    out.append(
        "- die vollständige Folge hypothetischer Fills, die bei höherem Loss-Budget "
        "ausgeführt worden wären,\n"
    )
    out.append(
        "- die daraus resultierenden Aktualisierungen von `long_avg` und `short_avg` "
        "für jedes einzelne Schritt-Fill.\n\n"
    )
    out.append(
        "Daher werden für die Budgetwerte *bis Pair-Neutralität*, *bis MINIMUM_PAIR_REACHED* "
        "und *bis READY_TO_CLOSE* keine numerischen Werte ausgegeben; sie wären nur mit "
        "zusätzlicher, nicht im Trace enthaltener Simulation belegbar.\n\n"
    )

    # Variante B: spätere Blockierungen – nur direkte Trace-Werte.
    out.append("### Variante B – spätere Blockierungen\n\n")
    if len(blocks) >= 1:
        b_first = _first(blocks)
        b_mid = _middle(blocks)
        b_last = _last(blocks)
        out.append("| Position | Candle | Preis | geplante Qty | erwarteter Verlust | benötigtes Gesamtbudget |\n")
        out.append("| -------- | ------ | ----- | -----------: | ------------------: | ----------------------: |\n")
        for label, b in [
            ("erste Blockierung", b_first),
            ("mittlere Blockierung", b_mid),
            ("letzte Blockierung", b_last),
        ]:
            if b is None:
                continue
            required_total = (
                b.loss_budget_used_usdt + b.expected_loss_before
                if b.expected_loss_before is not None
                else None
            )
            out.append(
                f"| {label} | {b.candle_index} | "
                f"{_format_float(b.current_price, 6)} | "
                f"{_format_float(b.planned_reduce_qty)} | "
                f"{_format_float(b.expected_loss_before)} | "
                f"{_format_float(required_total)} |\n"
            )
        out.append("\n")
        out.append(
            "Die Tabelle zeigt, wie das für eine einzelne Neutralisierung benötigte "
            "Loss-Budget mit fortschreitendem Preisverfall ansteigt. Da die Engine "
            "pro Candle höchstens einen Schritt ausführt und die tatsächlichen "
            "Zustands-Updates für hypothetische zusätzliche Schritte fehlen, werden "
            "keine kumulierten Budgetwerte über mehrere hypothetische Schritte angegeben.\n\n"
        )
    else:
        out.append(
            "- Für diesen Trade existiert im `recovery_trace` kein "
            "`NEUTRALIZATION_BLOCKED`-Ereignis; eine Budgetanalyse ist daher nicht "
            "anwendbar.\n\n"
        )

    # Pair Reduction und vollständiger Exit – hier nur qualitative Bewertung.
    out.append("### Pair Reduction und vollständiger Exit\n\n")
    out.append(
        "Da die betrachteten Trades den State `PAIR_REDUCING` nie erreichen und "
        "im Trace keine `PAIR_REDUCTION_*`-Events vorhanden sind, kann das zusätzlich "
        "erforderliche Budget für Pair Reduction und Final Exit anhand der vorhandenen "
        "Daten nicht numerisch belegt werden. Der aktuelle Code verlangt für diese "
        "Phasen explizite Zustandsübergänge und tatsächliche Fills, die bei den drei "
        "Trades aufgrund der Loss-Budget-Blockierung ausbleiben.\n\n"
    )

    return "".join(out)


def _write_markdown_summary(
    path: Path,
    per_trade_md: list[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write("# Recovery-Loss-Budget-Anforderungen\n\n")
        handle.write(
            "Diese Datei fasst pro Trade die aus dem `recovery_trace` belegbaren "
            "Loss-Budget-Anforderungen für blockierte Neutralisierungen zusammen. "
            "Es werden nur Werte ausgewiesen, die direkt aus Trace-Feldern oder "
            "einfachen, aus dem Code ableitbaren Formeln reproduzierbar berechnet "
            "werden können.\n\n"
        )
        for section in per_trade_md:
            handle.write(section)


def _write_summary_csv(
    path: Path,
    blocked_per_trade: dict[int, list[BlockedNeutralization]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "start_index",
        "budget_current",
        "budget_used",
        "budget_remaining",
        "next_neutralization_total_budget",
        "next_neutralization_additional_vs_1_50",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for start_index in sorted(blocked_per_trade):
            blocks = blocked_per_trade[start_index]
            first_block = _first(blocks)
            if not first_block:
                writer.writerow(
                    {
                        "start_index": start_index,
                        "budget_current": "",
                        "budget_used": "",
                        "budget_remaining": "",
                        "next_neutralization_total_budget": "",
                        "next_neutralization_additional_vs_1_50": "",
                    }
                )
                continue
            budget_current = first_block.loss_budget_usdt
            budget_used = first_block.loss_budget_used_usdt
            budget_remaining = (
                budget_current - budget_used
                if budget_current is not None
                else None
            )
            total_for_step = None
            additional_vs_1_5 = None
            if first_block.expected_loss_before is not None:
                total_for_step = (
                    budget_used + first_block.expected_loss_before
                )
                additional_vs_1_5 = total_for_step - 1.5
            writer.writerow(
                {
                    "start_index": start_index,
                    "budget_current": budget_current,
                    "budget_used": budget_used,
                    "budget_remaining": budget_remaining,
                    "next_neutralization_total_budget": total_for_step,
                    "next_neutralization_additional_vs_1_50": additional_vs_1_5,
                }
            )


def main() -> int:
    trade_starts = [4000, 7500, 9750]
    per_trade_blocks: dict[int, list[BlockedNeutralization]] = {}
    per_trade_md: list[str] = []

    for start in trade_starts:
        result = _load_full_result(start)
        blocks = _blocked_neutralizations(start, result)
        per_trade_blocks[start] = blocks

        # Per-Trade CSV (optional Detailansicht der Blockierungen).
        steps_csv = (
            LIFECYCLE_DIR
            / f"APTUSDT_start{start}_loss_budget_steps.csv"
        )
        _write_per_trade_steps_csv(steps_csv, blocks)

        per_trade_md.append(_build_trade_markdown(start, result, blocks))

    # Zusammenfassende CSV-Übersicht.
    summary_csv = (
        LIFECYCLE_DIR / "recovery_loss_budget_requirements.csv"
    )
    _write_summary_csv(summary_csv, per_trade_blocks)

    # Markdown-Bericht.
    summary_md = (
        LIFECYCLE_DIR / "recovery_loss_budget_requirements.md"
    )
    _write_markdown_summary(summary_md, per_trade_md)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

