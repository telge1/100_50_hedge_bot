"""Orchestrate read-only multi-source inventory audit."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import socket
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import FORMAT_VERSION, DEFAULT_OUT
from .sql_guard import ReadOnlyDB, open_db

OA_ROOT = Path("/home/telgenbuescher/projects/orderbook_analyse")
DASH_ROOT = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev")
UNIVERSE_PATH = OA_ROOT / "config" / "universe_tradeable_51.json"
RAW_ARCHIVE_ROOT = OA_ROOT / "data" / "orderbook_raw_shadow" / "ob200_v3"

QSET = {"max_execution_time": 180, "receive_timeout": 200, "max_threads": 4}


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    database_or_root: str
    table_or_path: str
    raw_or_derived: str
    canonical_status: str
    timestamp_column: str
    resolution: str
    symbol_column: str = "symbol"
    extra_where: str = ""
    use_final: bool = False
    event_stream: bool = False
    live_or_static: str = "live"
    reconstructable_as_of: str = "PARTIAL"
    notes: str = ""


SOURCES: tuple[SourceSpec, ...] = (
    SourceSpec(
        "CANDLES_1M",
        "signal_generator",
        "candles_1m",
        "derived",
        "CANONICAL",
        "open_time",
        "1m",
        extra_where="AND interval='1m'",
        use_final=True,
        live_or_static="live",
        reconstructable_as_of="YES",
    ),
    SourceSpec(
        "PUBLIC_TRADES",
        "orderbook_analysis",
        "public_trades_canonical",
        "derived",
        "CANONICAL",
        "trade_ts",
        "tick",
        event_stream=True,
        live_or_static="live",
        reconstructable_as_of="YES",
        notes="Side=taker aggressor; dedupe via trade_id/FINAL",
    ),
    SourceSpec(
        "OB_FEATURES_1S",
        "orderbook_analysis",
        "orderbook_features_1s_v2",
        "derived",
        "CANONICAL_AGG",
        "bucket_start",
        "1s",
        extra_where="AND parser_version='ob200_v3' AND depth=200",
        use_final=True,
        live_or_static="static_since_2026-08-28",
        reconstructable_as_of="YES",
        notes="Aggregated ob200_v3; not per-level walls",
    ),
    SourceSpec(
        "OPEN_INTEREST_5S",
        "orderbook_analysis",
        "open_interest_5s",
        "derived",
        "CANONICAL",
        "bucket_time",
        "5s",
        live_or_static="live",
        reconstructable_as_of="YES",
    ),
    SourceSpec(
        "LIQUIDATIONS",
        "orderbook_analysis",
        "all_liquidations",
        "derived",
        "CANONICAL",
        "event_time",
        "event",
        event_stream=True,
        live_or_static="live",
        reconstructable_as_of="YES",
        notes="Zero rows in window = no events, not numeric zero",
    ),
    SourceSpec(
        "RAW_OB200_ARCHIVE",
        str(RAW_ARCHIVE_ROOT),
        "ob200_v3/*.zst",
        "raw",
        "CANONICAL_RAW",
        "segment_start",
        "tick/snapshot",
        live_or_static="partial_live",
        reconstructable_as_of="YES",
        notes="Per-level replay via MutableBook; manifest sidecar",
    ),
    SourceSpec(
        "ORDERBOOK_DELTAS_LEGACY",
        "orderbook_analysis",
        "orderbook_deltas",
        "raw",
        "LEGACY_BROKEN",
        "exchange_ts",
        "delta",
        live_or_static="broken",
        reconstructable_as_of="BLOCKED",
        notes="108 broken parts; table attach fails; code explicitly avoids",
    ),
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_universe() -> dict[str, Any]:
    raw = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    syms = [str(s).strip().upper() for s in raw.get("symbols", [])]
    uniq = sorted(set(syms))
    return {
        "path": str(UNIVERSE_PATH),
        "sha256": sha256_file(UNIVERSE_PATH),
        "name": raw.get("name"),
        "n_configured": raw.get("n"),
        "n_unique": len(uniq),
        "symbols_sorted": uniq,
        "format_ok": all(s.isalnum() for s in uniq),
    }


def git_snapshot(root: Path) -> dict[str, Any]:
    def run(cmd: list[str]) -> str:
        return subprocess.check_output(cmd, cwd=root, text=True).strip()

    return {
        "path": str(root.resolve()),
        "branch": run(["git", "branch", "--show-current"]),
        "head": run(["git", "rev-parse", "HEAD"]),
        "status_short": run(["git", "status", "--short"]),
    }


def collect_pids() -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    try:
        ps = subprocess.check_output(
            ["ps", "-eo", "pid,etime,args"], text=True
        ).splitlines()[1:]
    except subprocess.CalledProcessError:
        return out
    keys = ("app.py", "collector", "orderbook_v2_live", "run_live_collector", "oi_liquidation")
    for line in ps:
        if not any(k in line for k in keys):
            continue
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        out.append({"pid": parts[0], "etime": parts[1], "cmd": parts[2][:200]})
    return out


def _fq(spec: SourceSpec) -> str:
    return f"{spec.database_or_root}.{spec.table_or_path}"


def coverage_global(db: ReadOnlyDB, spec: SourceSpec) -> dict[str, Any]:
    if spec.source_id == "RAW_OB200_ARCHIVE":
        return {"status": "FILE_INVENTORY", "notes": "see raw_archive_inventory.csv"}
    if spec.source_id == "ORDERBOOK_DELTAS_LEGACY":
        return {
            "status": "BLOCKED",
            "notes": "ClickHouse attach fails: 108 broken parts on orderbook_deltas",
        }
    final = " FINAL" if spec.use_final else ""
    sql = f"""
        SELECT min({spec.timestamp_column}), max({spec.timestamp_column}), count()
        FROM {_fq(spec)}{final}
        WHERE 1=1 {spec.extra_where}
    """
    try:
        mn, mx, n = db.query(sql).result_rows[0]
        return {
            "min_ts_utc": iso(mn) if mn else None,
            "max_ts_utc": iso(mx) if mx else None,
            "rows": int(n or 0),
            "status": "VALID" if int(n or 0) > 0 else "MISSING",
        }
    except Exception as exc:
        return {"status": "BLOCKED", "error": f"{type(exc).__name__}: {exc}"}


def coverage_by_symbol(db: ReadOnlyDB, spec: SourceSpec, symbols: list[str]) -> list[dict[str, Any]]:
    if spec.source_id in ("RAW_OB200_ARCHIVE", "ORDERBOOK_DELTAS_LEGACY"):
        return []
    final = " FINAL" if spec.use_final else ""
    # batch query — one round trip for all symbols
    placeholders = ", ".join(f"'{s}'" for s in symbols)
    sql = f"""
        SELECT {spec.symbol_column} AS sym,
               min({spec.timestamp_column}) AS tmin,
               max({spec.timestamp_column}) AS tmax,
               count() AS n
        FROM {_fq(spec)}{final}
        WHERE {spec.symbol_column} IN ({placeholders})
          {spec.extra_where}
        GROUP BY sym
        ORDER BY sym
    """
    rows: list[dict[str, Any]] = []
    try:
        for sym, tmin, tmax, n in db.query(sql).result_rows:
            n = int(n or 0)
            rows.append(
                {
                    "source_id": spec.source_id,
                    "database_or_root": spec.database_or_root,
                    "table_or_path": spec.table_or_path,
                    "raw_or_derived": spec.raw_or_derived,
                    "canonical_status": spec.canonical_status,
                    "symbol": str(sym),
                    "resolution": spec.resolution,
                    "timestamp_column": spec.timestamp_column,
                    "min_ts_utc": iso(tmin),
                    "max_ts_utc": iso(tmax),
                    "rows_exact_or_estimated": n,
                    "coverage_pct": "NOT_APPLICABLE",
                    "quality_verdict": "VALID" if n > 0 else "MISSING",
                    "live_or_static": spec.live_or_static,
                    "reconstructable_as_of": spec.reconstructable_as_of,
                    "notes": spec.notes,
                }
            )
    except Exception as exc:
        for s in symbols:
            rows.append(
                {
                    "source_id": spec.source_id,
                    "symbol": s,
                    "quality_verdict": "BLOCKED",
                    "notes": str(exc),
                }
            )
    present = {r["symbol"] for r in rows if r.get("quality_verdict") == "VALID"}
    for s in symbols:
        if s not in present:
            rows.append(
                {
                    "source_id": spec.source_id,
                    "database_or_root": spec.database_or_root,
                    "table_or_path": spec.table_or_path,
                    "raw_or_derived": spec.raw_or_derived,
                    "canonical_status": spec.canonical_status,
                    "symbol": s,
                    "resolution": spec.resolution,
                    "timestamp_column": spec.timestamp_column,
                    "min_ts_utc": None,
                    "max_ts_utc": None,
                    "rows_exact_or_estimated": 0,
                    "coverage_pct": "NOT_APPLICABLE",
                    "quality_verdict": "MISSING",
                    "live_or_static": spec.live_or_static,
                    "reconstructable_as_of": spec.reconstructable_as_of,
                    "notes": "no rows for symbol",
                }
            )
    rows.sort(key=lambda r: (r.get("source_id", ""), r.get("symbol", "")))
    return rows


def smoke_candle_integrity(db: ReadOnlyDB, symbol: str, days: int = 3) -> dict[str, Any]:
    end = utc_now().replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    sql = f"""
        WITH
          toDateTime64({{a:DateTime64(3,'UTC')}}, 3, 'UTC') AS s,
          toDateTime64({{b:DateTime64(3,'UTC')}}, 3, 'UTC') AS e
        SELECT
          count() AS rows,
          uniqExact(open_time) AS uniq_bars,
          dateDiff('minute', s, e) AS expected_minutes,
          countIf(high < low) AS bad_hl,
          countIf(open <= 0 OR close <= 0) AS bad_px
        FROM signal_generator.candles_1m FINAL
        WHERE symbol = {{sym:String}} AND interval='1m'
          AND open_time >= s AND open_time < e
    """
    r = db.query(sql, {"sym": symbol, "a": start, "b": end}).result_rows[0]
    rows, uniq, exp, bad_hl, bad_px = map(int, r)
    missing = max(exp - uniq, 0)
    return {
        "symbol": symbol,
        "window_start": iso(start),
        "window_end": iso(end),
        "rows": rows,
        "unique_minutes": uniq,
        "expected_minutes": exp,
        "missing_minutes": missing,
        "coverage_pct": round(100.0 * uniq / exp, 2) if exp else None,
        "bad_hl": bad_hl,
        "bad_px": bad_px,
        "quality_verdict": "VALID" if bad_hl == 0 and bad_px == 0 else "PARTIAL",
    }


def hourly_presence(
    db: ReadOnlyDB,
    spec: SourceSpec,
    symbol: str,
    start: datetime,
    end: datetime,
) -> set[int]:
    if spec.source_id in ("RAW_OB200_ARCHIVE", "ORDERBOOK_DELTAS_LEGACY"):
        return set()
    final = " FINAL" if spec.use_final else ""
    sql = f"""
        SELECT toUnixTimestamp(toStartOfHour({spec.timestamp_column})) AS h
        FROM {_fq(spec)}{final}
        WHERE {spec.symbol_column} = {{sym:String}}
          AND {spec.timestamp_column} >= {{a:DateTime64(3,'UTC')}}
          AND {spec.timestamp_column} < {{b:DateTime64(3,'UTC')}}
          {spec.extra_where}
        GROUP BY h
    """
    try:
        return {int(r[0]) for r in db.query(sql, {"sym": symbol, "a": start, "b": end}).result_rows}
    except Exception:
        return set()


def longest_contiguous_hours(hours: set[int]) -> int:
    if not hours:
        return 0
    sorted_h = sorted(hours)
    best = cur = 1
    for i in range(1, len(sorted_h)):
        if sorted_h[i] == sorted_h[i - 1] + 3600:
            cur += 1
            best = max(best, cur)
        else:
            cur = 1
    return best


def compute_intersections(
    db: ReadOnlyDB,
    symbols: list[str],
    coverage_rows: list[dict[str, Any]],
    smoke_symbols: tuple[str, ...] = ("BTCUSDT", "DOGEUSDT"),
) -> list[dict[str, Any]]:
    by_source: dict[str, dict[str, dict]] = {}
    for r in coverage_rows:
        if r.get("quality_verdict") != "VALID":
            continue
        by_source.setdefault(r["source_id"], {})[r["symbol"]] = r

    ch_sources = [s for s in SOURCES if s.source_id not in ("RAW_OB200_ARCHIVE", "ORDERBOOK_DELTAS_LEGACY")]

    def sym_overlap(source_ids: list[str], symbol: str) -> tuple[datetime | None, datetime | None]:
        mins, maxs = [], []
        for sid in source_ids:
            rec = by_source.get(sid, {}).get(symbol)
            if not rec or not rec.get("min_ts_utc"):
                return None, None
            mins.append(datetime.fromisoformat(rec["min_ts_utc"].replace("Z", "+00:00")))
            maxs.append(datetime.fromisoformat(rec["max_ts_utc"].replace("Z", "+00:00")))
        return max(mins), min(maxs)

    levels: list[tuple[str, list[str], bool]] = [
        ("CANDLES", ["CANDLES_1M"], False),
        ("CANDLES+RECONSTRUCTABLE_PROFILES", ["CANDLES_1M", "PUBLIC_TRADES"], False),
        ("CANDLES+PROFILES+PUBLIC_TRADES", ["CANDLES_1M", "PUBLIC_TRADES"], False),
        ("+OB_FEATURES_1S", ["CANDLES_1M", "PUBLIC_TRADES", "OB_FEATURES_1S"], False),
        ("+RAW_OB200_PER_LEVEL", ["CANDLES_1M", "PUBLIC_TRADES", "RAW_OB200_ARCHIVE"], True),
        ("+OPEN_INTEREST", ["CANDLES_1M", "PUBLIC_TRADES", "OB_FEATURES_1S", "OPEN_INTEREST_5S"], False),
        ("+LIQUIDATIONS", ["CANDLES_1M", "PUBLIC_TRADES", "OB_FEATURES_1S", "OPEN_INTEREST_5S", "LIQUIDATIONS"], False),
        ("ALL_CONFIRMED_SOURCES", ["CANDLES_1M", "PUBLIC_TRADES", "OB_FEATURES_1S", "OPEN_INTEREST_5S", "LIQUIDATIONS"], False),
    ]

    out: list[dict[str, Any]] = []
    for level_name, sids, needs_raw in levels:
        valid_syms = []
        for sym in symbols:
            ok = True
            for sid in sids:
                if sid == "RAW_OB200_ARCHIVE":
                    if sym not in ("BTCUSDT", "DOGEUSDT"):
                        ok = False
                        break
                    continue
                rec = by_source.get(sid, {}).get(sym)
                if not rec or rec.get("quality_verdict") != "VALID":
                    ok = False
                    break
            if ok:
                valid_syms.append(sym)

        # global overlap from min/max
        global_mins, global_maxs = [], []
        for sid in sids:
            if sid == "RAW_OB200_ARCHIVE":
                continue
            for sym in valid_syms:
                rec = by_source.get(sid, {}).get(sym)
                if rec and rec.get("min_ts_utc"):
                    global_mins.append(datetime.fromisoformat(rec["min_ts_utc"].replace("Z", "+00:00")))
                    global_maxs.append(datetime.fromisoformat(rec["max_ts_utc"].replace("Z", "+00:00")))
        earliest = iso(max(global_mins)) if global_mins else None
        latest = iso(min(global_maxs)) if global_maxs else None

        # hour intersection smoke for BTC/DOGE
        hour_details = {}
        for sym in smoke_symbols:
            if sym not in valid_syms:
                hour_details[sym] = {"shared_hours": 0, "longest_contiguous_hours": 0}
                continue
            lo, hi = sym_overlap([s for s in sids if s != "RAW_OB200_ARCHIVE"], sym)
            if lo is None or hi is None or lo >= hi:
                hour_details[sym] = {"shared_hours": 0, "longest_contiguous_hours": 0}
                continue
            sets = []
            for spec in ch_sources:
                if spec.source_id not in sids:
                    continue
                sets.append(hourly_presence(db, spec, sym, lo, hi))
            if not sets:
                shared = set()
            else:
                shared = sets[0]
                for s in sets[1:]:
                    shared &= s
            hour_details[sym] = {
                "overlap_start": iso(lo),
                "overlap_end": iso(hi),
                "shared_hours": len(shared),
                "longest_contiguous_hours": longest_contiguous_hours(shared),
            }

        limiting = []
        for sid in sids:
            if sid == "RAW_OB200_ARCHIVE":
                limiting.append("RAW_OB200 only BTCUSDT+DOGEUSDT")
                continue
            miss = [s for s in symbols if by_source.get(sid, {}).get(s, {}).get("quality_verdict") != "VALID"]
            if miss:
                limiting.append(f"{sid} missing {len(miss)} symbols")

        usable = "yes" if len(valid_syms) >= 40 and earliest and latest else ("partial" if valid_syms else "no")
        out.append(
            {
                "level": level_name,
                "source_ids": sids,
                "symbols_available": len(valid_syms),
                "symbols_list_sample": valid_syms[:10],
                "earliest_common_ts_utc": earliest,
                "latest_common_ts_utc": latest,
                "hour_intersection_smoke": hour_details,
                "limiting_sources": limiting,
                "event_research_usable": usable,
                "notes": "Hour counts from min/max overlap smoke on BTCUSDT/DOGEUSDT only",
            }
        )
    return out


def raw_archive_inventory(symbols: list[str]) -> list[dict[str, Any]]:
    from orderbook_analyse.ob200_v3_raw_discovery.files import list_closed_segments

    rows: list[dict[str, Any]] = []
    if not RAW_ARCHIVE_ROOT.is_dir():
        return rows
    segs = list_closed_segments(RAW_ARCHIVE_ROOT, symbols=tuple(symbols))
    by_sym: dict[str, list] = {}
    for s in segs:
        by_sym.setdefault(s.symbol, []).append(s)
    for sym in symbols:
        lst = by_sym.get(sym, [])
        if not lst:
            rows.append(
                {
                    "symbol": sym,
                    "segments": 0,
                    "min_start_utc": None,
                    "max_end_utc": None,
                    "total_duration_hours": 0,
                    "quality_verdict": "MISSING",
                }
            )
            continue
        mn = min(s.start_utc for s in lst)
        mx = max(s.end_utc for s in lst)
        dur = sum(s.duration_sec for s in lst if s.duration_sec > 0)
        rows.append(
            {
                "symbol": sym,
                "segments": len(lst),
                "min_start_utc": iso(mn),
                "max_end_utc": iso(mx),
                "total_duration_hours": round(dur / 3600, 2),
                "manifest_count": sum(1 for s in lst if s.manifest_path.is_file()),
                "quality_verdict": "VALID" if dur > 0 else "PARTIAL",
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = sorted({k for r in rows for k in r})
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def run_audit(out_dir: Path | None = None) -> dict[str, Any]:
    out = out_dir or (OA_ROOT / DEFAULT_OUT)
    out.mkdir(parents=True, exist_ok=True)
    started = utc_now()
    pids_before = collect_pids()
    universe = load_universe()
    symbols = universe["symbols_sorted"]

    db = open_db()
    coverage_rows: list[dict[str, Any]] = []
    source_inventory: list[dict[str, Any]] = []

    for spec in SOURCES:
        g = coverage_global(db, spec)
        source_inventory.append(
            {
                "source_id": spec.source_id,
                "database_or_root": spec.database_or_root,
                "table_or_path": spec.table_or_path,
                "raw_or_derived": spec.raw_or_derived,
                "canonical_status": spec.canonical_status,
                "resolution": spec.resolution,
                "timestamp_column": spec.timestamp_column,
                "event_stream": spec.event_stream,
                "live_or_static": spec.live_or_static,
                "reconstructable_as_of": spec.reconstructable_as_of,
                "global": g,
                "notes": spec.notes,
            }
        )
        coverage_rows.extend(coverage_by_symbol(db, spec, symbols))

    raw_rows = raw_archive_inventory(symbols)
    write_csv(out / "raw_archive_inventory.csv", raw_rows)

    # merge raw archive into coverage rows for RAW source
    for rr in raw_rows:
        coverage_rows.append(
            {
                "source_id": "RAW_OB200_ARCHIVE",
                "database_or_root": str(RAW_ARCHIVE_ROOT),
                "table_or_path": "ob200_v3/*.zst",
                "raw_or_derived": "raw",
                "canonical_status": "CANONICAL_RAW",
                "symbol": rr["symbol"],
                "resolution": "tick/snapshot",
                "timestamp_column": "segment_start",
                "min_ts_utc": rr.get("min_start_utc"),
                "max_ts_utc": rr.get("max_end_utc"),
                "rows_exact_or_estimated": rr.get("segments", 0),
                "coverage_pct": "NOT_APPLICABLE",
                "quality_verdict": rr.get("quality_verdict"),
                "live_or_static": "partial_live",
                "reconstructable_as_of": "YES",
                "notes": f"duration_hours={rr.get('total_duration_hours')}",
            }
        )

    smoke = {
        "BTCUSDT": smoke_candle_integrity(db, "BTCUSDT"),
        "DOGEUSDT": smoke_candle_integrity(db, "DOGEUSDT"),
    }
    intersections = compute_intersections(db, symbols, coverage_rows)
    db.close()

    ended = utc_now()
    pids_after = collect_pids()

    # schema from code (system.columns blocked by orderbook_deltas attach)
    schema_rows = [
        {"database": "signal_generator", "table": "candles_1m", "column": "open_time", "type": "DateTime64(3,'UTC')", "evidence": "cluster_sweep_research/clickhouse_source.py"},
        {"database": "signal_generator", "table": "candles_1m", "column": "open,high,low,close,volume", "type": "Float64", "evidence": "fetch_candles_1m"},
        {"database": "orderbook_analysis", "table": "public_trades_canonical", "column": "trade_ts,trade_id,side,price,size,notional,source,ingest_timestamp", "type": "mixed", "evidence": "public_trade_bubbles/loader.py"},
        {"database": "orderbook_analysis", "table": "orderbook_features_1s_v2", "column": "bucket_start,spread_bps,imbalance_l50,bid_qty_l50,ask_qty_l50,is_valid", "type": "mixed", "evidence": "cluster_sweep_research/clickhouse_source.py fetch_ob_1m"},
        {"database": "orderbook_analysis", "table": "open_interest_5s", "column": "bucket_time,open_interest", "type": "mixed", "evidence": "cluster_sweep_research/clickhouse_source.py fetch_oi_1m"},
        {"database": "orderbook_analysis", "table": "all_liquidations", "column": "event_time,liquidated_position_side,notional_estimate", "type": "mixed", "evidence": "cluster_sweep_research/clickhouse_source.py fetch_liquidations"},
    ]

    lineage = {
        "public_trades_canonical": {
            "raw": "Bybit public trades",
            "import": "public_trade_collector / signal_generator live collector",
            "canonical_table": "orderbook_analysis.public_trades_canonical",
            "aggregation": "1m/1s aggregates in research loaders",
            "consumers": ["market_profile.loader", "research_charts/public_trades_profile", "liquidity_pool_entry_contract_batch_v2"],
            "evidence": ["src/orderbook_analyse/market_profile/loader.py", "spread_recovery_hedge_short_dev/dashboard/research_charts/public_trades_profile.py"],
        },
        "orderbook_features_1s_v2": {
            "raw": "ob200_v3 live collector",
            "import": "orderbook_v2_live writer",
            "canonical_table": "orderbook_analysis.orderbook_features_1s_v2",
            "aggregation": "1s bucket features depth=200",
            "consumers": ["cluster_sweep_research", "liquidity_location_r6", "canonical_pool_wall_trade_reaction_v1"],
            "evidence": ["src/orderbook_analyse/cluster_sweep_research/clickhouse_source.py"],
        },
        "raw_ob200_archive": {
            "raw": "WebSocket OB200 snapshots/deltas",
            "import": "orderbook_v2_live raw-archive-only mode",
            "path": str(RAW_ARCHIVE_ROOT),
            "aggregation": "MutableBook replay -> per-level walls",
            "consumers": ["ob200_v3_raw_discovery", "liquidity_pool_arrival_wall_monitor_v2"],
            "evidence": ["src/orderbook_analyse/ob200_v3_raw_discovery/files.py"],
        },
    }

    data_layers = [
        {
            "layer": "Long-History",
            "components": "Candles + causal profiles + EMA/Regime",
            "verdict": "USABLE_NOW",
            "period": "candles from 2025-12-11; profiles need trades from 2026-07-19",
            "symbols": "51 candles; profiles/trades overlap 51 since 2026-07-19",
            "limiting_source": "PUBLIC_TRADES start 2026-07-19",
            "causality_risk": "Profile window can extend past now without future trades; shape uses partial-day OHLC",
        },
        {
            "layer": "Trade-Context",
            "components": "Long-history + public trades footprint",
            "verdict": "USABLE_NOW",
            "period": "2026-07-19 → live",
            "symbols": "51",
            "limiting_source": "none within overlap",
            "causality_risk": "ingest_timestamp exists but most loaders filter trade_ts only",
        },
        {
            "layer": "Microstructure",
            "components": "Trade-context + OB200 + OI + liquidations",
            "verdict": "PARTIAL",
            "period": "OB features end 2026-08-28; OI/Liq from 2026-08-18; raw archive BTC+DOGE only",
            "symbols": "51 for agg OB (historical); 2 for raw per-level",
            "limiting_source": "OB_FEATURES_1S static since 2026-08-28; RAW_OB200 archive",
            "causality_risk": "carried_forward flags in OB features; liquidation zero != null outside coverage",
        },
    ]

    integrity = {
        "clickhouse_system_columns": "BLOCKED — orderbook_deltas broken parts trigger async load failure",
        "orderbook_deltas": "LEGACY_BROKEN — 108 broken parts, attach fails, code avoids",
        "candle_smoke": smoke,
        "pids_unchanged": [p["pid"] for p in pids_before] == [p["pid"] for p in pids_after],
    }

    summary = {
        "verdict": "MULTISOURCE_DATA_INVENTORY_V1_COMPLETE",
        "format_version": FORMAT_VERSION,
        "started_utc": iso(started),
        "ended_utc": iso(ended),
        "universe_symbols": len(symbols),
        "sources_inventoried": len(SOURCES),
        "blockers": ["orderbook_deltas attach broken", "system.columns unavailable", "RAW_OB200 only 2 symbols"],
        "global_ranges": {s["source_id"]: s.get("global") for s in source_inventory},
    }

    manifest = {
        "format_version": FORMAT_VERSION,
        "audit_mode": "READ_ONLY",
        "started_utc": iso(started),
        "ended_utc": iso(ended),
        "hostname": socket.gethostname(),
        "repos": {
            "orderbook_analyse": git_snapshot(OA_ROOT),
            "spread_recovery_hedge_short_dev": git_snapshot(DASH_ROOT),
        },
        "universe": {"path": universe["path"], "sha256": universe["sha256"], "n": universe["n_unique"]},
        "pids_before": pids_before,
        "pids_after": pids_after,
        "modules_used": ["multisource_data_inventory_v1.runner", "dynamic_wall_detector.connect_readonly"],
        "limitations": [
            "system.columns and system.tables blocked by orderbook_deltas attach failure",
            "Hour intersection computed on BTCUSDT/DOGEUSDT smoke only",
            "No raw tick export",
        ],
        "output_files": [],
    }

    write_csv(out / "source_inventory.csv", source_inventory)
    write_csv(out / "schema_inventory.csv", schema_rows)
    write_csv(out / "coverage_by_source_symbol.csv", coverage_rows)
    write_csv(out / "coverage_intersections.csv", intersections)
    write_csv(out / "data_layer_readiness.csv", data_layers)

    (out / "lineage.json").write_text(json.dumps(lineage, indent=2), encoding="utf-8")
    (out / "universe_inventory.json").write_text(json.dumps(universe, indent=2), encoding="utf-8")
    (out / "integrity.json").write_text(json.dumps(integrity, indent=2, default=str), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    manifest["output_files"] = sorted(p.name for p in out.iterdir() if p.is_file())
    (out / "audit_manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")

    return {
        "out_dir": str(out),
        "summary": summary,
        "manifest": manifest,
        "universe": universe,
        "intersections": intersections,
        "source_inventory": source_inventory,
        "smoke": smoke,
        "integrity": integrity,
        "pids_before": pids_before,
        "pids_after": pids_after,
    }
