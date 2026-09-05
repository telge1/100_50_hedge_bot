"""Read-only ClickHouse source coverage for OB200 V2 discovery."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class SourceCoverageRow:
    source: str
    database: str
    table: str
    time_column: str
    symbol: str
    window_start_utc: str
    window_end_utc: str
    row_count: int
    min_ts: str | None
    max_ts: str | None
    coverage_status: str
    notes: str = ""


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _probe_client() -> Any:
    from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client, load_clickhouse_settings

    load_clickhouse_settings()
    return get_clickhouse_client()


def _query_coverage(
    client: Any,
    *,
    database: str,
    table: str,
    time_column: str,
    symbol: str,
    start: datetime,
    end: datetime,
    source: str,
    extra_where: str = "",
) -> SourceCoverageRow:
    fq = f"{database}.{table}"
    sql = f"""
    SELECT
      count() AS n,
      min({time_column}) AS tmin,
      max({time_column}) AS tmax
    FROM {fq}
    WHERE symbol = {{s:String}}
      AND {time_column} >= {{a:DateTime64(3,'UTC')}}
      AND {time_column} < {{b:DateTime64(3,'UTC')}}
      {extra_where}
    """
    try:
        row = client.query(
            sql,
            parameters={"s": symbol, "a": start, "b": end},
            settings={"max_execution_time": 120, "receive_timeout": 130},
        ).result_rows[0]
        n = int(row[0] or 0)
        tmin = None if row[1] is None else str(row[1])
        tmax = None if row[2] is None else str(row[2])
        status = "EMPTY_IN_WINDOW" if n == 0 else "AVAILABLE"
        return SourceCoverageRow(
            source=source,
            database=database,
            table=table,
            time_column=time_column,
            symbol=symbol,
            window_start_utc=_iso(start),
            window_end_utc=_iso(end),
            row_count=n,
            min_ts=tmin,
            max_ts=tmax,
            coverage_status=status,
        )
    except Exception as exc:
        return SourceCoverageRow(
            source=source,
            database=database,
            table=table,
            time_column=time_column,
            symbol=symbol,
            window_start_utc=_iso(start),
            window_end_utc=_iso(end),
            row_count=0,
            min_ts=None,
            max_ts=None,
            coverage_status="QUERY_ERROR",
            notes=f"{type(exc).__name__}:{exc}",
        )


CANDIDATES: list[tuple[str, str, str, str, str]] = [
    # source, database, table, time_column, extra_where
    ("public_trades", "orderbook_analysis", "public_trades_canonical", "trade_ts", ""),
    ("open_interest", "orderbook_analysis", "open_interest_5m_history", "bucket_time", ""),
    ("open_interest", "orderbook_analysis", "open_interest_5s", "bucket_time", ""),
    ("open_interest", "orderbook_analysis", "open_interest_events", "event_time", ""),
    ("open_interest", "orderbook_analysis", "ticker_samples", "exchange_ts", ""),
    ("liquidations", "orderbook_analysis", "all_liquidations", "event_time", ""),
]


def audit_source_coverage(
    symbols: tuple[str, ...],
    start: datetime,
    end: datetime,
) -> tuple[list[SourceCoverageRow], dict[str, Any]]:
    rows: list[SourceCoverageRow] = []
    audit: dict[str, Any] = {
        "window_start_utc": _iso(start),
        "window_end_utc": _iso(end),
        "symbols": list(symbols),
        "client_ok": False,
        "sources": {},
        "notes": [],
    }
    try:
        client = _probe_client()
        audit["client_ok"] = True
    except Exception as exc:
        audit["notes"].append(f"ch_connect_failed:{type(exc).__name__}:{exc}")
        for source, database, table, tcol, _ in CANDIDATES:
            for sym in symbols:
                rows.append(
                    SourceCoverageRow(
                        source=source,
                        database=database,
                        table=table,
                        time_column=tcol,
                        symbol=sym,
                        window_start_utc=_iso(start),
                        window_end_utc=_iso(end),
                        row_count=0,
                        min_ts=None,
                        max_ts=None,
                        coverage_status="NO_CLIENT",
                        notes=str(exc),
                    )
                )
        return rows, audit

    for source, database, table, tcol, extra in CANDIDATES:
        for sym in symbols:
            row = _query_coverage(
                client,
                database=database,
                table=table,
                time_column=tcol,
                symbol=sym,
                start=start,
                end=end,
                source=source,
                extra_where=extra,
            )
            rows.append(row)

    # pick best available per source/symbol
    best: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = f"{r.source}:{r.symbol}"
        cur = best.get(key)
        if r.coverage_status != "AVAILABLE":
            if cur is None:
                best[key] = asdict(r)
            continue
        if cur is None or cur.get("coverage_status") != "AVAILABLE" or int(cur.get("row_count") or 0) < r.row_count:
            best[key] = asdict(r)
    audit["sources"] = best
    audit["notes"].append(
        "Coverage claimed only from live queries; EMPTY_IN_WINDOW ≠ missing table."
    )
    return rows, audit


def write_coverage_artifacts(
    output_dir: Path,
    rows: list[SourceCoverageRow],
    audit: dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    import csv

    path = output_dir / "source_coverage.csv"
    dict_rows = [asdict(r) for r in rows]
    if not dict_rows:
        path.write_text("", encoding="utf-8")
    else:
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(dict_rows[0].keys()))
            w.writeheader()
            w.writerows(dict_rows)
    (output_dir / "market_join_audit.json").write_text(
        json.dumps(audit, indent=2, default=str) + "\n", encoding="utf-8"
    )
