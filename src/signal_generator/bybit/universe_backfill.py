"""Chunked, resumable multi-symbol Bybit history backfill."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from signal_generator.bybit.checkpoint import (
    CheckpointStore,
    SymbolCheckpoint,
    ensure_symbol_checkpoint,
    load_checkpoint,
    merge_checkpoint_window,
    resume_start_for,
    save_checkpoint,
    should_skip_symbol,
)
from signal_generator.bybit.chunks import iter_time_chunks
from signal_generator.bybit.history import (
    BybitHistoryClient,
    chunk_batches,
    ensure_utc,
    expected_candle_count,
)
from signal_generator.bybit.missing_ranges import (
    DEFAULT_MAX_MISSING_RANGES,
    DEFAULT_MAX_REPAIR_SPAN_MINUTES,
    MissingRangeReport,
    detect_missing_ranges,
)
from signal_generator.bybit.quality import (
    GapRecord,
    SymbolQualityReport,
    aggregate_quality_counts,
)
from signal_generator.bybit.sql_quality import audit_symbol_sql
from signal_generator.bybit.universe import Universe, UniverseSymbol, launch_time_utc
from signal_generator.db.candles import CandleRepository
from signal_generator.db.client import ClickHouseClient


def _ceil_to_minute(ts: datetime) -> datetime:
    ts = ensure_utc(ts)
    if ts.second == 0 and ts.microsecond == 0:
        return ts
    return ts.replace(second=0, microsecond=0) + timedelta(minutes=1)


@dataclass(slots=True)
class UniverseBackfillResult:
    requested_start: datetime
    requested_end: datetime
    symbols: list[str]
    quality: list[SymbolQualityReport]
    gaps: list[GapRecord]
    checkpoint: CheckpointStore
    unique_final_total: int
    physical_total: int
    repair_reports: list[MissingRangeReport] | None = None


def _detail_map(universe: Universe) -> dict[str, UniverseSymbol]:
    return {d.symbol: d for d in universe.details}


def listing_effective_start(
    requested_start: datetime,
    detail: UniverseSymbol | None,
) -> datetime:
    """Effective coverage start: max(requested_start, ceil(launchTime))."""
    start = ensure_utc(requested_start)
    launch = launch_time_utc(detail)
    if launch is None:
        return start
    launch_floor = _ceil_to_minute(launch)
    return start if start >= launch_floor else launch_floor


def effective_work_start(
    cp: SymbolCheckpoint,
    *,
    detail: UniverseSymbol | None,
) -> datetime:
    """Resume cursor, optionally skipping known pre-listing via launchTime."""
    cursor = resume_start_for(cp)
    launch = launch_time_utc(detail)
    if launch is not None:
        launch_floor = _ceil_to_minute(launch)
        if launch_floor > cursor:
            return launch_floor
    return cursor


def format_repair_dry_run(report: MissingRangeReport) -> str:
    lines = [
        f"{report.symbol}:",
        f"  existing={report.existing_count}",
        f"  missing_ranges={report.missing_range_count}",
        f"  missing_candles={report.missing_candles}",
    ]
    for r in report.ranges:
        lines.append(
            f"  [{ensure_utc(r.start).strftime('%Y-%m-%dT%H:%MZ')}, "
            f"{ensure_utc(r.end).strftime('%Y-%m-%dT%H:%MZ')})"
        )
    if report.truncated:
        lines.append(f"  truncated: {report.truncate_reason}")
    return "\n".join(lines)


def backfill_symbol_chunked(
    *,
    symbol: str,
    index: int,
    total: int,
    client: BybitHistoryClient,
    repo: CandleRepository | None,
    store: CheckpointStore,
    checkpoint_path: Path,
    requested_start: datetime,
    requested_end: datetime,
    chunk_days: int,
    batch_size: int,
    dry_run: bool,
    detail: UniverseSymbol | None,
) -> SymbolCheckpoint:
    cp = ensure_symbol_checkpoint(
        store,
        symbol,
        requested_start=requested_start,
        requested_end=requested_end,
    )
    cp.status = "RUNNING"
    cp.last_error = None
    cp.updated_at = datetime.now(timezone.utc)
    save_checkpoint(store, checkpoint_path)

    work_start = effective_work_start(cp, detail=detail)
    if work_start >= requested_end:
        cp.status = "COMPLETE"
        cp.last_completed_timestamp = requested_end
        cp.updated_at = datetime.now(timezone.utc)
        save_checkpoint(store, checkpoint_path)
        return cp

    chunks = iter_time_chunks(work_start, requested_end, chunk_days=chunk_days)
    try:
        for chunk_start, chunk_end in chunks:
            # Enough pages for chunk_days * 1440 candles (+margin)
            max_pages = max(50, (chunk_days * 1440 // 1000) + 5)
            candles = client.fetch_closed_1m(
                symbol,
                chunk_start,
                chunk_end,
                max_pages=max_pages,
            )
            inserted = 0
            if candles:
                if cp.effective_start is None:
                    cp.effective_start = ensure_utc(candles[0].open_time)
                if not dry_run:
                    if repo is None:
                        raise ValueError("repo required unless dry_run")
                    for batch in chunk_batches(candles, batch_size):
                        inserted += repo.insert_candles(batch)
                cp.rows_inserted += inserted if not dry_run else len(candles)

            cp.last_completed_timestamp = chunk_end
            cp.updated_at = datetime.now(timezone.utc)
            cp.status = "PARTIAL"
            save_checkpoint(store, checkpoint_path)
            print(
                f"[{index}/{total}] {symbol} "
                f"{chunk_start.strftime('%Y-%m-%d')} → {chunk_end.strftime('%Y-%m-%d')} "
                f"inserted={inserted if not dry_run else len(candles)}"
            )

        cp.status = "COMPLETE"
        cp.last_completed_timestamp = requested_end
        cp.updated_at = datetime.now(timezone.utc)
        save_checkpoint(store, checkpoint_path)
    except Exception as exc:  # noqa: BLE001
        cp.status = "FAILED"
        cp.last_error = str(exc)
        cp.updated_at = datetime.now(timezone.utc)
        save_checkpoint(store, checkpoint_path)
        print(f"[{index}/{total}] {symbol} FAILED: {exc}")
    return cp


def repair_symbol_missing_ranges(
    *,
    symbol: str,
    index: int,
    total: int,
    client: BybitHistoryClient,
    ch: ClickHouseClient | None,
    repo: CandleRepository | None,
    store: CheckpointStore,
    checkpoint_path: Path,
    requested_start: datetime,
    requested_end: datetime,
    batch_size: int,
    dry_run: bool,
    detail: UniverseSymbol | None,
    open_times: Sequence[datetime] | None = None,
    max_ranges: int = DEFAULT_MAX_MISSING_RANGES,
    max_span_minutes: int = DEFAULT_MAX_REPAIR_SPAN_MINUTES,
) -> MissingRangeReport:
    """Detect and repair missing 1m ranges using the existing history client."""
    cp = ensure_symbol_checkpoint(
        store,
        symbol,
        requested_start=requested_start,
        requested_end=requested_end,
    )
    eff = listing_effective_start(requested_start, detail)
    if cp.effective_start is None:
        cp.effective_start = eff

    if open_times is None and ch is None:
        raise ValueError("ch or open_times required for missing-range repair")

    report = detect_missing_ranges(
        ch,
        symbol=symbol,
        effective_start=eff,
        requested_end=requested_end,
        open_times=open_times,
        max_ranges=max_ranges,
        max_span_minutes=max_span_minutes,
    )

    cp.repair_started_at = datetime.now(timezone.utc)
    cp.missing_ranges_detected = report.missing_range_count
    cp.missing_candles_detected = report.missing_candles
    cp.ranges_repaired = 0
    cp.candles_repaired = 0
    cp.unresolved_ranges = 0
    cp.last_error = None
    cp.updated_at = datetime.now(timezone.utc)
    save_checkpoint(store, checkpoint_path)

    if dry_run:
        print(format_repair_dry_run(report))
        cp.repair_completed_at = datetime.now(timezone.utc)
        save_checkpoint(store, checkpoint_path)
        return report

    if not report.ranges:
        print(
            f"[{index}/{total}] {symbol} repair: no missing ranges "
            f"(existing={report.existing_count})"
        )
        cp.repair_completed_at = datetime.now(timezone.utc)
        if cp.status not in ("FAILED", "PARTIAL"):
            cp.status = "COMPLETE"
        cp.last_completed_timestamp = requested_end
        save_checkpoint(store, checkpoint_path)
        return report

    if report.truncated:
        print(
            f"[{index}/{total}] {symbol} repair safety limit: {report.truncate_reason}"
        )

    print(
        f"[{index}/{total}] {symbol} repair: "
        f"ranges={report.missing_range_count} missing_candles={report.missing_candles}"
    )

    try:
        for mr in report.ranges:
            span_m = mr.missing_minutes
            max_pages = max(50, (span_m // 1000) + 5)
            candles = client.fetch_closed_1m(
                symbol,
                mr.start,
                mr.end,
                max_pages=max_pages,
            )
            inserted = 0
            if candles:
                if repo is None:
                    raise ValueError("repo required unless dry_run")
                for batch in chunk_batches(candles, batch_size):
                    inserted += repo.insert_candles(batch)
                cp.rows_inserted += inserted
                cp.candles_repaired = (cp.candles_repaired or 0) + inserted
            cp.ranges_repaired = (cp.ranges_repaired or 0) + 1
            if len(candles) < span_m:
                cp.unresolved_ranges = (cp.unresolved_ranges or 0) + 1
            cp.updated_at = datetime.now(timezone.utc)
            save_checkpoint(store, checkpoint_path)
            print(
                f"[{index}/{total}] {symbol} repair "
                f"[{mr.start.strftime('%Y-%m-%dT%H:%MZ')}, "
                f"{mr.end.strftime('%Y-%m-%dT%H:%MZ')}) "
                f"kind={mr.kind} fetched={len(candles)} inserted={inserted}"
            )

        if report.truncated:
            cp.status = "PARTIAL"
        else:
            cp.status = "COMPLETE"
            cp.last_completed_timestamp = requested_end
        cp.repair_completed_at = datetime.now(timezone.utc)
        cp.updated_at = datetime.now(timezone.utc)
        save_checkpoint(store, checkpoint_path)
    except Exception as exc:  # noqa: BLE001
        cp.status = "FAILED"
        cp.last_error = str(exc)
        cp.updated_at = datetime.now(timezone.utc)
        save_checkpoint(store, checkpoint_path)
        print(f"[{index}/{total}] {symbol} repair FAILED: {exc}")
    return report


def audit_completed_symbol(
    *,
    symbol: str,
    ch: ClickHouseClient,
    cp: SymbolCheckpoint,
    requested_start: datetime,
    requested_end: datetime,
) -> SymbolQualityReport:
    report = audit_symbol_sql(
        ch,
        symbol=symbol,
        requested_start=requested_start,
        requested_end=requested_end,
        effective_start=cp.effective_start,
        checkpoint_status=cp.status if cp.status in ("FAILED", "PARTIAL") else None,
    )
    if cp.effective_start is None and report.min_open_time is not None:
        cp.effective_start = ensure_utc(report.min_open_time)
        report.effective_start = cp.effective_start
    if cp.status == "COMPLETE" and report.unique_final == 0:
        report.final_status = "NO_HISTORY"
    cp.final_status = report.final_status  # type: ignore[assignment]
    cp.unique_final = report.unique_final
    cp.physical_rows = report.physical_rows
    cp.gap_count = report.gap_count
    cp.largest_gap_seconds = report.largest_gap_seconds
    cp.missing_candle_count = report.missing_candle_count
    cp.ohlc_error_count = (
        report.ohlc_error_count
        if report.ohlc_error_count is not None
        else len(report.ohlc_errors)
    )
    cp.updated_at = datetime.now(timezone.utc)
    return report


def run_universe_backfill(
    *,
    universe: Universe,
    ch: ClickHouseClient | None,
    requested_start: datetime,
    requested_end: datetime,
    checkpoint_path: Path,
    symbols: Sequence[str] | None = None,
    max_symbols: int | None = None,
    chunk_days: int = 7,
    batch_size: int = 1000,
    dry_run: bool = False,
    resume: bool = True,
    retry_failed: bool = False,
    repair_missing: bool = False,
    bybit: BybitHistoryClient | None = None,
    max_missing_ranges: int = DEFAULT_MAX_MISSING_RANGES,
    max_repair_span_minutes: int = DEFAULT_MAX_REPAIR_SPAN_MINUTES,
    open_times_by_symbol: dict[str, Sequence[datetime]] | None = None,
    repo: CandleRepository | None = None,
) -> UniverseBackfillResult:
    """Run chunked backfill and optionally repair missing 1m ranges.

    Decision: ``--repair-missing`` is explicit (not implicit on ``--resume``) so
    resume remains a cheap forward cursor unless repair is requested.
    """
    requested_start = ensure_utc(requested_start)
    requested_end = ensure_utc(requested_end)
    bybit = bybit or BybitHistoryClient(request_pause_s=0.05)
    if repo is None and ch is not None:
        repo = CandleRepository(ch)

    selected = list(symbols) if symbols else list(universe.symbols)
    if max_symbols is not None:
        selected = selected[: max(0, max_symbols)]
    details = _detail_map(universe)

    existing = load_checkpoint(checkpoint_path) if resume else None
    if existing is not None:
        store = existing
        if (
            ensure_utc(store.requested_start) != requested_start
            or ensure_utc(store.requested_end) != requested_end
        ):
            merge_checkpoint_window(
                store,
                requested_start=requested_start,
                requested_end=requested_end,
            )
    else:
        store = CheckpointStore(
            version=1,
            requested_start=requested_start,
            requested_end=requested_end,
            chunk_days=chunk_days,
        )
    store.chunk_days = chunk_days
    store.requested_start = requested_start
    store.requested_end = requested_end

    if not resume:
        # Re-run from scratch for selected symbols (idempotent inserts)
        for sym in selected:
            store.symbols.pop(sym, None)

    total = len(selected)
    quality: list[SymbolQualityReport] = []
    gaps: list[GapRecord] = []
    repair_reports: list[MissingRangeReport] = []

    for i, symbol in enumerate(selected, start=1):
        cp = ensure_symbol_checkpoint(
            store,
            symbol,
            requested_start=requested_start,
            requested_end=requested_end,
        )
        if should_skip_symbol(
            cp,
            resume=resume,
            retry_failed=retry_failed,
            repair_missing=repair_missing,
        ):
            print(f"[{i}/{total}] {symbol} skip status={cp.status}")
            if ch is not None and not dry_run:
                report = audit_completed_symbol(
                    symbol=symbol,
                    ch=ch,
                    cp=cp,
                    requested_start=requested_start,
                    requested_end=requested_end,
                )
                quality.append(report)
                gaps.extend(report.gaps)
                save_checkpoint(store, checkpoint_path)
                print(
                    f"[{i}/{total}] {symbol} quality={report.final_status} "
                    f"final={report.unique_final}/{report.expected} gaps={report.gap_count}"
                )
            continue

        if cp.status == "FAILED" and retry_failed:
            cp.status = "PARTIAL"
            cp.last_error = None

        # Cursor resume for non-COMPLETE symbols (aborted / extended runs).
        run_cursor = cp.status != "COMPLETE"
        if run_cursor and not (dry_run and repair_missing):
            # dry-run + repair-missing: detection only, no API fetch via cursor
            backfill_symbol_chunked(
                symbol=symbol,
                index=i,
                total=total,
                client=bybit,
                repo=repo,
                store=store,
                checkpoint_path=checkpoint_path,
                requested_start=requested_start,
                requested_end=requested_end,
                chunk_days=chunk_days,
                batch_size=batch_size,
                dry_run=dry_run,
                detail=details.get(symbol),
            )
            cp = store.symbols[symbol]

        if repair_missing:
            if dry_run and ch is None and (
                open_times_by_symbol is None or symbol not in open_times_by_symbol
            ):
                raise ValueError(
                    "repair dry-run requires ClickHouse connection or open_times_by_symbol"
                )
            rr = repair_symbol_missing_ranges(
                symbol=symbol,
                index=i,
                total=total,
                client=bybit,
                ch=ch,
                repo=repo,
                store=store,
                checkpoint_path=checkpoint_path,
                requested_start=requested_start,
                requested_end=requested_end,
                batch_size=batch_size,
                dry_run=dry_run,
                detail=details.get(symbol),
                open_times=(
                    open_times_by_symbol.get(symbol) if open_times_by_symbol else None
                ),
                max_ranges=max_missing_ranges,
                max_span_minutes=max_repair_span_minutes,
            )
            repair_reports.append(rr)
            cp = store.symbols[symbol]
            # COMPLETE + zero missing ranges: skip further work
            if (
                rr.missing_range_count == 0
                and not dry_run
                and cp.status == "COMPLETE"
                and ch is not None
            ):
                report = audit_completed_symbol(
                    symbol=symbol,
                    ch=ch,
                    cp=cp,
                    requested_start=requested_start,
                    requested_end=requested_end,
                )
                quality.append(report)
                gaps.extend(report.gaps)
                save_checkpoint(store, checkpoint_path)
                print(
                    f"[{i}/{total}] {symbol} quality={report.final_status} "
                    f"final={report.unique_final}/{report.expected} gaps={report.gap_count}"
                )
                continue

        cp = store.symbols[symbol]
        if ch is not None and not dry_run:
            report = audit_completed_symbol(
                symbol=symbol,
                ch=ch,
                cp=cp,
                requested_start=requested_start,
                requested_end=requested_end,
            )
            quality.append(report)
            gaps.extend(report.gaps)
            save_checkpoint(store, checkpoint_path)
            print(
                f"[{i}/{total}] {symbol} quality={report.final_status} "
                f"final={report.unique_final}/{report.expected} gaps={report.gap_count}"
            )
        elif dry_run and not repair_missing:
            print(f"[{i}/{total}] {symbol} dry-run done status={cp.status}")

    unique_total = sum(q.unique_final for q in quality)
    physical_total = sum(q.physical_rows for q in quality)
    return UniverseBackfillResult(
        requested_start=requested_start,
        requested_end=requested_end,
        symbols=selected,
        quality=quality,
        gaps=gaps,
        checkpoint=store,
        unique_final_total=unique_total,
        physical_total=physical_total,
        repair_reports=repair_reports or None,
    )


def write_universe_artifacts(
    *,
    out_dir: Path,
    universe: Universe,
    result: UniverseBackfillResult,
    db_bytes: int | None = None,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    save_checkpoint(result.checkpoint, out_dir / "checkpoint.json")

    with (out_dir / "universe.csv").open("w", newline="", encoding="utf-8") as f:
        fields = [
            "rank",
            "symbol",
            "turnover24h",
            "volume24h",
            "launch_time",
            "contract_type",
            "status",
            "settle_coin",
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        detail_by = {d.symbol: d for d in universe.details}
        for sym in result.symbols:
            d = detail_by.get(sym)
            if d is None:
                w.writerow(
                    {
                        "rank": "",
                        "symbol": sym,
                        "turnover24h": "",
                        "volume24h": "",
                        "launch_time": "",
                        "contract_type": "",
                        "status": "",
                        "settle_coin": "",
                    }
                )
            else:
                w.writerow(
                    {
                        "rank": d.rank,
                        "symbol": d.symbol,
                        "turnover24h": d.turnover24h,
                        "volume24h": d.volume24h,
                        "launch_time": d.launch_time or "",
                        "contract_type": d.contract_type,
                        "status": d.status,
                        "settle_coin": d.settle_coin,
                    }
                )

    with (out_dir / "quality_by_symbol.csv").open("w", newline="", encoding="utf-8") as f:
        if result.quality:
            fields = list(result.quality[0].to_quality_row().keys())
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for q in result.quality:
                w.writerow(q.to_quality_row())
        else:
            f.write("symbol,final_status\n")

    with (out_dir / "gaps.csv").open("w", newline="", encoding="utf-8") as f:
        fields = [
            "symbol",
            "gap_class",
            "previous_open_time",
            "next_open_time",
            "gap_seconds",
            "missing_candle_count",
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for g in result.gaps:
            if g.gap_class != "INTERNAL_DATA_GAP":
                continue
            w.writerow(
                {
                    "symbol": g.symbol,
                    "gap_class": g.gap_class,
                    "previous_open_time": g.previous_open_time.isoformat(),
                    "next_open_time": g.next_open_time.isoformat(),
                    "gap_seconds": g.gap_seconds,
                    "missing_candle_count": g.missing_candle_count,
                }
            )

    with (out_dir / "failed_symbols.csv").open("w", newline="", encoding="utf-8") as f:
        fields = ["symbol", "status", "last_error", "last_completed_timestamp"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for sym, cp in sorted(result.checkpoint.symbols.items()):
            if cp.status != "FAILED" and (cp.final_status not in ("FAILED",)):
                continue
            w.writerow(
                {
                    "symbol": sym,
                    "status": cp.status,
                    "last_error": cp.last_error or "",
                    "last_completed_timestamp": (
                        cp.last_completed_timestamp.isoformat()
                        if cp.last_completed_timestamp
                        else ""
                    ),
                }
            )

    counts = aggregate_quality_counts(result.quality)
    meta = {
        "requested_start": result.requested_start.isoformat(),
        "requested_end": result.requested_end.isoformat(),
        "symbols": result.symbols,
        "unique_final_total": result.unique_final_total,
        "physical_total": result.physical_total,
        "db_bytes": db_bytes,
        "status_counts": counts,
        "universe": {
            "generated_at": universe.generated_at,
            "selection_method": universe.selection_method,
            "target_size": universe.target_size,
            "symbol_count": len(universe.symbols),
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "expected_full_coverage_per_symbol": expected_candle_count(
            result.requested_start, result.requested_end
        ),
    }
    (out_dir / "run_metadata.json").write_text(
        json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    coins_no_gaps = sum(1 for q in result.quality if q.gap_count == 0 and q.unique_final > 0)
    coins_with_gaps = sum(1 for q in result.quality if q.gap_count > 0)
    missing_total = sum(q.missing_candle_count for q in result.quality)
    largest_gap = max((q.largest_gap_seconds for q in result.quality), default=0)
    lines = [
        "# Bybit 100-Coin History Backfill",
        "",
        f"- Window: `{result.requested_start.isoformat()}` → `{result.requested_end.isoformat()}`",
        f"- Selection: `{universe.selection_method}`",
        f"- Symbols in run: {len(result.symbols)}",
        f"- Unique FINAL candles: {result.unique_final_total}",
        f"- Physical rows: {result.physical_total}",
        f"- DB bytes (approx): {db_bytes if db_bytes is not None else 'n/a'}",
        "",
        "## Coverage",
        "",
        "| Status | Count |",
        "| ------ | ----: |",
    ]
    for key in (
        "COMPLETE_CLEAN",
        "COMPLETE_WITH_INTERNAL_GAPS",
        "PARTIAL",
        "FAILED",
        "NO_HISTORY",
    ):
        lines.append(f"| {key} | {counts.get(key, 0)} |")
    lines += [
        "",
        "## Quality",
        "",
        f"- Coins without internal gaps: {coins_no_gaps}",
        f"- Coins with internal gaps: {coins_with_gaps}",
        f"- Total internal missing candles: {missing_total}",
        f"- Largest internal gap (seconds): {largest_gap}",
        "",
        "- Resume: checkpoint cursor (`last_completed_timestamp`) continues aborted runs.",
        "- Repair: `--repair-missing` fills leading/internal/trailing gaps from ClickHouse;",
        "  COMPLETE is skipped only when resume without repair, or repair finds 0 missing ranges.",
        "- Window extension merges checkpoint windows (no full store reset).",
        "",
        "## FINAL query note",
        "",
        "- This audit uses `FINAL` for correctness under `ReplacingMergeTree`.",
        "- For frequent production chart queries across ~100 symbols × months,",
        "  prefer ingestion-side idempotency + occasional `OPTIMIZE`, or",
        "  `argMax`/`LIMIT 1 BY` reads / a cleaned projection — avoid relying on",
        "  `FINAL` for every interactive dashboard query.",
        "",
    ]
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
