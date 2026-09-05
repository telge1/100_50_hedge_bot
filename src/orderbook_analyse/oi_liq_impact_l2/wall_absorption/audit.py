"""Read-only data availability audit for F3 wall absorption."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from orderbook_analyse.oi_liq_impact_l2.contracts import (
    ORDERBOOK_DEPTH,
    ORDERBOOK_PARSER_VERSION,
    ORDERBOOK_TABLE,
)
from orderbook_analyse.oi_liq_impact_l2.wall_absorption.constants import (
    DEFAULT_FILES_ROOT,
    SYMBOL,
    WINDOW_END,
    WINDOW_START,
)

FEATURES_DOMINANT_WALL_COLUMNS = (
    "bid_wall_price",
    "bid_wall_qty",
    "ask_wall_price",
    "ask_wall_qty",
)
FEATURES_AGGREGATE_ONLY_COLUMNS = (
    "bid_qty_l50",
    "ask_qty_l50",
    "bid_qty_added",
    "bid_qty_removed",
)


class WallAbsorptionError(Exception):
    """Raised when F3 wall absorption cannot proceed safely."""


@dataclass(frozen=True)
class AuditResult:
    passed: bool
    verdict: str
    block_reason: str | None
    payload: dict[str, Any]


def _parse_window() -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(WINDOW_START.replace("Z", "+00:00"))
    end = datetime.fromisoformat(WINDOW_END.replace("Z", "+00:00"))
    return start, end


def _day_range(start: datetime, end: datetime) -> list[date]:
    days: list[date] = []
    current = start.date()
    last = (end - timedelta(seconds=1)).date()
    while current <= last:
        days.append(current)
        current += timedelta(days=1)
    return days


def _scan_raw_files(files_root: Path, symbol: str, days: Sequence[date]) -> dict[str, Any]:
    symbol_dir = files_root / symbol
    found: list[str] = []
    missing: list[str] = []
    zipped_only: list[str] = []
    for day in days:
        plain = symbol_dir / day.isoformat() / f"{day.isoformat()}_{symbol}_ob200.data"
        zipped = symbol_dir / day.isoformat() / f"{day.isoformat()}_{symbol}_ob200.data.zip"
        nested_plain = files_root / f"{day.isoformat()}_{symbol}_ob200.data"
        if plain.is_file():
            found.append(str(plain))
        elif nested_plain.is_file():
            found.append(str(nested_plain))
        elif zipped.is_file():
            zipped_only.append(str(zipped))
            missing.append(day.isoformat())
        else:
            missing.append(day.isoformat())
    return {
        "files_root": str(files_root),
        "days_required": len(days),
        "plain_data_files_found": len(found),
        "missing_days": missing,
        "zip_only_days": zipped_only,
        "sample_found_files": found[:5],
        "covers_window": len(missing) == 0 and len(zipped_only) == 0,
    }


def _audit_features_table(
    *,
    client: Any | None,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "table": ORDERBOOK_TABLE,
        "parser_version": ORDERBOOK_PARSER_VERSION,
        "depth": ORDERBOOK_DEPTH,
        "resolution": "1s aggregates",
        "per_level_price_qty_arrays": False,
        "dominant_wall_columns": list(FEATURES_DOMINANT_WALL_COLUMNS),
        "aggregate_only_columns": list(FEATURES_AGGREGATE_ONLY_COLUMNS),
        "side_aggregate_dynamics_only": True,
        "policy": (
            "aggregate bid/ask depth or side totals must not be exported as "
            "concrete price-level walls"
        ),
    }
    if client is None:
        result["query_status"] = "skipped_no_client"
        return result

    query = """
    SELECT
      count() AS rows,
      min(bucket_start) AS min_ts,
      max(bucket_start) AS max_ts,
      countIf(bid_wall_price > 0) AS bid_wall_rows,
      countIf(ask_wall_price > 0) AS ask_wall_rows,
      countIf(has(splitByChar(',', quality_flags), 'carried_forward')) AS cf_rows
    FROM orderbook_analysis.orderbook_features_1s_v2 FINAL
    WHERE symbol = {symbol:String}
      AND parser_version = {parser:String}
      AND depth = {depth:UInt16}
      AND bucket_start >= toDateTime64({start:String}, 3, 'UTC')
      AND bucket_start <  toDateTime64({end:String}, 3, 'UTC')
    """
    try:
        row = client.query(
            query,
            parameters={
                "symbol": SYMBOL,
                "parser": ORDERBOOK_PARSER_VERSION,
                "depth": ORDERBOOK_DEPTH,
                "start": start.strftime("%Y-%m-%d %H:%M:%S"),
                "end": end.strftime("%Y-%m-%d %H:%M:%S"),
            },
        ).result_rows[0]
        result.update(
            {
                "query_status": "ok",
                "rows": int(row[0]),
                "min_ts": str(row[1]),
                "max_ts": str(row[2]),
                "bid_wall_rows": int(row[3]),
                "ask_wall_rows": int(row[4]),
                "carried_forward_rows": int(row[5]),
                "covers_window": int(row[0]) > 0,
            }
        )
    except Exception as exc:  # noqa: BLE001 - audit surface
        result["query_status"] = f"error:{type(exc).__name__}"
        result["error"] = str(exc)
    return result


def _audit_deltas_table(*, client: Any | None, start: datetime, end: datetime) -> dict[str, Any]:
    result: dict[str, Any] = {
        "table": "orderbook_analysis.orderbook_deltas",
        "semantics": "per-level snapshot/delta replay via OrderBookReplayer",
        "preferred_when_available": True,
    }
    if client is None:
        result["query_status"] = "skipped_no_client"
        return result
    query = """
    SELECT count() AS rows, min(exchange_ts) AS min_ts, max(exchange_ts) AS max_ts
    FROM orderbook_analysis.orderbook_deltas
    WHERE symbol = {symbol:String}
      AND exchange_ts >= toDateTime64({start:String}, 3, 'UTC')
      AND exchange_ts <  toDateTime64({end:String}, 3, 'UTC')
    """
    try:
        row = client.query(
            query,
            parameters={
                "symbol": SYMBOL,
                "start": start.strftime("%Y-%m-%d %H:%M:%S"),
                "end": end.strftime("%Y-%m-%d %H:%M:%S"),
            },
        ).result_rows[0]
        result.update(
            {
                "query_status": "ok",
                "rows": int(row[0]),
                "min_ts": str(row[1]) if row[1] is not None else None,
                "max_ts": str(row[2]) if row[2] is not None else None,
                "usable": int(row[0]) > 0,
            }
        )
    except Exception as exc:  # noqa: BLE001 - audit surface
        result["query_status"] = f"error:{type(exc).__name__}"
        result["error"] = str(exc)
        result["usable"] = False
    return result


def _choose_block_reason(payload: Mapping[str, Any]) -> str:
    raw = payload["raw_ob200_files"]
    deltas = payload["orderbook_deltas"]
    reasons: list[str] = []
    if not raw.get("covers_window"):
        reasons.append(
            "missing uncompressed OB200 day files for the frozen BTC window "
            f"({len(raw.get('missing_days', []))} day(s); "
            f"{len(raw.get('zip_only_days', []))} zip-only)"
        )
    if not deltas.get("usable"):
        reasons.append(
            "orderbook_deltas per-level replay is unavailable "
            f"({deltas.get('query_status')})"
        )
    reasons.append(
        "orderbook_features_1s_v2 stores only 1s aggregates plus one "
        "dominant bid_wall/ask_wall per second, not full ob200 level books"
    )
    return "; ".join(reasons)


def run_data_availability_audit(
    *,
    files_root: Path | str = DEFAULT_FILES_ROOT,
    client: Any | None = None,
    query_clickhouse: bool = True,
) -> AuditResult:
    start, end = _parse_window()
    days = _day_range(start, end)
    files_root = Path(files_root)

    if client is None and query_clickhouse:
        try:
            from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

            client = get_clickhouse_client()
        except Exception:  # noqa: BLE001 - optional CH
            client = None

    raw = _scan_raw_files(files_root, SYMBOL, days)
    features = _audit_features_table(
        client=client if query_clickhouse else None, start=start, end=end
    )
    deltas = _audit_deltas_table(
        client=client if query_clickhouse else None, start=start, end=end
    )

    per_level_source_available = bool(raw.get("covers_window") or deltas.get("usable"))
    passed = per_level_source_available

    if raw.get("covers_window"):
        recommended = "ob200_files_via_Ob200FileOrderBookEventSource_and_OrderBookReplayer"
    elif deltas.get("usable"):
        recommended = "orderbook_deltas_via_ClickHouseOrderBookEventSource"
    else:
        recommended = None

    payload: dict[str, Any] = {
        "symbol": SYMBOL,
        "window": {"start": WINDOW_START, "end": WINDOW_END, "semantics": "[start,end)"},
        "audit_timestamp_policy": "static research window only; no outcome-based selection",
        "features_table": features,
        "orderbook_deltas": deltas,
        "raw_ob200_files": raw,
        "existing_reconstructor": {
            "file_source": (
                "orderbook_analyse.ob_data_source.ob200_file_source."
                "Ob200FileOrderBookEventSource"
            ),
            "clickhouse_source": (
                "orderbook_analyse.ob_data_source.clickhouse_source."
                "ClickHouseOrderBookEventSource"
            ),
            "replayer": "orderbook_analyse.orderbook_replay.OrderBookReplayer",
            "parser_version": ORDERBOOK_PARSER_VERSION,
            "depth": ORDERBOOK_DEPTH,
            "no_second_reconstructor": True,
        },
        "genuine_semantics": {
            "carried_forward": (
                "unchanged known state only; never confirms add/remove/refill/absorption"
            ),
            "sequence_gap": "aborts episode; no skip",
        },
        "recommended_source": recommended,
        "per_level_reconstructable_for_window": passed,
        "aggregate_depth_must_not_substitute_for_levels": True,
    }
    block_reason = None if passed else _choose_block_reason(payload)
    verdict = (
        "BTC_F3_WALL_ABSORPTION_DISCOVERY_READY"
        if passed
        else "BTC_F3_WALL_ABSORPTION_DISCOVERY_BLOCKED"
    )
    return AuditResult(
        passed=passed,
        verdict=verdict,
        block_reason=block_reason,
        payload=payload,
    )


def audit_to_json(audit: AuditResult) -> dict[str, Any]:
    body = dict(audit.payload)
    body["passed"] = audit.passed
    body["verdict"] = audit.verdict
    body["block_reason"] = audit.block_reason
    return body
