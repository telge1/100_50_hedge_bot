"""Independent trust and consistency audit for the original hedge backtester."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from fixed_cycle_hedge_bot.math_utils import calculate_pnl

from .backtest_audit_recorder import BacktestAuditRecorder, FillAuditRecord
from .backtest_report import BacktestResult, resolve_net_closed_pnl
from .candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol
from .historical_backtest import normalize_candles, run_historical_backtest
from .simulated_execution import evaluate_order_touch, resolve_simulated_fee_rate
from .simulated_order_book import SyntheticCandle, VirtualOrder
from .simulated_pnl import calculate_simulated_closed_pnl

QTY_TOLERANCE = 1e-6
AVG_TOLERANCE = 1e-8
PNL_TOLERANCE = 1e-4
DEFAULT_FEE_RATE = resolve_simulated_fee_rate()


def _safe_float(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _resolve_export_fee_rate(row: dict[str, Any]) -> float | None:
    fee = _safe_float(row.get("fee_rate"))
    if fee is not None:
        return fee
    if row.get("entry_fee") not in (None, "") or row.get("exit_fee") not in (None, ""):
        return resolve_simulated_fee_rate()
    return resolve_simulated_fee_rate()

FINDING_CLASSES = frozenset(
    {
        "audit_export_error",
        "backtest_integration_error",
        "strategy_logic_error",
        "fill_model_error",
        "rounding_only",
    }
)


@dataclass
class AuditFinding:
    check_id: str
    trade_block_id: str
    severity: str
    classification: str
    message: str
    expected: Any = None
    actual: Any = None
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TradeAuditSummary:
    trade_block_id: str
    direction: str
    start_index: int
    end_index: int | None
    realized_pnl: float | None
    fills_count: int
    checks_passed: int
    checks_failed: int
    findings: list[AuditFinding] = field(default_factory=list)
    forward_rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def trusted(self) -> bool:
        return self.checks_failed == 0


@dataclass
class IndependentLedger:
    """Standalone position ledger — mirrors book math without strategy imports."""

    long_qty: float = 0.0
    short_qty: float = 0.0
    long_avg: float = 0.0
    short_avg: float = 0.0
    realized_pnl: float = 0.0
    total_entry_fees: float = 0.0
    total_exit_fees: float = 0.0

    def apply_fill(
        self,
        *,
        side: str,
        qty: float,
        fill_price: float,
        reduce_only: bool,
        fee_rate: float | None = DEFAULT_FEE_RATE,
    ) -> dict[str, float | None]:
        side_norm = str(side).lower()
        close_qty = float(qty)
        avg_for_pnl = 0.0

        if side_norm == "long":
            if reduce_only:
                close_qty = min(float(qty), self.long_qty)
                avg_for_pnl = self.long_avg
                self.long_qty = max(0.0, self.long_qty - close_qty)
                if self.long_qty <= QTY_TOLERANCE:
                    self.long_qty = 0.0
                    self.long_avg = 0.0
            else:
                prev_qty = self.long_qty
                new_qty = prev_qty + float(qty)
                if new_qty > 0:
                    self.long_avg = (
                        (prev_qty * self.long_avg + float(qty) * float(fill_price)) / new_qty
                        if prev_qty > 0
                        else float(fill_price)
                    )
                self.long_qty = new_qty
        elif side_norm == "short":
            if reduce_only:
                close_qty = min(float(qty), self.short_qty)
                avg_for_pnl = self.short_avg
                self.short_qty = max(0.0, self.short_qty - close_qty)
                if self.short_qty <= QTY_TOLERANCE:
                    self.short_qty = 0.0
                    self.short_avg = 0.0
            else:
                prev_qty = self.short_qty
                new_qty = prev_qty + float(qty)
                if new_qty > 0:
                    self.short_avg = (
                        (prev_qty * self.short_avg + float(qty) * float(fill_price)) / new_qty
                        if prev_qty > 0
                        else float(fill_price)
                    )
                self.short_qty = new_qty
        else:
            raise ValueError(f"unsupported side: {side}")

        net_pnl, details = calculate_simulated_closed_pnl(
            side=side_norm,
            avg_entry_price=float(avg_for_pnl),
            fill_price=float(fill_price),
            qty=float(close_qty if reduce_only else qty),
            reduce_only=bool(reduce_only),
            fee_rate=fee_rate,
        )
        self.realized_pnl += float(net_pnl)
        if details.get("entry_fee") is not None:
            self.total_entry_fees += float(details["entry_fee"])
        if details.get("exit_fee") is not None:
            self.total_exit_fees += float(details["exit_fee"])
        return {
            "net_pnl": float(net_pnl),
            "gross_pnl": float(details.get("gross_pnl") or 0.0),
            "entry_fee": details.get("entry_fee"),
            "exit_fee": details.get("exit_fee"),
            "executed_qty": float(close_qty if reduce_only else qty),
        }


def _row_candle_index(row: dict[str, Any]) -> int:
    value = row.get("candle_index")
    if value in (None, ""):
        return -1
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def _load_trade_block_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        return list(payload.get("trade_blocks") or [])
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows.append(dict(row))
    return rows


def _find_trade_block_file(output_dir: Path, result: dict[str, Any]) -> Path | None:
    trade_block_id = str(result.get("trade_block_id") or "")
    trade_number = result.get("trade_number")
    symbol = str(result.get("symbol") or "APTUSDT").upper()
    direction = str(result.get("direction") or "long")
    fill_model = str(result.get("fill_model") or "conservative")
    config_source = str(result.get("config_source") or "live")

    patterns: list[str] = []
    if trade_number is not None:
        patterns.append(f"{symbol}_{direction}_continuous_trade_{int(trade_number):04d}_*_trade_blocks.json")
    if trade_block_id:
        slug = trade_block_id.replace("backtest_", "")
        patterns.append(f"{symbol}_{slug}_*_trade_blocks.json")
    patterns.append(f"{symbol}_{direction}_*_{fill_model}_{config_source}_trade_blocks.json")

    for pattern in patterns:
        matches = sorted(output_dir.glob(pattern))
        if matches:
            return matches[0]
    return None


def _order_lifecycle_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lifecycle: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("row_type") != "order":
            continue
        order_id = str(row.get("order_id") or "").strip()
        if not order_id:
            continue
        entry = lifecycle.setdefault(
            order_id,
            {
                "submitted_candle": None,
                "filled_candle": None,
                "cancelled_candle": None,
                "status": None,
                "purpose": row.get("purpose"),
                "side": row.get("side"),
                "reduce_only": row.get("reduce_only"),
                "qty": row.get("qty"),
                "price": row.get("price"),
                "trigger_price": row.get("trigger_price"),
                "trigger_direction": row.get("trigger_direction"),
            },
        )
        event_type = str(row.get("event_type") or row.get("status") or "").lower()
        candle = _row_candle_index(row)
        if event_type == "submitted" and entry["submitted_candle"] is None:
            entry["submitted_candle"] = candle
            for key in ("reduce_only", "qty", "price", "trigger_price", "trigger_direction", "side"):
                if row.get(key) not in (None, ""):
                    entry[key] = row.get(key)
        elif event_type == "filled":
            entry["filled_candle"] = candle
            entry["status"] = "FILLED"
            for key in ("reduce_only", "qty", "price", "trigger_price", "trigger_direction", "side", "fill_price"):
                if row.get(key) not in (None, ""):
                    entry[key] = row.get(key)
        elif event_type in {"cancelled", "canceled"}:
            entry["cancelled_candle"] = candle
            entry["status"] = "CANCELED"
    return lifecycle


def _virtual_order_from_lifecycle(entry: dict[str, Any], order_id: str) -> VirtualOrder:
    return VirtualOrder(
        order_id=order_id,
        exchange_order_id=f"audit-{order_id}",
        symbol="AUDIT",
        side=str(entry.get("side") or "long"),
        qty=float(_safe_float(entry.get("qty"), 0.0) or 0.0),
        price=_safe_float(entry.get("price")),
        trigger_price=_safe_float(entry.get("trigger_price")),
        trigger_direction=entry.get("trigger_direction"),
        order_type="Limit",
        reduce_only=str(entry.get("reduce_only")).lower() in {"true", "1", "yes"}
        if entry.get("reduce_only") not in (None, "")
        else False,
        purpose=str(entry.get("purpose") or ""),
        status=str(entry.get("status") or "NEW"),
    )


def _resolve_absolute_candle_index(
    row: dict[str, Any],
    *,
    start_index: int,
    input_slice_start_index: int = 0,
) -> int | None:
    local = _row_candle_index(row)
    if local >= 0:
        return int(input_slice_start_index) + int(start_index) + local
    for key in ("global_candle_index", "absolute_candle_index", "slice_candle_index"):
        value = row.get(key)
        if value not in (None, ""):
            try:
                return int(value)
            except (TypeError, ValueError):
                pass
    return None


def _candle_at(candles: list[SyntheticCandle], absolute_index: int | None) -> SyntheticCandle | None:
    if absolute_index is None or absolute_index < 0 or absolute_index >= len(candles):
        return None
    return candles[absolute_index]


def audit_trade_blocks(
    *,
    rows: list[dict[str, Any]],
    result: dict[str, Any],
    candles: list[SyntheticCandle],
    fill_audit_records: list[FillAuditRecord] | None = None,
) -> TradeAuditSummary:
    trade_block_id = str(rows[0].get("trade_block_id") if rows else result.get("trade_block_id") or "")
    direction = str(result.get("direction") or rows[0].get("direction") if rows else "long")
    start_index = int(result.get("start_index") or 0)
    input_slice_start_index = int(result.get("input_slice_start_index") or 0)
    end_index = result.get("end_index")
    if end_index is not None:
        end_index = int(end_index)

    summary = TradeAuditSummary(
        trade_block_id=trade_block_id,
        direction=direction,
        start_index=start_index,
        end_index=end_index,
        realized_pnl=_safe_float(result.get("realized_pnl")),
        fills_count=int(result.get("fills_count") or 0),
        checks_passed=0,
        checks_failed=0,
    )

    def record(
        check_id: str,
        passed: bool,
        *,
        classification: str = "backtest_integration_error",
        severity: str = "error",
        message: str = "",
        expected: Any = None,
        actual: Any = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        if passed:
            summary.checks_passed += 1
            return
        summary.checks_failed += 1
        summary.findings.append(
            AuditFinding(
                check_id=check_id,
                trade_block_id=trade_block_id,
                severity=severity,
                classification=classification,
                message=message,
                expected=expected,
                actual=actual,
                context=context or {},
            )
        )

    fill_rows = _sort_fill_rows([row for row in rows if row.get("row_type") == "fill"])
    order_rows = [row for row in rows if row.get("row_type") == "order"]
    intent_rows = [row for row in rows if row.get("row_type") == "intent"]

    record(
        "trade_block_has_rows",
        bool(rows),
        classification="audit_export_error",
        message="trade block export is empty",
    )
    record(
        "fill_count_matches_result",
        len(fill_rows) == int(result.get("fills_count") or len(fill_rows)),
        classification="audit_export_error",
        message="fill row count does not match result.fills_count",
        expected=int(result.get("fills_count") or 0),
        actual=len(fill_rows),
    )

    lifecycle = _order_lifecycle_map(rows)
    ledger = IndependentLedger()
    forward_rows: list[dict[str, Any]] = []
    cumulative_from_fills = 0.0

    long_add = long_reduce = long_exit = 0.0
    short_add = short_reduce = short_exit = 0.0

    idx = 0
    candle_groups: dict[int, list[dict[str, Any]]] = {}
    for row in fill_rows:
        candle_groups.setdefault(_row_candle_index(row), []).append(row)

    for candle_idx in sorted(candle_groups):
        group = candle_groups[candle_idx]
        group_independent_pnl = 0.0
        group_reported_pnl = 0.0
        for row in group:
            side = str(row.get("side") or "").lower()
            qty = _safe_float(row.get("qty"), 0.0) or 0.0
            fill_price = _safe_float(row.get("fill_price"), 0.0) or 0.0
            purpose = str(row.get("purpose") or "")
            reduce_only = _resolve_reduce_only(row, lifecycle)
            fee_rate = _resolve_export_fee_rate(row)

            fill_pnl = resolve_net_closed_pnl(row) or 0.0
            cumulative_from_fills += float(fill_pnl)
            group_reported_pnl += float(fill_pnl)

            details = ledger.apply_fill(
                side=side,
                qty=qty,
                fill_price=fill_price,
                reduce_only=reduce_only,
                fee_rate=fee_rate,
            )
            group_independent_pnl += float(details["net_pnl"] or 0.0)

            pnl_delta = abs(float(details["net_pnl"] or 0.0) - float(fill_pnl))
            if len(group) == 1 or not _is_exit_purpose(purpose):
                record(
                    f"forward_fill_pnl_{idx}",
                    pnl_delta <= PNL_TOLERANCE,
                    classification="rounding_only" if pnl_delta <= PNL_TOLERANCE else "backtest_integration_error",
                    message=f"independent PnL mismatch on fill {purpose}",
                    expected=details["net_pnl"],
                    actual=fill_pnl,
                    context={"purpose": purpose, "candle_index": row.get("candle_index")},
                )

            if side == "long":
                if reduce_only:
                    long_reduce += float(details["executed_qty"] or 0.0)
                else:
                    long_add += float(details["executed_qty"] or 0.0)
            elif side == "short":
                if reduce_only:
                    short_reduce += float(details["executed_qty"] or 0.0)
                else:
                    short_add += float(details["executed_qty"] or 0.0)

            if "EXIT" in purpose.upper():
                if side == "long" and reduce_only:
                    long_exit += float(details["executed_qty"] or 0.0)
                if side == "short" and reduce_only:
                    short_exit += float(details["executed_qty"] or 0.0)

            forward_rows.append(
                {
                    "seq": idx + 1,
                    "candle_index": row.get("candle_index"),
                    "purpose": purpose,
                    "side": side,
                    "qty": qty,
                    "fill_price": fill_price,
                    "reduce_only": reduce_only,
                    "closed_pnl": fill_pnl,
                    "independent_pnl": details["net_pnl"],
                    "long_qty": ledger.long_qty,
                    "short_qty": ledger.short_qty,
                    "long_avg": ledger.long_avg,
                    "short_avg": ledger.short_avg,
                    "gap": max(ledger.long_qty - ledger.short_qty, 0.0),
                    "cumulative_realized_pnl": ledger.realized_pnl,
                }
            )

            order_id = str(row.get("order_id") or "").strip()
            if order_id and order_id in lifecycle:
                lc = lifecycle[order_id]
                fill_candle = _row_candle_index(row)
                submitted = lc.get("submitted_candle")
                cancelled = lc.get("cancelled_candle")
                record(
                    f"lifecycle_fill_after_submit_{order_id}",
                    submitted is None or fill_candle >= submitted,
                    classification="fill_model_error",
                    message=f"fill before order submission for {purpose}",
                    expected=f">= {submitted}",
                    actual=fill_candle,
                )
                record(
                    f"lifecycle_no_fill_after_cancel_{order_id}",
                    cancelled is None or fill_candle <= cancelled,
                    classification="fill_model_error",
                    message=f"fill after cancel for {purpose}",
                    expected=f"cancel at {cancelled}",
                    actual=fill_candle,
                )

            abs_idx_int = _resolve_absolute_candle_index(
                row,
                start_index=start_index,
                input_slice_start_index=input_slice_start_index,
            )
            candle = _candle_at(candles, abs_idx_int)
            if candle is not None and order_id and order_id in lifecycle:
                touch = evaluate_order_touch(_virtual_order_from_lifecycle(lifecycle[order_id], order_id), candle)
                record(
                    f"candle_touch_{order_id}",
                    touch.touched,
                    classification="fill_model_error",
                    message=f"candle does not touch order for {purpose}",
                    expected=touch.trigger_touch_rule,
                    actual={
                        "high": candle.high,
                        "low": candle.low,
                        "trigger": lifecycle[order_id].get("trigger_price") or lifecycle[order_id].get("price"),
                    },
                )
            idx += 1

        last_row = group[-1]
        reported_long = _safe_float(last_row.get("long_qty_after"))
        reported_short = _safe_float(last_row.get("short_qty_after"))
        reported_long_avg = _safe_float(last_row.get("long_avg_after"))
        reported_short_avg = _safe_float(last_row.get("short_avg_after"))
        if reported_long is not None:
            record(
                f"forward_long_qty_candle_{candle_idx}",
                abs(reported_long - ledger.long_qty) <= QTY_TOLERANCE,
                classification="audit_export_error"
                if len(group) > 1
                else "backtest_integration_error",
                message=f"long qty after candle {candle_idx} fill group",
                expected=ledger.long_qty,
                actual=reported_long,
            )
        if reported_short is not None:
            record(
                f"forward_short_qty_candle_{candle_idx}",
                abs(reported_short - ledger.short_qty) <= QTY_TOLERANCE,
                classification="audit_export_error"
                if len(group) > 1
                else "backtest_integration_error",
                message=f"short qty after candle {candle_idx} fill group",
                expected=ledger.short_qty,
                actual=reported_short,
            )
        if reported_long_avg is not None and ledger.long_qty > 0:
            record(
                f"forward_long_avg_candle_{candle_idx}",
                abs(reported_long_avg - ledger.long_avg) <= AVG_TOLERANCE,
                classification="rounding_only"
                if abs(reported_long_avg - ledger.long_avg) <= AVG_TOLERANCE
                else "backtest_integration_error",
                message=f"long avg after candle {candle_idx}",
                expected=ledger.long_avg,
                actual=reported_long_avg,
            )
        if reported_short_avg is not None and ledger.short_qty > 0:
            record(
                f"forward_short_avg_candle_{candle_idx}",
                abs(reported_short_avg - ledger.short_avg) <= AVG_TOLERANCE,
                classification="rounding_only"
                if abs(reported_short_avg - ledger.short_avg) <= AVG_TOLERANCE
                else "backtest_integration_error",
                message=f"short avg after candle {candle_idx}",
                expected=ledger.short_avg,
                actual=reported_short_avg,
            )

        if len(group) > 1 and all(_is_exit_purpose(str(r.get("purpose") or "")) for r in group):
            record(
                f"paired_exit_pnl_candle_{candle_idx}",
                abs(group_independent_pnl - group_reported_pnl) <= PNL_TOLERANCE,
                classification="rounding_only"
                if abs(group_independent_pnl - group_reported_pnl) <= PNL_TOLERANCE
                else "backtest_integration_error",
                message=f"paired exit bundle PnL candle {candle_idx}",
                expected=group_independent_pnl,
                actual=group_reported_pnl,
            )

    summary.forward_rows = forward_rows

    final_long = _safe_float(result.get("final_long_qty"), ledger.long_qty) or ledger.long_qty
    final_short = _safe_float(result.get("final_short_qty"), ledger.short_qty) or ledger.short_qty
    record(
        "backward_final_long_qty",
        abs(final_long - ledger.long_qty) <= QTY_TOLERANCE,
        message="final long qty reconciliation",
        expected=ledger.long_qty,
        actual=final_long,
    )
    record(
        "backward_final_short_qty",
        abs(final_short - ledger.short_qty) <= QTY_TOLERANCE,
        message="final short qty reconciliation",
        expected=ledger.short_qty,
        actual=final_short,
    )

    reported_realized = _safe_float(result.get("realized_pnl"))
    if reported_realized is not None:
        record(
            "backward_fill_pnl_sum",
            abs(cumulative_from_fills - reported_realized) <= PNL_TOLERANCE,
            classification="rounding_only"
            if abs(cumulative_from_fills - reported_realized) <= PNL_TOLERANCE
            else "audit_export_error",
            message="exported fill PnL sum vs result realized_pnl",
            expected=reported_realized,
            actual=cumulative_from_fills,
        )
        ledger_gap = abs(reported_realized - ledger.realized_pnl)
        record(
            "backward_independent_ledger_vs_reported",
            ledger_gap <= PNL_TOLERANCE,
            classification="fill_model_error"
            if ledger_gap > PNL_TOLERANCE
            else "rounding_only",
            message="independent per-leg ledger sum vs reported realized_pnl (paired exits may diverge)",
            expected=reported_realized,
            actual=ledger.realized_pnl,
        )

    if fill_audit_records:
        for rec in fill_audit_records:
            if rec.long_qty_after is not None:
                matching = [
                    row
                    for row in fill_rows
                    if str(row.get("order_id") or "") == rec.order_id
                ]
                if matching:
                    row = matching[0]
                    record(
                        f"fill_audit_qty_match_{rec.order_id}",
                        abs(float(rec.long_qty_after) - float(_safe_float(row.get("long_qty_after"), 0.0) or 0.0))
                        <= QTY_TOLERANCE,
                        classification="audit_export_error",
                        message="fill audit recorder vs trade block long_qty_after",
                    )

    record(
        "intent_before_order",
        _intents_precede_orders(intent_rows, order_rows),
        classification="audit_export_error",
        message="some orders appear before their intent in export ordering",
    )

    # Cycle sequence sanity: first leg before second leg when both present
    cycle_first = [r for r in fill_rows if re.search(r"CYCLE_\d+_(LONG_ADD|SHORT_REDUCE)$", str(r.get("purpose") or ""))]
    cycle_second = [r for r in fill_rows if re.search(r"CYCLE_\d+_(LONG_REDUCE|SHORT_ADD)$", str(r.get("purpose") or ""))]
    if cycle_first and cycle_second:
        first_idx = min(_row_candle_index(r) for r in cycle_first)
        second_idx = min(_row_candle_index(r) for r in cycle_second if _row_candle_index(r) >= 0)
        record(
            "cycle_first_leg_before_second",
            first_idx <= second_idx,
            classification="strategy_logic_error",
            message="cycle second leg filled before first leg",
            expected=f"first <= {second_idx}",
            actual=first_idx,
        )

    return summary


FILL_PURPOSE_PRIORITY = {
    "INITIAL_LONG_ENTRY": 10,
    "INITIAL_SHORT_ENTRY": 20,
}


def _fill_purpose_priority(purpose: str) -> int:
    upper = str(purpose or "").upper()
    if upper in FILL_PURPOSE_PRIORITY:
        return FILL_PURPOSE_PRIORITY[upper]
    if "EXIT" in upper:
        return 900
    match = re.search(r"CYCLE_(\d+)_(LONG_ADD|SHORT_REDUCE)", upper)
    if match:
        return 100 + int(match.group(1)) * 10
    match = re.search(r"CYCLE_(\d+)_(LONG_REDUCE|SHORT_ADD)", upper)
    if match:
        return 200 + int(match.group(1)) * 10
    return 500


def _sort_fill_rows(fill_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        fill_rows,
        key=lambda row: (
            _row_candle_index(row),
            _fill_purpose_priority(str(row.get("purpose") or "")),
            str(row.get("order_id") or ""),
        ),
    )


def _resolve_reduce_only(
    row: dict[str, Any],
    lifecycle: dict[str, dict[str, Any]],
) -> bool:
    raw = row.get("reduce_only")
    if raw not in (None, ""):
        return str(raw).lower() in {"true", "1", "yes"}
    order_id = str(row.get("order_id") or "").strip()
    if order_id and order_id in lifecycle and lifecycle[order_id].get("reduce_only") not in (None, ""):
        return str(lifecycle[order_id]["reduce_only"]).lower() in {"true", "1", "yes"}
    purpose = str(row.get("purpose") or "")
    side = str(row.get("side") or "").lower()
    return _infer_reduce_only(purpose, side)


def _is_exit_purpose(purpose: str) -> bool:
    return "EXIT" in str(purpose or "").upper()
def _infer_reduce_only(purpose: str, side: str) -> bool:
    upper = str(purpose or "").upper()
    if "EXIT" in upper:
        return True
    if upper.endswith("_LONG_REDUCE") or upper.endswith("_SHORT_REDUCE"):
        return True
    if upper.endswith("_LONG_ADD") or upper.endswith("_SHORT_ADD"):
        return False
    return False


def _intents_precede_orders(intent_rows: list[dict[str, Any]], order_rows: list[dict[str, Any]]) -> bool:
    intent_purposes = {str(r.get("purpose") or "") for r in intent_rows}
    for order in order_rows:
        if str(order.get("event_type") or "").lower() != "submitted":
            continue
        purpose = str(order.get("purpose") or "")
        if purpose not in intent_purposes:
            continue
        intent_candle = min(
            (_row_candle_index(r) for r in intent_rows if str(r.get("purpose") or "") == purpose),
            default=-1,
        )
        order_candle = _row_candle_index(order)
        if intent_candle >= 0 and order_candle >= 0 and intent_candle > order_candle:
            return False
    return True


def export_fill_audit_records(path: Path, recorder: BacktestAuditRecorder) -> None:
    payload = {
        "fills": [asdict(record) for record in recorder.fills],
        "fill_count": len(recorder.fills),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def result_fingerprint(result: dict[str, Any]) -> str:
    payload = {
        "realized_pnl": result.get("realized_pnl"),
        "fills_count": result.get("fills_count"),
        "final_long_qty": result.get("final_long_qty"),
        "final_short_qty": result.get("final_short_qty"),
        "final_status": result.get("final_status"),
        "exit_reason": result.get("exit_reason"),
    }
    encoded = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def run_determinism_check(
    *,
    symbol: str,
    direction: str,
    candles: list[SyntheticCandle],
    start_index: int,
    window_candles: int,
) -> tuple[bool, str, str]:
    slice_candles = candles[start_index : start_index + window_candles]
    recorder_a = BacktestAuditRecorder(enabled=True)
    recorder_b = BacktestAuditRecorder(enabled=True)
    result_a = run_historical_backtest(
        symbol,
        direction,
        slice_candles,
        fill_model="conservative",
        config_source="live",
        audit_recorder=recorder_a,
        absolute_trade_start_index=start_index,
    )
    result_b = run_historical_backtest(
        symbol,
        direction,
        slice_candles,
        fill_model="conservative",
        config_source="live",
        audit_recorder=recorder_b,
        absolute_trade_start_index=start_index,
    )
    from .multi_start_backtest import compact_result_dict

    fp_a = result_fingerprint(compact_result_dict(result_a))
    fp_b = result_fingerprint(compact_result_dict(result_b))
    return fp_a == fp_b, fp_a, fp_b


def git_head_commit() -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL)
            .strip()
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def archive_existing_results(
    results_root: Path,
    *,
    archive_name: str,
    preserve: Iterable[str] = (),
) -> Path:
    archive_dir = results_root / archive_name
    archive_dir.mkdir(parents=True, exist_ok=True)
    preserve_set = {str(item) for item in preserve} | {archive_name, ".gitignore"}
    for entry in sorted(results_root.iterdir()):
        if entry.name in preserve_set:
            continue
        target = archive_dir / entry.name
        if target.exists():
            continue
        entry.rename(target)
    return archive_dir


def audit_results_directory(
    *,
    output_dir: Path,
    runs: list[dict[str, Any]],
    candles: list[SyntheticCandle],
    fill_audit_dir: Path | None = None,
    start_indices: list[int] | None = None,
    input_slice_start_index: int = 0,
) -> list[TradeAuditSummary]:
    summaries: list[TradeAuditSummary] = []
    for run in runs:
        trade_file = _find_trade_block_file(output_dir, run)
        if trade_file is None:
            summaries.append(
                TradeAuditSummary(
                    trade_block_id=str(run.get("trade_block_id") or "missing"),
                    direction=str(run.get("direction") or ""),
                    start_index=int(run.get("start_index") or 0),
                    end_index=run.get("end_index"),
                    realized_pnl=_safe_float(run.get("realized_pnl")),
                    fills_count=int(run.get("fills_count") or 0),
                    checks_passed=0,
                    checks_failed=1,
                    findings=[
                        AuditFinding(
                            check_id="trade_block_file_missing",
                            trade_block_id=str(run.get("trade_block_id") or ""),
                            severity="error",
                            classification="audit_export_error",
                            message=f"no trade block export for start_index={run.get('start_index')}",
                        )
                    ],
                )
            )
            continue
        rows = _load_trade_block_rows(trade_file)
        fill_records: list[FillAuditRecord] | None = None
        if fill_audit_dir is not None:
            trade_block_id = str(run.get("trade_block_id") or "")
            audit_path = fill_audit_dir / f"{trade_block_id}_fill_audit.json"
            if not audit_path.is_file():
                audit_path = fill_audit_dir / f"{trade_file.stem}_fill_audit.json"
            if audit_path.is_file():
                payload = json.loads(audit_path.read_text(encoding="utf-8"))
                fill_records = [FillAuditRecord(**item) for item in payload.get("fills") or []]
        run_payload = dict(run)
        if run_payload.get("start_index") is None and start_indices:
            trade_number = run_payload.get("trade_number")
            try:
                if trade_number is not None:
                    run_payload["start_index"] = start_indices[int(trade_number) - 1]
            except (TypeError, ValueError, IndexError):
                pass
        if run_payload.get("input_slice_start_index") is None:
            run_payload["input_slice_start_index"] = input_slice_start_index
        summaries.append(
            audit_trade_blocks(
                rows=rows,
                result=run_payload,
                candles=candles,
                fill_audit_records=fill_records,
            )
        )
    return summaries


def build_report_markdown(
    *,
    reproduction_command: str,
    git_commit: str,
    config_summary: dict[str, Any],
    input_summary: dict[str, Any],
    summaries: list[TradeAuditSummary],
    determinism: dict[str, Any],
    archive_path: str | None,
    audit_gaps: list[str],
) -> str:
    determinism_passed = all(
        bool(block.get("passed"))
        for block in determinism.values()
        if isinstance(block, dict)
    )
    trusted = all(summary.trusted for summary in summaries) and determinism_passed
    verdict = "VERTRAUENSWÜRDIG" if trusted else "NICHT VERTRAUENSWÜRDIG"
    lines = [
        "# Backtester Trust Audit",
        "",
        f"**Verdict:** `{verdict}`",
        "",
        "## Reproduction",
        "",
        "```bash",
        reproduction_command,
        "```",
        "",
        f"- Git commit: `{git_commit}`",
        f"- Archive: `{archive_path or 'n/a'}`",
        "",
        "## Input",
        "",
        "```json",
        json.dumps(input_summary, indent=2),
        "```",
        "",
        "## Configuration",
        "",
        "```json",
        json.dumps(config_summary, indent=2),
        "```",
        "",
        "## Determinism",
        "",
        "```json",
        json.dumps(determinism, indent=2),
        "```",
        "",
        "## Audit export gaps",
        "",
    ]
    if audit_gaps:
        for gap in audit_gaps:
            lines.append(f"- {gap}")
    else:
        lines.append("- None identified in this run.")
    lines.extend(["", "## Trade summaries", ""])
    for summary in summaries:
        lines.append(f"### {summary.trade_block_id} ({summary.direction}, start={summary.start_index})")
        lines.append("")
        lines.append(
            f"- Checks passed: **{summary.checks_passed}**, failed: **{summary.checks_failed}**"
        )
        lines.append(f"- Realized PnL: **{summary.realized_pnl}**, fills: **{summary.fills_count}**")
        if summary.findings:
            lines.append("- Findings:")
            for finding in summary.findings:
                lines.append(
                    f"  - `{finding.check_id}` [{finding.classification}]: {finding.message}"
                )
        if summary.forward_rows:
            lines.append("")
            lines.append("| # | candle | purpose | side | qty | price | pnl | long | short | gap |")
            lines.append("|---:|---:|---|---|---:|---:|---:|---:|---:|---:|")
            for row in summary.forward_rows[:30]:
                lines.append(
                    f"| {row['seq']} | {row['candle_index']} | {row['purpose']} | {row['side']} | "
                    f"{row['qty']:.4f} | {row['fill_price']:.6f} | {row['closed_pnl']:.6f} | "
                    f"{row['long_qty']:.4f} | {row['short_qty']:.4f} | {row['gap']:.4f} |"
                )
            if len(summary.forward_rows) > 30:
                lines.append(f"| … | | ({len(summary.forward_rows) - 30} more rows) | | | | | | | |")
        lines.append("")
    return "\n".join(lines)


def default_audit_gaps() -> list[str]:
    return [
        "Trade blocks do not export explicit cancel_reason text (only event_type=cancelled).",
        "Position-before fields are only in fill_audit JSON when audit recorder is enabled.",
        "Unrealized PnL at series end is in result JSON but not per-candle in trade blocks.",
        "Same-candle multi-fill rows use end-of-candle position snapshots in trade blocks (compare per candle group).",
        "Paired basket exits share one exit price; per-leg closed_pnl is independent leg math (not redistributed basket pool).",
        "Strategy internal cycle state (long_add_confirmed_pnl etc.) is not in trade blocks.",
    ]
