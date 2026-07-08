from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from research.backtests.backtest_report import BacktestResult
from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
from research.backtests.fill_models import resolve_fill_model_config
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.backtest_config_loader import (
    DEFAULT_SHORT_CONFIG_PATH,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
BASE_RESULTS_DIR = (
    REPO_ROOT / "research" / "backtests" / "results" / "recovery_bot_current_three_audit"
)
BASE_FULL_PATH = BASE_RESULTS_DIR / "APTUSDT_start4000_full.json"

OUT_DIR = (
    REPO_ROOT
    / "research"
    / "backtests"
    / "results"
    / "start4000_strict_no_refill_no_recovery"
)


def _load_baseline_full() -> Dict[str, Any]:
    return json.loads(BASE_FULL_PATH.read_text(encoding="utf-8"))


def _load_window_candles(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Rekonstruiere exakt dasselbe Candle-Fenster wie im Audit-Lauf."""
    source_ts = str(meta.get("source_candle_timestamp") or "")
    candles_processed = int(meta.get("candles_processed") or 0)
    rows = load_candles_for_symbol(
        "APTUSDT",
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
        if ts_str == source_ts:
            start_idx = idx
            break
    if start_idx is None:
        raise ValueError(
            f"could not find candle with timestamp={source_ts!r} in APTUSDT candles"
        )
    end_idx = start_idx + candles_processed
    return rows[start_idx:end_idx]


def _build_long_config_without_refill(baseline: Dict[str, Any]) -> Path:
    """Kopiere die Live-Config und deaktiviere ausschließlich den Time-Distance-Refill."""
    cfg_diag = dict(baseline.get("config_diagnostics") or {})
    cfg_path_str = cfg_diag.get("config_path")
    if not cfg_path_str:
        raise ValueError("baseline config_diagnostics.config_path missing")
    cfg_path = Path(cfg_path_str)
    if not cfg_path.is_absolute():
        cfg_path = REPO_ROOT / cfg_path

    payload = json.loads(cfg_path.read_text(encoding="utf-8"))
    # Laut FixedCycleHedgeConfig-Dokumentation deaktivieren 0 oder negative Werte
    # den Time-Distance-Refill-Trigger. Zusätzlich schalten wir für diesen
    # Backtest explizit Cycle- und Recovery-Refillpfade ab.
    payload["time_distance_refill_trigger_minutes"] = 0
    payload["disable_cycle_refill"] = True
    payload["disable_recovery_refill"] = True

    out_cfg_path = OUT_DIR / "long_config_no_refill.json"
    out_cfg_path.parent.mkdir(parents=True, exist_ok=True)
    out_cfg_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out_cfg_path


def _write_full_result(path: Path, result: BacktestResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = result.to_dict()
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _build_fill_timeline(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    fill_log: List[Dict[str, Any]] = list(result.get("fill_log") or [])
    rows: List[Dict[str, Any]] = []

    cum_pnl = 0.0
    long_qty: Optional[float] = None
    short_qty: Optional[float] = None
    long_avg: Optional[float] = None
    short_avg: Optional[float] = None

    for ev in fill_log:
        ci = ev.get("candle_index")
        ts = ev.get("timestamp")
        purpose = ev.get("purpose") or ev.get("purpose_original") or ""
        side = ev.get("side")
        fill_price = _safe_float(ev.get("fill_price"))
        qty = _safe_float(ev.get("qty"))
        closed_pnl = _safe_float(ev.get("closed_pnl")) or 0.0

        long_before = long_qty
        short_before = short_qty

        cum_pnl += closed_pnl

        long_after = _safe_float(ev.get("long_qty_after"))
        short_after = _safe_float(ev.get("short_qty_after"))
        long_avg_after = _safe_float(ev.get("long_avg_after"))
        short_avg_after = _safe_float(ev.get("short_avg_after"))

        if long_after is not None:
            long_qty = long_after
        if short_after is not None:
            short_qty = short_after
        if long_avg_after is not None:
            long_avg = long_avg_after
        if short_avg_after is not None:
            short_avg = short_avg_after

        net_after: Optional[float] = None
        ratio_after: Optional[float] = None
        if long_qty is not None and short_qty is not None:
            net_after = float(long_qty) - float(short_qty)
            if abs(short_qty) > 1e-12:
                ratio_after = float(long_qty) / float(short_qty)

        rows.append(
            {
                "candle_index": ci,
                "timestamp": ts,
                "purpose": purpose,
                "side": side,
                "fill_price": fill_price,
                "fill_qty": qty,
                "realized_pnl_this_fill": closed_pnl,
                "cumulative_realized_pnl": cum_pnl,
                "long_qty_before": long_before,
                "long_qty_after": long_qty,
                "short_qty_before": short_before,
                "short_qty_after": short_qty,
                "net_long_qty_after": net_after,
                "long_short_ratio_after": ratio_after,
                "long_avg_after": long_avg,
                "short_avg_after": short_avg,
            }
        )
    return rows


def _assert_no_refill_markers(result: Dict[str, Any]) -> None:
    """Fail fast if any refill-related *purpose* sneaks into the backtest.

    We restrict the check to purpose-like fields in the various logs so that
    diagnostic dumps of constant names (e.g. mapping tables) do not trigger
    false positives.
    """
    forbidden_prefixes = (
        "REFILL_LONG",
        "REFILL_SHORT",
        "RECOVERY_REFILL_",
        "RECOVERY_RELOAD_",
    )

    def _check_value(raw: Any) -> str | None:
        text = str(raw or "")
        upper = text.upper()
        for prefix in forbidden_prefixes:
            if upper.startswith(prefix):
                return text
        return None

    offenders: List[Tuple[str, str]] = []

    def _scan_section(section_name: str, records: List[Dict[str, Any]]) -> None:
        for rec in records:
            for key in (
                "purpose",
                "purpose_original",
                "intent_purpose",
                "source_fill_purpose",
                "target_purpose",
            ):
                if key in rec:
                    hit = _check_value(rec.get(key))
                    if hit is not None:
                        offenders.append((section_name, hit))

    _scan_section("fill_log", list(result.get("fill_log") or []))
    _scan_section("order_log", list(result.get("order_log") or []))
    _scan_section("intent_log", list(result.get("intent_log") or []))
    _scan_section("final_active_orders", list(result.get("final_active_orders") or []))

    if offenders:
        details = ", ".join(f"{section}:{value}" for section, value in offenders)
        raise RuntimeError(
            f"strict no-refill invariant violated; found refill purposes in: {details}"
        )


@dataclass
class CycleSummaryRow:
    cycle_index: int
    long_reduce_purpose: str | None
    long_reduce_candle: int | None
    long_reduce_fill_price: float | None
    long_reduce_qty: float | None
    long_reduce_realized_pnl: float
    short_reduce_purpose: str | None
    short_reduce_candle: int | None
    short_reduce_fill_price: float | None
    short_reduce_qty: float | None
    short_reduce_realized_pnl: float
    cycle_net_pnl: float
    long_qty_before_cycle: float | None
    short_qty_before_cycle: float | None
    long_qty_after_cycle: float | None
    short_qty_after_cycle: float | None
    net_long_qty_after_cycle: float | None
    long_short_ratio_after_cycle: float | None
    total_position_notional_after_cycle: float | None
    cycle_complete: bool
    blocked_reason: str | None


def _build_cycle_summary(result: Dict[str, Any]) -> List[CycleSummaryRow]:
    fill_log: List[Dict[str, Any]] = list(result.get("fill_log") or [])
    rows: List[CycleSummaryRow] = []

    # Track position over time to know before/after-cycle states.
    long_qty: Optional[float] = None
    short_qty: Optional[float] = None

    # Group fills by cycle_index.
    cycles: Dict[int, Dict[str, Any]] = {}

    for ev in fill_log:
        purpose = str(ev.get("purpose") or ev.get("purpose_original") or "")
        ci = ev.get("candle_index")
        cycle_index = ev.get("cycle_index")
        if not isinstance(cycle_index, int) or cycle_index <= 0:
            continue

        if purpose not in {
            f"CYCLE_{cycle_index}_LONG_ADD",
            f"CYCLE_{cycle_index}_SHORT_REDUCE",
        }:
            continue

        cycles.setdefault(cycle_index, {}).setdefault("fills", []).append(ev)

    # Walk again in chronological order to compute before/after per cycle.
    for ci in sorted(cycles.keys()):
        data = cycles[ci]
        fills = sorted(
            data["fills"],
            key=lambda ev: (
                int(ev.get("candle_index") or 0),
                (ev.get("timestamp") or ""),
            ),
        )
        if not fills:
            continue

        # Determine before-state from first relevant fill.
        first = fills[0]
        # To approximate before-state, replay fills up to but excluding the first cycle fill.
        long_qty = None
        short_qty = None
        for ev in fill_log:
            if ev is first:
                break
            l_after = _safe_float(ev.get("long_qty_after"))
            s_after = _safe_float(ev.get("short_qty_after"))
            if l_after is not None:
                long_qty = l_after
            if s_after is not None:
                short_qty = s_after

        long_before = long_qty
        short_before = short_qty

        # Extract long/short reduce fills.
        long_fill = next(
            (
                ev
                for ev in fills
                if "LONG_ADD" in str(ev.get("purpose") or ev.get("purpose_original") or "")
            ),
            None,
        )
        short_fill = next(
            (
                ev
                for ev in fills
                if "SHORT_REDUCE" in str(ev.get("purpose") or ev.get("purpose_original") or "")
            ),
            None,
        )

        long_purpose = (
            str(long_fill.get("purpose") or long_fill.get("purpose_original") or "")
            if long_fill
            else None
        )
        short_purpose = (
            str(short_fill.get("purpose") or short_fill.get("purpose_original") or "")
            if short_fill
            else None
        )

        long_candle = int(long_fill.get("candle_index")) if long_fill else None
        short_candle = int(short_fill.get("candle_index")) if short_fill else None

        long_price = _safe_float(long_fill.get("fill_price")) if long_fill else None
        short_price = _safe_float(short_fill.get("fill_price")) if short_fill else None

        long_qty_fill = _safe_float(long_fill.get("qty")) if long_fill else None
        short_qty_fill = _safe_float(short_fill.get("qty")) if short_fill else None

        long_pnl = _safe_float(long_fill.get("closed_pnl")) if long_fill else 0.0
        short_pnl = _safe_float(short_fill.get("closed_pnl")) if short_fill else 0.0

        cycle_net_pnl = (long_pnl or 0.0) + (short_pnl or 0.0)

        # After-cycle position: nehmen wir den letzten Fill der beiden.
        last = short_fill or long_fill or first
        long_after = _safe_float(last.get("long_qty_after"))
        short_after = _safe_float(last.get("short_qty_after"))

        net_after = None
        ratio_after = None
        notional_after = None
        if long_after is not None and short_after is not None:
            net_after = float(long_after) - float(short_after)
            if abs(short_after) > 1e-12:
                ratio_after = float(long_after) / float(short_after)
            price = _safe_float(last.get("fill_price")) or 0.0
            notional_after = (abs(float(long_after)) + abs(float(short_after))) * float(
                price
            )

        cycle_complete = bool(long_fill and short_fill)
        blocked_reason = None
        if not cycle_complete:
            blocked_reason = "short_reduce_missing" if long_fill else "long_reduce_missing"

        rows.append(
            CycleSummaryRow(
                cycle_index=ci,
                long_reduce_purpose=long_purpose,
                long_reduce_candle=long_candle,
                long_reduce_fill_price=long_price,
                long_reduce_qty=long_qty_fill,
                long_reduce_realized_pnl=long_pnl or 0.0,
                short_reduce_purpose=short_purpose,
                short_reduce_candle=short_candle,
                short_reduce_fill_price=short_price,
                short_reduce_qty=short_qty_fill,
                short_reduce_realized_pnl=short_pnl or 0.0,
                cycle_net_pnl=cycle_net_pnl,
                long_qty_before_cycle=long_before,
                short_qty_before_cycle=short_before,
                long_qty_after_cycle=long_after,
                short_qty_after_cycle=short_after,
                net_long_qty_after_cycle=net_after,
                long_short_ratio_after_cycle=ratio_after,
                total_position_notional_after_cycle=notional_after,
                cycle_complete=cycle_complete,
                blocked_reason=blocked_reason,
            )
        )

    return rows


def _write_fill_timeline_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "candle_index",
        "timestamp",
        "purpose",
        "side",
        "fill_price",
        "fill_qty",
        "realized_pnl_this_fill",
        "cumulative_realized_pnl",
        "long_qty_before",
        "long_qty_after",
        "short_qty_before",
        "short_qty_after",
        "net_long_qty_after",
        "long_short_ratio_after",
        "long_avg_after",
        "short_avg_after",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def _write_cycle_summary_csv(path: Path, rows: List[CycleSummaryRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "cycle_index",
        "long_reduce_purpose",
        "long_reduce_candle",
        "long_reduce_fill_price",
        "long_reduce_qty",
        "long_reduce_realized_pnl",
        "short_reduce_purpose",
        "short_reduce_candle",
        "short_reduce_fill_price",
        "short_reduce_qty",
        "short_reduce_realized_pnl",
        "cycle_net_pnl",
        "long_qty_before_cycle",
        "short_qty_before_cycle",
        "long_qty_after_cycle",
        "short_qty_after_cycle",
        "net_long_qty_after_cycle",
        "long_short_ratio_after_cycle",
        "total_position_notional_after_cycle",
        "cycle_complete",
        "blocked_reason",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(r.__dict__)


def _write_markdown_diagnosis(path: Path, result: Dict[str, Any], cycles: List[CycleSummaryRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    fill_log: List[Dict[str, Any]] = list(result.get("fill_log") or [])
    order_log: List[Dict[str, Any]] = list(result.get("order_log") or [])
    summary: Dict[str, Any] = dict(result.get("recovery_summary") or {})

    with path.open("w", encoding="utf-8") as handle:
        handle.write("# Start 4000 – Diagnose ohne Refill und ohne Recovery\n\n")

        handle.write("## Refill-Prüfung\n\n")
        refill_fills = [
            ev
            for ev in fill_log
            if str(ev.get("purpose") or "").startswith("REFILL_")
        ]
        refill_orders = [
            ev
            for ev in order_log
            if str(ev.get("purpose") or "").startswith("REFILL_")
        ]
        handle.write(f"- refill_count (fills) = {len(refill_fills)}\n")
        handle.write(f"- refill_count (orders) = {len(refill_orders)}\n")
        if not refill_fills and not refill_orders:
            handle.write(
                "- **Bestätigung**: keine Refill-Orders und keine Refill-Fills; "
                "kein Positionssprung zurück auf die Ausgangsgröße.\n\n"
            )
        else:
            handle.write(
                "- Achtung: Refill-Ereignisse im Result vorhanden – "
                "Refill wurde nicht vollständig deaktiviert.\n\n"
            )

        handle.write("## Recovery-Prüfung\n\n")
        handle.write(f"- recovery_enabled: false (Backtest ohne recovery_bot_config)\n")
        handle.write(
            f"- recovery_trace Länge: {len(result.get('recovery_trace') or [])}\n"
        )
        handle.write(
            f"- recovery_summary.neutralization_count: {summary.get('neutralization_count')}\n"
        )
        handle.write(
            f"- recovery_summary.pair_reduction_count: {summary.get('pair_reduction_count')}\n\n"
        )

        handle.write("## Cycle-Summary\n\n")
        for r in cycles:
            handle.write(
                f"- Cycle {r.cycle_index}: net_pnl={r.cycle_net_pnl}, "
                f"complete={r.cycle_complete}, "
                f"long_after={r.long_qty_after_cycle}, short_after={r.short_qty_after_cycle}, "
                f"net_long_after={r.net_long_qty_after_cycle}, "
                f"ratio_after={r.long_short_ratio_after_cycle}\n"
            )


def main() -> int:
    baseline = _load_baseline_full()
    window = _load_window_candles(baseline)
    long_cfg_path = _build_long_config_without_refill(baseline)

    # Verwende das gleiche Fill-Modell und denselben Startindex wie im Audit.
    fill_model_name = str(baseline.get("fill_model") or "conservative")
    fill_cfg = resolve_fill_model_config(
        fill_model=fill_model_name, max_fills_per_candle=None
    )

    requested_start_index = int(baseline.get("requested_start_index") or 4000)
    candles_processed = int(baseline.get("candles_processed") or 0)

    result = run_historical_backtest(
        "APTUSDT",
        "long",
        window,
        max_candles=candles_processed,
        fill_model=fill_cfg.fill_model,
        max_fills_per_candle=fill_cfg.max_fills_per_candle,
        config_source="file",
        long_config_path=long_cfg_path,
        short_config_path=DEFAULT_SHORT_CONFIG_PATH,
        file_config_path=long_cfg_path,
        recovery_bot_config=None,
    )
    result.start_index = requested_start_index
    result.window_candles = len(window)

    full_path = OUT_DIR / "APTUSDT_start4000_no_refill_no_recovery_full.json"
    _write_full_result(full_path, result)

    result_dict = result.to_dict()
    _assert_no_refill_markers(result_dict)

    fill_rows = _build_fill_timeline(result_dict)
    fill_csv = OUT_DIR / "APTUSDT_start4000_no_refill_no_recovery_fill_timeline.csv"
    _write_fill_timeline_csv(fill_csv, fill_rows)

    cycle_rows = _build_cycle_summary(result_dict)
    cycle_csv = OUT_DIR / "APTUSDT_start4000_no_refill_no_recovery_cycle_summary.csv"
    _write_cycle_summary_csv(cycle_csv, cycle_rows)

    md_path = OUT_DIR / "APTUSDT_start4000_no_refill_no_recovery_diagnosis.md"
    _write_markdown_diagnosis(md_path, result_dict, cycle_rows)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

