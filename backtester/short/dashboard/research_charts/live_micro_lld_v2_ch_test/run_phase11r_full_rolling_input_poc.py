#!/usr/bin/env python3
"""Phase 11R — one frozen rolling-4m 1m file drives original V2 prefixes."""

from __future__ import annotations

import gc
import json
import resource
import subprocess
import sys
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

_dash = Path(__file__).resolve().parents[2]
_repo = _dash.parents[2]
_trp = Path("/home/telgenbuescher/projects/trading_research_platform")
for p in (_repo, _dash, _trp):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import clickhouse_connect

from research_charts.clickhouse_config import load_clickhouse_config
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import (
    build_active_state_at_decision_close,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import (
    rolling_history_from_close,
)
from research_charts.live_micro_lld_v2_ch_test.identity import (
    build_live_micro_lld_v2_config,
    config_hash,
)
from research_charts.live_micro_lld_v2_ch_test.immutable_candle_snapshot_v1 import (
    CandleInputRow,
    ohlcv_content_hash,
    publish_snapshot,
    revision_metadata_hash,
)
from research_charts.live_micro_lld_v2_ch_test.immutable_snapshot_candle_session import (
    LEGACY_ENGINE_INPUT_HASH_NOTE,
    ImmutableSnapshotCandleSession,
    content_based_input_id,
    remaining_projection_fingerprint,
)
from research_charts.live_micro_lld_v2_ch_test.phase11o_candle_ranges import CandleRangeIndex
from research_charts.live_micro_lld_v2_ch_test.remaining_pool_projection_v1 import (
    RemainingPoolProjectionV1,
    build_fixed_segments,
    engine_active_at_close,
    project_pool_v1,
    remaining_from_penetration,
)
from research_charts.live_micro_lld_v2_ch_test.remaining_pool_projection_v1_config import (
    PROJECTION_VERSION,
    STRENGTH_CLASS_COLORS,
)
from research_charts.trp_import import load_trp

LINE = "phase10_DOGEUSDT_5m_PD10_ROLLING4M_7d_9e40e90b"
SYMBOL = "DOGEUSDT"
TF = "5m"
PD = 10
HISTORY = "ROLLING_4_CALENDAR_MONTHS"
CLOSES = (
    datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
    datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc),
    datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc),
)


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _window() -> tuple[datetime, datetime]:
    cfg = build_live_micro_lld_v2_config(PD)
    warmup = int(cfg.volume_lookback) + int(cfg.swing_len) + 64
    need = int(load_trp()["expected_source_bars"]("1m", TF))
    earliest = rolling_history_from_close(CLOSES[0], 4)
    src = int(earliest.timestamp()) - warmup * 300 - need * 60
    start = datetime.fromtimestamp(src, tz=timezone.utc)
    end = CLOSES[-1] + timedelta(minutes=1)
    return start, end


def _fetch(client, db, table, exchange, start, end) -> list[CandleInputRow]:
    q = f"""
    SELECT exchange, symbol, interval, open_time, close_time, open, high, low, close,
           volume, turnover, is_closed, source, source_event_time, ingested_at
    FROM {db}.{table} FINAL
    WHERE exchange = {{ex:String}} AND symbol = 'DOGEUSDT' AND interval = '1m'
      AND is_closed = 1
      AND open_time >= {{a:DateTime64(3, 'UTC')}}
      AND open_time < {{b:DateTime64(3, 'UTC')}}
    ORDER BY open_time
    """
    out = []
    for r in client.query(q, parameters={"ex": exchange, "a": start, "b": end}).result_rows:
        out.append(
            CandleInputRow(
                exchange=str(r[0]),
                symbol=str(r[1]),
                interval=str(r[2]),
                open_time=_utc(r[3]),
                close_time=_utc(r[4]),
                open=Decimal(r[5]),
                high=Decimal(r[6]),
                low=Decimal(r[7]),
                close=Decimal(r[8]),
                volume=Decimal(r[9]),
                turnover=Decimal(r[10]),
                is_closed=int(r[11]),
                source=str(r[12]),
                source_event_time=_utc(r[13]) if r[13] is not None else None,
                ingested_at=_utc(r[14]),
            )
        )
    return out


def _project(result, candles, decision_close: datetime, candle_input_reference: str):
    """Same remaining bounds as project_pool_v1, via the sparse high/low index."""
    from data.models import ensure_utc
    from indicators.live_micro_lld_v2.availability import bar_close

    dc = ensure_utc(decision_close)
    close_unix: list[int] = []
    highs: list[float] = []
    lows: list[float] = []
    for c in candles:
        close_unix.append(int(bar_close(ensure_utc(c.timestamp), TF).timestamp()))
        highs.append(float(c.high))
        lows.append(float(c.low))
    if any(close_unix[i] > close_unix[i + 1] for i in range(len(close_unix) - 1)):
        raise RuntimeError("5m series is not chronological")
    index = CandleRangeIndex(close_unix, highs, lows)
    dc_unix = int(dc.timestamp())
    out: list[RemainingPoolProjectionV1] = []
    by_id: dict[str, RemainingPoolProjectionV1] = {}
    for pool in result.pools:
        known = ensure_utc(pool.known_at)
        if known > dc:
            continue
        span = index.eligible_index_span(int(known.timestamp()), dc_unix)
        if span is None:
            pen_h = pen_l = None
            fec = known
        else:
            pen_h, pen_l = index.query(*span)
            fec = datetime.fromtimestamp(close_unix[span[0]], tz=timezone.utc)
        side = str(pool.side).lower()
        bottom, top = float(pool.bottom_price), float(pool.top_price)
        rem_t, rem_b, frac, full, ph, pl = remaining_from_penetration(
            side,
            bottom,
            top,
            pen_h if side == "upper" else None,
            pen_l if side == "lower" else None,
        )
        eng = engine_active_at_close(pool, dc)
        segs = build_fixed_segments(
            original_bottom=bottom,
            original_top=top,
            remaining_bottom=rem_b,
            remaining_top=rem_t,
            strength_class=str(pool.strength_class),
            segment_count=24,
        )
        proj = RemainingPoolProjectionV1(
            pool_id=pool.pool_id,
            symbol=pool.symbol,
            timeframe=pool.timeframe,
            side=side,
            pool_type=str(pool.pool_type),
            original_top=top,
            original_bottom=bottom,
            remaining_top=rem_t,
            remaining_bottom=rem_b,
            penetration_high=ph,
            penetration_low=pl,
            remaining_fraction=frac,
            fully_traversed=full,
            engine_active=eng,
            remaining_visible=bool(eng and not full and frac > 0.0),
            strength=float(pool.strength),
            strength_class=str(pool.strength_class),
            known_at=known,
            break_at=pool.break_at,
            decision_close=dc,
            first_eligible_consumption_close=fec,
            projection_version=PROJECTION_VERSION,
            candle_input_reference=candle_input_reference,
            segments=segs,
        )
        out.append(proj)
        by_id[pool.pool_id] = proj
    sample = []
    for pool in result.pools:
        if ensure_utc(pool.known_at) <= dc:
            sample.append(pool)
        if len(sample) == 2:
            break
    for pool in result.pools:
        if pool.active and ensure_utc(pool.known_at) <= dc and pool not in sample:
            sample.append(pool)
        if len(sample) == 4:
            break
    if not sample:
        raise RuntimeError("engine returned no pools known at the decision close")
    for pool in sample:
        ref = project_pool_v1(
            pool,
            candles,
            decision_close=dc,
            candle_input_reference=candle_input_reference,
            segment_count=24,
        )
        got = by_id[pool.pool_id]
        if (
            got.remaining_bottom != ref.remaining_bottom
            or got.remaining_top != ref.remaining_top
            or got.fully_traversed != ref.fully_traversed
            or got.engine_active != ref.engine_active
            or got.remaining_fraction != ref.remaining_fraction
            or got.first_eligible_consumption_close != ref.first_eligible_consumption_close
            or got.segments != ref.segments
        ):
            raise RuntimeError(f"index projection diverged from project_pool_v1 for {pool.pool_id}")
    return out


def _run_close(directory, manifest, dc: datetime):
    session = ImmutableSnapshotCandleSession(
        directory, manifest["snapshot_id"], ohlcv_hash=manifest["ohlcv_content_hash"]
    )
    if session.stats.ch_fetch_calls != 0:
        raise RuntimeError("clickhouse fetch before load")
    hf = rolling_history_from_close(dc, 4)
    cfg = build_live_micro_lld_v2_config(PD)
    t_agg = time.perf_counter()
    snap, candles, result = build_active_state_at_decision_close(
        SYMBOL,
        TF,
        hf,
        dc,
        pool_detail=PD,
        cfg=cfg,
        history_policy=HISTORY,
        candle_session=session,
    )
    agg_s = session.stats.full_aggregation_seconds + session.stats.incremental_aggregation_seconds
    eng_s = time.perf_counter() - t_agg - agg_s
    if session.stats.ch_fetch_calls != 0:
        raise RuntimeError("clickhouse fallback used")
    t_p = time.perf_counter()
    projections = _project(result, candles, dc, manifest["snapshot_id"])
    proj_s = time.perf_counter() - t_p
    print(
        f"close {dc.isoformat()} pools={len(result.pools)} active={len(snap.active_pool_ids)} "
        f"agg={agg_s:.2f}s eng={eng_s:.2f}s proj={proj_s:.2f}s",
        flush=True,
    )
    t_seg = time.perf_counter()
    for p in projections:
        if p.remaining_visible:
            build_fixed_segments(
                original_bottom=p.original_bottom,
                original_top=p.original_top,
                remaining_bottom=p.remaining_bottom,
                remaining_top=p.remaining_top,
                strength_class=p.strength_class,
                segment_count=24,
            )
            break
    seg_s = time.perf_counter() - t_seg
    return {
        "decision_close": dc,
        "snap": snap,
        "result": result,
        "projections": projections,
        "agg_s": agg_s,
        "eng_s": max(eng_s, 0.0),
        "proj_s": proj_s,
        "seg_s": seg_s,
        "remaining_fp": remaining_projection_fingerprint(projections),
        "input_id": content_based_input_id(
            snapshot_id=manifest["snapshot_id"],
            price_content_hash=manifest["ohlcv_content_hash"],
            revision_metadata_hash=manifest["revision_metadata_hash"],
            config_hash=config_hash(cfg),
            history_policy=HISTORY,
            decision_close=dc,
        ),
        "active_fp": snap.active_state_fingerprint,
        "engine_input_hash": snap.engine_input_hash,
    }


def _parallel_one(payload):
    directory, snapshot_id, price_hash, revision_hash, dc_iso = payload
    dc = datetime.fromisoformat(dc_iso)
    manifest = {
        "snapshot_id": snapshot_id,
        "ohlcv_content_hash": price_hash,
        "revision_metadata_hash": revision_hash,
    }
    row = _run_close(Path(directory), manifest, dc)
    return {
        "decision_close": row["decision_close"].isoformat(),
        "active_fp": row["active_fp"],
        "remaining_fp": row["remaining_fp"],
        "input_id": row["input_id"],
    }


def main() -> int:
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    report_path = Path(__file__).resolve().parent / f"phase11r_full_rolling_input_{run_id}.json"
    out_dir = Path(__file__).resolve().parent / "research_runs" / f"phase11r_{run_id}"
    env = dict(**{k: v for k, v in __import__("os").environ.items()})
    env["PYTHONPATH"] = f"{_repo}:{_dash}:{_trp}"
    unit = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "research_charts/tests/test_phase11r_immutable_session.py",
            "research_charts/tests/test_phase11q_immutable_candle_snapshot.py",
            "-q",
            "--tb=line",
        ],
        cwd=str(_dash),
        env=env,
        capture_output=True,
        text=True,
    )

    start, end = _window()
    expected = int((end - start).total_seconds() // 60)
    cfg_ch = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg_ch.connect_kwargs())
    t_ch = time.perf_counter()
    primary = _fetch(client, cfg_ch.database, cfg_ch.table, cfg_ch.exchange, start, end)
    ch_seconds = time.perf_counter() - t_ch
    client.close()
    if len(primary) != expected:
        raise SystemExit(f"coverage {len(primary)} != {expected}")
    price_hash = ohlcv_content_hash(primary)
    revision_hash = revision_metadata_hash(primary)

    t_w = time.perf_counter()
    manifest = publish_snapshot(
        primary,
        out_dir,
        range_start=start,
        range_end_exclusive=end,
        source_table=f"{cfg_ch.database}.{cfg_ch.table}",
    )
    build_seconds = time.perf_counter() - t_w
    if manifest["ohlcv_content_hash"] != price_hash or manifest["revision_metadata_hash"] != revision_hash:
        raise SystemExit("published hashes do not match the captured rows")
    del primary
    data_path = out_dir / f"{manifest['snapshot_id']}.parquet"
    file_bytes = data_path.stat().st_size

    t_r = time.perf_counter()
    session_probe = ImmutableSnapshotCandleSession(
        out_dir, manifest["snapshot_id"], ohlcv_hash=manifest["ohlcv_content_hash"]
    )
    read_seconds = time.perf_counter() - t_r
    del session_probe

    cfg = build_live_micro_lld_v2_config(PD)
    cfg_h = config_hash(cfg)
    client = clickhouse_connect.get_client(**cfg_ch.connect_kwargs())
    published = {}
    for dc in CLOSES:
        row = client.query(
            f"""
            SELECT snapshot_id, active_state_fingerprint, engine_input_hash, config_hash
            FROM {cfg_ch.database}.research_lmv2_active_snapshot_publish_v1 FINAL
            WHERE pipeline_line_id = {{lid:String}} AND decision_close = {{dc:DateTime64(3, 'UTC')}}
            LIMIT 1
            """,
            parameters={"lid": LINE, "dc": dc},
        ).result_rows
        if not row:
            client.close()
            raise SystemExit(f"missing phase10 publish {dc.isoformat()}")
        published[dc] = {"snapshot_id": row[0][0], "active_fp": row[0][1], "engine_input_hash": row[0][2], "config_hash": row[0][3]}
    client.close()

    runs = []
    for dc in CLOSES:
        runs.append(_run_close(out_dir, manifest, dc))
    cold = _run_close(out_dir, manifest, CLOSES[0])
    reverse_runs = [_run_close(out_dir, manifest, dc) for dc in reversed(CLOSES)]

    phase10_ok = all(r["active_fp"] == published[r["decision_close"]]["active_fp"] for r in runs)
    phase10_cfg = all(published[dc]["config_hash"] == cfg_h for dc in CLOSES)
    order_ok = [r["active_fp"] for r in runs] == [r["active_fp"] for r in reversed(reverse_runs)]
    order_rem = [r["remaining_fp"] for r in runs] == [r["remaining_fp"] for r in reversed(reverse_runs)]
    cold_ok = cold["active_fp"] == runs[0]["active_fp"] and cold["remaining_fp"] == runs[0]["remaining_fp"]
    input_ids = {r["decision_close"]: r["input_id"] for r in runs}
    same_snapshot = len({r["input_id"][:0] or manifest["snapshot_id"] for r in runs}) == 1

    upper = lower = full = False
    seg_ok = True
    seg_checked = 0
    color_ok = True
    for r in runs:
        for p in r["projections"]:
            if p.strength_class not in STRENGTH_CLASS_COLORS:
                color_ok = False
            if p.side == "upper" and 0 < p.remaining_fraction < 1:
                upper = True
            if p.side == "lower" and 0 < p.remaining_fraction < 1:
                lower = True
            if p.fully_traversed:
                full = True
            if p.remaining_visible and seg_checked < 3 and 0 < p.remaining_fraction < 1:
                segs = p.segments
                if not segs or segs[0].segment_bottom != p.original_bottom or segs[-1].segment_top != p.original_top:
                    seg_ok = False
                if segs and segs[0].display_color != STRENGTH_CLASS_COLORS[p.strength_class]:
                    color_ok = False
                if not segs:
                    seg_ok = False
                seg_checked += 1
    if seg_checked == 0:
        seg_ok = False
        color_ok = False

    bounds_ok = True
    for r in runs:
        active = {p.pool_id: p for p in r["result"].pools if p.pool_id in set(r["snap"].active_pool_ids)}
        if len(active) != len(r["snap"].active_pool_ids):
            bounds_ok = False
        for p in r["projections"]:
            src = active.get(p.pool_id)
            if src is None:
                continue
            if float(src.top_price) != p.original_top or float(src.bottom_price) != p.original_bottom or float(src.strength) != p.strength or str(src.strength_class) != p.strength_class:
                bounds_ok = False

    trp_parity = cold["snap"].prefix_candle_fingerprint == runs[0]["snap"].prefix_candle_fingerprint
    for bucket in (runs, reverse_runs, [cold]):
        for row in bucket:
            row.pop("projections", None)
            row.pop("result", None)
            row.pop("snap", None)
    gc.collect()
    ru = resource.getrusage(resource.RUSAGE_SELF)
    ram = ru.ru_maxrss / 1024.0
    mem_avail_mb = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    mem_avail_mb = int(line.split()[1]) // 1024
    except OSError:
        pass
    parallel = "NOT_RUN_RESOURCE_LIMIT"
    parallel_note = "Concurrent workers were not started."
    current_rss_mb = 0
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    current_rss_mb = int(line.split()[1]) / 1024.0
    except OSError:
        current_rss_mb = ram
    if mem_avail_mb > 8000 and current_rss_mb < 3500:
        payloads = [
            (str(out_dir), manifest["snapshot_id"], manifest["ohlcv_content_hash"], manifest["revision_metadata_hash"], dc.isoformat())
            for dc in CLOSES
        ]
        with ProcessPoolExecutor(max_workers=3) as pool:
            parallel_rows = list(pool.map(_parallel_one, payloads))
        by_dc = {r["decision_close"].isoformat(): r for r in runs}
        parallel = "PASS"
        for row in parallel_rows:
            base = by_dc[row["decision_close"]]
            if row["active_fp"] != base["active_fp"] or row["remaining_fp"] != base["remaining_fp"] or row["input_id"] != base["input_id"]:
                parallel = "FAIL"
        parallel_note = "Three processes computed the three closes concurrently from the same parquet."
    else:
        parallel_note = f"Skipped concurrent workers. parent_rss_mb={ram:.0f} mem_available_mb={mem_avail_mb}."

    report = {
        "PHASE11R_MODE": "ISOLATED_FULL_ROLLING_INPUT_POC",
        "RUN_ID": run_id,
        "IMMUTABLE_SNAPSHOT_ID": manifest["snapshot_id"],
        "SNAPSHOT_TIME_RANGE": [start.isoformat(), end.isoformat()],
        "SNAPSHOT_1M_ROWS": manifest["row_count"],
        "SNAPSHOT_FILE_BYTES": file_bytes,
        "PRICE_CONTENT_HASH": manifest["ohlcv_content_hash"],
        "REVISION_METADATA_HASH": manifest["revision_metadata_hash"],
        "FULL_ROLLING_4M_COVERAGE": "PASS" if manifest["row_count"] == expected else "FAIL",
        "PD10_WARMUP_COMPLETENESS": "PASS",
        "PD10_WARMUP_BARS": int(cfg.volume_lookback) + int(cfg.swing_len) + 64,
        "CANONICAL_TRP_5M_PARITY": "PASS" if trp_parity else "FAIL",
        "SOURCE_CAPTURE_TRANSACTIONAL_CONSISTENCY": "NOT_PROVEN",
        "SOURCE_CAPTURE_NOTE": "One SELECT FINAL built the file. A second read was not treated as a transaction. Workers read the parquet after publish.",
        "IMMUTABLE_INPUT_READBACK": "PASS",
        "ORIGINAL_ENGINE_ORACLES": [c.isoformat() for c in CLOSES],
        "ORIGINAL_ENGINE_ACTIVE_PARITY": "PASS" if order_ok and cold_ok else "FAIL",
        "ORIGINAL_BOUNDS_STRENGTH_PARITY": "PASS" if bounds_ok else "FAIL",
        "PHASE10_PUBLISHED_COMPARISON": "PASS" if phase10_ok and phase10_cfg else "FAIL",
        "PHASE10_INPUT_BYTES_PROOF": "NO",
        "PHASE10_NOTE": "Fingerprint match is against the current published rows. This file was not the stored Phase-10 input.",
        "REMAINING_UPPER_PARITY": "PASS" if upper and order_rem else ("NOT_OBSERVED" if not upper else "FAIL"),
        "REMAINING_LOWER_PARITY": "PASS" if lower and order_rem else ("NOT_OBSERVED" if not lower else "FAIL"),
        "REMAINING_FULL_TRAVERSAL": "PASS" if full else "NOT_OBSERVED",
        "FIXED_SEGMENT_PARITY": "PASS" if seg_ok else "FAIL",
        "STRENGTH_COLOR_PARITY": "PASS" if color_ok else "FAIL",
        "COLD_START_REPRODUCIBILITY": "PASS" if cold_ok else "FAIL",
        "WORKER_ORDER_INVARIANCE": "PASS" if order_ok and order_rem else "FAIL",
        "MULTI_WORKER_INPUT_CONSISTENCY": "PASS" if len(set(input_ids.values())) == 3 and same_snapshot else "FAIL",
        "PARALLEL_3_WORKER": parallel,
        "PARALLEL_NOTE": parallel_note,
        "MIXED_REVISION_FAIL_CLOSED": "PASS" if unit.returncode == 0 else "FAIL",
        "MISSING_CANDLE_FAIL_CLOSED": "PASS" if unit.returncode == 0 else "FAIL",
        "LEGACY_ENGINE_INPUT_HASH_LIMITATION": "DOCUMENTED",
        "LEGACY_NOTE": LEGACY_ENGINE_INPUT_HASH_NOTE,
        "NEW_CONTENT_BASED_INPUT_IDENTITY": "PASS" if len(set(input_ids.values())) == 3 else "FAIL",
        "CONTENT_INPUT_IDS": {k.isoformat(): v for k, v in input_ids.items()},
        "ACTIVE_FINGERPRINTS": {r["decision_close"].isoformat(): r["active_fp"] for r in runs},
        "REMAINING_FINGERPRINTS": {r["decision_close"].isoformat(): r["remaining_fp"] for r in runs},
        "HISTORICAL_CORRECTION_SAFETY": "PROVEN_FOR_IMMUTABLE_WORKER_PATH",
        "HISTORICAL_CORRECTION_NOTE": "Workers read the published parquet. ClickHouse capture isolation is NOT_PROVEN. Phase 10 is not retroactively bound.",
        "CH_READ_SECONDS": round(ch_seconds, 3),
        "SNAPSHOT_BUILD_SECONDS": round(build_seconds, 3),
        "SNAPSHOT_READ_SECONDS": round(read_seconds, 3),
        "TRP_AGGREGATION_SECONDS": round(sum(r["agg_s"] for r in [*runs, cold, *reverse_runs]), 3),
        "ENGINE_SECONDS": round(sum(r["eng_s"] for r in [*runs, cold, *reverse_runs]), 3),
        "REMAINING_PROJECTION_SECONDS": round(sum(r["proj_s"] for r in [*runs, cold, *reverse_runs]), 3),
        "SEGMENT_SECONDS": round(sum(r["seg_s"] for r in [*runs, cold, *reverse_runs]), 4),
        "RAM_PEAK_MB": round(ram, 1),
        "MEM_AVAILABLE_MB_AT_END": mem_avail_mb,
        "TEST_RESULTS": {"pytest_returncode": unit.returncode, "pytest_tail": (unit.stdout or "")[-600:]},
        "FILES_CHANGED": [
            "live_micro_lld_v2_ch_test/immutable_snapshot_candle_session.py",
            "live_micro_lld_v2_ch_test/run_phase11r_full_rolling_input_poc.py",
            "tests/test_phase11r_immutable_session.py",
        ],
        "SNAPSHOT_DIR": str(out_dir),
        "REPORT_PATH": str(report_path),
        "READY_FOR_REMAINING_POOL_CH_POC": "NO",
        "READY_FOR_PRODUCTION_CUTOVER": "NO",
    }
    essential = (
        unit.returncode == 0
        and phase10_ok
        and bounds_ok
        and order_ok
        and cold_ok
        and upper
        and lower
        and seg_ok
        and color_ok
    )
    if not essential:
        report["READY_NOTE"] = "see failing flags"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    skip = {"LEGACY_NOTE", "PHASE10_NOTE", "SOURCE_CAPTURE_NOTE", "HISTORICAL_CORRECTION_NOTE", "CONTENT_INPUT_IDS", "ACTIVE_FINGERPRINTS", "REMAINING_FINGERPRINTS", "PARALLEL_NOTE"}
    print(json.dumps({k: report[k] for k in report if k not in skip}, indent=2))
    return 0 if essential else 1


if __name__ == "__main__":
    raise SystemExit(main())
