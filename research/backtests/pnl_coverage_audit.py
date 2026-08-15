"""Cycle PnL coverage audit for backtest results (Phase 15)."""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .backtest_report import BacktestResult
from .purpose_utils import is_cycle_purpose, preserve_bot_purpose
from .trade_block_export import trade_block_export_base_name

COVER_TOLERANCE = 1e-6

EXIT_QUALITY_CLOSED_OK = "closed_ok"
EXIT_QUALITY_UNDERCOVERED_FINAL = "closed_undercovered_final_exit"
EXIT_QUALITY_NEGATIVE = "closed_negative_pnl"
EXIT_QUALITY_PROFITABLE_CYCLE_UNDER = "closed_profitable_with_cycle_undercoverage"

PNL_COVERAGE_AUDIT_FIELDS = (
    "symbol",
    "direction",
    "start_index",
    "cycle_index",
    "loss_purpose",
    "cover_purpose",
    "loss_cycle_role",
    "cover_cycle_role",
    "loss_pnl",
    "cover_pnl",
    "net_pnl",
    "coverage_ratio",
    "missing_pnl",
    "status",
    "pending_final_active_order_purposes",
    "expected_cover_qty",
    "actual_cover_qty",
    "qty_shortfall",
    "cover_entry_price",
    "cover_fill_price",
    "intent_qty",
    "order_qty",
    "fill_qty",
    "qty_mapping_warning",
    "loss_fill_timestamp",
    "cover_fill_timestamp",
    "loss_fill_price",
    "loss_side",
    "cover_side",
    "coverage_source",
    "final_exit_pool_id",
    "final_exit_pool_net_pnl",
    "final_exit_pool_available_before",
    "final_exit_claimed_pnl",
    "final_exit_pool_remaining_after",
    "cover_fill_ids",
)

EXIT_PURPOSES = frozenset(
    {
        "LONG_TP_EXIT",
        "LONG_SL_EXIT",
        "SHORT_TP_EXIT",
        "SHORT_SL_EXIT",
    }
)


def _safe_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cycle_index(record: dict[str, Any]) -> int | None:
    value = record.get("cycle_index")
    if value is None:
        excerpt = record.get("metadata_excerpt")
        if isinstance(excerpt, dict):
            value = excerpt.get("cycle_index")
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def expected_cover_purpose(loss_purpose: str) -> str:
    purpose = preserve_bot_purpose(loss_purpose)
    match = re.match(r"^CYCLE_(\d+)_(LONG_ADD|LONG_REDUCE|SHORT_REDUCE|SHORT_ADD)$", purpose)
    if not match:
        return ""
    cycle_no, leg = match.group(1), match.group(2)
    if leg in {"LONG_ADD", "LONG_REDUCE"}:
        return f"CYCLE_{cycle_no}_SHORT_REDUCE"
    if leg in {"SHORT_REDUCE", "SHORT_ADD"}:
        return f"CYCLE_{cycle_no}_LONG_REDUCE"
    return ""


def _fill_pnl(fill: dict[str, Any]) -> float:
    return _safe_float(fill.get("closed_pnl")) or 0.0


def _is_loss_fill(fill: dict[str, Any]) -> bool:
    purpose = preserve_bot_purpose(fill.get("purpose"))
    if not is_cycle_purpose(purpose) and purpose not in EXIT_PURPOSES:
        return False
    return _fill_pnl(fill) < -COVER_TOLERANCE


def _is_cover_fill(fill: dict[str, Any]) -> bool:
    return _fill_pnl(fill) > COVER_TOLERANCE


def _fill_log(result: BacktestResult) -> list[dict[str, Any]]:
    return list(getattr(result, "fill_log", None) or [])


def _stable_fill_id(fill: dict[str, Any], index: int) -> str:
    existing = fill.get("fill_id") or fill.get("audit_fill_id")
    if existing:
        return str(existing)
    order_id = fill.get("order_id") or fill.get("exchange_order_id") or ""
    return (
        f"{index}:"
        f"{fill.get('timestamp') or ''}:"
        f"{preserve_bot_purpose(fill.get('purpose'))}:"
        f"{order_id}:"
        f"{fill.get('qty')}:"
        f"{fill.get('closed_pnl')}"
    )


def _annotate_fill_ids(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for index, fill in enumerate(fills):
        item = dict(fill)
        item["_audit_fill_id"] = _stable_fill_id(fill, index)
        item["_audit_fill_index"] = index
        annotated.append(item)
    return annotated


def _coverage_status(
    *,
    loss_pnl: float,
    cover_pnl: float,
    pending_exit_purposes: list[str],
) -> str:
    abs_loss = abs(loss_pnl)
    if cover_pnl <= COVER_TOLERANCE:
        if pending_exit_purposes:
            return "pending_final_exit"
        return "no_cover_fill"
    if cover_pnl >= abs_loss - COVER_TOLERANCE:
        if cover_pnl > abs_loss + COVER_TOLERANCE:
            return "overcovered"
        return "covered"
    if pending_exit_purposes:
        return "pending_final_exit"
    return "undercovered"


def _entry_price_for_cover(
    *,
    loss_fill: dict[str, Any],
    cover_fill: dict[str, Any] | None,
) -> float | None:
    cover_role = str(cover_fill.get("cycle_role") or "").lower() if cover_fill else ""
    cover_side = str(cover_fill.get("side") or "").lower() if cover_fill else ""
    if cover_role == "short_reduce" or cover_side == "short":
        return _safe_float(loss_fill.get("short_avg_after")) or _safe_float(
            loss_fill.get("short_avg_before")
        )
    if cover_role == "long_reduce" or cover_side == "long":
        return _safe_float(loss_fill.get("long_avg_after")) or _safe_float(
            loss_fill.get("long_avg_before")
        )
    return None


def expected_cover_qty(
    *,
    loss_pnl: float,
    entry_price: float | None,
    fill_price: float | None,
) -> float | None:
    if entry_price is None or fill_price is None:
        return None
    spread = abs(entry_price - fill_price)
    if spread <= COVER_TOLERANCE:
        return None
    return abs(loss_pnl) / spread


def inspect_qty_mapping(
    result: BacktestResult,
    *,
    purpose: str,
    cycle_index: int | None,
) -> dict[str, Any]:
    normalized = preserve_bot_purpose(purpose)
    intent_qtys: list[float] = []
    order_qtys: list[float] = []
    fill_qtys: list[float] = []

    for intent in result.intent_log or []:
        if preserve_bot_purpose(intent.get("purpose")) != normalized:
            continue
        if cycle_index is not None and _cycle_index(intent) != cycle_index:
            continue
        qty = _safe_float(intent.get("qty"))
        if qty is not None:
            intent_qtys.append(qty)

    for order in result.order_log or []:
        if preserve_bot_purpose(order.get("purpose")) != normalized:
            continue
        if cycle_index is not None and _cycle_index(order) != cycle_index:
            continue
        if str(order.get("event_type") or "").lower() not in {
            "submitted",
            "filled",
            "replaced",
            "partially_filled",
        }:
            continue
        qty = _safe_float(order.get("qty"))
        if qty is not None:
            order_qtys.append(qty)

    for fill in _fill_log(result):
        if preserve_bot_purpose(fill.get("purpose")) != normalized:
            continue
        if cycle_index is not None and _cycle_index(fill) != cycle_index:
            continue
        qty = _safe_float(fill.get("qty"))
        if qty is not None:
            fill_qtys.append(qty)

    intent_qty = intent_qtys[-1] if intent_qtys else None
    order_qty = order_qtys[-1] if order_qtys else None
    fill_qty = fill_qtys[-1] if fill_qtys else None

    warnings: list[str] = []
    if intent_qty is not None and order_qty is not None and abs(intent_qty - order_qty) > COVER_TOLERANCE:
        warnings.append("intent_qty!=order_qty")
    if order_qty is not None and fill_qty is not None and abs(order_qty - fill_qty) > COVER_TOLERANCE:
        warnings.append("order_qty!=fill_qty")
    if intent_qty is not None and fill_qty is not None and abs(intent_qty - fill_qty) > COVER_TOLERANCE:
        warnings.append("intent_qty!=fill_qty")

    return {
        "intent_qty": intent_qty,
        "order_qty": order_qty,
        "fill_qty": fill_qty,
        "qty_mapping_warning": "|".join(warnings),
    }


def _pending_exit_purposes(result: BacktestResult) -> list[str]:
    return [
        preserve_bot_purpose(purpose)
        for purpose in (result.final_active_order_purposes or [])
        if preserve_bot_purpose(purpose) in EXIT_PURPOSES
    ]


def _select_cover_fills(
    fills: list[dict[str, Any]],
    *,
    cycle_index: int,
    cover_purpose: str,
    loss_cycle_role: str = "",
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    normalized_cover = preserve_bot_purpose(cover_purpose)
    for fill in fills:
        if _cycle_index(fill) != cycle_index:
            continue
        if not _is_cover_fill(fill):
            continue
        purpose = preserve_bot_purpose(fill.get("purpose"))
        if purpose in EXIT_PURPOSES:
            continue
        if normalized_cover and purpose != normalized_cover:
            continue
        selected.append(fill)
    if selected:
        return selected

    expected_role = ""
    if loss_cycle_role == "long_reduce":
        expected_role = "short_reduce"
    elif loss_cycle_role == "short_reduce":
        expected_role = "long_reduce"

    for fill in fills:
        if _cycle_index(fill) != cycle_index:
            continue
        if not _is_cover_fill(fill):
            continue
        purpose = preserve_bot_purpose(fill.get("purpose"))
        if purpose in EXIT_PURPOSES:
            continue
        role = str(fill.get("cycle_role") or "").lower()
        if expected_role and role == expected_role:
            selected.append(fill)
    return selected


def _fill_sort_key(fill: dict[str, Any]) -> tuple[str, int, str]:
    return (
        str(fill.get("timestamp") or ""),
        int(fill.get("_audit_fill_index") or 0),
        str(fill.get("order_id") or ""),
    )


def _select_final_exit_fills_after_loss(
    fills: list[dict[str, Any]],
    *,
    loss_fill: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return final exit fills after a cycle loss fill.

    Some strategy paths do not cover a cycle loss with another cycle purpose.
    Instead the bot recalculates final exit orders so the remaining hedge basket
    closes flat with profit. In that case LONG_*_EXIT / SHORT_*_EXIT are the
    economic cover for the cycle loss.
    """
    loss_ts = str(loss_fill.get("timestamp") or "")
    selected: list[dict[str, Any]] = []
    for fill in fills:
        purpose = preserve_bot_purpose(fill.get("purpose"))
        if purpose not in EXIT_PURPOSES:
            continue
        if fill is loss_fill:
            continue
        fill_ts = str(fill.get("timestamp") or "")
        if loss_ts and fill_ts and fill_ts < loss_ts:
            continue
        selected.append(fill)
    return sorted(selected, key=_fill_sort_key)


def _final_exit_coverage_status(
    *,
    loss_pnl: float,
    claimed_pnl: float,
    pool_available_before: float,
) -> str:
    abs_loss = abs(loss_pnl)
    if claimed_pnl + COVER_TOLERANCE < abs_loss:
        return "undercovered_by_final_exit"
    if pool_available_before > abs_loss + COVER_TOLERANCE:
        return "overcovered_by_final_exit"
    return "covered_by_final_exit"


def _pool_id_for_fills(fills: list[dict[str, Any]]) -> str:
    ids = [str(fill.get("_audit_fill_id") or "") for fill in fills]
    return "final_exit_pool:" + "|".join(ids)


def _actual_cover_qty_for_fills(cover_fills: list[dict[str, Any]]) -> float | None:
    if not cover_fills:
        return None
    sides = {
        str(fill.get("side") or "").lower()
        for fill in cover_fills
        if str(fill.get("side") or "").strip()
    }
    if len(sides) > 1:
        # Mixed long/short basket: do not sum economically incompatible qtys.
        return None
    qtys = [_safe_float(fill.get("qty")) for fill in cover_fills]
    if any(qty is None for qty in qtys):
        return None
    return float(sum(qtys))  # type: ignore[arg-type]


def _cover_leg_qty_breakdown(cover_fills: list[dict[str, Any]]) -> dict[str, float]:
    breakdown: dict[str, float] = {}
    for fill in cover_fills:
        purpose = preserve_bot_purpose(fill.get("purpose"))
        qty = _safe_float(fill.get("qty"))
        if qty is None:
            continue
        breakdown[purpose] = breakdown.get(purpose, 0.0) + float(qty)
    return breakdown


@dataclass
class _FinalExitPool:
    pool_id: str
    fills: list[dict[str, Any]]
    net_pnl: float
    remaining: float
    fill_ids: list[str] = field(default_factory=list)
    total_claimed: float = 0.0


def _loss_sort_key(fill: dict[str, Any]) -> tuple[str, int, int]:
    return (
        str(fill.get("timestamp") or ""),
        int(fill.get("_audit_fill_index") or 0),
        int(_cycle_index(fill) or 0),
    )


def has_undercovered_final_exit(result: BacktestResult) -> bool:
    return any(
        row.get("status") == "undercovered_by_final_exit"
        for row in build_pnl_coverage_audit(result)
    )


def has_cycle_undercoverage(result: BacktestResult) -> bool:
    return any(
        str(row.get("status") or "")
        in {
            "undercovered",
            "undercovered_by_final_exit",
            "no_cover_fill",
        }
        for row in build_pnl_coverage_audit(result)
    )


def _trade_is_flat(result: BacktestResult) -> bool:
    long_qty = _safe_float(getattr(result, "final_long_qty", None)) or 0.0
    short_qty = _safe_float(getattr(result, "final_short_qty", None)) or 0.0
    if abs(long_qty) > COVER_TOLERANCE or abs(short_qty) > COVER_TOLERANCE:
        return False
    status = str(result.final_status or "")
    if status in {
        "closed",
        EXIT_QUALITY_CLOSED_OK,
        EXIT_QUALITY_UNDERCOVERED_FINAL,
        EXIT_QUALITY_NEGATIVE,
        EXIT_QUALITY_PROFITABLE_CYCLE_UNDER,
        "closed_undercovered_final_exit",
        "closed_negative_pnl",
    }:
        return True
    exit_reason = str(result.exit_reason or "")
    return exit_reason in {
        "flat_no_active_orders",
        "recovery_joint_exit",
        "recovery_timeout_close_all",
        "recovery_max_loss_close_all",
        "recovery_max_additional_loss_close_all",
    }


def _trade_final_exit_net(result: BacktestResult) -> float:
    total = 0.0
    for fill in _fill_log(result):
        if preserve_bot_purpose(fill.get("purpose")) in EXIT_PURPOSES:
            total += _fill_pnl(fill)
    return total


def build_trade_coverage_summary(
    result: BacktestResult,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    audit_rows = rows if rows is not None else build_pnl_coverage_audit(result)
    cycle_under = any(
        str(row.get("status") or "")
        in {"undercovered", "undercovered_by_final_exit", "no_cover_fill"}
        for row in audit_rows
    )
    under_final = any(
        str(row.get("status") or "") == "undercovered_by_final_exit" for row in audit_rows
    )
    realized = float(result.realized_pnl or 0.0)
    flat = _trade_is_flat(result)
    trade_level_undercovered = bool(flat and realized < -COVER_TOLERANCE)
    pool_nets: dict[str, float] = {}
    pool_claimed: dict[str, float] = {}
    for row in audit_rows:
        pool_id = row.get("final_exit_pool_id")
        if not pool_id:
            continue
        pool_id_s = str(pool_id)
        if pool_id_s not in pool_nets and row.get("final_exit_pool_net_pnl") is not None:
            pool_nets[pool_id_s] = float(row["final_exit_pool_net_pnl"])
        claimed = _safe_float(row.get("final_exit_claimed_pnl")) or 0.0
        pool_claimed[pool_id_s] = pool_claimed.get(pool_id_s, 0.0) + claimed

    return {
        "has_cycle_undercoverage": cycle_under,
        "has_undercovered_final_exit": under_final,
        "trade_realized_pnl": realized,
        "trade_final_exit_net": _trade_final_exit_net(result),
        "trade_level_undercovered": trade_level_undercovered,
        "trade_is_flat": flat,
        "final_exit_pool_net_by_id": pool_nets,
        "final_exit_pool_claimed_by_id": pool_claimed,
        "final_exit_pool_available_total": sum(pool_nets.values()),
        "final_exit_pool_claimed_total": sum(pool_claimed.values()),
    }


def classify_trade_exit_quality(
    result: BacktestResult,
    rows: list[dict[str, Any]] | None = None,
    summary: dict[str, Any] | None = None,
) -> str:
    status = str(result.final_status or "")
    closed_like = {
        "closed",
        EXIT_QUALITY_CLOSED_OK,
        EXIT_QUALITY_UNDERCOVERED_FINAL,
        EXIT_QUALITY_NEGATIVE,
        EXIT_QUALITY_PROFITABLE_CYCLE_UNDER,
    }
    if status not in closed_like and not status.startswith("closed_"):
        return status

    audit_rows = rows if rows is not None else build_pnl_coverage_audit(result)
    coverage = summary if summary is not None else build_trade_coverage_summary(result, audit_rows)
    realized = float(coverage["trade_realized_pnl"])
    has_under_final = bool(coverage["has_undercovered_final_exit"])
    has_cycle_under = bool(coverage["has_cycle_undercoverage"])

    # Closed trades: separate cycle-coverage issues from trade-level economics.
    if has_under_final or has_cycle_under:
        if realized > COVER_TOLERANCE:
            return EXIT_QUALITY_PROFITABLE_CYCLE_UNDER
        return EXIT_QUALITY_UNDERCOVERED_FINAL
    if realized < -COVER_TOLERANCE:
        return EXIT_QUALITY_NEGATIVE
    return EXIT_QUALITY_CLOSED_OK


def apply_trade_exit_quality(result: BacktestResult) -> str:
    rows = build_pnl_coverage_audit(result)
    summary = build_trade_coverage_summary(result, rows)
    quality = classify_trade_exit_quality(result, rows=rows, summary=summary)
    result.exit_quality = quality
    result.exit_quality_detail = {
        **summary,
        "exit_quality": quality,
    }
    result.has_cycle_undercoverage = bool(summary["has_cycle_undercoverage"])
    result.trade_level_undercovered = bool(summary["trade_level_undercovered"])
    result.trade_final_exit_net = float(summary["trade_final_exit_net"])
    # Keep overwriting final_status only for economically failed closed labels.
    if quality in {EXIT_QUALITY_UNDERCOVERED_FINAL, EXIT_QUALITY_NEGATIVE}:
        result.final_status = quality
    return quality


def build_pnl_coverage_audit(result: BacktestResult) -> list[dict[str, Any]]:
    """Audit cycle loss fills against cover fills with FIFO final-exit pooling."""
    fills = _annotate_fill_ids(_fill_log(result))
    pending_exits = _pending_exit_purposes(result)
    pending_exit_joined = "|".join(pending_exits)
    rows: list[dict[str, Any]] = []

    loss_fills = sorted(
        [fill for fill in fills if _is_loss_fill(fill)],
        key=_loss_sort_key,
    )
    if not loss_fills:
        return rows

    seen_pairs: set[tuple[int, str, str]] = set()
    pools: dict[str, _FinalExitPool] = {}
    pool_member_ids: set[str] = set()

    for loss_fill in loss_fills:
        loss_fill_id = str(loss_fill.get("_audit_fill_id") or "")
        if loss_fill_id in pool_member_ids:
            # Negative exit legs already consumed as part of a final-exit cover pool.
            continue

        cycle_index = _cycle_index(loss_fill)
        if cycle_index is None:
            continue
        loss_purpose = preserve_bot_purpose(loss_fill.get("purpose"))
        cover_purpose = expected_cover_purpose(loss_purpose)
        pair_key = (cycle_index, loss_purpose, cover_purpose)
        if pair_key in seen_pairs:
            continue
        seen_pairs.add(pair_key)

        cover_candidates = _select_cover_fills(
            fills,
            cycle_index=cycle_index,
            cover_purpose=cover_purpose,
            loss_cycle_role=str(loss_fill.get("cycle_role") or "").lower(),
        )
        loss_pnl = _fill_pnl(loss_fill)
        abs_loss = abs(loss_pnl)
        coverage_source = "cycle_cover"
        pool: _FinalExitPool | None = None
        claimed = 0.0
        available_before = 0.0
        available_after = 0.0

        if cover_candidates:
            cover_pnl = sum(_fill_pnl(fill) for fill in cover_candidates)
            claimed = cover_pnl
        else:
            final_exit_candidates = _select_final_exit_fills_after_loss(
                fills,
                loss_fill=loss_fill,
            )
            final_exit_net_pnl = sum(_fill_pnl(fill) for fill in final_exit_candidates)
            if final_exit_candidates and final_exit_net_pnl > COVER_TOLERANCE:
                pool_id = _pool_id_for_fills(final_exit_candidates)
                pool = pools.get(pool_id)
                if pool is None:
                    fill_ids = [str(fill.get("_audit_fill_id") or "") for fill in final_exit_candidates]
                    pool = _FinalExitPool(
                        pool_id=pool_id,
                        fills=final_exit_candidates,
                        net_pnl=float(final_exit_net_pnl),
                        remaining=float(final_exit_net_pnl),
                        fill_ids=fill_ids,
                    )
                    pools[pool_id] = pool
                    pool_member_ids.update(fill_ids)
                available_before = float(pool.remaining)
                claimed = min(abs_loss, available_before)
                pool.remaining = max(0.0, available_before - claimed)
                pool.total_claimed += claimed
                available_after = float(pool.remaining)
                cover_candidates = list(pool.fills)
                cover_purpose = "|".join(
                    preserve_bot_purpose(fill.get("purpose")) for fill in cover_candidates
                )
                cover_pnl = claimed
                coverage_source = "final_exit_pool"
            else:
                cover_pnl = 0.0

        primary_cover = cover_candidates[0] if cover_candidates else None
        net_pnl = loss_pnl + cover_pnl
        coverage_ratio = (cover_pnl / abs_loss) if abs_loss > COVER_TOLERANCE else None
        missing_pnl = max(0.0, abs_loss - cover_pnl) if cover_pnl < abs_loss - COVER_TOLERANCE else 0.0

        cover_fill_price = _safe_float(primary_cover.get("fill_price")) if primary_cover else None
        cover_entry_price = _entry_price_for_cover(loss_fill=loss_fill, cover_fill=primary_cover)
        expected_qty = expected_cover_qty(
            loss_pnl=loss_pnl,
            entry_price=cover_entry_price,
            fill_price=cover_fill_price,
        )
        actual_qty = _actual_cover_qty_for_fills(cover_candidates)
        qty_shortfall = None
        if expected_qty is not None and actual_qty is not None:
            qty_shortfall = max(0.0, expected_qty - actual_qty)

        qty_mapping_purpose = cover_purpose
        if coverage_source == "final_exit_pool":
            qty_mapping_purpose = preserve_bot_purpose((primary_cover or {}).get("purpose") or "")
        qty_mapping = inspect_qty_mapping(
            result,
            purpose=qty_mapping_purpose or (primary_cover or {}).get("purpose", ""),
            cycle_index=None if coverage_source == "final_exit_pool" else cycle_index,
        )

        if coverage_source == "final_exit_pool" and pool is not None:
            status = _final_exit_coverage_status(
                loss_pnl=loss_pnl,
                claimed_pnl=cover_pnl,
                pool_available_before=available_before,
            )
        else:
            status = _coverage_status(
                loss_pnl=loss_pnl,
                cover_pnl=cover_pnl,
                pending_exit_purposes=pending_exits,
            )

        rows.append(
            {
                "symbol": result.symbol,
                "direction": result.direction,
                "start_index": result.start_index if result.start_index is not None else 0,
                "cycle_index": cycle_index,
                "loss_purpose": loss_purpose,
                "cover_purpose": preserve_bot_purpose(
                    cover_purpose or (primary_cover or {}).get("purpose")
                ),
                "loss_cycle_role": loss_fill.get("cycle_role") or "",
                "cover_cycle_role": (primary_cover or {}).get("cycle_role") or "",
                "loss_pnl": loss_pnl,
                "cover_pnl": cover_pnl,
                "net_pnl": net_pnl,
                "coverage_ratio": coverage_ratio,
                "missing_pnl": missing_pnl,
                "status": status,
                "pending_final_active_order_purposes": pending_exit_joined,
                "expected_cover_qty": expected_qty,
                "actual_cover_qty": actual_qty,
                "qty_shortfall": qty_shortfall,
                "cover_entry_price": cover_entry_price,
                "cover_fill_price": cover_fill_price,
                "intent_qty": qty_mapping.get("intent_qty"),
                "order_qty": qty_mapping.get("order_qty"),
                "fill_qty": qty_mapping.get("fill_qty"),
                "qty_mapping_warning": qty_mapping.get("qty_mapping_warning") or "",
                "loss_fill_timestamp": loss_fill.get("timestamp") or "",
                "cover_fill_timestamp": (primary_cover or {}).get("timestamp") or "",
                "loss_fill_price": _safe_float(loss_fill.get("fill_price")),
                "loss_side": loss_fill.get("side") or "",
                "cover_side": (primary_cover or {}).get("side") or "",
                "coverage_source": coverage_source,
                "final_exit_pool_id": pool.pool_id if pool is not None else None,
                "final_exit_pool_net_pnl": pool.net_pnl if pool is not None else None,
                "final_exit_pool_available_before": (
                    available_before if pool is not None else None
                ),
                "final_exit_claimed_pnl": claimed if pool is not None else None,
                "final_exit_pool_remaining_after": (
                    available_after if pool is not None else None
                ),
                "cover_fill_ids": [
                    str(fill.get("_audit_fill_id") or "") for fill in cover_candidates
                ],
                "cover_leg_qtys": _cover_leg_qty_breakdown(cover_candidates),
            }
        )

    return rows


def write_pnl_coverage_audit(
    result: BacktestResult,
    output_dir: str | Path,
    base_name: str | None = None,
) -> dict[str, str]:
    base = trade_block_export_base_name(result) if base_name is None else base_name
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    rows = build_pnl_coverage_audit(result)
    summary = build_trade_coverage_summary(result, rows)

    csv_path = output_path / f"{base}_pnl_coverage_audit.csv"
    json_path = output_path / f"{base}_pnl_coverage_audit.json"

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(PNL_COVERAGE_AUDIT_FIELDS))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: ("" if row.get(key) is None else row.get(key))
                    for key in PNL_COVERAGE_AUDIT_FIELDS
                }
            )

    payload = {
        "metadata": {
            "base_name": base,
            "symbol": result.symbol,
            "direction": result.direction,
            "start_index": result.start_index,
            "final_status": result.final_status,
            "realized_pnl": result.realized_pnl,
            "final_active_order_purposes": list(result.final_active_order_purposes or []),
            **summary,
        },
        "audit_rows": rows,
    }
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    return {
        "pnl_coverage_audit_csv": str(csv_path),
        "pnl_coverage_audit_json": str(json_path),
    }


def export_pnl_coverage_audits(
    results: list[BacktestResult],
    output_dir: str | Path,
    *,
    start_indices: set[int] | None = None,
) -> tuple[list[dict[str, str]], list[list[dict[str, Any]]]]:
    written: list[dict[str, str]] = []
    rows_by_result: list[list[dict[str, Any]]] = []
    for result in results:
        if start_indices is not None:
            index = result.start_index if result.start_index is not None else 0
            if int(index) not in start_indices:
                continue
        rows = build_pnl_coverage_audit(result)
        written.append(write_pnl_coverage_audit(result, output_dir))
        rows_by_result.append(rows)
    return written, rows_by_result


def print_pnl_coverage_audit_summary(written: list[dict[str, str]], rows_by_file: list[list[dict[str, Any]]]) -> None:
    if not written:
        print("pnl_coverage_audit: no files written")
        return
    print(f"pnl_coverage_audit: files={len(written)}")
    for files, rows in zip(written, rows_by_file):
        if files.get("pnl_coverage_audit_csv"):
            print(f"  pnl_coverage_audit_csv={files['pnl_coverage_audit_csv']}")
        undercovered = sum(1 for row in rows if row.get("status") == "undercovered")
        pending = sum(1 for row in rows if row.get("status") == "pending_final_exit")
        print(
            f"  rows={len(rows)} undercovered={undercovered} pending_final_exit={pending}"
        )
