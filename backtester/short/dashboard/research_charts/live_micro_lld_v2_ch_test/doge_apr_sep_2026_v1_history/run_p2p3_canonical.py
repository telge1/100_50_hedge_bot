#!/usr/bin/env python3
"""P2/P3: canonical frozen + isolated V2 reproducibility pilots (no mass production)."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

for _entry in (
    Path(__file__).resolve().parents[3],
    Path("/home/telgenbuescher/projects/trading_research_platform"),
):
    _text = str(_entry)
    if _text not in sys.path:
        sys.path.insert(0, _text)

from research_charts.clickhouse_config import load_clickhouse_config
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import (
    build_active_state_at_decision_close,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.parallel_verify import (
    compare_records,
    verification_record,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import (
    HISTORY_POLICY_ROLLING_4M,
)
from research_charts.live_micro_lld_v2_ch_test.identity import (
    ENGINE_VERSION,
    build_live_micro_lld_v2_config,
    config_hash,
    effective_config_dict,
)
from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import (
    CandleInputRow,
    ohlcv_content_hash,
    publish_snapshot,
    require_snapshot,
    revision_metadata_hash,
)
from research_charts.live_micro_lld_v2_ch_test.phase11l.compare import compare_published_close
from research_charts.live_micro_lld_v2_ch_test.phase11m.session import ReusedSnapshotSession
from research_charts.live_micro_lld_v2_ch_test.doge_multi_month_2026_history.run_multi_month import (
    _client,
    _utc,
)

HERE = Path(__file__).resolve().parent
INPUTS = HERE / "inputs"
PILOT = HERE / "pilot"
REPORT = HERE / "p2p3_report.json"

RANGE_START = datetime(2025, 9, 30, 20, 0, tzinfo=timezone.utc)
RANGE_END = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)

CFAE_DIR = HERE.parent / "doge_multi_month_2026_history" / "inputs"
FE549_DIR = HERE.parent / "doge_september_2026_history" / "inputs"
CFAE_ID = "cfae2571806c823407767e4d81da82fe984a928ce29843c9f061fc66d79c115b"
FE549_ID = "fe549309ef047ea092bd1fa3eba5c16fae647db7f3fad95dc567b47c66ebcb66"

MM_ROOT = HERE.parent / "doge_multi_month_2026_history" / "production" / "results"
SEP_ROOT = HERE.parent / "doge_september_2026_history" / "production" / "results"


def _fetch_rows() -> list[CandleInputRow]:
    cfg, client = _client()
    query = f"""
    SELECT exchange, symbol, interval, open_time, close_time, open, high, low, close,
           volume, turnover, is_closed, source, source_event_time, ingested_at
    FROM {cfg.database}.{cfg.table} FINAL
    WHERE exchange = {{ex:String}} AND symbol = 'DOGEUSDT' AND interval = '1m'
      AND is_closed = 1
      AND open_time >= {{a:DateTime64(3, 'UTC')}}
      AND open_time < {{b:DateTime64(3, 'UTC')}}
    ORDER BY open_time
    """
    try:
        rows = client.query(
            query, parameters={"ex": cfg.exchange, "a": RANGE_START, "b": RANGE_END}
        ).result_rows
    finally:
        client.close()
    out = []
    for row in rows:
        out.append(
            CandleInputRow(
                exchange=str(row[0]),
                symbol=str(row[1]),
                interval=str(row[2]),
                open_time=_utc(row[3]),
                close_time=_utc(row[4]),
                open=Decimal(row[5]),
                high=Decimal(row[6]),
                low=Decimal(row[7]),
                close=Decimal(row[8]),
                volume=Decimal(row[9]),
                turnover=Decimal(row[10]),
                is_closed=int(row[11]),
                source=str(row[12]),
                source_event_time=_utc(row[13]) if row[13] is not None else None,
                ingested_at=_utc(row[14]),
            )
        )
    return out


def _parse_open_time(ot) -> datetime:
    if isinstance(ot, str):
        ot = ot.replace("Z", "+00:00")
        return datetime.fromisoformat(ot)
    if isinstance(ot, (int, float)):
        return datetime.fromtimestamp(int(ot), tz=timezone.utc)
    if hasattr(ot, "to_pydatetime"):
        ot = ot.to_pydatetime()
    if ot.tzinfo is None:
        ot = ot.replace(tzinfo=timezone.utc)
    return ot


def _ohlcv_map_from_parquet(path: Path, lo: datetime, hi: datetime) -> dict[int, tuple]:
    import pyarrow.parquet as pq

    t = pq.read_table(
        path, columns=["open_time", "open", "high", "low", "close", "volume"]
    )
    d = t.to_pydict()
    m = {}
    for ot, o, h, l, c, v in zip(
        d["open_time"], d["open"], d["high"], d["low"], d["close"], d["volume"]
    ):
        ot = _parse_open_time(ot)
        if lo <= ot <= hi:
            m[int(ot.timestamp())] = (
                round(float(o), 10),
                round(float(h), 10),
                round(float(l), 10),
                round(float(c), 10),
                round(float(v), 10),
            )
    return m


def _parity_maps(a: dict, b: dict) -> dict:
    only_a = set(a) - set(b)
    only_b = set(b) - set(a)
    common = set(a) & set(b)
    diff = 0
    first = None
    for u in sorted(common):
        if a[u] != b[u]:
            diff += 1
            if first is None:
                first = (u, a[u], b[u])
    return {
        "only_a": len(only_a),
        "only_b": len(only_b),
        "common": len(common),
        "ohlcv_diff": diff,
        "first_diff_unix": first[0] if first else None,
    }


def _engine_code_hash() -> str:
    root = Path("/home/telgenbuescher/projects/trading_research_platform/indicators/live_micro_lld_v2")
    h = hashlib.sha256()
    for p in sorted(root.rglob("*.py")):
        if "tests" in p.parts:
            continue
        h.update(str(p.relative_to(root)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def _run_close(
    session: ReusedSnapshotSession,
    *,
    timeframe: str,
    pool_detail: int,
    decision_iso: str,
    out_name: str,
) -> dict:
    dc = datetime.fromisoformat(decision_iso.replace("Z", "+00:00"))
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import (
        rolling_history_from_close,
    )

    history = rolling_history_from_close(dc, 4)
    snap, candles, result = build_active_state_at_decision_close(
        "DOGEUSDT",
        timeframe,
        history,
        dc,
        pool_detail=pool_detail,
        history_policy=HISTORY_POLICY_ROLLING_4M,
        candle_session=session,
    )
    record = verification_record(snap, candles, result, dc)
    record["pilot_frozen_id"] = session.snapshot_id
    path = PILOT / out_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    return record


def _load_ref(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_existing_manifest(snapshot_id: str) -> dict:
    path = INPUTS / f"{snapshot_id}.manifest.json"
    if not path.is_file():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reuse-frozen",
        metavar="SNAPSHOT_ID",
        help="Skip CH fetch; use existing immutable input under inputs/",
    )
    args = parser.parse_args()

    PILOT.mkdir(parents=True, exist_ok=True)
    INPUTS.mkdir(parents=True, exist_ok=True)

    report: dict = {"phase": "P2P3", "started_at": datetime.now(timezone.utc).isoformat()}

    expected = int((RANGE_END - RANGE_START).total_seconds() // 60)
    report["expected_minutes"] = expected

    if args.reuse_frozen:
        manifest = _load_existing_manifest(args.reuse_frozen)
        loaded = require_snapshot(
            INPUTS, manifest["snapshot_id"], ohlcv_hash=manifest["ohlcv_content_hash"]
        )
        report["fetch_row_count"] = "skipped_reuse"
    else:
        rows = _fetch_rows()
        report["fetch_row_count"] = len(rows)
        if len(rows) != expected:
            report["RAW_1M_INTEGRITY"] = "FAIL"
            report["stop_reason"] = "incomplete minute span"
            REPORT.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
            return 1

        manifest = publish_snapshot(
            rows,
            INPUTS,
            range_start=RANGE_START,
            range_end_exclusive=RANGE_END,
            source_table="signal_generator.candles_1m",
        )
        loaded = require_snapshot(
            INPUTS, manifest["snapshot_id"], ohlcv_hash=manifest["ohlcv_content_hash"]
        )
    if len(loaded) != expected:
        report["RAW_1M_INTEGRITY"] = "FAIL"
        REPORT.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        return 1
    report["NEW_FROZEN_ID"] = manifest["snapshot_id"]
    report["NEW_FROZEN_RANGE"] = {
        "start": manifest["range_start"],
        "end_exclusive": manifest["range_end_exclusive"],
    }
    report["NEW_FROZEN_1M_ROWS"] = len(loaded)
    report["RAW_1M_INTEGRITY"] = "PASS"

    new_pq = INPUTS / f"{manifest['snapshot_id']}.parquet"
    cfae_pq = CFAE_DIR / f"{CFAE_ID}.parquet"
    fe549_pq = FE549_DIR / f"{FE549_ID}.parquet"

    cfae_lo = datetime(2025, 12, 11, tzinfo=timezone.utc)
    cfae_hi = datetime(2026, 8, 31, 23, 59, tzinfo=timezone.utc)
    fe_lo = datetime(2026, 2, 28, 20, 0, tzinfo=timezone.utc)
    fe_hi = datetime(2026, 8, 31, 23, 59, tzinfo=timezone.utc)

    new_map_full = _ohlcv_map_from_parquet(new_pq, cfae_lo, cfae_hi)
    cfae_map = _ohlcv_map_from_parquet(cfae_pq, cfae_lo, cfae_hi)
    p_cfae = _parity_maps(new_map_full, cfae_map)
    report["RAW_1M_PARITY_WITH_CFAE"] = "PASS" if p_cfae["ohlcv_diff"] == 0 and p_cfae["only_a"] == 0 and p_cfae["only_b"] == 0 else "DIFFERENT"
    report["parity_cfae_detail"] = p_cfae

    new_map_fe = _ohlcv_map_from_parquet(new_pq, fe_lo, fe_hi)
    fe_map = _ohlcv_map_from_parquet(fe549_pq, fe_lo, fe_hi)
    p_fe = _parity_maps(new_map_fe, fe_map)
    report["RAW_1M_PARITY_WITH_FE549"] = "PASS" if p_fe["ohlcv_diff"] == 0 and p_fe["only_a"] == 0 and p_fe["only_b"] == 0 else "DIFFERENT"
    report["parity_fe549_detail"] = p_fe

    if report["RAW_1M_PARITY_WITH_CFAE"] != "PASS" or report["RAW_1M_PARITY_WITH_FE549"] != "PASS":
        report["stop_reason"] = "parity failure"
        REPORT.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        return 1

    report["ENGINE_VERSION"] = ENGINE_VERSION
    report["ENGINE_CODE_HASH"] = _engine_code_hash()
    report["ENGINE_CONFIG_PARITY"] = {
        pd: config_hash(build_live_micro_lld_v2_config(pd))
        for pd in (1, 5, 10)
    }
    report["ENGINE_CONFIG_EFFECTIVE_PD10"] = effective_config_dict(build_live_micro_lld_v2_config(10))
    report["HISTORY_POLICY"] = HISTORY_POLICY_ROLLING_4M

    def _session(tf: str) -> ReusedSnapshotSession:
        return ReusedSnapshotSession(
            loaded,
            snapshot_id=manifest["snapshot_id"],
            ohlcv_hash=manifest["ohlcv_content_hash"],
            timeframe=tf,
        )

    april = {}
    for tf, pd in [("15m", 1), ("15m", 5), ("15m", 10), ("4h", 10)]:
        name = f"april_20260401_{tf}_PD{pd}.json"
        try:
            rec = _run_close(
                _session(tf),
                timeframe=tf,
                pool_detail=pd,
                decision_iso="2026-04-01T00:00:00+00:00",
                out_name=name,
            )
            april[f"{tf}_PD{pd}"] = {
                "status": "PASS",
                "active_count": len(rec["active_pool_ids"]),
                "fingerprint": rec["active_state_fingerprint"],
            }
        except Exception as exc:
            april[f"{tf}_PD{pd}"] = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
    report["APRIL_PILOT_RESULTS"] = april
    report["APRIL_FIRST_CLOSE_FEASIBILITY"] = (
        "PASS"
        if all(v.get("status") == "PASS" for v in april.values())
        else "FAIL"
    )

    july_ref_path = MM_ROOT / "2026-07" / "15m_PD10" / "20260707T070000Z.json"
    july_pilot = _run_close(
        _session("15m"),
        timeframe="15m",
        pool_detail=10,
        decision_iso="2026-07-07T07:00:00+00:00",
        out_name="july_20260707_15m_PD10.json",
    )
    ref = _load_ref(july_ref_path)
    cmp_july = compare_records(ref, july_pilot)
    pub_errs = compare_published_close(
        july_pilot,
        ref,
        [ref["pools"][pid] for pid in ref["active_pool_ids"]],
    )
    report["JULY_REFERENCE_RESULTS"] = {
        "compare_records_errors": cmp_july,
        "compare_published_errors": pub_errs,
        "ref_fingerprint": ref["active_state_fingerprint"],
        "pilot_fingerprint": july_pilot["active_state_fingerprint"],
        "ref_frozen": (ref.get("frozen_input") or {}).get("snapshot_id"),
        "ref_active_count": len(ref["active_pool_ids"]),
        "pilot_active_count": len(july_pilot["active_pool_ids"]),
    }
    report["JULY_REPRODUCIBILITY"] = (
        "PASS" if not cmp_july and not pub_errs else "DIFFERENT"
    )

    boundaries = []
    cases = [
        ("2026-06-30T20:00:00+00:00", "4h", 10, "2026-06", MM_ROOT),
        ("2026-07-01T00:00:00+00:00", "15m", 10, "2026-07", MM_ROOT),
        ("2026-08-31T20:00:00+00:00", "4h", 10, "2026-08", MM_ROOT),
        ("2026-09-01T00:00:00+00:00", "15m", 10, None, SEP_ROOT),
    ]
    for dc_iso, tf, pd, month, root in cases:
        stem = datetime.fromisoformat(dc_iso.replace("Z", "+00:00")).strftime("%Y%m%dT%H%M%SZ")
        if month:
            ref_p = root / month / f"{tf}_PD{pd}" / f"{stem}.json"
        else:
            ref_p = root / f"{tf}_PD{pd}" / f"{stem}.json"
        pilot_name = f"boundary_{stem}_{tf}_PD{pd}.json"
        pilot_rec = _run_close(
            _session(tf), timeframe=tf, pool_detail=pd, decision_iso=dc_iso, out_name=pilot_name
        )
        if ref_p.is_file():
            ref_rec = _load_ref(ref_p)
            errs = compare_records(ref_rec, pilot_rec)
            pub = compare_published_close(
                pilot_rec,
                ref_rec,
                [ref_rec["pools"][pid] for pid in ref_rec["active_pool_ids"]],
            )
            boundaries.append(
                {
                    "decision_close": dc_iso,
                    "combo": f"{tf}_PD{pd}",
                    "compare_records": errs,
                    "compare_published": pub,
                    "status": "PASS" if not errs and not pub else "DIFFERENT",
                }
            )
        else:
            boundaries.append(
                {
                    "decision_close": dc_iso,
                    "combo": f"{tf}_PD{pd}",
                    "status": "NO_REFERENCE_FILE",
                    "path": str(ref_p),
                }
            )
    report["CATALOG_BOUNDARY_RESULTS"] = boundaries

    july_aug_ok = all(
        b.get("status") == "PASS"
        for b in boundaries
        if b["decision_close"]
        in (
            "2026-06-30T20:00:00+00:00",
            "2026-07-01T00:00:00+00:00",
            "2026-08-31T20:00:00+00:00",
        )
    )
    report["JULY_AUGUST_REUSE_ELIGIBLE"] = "YES" if report["JULY_REPRODUCIBILITY"] == "PASS" and july_aug_ok else "NOT_PROVEN"

    sep_case = next(b for b in boundaries if b["decision_close"].startswith("2026-09-01"))
    report["SEPTEMBER_REUSE_ELIGIBLE"] = (
        "YES" if sep_case.get("status") == "PASS" else "NOT_PROVEN"
    )

    report["RAW_1M_PARITY"] = "PASS"
    report["GUARD_OFF_BACKTEST_FEASIBILITY"] = (
        "YES"
        if report["APRIL_FIRST_CLOSE_FEASIBILITY"] == "PASS"
        else "PARTIAL — pools missing until production; strategy_loop supports floor_guard_enabled=False"
    )
    report["REMAINING_PRODUCTION_WORK"] = {
        "note": "Mass production not started; ~9148 Apr-Jun strategy closes still missing vs full calendar",
        "pilot_outputs": str(PILOT),
    }
    report["NEXT_SAFE_STEP"] = (
        "Separate approval for gap-fill production under NEW_FROZEN_ID into doge_apr_sep_2026_v1/production/results"
    )
    report["EXISTING_FROZEN_MODIFIED"] = "NO"
    report["EXISTING_SNAPSHOTS_MODIFIED"] = "NO"
    report["V2_ENGINE_MODIFIED"] = "NO"
    report["MASS_PRODUCTION_STARTED"] = "NO"
    report["FULL_BACKTEST_STARTED"] = "NO"
    report["SCANNERS_TOUCHED"] = "NO"
    report["COLLECTORS_TOUCHED"] = "NO"
    report["DASHBOARD_RESTARTED"] = "NO"
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    REPORT.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
