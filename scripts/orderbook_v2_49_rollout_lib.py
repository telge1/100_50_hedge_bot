#!/usr/bin/env python3
"""Helpers for the 49-coin Orderbook V2 nohup rollout. SELECT-only except when
the caller separately invokes the existing pilot CLI.

This module never writes ClickHouse, never downloads, and never prints secrets.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MIN_HEAD = "af1623f16f02ac770bb24a9c45669949b51778e1"


def current_head() -> str:
    root = Path(__file__).resolve().parents[1]
    import subprocess

    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True,
    ).strip()


EXPECTED_HEAD = current_head()
from orderbook_analyse.orderbook_v2 import PARSER_VERSION as EXPECTED_PARSER
WINDOW_START = "2026-08-11"
WINDOW_END_INCLUSIVE = "2026-08-17"
WINDOW_END_EXCLUSIVE = "2026-08-18"
COLLECTOR_PID = 147111
PASS_DECISION_TEMPLATE = "{symbol}_OB_V2_7D_PILOT_PASSED"
N_EXPECTED_SECONDS = 604800
N_DAY_SECONDS = 86400

SYMBOLS_48: tuple[str, ...] = (
    "SOLUSDT",
    "XRPUSDT",
    "BNBUSDT",
    "DOGEUSDT",
    "LINKUSDT",
    "AVAXUSDT",
    "LTCUSDT",
    "DOTUSDT",
    "SUIUSDT",
    "APTUSDT",
    "NEARUSDT",
    "ATOMUSDT",
    "UNIUSDT",
    "AAVEUSDT",
    "ARBUSDT",
    "OPUSDT",
    "TRXUSDT",
    "XLMUSDT",
    "HBARUSDT",
    "ALGOUSDT",
    "INJUSDT",
    "TIAUSDT",
    "ICPUSDT",
    "RENDERUSDT",
    "CRVUSDT",
    "MNTUSDT",
    "HYPEUSDT",
    "ZECUSDT",
    "XMRUSDT",
    "TAOUSDT",
    "WLDUSDT",
    "ENAUSDT",
    "ONDOUSDT",
    "JTOUSDT",
    "1000PEPEUSDT",
    "SHIB1000USDT",
    "1000BONKUSDT",
    "WIFUSDT",
    "PENGUUSDT",
    "TRUMPUSDT",
    "PUMPFUNUSDT",
    "FARTCOINUSDT",
    "KAITOUSDT",
    "WLFIUSDT",
    "XPLUSDT",
    "LITUSDT",
    "XAUTUSDT",
    "PAXGUSDT",
)

FORBIDDEN_SYMBOLS = frozenset({"ADAUSDT", "BTCUSDT", "ETHUSDT", "XAUUSDT"})
REQUIRED_ENV = (
    "CLICKHOUSE_HOST",
    "CLICKHOUSE_HTTP_PORT",
    "CLICKHOUSE_DATABASE",
    "CLICKHOUSE_USER",
)

# Known leftover rows at 2026-08-18 00:00:00 that must not be deleted in this task.
# A new ob200_v3 row at that timestamp is never allowed.
KNOWN_LEGACY_OVERFLOW: dict[str, tuple[str, int]] = {
    "ETHUSDT": ("ob200_v2", 1),
    "ADAUSDT": ("ob200_v1", 1),
}


def _legacy_overflow_ok(
    symbol: str,
    overflow_rows: list[Any],
    overflow_total: int,
) -> bool:
    if overflow_total == 0:
        return True
    allowed = KNOWN_LEGACY_OVERFLOW.get(symbol)
    if allowed is None:
        return False
    exp_ver, exp_n = allowed
    got = [(str(ver), int(n)) for ver, n in overflow_rows]
    return got == [(exp_ver, exp_n)]


def validate_symbol_set(symbols: tuple[str, ...] = SYMBOLS_48) -> None:
    if len(symbols) != 48:
        raise ValueError(f"expected 48 symbols, got {len(symbols)}")
    if len(set(symbols)) != len(symbols):
        raise ValueError("duplicate symbols")
    hit = FORBIDDEN_SYMBOLS.intersection(symbols)
    if hit:
        raise ValueError(f"forbidden symbols present: {sorted(hit)}")


def check_window(days: list[Any] | None = None) -> tuple[bool, str]:
    if days is None:
        from orderbook_analyse.orderbook_v2.downloader import pilot_days

        days = pilot_days(7)
    iso = [d.isoformat() if hasattr(d, "isoformat") else str(d) for d in days]
    uniq = sorted(set(iso))
    ok = (
        len(iso) == 7
        and len(uniq) == 7
        and min(uniq) == WINDOW_START
        and max(uniq) == WINDOW_END_INCLUSIVE
    )
    return ok, ",".join(iso)


def env_presence(*, load_env_file: bool = True) -> dict[str, str]:
    if load_env_file:
        from orderbook_analyse.orderbook_v2.ch_client import load_clickhouse_settings

        try:
            load_clickhouse_settings(load_env_file=True)
        except Exception:
            pass
    out: dict[str, str] = {}
    for name in REQUIRED_ENV:
        raw = os.environ.get(name)
        if raw is None:
            out[name] = "UNSET"
        elif not raw.strip():
            out[name] = "EMPTY"
        else:
            out[name] = "SET"
    return out


def collector_ok(pid: int = COLLECTOR_PID) -> tuple[bool, str]:
    cmd_path = Path(f"/proc/{pid}/cmdline")
    if not cmd_path.is_file():
        return False, f"pid_{pid}_missing"
    cmd = cmd_path.read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace")
    if "oi_liquidation_collector" not in cmd:
        return False, f"pid_{pid}_unexpected_command"
    return True, "ok"


def _where_sql() -> str:
    return (
        "symbol = {sym:String} "
        "AND bucket_start >= toDateTime64('2026-08-11 00:00:00', 3, 'UTC') "
        "AND bucket_start <  toDateTime64('2026-08-18 00:00:00', 3, 'UTC')"
    )


def classify_symbol(client: Any, symbol: str) -> tuple[str, str]:
    """Return (NOT_IMPORTED|COMPLETE_V3|INCONSISTENT, reason)."""
    w = _where_sql()
    p = {"sym": symbol}
    logical, distinct, nver, invalid = client.query(
        f"""
        SELECT count(), countDistinct(bucket_start),
               uniqExact(parser_version), countIf(is_valid != 1)
        FROM orderbook_analysis.orderbook_features_1s_v2 FINAL
        WHERE {w}
        """,
        parameters=p,
    ).result_rows[0]
    versions = client.query(
        f"""
        SELECT parser_version, count()
        FROM orderbook_analysis.orderbook_features_1s_v2 FINAL
        WHERE {w}
        GROUP BY parser_version
        """,
        parameters=p,
    ).result_rows
    manifest = client.query(
        """
        SELECT source_date, status, parser_version, inserted_feature_rows
        FROM orderbook_analysis.orderbook_import_manifest_v2 FINAL
        WHERE symbol = {sym:String}
          AND source_date >= toDate('2026-08-11')
          AND source_date <  toDate('2026-08-18')
        ORDER BY source_date
        """,
        parameters=p,
    ).result_rows

    if logical == 0 and not manifest:
        return "NOT_IMPORTED", "no_rows_no_manifest"
    if (
        logical == N_EXPECTED_SECONDS
        and distinct == N_EXPECTED_SECONDS
        and invalid == 0
        and versions == [(EXPECTED_PARSER, N_EXPECTED_SECONDS)]
        and nver == 1
        and len(manifest) == 7
        and all(
            row[1] == "COMPLETE"
            and row[2] == EXPECTED_PARSER
            and int(row[3]) == N_DAY_SECONDS
            for row in manifest
        )
    ):
        return "COMPLETE_V3", "skip_complete"
    reason = (
        f"logical={logical} distinct={distinct} invalid={invalid} "
        f"versions={versions} manifest_n={len(manifest)}"
    )
    return "INCONSISTENT", reason


def audit_symbol(client: Any, symbol: str) -> tuple[bool, str]:
    w = _where_sql()
    p = {"sym": symbol}
    logical, distinct, invalid, first_b, last_b, event, cf = client.query(
        f"""
        SELECT
            count(),
            countDistinct(bucket_start),
            countIf(is_valid != 1),
            min(bucket_start),
            max(bucket_start),
            countIf(quality_flags = ''),
            countIf(quality_flags = 'carried_forward')
        FROM orderbook_analysis.orderbook_features_1s_v2 FINAL
        WHERE {w}
        """,
        parameters=p,
    ).result_rows[0]
    days = client.query(
        f"""
        SELECT toDate(bucket_start), count(), countDistinct(bucket_start)
        FROM orderbook_analysis.orderbook_features_1s_v2 FINAL
        WHERE {w}
        GROUP BY toDate(bucket_start)
        ORDER BY 1
        """,
        parameters=p,
    ).result_rows
    versions = client.query(
        f"""
        SELECT parser_version, count()
        FROM orderbook_analysis.orderbook_features_1s_v2 FINAL
        WHERE {w}
        GROUP BY parser_version
        """,
        parameters=p,
    ).result_rows
    duplicates = client.query(
        f"""
        SELECT count() FROM (
          SELECT exchange, market, symbol, depth, bucket_start
          FROM orderbook_analysis.orderbook_features_1s_v2 FINAL
          WHERE {w}
          GROUP BY exchange, market, symbol, depth, bucket_start
          HAVING count() > 1
        )
        """,
        parameters=p,
    ).result_rows[0][0]
    manifest = client.query(
        """
        SELECT source_date, status, parser_version, inserted_feature_rows
        FROM orderbook_analysis.orderbook_import_manifest_v2 FINAL
        WHERE symbol = {sym:String}
          AND source_date >= toDate('2026-08-11')
          AND source_date <  toDate('2026-08-18')
        ORDER BY source_date
        """,
        parameters=p,
    ).result_rows
    cf_anom = client.query(
        f"""
        SELECT
          countIf(processed_updates != 0),
          countIf(bid_add_count != 0), countIf(bid_remove_count != 0),
          countIf(ask_add_count != 0), countIf(ask_remove_count != 0),
          countIf(bid_qty_added != 0), countIf(bid_qty_removed != 0),
          countIf(ask_qty_added != 0), countIf(ask_qty_removed != 0),
          countIf(ofi != 0),
          countIf(mid_price_change IS NOT NULL),
          countIf(imbalance_l10_change IS NOT NULL),
          countIf(imbalance_l50_change IS NOT NULL),
          count()
        FROM orderbook_analysis.orderbook_features_1s_v2 FINAL
        WHERE {w} AND quality_flags = 'carried_forward'
        """,
        parameters=p,
    ).result_rows[0]
    overflow_rows = client.query(
        """
        SELECT parser_version, count()
        FROM orderbook_analysis.orderbook_features_1s_v2 FINAL
        WHERE symbol = {sym:String}
          AND bucket_start = toDateTime64('2026-08-18 00:00:00', 3, 'UTC')
        GROUP BY parser_version
        ORDER BY parser_version
        """,
        parameters=p,
    ).result_rows
    overflow_v3 = sum(int(n) for ver, n in overflow_rows if ver == EXPECTED_PARSER)
    overflow_total = sum(int(n) for _ver, n in overflow_rows)
    seq_mismatch = client.query(
        f"""
        WITH base AS (
          SELECT
            bucket_start, quality_flags, last_update_seq,
            anyLast(IF(quality_flags = '', last_update_seq, NULL)) OVER (
              ORDER BY bucket_start
              ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
            ) AS prev_event_seq
          FROM orderbook_analysis.orderbook_features_1s_v2 FINAL
          WHERE {w}
            AND (quality_flags = '' OR quality_flags = 'carried_forward')
        )
        SELECT
          countIf(quality_flags = 'carried_forward' AND prev_event_seq IS NULL),
          countIf(quality_flags = 'carried_forward' AND prev_event_seq != last_update_seq)
        FROM base
        """,
        parameters=p,
    ).result_rows[0]

    def _fmt(ts: Any) -> str:
        if hasattr(ts, "strftime"):
            return ts.strftime("%Y-%m-%d %H:%M:%S")
        return str(ts)[:19]

    col_ok, _ = collector_ok()
    cf_n = int(cf)
    checks = {
        "logical": logical == N_EXPECTED_SECONDS,
        "distinct": distinct == N_EXPECTED_SECONDS,
        "invalid": invalid == 0,
        "event_cf": int(event) + cf_n == N_EXPECTED_SECONDS,
        "min": _fmt(first_b) == "2026-08-11 00:00:00",
        "max": _fmt(last_b) == "2026-08-17 23:59:59",
        "seven_days": len(days) == 7
        and all(int(r[1]) == N_DAY_SECONDS and int(r[2]) == N_DAY_SECONDS for r in days),
        "parser": versions == [(EXPECTED_PARSER, N_EXPECTED_SECONDS)],
        "dups": int(duplicates) == 0,
        "manifest": len(manifest) == 7
        and all(
            row[1] == "COMPLETE"
            and row[2] == EXPECTED_PARSER
            and int(row[3]) == N_DAY_SECONDS
            for row in manifest
        ),
        "cf_metrics": (cf_n == 0) or all(int(x) == 0 for x in cf_anom[:-1]),
        "cf_count": (cf_n == 0) or int(cf_anom[-1]) == cf_n,
        "cf_seq": (cf_n == 0) or (int(seq_mismatch[0]) == 0 and int(seq_mismatch[1]) == 0),
        "collector": col_ok,
        "overflow_v3": overflow_v3 == 0,
        "overflow_dplus1": _legacy_overflow_ok(symbol, overflow_rows, overflow_total),
        "no_valid_book_prefix": invalid == 0,
    }
    failed = [k for k, v in checks.items() if not v]
    if failed:
        return False, ",".join(failed)
    return True, "ok"


def write_progress_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def clickhouse_reachable(client: Any) -> bool:
    return client.query("SELECT 1").result_rows[0][0] == 1


def _cmd_list_symbols() -> int:
    validate_symbol_set()
    for s in SYMBOLS_48:
        print(s)
    return 0


def _cmd_check_window() -> int:
    ok, detail = check_window()
    print("WINDOW_OK" if ok else "WINDOW_BAD", detail)
    return 0 if ok else 2


def _cmd_check_env() -> int:
    presence = env_presence(load_env_file=True)
    bad = [k for k, v in presence.items() if v != "SET"]
    for k, v in presence.items():
        print(f"{k}={v}")
    return 0 if not bad else 2


def _cmd_check_collector() -> int:
    ok, reason = collector_ok()
    print("COLLECTOR_OK" if ok else "COLLECTOR_BAD", reason)
    return 0 if ok else 2


def _cmd_check_clickhouse() -> int:
    from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

    client = get_clickhouse_client()
    try:
        ok = clickhouse_reachable(client)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    print("CLICKHOUSE_OK" if ok else "CLICKHOUSE_BAD")
    return 0 if ok else 2


def _cmd_classify(symbol: str) -> int:
    from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

    client = get_clickhouse_client()
    try:
        klass, reason = classify_symbol(client, symbol)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    print(klass, reason)
    if klass == "INCONSISTENT":
        return 3
    return 0


def _cmd_audit(symbol: str) -> int:
    from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client

    client = get_clickhouse_client()
    try:
        ok, reason = audit_symbol(client, symbol)
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()
    print("AUDIT_PASS" if ok else "AUDIT_FAIL", symbol, reason)
    return 0 if ok else 4


def _cmd_write_progress(path: str, stdin_json: str | None) -> int:
    raw = stdin_json if stdin_json is not None else sys.stdin.read()
    payload = json.loads(raw)
    write_progress_atomic(Path(path), payload)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Orderbook V2 49-coin rollout helpers")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list-symbols")
    sub.add_parser("check-window")
    sub.add_parser("check-env")
    sub.add_parser("check-collector")
    sub.add_parser("check-clickhouse")
    p_cls = sub.add_parser("classify")
    p_cls.add_argument("symbol")
    p_aud = sub.add_parser("audit")
    p_aud.add_argument("symbol")
    p_wp = sub.add_parser("write-progress")
    p_wp.add_argument("path")
    args = p.parse_args(argv)
    if args.cmd == "list-symbols":
        return _cmd_list_symbols()
    if args.cmd == "check-window":
        return _cmd_check_window()
    if args.cmd == "check-env":
        return _cmd_check_env()
    if args.cmd == "check-collector":
        return _cmd_check_collector()
    if args.cmd == "check-clickhouse":
        return _cmd_check_clickhouse()
    if args.cmd == "classify":
        return _cmd_classify(args.symbol)
    if args.cmd == "audit":
        return _cmd_audit(args.symbol)
    if args.cmd == "write-progress":
        return _cmd_write_progress(args.path, None)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
