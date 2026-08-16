"""Historical Bybit → candles_1m backfill orchestration."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from signal_generator.bybit.history import (
    BybitHistoryClient,
    chunk_batches,
    expected_candle_count,
    ensure_utc,
)
from signal_generator.bybit.quality import (
    CrossCheckSample,
    GapRecord,
    SymbolQualityReport,
    audit_symbol_candles,
    pick_sample_indices,
)
from signal_generator.db.candles import Candle1m, CandleRepository
from signal_generator.db.client import ClickHouseClient


@dataclass(slots=True)
class SymbolBackfillResult:
    symbol: str
    fetched: int
    inserted: int
    dry_run: bool
    final_count: int | None
    physical_count: int | None


@dataclass(slots=True)
class BackfillRunResult:
    start: datetime
    end: datetime
    symbols: list[str]
    per_symbol: list[SymbolBackfillResult]
    quality: list[SymbolQualityReport]
    gaps: list[GapRecord]
    crosschecks: list[CrossCheckSample]
    idempotency: dict[str, dict[str, int]] | None = None


def backfill_symbol(
    client: BybitHistoryClient,
    repo: CandleRepository | None,
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    batch_size: int = 1000,
    dry_run: bool = False,
) -> tuple[SymbolBackfillResult, list[Candle1m]]:
    candles = client.fetch_closed_1m(symbol, start, end)
    inserted = 0
    if not dry_run:
        if repo is None:
            raise ValueError("repo is required unless dry_run=True")
        for batch in chunk_batches(candles, batch_size):
            inserted += repo.insert_candles(batch)
    else:
        inserted = 0

    final_count = None
    physical_count = None
    if repo is not None and not dry_run:
        final_count = repo.count_final(symbol, start, end)
        physical_count = repo.count_physical(symbol, start, end)

    result = SymbolBackfillResult(
        symbol=symbol,
        fetched=len(candles),
        inserted=inserted if not dry_run else 0,
        dry_run=dry_run,
        final_count=final_count,
        physical_count=physical_count,
    )
    return result, candles


def run_backfill(
    *,
    ch: ClickHouseClient | None,
    symbols: Sequence[str],
    start: datetime,
    end: datetime,
    batch_size: int = 1000,
    dry_run: bool = False,
    bybit: BybitHistoryClient | None = None,
) -> BackfillRunResult:
    start = ensure_utc(start)
    end = ensure_utc(end)
    bybit = bybit or BybitHistoryClient()
    repo = CandleRepository(ch) if ch is not None else None

    per_symbol: list[SymbolBackfillResult] = []
    fetched_by_symbol: dict[str, list[Candle1m]] = {}
    for symbol in symbols:
        res, candles = backfill_symbol(
            bybit,
            repo,
            symbol,
            start,
            end,
            batch_size=batch_size,
            dry_run=dry_run,
        )
        per_symbol.append(res)
        fetched_by_symbol[symbol] = candles

    quality: list[SymbolQualityReport] = []
    gaps: list[GapRecord] = []
    crosschecks: list[CrossCheckSample] = []

    if repo is not None and not dry_run:
        for symbol in symbols:
            rows = repo.get_candles(symbol, start, end)
            physical = repo.count_physical(symbol, start, end)
            fetched = fetched_by_symbol[symbol]
            idxs = pick_sample_indices(len(fetched))
            samples = [fetched[i] for i in idxs.values()]
            # Label positions in crosscheck
            report, cross = audit_symbol_candles(
                symbol=symbol,
                start=start,
                end=end,
                final_rows=rows,
                physical_rows=physical,
                bybit_samples=samples,
            )
            # Relabel sample positions
            pos_by_ot = {
                ensure_utc(fetched[i].open_time): name for name, i in idxs.items()
            }
            for item in cross:
                item.position = pos_by_ot.get(item.open_time, item.position)
            quality.append(report)
            gaps.extend(report.gaps)
            crosschecks.extend(cross)

    return BackfillRunResult(
        start=start,
        end=end,
        symbols=list(symbols),
        per_symbol=per_symbol,
        quality=quality,
        gaps=gaps,
        crosschecks=crosschecks,
    )


def write_artifacts(run: BackfillRunResult, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    quality_path = out_dir / "quality_by_symbol.csv"
    with quality_path.open("w", newline="", encoding="utf-8") as f:
        fields = [
            "symbol",
            "expected",
            "unique_final",
            "physical_rows",
            "min_open_time",
            "max_open_time",
            "duplicate_logical_keys",
            "gap_count",
            "largest_gap_seconds",
            "ohlc_error_count",
            "close_time_errors",
            "crosscheck_pass",
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for q in run.quality:
            writer.writerow(q.to_quality_row())

    gaps_path = out_dir / "gaps.csv"
    with gaps_path.open("w", newline="", encoding="utf-8") as f:
        fields = [
            "symbol",
            "previous_open_time",
            "next_open_time",
            "gap_seconds",
            "missing_candle_count",
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for g in run.gaps:
            writer.writerow(
                {
                    "symbol": g.symbol,
                    "previous_open_time": g.previous_open_time.isoformat(),
                    "next_open_time": g.next_open_time.isoformat(),
                    "gap_seconds": g.gap_seconds,
                    "missing_candle_count": g.missing_candle_count,
                }
            )

    cross_path = out_dir / "sample_crosscheck.csv"
    with cross_path.open("w", newline="", encoding="utf-8") as f:
        fields = [
            "symbol",
            "position",
            "open_time",
            "field",
            "bybit_value",
            "clickhouse_value",
            "match",
        ]
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for c in run.crosschecks:
            writer.writerow(
                {
                    "symbol": c.symbol,
                    "position": c.position,
                    "open_time": c.open_time.isoformat(),
                    "field": c.field,
                    "bybit_value": c.bybit_value,
                    "clickhouse_value": c.clickhouse_value,
                    "match": c.match,
                }
            )

    meta = {
        "start": run.start.isoformat(),
        "end": run.end.isoformat(),
        "symbols": run.symbols,
        "expected_per_symbol": expected_candle_count(run.start, run.end),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "idempotency": run.idempotency,
        "per_symbol": [
            {
                "symbol": s.symbol,
                "fetched": s.fetched,
                "inserted": s.inserted,
                "dry_run": s.dry_run,
                "final_count": s.final_count,
                "physical_count": s.physical_count,
            }
            for s in run.per_symbol
        ],
    }
    (out_dir / "run_metadata.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# Bybit History Backfill Smoke",
        "",
        f"- Window: `{run.start.isoformat()}` → `{run.end.isoformat()}` (UTC, half-open)",
        f"- Symbols: {', '.join(run.symbols)}",
        f"- Expected per symbol: {expected_candle_count(run.start, run.end)}",
        "",
        "## Imported",
        "",
        "| Symbol | Expected | Unique FINAL | Physical Rows |",
        "| ------ | -------: | -----------: | ------------: |",
    ]
    for q in run.quality:
        lines.append(
            f"| {q.symbol} | {q.expected} | {q.unique_final} | {q.physical_rows} |"
        )
    lines += [
        "",
        "## Data Quality",
        "",
        "| Symbol | Gaps | Largest Gap | OHLC Errors | CloseTime Errors | Cross-check |",
        "| ------ | ---: | ----------: | ----------: | ---------------: | ----------- |",
    ]
    for q in run.quality:
        cc = "PASS" if q.crosscheck_pass else ("FAIL" if q.crosscheck_pass is False else "n/a")
        lines.append(
            f"| {q.symbol} | {q.gap_count} | {q.largest_gap_seconds} | "
            f"{len(q.ohlc_errors)} | {q.close_time_errors} | {cc} |"
        )
    if run.idempotency:
        lines += ["", "## Idempotency", ""]
        for sym, counts in run.idempotency.items():
            lines.append(
                f"- `{sym}`: FINAL before={counts.get('final_before')} "
                f"after={counts.get('final_after')}; "
                f"physical before={counts.get('physical_before')} "
                f"after={counts.get('physical_after')}"
            )
    lines += [
        "",
        "## Notes",
        "",
        "- Window semantics: half-open `[start, end)` on `open_time`.",
        "- Reads for analysis use `FINAL`; physical row counts without `FINAL` may",
        "  temporarily exceed unique counts until `ReplacingMergeTree` merges.",
        "- Production history rows use `source=bybit_history` and are kept.",
        "",
        "## Artifacts",
        "",
        "- `quality_by_symbol.csv`",
        "- `gaps.csv`",
        "- `sample_crosscheck.csv`",
        "- `run_metadata.json`",
        "",
    ]
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
