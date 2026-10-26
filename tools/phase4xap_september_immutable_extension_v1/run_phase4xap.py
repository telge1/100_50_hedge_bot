#!/usr/bin/env python3
"""
Phase 4X-AP — September 2026 immutable 1m extension (staging only).

- Read-only CH preflight for September
- Publishes merged snapshot under doge_multi_month_2026_history/staging/phase4xap_sep_immutable_extension_v1/inputs
- Does NOT modify doge_multi_month_2026_history/inputs (production manifest)
- Does NOT run 4X-AO resume or CH publish
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POOLS_DASH = ROOT / "backtester/short/dashboard"
LMV2 = POOLS_DASH / "research_charts/live_micro_lld_v2_ch_test"
BASE_INPUTS = LMV2 / "doge_multi_month_2026_history/inputs"
OUT_INPUTS = LMV2 / "doge_multi_month_2026_history/staging/phase4xap_sep_immutable_extension_v1/inputs"
RESULTS = ROOT / "results/phase4xap_september_immutable_extension_v1"

OLD_ID = "cfae2571806c823407767e4d81da82fe984a928ce29843c9f061fc66d79c115b"
OLD_HASH = "a0e6bd603bb705671deb9ad776672f807f469a56de1b90fb9a365337a01f240b"
RANGE_START = datetime(2025, 12, 11, 0, 0, tzinfo=timezone.utc)
SEP_START = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
RANGE_END = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
EXPECTED_SEP_ROWS = 43200


def _setup_paths() -> None:
    for p in (ROOT, Path("/home/telgenbuescher/projects/trading_research_platform"), POOLS_DASH):
        if p.is_dir() and str(p) not in sys.path:
            sys.path.insert(0, str(p))
    from research_charts.trp_import import ensure_trp_on_path

    ensure_trp_on_path()


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def preflight_september_ch() -> dict:
    import clickhouse_connect
    from research_charts.clickhouse_config import load_clickhouse_config

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    row = client.query(
        f"""
        SELECT count(), countDistinct(open_time), min(open_time), max(open_time),
               sum(is_closed = 0), sum(open > high OR low > open OR low > close OR close > high),
               sum(volume < 0)
        FROM {db}.candles_1m FINAL
        WHERE exchange = 'bybit' AND symbol = 'DOGEUSDT' AND interval = '1m'
          AND is_closed = 1
          AND open_time >= {{a:DateTime64(3, 'UTC')}}
          AND open_time < {{b:DateTime64(3, 'UTC')}}
        """,
        parameters={"a": SEP_START, "b": RANGE_END},
    ).result_rows[0]
    dups = client.query(
        f"""
        SELECT count() FROM (
          SELECT open_time FROM {db}.candles_1m FINAL
          WHERE exchange='bybit' AND symbol='DOGEUSDT' AND interval='1m' AND is_closed=1
            AND open_time >= {{a:DateTime64(3,'UTC')}} AND open_time < {{b:DateTime64(3,'UTC')}}
          GROUP BY open_time HAVING count() > 1
        )
        """,
        parameters={"a": SEP_START, "b": RANGE_END},
    ).result_rows[0][0]
    client.close()
    n, nd, min_ot, max_ot, open_rows, ohlc_bad, vol_bad = row
    ok = (
        int(n) == EXPECTED_SEP_ROWS
        and int(nd) == EXPECTED_SEP_ROWS
        and int(dups) == 0
        and int(open_rows) == 0
        and int(ohlc_bad) == 0
        and int(vol_bad) == 0
        and _utc(min_ot) == SEP_START
        and _utc(max_ot) == RANGE_END - timedelta(minutes=1)
    )
    return {
        "verdict": "SOURCE_COVERAGE_PASS" if ok else "BLOCKED_SOURCE_COVERAGE",
        "row_count": int(n),
        "distinct_open_time": int(nd),
        "min_open_time": str(min_ot),
        "max_open_time": str(max_ot),
        "duplicate_minutes": int(dups),
        "expected_rows": EXPECTED_SEP_ROWS,
    }


def build_merged_snapshot() -> dict:
    import clickhouse_connect
    from research_charts.clickhouse_config import load_clickhouse_config
    from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import (
        CandleInputRow,
        SOURCE_QUERY_SEMANTICS,
        ohlcv_content_hash,
        publish_snapshot,
        require_snapshot,
        revision_metadata_hash,
    )

    OUT_INPUTS.mkdir(parents=True, exist_ok=True)
    base_rows = require_snapshot(BASE_INPUTS, OLD_ID, ohlcv_hash=OLD_HASH)
    assert ohlcv_content_hash(base_rows) == OLD_HASH
    assert base_rows[-1].open_time == datetime(2026, 8, 31, 23, 59, tzinfo=timezone.utc)

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    q = f"""
    SELECT exchange, symbol, interval, open_time, close_time, open, high, low, close,
           volume, turnover, is_closed, source, source_event_time, ingested_at
    FROM {db}.candles_1m FINAL
    WHERE exchange = {{ex:String}} AND symbol = 'DOGEUSDT' AND interval = '1m'
      AND is_closed = 1
      AND open_time >= {{a:DateTime64(3, 'UTC')}}
      AND open_time < {{b:DateTime64(3, 'UTC')}}
    ORDER BY open_time
    """
    sep_rows: list[CandleInputRow] = []
    for record in client.query(q, parameters={"ex": "bybit", "a": SEP_START, "b": RANGE_END}).result_rows:
        sep_rows.append(
            CandleInputRow(
                exchange=str(record[0]),
                symbol=str(record[1]),
                interval=str(record[2]),
                open_time=_utc(record[3]),
                close_time=_utc(record[4]),
                open=Decimal(record[5]),
                high=Decimal(record[6]),
                low=Decimal(record[7]),
                close=Decimal(record[8]),
                volume=Decimal(record[9]),
                turnover=Decimal(record[10]),
                is_closed=int(record[11]),
                source=str(record[12]),
                source_event_time=_utc(record[13]) if record[13] is not None else None,
                ingested_at=_utc(record[14]),
            )
        )
    client.close()
    if len(sep_rows) != EXPECTED_SEP_ROWS:
        raise RuntimeError(f"September fetch {len(sep_rows)} != {EXPECTED_SEP_ROWS}")
    merged = list(base_rows) + sep_rows
    manifest = publish_snapshot(
        merged,
        OUT_INPUTS,
        range_start=RANGE_START,
        range_end_exclusive=RANGE_END,
        source_table="signal_generator.candles_1m",
    )
    loaded = require_snapshot(OUT_INPUTS, manifest["snapshot_id"], ohlcv_hash=manifest["ohlcv_content_hash"])
    if ohlcv_content_hash(loaded[: len(base_rows)]) != OLD_HASH:
        raise RuntimeError("prefix ohlcv hash mismatch vs base snapshot")
    provenance = {
        "phase": "4X-AP",
        "created_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base_snapshot_id": OLD_ID,
        "base_ohlcv_content_hash": OLD_HASH,
        "base_revision_metadata_hash": revision_metadata_hash(base_rows),
        "merged_snapshot_id": manifest["snapshot_id"],
        "merged_ohlcv_content_hash": manifest["ohlcv_content_hash"],
        "merged_revision_metadata_hash": manifest["revision_metadata_hash"],
        "merged_directory": str(OUT_INPUTS),
        "prefix_integrity_ohlcv_hash_match": True,
        "source_query_semantics": SOURCE_QUERY_SEMANTICS,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    (RESULTS / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def run_verification(manifest: dict) -> dict:
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import (
        build_active_state_at_decision_close,
    )
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.parallel_verify import verification_record
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import (
        HISTORY_POLICY_ROLLING_4M,
        rolling_history_from_close,
    )
    from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import require_snapshot
    from research_charts.live_micro_lld_v2_ch_test.phase11m.session import ReusedSnapshotSession

    sid = manifest["snapshot_id"]
    ph = manifest["ohlcv_content_hash"]
    loaded = require_snapshot(OUT_INPUTS, sid, ohlcv_hash=ph)
    session = ReusedSnapshotSession(loaded, snapshot_id=sid, ohlcv_hash=ph, timeframe="5m")
    session.max_1m_bars = 500_000

    boundary = {}
    for iso in (
        "2026-08-31T23:55:00Z",
        "2026-09-01T00:00:00Z",
        "2026-09-01T00:05:00Z",
        "2026-09-12T12:00:00Z",
        "2026-09-30T23:55:00Z",
    ):
        dc = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        hist = rolling_history_from_close(dc, 4)
        try:
            snap, candles, result = build_active_state_at_decision_close(
                "DOGEUSDT",
                "5m",
                hist,
                dc,
                pool_detail=1,
                history_policy=HISTORY_POLICY_ROLLING_4M,
                candle_session=session,
            )
            boundary[iso] = {"ok": True, "active": len(snap.active_pool_ids), "candles": len(candles)}
        except Exception as exc:
            boundary[iso] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    sep_frozen = (
        LMV2 / "doge_multi_month_2026_history/staging/phase6xt_5m_pd1_jan_sep_v1/results/2026-09/5m_PD1"
    )
    if not sep_frozen.is_dir():
        sep_frozen = LMV2 / "doge_multi_month_2026_history/production/results/2026-09/5m_PD1"

    parity = {}
    for stem in (
        "20260901T000000Z",
        "20260909T000000Z",
        "20260912T120000Z",
        "20260917T235500Z",
        "20260930T235500Z",
    ):
        path = sep_frozen / f"{stem}.json"
        if not path.is_file():
            parity[stem] = {"ok": False, "error": "frozen_missing"}
            continue
        frozen = json.loads(path.read_text(encoding="utf-8"))
        dc = datetime.strptime(stem, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        hist = rolling_history_from_close(dc, 4)
        snap, candles, result = build_active_state_at_decision_close(
            "DOGEUSDT",
            "5m",
            hist,
            dc,
            pool_detail=1,
            history_policy=HISTORY_POLICY_ROLLING_4M,
            candle_session=session,
        )
        rec = verification_record(snap, candles, result, dc)
        parity[stem] = {
            "ok": (
                frozen.get("engine_input_hash") == rec.get("engine_input_hash")
                and frozen.get("active_state_fingerprint") == rec.get("active_state_fingerprint")
                and sorted(frozen.get("active_pool_ids") or []) == sorted(snap.active_pool_ids)
            ),
            "engine_input_hash_match": frozen.get("engine_input_hash") == rec.get("engine_input_hash"),
            "fingerprint_match": frozen.get("active_state_fingerprint") == rec.get("active_state_fingerprint"),
            "active_ids_match": sorted(frozen.get("active_pool_ids") or []) == sorted(snap.active_pool_ids),
        }

    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "boundary_tests.json").write_text(json.dumps(boundary, indent=2), encoding="utf-8")
    (RESULTS / "parity_samples.json").write_text(json.dumps(parity, indent=2), encoding="utf-8")
    summary = {
        "merged_snapshot_id": sid,
        "merged_ohlcv_content_hash": ph,
        "boundary_all_ok": all(v.get("ok") for v in boundary.values()),
        "parity_all_ok": all(v.get("ok") for v in parity.values()),
    }
    if not summary["boundary_all_ok"]:
        summary["verdict"] = "BLOCKED_OTHER"
    elif not summary["parity_all_ok"]:
        summary["verdict"] = "BLOCKED_PARITY"
    else:
        summary["verdict"] = "SEPTEMBER_INPUT_READY_FOR_RESUME_REVIEW"
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 4X-AP September immutable extension")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--skip-build", action="store_true", help="Use existing staging manifest")
    args = parser.parse_args()
    _setup_paths()
    RESULTS.mkdir(parents=True, exist_ok=True)
    pf = preflight_september_ch()
    (RESULTS / "source_preflight.json").write_text(json.dumps(pf, indent=2), encoding="utf-8")
    print(json.dumps(pf, indent=2))
    if pf["verdict"] != "SOURCE_COVERAGE_PASS":
        return 1
    if args.preflight_only:
        return 0
    if args.skip_build and (RESULTS / "manifest.json").is_file():
        manifest = json.loads((RESULTS / "manifest.json").read_text(encoding="utf-8"))
    else:
        manifest = build_merged_snapshot()
    summary = run_verification(manifest)
    print(json.dumps(summary, indent=2))
    return 0 if summary.get("verdict") == "SEPTEMBER_INPUT_READY_FOR_RESUME_REVIEW" else 1


if __name__ == "__main__":
    raise SystemExit(main())
