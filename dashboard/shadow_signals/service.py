"""Build shadow signal page and API payloads."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from shadow_signals.formatting import (
    DISPLAY_COLUMNS,
    filter_input_for_datetime_local,
    format_total_pnl_summary,
    format_winrate,
    parse_filter_input_to_utc,
    row_to_display,
)
from shadow_signals.queries import (
    SelectOnlyExecutor,
    clamp_page,
    clamp_page_size,
    count_signals,
    fetch_signal_rows,
    fetch_summary_metrics,
    normalize_side,
    table_for_side,
)


def build_pagination(
    *,
    page: int,
    page_size: int,
    total: int,
) -> dict[str, Any]:
    total_pages = (total + page_size - 1) // page_size if page_size > 0 else 0
    normalized_page = min(page, max(total_pages - 1, 0)) if total_pages else 0
    return {
        "page": normalized_page,
        "page_size": page_size,
        "total_filtered_trades": total,
        "total_signals": total,
        "total_pages": total_pages,
        "has_prev": normalized_page > 0,
        "has_next": normalized_page + 1 < total_pages,
    }


def _summary_from_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    winning = int(metrics.get("winning_allowed") or 0)
    denom = int(metrics.get("allowed_closed_with_pnl") or 0)
    total_pnl = float(metrics.get("total_pnl_sum") or 0.0)
    return {
        "total_signals": int(metrics.get("total_signals") or 0),
        "closed_signals": int(metrics.get("closed_signals") or 0),
        "open_signals": int(metrics.get("open_signals") or 0),
        "winrate": format_winrate(winning, denom),
        "winning_signals": winning,
        "total_pnl": format_total_pnl_summary(total_pnl),
        "total_pnl_raw": total_pnl,
    }


def empty_summary() -> dict[str, Any]:
    return {
        "total_signals": 0,
        "closed_signals": 0,
        "open_signals": 0,
        "winrate": "0 %",
        "winning_signals": 0,
        "total_pnl": "0.00 %",
        "total_pnl_raw": 0.0,
    }


def build_shadow_signals_payload(
    executor: SelectOnlyExecutor,
    *,
    side: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
) -> dict[str, Any]:
    normalized_side = normalize_side(side)
    effective_page_size = clamp_page_size(page_size)
    effective_page = clamp_page(page)
    start_utc = parse_filter_input_to_utc(start_time)
    end_utc = parse_filter_input_to_utc(end_time)

    total = count_signals(executor, normalized_side, start_utc=start_utc, end_utc=end_utc)
    pagination = build_pagination(
        page=effective_page,
        page_size=effective_page_size,
        total=total,
    )
    offset = pagination["page"] * effective_page_size
    raw_rows = fetch_signal_rows(
        executor,
        normalized_side,
        start_utc=start_utc,
        end_utc=end_utc,
        limit=effective_page_size,
        offset=offset,
    )
    rows = [row_to_display(r) for r in raw_rows]
    metrics = fetch_summary_metrics(
        executor,
        normalized_side,
        start_utc=start_utc,
        end_utc=end_utc,
    )
    summary = _summary_from_metrics(metrics)
    now = datetime.now(timezone.utc).isoformat()
    return {
        "success": True,
        "side": normalized_side,
        "table": table_for_side(normalized_side),
        "rows": rows,
        "columns": list(DISPLAY_COLUMNS),
        "summary": summary,
        "pagination": pagination,
        "filters": {
            "start_time": start_time or "",
            "end_time": end_time or "",
            "start_time_input": filter_input_for_datetime_local(start_time),
            "end_time_input": filter_input_for_datetime_local(end_time),
            "page": pagination["page"],
            "page_size": effective_page_size,
        },
        "last_updated": now,
        "offline": False,
    }


def build_offline_payload(
    *,
    side: str | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    page: int | None = None,
    page_size: int | None = None,
    message: str = "Shadow-Signal-Daten aktuell nicht verfügbar",
) -> dict[str, Any]:
    normalized_side = normalize_side(side)
    effective_page_size = clamp_page_size(page_size)
    pagination = build_pagination(page=clamp_page(page), page_size=effective_page_size, total=0)
    return {
        "success": False,
        "offline": True,
        "error": "shadow_signals_ch_unavailable",
        "message": message,
        "side": normalized_side,
        "rows": [],
        "columns": list(DISPLAY_COLUMNS),
        "summary": empty_summary(),
        "pagination": pagination,
        "filters": {
            "start_time": start_time or "",
            "end_time": end_time or "",
            "start_time_input": filter_input_for_datetime_local(start_time),
            "end_time_input": filter_input_for_datetime_local(end_time),
            "page": pagination["page"],
            "page_size": effective_page_size,
        },
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
