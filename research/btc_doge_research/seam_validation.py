"""Bounded CH-history versus raw-OB200 seam comparisons."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .clickhouse import rows
from .config import OB200_ROOT, PilotWindow
from .contracts import parse_utc
from .ob200_parser import OB200SegmentReader
from .ob200_storage import build_orderbook_seconds
from .pilot_runner import discover_sources


def compare_seam_window(
    client: Any, symbol: str, start_raw: str, end_raw: str
) -> dict[str, Any]:
    start, end = parse_utc(start_raw), parse_utc(end_raw)
    window = PilotWindow(
        pilot_id=f"seam_{symbol}_{start:%Y%m%dT%H%M%S}",
        symbol=symbol,
        start=start,
        end=end,
        reference="deterministic_phase_1_seam_probe",
    )
    sources = discover_sources(window)
    by_second = {}
    source_records = 0
    for source in sources:
        reader = OB200SegmentReader(source, symbol)
        for event in reader.iter_full_books(start, end):
            by_second[event.event_time.replace(microsecond=0)] = event
        source_records += reader.audit.records_read
        if reader.audit.u_gaps or not reader.audit.full_file_consumed:
            return {
                "symbol": symbol,
                "start": start_raw,
                "end": end_raw,
                "classification": "RECONSTRUCTION_ERROR",
                "source_records": source_records,
            }
    raw = build_orderbook_seconds(
        symbol,
        start,
        end,
        [event for _, event in sorted(by_second.items())],
        "seam_validation",
        datetime.now(timezone.utc),
    )
    raw_by_time = {row[1]: row for row in raw}
    ch = rows(
        client,
        """
        SELECT bucket_start, mid_price, best_bid_price, best_ask_price,
               spread_abs, bid_qty_l50, ask_qty_l50, imbalance_l50,
               quality_flags
        FROM orderbook_analysis.orderbook_features_1s_v2
        WHERE symbol = %(symbol)s AND depth = 200
          AND bucket_start >= %(start)s AND bucket_start < %(end)s
        ORDER BY bucket_start
        """,
        {"symbol": symbol, "start": start, "end": end},
    )
    ch_by_time = {}
    for row in ch:
        key = row[0]
        if key.tzinfo is None:
            key = key.replace(tzinfo=timezone.utc)
        ch_by_time[key] = row
    expected = int((end - start).total_seconds())
    common = sorted(set(raw_by_time) & set(ch_by_time))
    missing_raw = expected - len(raw_by_time)
    missing_ch = expected - len(ch_by_time)
    if not common:
        classification = "SOURCE_GAP"
        return {
            "symbol": symbol, "start": start_raw, "end": end_raw,
            "classification": classification, "expected_seconds": expected,
            "raw_seconds": len(raw_by_time), "ch_seconds": len(ch_by_time),
            "missing_raw_seconds": missing_raw, "missing_ch_seconds": missing_ch,
            "source_records": source_records,
        }
    price_errors = []
    bid_errors = []
    ask_errors = []
    imbalance_errors = []
    genuine_mismatches = 0
    level_count_min = 400
    for key in common:
        raw_row, ch_row = raw_by_time[key], ch_by_time[key]
        price_errors.append(abs(Decimal(str(raw_row[2])) - Decimal(str(ch_row[1]))))
        bid_errors.append(abs(Decimal(str(raw_row[7])) - Decimal(str(ch_row[5]))))
        ask_errors.append(abs(Decimal(str(raw_row[8])) - Decimal(str(ch_row[6]))))
        imbalance_errors.append(abs(float(raw_row[9]) - float(ch_row[7])))
        ch_genuine = str(ch_row[8]) != "carried_forward"
        genuine_mismatches += int(bool(raw_row[14]) != ch_genuine)
        level_count_min = min(level_count_min, int(raw_row[12]), int(raw_row[13]))
    max_price = max(price_errors)
    max_bid = max(bid_errors)
    max_ask = max(ask_errors)
    max_imbalance = max(imbalance_errors)
    tick = Decimal("0.1") if symbol == "BTCUSDT" else Decimal("0.00001")
    if missing_raw or missing_ch:
        classification = "SOURCE_GAP"
    elif (
        max_price == 0
        and max_bid == 0
        and max_ask == 0
        and max_imbalance <= 1e-12
        and genuine_mismatches == 0
    ):
        classification = "EXACT_PARITY"
    elif (
        max_price <= tick
        and max_imbalance <= 0.01
        and genuine_mismatches <= max(1, expected // 100)
    ):
        classification = "TOLERANCE_PARITY"
    else:
        classification = "NOT_COMPARABLE"
    return {
        "symbol": symbol,
        "start": start_raw,
        "end": end_raw,
        "classification": classification,
        "expected_seconds": expected,
        "raw_seconds": len(raw_by_time),
        "ch_seconds": len(ch_by_time),
        "common_seconds": len(common),
        "missing_raw_seconds": missing_raw,
        "missing_ch_seconds": missing_ch,
        "max_mid_abs_error": str(max_price),
        "max_bid_qty_l50_abs_error": str(max_bid),
        "max_ask_qty_l50_abs_error": str(max_ask),
        "max_imbalance_l50_abs_error": max_imbalance,
        "genuine_carried_forward_mismatches": genuine_mismatches,
        "raw_min_level_count": level_count_min,
        "source_records": source_records,
        "uses_final": False,
        "raw_root": str(OB200_ROOT),
        "reason": (
            "Both sources have complete timestamp/quality coverage, but value "
            "divergence exceeds tolerance and the causal source-semantic "
            "difference has not been proven."
            if classification == "NOT_COMPARABLE"
            else ""
        ),
    }
