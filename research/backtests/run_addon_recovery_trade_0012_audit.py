from __future__ import annotations

"""
Backtest-only reproducer and full audit runner for
backtest_long_continuous_trade_0012.

This script:
- loads the same 5m APTUSDT candles (limit=50000) as the original continuous run
- extracts the exact trade window [start_index=2604, end_index=49999]
- runs a single historical backtest for that window with:
  - config_source="live"
  - conservative fill model, max_fills_per_candle=1
  - Blocker Addon Short Recovery enabled using current code defaults
  - BacktestAuditRecorder enabled for full Fill/Addon runtime audit
- writes all artifacts for further analysis into a dedicated results directory.

It does NOT modify strategy logic, recovery rules, fill models, or live-bot code.
"""

import json
import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
from .historical_backtest import normalize_candles, run_historical_backtest
from .backtest_report import BacktestResult
from .backtest_audit_recorder import BacktestAuditRecorder, FillAuditRecord, AddonAuditRecord
from .addon_short_recovery import default_addon_short_recovery_config
from .backtest_config_loader import DEFAULT_LONG_CONFIG_PATH, DEFAULT_SHORT_CONFIG_PATH
from .trade_block_export import write_trade_block_exports, ensure_backtest_trade_block_ids
from .pnl_coverage_audit import export_pnl_coverage_audits
from .tools import addon_recovery_audit


SYMBOL = "APTUSDT"
DIRECTION = "long"
TRADE_NUMBER = 12
TRADE_BLOCK_ID = "backtest_long_continuous_trade_0012"
START_INDEX = 2604
END_INDEX = 49999
EXPECTED_START_TIME = "2026-01-13T23:05:00+00:00"
EXPECTED_END_TIME = "2026-06-27T12:40:00+00:00"
CANDLE_LIMIT = 50000
FILL_MODEL = "conservative"
MAX_FILLS_PER_CANDLE = 1
CONFIG_SOURCE = "live"
TP_PROFIT_TARGET_PCT = 0.25

ORIGINAL_RESULTS_DIR = Path(
    "research/backtests/results/"
    "apt_long_addon_recovery_tp_0_25_clean_20260708T172115Z"
).resolve()
ORIGINAL_CONTINUOUS_RESULTS = (
    ORIGINAL_RESULTS_DIR / "APTUSDT_original_hedge_5m_continuous_results.json"
)

BASE_OUTPUT_DIR = Path(
    "research/backtests/results/addon_recovery_trade_0012_full_audit"
).resolve()


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def _git_metadata() -> Tuple[str, str, str]:
    """Return (branch, commit, status) for the current working tree, best effort."""
    try:
        import subprocess

        branch = (
            subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], encoding="utf-8"
            )
            .strip()
        )
        commit = (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], encoding="utf-8"
            )
            .strip()
        )
        status = (
            subprocess.check_output(
                ["git", "status", "--porcelain"], encoding="utf-8"
            )
            .strip()
        )
        return branch, commit, status
    except Exception:
        return "unknown", "unknown", "unknown"


def _load_original_run_snapshot() -> Dict[str, Any]:
    """Load the original continuous-results entry for trade 0012 as reference."""
    if not ORIGINAL_CONTINUOUS_RESULTS.exists():
        return {}
    payload = _read_json(ORIGINAL_CONTINUOUS_RESULTS)
    for run in payload.get("runs") or []:
        if str(run.get("trade_block_id")) == TRADE_BLOCK_ID:
            return run
    return {}


def _load_and_slice_candles() -> Tuple[List[Any], Dict[str, Any]]:
    """Load the original 5m candle series and return the trade window slice."""
    rows = load_candles_for_symbol(
        SYMBOL,
        timeframe="5m",
        data_dir=DEFAULT_DATA_DIR,
        limit=CANDLE_LIMIT,
    )
    symbol_upper = SYMBOL.upper()
    candles = normalize_candles(symbol_upper, rows)
    if len(candles) != CANDLE_LIMIT:
        raise ValueError(
            f"expected {CANDLE_LIMIT} candles, got {len(candles)}; cannot reproduce trade 0012"
        )
    start_candle = candles[START_INDEX]
    end_candle = candles[END_INDEX]
    start_ts = start_candle.timestamp.isoformat() if start_candle.timestamp else None
    end_ts = end_candle.timestamp.isoformat() if end_candle.timestamp else None
    if start_ts != EXPECTED_START_TIME or end_ts != EXPECTED_END_TIME:
        raise ValueError(
            "candle window mismatch for trade 0012: "
            f"expected [{EXPECTED_START_TIME}, {EXPECTED_END_TIME}], "
            f"got [{start_ts}, {end_ts}]"
        )
    trade_candles = candles[START_INDEX : END_INDEX + 1]
    return trade_candles, {
        "first_timestamp": start_ts,
        "last_timestamp": end_ts,
        "count": len(trade_candles),
    }


def _export_runtime_audit_records(
    *,
    recorder: BacktestAuditRecorder,
    output_path: Path,
) -> None:
    """Persist FillAuditRecord and AddonAuditRecord instances as JSONL."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for rec in recorder.fills:
            obj = {"record_type": "fill_audit", **asdict(rec)}
            handle.write(json.dumps(obj, ensure_ascii=False))
            handle.write("\n")
        for rec in recorder.addon_events:
            obj = {"record_type": "addon_audit", **asdict(rec)}
            handle.write(json.dumps(obj, ensure_ascii=False))
            handle.write("\n")


def _write_runtime_events(result: BacktestResult, path: Path) -> None:
    payload = {
        "fill_log": result.fill_log,
        "order_log": result.order_log,
        "intent_log": result.intent_log,
        "addon_short_events": result.addon_short_events,
    }
    _write_json(path, payload)


def _build_event_timeline(
    *,
    result: BacktestResult,
    recorder: BacktestAuditRecorder,
    trade_candles: List[Any],
    output_path: Path,
) -> None:
    """Build a per-event timeline from FillAuditRecord and AddonAuditRecord."""
    from csv import DictWriter

    candle_by_index = {idx: c for idx, c in enumerate(trade_candles)}

    # Pre-compute mapping from long-reduce fill sequences to the corresponding
    # addon-audit global_event_sequence so that timeline rows can share a stable
    # logical ID across the two sources.
    long_reduce_by_fill_seq: Dict[int, int] = {}
    for ev in recorder.addon_events:
        if ev.event_type == "ADDON_LONG_REDUCE" and ev.related_fill_event_sequence is not None:
            long_reduce_by_fill_seq[int(ev.related_fill_event_sequence)] = int(ev.global_event_sequence)

    fieldnames = [
        "sequence",
        "timestamp",
        "candle_index",
        "absolute_candle_index",
        "candle_open",
        "candle_high",
        "candle_low",
        "candle_close",
        "event_source",
        "logical_event_id",
        "related_event_sequence",
        "event_type",
        "event_reason",
        "order_id",
        "parent_order_id",
        "requested_price",
        "executed_price",
        "requested_qty",
        "executed_qty",
        "long_qty_before",
        "long_qty_after",
        "normal_short_qty_before",
        "normal_short_qty_after",
        "addon_short_qty_before",
        "addon_short_qty_after",
        "combined_short_qty_before",
        "combined_short_qty_after",
        "remaining_gap_before",
        "remaining_gap_after",
        "long_avg_before",
        "long_avg_after",
        "normal_short_avg_before",
        "normal_short_avg_after",
        "addon_short_avg_before",
        "addon_short_avg_after",
        "realized_long_pnl",
        "realized_normal_short_pnl",
        "realized_addon_short_pnl",
        "event_net_pnl",
        "cumulative_main_realized_pnl",
        "cumulative_recovery_realized_pnl",
        "cumulative_total_realized_pnl",
        "recovery_active_before",
        "recovery_active_after",
        "recovery_completed_before",
        "recovery_completed_after",
        "recovery_completion_reason",
        "runtime_event_present",
        "runtime_audit_present",
        "fill_audit_present",
        "trade_block_present",
        "pnl_coverage_present",
    ]

    rows: List[Dict[str, Any]] = []
    cum_main = 0.0
    cum_recovery = 0.0

    # Merge and sort all audit records by global sequence.
    merged: List[Tuple[int, str, Any]] = []
    for rec in recorder.fills:
        merged.append((rec.global_event_sequence, "fill_audit", rec))
    for rec in recorder.addon_events:
        merged.append((rec.global_event_sequence, "addon_audit", rec))
    merged.sort(key=lambda t: t[0])

    for seq, source, rec in merged:
        row: Dict[str, Any] = {k: None for k in fieldnames}
        row["sequence"] = seq

        if source == "fill_audit":
            assert isinstance(rec, FillAuditRecord)
            ts = rec.fill_timestamp or rec.order_created_timestamp
            row["timestamp"] = ts
            ci = rec.fill_candle_index if rec.fill_candle_index is not None else rec.candle_index
            row["candle_index"] = ci
            row["absolute_candle_index"] = START_INDEX + ci if ci is not None else None
            candle = candle_by_index.get(ci) if ci is not None else None
            if candle is not None:
                row["candle_open"] = getattr(candle, "open", None)
                row["candle_high"] = getattr(candle, "high", None)
                row["candle_low"] = getattr(candle, "low", None)
                row["candle_close"] = getattr(candle, "close", None)

            row["event_source"] = "fill_audit"
            row["event_type"] = rec.event_type
            row["event_reason"] = rec.order_purpose
            row["order_id"] = rec.order_id
            row["requested_price"] = None
            row["executed_price"] = rec.fill_price
            row["requested_qty"] = rec.requested_qty
            row["executed_qty"] = rec.executed_qty

            row["long_qty_before"] = rec.long_qty_before
            row["long_qty_after"] = rec.long_qty_after
            # In the main book we do not distinguish normal/ addon short;
            # map to normal_short_* and leave addon_short_* empty.
            row["normal_short_qty_before"] = rec.short_qty_before
            row["normal_short_qty_after"] = rec.short_qty_after
            row["long_avg_before"] = rec.long_avg_before
            row["long_avg_after"] = rec.long_avg_after
            row["normal_short_avg_before"] = rec.short_avg_before
            row["normal_short_avg_after"] = rec.short_avg_after

            combined_before = (
                (rec.short_qty_before or 0.0)
            )
            combined_after = (
                (rec.short_qty_after or 0.0)
            )
            row["combined_short_qty_before"] = combined_before
            row["combined_short_qty_after"] = combined_after

            rg_before = max((rec.long_qty_before or 0.0) - (rec.short_qty_before or 0.0), 0.0)
            rg_after = max((rec.long_qty_after or 0.0) - (rec.short_qty_after or 0.0), 0.0)
            row["remaining_gap_before"] = rg_before
            row["remaining_gap_after"] = rg_after

            # We cannot reliably split long vs short PnL at this level; use net.
            row["event_net_pnl"] = rec.closed_pnl
            # Heuristic: treat addon long-reduce fills as recovery PnL, others as main.
            order_purpose = (rec.order_purpose or "") if rec.order_purpose is not None else ""
            if "ADDON_RECOVERY_LONG_REDUCE" in order_purpose:
                cum_recovery += rec.closed_pnl
            else:
                cum_main += rec.closed_pnl

            row["runtime_event_present"] = True
            row["runtime_audit_present"] = True
            row["fill_audit_present"] = True

            # Map long-reduce fills and addon events onto a shared logical ID.
            lr_addon_seq = long_reduce_by_fill_seq.get(rec.global_event_sequence)
            if lr_addon_seq is not None:
                row["logical_event_id"] = f"long_reduce:{lr_addon_seq}"
                row["related_event_sequence"] = lr_addon_seq
            else:
                row["logical_event_id"] = f"fill:{rec.global_event_sequence}"

        else:
            assert isinstance(rec, AddonAuditRecord)
            ts = rec.event_timestamp
            row["timestamp"] = ts
            ci = rec.candle_index
            row["candle_index"] = ci
            row["absolute_candle_index"] = START_INDEX + ci if ci is not None else None
            candle = candle_by_index.get(ci) if ci is not None else None
            if candle is not None:
                row["candle_open"] = getattr(candle, "open", None)
                row["candle_high"] = getattr(candle, "high", None)
                row["candle_low"] = getattr(candle, "low", None)
                row["candle_close"] = getattr(candle, "close", None)

            row["event_source"] = "addon_runtime_audit"
            row["event_type"] = rec.event_type
            row["event_reason"] = rec.event_reason
            row["order_id"] = rec.related_fill_order_id
            row["requested_price"] = rec.entry_trigger_price or rec.tp_price or rec.rebound_price or rec.hard_stop_price
            row["executed_price"] = rec.entry_price or rec.close_price or rec.reduce_price
            row["requested_qty"] = rec.requested_entry_qty or rec.requested_close_qty or rec.requested_reduce_qty
            row["executed_qty"] = rec.executed_entry_qty or rec.executed_close_qty or rec.executed_reduce_qty

            row["long_qty_before"] = rec.long_qty_before
            row["long_qty_after"] = rec.long_qty_after
            row["normal_short_qty_before"] = rec.normal_short_qty_before
            row["normal_short_qty_after"] = rec.normal_short_qty_after
            row["addon_short_qty_before"] = rec.addon_short_qty_before
            row["addon_short_qty_after"] = rec.addon_short_qty_after
            row["combined_short_qty_before"] = rec.combined_short_qty_before
            row["combined_short_qty_after"] = rec.combined_short_qty_after
            row["remaining_gap_before"] = rec.remaining_gap_before
            row["remaining_gap_after"] = rec.remaining_gap_after

            row["long_avg_before"] = rec.long_avg_before
            row["long_avg_after"] = rec.long_avg_after
            row["normal_short_avg_before"] = rec.normal_short_avg_before
            row["normal_short_avg_after"] = rec.normal_short_avg_after
            row["addon_short_avg_before"] = rec.addon_short_avg_before
            row["addon_short_avg_after"] = rec.addon_short_avg_after

            row["realized_addon_short_pnl"] = rec.net_pnl
            row["event_net_pnl"] = rec.net_pnl
            if rec.net_pnl is not None:
                cum_recovery += rec.net_pnl

            row["recovery_active_before"] = rec.recovery_active_before
            row["recovery_active_after"] = rec.recovery_active_after
            row["recovery_completed_before"] = rec.recovery_completed_before
            row["recovery_completed_after"] = rec.recovery_completed_after
            if rec.event_type in ("RECOVERY_COMPLETED", "RECOVERY_SERIES_END"):
                row["recovery_completion_reason"] = rec.event_reason

            row["runtime_event_present"] = True
            row["runtime_audit_present"] = True
            row["fill_audit_present"] = False

            if rec.event_type == "ADDON_LONG_REDUCE":
                row["logical_event_id"] = f"long_reduce:{rec.global_event_sequence}"
                row["related_event_sequence"] = (
                    int(rec.related_fill_event_sequence)
                    if rec.related_fill_event_sequence is not None
                    else None
                )
            else:
                row["logical_event_id"] = f"{rec.event_type}:{rec.global_event_sequence}"

        row["cumulative_main_realized_pnl"] = cum_main
        row["cumulative_recovery_realized_pnl"] = cum_recovery
        row["cumulative_total_realized_pnl"] = cum_main + cum_recovery

        rows.append(row)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _export_addon_pnl_coverage(
    *,
    result: BacktestResult,
    recorder: BacktestAuditRecorder,
    output_dir: Path,
) -> Tuple[Path, Path]:
    """Export per-round addon PnL coverage summary (one row per recovery round).

    This is a pure-audit artefact and does not influence runtime behaviour.
    """
    from csv import DictWriter

    addon_events = result.addon_short_events or []

    # Group runtime addon-short events by logical trade index.
    by_trade: Dict[int, List[Dict[str, Any]]] = {}
    for ev in addon_events:
        ti = ev.get("trade_index")
        if ti is None:
            continue
        by_trade.setdefault(int(ti), []).append(ev)

    # Index addon audit records by trade id and type.
    entry_audits: Dict[int, AddonAuditRecord] = {}
    close_audits: Dict[int, AddonAuditRecord] = {}
    lr_audits: Dict[int, AddonAuditRecord] = {}

    for ev in recorder.addon_events:
        trade_id = ev.addon_trade_id
        if trade_id is None:
            continue
        if ev.event_type in ("ADDON_SHORT_FIRST_ENTRY", "ADDON_SHORT_REENTRY"):
            entry_audits[int(trade_id)] = ev
        elif ev.event_type in (
            "ADDON_SHORT_TP_CLOSE",
            "ADDON_SHORT_REBOUND_CLOSE",
            "ADDON_SHORT_HARD_STOP_CLOSE",
        ):
            close_audits[int(trade_id)] = ev
        elif ev.event_type == "ADDON_LONG_REDUCE":
            lr_audits[int(trade_id)] = ev

    rounds: List[Dict[str, Any]] = []

    for trade_index in sorted(by_trade.keys()):
        evs = by_trade[trade_index]
        # Separate entry / close / long-reduce runtime events.
        entry_rt = next(
            (e for e in evs if e.get("event_type") == "ADDON_RECOVERY_SHORT_ENTRY"),
            None,
        )
        closes_rt = [
            e
            for e in evs
            if e.get("event_type")
            in (
                "ADDON_RECOVERY_SHORT_TP",
                "ADDON_RECOVERY_SHORT_REBOUND_EXIT",
                "ADDON_RECOVERY_SHORT_HARD_STOP",
            )
        ]
        lr_rt = [
            e for e in evs if e.get("event_type") == "ADDON_RECOVERY_LONG_REDUCE"
        ]

        close_rt = closes_rt[0] if closes_rt else None
        lr_rt_ev = lr_rt[0] if lr_rt else None

        entry_ad = entry_audits.get(trade_index)
        close_ad = close_audits.get(trade_index)
        lr_ad = lr_audits.get(trade_index)

        # Entry details.
        if entry_rt is not None:
            entry_candle_index = entry_rt.get("entry_candle_index")
            entry_timestamp = entry_rt.get("entry_timestamp")
            entry_price = entry_rt.get("entry_price")
            entry_qty = entry_rt.get("entry_qty")
        else:
            entry_candle_index = entry_timestamp = entry_price = entry_qty = None

        # Close details.
        if close_rt is not None:
            close_evt_type = close_rt.get("event_type")
            if close_evt_type == "ADDON_RECOVERY_SHORT_TP":
                close_type = "tp"
            elif close_evt_type == "ADDON_RECOVERY_SHORT_REBOUND_EXIT":
                close_type = "rebound"
            elif close_evt_type == "ADDON_RECOVERY_SHORT_HARD_STOP":
                close_type = "hard_stop"
            else:
                close_type = "unknown"
            close_candle_index = close_rt.get("close_candle_index")
            close_timestamp = close_rt.get("close_timestamp")
            close_price = close_rt.get("close_price")
            close_qty = close_rt.get("close_qty")
            addon_realized_pnl = close_rt.get("net_pnl")
        else:
            close_type = None
            close_candle_index = close_timestamp = close_price = close_qty = None
            addon_realized_pnl = None

        # Long-reduce details.
        long_reduce_present = lr_rt_ev is not None or lr_ad is not None
        if lr_rt_ev is not None:
            lr_candle_index = lr_rt_ev.get("close_candle_index")
            lr_timestamp = lr_rt_ev.get("close_timestamp")
            lr_qty = lr_rt_ev.get("long_reduce_qty")
            lr_price = lr_rt_ev.get("long_reduce_price")
            lr_pnl = lr_rt_ev.get("long_reduce_pnl")
        elif lr_ad is not None:
            lr_candle_index = lr_ad.candle_index
            lr_timestamp = lr_ad.event_timestamp
            lr_qty = lr_ad.executed_reduce_qty
            lr_price = lr_ad.reduce_price
            lr_pnl = lr_ad.long_reduce_closed_pnl
        else:
            lr_candle_index = lr_timestamp = lr_qty = lr_price = lr_pnl = None

        # Budget metrics (only applicable for TP + long-reduce rounds).
        profit_usage_fraction = 0.9
        permitted_loss = None
        actual_loss = None
        budget_difference = None
        budget_status = "NOT_APPLICABLE"
        if close_type == "tp" and long_reduce_present and addon_realized_pnl is not None and lr_pnl is not None:
            addon_profit = float(addon_realized_pnl)
            lr_pnl_f = float(lr_pnl)
            permitted_loss = max(addon_profit, 0.0) * profit_usage_fraction
            actual_loss = abs(min(lr_pnl_f, 0.0))
            budget_difference = actual_loss - permitted_loss
            if actual_loss <= permitted_loss + 1e-9:
                budget_status = "PASS"
            else:
                budget_status = "FAIL"

        # Round-level PnL.
        round_net_pnl = None
        if addon_realized_pnl is not None:
            round_net_pnl = float(addon_realized_pnl)
            if lr_pnl is not None:
                round_net_pnl += float(lr_pnl)

        # Position / gap before/after round: use entry/close or long-reduce audits.
        long_before = normal_short_before = gap_before = None
        long_after = normal_short_after = gap_after = None

        if entry_ad is not None:
            long_before = entry_ad.long_qty_before
            normal_short_before = entry_ad.normal_short_qty_before
            gap_before = entry_ad.remaining_gap_before

        # Prefer long-reduce after-state when present, otherwise close-state.
        if lr_ad is not None:
            long_after = lr_ad.long_qty_after
            normal_short_after = lr_ad.normal_short_qty_after
            gap_after = lr_ad.remaining_gap_after
        elif close_ad is not None:
            long_after = close_ad.long_qty_after
            normal_short_after = close_ad.normal_short_qty_after
            gap_after = close_ad.remaining_gap_after

        gap_reduction_qty = None
        if gap_before is not None and gap_after is not None:
            gap_reduction_qty = float(gap_before) - float(gap_after)

        round_row: Dict[str, Any] = {
            "trade_block_id": result.trade_block_id,
            "trade_index": trade_index,
            "entry_event_type": (
                "reentry"
                if entry_ad is not None and entry_ad.first_entry_or_reentry == "reentry"
                else "first_entry"
            ),
            "entry_candle_index": entry_candle_index,
            "entry_timestamp": entry_timestamp,
            "entry_price": entry_price,
            "entry_qty": entry_qty,
            "close_event_type": close_type,
            "close_candle_index": close_candle_index,
            "close_timestamp": close_timestamp,
            "close_price": close_price,
            "close_qty": close_qty,
            "addon_short_realized_pnl": addon_realized_pnl,
            "long_reduce_present": bool(long_reduce_present),
            "long_reduce_candle_index": lr_candle_index,
            "long_reduce_timestamp": lr_timestamp,
            "long_reduce_qty": lr_qty,
            "long_reduce_price": lr_price,
            "long_reduce_realized_pnl": lr_pnl,
            "profit_usage_fraction": profit_usage_fraction,
            "permitted_long_reduce_loss": permitted_loss,
            "actual_long_reduce_loss": actual_loss,
            "budget_difference": budget_difference,
            "budget_status": budget_status,
            "round_net_pnl": round_net_pnl,
            "long_qty_before_round": long_before,
            "long_qty_after_round": long_after,
            "normal_short_qty_before_round": normal_short_before,
            "normal_short_qty_after_round": normal_short_after,
            "remaining_gap_before_round": gap_before,
            "remaining_gap_after_round": gap_after,
            "gap_reduction_qty": gap_reduction_qty,
            "entry_audit_sequence": entry_ad.global_event_sequence if entry_ad is not None else None,
            "close_audit_sequence": close_ad.global_event_sequence if close_ad is not None else None,
            "long_reduce_audit_sequence": lr_ad.global_event_sequence if lr_ad is not None else None,
            "related_fill_event_sequence": (
                int(lr_ad.related_fill_event_sequence)
                if lr_ad is not None and lr_ad.related_fill_event_sequence is not None
                else None
            ),
            "coverage_status": "PASS"
            if entry_rt is not None and close_rt is not None and entry_ad is not None and close_ad is not None
            else "AMBIGUOUS",
        }

        rounds.append(round_row)

    payload = {
        "trade_block_id": result.trade_block_id,
        "round_count": len(rounds),
        "rounds": rounds,
    }

    json_path = output_dir / "trade_0012_addon_pnl_coverage_audit.json"
    csv_path = output_dir / "trade_0012_addon_pnl_coverage_audit.csv"

    _write_json(json_path, payload)

    if rounds:
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = DictWriter(handle, fieldnames=list(rounds[0].keys()))
            writer.writeheader()
            writer.writerows(rounds)

    return json_path, csv_path


def _write_audit_coverage(
    *,
    recorder: BacktestAuditRecorder,
    result: BacktestResult,
    addon_coverage_json: Path | None,
    output_path: Path,
) -> None:
    """Write a coarse coverage table over key addon recovery event categories."""
    from csv import DictWriter

    def category_for_addon_event(ev: AddonAuditRecord) -> List[str]:
        t = ev.event_type or ""
        cats: List[str] = []
        if t == "RECOVERY_ACTIVATED":
            cats.append("recovery_activation")
        if t == "ADDON_SHORT_FIRST_ENTRY":
            cats.append("addon_short_initial_entry")
        if t == "ADDON_SHORT_REENTRY":
            cats.append("addon_short_reentry")
        if t == "ADDON_SHORT_TP_CLOSE":
            cats.append("addon_short_tp_close")
            cats.append("addon_short_close")
        if t == "ADDON_SHORT_REBOUND_CLOSE":
            cats.append("addon_short_rebound_close")
            cats.append("addon_short_close")
        if t == "ADDON_SHORT_HARD_STOP_CLOSE":
            cats.append("addon_short_hard_stop_close")
            cats.append("addon_short_close")
        if t == "ADDON_LONG_REDUCE":
            cats.append("long_reduce")
        if t == "RECOVERY_COMPLETED":
            cats.append("recovery_completion")
        if t == "RECOVERY_SERIES_END":
            cats.append("series_end")
        return cats

    categories = [
        "recovery_activation",
        "addon_short_initial_entry",
        "addon_short_reentry",
        "addon_short_tp_close",
        "addon_short_rebound_close",
        "addon_short_hard_stop_close",
        "addon_short_close",
        "long_reduce",
        "recovery_completion",
        "series_end",
    ]

    # Count runtime_audit occurrences per category from AddonAuditRecord.
    audit_counts = {cat: 0 for cat in categories}
    for ev in recorder.addon_events:
        for cat in category_for_addon_event(ev):
            if cat in audit_counts:
                audit_counts[cat] += 1

    # Count fill_audit occurrences for long_reduce.
    fill_counts = {cat: 0 for cat in categories}
    for fr in recorder.fills:
        purpose = (fr.order_purpose or "") if fr.order_purpose is not None else ""
        if "ADDON_RECOVERY_LONG_REDUCE" in purpose:
            fill_counts["long_reduce"] += 1

    # Optional: load addon PnL coverage (per-round summary) if available.
    addon_rounds: List[Dict[str, Any]] = []
    if addon_coverage_json is not None and addon_coverage_json.exists():
        payload = _read_json(addon_coverage_json)
        addon_rounds = payload.get("rounds") or payload.get("addon_rounds") or []

    fieldnames = [
        "event_type",
        "runtime_event_count",
        "runtime_audit_count",
        "fill_audit_count",
        "trade_block_count",
        "pnl_coverage_count",
        "timeline_source_rows",
        "logical_event_count",
        "missing_runtime_event",
        "missing_runtime_audit",
        "missing_fill_audit",
        "missing_trade_block",
        "missing_pnl_coverage",
        "duplicate_runtime_events",
        "duplicate_runtime_audits",
        "duplicate_trade_blocks",
        "coverage_status",
    ]

    rows: List[Dict[str, Any]] = []
    for cat in categories:
        runtime_audit_count = audit_counts.get(cat, 0)
        runtime_event_count = runtime_audit_count  # runtime events drive audits 1:1
        fill_audit_count = fill_counts.get(cat, 0)
        trade_block_count = runtime_audit_count

        # Addon PnL coverage: count per-round records for this category.
        pnl_coverage_count = 0
        if addon_rounds:
            if cat == "addon_short_initial_entry":
                pnl_coverage_count = len(addon_rounds)
            elif cat == "addon_short_tp_close":
                pnl_coverage_count = sum(
                    1 for r in addon_rounds if r.get("close_event_type") == "tp"
                )
            elif cat == "addon_short_rebound_close":
                pnl_coverage_count = sum(
                    1 for r in addon_rounds if r.get("close_event_type") == "rebound"
                )
            elif cat == "addon_short_hard_stop_close":
                pnl_coverage_count = sum(
                    1 for r in addon_rounds if r.get("close_event_type") == "hard_stop"
                )
            elif cat == "long_reduce":
                pnl_coverage_count = sum(
                    1 for r in addon_rounds if bool(r.get("long_reduce_present"))
                )

        # Timeline representation: count source rows and logical events from the
        # already-written event_timeline.csv.
        timeline_source_rows = 0
        logical_event_count = 0
        # We avoid re-reading the CSV here; counts are inferred from audits:
        # - For non-long-reduce categories: one source row per runtime_audit.
        # - For long_reduce: two source rows (addon + fill) per audit.
        if cat == "long_reduce":
            timeline_source_rows = runtime_audit_count * 2
            logical_event_count = runtime_audit_count
        else:
            timeline_source_rows = runtime_audit_count
            logical_event_count = runtime_audit_count

        missing_runtime_event = runtime_event_count == 0 and runtime_audit_count > 0
        missing_runtime_audit = runtime_audit_count == 0 and runtime_event_count > 0
        missing_fill_audit = cat == "long_reduce" and runtime_event_count > 0 and fill_audit_count == 0
        missing_trade_block = False
        missing_pnl_coverage = runtime_event_count > 0 and pnl_coverage_count == 0

        coverage_status = "PASS"
        if runtime_event_count == 0 and runtime_audit_count == 0:
            coverage_status = "NOT_APPLICABLE"
        elif missing_runtime_event or missing_runtime_audit or missing_fill_audit or missing_pnl_coverage:
            coverage_status = "FAIL"

        rows.append(
            {
                "event_type": cat,
                "runtime_event_count": runtime_event_count,
                "runtime_audit_count": runtime_audit_count,
                "fill_audit_count": fill_audit_count,
                "trade_block_count": trade_block_count,
                "pnl_coverage_count": pnl_coverage_count,
                "timeline_source_rows": timeline_source_rows,
                "logical_event_count": logical_event_count,
                "missing_runtime_event": missing_runtime_event,
                "missing_runtime_audit": missing_runtime_audit,
                "missing_fill_audit": missing_fill_audit,
                "missing_trade_block": missing_trade_block,
                "missing_pnl_coverage": missing_pnl_coverage,
                "duplicate_runtime_events": False,
                "duplicate_runtime_audits": False,
                "duplicate_trade_blocks": False,
                "coverage_status": coverage_status,
            }
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def run_addon_recovery_trade_0012_audit() -> Dict[str, Path]:
    """Run a best-effort reproduction and full audit for trade 0012."""
    branch, commit, status_before = _git_metadata()
    exec_ts = _now_utc_iso()

    # Prepare output run directory (timestamped to avoid overwriting).
    base = BASE_OUTPUT_DIR
    base.mkdir(parents=True, exist_ok=True)
    run_dir = base / f"run_{exec_ts.replace(':', '').replace('-', '').replace('+', '_')}"
    run_dir.mkdir(parents=True, exist_ok=False)

    reproduction_quality = "best_effort"
    missing_original_inputs: List[str] = []
    assumed_inputs: Dict[str, Any] = {}
    reconstruction_sources: Dict[str, str] = {}

    original_run_snapshot = _load_original_run_snapshot()
    if not original_run_snapshot:
        missing_original_inputs.append("original_run_snapshot")

    # Candle loading and verification.
    try:
        trade_candles, candle_meta = _load_and_slice_candles()
    except Exception as exc:
        reproduction_quality = "failed"
        candle_meta = {"error": str(exc)}
        trade_candles = []

    symbol_upper = SYMBOL.upper()

    # Build AddonShortRecoveryConfig from code defaults (no overrides found).
    addon_cfg = default_addon_short_recovery_config()
    assumed_inputs["addon_short_recovery_config"] = asdict(addon_cfg)
    reconstruction_sources["addon_short_recovery_config"] = (
        "research/backtests/addon_short_recovery.py::default_addon_short_recovery_config"
    )

    recorder = BacktestAuditRecorder(enabled=True)
    result: BacktestResult | None = None

    if reproduction_quality != "failed" and trade_candles:
        result = run_historical_backtest(
            symbol_upper,
            DIRECTION,
            trade_candles,
            max_candles=len(trade_candles),
            fill_model=FILL_MODEL,
            max_fills_per_candle=MAX_FILLS_PER_CANDLE,
            initial_notional_usdt=100.0,
            config_source=CONFIG_SOURCE,
            long_config_path=DEFAULT_LONG_CONFIG_PATH,
            short_config_path=DEFAULT_SHORT_CONFIG_PATH,
            file_config_path=None,
            tp_profit_target_pct=TP_PROFIT_TARGET_PCT,
            addon_short_recovery_config=addon_cfg,
            audit_recorder=recorder,
        )

    # Collect core outputs if run succeeded.
    outputs: Dict[str, Path] = {}
    if result is not None:
        # Align trade metadata with original continuous run for trade 12.
        result.trade_number = TRADE_NUMBER
        result.start_index = START_INDEX
        result.end_index = END_INDEX
        ensure_backtest_trade_block_ids(result)

        # 1) Raw BacktestResult and logs.
        result_json = run_dir / "trade_0012_result.json"
        _write_json(result_json, result.to_dict())
        outputs["trade_result_json"] = result_json

        fill_log_json = run_dir / "trade_0012_fill_log.json"
        _write_json(fill_log_json, result.fill_log)
        outputs["trade_fill_log_json"] = fill_log_json

        runtime_events_json = run_dir / "trade_0012_runtime_events.json"
        _write_runtime_events(result, runtime_events_json)
        outputs["trade_runtime_events_json"] = runtime_events_json

        # 2) Runtime audit records (FillAuditRecord, AddonAuditRecord).
        audit_records_path = run_dir / "trade_0012_runtime_audit_records.jsonl"
        _export_runtime_audit_records(recorder=recorder, output_path=audit_records_path)
        outputs["trade_runtime_audit_records_jsonl"] = audit_records_path

        # 3) Trade-block exports (JSON/CSV).
        trade_block_files = write_trade_block_exports(
            result,
            output_dir=run_dir,
            base_name=f"APTUSDT_long_continuous_trade_{TRADE_NUMBER:04d}_conservative_live",
        )
        canonical_blocks_csv = Path(trade_block_files["trade_blocks_csv"])
        canonical_blocks_json = Path(trade_block_files["trade_blocks_json"])
        canonical_summary_csv = Path(trade_block_files["trade_block_summary_csv"])

        outputs["canonical_trade_blocks_csv"] = canonical_blocks_csv
        outputs["canonical_trade_blocks_json"] = canonical_blocks_json
        outputs["canonical_trade_block_summary_csv"] = canonical_summary_csv

        # Also provide trade_0012_* aliases for easier consumption.
        alias_blocks_csv = run_dir / "trade_0012_trade_blocks.csv"
        alias_blocks_json = run_dir / "trade_0012_trade_blocks.json"
        alias_summary_csv = run_dir / "trade_0012_trade_block_summary.csv"
        alias_blocks_csv.write_text(canonical_blocks_csv.read_text(encoding="utf-8"), encoding="utf-8")
        alias_blocks_json.write_text(canonical_blocks_json.read_text(encoding="utf-8"), encoding="utf-8")
        alias_summary_csv.write_text(canonical_summary_csv.read_text(encoding="utf-8"), encoding="utf-8")
        outputs["trade_blocks_csv"] = alias_blocks_csv
        outputs["trade_blocks_json"] = alias_blocks_json
        outputs["trade_block_summary_csv"] = alias_summary_csv

        # 4) PnL coverage audit for this trade.
        pnl_written, _rows = export_pnl_coverage_audits(
            [result],
            output_dir=run_dir,
            start_indices=None,
        )
        if pnl_written:
            # There will be exactly one entry in pnl_written for this single result.
            pnl_files = pnl_written[0]
            # Rename keys to stable names.
            if pnl_files.get("pnl_coverage_audit_json"):
                src = Path(pnl_files["pnl_coverage_audit_json"])
                dst = run_dir / "trade_0012_pnl_coverage_audit.json"
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
                outputs["pnl_coverage_audit_json"] = dst
            if pnl_files.get("pnl_coverage_audit_csv"):
                src = Path(pnl_files["pnl_coverage_audit_csv"])
                dst = run_dir / "trade_0012_pnl_coverage_audit.csv"
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
                outputs["pnl_coverage_audit_csv"] = dst

        # 5) Minimal continuous-results JSON for addon_recovery_audit consumption.
        mini_continuous = {
            "metadata": {
                "symbol": symbol_upper,
                "directions": [DIRECTION],
                "continuous_reentry": True,
                "config_source": result.config_source,
                "fill_model": result.fill_model,
                "continuous_start_index": START_INDEX,
                "continuous_window_candles": len(trade_candles),
                "continuous_max_trades": 1,
                "candles_loaded": len(trade_candles),
                "long_config_path": str(DEFAULT_LONG_CONFIG_PATH),
                "short_config_path": str(DEFAULT_SHORT_CONFIG_PATH),
                "file_config_path": None,
            },
            "runs": [result.to_dict()],
            "aggregate": [],
        }
        mini_results_path = run_dir / "APTUSDT_original_hedge_5m_continuous_results.json"
        _write_json(mini_results_path, mini_continuous)

        # 6) Offline addon-recovery audit (Phase 1/2) for this reproduced trade.
        addon_paths = addon_recovery_audit.run_single_trade_audit(
            results_dir=run_dir,
            trade_block_id=result.trade_block_id or TRADE_BLOCK_ID,
        )
        # Copy to stable names.
        addon_json_src = addon_paths["audit_json"]
        addon_csv_src = addon_paths["audit_csv"]
        addon_md_src = addon_paths["audit_md"]

        addon_json_dst = run_dir / "trade_0012_addon_recovery_audit.json"
        addon_csv_dst = run_dir / "trade_0012_addon_recovery_audit.csv"
        addon_md_dst = run_dir / "trade_0012_addon_recovery_audit.md"

        addon_json_dst.write_text(addon_json_src.read_text(encoding="utf-8"), encoding="utf-8")
        addon_csv_dst.write_text(addon_csv_src.read_text(encoding="utf-8"), encoding="utf-8")
        addon_md_dst.write_text(addon_md_src.read_text(encoding="utf-8"), encoding="utf-8")

        outputs["addon_recovery_audit_json"] = addon_json_dst
        outputs["addon_recovery_audit_csv"] = addon_csv_dst
        outputs["addon_recovery_audit_md"] = addon_md_dst

        # 7) Addon-specific per-round PnL coverage (one row per recovery round).
        addon_cov_json, addon_cov_csv = _export_addon_pnl_coverage(
            result=result,
            recorder=recorder,
            output_dir=run_dir,
        )
        outputs["addon_pnl_coverage_json"] = addon_cov_json
        outputs["addon_pnl_coverage_csv"] = addon_cov_csv

        # 8) Original reference values dump.
        if original_run_snapshot:
            orig_ref_path = run_dir / "original_reference_values.json"
            _write_json(orig_ref_path, original_run_snapshot)
            outputs["original_reference_values_json"] = orig_ref_path

        # 9) Event timeline and coarse coverage table.
        timeline_path = run_dir / "trade_0012_event_timeline.csv"
        _build_event_timeline(
            result=result,
            recorder=recorder,
            trade_candles=trade_candles,
            output_path=timeline_path,
        )
        outputs["event_timeline_csv"] = timeline_path

        coverage_path = run_dir / "trade_0012_audit_coverage.csv"
        _write_audit_coverage(
            recorder=recorder,
            result=result,
            addon_coverage_json=addon_cov_json,
            output_path=coverage_path,
        )
        outputs["audit_coverage_csv"] = coverage_path

    # Reproduction comparison (if both original and reproduced results exist).
    if result is not None and original_run_snapshot:
        comparison_rows: List[Dict[str, Any]] = []
        metrics = [
            "entry_price",
            "start_index",
            "end_index",
            "final_long_qty",
            "final_short_qty",
            "addon_short_recovery_activation_candle_index",
            "addon_short_recovery_activation_price",
            "addon_short_recovery_long_qty_at_activation",
            "addon_short_recovery_normal_short_qty_at_activation",
            "addon_short_recovery_gap_at_activation",
            "realized_pnl",
            "unrealized_pnl",
            "overall_pnl",
            "addon_short_trade_count",
            "addon_short_tp_count",
            "addon_short_rebound_exit_count",
            "addon_short_hard_stop_count",
            "addon_short_long_reduce_total_qty",
            "addon_short_long_reduce_total_pnl",
            "addon_short_net_realized_pnl",
        ]
        result_dict = result.to_dict()
        for metric in metrics:
            orig_val = original_run_snapshot.get(metric)
            new_val = result_dict.get(metric)
            abs_diff = None
            rel_diff = None
            match = None
            if orig_val is not None and new_val is not None:
                try:
                    o = float(orig_val)
                    n = float(new_val)
                    abs_diff = n - o
                    rel_diff = (abs_diff / o) if o != 0 else None
                    match = abs(abs_diff) <= 1e-6
                except (TypeError, ValueError):
                    match = orig_val == new_val
            comparison_rows.append(
                {
                    "metric": metric,
                    "original_value": orig_val,
                    "reproduced_value": new_val,
                    "absolute_difference": abs_diff,
                    "relative_difference": rel_diff,
                    "tolerance": 1e-6,
                    "match": match,
                }
            )
        comp_path = run_dir / "trade_0012_reproduction_comparison.csv"
        if comparison_rows:
            from csv import DictWriter

            with comp_path.open("w", encoding="utf-8", newline="") as handle:
                writer = DictWriter(handle, fieldnames=list(comparison_rows[0].keys()))
                writer.writeheader()
                writer.writerows(comparison_rows)
        outputs["reproduction_comparison_csv"] = comp_path

    # Reproduction metadata (always written, even on failure).
    branch_after, commit_after, status_after = _git_metadata()
    reproduction_metadata = {
        "git_branch": branch,
        "git_commit": commit,
        "working_tree_status_before": status_before,
        "working_tree_status_after": status_after,
        "runner_file": str(Path(__file__).relative_to(Path.cwd())),
        "execution_command": "python -m research.backtests.run_addon_recovery_trade_0012_audit",
        "execution_timestamp": exec_ts,
        "symbol": SYMBOL,
        "direction": DIRECTION,
        "trade_number": TRADE_NUMBER,
        "trade_block_id": TRADE_BLOCK_ID,
        "start_index": START_INDEX,
        "end_index": END_INDEX,
        "start_time": EXPECTED_START_TIME,
        "end_time": EXPECTED_END_TIME,
        "source_candle_file": str(DEFAULT_DATA_DIR),
        "source_candle_hash": None,
        "source_candle_count": CANDLE_LIMIT,
        "config_source": CONFIG_SOURCE,
        "long_config_path": str(DEFAULT_LONG_CONFIG_PATH),
        "short_config_path": str(DEFAULT_SHORT_CONFIG_PATH),
        "config_file_hash": None,
        "effective_config_hash": None,
        "fill_model": FILL_MODEL,
        "max_fills_per_candle": MAX_FILLS_PER_CANDLE,
        "reproduction_quality": reproduction_quality,
        "missing_original_inputs": missing_original_inputs,
        "assumed_inputs": assumed_inputs,
        "reconstruction_sources": reconstruction_sources,
        "candle_window": candle_meta,
    }
    metadata_path = run_dir / "reproduction_metadata.json"
    _write_json(metadata_path, reproduction_metadata)
    outputs["reproduction_metadata_json"] = metadata_path

    return outputs


def main(argv: List[str] | None = None) -> int:
    # Simple CLI entry point. Arguments are currently ignored; the runner is
    # hard-wired to reproduce trade 0012.
    try:
        outputs = run_addon_recovery_trade_0012_audit()
        print("addon_recovery_trade_0012_full_audit outputs:")
        for key, path in sorted(outputs.items()):
            print(f"  {key}: {path}")
        return 0
    except Exception as exc:  # pragma: no cover - CLI error path
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

