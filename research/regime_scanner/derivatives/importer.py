"""Orchestration for derivatives 5m import (dry-run / persist / verify)."""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from research.regime_scanner.derivatives.aggregate_5m import (
    AggregationResult,
    BucketRecord,
    aggregate_rows,
    parse_utc,
)
from research.regime_scanner.derivatives.config import (
    KNOWN_OUTAGE_END,
    KNOWN_OUTAGE_START,
    KNOWN_UNAVAILABLE_SYMBOLS,
    PILOT_SYMBOLS,
    SOURCE_DATABASE_DEFAULT,
    SOURCE_TABLE,
    DerivativeSourceConfig,
)
from research.regime_scanner.derivatives.hashing import json_hash
from research.regime_scanner.derivatives.source_adapter import DerivativeSourceAdapter
from research.regime_scanner.derivatives.store_memory import InMemoryDerivativeStore, UpsertStats
from research.regime_scanner.derivatives.validation import validate_before_persist

logger = logging.getLogger(__name__)


@dataclass
class ImportRequest:
    symbols: list[str]
    start: datetime
    end: datetime
    import_version: str
    import_label: str | None
    mode: str  # dry_run | persist | verify_only
    output_dir: Path
    row_limit: int | None = None
    chunk_size: int = 5000
    persist_import_run_on_dry_run: bool = False
    # Optional pilot baseline (e.g. 42390); None disables baseline gate.
    baseline_buckets: int | None = None
    max_reject_rate: float = 0.05


@dataclass
class ImportResult:
    mode: str
    status: str
    rows_read: int = 0
    buckets_generated: int = 0
    rows_inserted: int = 0
    rows_updated: int = 0
    rows_unchanged: int = 0
    rows_rejected: int = 0
    symbols_completed: list[str] = field(default_factory=list)
    unavailable_symbols: list[str] = field(default_factory=list)
    reconciliation: dict[str, Any] = field(default_factory=dict)
    join_stats: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None
    output_files: list[str] = field(default_factory=list)


def validate_symbols(symbols: Sequence[str]) -> tuple[list[str], list[str]]:
    """Return (accepted, unavailable). Raises on empty or invalid format."""
    from research.regime_scanner.derivatives.aggregate_5m import normalize_symbol

    if not symbols:
        raise ValueError("empty symbol list")
    accepted: list[str] = []
    unavailable: list[str] = []
    for s in symbols:
        u = normalize_symbol(s)
        if u in KNOWN_UNAVAILABLE_SYMBOLS:
            unavailable.append(u)
        else:
            accepted.append(u)
    if unavailable and not accepted:
        raise ValueError(
            "all requested symbols are known-unavailable in source: "
            + ",".join(unavailable)
        )
    return accepted, unavailable


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        if fieldnames:
            with path.open("w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
        return
    keys = fieldnames or list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def reconcile_source_vs_buckets(
    raw_rows: list[dict[str, Any]],
    buckets: list[BucketRecord],
) -> list[dict[str, Any]]:
    """Sum checks per symbol (source minute totals vs 5m aggregates)."""
    import math
    from collections import defaultdict

    def _close(a: float, b: float) -> bool:
        # Large USD notionals need relative tolerance; tiny abs noise is fine.
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-3)

    src: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "long": 0.0,
            "short": 0.0,
            "buy": 0.0,
            "sell": 0.0,
            "rows": 0.0,
        }
    )
    for r in raw_rows:
        sym = str(r["symbol"]).upper()
        src[sym]["rows"] += 1
        for k_src, k_dst in (
            ("long_liq_usd", "long"),
            ("short_liq_usd", "short"),
            ("buy_volume", "buy"),
            ("sell_volume", "sell"),
        ):
            v = r.get(k_src)
            if v is not None and v != "":
                src[sym][k_dst] += float(v)

    agg: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "long": 0.0,
            "short": 0.0,
            "buy": 0.0,
            "sell": 0.0,
            "buckets": 0.0,
            "complete": 0.0,
            "incomplete": 0.0,
        }
    )
    for b in buckets:
        a = agg[b.symbol]
        a["buckets"] += 1
        if b.data_available:
            a["complete"] += 1
        else:
            a["incomplete"] += 1
        if b.long_liquidation_usd is not None:
            a["long"] += b.long_liquidation_usd
        if b.short_liquidation_usd is not None:
            a["short"] += b.short_liquidation_usd
        if b.buy_volume is not None:
            a["buy"] += b.buy_volume
        if b.sell_volume is not None:
            a["sell"] += b.sell_volume

    out: list[dict[str, Any]] = []
    for sym in sorted(set(src) | set(agg)):
        s = src[sym]
        a = agg[sym]
        out.append(
            {
                "symbol": sym,
                "source_rows": int(s["rows"]),
                "buckets": int(a["buckets"]),
                "complete_buckets": int(a["complete"]),
                "incomplete_buckets": int(a["incomplete"]),
                "source_long_liq_usd": s["long"],
                "agg_long_liq_usd": a["long"],
                "long_match": _close(s["long"], a["long"]),
                "source_short_liq_usd": s["short"],
                "agg_short_liq_usd": a["short"],
                "short_match": _close(s["short"], a["short"]),
                "source_buy_volume": s["buy"],
                "agg_buy_volume": a["buy"],
                "buy_match": _close(s["buy"], a["buy"]),
                "source_sell_volume": s["sell"],
                "agg_sell_volume": a["sell"],
                "sell_match": _close(s["sell"], a["sell"]),
                "delta_recomputed_ok": _close(a["buy"] - a["sell"], s["buy"] - s["sell"]),
            }
        )
    return out


def join_with_ohlcv(
    buckets: list[BucketRecord],
    ohlcv_by_symbol: dict[str, set[datetime]],
) -> list[dict[str, Any]]:
    outage_start = parse_utc(KNOWN_OUTAGE_START)
    outage_end = parse_utc(KNOWN_OUTAGE_END)
    rows: list[dict[str, Any]] = []
    for sym in sorted({b.symbol for b in buckets} | set(ohlcv_by_symbol)):
        deriv = {b.bucket_start.astimezone(timezone.utc) for b in buckets if b.symbol == sym}
        # normalize naive sets
        ohlcv_raw = ohlcv_by_symbol.get(sym, set())
        ohlcv: set[datetime] = set()
        for t in ohlcv_raw:
            if t.tzinfo is None:
                ohlcv.add(t.replace(tzinfo=timezone.utc))
            else:
                ohlcv.add(t.astimezone(timezone.utc))
        both = deriv & ohlcv
        only_d = deriv - ohlcv
        only_o = ohlcv - deriv

        def in_outage(ts: datetime) -> bool:
            return outage_start <= ts < outage_end

        both_ex = {t for t in both if not in_outage(t)}
        deriv_ex = {t for t in deriv if not in_outage(t)}
        ohlcv_ex = {t for t in ohlcv if not in_outage(t)}
        join_denom = len(deriv) if deriv else 0
        join_rate = (len(both) / join_denom) if join_denom else None
        join_rate_ex = (len(both_ex) / len(deriv_ex)) if deriv_ex else None
        rows.append(
            {
                "symbol": sym,
                "derivative_buckets": len(deriv),
                "ohlcv_buckets": len(ohlcv),
                "joined": len(both),
                "missing_ohlcv": len(only_d),
                "missing_derivatives": len(only_o),
                "join_rate": join_rate,
                "join_rate_excluding_outage": join_rate_ex,
                "outage_derivative_buckets": sum(1 for t in deriv if in_outage(t)),
            }
        )
    return rows


def gap_summary(buckets: list[BucketRecord]) -> list[dict[str, Any]]:
    outage_start = parse_utc(KNOWN_OUTAGE_START)
    outage_end = parse_utc(KNOWN_OUTAGE_END)
    rows: list[dict[str, Any]] = []
    for sym in sorted({b.symbol for b in buckets}):
        sym_b = [b for b in buckets if b.symbol == sym]
        gaps = [
            b
            for b in sym_b
            if b.gap_before_seconds is not None and b.gap_before_seconds >= 3600
        ]
        seq_max = max((b.sequence_id for b in sym_b), default=0)
        incomplete = sum(1 for b in sym_b if not b.data_available)
        rows.append(
            {
                "symbol": sym,
                "buckets": len(sym_b),
                "incomplete_buckets": incomplete,
                "max_sequence_id": seq_max,
                "large_gap_bucket_starts": len(gaps),
                "known_outage_crossed": any(
                    b.gap_before_seconds and b.gap_before_seconds > 40 * 3600 for b in gaps
                ),
                "outage_window_utc": f"{outage_start.isoformat()}..{outage_end.isoformat()}",
            }
        )
    return rows


class DerivativesImporter:
    def __init__(
        self,
        *,
        source: DerivativeSourceAdapter,
        target: Any | None = None,
        memory: InMemoryDerivativeStore | None = None,
    ) -> None:
        self.source = source
        self.target = target
        self.memory = memory or InMemoryDerivativeStore()

    def run(self, req: ImportRequest) -> ImportResult:
        started = datetime.now(timezone.utc)
        accepted, unavailable = validate_symbols(req.symbols)
        result = ImportResult(
            mode=req.mode,
            status="running",
            unavailable_symbols=unavailable,
        )
        if unavailable:
            logger.warning("known-unavailable symbols skipped: %s", unavailable)

        raw_rows = list(
            self.source.iter_rows(
                symbols=accepted,
                start=req.start,
                end=req.end,
                chunk_size=req.chunk_size,
                row_limit=req.row_limit,
            )
        )
        agg = aggregate_rows(
            raw_rows,
            import_version=req.import_version,
            source_database=SOURCE_DATABASE_DEFAULT,
            source_table=SOURCE_TABLE,
        )
        result.rows_read = agg.rows_read
        result.buckets_generated = len(agg.buckets)
        result.rows_rejected = agg.rows_rejected
        result.symbols_completed = sorted({b.symbol for b in agg.buckets})

        recon = reconcile_source_vs_buckets(raw_rows, agg.buckets)
        result.reconciliation = {"by_symbol": recon}

        # OHLCV join if target store supports it
        ohlcv_map: dict[str, set[datetime]] = {s: set() for s in accepted}
        if self.target is not None and hasattr(self.target, "fetch_ohlcv_bucket_starts"):
            try:
                ohlcv_map = self.target.fetch_ohlcv_bucket_starts(
                    symbols=accepted, start=req.start, end=req.end
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("OHLCV join skipped: %s", exc)
                # Fallback: try CLI read of market_candles for dry-run reports
                ohlcv_map = _fetch_ohlcv_via_cli(accepted, req.start, req.end)

        else:
            ohlcv_map = _fetch_ohlcv_via_cli(accepted, req.start, req.end)

        join_rows = join_with_ohlcv(agg.buckets, ohlcv_map)
        result.join_stats = {"by_symbol": join_rows}

        out = req.output_dir
        out.mkdir(parents=True, exist_ok=True)
        files: list[str] = []

        sample = [b.to_dict() for b in agg.buckets[:50]]
        _write_csv(out / "pilot_bucket_sample.csv", sample)
        files.append("pilot_bucket_sample.csv")

        cov = [
            {
                "symbol": r["symbol"],
                "source_rows": r["source_rows"],
                "buckets": r["buckets"],
                "complete_buckets": r["complete_buckets"],
                "incomplete_buckets": r["incomplete_buckets"],
                "coverage_ratio_buckets": (
                    r["complete_buckets"] / r["buckets"] if r["buckets"] else None
                ),
            }
            for r in recon
        ]
        _write_csv(out / "pilot_coverage_by_symbol.csv", cov)
        files.append("pilot_coverage_by_symbol.csv")

        _write_csv(out / "pilot_gap_summary.csv", gap_summary(agg.buckets))
        files.append("pilot_gap_summary.csv")

        rejects = [
            {
                "symbol": r.symbol,
                "timestamp": r.timestamp,
                "reason": r.reason,
                "detail": r.detail,
                "exception_type": r.exception_type,
                "affected_field": r.affected_field,
                "source_python_type": r.source_python_type,
                "safe_example": r.safe_example,
                "category": r.category,
            }
            for r in agg.rejects
        ]
        # Also note incomplete buckets as soft rejects for audit
        soft = [
            {
                "symbol": b.symbol,
                "timestamp": b.bucket_start.isoformat().replace("+00:00", "Z"),
                "reason": b.reject_reason or "incomplete_bucket",
                "detail": f"source_row_count={b.source_row_count}",
                "exception_type": "",
                "affected_field": "",
                "source_python_type": "",
                "safe_example": "",
                "category": "domain_reject",
            }
            for b in agg.buckets
            if not b.data_available
        ]
        _write_csv(out / "pilot_rejects.csv", rejects + soft)
        files.append("pilot_rejects.csv")

        # Reject reason summary (hard rejects only)
        from collections import Counter

        reason_counts = Counter((r.reason, r.category, r.exception_type, r.affected_field, r.source_python_type) for r in agg.rejects)
        reject_summary = [
            {
                "reject_reason": reason,
                "count": count,
                "exception_type": exc_t,
                "affected_field": field,
                "source_python_type": py_t,
                "category": category,
                "safe_example": next(
                    (x.safe_example for x in agg.rejects if x.reason == reason and x.safe_example),
                    "",
                ),
            }
            for (reason, category, exc_t, field, py_t), count in sorted(
                reason_counts.items(), key=lambda kv: (-kv[1], kv[0][0])
            )
        ]
        _write_csv(out / "reject_reason_summary.csv", reject_summary)
        files.append("reject_reason_summary.csv")

        _write_csv(out / "pilot_reconciliation.csv", recon)
        files.append("pilot_reconciliation.csv")

        _write_csv(out / "pilot_join_with_ohlcv.csv", join_rows)
        files.append("pilot_join_with_ohlcv.csv")

        gate = validate_before_persist(
            mode=req.mode,
            rows_read=result.rows_read,
            agg=agg,
            buckets=agg.buckets,
            symbols_requested=accepted,
            reconciliation=recon,
            max_reject_rate=req.max_reject_rate,
            baseline_buckets=req.baseline_buckets,
        )
        if not gate.ok:
            result.status = gate.status
            result.error_message = gate.error_message
            logger.error("import validation failed: %s", gate.error_message)
            # Do NOT write fact tables
            finished = datetime.now(timezone.utc)
            integrity = {
                "mode": req.mode,
                "status": result.status,
                "error_message": result.error_message,
                "import_version": req.import_version,
                "import_label": req.import_label,
                "rows_read": result.rows_read,
                "buckets_generated": result.buckets_generated,
                "rows_rejected": result.rows_rejected,
                "gate": gate.details,
                "no_fact_writes": True,
                "no_secrets": True,
                "started_at": started.isoformat().replace("+00:00", "Z"),
                "finished_at": finished.isoformat().replace("+00:00", "Z"),
            }
            (out / "pilot_integrity.json").write_text(
                json.dumps(integrity, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            files.append("pilot_integrity.json")
            result.output_files = files
            # Record failed run metadata on persist attempts only
            if req.mode == "persist" and req.import_label and self.target is not None:
                self.target.record_import_run(
                    req.import_label,
                    {
                        "import_version": req.import_version,
                        "source_database": SOURCE_DATABASE_DEFAULT,
                        "source_table": SOURCE_TABLE,
                        "symbols_requested": accepted,
                        "symbols_completed": result.symbols_completed,
                        "status": result.status,
                        "dry_run": False,
                        "rows_read": result.rows_read,
                        "buckets_generated": result.buckets_generated,
                        "rows_inserted": 0,
                        "rows_updated": 0,
                        "rows_unchanged": 0,
                        "rows_rejected": result.rows_rejected,
                        "started_at": started.replace(tzinfo=None),
                        "finished_at": finished.replace(tzinfo=None),
                        "error_message": result.error_message,
                        "metadata_json": {"gate": gate.details, "no_fact_writes": True},
                    },
                )
            return result

        stats = UpsertStats()
        if req.mode == "dry_run":
            # Memory only — no fact table writes to target MySQL
            stats = self.memory.upsert_buckets(agg.buckets)
            result.rows_inserted = stats.inserted
            result.rows_updated = stats.updated
            result.rows_unchanged = stats.unchanged
            result.status = "dry_run_completed"
        elif req.mode == "persist":
            if self.target is None:
                raise RuntimeError("persist requires target store")
            if not req.import_label:
                raise ValueError("persist requires import_label")
            # Per-symbol transactions
            for sym in sorted({b.symbol for b in agg.buckets}):
                part = [b for b in agg.buckets if b.symbol == sym]
                s = self.target.upsert_buckets_for_symbol(part)
                stats.inserted += s.inserted
                stats.updated += s.updated
                stats.unchanged += s.unchanged
            result.rows_inserted = stats.inserted
            result.rows_updated = stats.updated
            result.rows_unchanged = stats.unchanged
            # Only mark persisted if something was actually written or confirmed unchanged
            if result.rows_inserted + result.rows_updated + result.rows_unchanged == 0:
                result.status = "failed_validation"
                result.error_message = "persist produced no fact-row writes"
            else:
                result.status = "persisted"
        elif req.mode == "verify_only":
            if self.target is None:
                raise RuntimeError("verify-only requires target store")
            existing = self.target.get_buckets(
                symbols=accepted, import_version=req.import_version, start=req.start, end=req.end
            )
            if not existing:
                result.status = "failed"
                result.error_message = "no target rows found for verify-only"
            else:
                by_key = {
                    (
                        str(r["symbol"]),
                        _ts_key(r["bucket_start"]),
                        str(r["import_version"]),
                    ): r
                    for r in existing
                }
                mismatches = 0
                matched = 0
                missing = 0
                for b in agg.buckets:
                    key = (
                        b.symbol,
                        b.bucket_start.isoformat().replace("+00:00", "Z"),
                        b.import_version,
                    )
                    # also try naive key
                    alt = (b.symbol, _ts_key(b.bucket_start), b.import_version)
                    row = by_key.get(key) or by_key.get(alt)
                    if row is None:
                        missing += 1
                        continue
                    if str(row.get("source_hash")) == b.source_hash:
                        matched += 1
                    else:
                        mismatches += 1
                result.reconciliation["verify"] = {
                    "matched_hashes": matched,
                    "mismatches": mismatches,
                    "missing_in_target": missing,
                    "target_rows": len(existing),
                }
                result.status = "verified" if mismatches == 0 and missing == 0 else "failed"
                if result.status == "failed":
                    result.error_message = "verify mismatches or missing rows"
            result.rows_unchanged = result.reconciliation.get("verify", {}).get("matched_hashes", 0)
        else:
            raise ValueError(f"unknown mode: {req.mode}")

        finished = datetime.now(timezone.utc)
        config_hash = json_hash(
            {
                "symbols": accepted,
                "start": req.start.isoformat(),
                "end": req.end.isoformat(),
                "import_version": req.import_version,
                "source_table": SOURCE_TABLE,
            }
        )
        source_query_hash = json_hash(
            {
                "database": SOURCE_DATABASE_DEFAULT,
                "table": SOURCE_TABLE,
                "symbols": accepted,
                "start": req.start.isoformat(),
                "end": req.end.isoformat(),
                "columns": list(
                    __import__(
                        "research.regime_scanner.derivatives.config",
                        fromlist=["SOURCE_SELECT_COLUMNS"],
                    ).SOURCE_SELECT_COLUMNS
                ),
            }
        )

        integrity = {
            "mode": req.mode,
            "status": result.status,
            "import_version": req.import_version,
            "import_label": req.import_label,
            "symbols_requested": list(req.symbols),
            "symbols_accepted": accepted,
            "symbols_unavailable": unavailable,
            "rows_read": result.rows_read,
            "buckets_generated": result.buckets_generated,
            "rows_rejected": result.rows_rejected,
            "rows_inserted": result.rows_inserted,
            "rows_updated": result.rows_updated,
            "rows_unchanged": result.rows_unchanged,
            "config_hash": config_hash,
            "source_query_hash": source_query_hash,
            "started_at": started.isoformat().replace("+00:00", "Z"),
            "finished_at": finished.isoformat().replace("+00:00", "Z"),
            "reconciliation_all_match": all(
                r["long_match"] and r["short_match"] and r["buy_match"] and r["sell_match"]
                for r in recon
            ),
            "join_by_symbol": join_rows,
            "pilot_default_symbols": list(PILOT_SYMBOLS),
            "no_secrets": True,
        }
        (out / "pilot_integrity.json").write_text(
            json.dumps(integrity, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        files.append("pilot_integrity.json")
        result.output_files = files

        # Persist import-run metadata only on persist (or optional dry-run flag)
        if req.mode == "persist" or (
            req.mode == "dry_run" and req.persist_import_run_on_dry_run and self.target
        ):
            if req.import_label and self.target is not None:
                self.target.record_import_run(
                    req.import_label,
                    {
                        "import_version": req.import_version,
                        "source_database": SOURCE_DATABASE_DEFAULT,
                        "source_table": SOURCE_TABLE,
                        "source_min_timestamp": min(
                            (b.source_first_timestamp for b in agg.buckets), default=None
                        ),
                        "source_max_timestamp": max(
                            (b.source_last_timestamp for b in agg.buckets), default=None
                        ),
                        "symbols_requested": accepted,
                        "symbols_completed": result.symbols_completed,
                        "status": result.status,
                        "dry_run": req.mode == "dry_run",
                        "source_query_hash": source_query_hash,
                        "config_hash": config_hash,
                        "rows_read": result.rows_read,
                        "buckets_generated": result.buckets_generated,
                        "rows_inserted": result.rows_inserted,
                        "rows_updated": result.rows_updated,
                        "rows_unchanged": result.rows_unchanged,
                        "rows_rejected": result.rows_rejected,
                        "started_at": started.replace(tzinfo=None),
                        "finished_at": finished.replace(tzinfo=None),
                        "error_message": result.error_message,
                        "metadata_json": {
                            "unavailable_symbols": unavailable,
                            "join_summary": join_rows,
                        },
                    },
                )
        elif req.mode == "dry_run":
            self.memory.record_import_run(
                req.import_label or "dry_run",
                {"status": result.status, "dry_run": True},
            )

        return result


def _ts_key(ts: Any) -> str:
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    s = str(ts).strip().replace(" ", "T")
    if s.endswith("Z"):
        return s
    if "+" not in s[10:]:
        return s + "Z" if "T" in s else s
    return s


def _fetch_ohlcv_via_cli(
    symbols: list[str],
    start: datetime,
    end: datetime,
) -> dict[str, set[datetime]]:
    """Best-effort OHLCV read via mysql CLI for dry-run join stats."""
    import subprocess

    out: dict[str, set[datetime]] = {s: set() for s in symbols}
    sym_list = ",".join(f"'{s}'" for s in symbols)
    start_s = start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    end_s = end.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    sql = (
        "SELECT symbol, open_time FROM regime_scanner_research.market_candles "
        f"WHERE exchange='bybit' AND timeframe='5m' AND symbol IN ({sym_list}) "
        f"AND open_time >= '{start_s}' AND open_time < '{end_s}'"
    )
    proc = subprocess.run(
        ["mysql", "-N", "-e", sql],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        logger.warning("OHLCV CLI read failed: %s", proc.stderr.strip()[:200])
        return out
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        sym, ot = parts
        dt = datetime.fromisoformat(ot.replace(" ", "T")).replace(tzinfo=timezone.utc)
        if sym in out:
            out[sym].add(dt)
    return out
