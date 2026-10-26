#!/usr/bin/env python3
"""Phase 4U — read-only ClickHouse historical discovery (DOGEUSDT)."""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DASH = ROOT / "backtester" / "short" / "dashboard"
OUT = ROOT / "results" / "phase4u_clickhouse_historical_discovery_v1"
MULTI = (
    DASH
    / "research_charts/live_micro_lld_v2_ch_test/doge_multi_month_2026_history/production/results"
)
APR_SEP_5M = (
    DASH
    / "research_charts/live_micro_lld_v2_ch_test/doge_apr_sep_2026_v1_history/production/results"
)

for p in (DASH, ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect  # noqa: E402

from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.pool_catalog_schema import (  # noqa: E402
    TABLE_ACTIVE_POOL,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.publish_schema import (  # noqa: E402
    TABLE_PUBLISH,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.schema import (  # noqa: E402
    TABLE_SNAPSHOT_MEMBER,
    TABLE_SNAPSHOT_META,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.provenance_sidecar_design import (  # noqa: E402
    TABLE_IMPORT_PROVENANCE,
)
from research_charts.live_micro_lld_v2_ch_test.schema import (  # noqa: E402
    TABLE_POOL,
    TABLE_RUN,
)

LINE_5M_PD1 = "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1"
CFG_HASH_5M_PD1 = "4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9"

MONTHS_2026 = [(2026, m) for m in range(1, 10)]

RELEVANT_NAME_FRAGMENTS = (
    "candle",
    "lmv2",
    "pool",
    "publish",
    "snapshot",
    "doge",
    "research",
    "active",
    "provenance",
    "origin",
    "remaining",
    "ohlcv",
    "orderbook",
)


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _close_stem(dt: datetime) -> str:
    return _utc(dt).strftime("%Y%m%dT%H%M%SZ")


def expected_5m_closes(y: int, m: int) -> int:
    return monthrange(y, m)[1] * 288


def _wcsv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = sorted({k for r in rows for k in r})
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _wjson(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


@dataclass
class ChCtx:
    client: Any
    database: str
    candle_table: str
    exchange: str


def connect() -> ChCtx:
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    return ChCtx(
        client=client,
        database=cfg.database,
        candle_table=cfg.table,
        exchange=cfg.exchange,
    )


def list_databases(client) -> list[str]:
    return [r[0] for r in client.query("SHOW DATABASES").result_rows]


def table_inventory(client, databases: list[str]) -> tuple[list[dict], list[dict]]:
    db_rows: list[dict] = []
    tbl_rows: list[dict] = []
    for db in databases:
        if db in ("system", "INFORMATION_SCHEMA", "information_schema"):
            continue
        try:
            tables = client.query(f"SHOW TABLES FROM `{db}`").result_rows
        except Exception as exc:
            db_rows.append({"database": db, "table_count": 0, "note": f"SHOW TABLES failed: {type(exc).__name__}"})
            continue
        names = [t[0] for t in tables]
        db_rows.append({"database": db, "table_count": len(names), "note": ""})
        for name in names:
            low = name.lower()
            relevance = any(f in low for f in RELEVANT_NAME_FRAGMENTS)
            row_count = ""
            engine = ""
            try:
                desc = client.query(
                    f"""
                    SELECT engine, total_rows, total_bytes
                    FROM system.tables
                    WHERE database = {{db:String}} AND name = {{name:String}}
                    """,
                    parameters={"db": db, "name": name},
                ).first_row
                if desc:
                    engine = str(desc[0])
                    row_count = str(desc[1])
            except Exception:
                pass
            columns = ""
            try:
                cols = client.query(f"DESCRIBE TABLE `{db}`.`{name}`").result_rows
                columns = ";".join(f"{c[0]}:{c[1]}" for c in cols[:40])
                if len(cols) > 40:
                    columns += ";…"
            except Exception:
                columns = "DESCRIBE failed"
            tbl_rows.append(
                {
                    "database": db,
                    "table_name": name,
                    "engine": engine,
                    "approx_total_rows": row_count,
                    "relevance_heuristic": "yes" if relevance else "maybe",
                    "column_schema_sample": columns,
                }
            )
    return db_rows, tbl_rows


def doge_candle_monthly(ctx: ChCtx) -> list[dict]:
    rows: list[dict] = []
    q = f"""
    SELECT
        toStartOfMonth(open_time) AS mo,
        count() AS n,
        min(open_time) AS mn,
        max(open_time) AS mx,
        countDistinct(open_time) AS distinct_ts
    FROM {ctx.database}.{ctx.candle_table} FINAL
    WHERE exchange = {{ex:String}}
      AND symbol = 'DOGEUSDT'
      AND interval = '1m'
      AND is_closed = 1
      AND open_time >= '2026-01-01' AND open_time < '2026-10-01'
    GROUP BY mo
    ORDER BY mo
    """
    for r in client_query(ctx, q, {"ex": ctx.exchange}).named_results():
        mo = r["mo"]
        y, m = int(str(mo)[:4]), int(str(mo)[5:7])
        exp = monthrange(y, m)[1] * 24 * 60
        rows.append(
            {
                "layer": "A_1m_OHLCV",
                "database": ctx.database,
                "table_name": ctx.candle_table,
                "data_type": "candle_1m",
                "timeframe": "1m",
                "pool_detail": "",
                "pipeline_line_id": "",
                "month": f"{y:04d}-{m:02d}",
                "earliest_utc": str(r["mn"]),
                "latest_utc": str(r["mx"]),
                "point_count": r["n"],
                "distinct_5m_closes": "",
                "expected_5m_closes": expected_5m_closes(y, m),
                "missing_vs_expected": max(0, expected_5m_closes(y, m) - int(r["n"] // 5)),
                "duplicates_note": "ok" if r["n"] == r["distinct_ts"] else f"n!=distinct:{r['distinct_ts']}",
                "active_vs_remaining": "price_only",
                "provenance": "collector_candles_1m",
                "backtester_suitability": "price_axis_only_not_pool_state",
            }
        )
    return rows


def client_query(ctx: ChCtx, q: str, params: dict | None = None):
    return ctx.client.query(q, parameters=params or {})


def pipeline_inventory(ctx: ChCtx) -> list[dict]:
    q = f"""
    SELECT
        pipeline_line_id,
        symbol,
        timeframe,
        pool_detail,
        config_hash,
        count() AS n,
        min(decision_close) AS mn,
        max(decision_close) AS mx,
        countDistinct(decision_close) AS distinct_closes
    FROM {ctx.database}.{TABLE_PUBLISH} FINAL
    WHERE publish_complete = 1
      AND (symbol = 'DOGEUSDT' OR pipeline_line_id LIKE '%doge%' OR pipeline_line_id LIKE '%DOGE%')
    GROUP BY pipeline_line_id, symbol, timeframe, pool_detail, config_hash
    ORDER BY pipeline_line_id, timeframe, pool_detail
    """
    rows = []
    for r in client_query(ctx, q).named_results():
        rows.append(
            {
                "pipeline_line_id": r["pipeline_line_id"],
                "symbol": r["symbol"],
                "timeframe": r["timeframe"],
                "pool_detail": r["pool_detail"],
                "config_hash": r["config_hash"],
                "published_closes": r["n"],
                "distinct_decision_closes": r["distinct_closes"],
                "earliest_decision_close_utc": str(r["mn"]),
                "latest_decision_close_utc": str(r["mx"]),
                "duplicates": "yes" if r["n"] != r["distinct_closes"] else "no",
            }
        )
    return rows


def publish_monthly_by_pipeline(ctx: ChCtx) -> list[dict]:
    rows: list[dict] = []
    q = f"""
    SELECT
        pipeline_line_id,
        timeframe,
        pool_detail,
        toStartOfMonth(decision_close) AS mo,
        count() AS n,
        min(decision_close) AS mn,
        max(decision_close) AS mx,
        countDistinct(decision_close) AS distinct_closes
    FROM {ctx.database}.{TABLE_PUBLISH} FINAL
    WHERE publish_complete = 1
      AND symbol = 'DOGEUSDT'
      AND decision_close >= '2026-01-01' AND decision_close < '2026-10-01'
    GROUP BY pipeline_line_id, timeframe, pool_detail, mo
    ORDER BY pipeline_line_id, mo
    """
    for r in client_query(ctx, q).named_results():
        mo = r["mo"]
        y, m = int(str(mo)[:4]), int(str(mo)[5:7])
        exp = expected_5m_closes(y, m) if r["timeframe"] == "5m" else ""
        missing = ""
        if exp and r["timeframe"] == "5m":
            missing = max(0, exp - int(r["distinct_closes"]))
        layer = "E_ACTIVE_POOL_LANDSCAPE_PUBLISH"
        if r["timeframe"] == "5m" and int(r["pool_detail"]) == 1:
            layer = "C_5m_PD1_PUBLISHED_LEDGER"
        rows.append(
            {
                "layer": layer,
                "database": ctx.database,
                "table_name": TABLE_PUBLISH,
                "data_type": "publish_ledger",
                "timeframe": r["timeframe"],
                "pool_detail": r["pool_detail"],
                "pipeline_line_id": r["pipeline_line_id"],
                "month": f"{y:04d}-{m:02d}",
                "earliest_utc": str(r["mn"]),
                "latest_utc": str(r["mx"]),
                "point_count": r["n"],
                "distinct_5m_closes": r["distinct_closes"] if r["timeframe"] == "5m" else "",
                "expected_5m_closes": exp,
                "missing_vs_expected": missing,
                "duplicates_note": "yes" if r["n"] != r["distinct_closes"] else "no",
                "active_vs_remaining": "ACTIVE_publish_only",
                "provenance": "clickhouse_ssot_publish",
                "backtester_suitability": _backtest_suitability(r["pipeline_line_id"], r["timeframe"], int(r["pool_detail"]), int(r["distinct_closes"]), y, m),
            }
        )
    return rows


def _backtest_suitability(line: str, tf: str, pd: int, distinct: int, y: int, m: int) -> str:
    if tf == "5m" and pd == 1:
        exp = expected_5m_closes(y, m)
        if distinct == 0:
            return "not_available"
        if distinct >= exp:
            return "causal_5m_pd1_if_catalog_complete"
        if line == LINE_5M_PD1 and distinct > 0:
            return "partial_pilot_window_only"
    if tf == "15m" and pd == 1:
        return "15m_not_5m_pd1_backtest"
    return "other_combo"


def active_pool_catalog_monthly(ctx: ChCtx) -> list[dict]:
    rows: list[dict] = []
    q = f"""
    SELECT
        pipeline_line_id,
        timeframe,
        pool_detail,
        toStartOfMonth(decision_close) AS mo,
        countDistinct(decision_close) AS distinct_closes,
        countDistinct(snapshot_id) AS distinct_snapshots,
        count() AS pool_rows
    FROM {ctx.database}.{TABLE_ACTIVE_POOL}
    WHERE symbol = 'DOGEUSDT'
      AND decision_close >= '2026-01-01' AND decision_close < '2026-10-01'
    GROUP BY pipeline_line_id, timeframe, pool_detail, mo
    ORDER BY pipeline_line_id, mo
    """
    for r in client_query(ctx, q).named_results():
        mo = r["mo"]
        y, m = int(str(mo)[:4]), int(str(mo)[5:7])
        rows.append(
            {
                "layer": "E_ACTIVE_POOL_CATALOG",
                "database": ctx.database,
                "table_name": TABLE_ACTIVE_POOL,
                "pipeline_line_id": r["pipeline_line_id"],
                "timeframe": r["timeframe"],
                "pool_detail": r["pool_detail"],
                "month": f"{y:04d}-{m:02d}",
                "distinct_decision_closes": r["distinct_closes"],
                "distinct_snapshot_ids": r["distinct_snapshots"],
                "active_pool_row_count": r["pool_rows"],
                "active_vs_remaining": "ACTIVE_catalog_rows_only",
                "backtester_suitability": "needs_publish_ledger_plus_168h_context",
            }
        )
    return rows


def pd_timeframe_summary(pipelines: list[dict]) -> list[dict]:
    out = []
    for p in pipelines:
        out.append(
            {
                "timeframe": p["timeframe"],
                "pool_detail": p["pool_detail"],
                "pipeline_line_id": p["pipeline_line_id"],
                "published_closes_total": p["published_closes"],
                "range_start": p["earliest_decision_close_utc"],
                "range_end": p["latest_decision_close_utc"],
                "is_5m_pd1": p["pipeline_line_id"] == LINE_5M_PD1,
            }
        )
    return out


def disk_frozen_monthly() -> list[dict]:
    rows = []
    by_month: dict[str, set[str]] = {}
    for p in MULTI.rglob("5m_PD1/*.json"):
        stem = p.stem
        by_month.setdefault(stem[:6], set()).add(stem)
    for y, m in MONTHS_2026:
        key = f"{y:04d}{m:02d}"
        stems = sorted(by_month.get(key, set()))
        exp = expected_5m_closes(y, m)
        rows.append(
            {
                "layer": "C_5m_PD1_Frozen_JSON",
                "source": "doge_multi_month_2026_history/production",
                "month": f"{y:04d}-{m:02d}",
                "snapshot_count": len(stems),
                "expected_5m_closes": exp,
                "missing": max(0, exp - len(stems)),
                "earliest_stem": stems[0] if stems else "",
                "latest_stem": stems[-1] if stems else "",
            }
        )
    return rows


def load_frozen_json(close: datetime) -> dict | None:
    stem = _close_stem(close)
    y, m = close.year, close.month
    candidates = [
        MULTI / f"2026-{m:02d}/5m_PD1/{stem}.json",
        APR_SEP_5M / f"2026-{m:02d}/5m_PD1/{stem}.json",
    ]
    for path in candidates:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    return None


def ch_publish_row(ctx: ChCtx, close: datetime) -> dict | None:
    q = f"""
    SELECT snapshot_id, active_state_fingerprint, engine_input_hash, prefix_candle_fingerprint
    FROM {ctx.database}.{TABLE_PUBLISH} FINAL
    WHERE pipeline_line_id = {{lid:String}}
      AND config_hash = {{ch:String}}
      AND decision_close = {{dc:DateTime64(3, 'UTC')}}
      AND publish_complete = 1
    LIMIT 1
    """
    row = client_query(
        ctx,
        q,
        {"lid": LINE_5M_PD1, "ch": CFG_HASH_5M_PD1, "dc": close},
    ).first_row
    if not row:
        return None
    return {
        "snapshot_id": row[0],
        "active_state_fingerprint": row[1],
        "engine_input_hash": row[2],
        "prefix_candle_fingerprint": row[3],
    }


def ch_catalog_stats(ctx: ChCtx, snapshot_id: str) -> dict:
    q = f"""
    SELECT count(), countDistinct(pool_id)
    FROM {ctx.database}.{TABLE_ACTIVE_POOL}
    WHERE snapshot_id = {{sid:String}}
    """
    row = client_query(ctx, q, {"sid": snapshot_id}).first_row
    return {"pool_rows": row[0], "distinct_pool_ids": row[1]}


def frozen_fingerprint(payload: dict) -> str:
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.fingerprint import (
        active_state_fingerprint,
    )

    ids = payload.get("active_pool_ids") or list((payload.get("pools") or {}).keys())
    sides = payload.get("active_sides") or [payload["pools"][i]["side"] for i in ids]
    return active_state_fingerprint(ids, sides)


def parity_samples(ctx: ChCtx) -> list[dict]:
    sample_closes = [
        (2026, 5, 15, 12, 0),
        (2026, 6, 15, 12, 0),
        (2026, 7, 15, 12, 0),
        (2026, 8, 15, 12, 0),
        (2026, 9, 15, 12, 0),
        (2026, 9, 10, 0, 0),
        (2026, 9, 16, 12, 0),
    ]
    rows = []
    for y, m, d, h, mi in sample_closes:
        close = datetime(y, m, d, h, mi, tzinfo=timezone.utc)
        frozen = load_frozen_json(close)
        pub = ch_publish_row(ctx, close)
        row = {
            "decision_close_utc": close.isoformat().replace("+00:00", "Z"),
            "month": f"{y:04d}-{m:02d}",
            "frozen_json_present": "yes" if frozen else "no",
            "clickhouse_publish_present": "yes" if pub else "no",
            "verdict": "",
            "frozen_active_count": "",
            "ch_pool_rows": "",
            "fingerprint_match": "",
            "engine_input_hash_match": "",
            "notes": "",
        }
        if not frozen and not pub:
            row["verdict"] = "NO_DATA_EITHER"
            rows.append(row)
            continue
        if frozen and not pub:
            row["verdict"] = "SOURCE_PARITY_N/A_CH_ABSENT"
            row["frozen_active_count"] = len(frozen.get("active_pool_ids") or [])
            row["notes"] = "Disk-only; CH has no 5m PD1 publish for this close"
            rows.append(row)
            continue
        if pub and not frozen:
            row["verdict"] = "SOURCE_PARITY_FAILED_DISK_ABSENT"
            rows.append(row)
            continue
        # both present
        fp_f = frozen_fingerprint(frozen)
        fp_ch = pub["active_state_fingerprint"]
        row["frozen_active_count"] = len(frozen.get("active_pool_ids") or [])
        cat = ch_catalog_stats(ctx, pub["snapshot_id"])
        row["ch_pool_rows"] = cat["pool_rows"]
        row["fingerprint_match"] = "yes" if fp_f == fp_ch else "no"
        row["engine_input_hash_match"] = (
            "yes" if frozen.get("engine_input_hash") == pub["engine_input_hash"] else "no"
        )
        if row["fingerprint_match"] == "yes" and row["engine_input_hash_match"] == "yes":
            row["verdict"] = "SOURCE_PARITY_OK"
        else:
            row["verdict"] = "SOURCE_PARITY_FAILED"
        rows.append(row)
    return rows


def window_coverage_5m_pd1(ctx: ChCtx, start: datetime, end_exclusive: datetime) -> dict:
    """Count published 5m PD1 closes in [start, end_exclusive)."""
    q = f"""
    SELECT countDistinct(decision_close) AS n, min(decision_close), max(decision_close)
    FROM {ctx.database}.{TABLE_PUBLISH} FINAL
    WHERE pipeline_line_id = {{lid:String}}
      AND config_hash = {{ch:String}}
      AND publish_complete = 1
      AND decision_close >= {{s:DateTime64(3, 'UTC')}}
      AND decision_close < {{e:DateTime64(3, 'UTC')}}
    """
    row = client_query(
        ctx,
        q,
        {"lid": LINE_5M_PD1, "ch": CFG_HASH_5M_PD1, "s": start, "e": end_exclusive},
    ).first_row
    # expected 5m closes in window
    expected = 0
    t = start
    while t < end_exclusive:
        expected += 1
        t += timedelta(minutes=5)
    return {
        "window_start": start.isoformat().replace("+00:00", "Z"),
        "window_end_exclusive": end_exclusive.isoformat().replace("+00:00", "Z"),
        "expected_5m_closes": expected,
        "published_closes": int(row[0] or 0),
        "min_close": str(row[1]) if row[1] else "",
        "max_close": str(row[2]) if row[2] else "",
        "complete": int(row[0] or 0) >= expected,
    }


def probe_aux_tables(ctx: ChCtx) -> list[dict]:
    """Lightweight DOGE / empty checks on known LMV2 tables."""
    probes = []
    for label, table, where in [
        ("snapshot_meta", TABLE_SNAPSHOT_META, "symbol = 'DOGEUSDT'"),
        ("snapshot_member", TABLE_SNAPSHOT_MEMBER, "1"),
        ("import_provenance", TABLE_IMPORT_PROVENANCE, "symbol = 'DOGEUSDT'"),
        ("ch_test_run", TABLE_RUN, "symbol = 'DOGEUSDT'"),
        ("ch_test_pool", TABLE_POOL, "symbol = 'DOGEUSDT'"),
    ]:
        try:
            q = f"SELECT count() FROM {ctx.database}.{table} WHERE {where}"
            n = client_query(ctx, q).first_row[0]
            probes.append({"table": table, "doge_or_all_filter": where, "row_count": n})
        except Exception as exc:
            probes.append({"table": table, "doge_or_all_filter": where, "row_count": f"err:{type(exc).__name__}"})
    return probes


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    ctx = connect()
    try:
        databases = list_databases(ctx.client)
        db_inv, tbl_inv = table_inventory(ctx.client, databases)
        _wcsv(OUT / "clickhouse_database_inventory.csv", db_inv)
        _wcsv(OUT / "clickhouse_table_inventory.csv", tbl_inv)

        pipelines = pipeline_inventory(ctx)
        _wcsv(OUT / "pipeline_id_inventory.csv", pipelines)
        _wcsv(OUT / "pd_timeframe_inventory.csv", pd_timeframe_summary(pipelines))

        coverage = doge_candle_monthly(ctx)
        coverage.extend(publish_monthly_by_pipeline(ctx))
        _wcsv(OUT / "historical_coverage_by_month.csv", coverage)

        active_rows = active_pool_catalog_monthly(ctx)
        _wcsv(OUT / "active_pool_source_inventory.csv", active_rows)

        disk_rows = disk_frozen_monthly()
        parity = parity_samples(ctx)
        _wcsv(OUT / "frozen_vs_clickhouse_parity.csv", parity)

        aug_window = window_coverage_5m_pd1(
            ctx,
            datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 29, 0, 0, tzinfo=timezone.utc),
        )
        may_window = window_coverage_5m_pd1(
            ctx,
            datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 5, 29, 0, 0, tzinfo=timezone.utc),
        )
        probes = probe_aux_tables(ctx)

        # Markdown outputs
        write_backtester_options(OUT, pipelines, disk_rows, aug_window, may_window, parity)
        write_unresolved_gaps(OUT, pipelines, coverage, disk_rows, aug_window, probes)

        summary = {
            "databases_scanned": len(databases),
            "primary_database": ctx.database,
            "docker_container": "orderbook-clickhouse",
            "pipeline_count_doge": len(pipelines),
            "parity_samples": parity,
            "window_aug_01_29_exclusive": aug_window,
            "window_may_01_29_exclusive": may_window,
            "disk_frozen_monthly": disk_rows,
            "aux_table_probes": probes,
        }
        _wjson(OUT / "discovery_summary.json", summary)
    finally:
        ctx.client.close()
    return 0


def write_backtester_options(
    out: Path,
    pipelines: list[dict],
    disk: list[dict],
    aug_window: dict,
    may_window: dict,
    parity: list[dict],
) -> None:
    lines = [
        "# Backtester source options (Phase 4U read-only)",
        "",
        "## Option A — ClickHouse direct",
        "- **Coverage:** 1m OHLCV Jan–Sep 2026 complete; 5m PD1 publish **2592 closes only** (2026-09-09 – 2026-09-17).",
        "- **Causal pool state:** Only where `research_lmv2_active_snapshot_publish_v1` + `research_lmv2_snapshot_active_pool_v1` rows exist.",
        "- **28-day window Aug 1–29 excl.:** published closes "
        f"{aug_window['published_closes']}/{aug_window['expected_5m_closes']} — **not sufficient**.",
        "",
        "## Option B — Frozen JSON on disk",
        "- **Coverage:** May–Aug 2026 full months; April partial; September full month on disk (multi_month + september catalog).",
        "- **Not wired** to current `build_landscape_window` / publish reader without import.",
        "",
        "## Option C — Hybrid",
        "- Only viable where CH and disk **overlap** (Sep 9–17): parity samples show "
        + ", ".join({p["verdict"] for p in parity if p["clickhouse_publish_present"] == "yes"})
        + ".",
        "- **No silent mixing** across May–Aug (CH absent, disk present).",
        "",
        "## Recommendation (audit only, no integration)",
        "Prefer **Option B + controlled publish extension** until CH contains full 5m PD1 months; CH alone is **not** backtest-ready for 28d from May/Aug 2026.",
        "",
    ]
    (out / "backtester_source_options.md").write_text("\n".join(lines), encoding="utf-8")


def write_unresolved_gaps(
    out: Path,
    pipelines: list[dict],
    coverage: list[dict],
    disk: list[dict],
    aug_window: dict,
    probes: list[dict],
) -> None:
    five_pd1_lines = [p for p in pipelines if p["timeframe"] == "5m" and str(p["pool_detail"]) == "1"]
    lines = [
        "# Unresolved source gaps (Phase 4U)",
        "",
        "## ClickHouse 5m PD1 publish lines",
        "",
    ]
    for p in five_pd1_lines:
        lines.append(f"- `{p['pipeline_line_id']}`: {p['published_closes']} closes, {p['earliest_decision_close_utc']} – {p['latest_decision_close_utc']}")
    lines.extend(
        [
            "",
            "## Disk without CH (May–Aug 2026)",
            "Full frozen months exist under `doge_multi_month_2026_history` but **zero** matching publish rows in CH.",
            "",
            "## 28-day causal pool gap",
            f"Preferred window 2026-08-01 – 2026-08-29 (excl.): CH 5m PD1 publishes {aug_window['published_closes']} vs expected {aug_window['expected_5m_closes']}.",
            "",
            "## Remaining pools",
            "No dedicated Remaining-pool publish table found in project schema; only ACTIVE catalog + publish ledger probed.",
            "",
            "## Aux tables",
            "",
        ]
    )
    for pr in probes:
        lines.append(f"- `{pr['table']}`: {pr['row_count']} rows ({pr['doge_or_all_filter']})")
    (out / "unresolved_source_gaps.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
