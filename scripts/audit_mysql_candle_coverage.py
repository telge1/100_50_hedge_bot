#!/usr/bin/env python3
"""Read-only MySQL candle coverage audit (SELECT only; no schema/data writes)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from research.regime_scanner.candle_sources import load_regime_db_env_file  # noqa: E402
from research.regime_scanner.mysql_candle_coverage_audit import (  # noqa: E402
    SYMBOL_ALIASES,
    analyze_ohlcv_series,
    candle_close_from_open,
    ensure_utc,
    find_gaps,
    invalid_ohlcv_mask,
    normalize_symbol_lookup,
    select_last_closed_candle,
    timeframe_to_seconds,
    warmup_available,
)
from research.regime_scanner.mysql_candle_store.config import (  # noqa: E402
    RegimeDbConfigError,
    load_regime_db_config,
)
from research.regime_scanner.mysql_candle_store.store_mysql import MySQLCandleStore  # noqa: E402

DEFAULT_OUT = ROOT / "results" / "mysql_candle_coverage_audit"
FOCUS = ("APTUSDT", "DOGEUSDT", "BTCUSDT")


def _connect() -> tuple[MySQLCandleStore, dict[str, Any]]:
    load_regime_db_env_file()
    cfg = load_regime_db_config()
    store = MySQLCandleStore(cfg)
    meta = {
        "config_file": "research/regime_scanner/.env.regime_db (gitignored) + REGIME_DB_*",
        "connector": "research.regime_scanner.mysql_candle_store.store_mysql.MySQLCandleStore",
        "repository": "research.regime_scanner.mysql_candle_store.repository.load_candles",
        "database": cfg.name,
        "host_set": bool(cfg.host),
        "port": cfg.port,
        "user_set": bool(cfg.user),
        # never include password
    }
    return store, meta


def inspect_schema(store: MySQLCandleStore) -> dict[str, Any]:
    text = store._text
    engine = store._engine
    out: dict[str, Any] = {"tables": {}, "candle_like_tables": []}
    with engine.connect() as conn:
        db = conn.execute(text("SELECT DATABASE()")).scalar_one()
        out["database"] = db
        tables = conn.execute(
            text(
                """
                SELECT TABLE_NAME
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA = DATABASE()
                ORDER BY TABLE_NAME
                """
            )
        ).fetchall()
        table_names = [r[0] for r in tables]
        out["all_tables"] = table_names
        for tname in table_names:
            cols = conn.execute(
                text(
                    """
                    SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_KEY, COLUMN_COMMENT, EXTRA
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t
                    ORDER BY ORDINAL_POSITION
                    """
                ),
                {"t": tname},
            ).mappings().all()
            idxs = conn.execute(
                text(
                    """
                    SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME
                    FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t
                    ORDER BY INDEX_NAME, SEQ_IN_INDEX
                    """
                ),
                {"t": tname},
            ).mappings().all()
            col_names = [c["COLUMN_NAME"] for c in cols]
            looks_candle = (
                "open" in col_names
                and "high" in col_names
                and "low" in col_names
                and "close" in col_names
                and ("open_time" in col_names or "timestamp" in col_names or "date" in col_names)
            )
            entry = {
                "columns": [dict(c) for c in cols],
                "indexes": [dict(i) for i in idxs],
                "looks_like_candle_table": looks_candle,
            }
            out["tables"][tname] = entry
            if looks_candle:
                out["candle_like_tables"].append(tname)
    return out


def inventory_keys(store: MySQLCandleStore) -> pd.DataFrame:
    sql = store._text(
        """
        SELECT exchange, symbol, timeframe,
               COUNT(*) AS row_count,
               MIN(open_time) AS first_open,
               MAX(open_time) AS last_open,
               MIN(close_time) AS first_close,
               MAX(close_time) AS last_close,
               SUM(CASE WHEN is_closed = 0 THEN 1 ELSE 0 END) AS open_candle_count
        FROM market_candles
        GROUP BY exchange, symbol, timeframe
        ORDER BY symbol, timeframe, first_open
        """
    )
    with store._engine.connect() as conn:
        rows = [dict(r._mapping) for r in conn.execute(sql)]
    return pd.DataFrame(rows)


def load_series(store: MySQLCandleStore, exchange: str, symbol: str, timeframe: str) -> pd.DataFrame:
    return store.fetch_candles(
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        closed_only=False,
    )


def run_causality_checks(
    df: pd.DataFrame,
    *,
    timeframe: str,
    symbol: str,
    exchange: str,
) -> list[dict[str, Any]]:
    if df.empty:
        return []
    opens = df["open_time"].tolist()
    closes = df["close_time"].tolist()
    first_open = ensure_utc(opens[0])
    last_close = ensure_utc(closes[-1])
    # pick a mid closed candle
    mid_i = len(df) // 2
    mid_close = ensure_utc(closes[mid_i])
    mid_open = ensure_utc(opens[mid_i])
    step = pd.Timedelta(seconds=timeframe_to_seconds(timeframe))

    cases = [
        ("exact_close", mid_close),
        ("inside_running", mid_open + step / 2),  # during candle that opens at mid_open... wait
        ("one_sec_before_close", mid_close - pd.Timedelta(seconds=1)),
        ("before_data", first_open - pd.Timedelta(days=1)),
        ("after_data", last_close + pd.Timedelta(days=1)),
    ]
    # For inside_running: use last candle open + 1 minute (running relative to that open's close)
    last_open = ensure_utc(opens[-1])
    last_close_ts = ensure_utc(closes[-1])
    # better inside: take a candle that has a successor; query = mid_open + 1min while mid still running
    cases[1] = ("inside_running", mid_open + pd.Timedelta(minutes=1))

    out = []
    for name, q in cases:
        # For inside_running on mid candle: expected selected is previous candle (mid_i-1) if mid not closed
        sel = select_last_closed_candle(opens, closes, q)
        # Verify causality against full frame via SQL-equivalent filter
        q_utc = ensure_utc(q)
        eligible = df.loc[pd.to_datetime(df["close_time"], utc=True) <= q_utc]
        if eligible.empty:
            expected_open = None
            expected_close = None
        else:
            expected_open = ensure_utc(eligible.iloc[-1]["open_time"]).isoformat()
            expected_close = ensure_utc(eligible.iloc[-1]["close_time"]).isoformat()
        pass_ok = (
            sel["selected_last_candle_open_utc"] == expected_open
            and sel["selected_last_candle_close_utc"] == expected_close
            and bool(sel["causality_pass"])
        )
        # special: inside running must not select a candle whose close > q
        if sel["selected_last_candle_close_utc"] is not None:
            if ensure_utc(sel["selected_last_candle_close_utc"]) > q_utc:
                pass_ok = False
        out.append(
            {
                "case": name,
                "symbol": symbol,
                "exchange": exchange,
                "timeframe": timeframe,
                "requested_timestamp_utc": q_utc.isoformat(),
                "selected_last_candle_open_utc": sel["selected_last_candle_open_utc"],
                "selected_last_candle_close_utc": sel["selected_last_candle_close_utc"],
                "expected_open_utc": expected_open,
                "expected_close_utc": expected_close,
                "causality_pass": pass_ok,
            }
        )
    # Documented example if data covers it
    example_q = ensure_utc("2026-07-31 12:03:00")
    if first_open <= example_q <= last_close + pd.Timedelta(days=1):
        sel = select_last_closed_candle(opens, closes, example_q)
        eligible = df.loc[pd.to_datetime(df["close_time"], utc=True) <= example_q]
        expected_open = (
            ensure_utc(eligible.iloc[-1]["open_time"]).isoformat() if not eligible.empty else None
        )
        expected_close = (
            ensure_utc(eligible.iloc[-1]["close_time"]).isoformat() if not eligible.empty else None
        )
        out.append(
            {
                "case": "example_2026-07-31T12:03Z",
                "symbol": symbol,
                "exchange": exchange,
                "timeframe": timeframe,
                "requested_timestamp_utc": example_q.isoformat(),
                "selected_last_candle_open_utc": sel["selected_last_candle_open_utc"],
                "selected_last_candle_close_utc": sel["selected_last_candle_close_utc"],
                "expected_open_utc": expected_open,
                "expected_close_utc": expected_close,
                "causality_pass": sel["selected_last_candle_open_utc"] == expected_open,
            }
        )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only MySQL candle coverage audit")
    ap.add_argument("--symbols", nargs="*", default=None, help="Optional symbol filter (e.g. APTUSDT)")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--exchange", default=None, help="Optional exchange filter")
    args = ap.parse_args()

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        store, conn_meta = _connect()
    except RegimeDbConfigError as exc:
        print(f"CONFIG_ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        schema = inspect_schema(store)
        inv = inventory_keys(store)
        if args.exchange:
            inv = inv.loc[inv["exchange"] == args.exchange].copy()
        if args.symbols:
            avail = inv["symbol"].unique().tolist() if not inv.empty else []
            mapped = []
            for s in args.symbols:
                m = normalize_symbol_lookup(s, avail) if avail else s
                if m:
                    mapped.append(m)
            inv = inv.loc[inv["symbol"].isin(mapped)].copy() if mapped else inv.iloc[0:0]

        coverage_rows: list[dict[str, Any]] = []
        gap_rows: list[dict[str, Any]] = []
        dup_rows: list[dict[str, Any]] = []
        invalid_rows: list[dict[str, Any]] = []
        causality_rows: list[dict[str, Any]] = []
        warmup_rows: list[dict[str, Any]] = []

        market_type_note = (
            "No market_type column in market_candles; exchange+symbol encode identity. "
            "Docs/bootstrap describe Bybit futures feathers."
        )

        for _, key in inv.iterrows():
            exchange = str(key["exchange"])
            symbol = str(key["symbol"])
            timeframe = str(key["timeframe"])
            print(f"auditing {exchange} {symbol} {timeframe} ...", flush=True)
            df = load_series(store, exchange, symbol, timeframe)
            cov = analyze_ohlcv_series(
                df,
                exchange=exchange,
                symbol=symbol,
                timeframe=timeframe,
                market_type=None,
            )
            coverage_rows.append(cov.to_dict())

            if not df.empty:
                opens = [ensure_utc(t) for t in df["open_time"].tolist()]
                for g in find_gaps(opens, timeframe):
                    gap_rows.append(
                        {
                            "exchange": exchange,
                            "symbol": symbol,
                            "timeframe": timeframe,
                            **g,
                        }
                    )
                # duplicates detail
                ot = pd.to_datetime(df["open_time"], utc=True)
                dup_mask = ot.duplicated(keep=False)
                if bool(dup_mask.any()):
                    for _, r in df.loc[dup_mask].iterrows():
                        dup_rows.append(
                            {
                                "exchange": exchange,
                                "symbol": symbol,
                                "timeframe": timeframe,
                                "open_time_utc": ensure_utc(r["open_time"]).isoformat(),
                                "close_time_utc": ensure_utc(r["close_time"]).isoformat(),
                                "source": r.get("source"),
                            }
                        )
                bad = invalid_ohlcv_mask(df)
                if bool(bad.any()):
                    for _, r in df.loc[bad].head(50).iterrows():
                        invalid_rows.append(
                            {
                                "exchange": exchange,
                                "symbol": symbol,
                                "timeframe": timeframe,
                                "open_time_utc": ensure_utc(r["open_time"]).isoformat(),
                                "open": r.get("open"),
                                "high": r.get("high"),
                                "low": r.get("low"),
                                "close": r.get("close"),
                                "volume": r.get("volume"),
                            }
                        )

                # warmup
                first_o = ensure_utc(df["open_time"].iloc[0])
                last_c = ensure_utc(df["close_time"].iloc[-1])
                warmup_rows.append(
                    {
                        "exchange": exchange,
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "span_days": float((last_c - first_o).total_seconds() / 86400.0),
                        "warmup_7d": warmup_available(first_o, last_c, days=7),
                        "warmup_30d": warmup_available(first_o, last_c, days=30),
                        "warmup_60d": warmup_available(first_o, last_c, days=60),
                        "warmup_90d": warmup_available(first_o, last_c, days=90),
                    }
                )

                # causality for focus symbols + 5m preferentially
                if symbol in {normalize_symbol_lookup(f, [symbol]) or symbol for f in FOCUS} or any(
                    symbol == normalize_symbol_lookup(f, inv["symbol"].unique()) for f in FOCUS
                ):
                    if timeframe in ("5m", "15m", "1h", "4h", "30m"):
                        causality_rows.extend(
                            run_causality_checks(
                                df.sort_values("open_time"),
                                timeframe=timeframe,
                                symbol=symbol,
                                exchange=exchange,
                            )
                        )

        # Ensure focus causality even if filter missed alias mapping
        avail_syms = inv["symbol"].unique().tolist() if not inv.empty else []
        for focus in FOCUS:
            mapped = normalize_symbol_lookup(focus, avail_syms)
            if not mapped:
                continue
            sub = inv.loc[inv["symbol"] == mapped]
            for _, key in sub.iterrows():
                if any(
                    r.get("symbol") == mapped and r.get("timeframe") == key["timeframe"]
                    for r in causality_rows
                ):
                    continue
                df = load_series(store, str(key["exchange"]), mapped, str(key["timeframe"]))
                if df.empty:
                    continue
                causality_rows.extend(
                    run_causality_checks(
                        df.sort_values("open_time"),
                        timeframe=str(key["timeframe"]),
                        symbol=mapped,
                        exchange=str(key["exchange"]),
                    )
                )

        cov_df = pd.DataFrame(coverage_rows)
        if not cov_df.empty:
            cov_df = cov_df.sort_values(["symbol", "timeframe", "first_candle_open_utc"])
        cov_df.to_csv(out_dir / "symbol_timeframe_coverage.csv", index=False)
        pd.DataFrame(gap_rows).to_csv(out_dir / "data_gaps.csv", index=False)
        pd.DataFrame(dup_rows).to_csv(out_dir / "duplicates.csv", index=False)
        pd.DataFrame(invalid_rows).to_csv(out_dir / "invalid_ohlcv.csv", index=False)
        pd.DataFrame(causality_rows).to_csv(out_dir / "timestamp_causality_checks.csv", index=False)
        pd.DataFrame(warmup_rows).to_csv(out_dir / "warmup_availability.csv", index=False)

        # schema inventory (no secrets)
        schema_out = {
            "connection": conn_meta,
            "market_type_note": market_type_note,
            "canonical_table": "market_candles",
            "identity_key": ["exchange", "symbol", "timeframe", "open_time"],
            "timestamp_semantics": {
                "open_time": "UTC candle open (DATETIME(6), naive UTC wall-clock)",
                "close_time": "UTC candle close = open + timeframe",
                "created_at": "import/row create time",
                "updated_at": "row update time",
            },
            "schema_inspection": schema,
            "column_map": {
                "exchange": "exchange",
                "symbol": "symbol",
                "timeframe": "timeframe",
                "candle_open": "open_time",
                "candle_close": "close_time",
                "ohlcv": ["open", "high", "low", "close", "volume"],
                "market_type": None,
                "is_closed": "is_closed",
                "source": "source",
            },
        }
        (out_dir / "schema_inventory.json").write_text(json.dumps(schema_out, indent=2, default=str))

        # Focus table
        focus_rows = []
        for focus in FOCUS:
            mapped = normalize_symbol_lookup(focus, avail_syms)
            focus_rows.append(
                {
                    "requested": focus,
                    "mapped_symbol": mapped,
                    "aliases_tried": list(SYMBOL_ALIASES.get(focus, ())),
                    "found": mapped is not None,
                }
            )
        focus_cov = []
        for fr in focus_rows:
            if not fr["found"]:
                continue
            sub = cov_df.loc[cov_df["symbol"] == fr["mapped_symbol"]] if not cov_df.empty else cov_df
            for _, r in sub.iterrows():
                focus_cov.append(r.to_dict())

        # Primary decision
        causality_fail = sum(1 for r in causality_rows if not r.get("causality_pass"))
        critical_gaps = [
            r
            for r in coverage_rows
            if (r.get("coverage_pct") is not None and r["coverage_pct"] < 95.0)
            or (r.get("missing_intervals") or 0) > 100
        ]
        focus_found = all(
            normalize_symbol_lookup(f, avail_syms) is not None for f in FOCUS
        ) if avail_syms else False
        candle_tables = schema.get("candle_like_tables") or []
        ambiguous = len(candle_tables) != 1 or "market_candles" not in candle_tables

        if not coverage_rows:
            primary = "MYSQL_CANDLE_COVERAGE_PARTIAL"
            reason = "no candle series found under filters"
        elif ambiguous:
            primary = "MYSQL_CANDLE_SCHEMA_AMBIGUOUS"
            reason = f"candle-like tables={candle_tables}"
        elif causality_fail:
            primary = "MYSQL_CANDLE_CAUSAL_QUERY_NOT_READY"
            reason = f"{causality_fail} causality check failures"
        elif not focus_found:
            primary = "MYSQL_CANDLE_COVERAGE_PARTIAL"
            reason = "APT/DOGE/BTC not all present"
        elif critical_gaps:
            # distinguish partial vs critical
            worst = min((r.get("coverage_pct") or 100.0) for r in critical_gaps)
            if worst < 80.0:
                primary = "MYSQL_CANDLE_DATA_GAPS_CRITICAL"
                reason = f"worst coverage_pct={worst}"
            else:
                primary = "MYSQL_CANDLE_COVERAGE_PARTIAL"
                reason = f"{len(critical_gaps)} series below 95% coverage or >100 missing"
        else:
            primary = "MYSQL_CANDLE_DATA_READY"
            reason = "schema clear; focus symbols present; causality pass; no critical gaps"

        summary = {
            "primary_decision": primary,
            "reason": reason,
            "connection": conn_meta,
            "n_series": len(coverage_rows),
            "symbols": sorted(cov_df["symbol"].unique().tolist()) if not cov_df.empty else [],
            "timeframes": sorted(cov_df["timeframe"].unique().tolist()) if not cov_df.empty else [],
            "exchanges": sorted(cov_df["exchange"].unique().tolist()) if not cov_df.empty else [],
            "focus_symbol_mapping": focus_rows,
            "focus_coverage": focus_cov,
            "causality_checks": len(causality_rows),
            "causality_failures": causality_fail,
            "duplicate_rows_logged": len(dup_rows),
            "invalid_ohlcv_logged": len(invalid_rows),
            "gap_events_logged": len(gap_rows),
            "warmup": warmup_rows,
            "market_type_note": market_type_note,
            "next_step": (
                "Build timestamp direction runner using MySQLCandleStore.fetch_candles/"
                "repository.load_candles with decision_time (close_time <= T) on market_candles."
            ),
        }
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

        # REPORT
        lines = [
            "# MySQL Candle Coverage Audit",
            "",
            f"## Primärentscheidung",
            "",
            f"**{primary}**",
            "",
            f"Reason: {reason}",
            "",
            "## Connection / Schema",
            "",
            f"- Config: `{conn_meta['config_file']}`",
            f"- Connector: `{conn_meta['connector']}`",
            f"- Database: `{conn_meta['database']}`",
            f"- Canonical table: `market_candles`",
            f"- Identity: `(exchange, symbol, timeframe, open_time)`",
            f"- Timestamp: `open_time` = candle open UTC; `close_time` = open + TF",
            f"- Market-Type column: **none** ({market_type_note})",
            "",
            "## Inventory",
            "",
            f"- Exchanges: {summary['exchanges']}",
            f"- Symbols ({len(summary['symbols'])}): {summary['symbols']}",
            f"- Timeframes: {summary['timeframes']}",
            "",
            "## Focus symbols",
            "",
            "| Requested | Mapped | Found |",
            "|---|---|---|",
        ]
        for fr in focus_rows:
            lines.append(f"| {fr['requested']} | {fr['mapped_symbol']} | {fr['found']} |")
        lines += [
            "",
            "| Symbol | Timeframe | Beginn UTC | Ende UTC | Candles | Coverage % | Größte Lücke (s) |",
            "|---|---|---|---|---:|---:|---:|",
        ]
        for r in focus_cov:
            gap = r.get("largest_gap_seconds")
            lines.append(
                f"| {r['symbol']} | {r['timeframe']} | {r['first_candle_open_utc']} | "
                f"{r['last_candle_close_utc']} | {r['row_count']} | {r.get('coverage_pct')} | {gap} |"
            )
        lines += [
            "",
            "## Warm-up",
            "",
            "| Symbol | TF | 7d | 30d | 60d | 90d | Span days |",
            "|---|---|---:|---:|---:|---:|---:|",
        ]
        for w in warmup_rows:
            if w["symbol"] not in {fr["mapped_symbol"] for fr in focus_rows if fr["found"]}:
                # still show all if small
                pass
            lines.append(
                f"| {w['symbol']} | {w['timeframe']} | {w['warmup_7d']} | {w['warmup_30d']} | "
                f"{w['warmup_60d']} | {w['warmup_90d']} | {w['span_days']:.1f} |"
            )
        lines += [
            "",
            "## Causality",
            "",
            f"Checks: {len(causality_rows)}, failures: {causality_fail}",
            "",
            "Rule: `close_time <= query_timestamp` (no running candle).",
            "",
            "## Data quality",
            "",
            f"- Duplicate open keys logged: {len(dup_rows)}",
            f"- Invalid OHLCV rows logged: {len(invalid_rows)}",
            f"- Gap events logged: {len(gap_rows)}",
            "",
            "## Answers",
            "",
            "1. Symbols: see Inventory.",
            "2. Timeframes: see Inventory.",
            "3. Ranges: see Focus table / `symbol_timeframe_coverage.csv`.",
            "4. Use `market_candles` with columns listed in `schema_inventory.json`.",
            "5. Stored identity timestamp is **candle open** (`open_time`); close is stored and derived.",
            "6. Historical query: `WHERE close_time <= :decision_time` (also via `load_candles(..., decision_time=T)`).",
            "7. Gaps/dupes: see CSVs.",
            "8. Warm-up: see table above.",
            "9. Direction runner: yes if primary is READY or PARTIAL with known TF limits.",
            "10. Next: implement timestamp→closed candles→structure direction using existing MySQL read path.",
            "",
        ]
        (out_dir / "REPORT.md").write_text("\n".join(lines))
        print(f"PRIMARY={primary}")
        print(f"Wrote {out_dir}")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
