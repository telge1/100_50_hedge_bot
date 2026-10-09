#!/usr/bin/env python3
"""Phase 11G — incremental canonical 5m materialization POC (no CH writes)."""

from __future__ import annotations

import json
import resource
import statistics
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research_charts.live_micro_lld_v2_ch_test.active_snapshot.canonical_candle_session import (
    CandleDataGapError,
    CandleRevisionConflictError,
    CanonicalCandleSession,
    candles_equal,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import (
    assemble_active_state_from_candles,
    build_active_state_at_decision_close,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import (
    assert_rolling_coverage,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
    PublishedActiveReader,
)
from research_charts.live_micro_lld_v2_ch_test.batch import _load_candles_for_window
from research_charts.live_micro_lld_v2_ch_test.identity import (
    build_live_micro_lld_v2_config,
    config_hash,
)
from research_charts.live_micro_lld_v2_ch_test.run_phase11d_canonical_candle_poc import (
    _compare_pools,
    _pick_closes,
    _pool_active_map,
)
SYMBOL = "DOGEUSDT"
TF = "5m"
PD10 = 10
PD1 = 1
PHASE10_LINE = "phase10_DOGEUSDT_5m_PD10_ROLLING4M_7d_9e40e90b"
MAIN_COUNT = 150
HISTORY_POLICY = "ROLLING_4_CALENDAR_MONTHS"


def main() -> int:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    closes = _pick_closes(MAIN_COUNT)
    cfg10 = build_live_micro_lld_v2_config(PD10)
    cfg_h = config_hash(cfg10)
    pub_reader = PublishedActiveReader()

    ref_load_times: list[float] = []
    full_load_times: list[float] = []
    inc_load_times: list[float] = []
    ref_total_times: list[float] = []
    full_total_times: list[float] = []
    inc_total_times: list[float] = []
    engine_times: list[float] = []

    flags = {
        "reference_candle": True,
        "full_cache": True,
        "incremental_cache": True,
        "engine": True,
        "membership": True,
        "strength": True,
        "phase10": True,
        "pd_shared": True,
        "gap_detection": True,
        "revision_detection": True,
    }
    failures: list[str] = []

    session_full = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=False)
    session_inc = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=True)
    cold_full = cold_inc = None

    for i, dc in enumerate(closes):
        hf = assert_rolling_coverage(dc)

        t0 = time.perf_counter()
        ref_candles, ref_meta = _load_candles_for_window(SYMBOL, TF, hf, dc, PD10)
        ref_load_times.append(time.perf_counter() - t0)

        t1 = time.perf_counter()
        full_candles, full_meta = session_full.load_for_window(hf, dc, PD10)
        full_load_times.append(time.perf_counter() - t1)
        if i == 0:
            cold_full = full_load_times[0]

        t2 = time.perf_counter()
        inc_candles, inc_meta = session_inc.load_for_window(
            hf, dc, PD10, verify_incremental_5m=(i % 15 == 0)
        )
        inc_load_times.append(time.perf_counter() - t2)
        if i == 0:
            cold_inc = inc_load_times[0]

        if not candles_equal(ref_candles, full_candles):
            flags["reference_candle"] = False
            flags["full_cache"] = False
            failures.append(f"ref_vs_full candles {dc.isoformat()}")
        if not candles_equal(ref_candles, inc_candles):
            flags["reference_candle"] = False
            flags["incremental_cache"] = False
            failures.append(f"ref_vs_inc candles {dc.isoformat()}")
        if not candles_equal(full_candles, inc_candles):
            flags["full_cache"] = False
            flags["incremental_cache"] = False
            failures.append(f"full_vs_inc candles {dc.isoformat()}")
        if ref_meta["candle_load_start"] != inc_meta["candle_load_start"]:
            flags["incremental_cache"] = False
            failures.append(f"load_start {dc.isoformat()}")

        t_eng = time.perf_counter()
        ref_snap, _, ref_res = assemble_active_state_from_candles(
            ref_candles,
            ref_meta,
            history_from_close=hf,
            decision_close=dc,
            pool_detail=PD10,
            cfg=cfg10,
            history_policy=HISTORY_POLICY,
            timeframe=TF,
        )
        eng_s = time.perf_counter() - t_eng
        engine_times.append(eng_s)

        inc_snap, _, inc_res = assemble_active_state_from_candles(
            inc_candles,
            inc_meta,
            history_from_close=hf,
            decision_close=dc,
            pool_detail=PD10,
            cfg=cfg10,
            history_policy=HISTORY_POLICY,
            timeframe=TF,
        )

        ref_total_times.append(ref_load_times[-1] + eng_s)
        full_total_times.append(full_load_times[-1] + eng_s)
        inc_total_times.append(inc_load_times[-1] + eng_s)

        if ref_snap.engine_input_hash != inc_snap.engine_input_hash:
            flags["engine"] = False
            failures.append(f"eih {dc.isoformat()}")
        if ref_snap.active_state_fingerprint != inc_snap.active_state_fingerprint:
            flags["membership"] = False
            failures.append(f"active_fp {dc.isoformat()}")
        pool_errs = _compare_pools(_pool_active_map(ref_res, dc), _pool_active_map(inc_res, dc))
        if pool_errs:
            flags["strength"] = False
            flags["engine"] = False
            failures.append(f"pools {dc.isoformat()}: {pool_errs[:3]}")

        if i % 9 == 0:
            try:
                pub = pub_reader.load_published(PHASE10_LINE, dc, config_hash=cfg_h)
                if pub["active_state_fingerprint"] != inc_snap.active_state_fingerprint:
                    flags["phase10"] = False
                    failures.append(f"phase10_fp {dc.isoformat()}")
            except Exception as exc:
                flags["phase10"] = False
                failures.append(f"phase10_read {dc.isoformat()}: {exc}")

        if i in (0, 50, 100, 149):
            full_int = build_active_state_at_decision_close(
                SYMBOL,
                TF,
                hf,
                dc,
                pool_detail=PD10,
                history_policy=HISTORY_POLICY,
                candle_session=session_full,
            )[0]
            inc_int = build_active_state_at_decision_close(
                SYMBOL,
                TF,
                hf,
                dc,
                pool_detail=PD10,
                history_policy=HISTORY_POLICY,
                candle_session=session_inc,
            )[0]
            if full_int.engine_input_hash != inc_int.engine_input_hash:
                flags["engine"] = False
                failures.append(f"integration {dc.isoformat()}")

    shared = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=True)
    for pd in (PD1, PD10):
        cfg_pd = build_live_micro_lld_v2_config(pd)
        for dc in closes[::37][:3]:
            hf = assert_rolling_coverage(dc)
            ref_c, _ = _load_candles_for_window(SYMBOL, TF, hf, dc, pd)
            cand_c, cand_m = shared.load_for_window(hf, dc, pd)
            if not candles_equal(ref_c, cand_c):
                flags["pd_shared"] = False
                failures.append(f"pd{pd} candles {dc.isoformat()}")
            ref_meta = {
                **cand_m,
                "config": cfg_pd,
                "pool_detail": pd,
            }
            ref_snap, _, _ = assemble_active_state_from_candles(
                ref_c,
                ref_meta,
                history_from_close=hf,
                decision_close=dc,
                pool_detail=pd,
                cfg=cfg_pd,
                history_policy=HISTORY_POLICY,
                timeframe=TF,
            )
            cand_snap, _, cand_res = assemble_active_state_from_candles(
                cand_c,
                cand_m,
                history_from_close=hf,
                decision_close=dc,
                pool_detail=pd,
                cfg=cfg_pd,
                history_policy=HISTORY_POLICY,
                timeframe=TF,
            )
            if ref_snap.engine_input_hash != cand_snap.engine_input_hash:
                flags["pd_shared"] = False
                failures.append(f"pd{pd} eih {dc.isoformat()}")
            if config_hash(cfg_pd) != cand_snap.config_hash:
                failures.append(f"pd{pd} cfg_hash {dc.isoformat()}")

    gap_ok = revision_ok = True
    try:
        probe = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=True)
        dc0 = closes[10]
        hf0 = assert_rolling_coverage(dc0)
        probe.load_for_window(hf0, dc0, PD10)
        cfg = build_live_micro_lld_v2_config(PD10)
        warmup_bars = int(cfg.volume_lookback) + int(cfg.swing_len) + 64
        tf_sec = 300
        from_unix = int(hf0.timestamp())
        to_unix = int(dc0.timestamp())
        load_start = max(0, from_unix - warmup_bars * tf_sec)
        need = probe._need_1m_per_5m
        src_lo = load_start - need * 60
        probe.check_1m_gap_free(src_lo, to_unix)
        if probe._1m_by_unix:
            u0 = min(probe._1m_by_unix)
            probe._1m_by_unix.pop(u0, None)
            try:
                probe.check_1m_gap_free(src_lo, to_unix)
                gap_ok = False
            except CandleDataGapError:
                pass
    except Exception as exc:
        gap_ok = False
        failures.append(f"gap_probe: {exc}")

    try:
        rev = CanonicalCandleSession(SYMBOL, TF, use_incremental_5m=True)
        dc1 = closes[11]
        hf1 = assert_rolling_coverage(dc1)
        rev.load_for_window(hf1, dc1, PD10)
        u_pick = min(rev._1m_by_unix)
        c0 = rev._1m_by_unix[u_pick]
        from data.models import Candle

        o = float(c0.open) * 1.01
        c1 = Candle(
            timestamp=c0.timestamp,
            open=o,
            high=max(float(c0.high), o) + 0.001,
            low=min(float(c0.low), o) - 0.001,
            close=float(c0.close),
            volume=float(c0.volume),
            symbol=c0.symbol,
            timeframe=c0.timeframe,
        )
        try:
            rev.ingest_1m_closed([c1])
            revision_ok = False
        except CandleRevisionConflictError:
            pass
    except Exception as exc:
        revision_ok = False
        failures.append(f"revision_probe: {exc}")

    flags["gap_detection"] = gap_ok
    flags["revision_detection"] = revision_ok

    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_mb = ru * 1024 / 1e6

    ref_sum = sum(ref_load_times)
    full_sum = sum(full_load_times)
    inc_sum = sum(inc_load_times)
    all_ok = all(flags.values()) and not failures

    report: dict[str, Any] = {
        "RUN_ID": run_id,
        "TEST_CLOSES": len(closes),
        "REFERENCE_CANDLE_PARITY": "PASS" if flags["reference_candle"] else "FAIL",
        "FULL_CACHE_PARITY": "PASS" if flags["full_cache"] else "FAIL",
        "INCREMENTAL_CACHE_PARITY": "PASS" if flags["incremental_cache"] else "FAIL",
        "ENGINE_POOL_PARITY": "PASS" if flags["engine"] else "FAIL",
        "ACTIVE_MEMBERSHIP_PARITY": "PASS" if flags["membership"] else "FAIL",
        "STRENGTH_PARITY": "PASS" if flags["strength"] else "FAIL",
        "PHASE10_PUBLISHED_PARITY": "PASS" if flags["phase10"] else "FAIL",
        "PD1_PD10_SHARED_CACHE_PARITY": "PASS" if flags["pd_shared"] else "FAIL",
        "GAP_DETECTION": "PASS" if gap_ok else "FAIL",
        "REVISION_CONFLICT_DETECTION": "PASS" if revision_ok else "FAIL",
        "HISTORICAL_CORRECTION_SAFETY": "NOT_PROVEN",
        "HISTORICAL_CORRECTION_NOTE": (
            "ClickHouse candles_1m read path exposes no durable revision/version id; "
            "conflicts detected only when the same open_time is re-fetched with different OHLCV "
            "in one process. A one-shot content hash does not prove immutability over time."
        ),
        "PRODUCTION_REVISION_MITIGATION": (
            "Store ingest_version or exchange_seq per 1m bar; on correction, bump version and "
            "invalidate affected 5m bucket opens in the materialized cache; fail closed if "
            "overlapping revisions would be mixed in one engine prefix."
        ),
        "REFERENCE_TOTAL_SECONDS": round(sum(ref_total_times), 3),
        "FULL_CACHE_TOTAL_SECONDS": round(sum(full_total_times), 3),
        "INCREMENTAL_CACHE_TOTAL_SECONDS": round(sum(inc_total_times), 3),
        "REFERENCE_LOAD_SECONDS": round(ref_sum, 3),
        "FULL_CACHE_LOAD_SECONDS": round(full_sum, 3),
        "INCREMENTAL_CACHE_LOAD_SECONDS": round(inc_sum, 3),
        "FULL_CACHE_AGGREGATION_SECONDS": round(session_full.stats.full_aggregation_seconds, 3),
        "INCREMENTAL_AGGREGATION_SECONDS": round(
            session_inc.stats.incremental_aggregation_seconds, 3
        ),
        "INCREMENTAL_WINDOW_SLICE_SECONDS": round(session_inc.stats.window_slice_seconds, 3),
        "MEAN_ENGINE_SECONDS": round(statistics.mean(engine_times), 4),
        "COLD_START_FULL_SECONDS": round(cold_full or 0, 4),
        "COLD_START_INCREMENTAL_SECONDS": round(cold_inc or 0, 4),
        "MEASURED_SPEEDUP_VS_REFERENCE": {
            "load_sum_ratio": round(ref_sum / max(inc_sum, 1e-9), 3),
            "total_sum_ratio": round(sum(ref_total_times) / max(sum(inc_total_times), 1e-9), 3),
        },
        "MEASURED_SPEEDUP_VS_PHASE11D_FULL_CACHE": {
            "load_sum_ratio": round(full_sum / max(inc_sum, 1e-9), 3),
            "note": "Phase 11D original 150-close report was overwritten; compare to full-cache arm in this run.",
        },
        "CH_FETCH_CALLS": {
            "full_session": session_full.stats.ch_fetch_calls,
            "incremental_session": session_inc.stats.ch_fetch_calls,
        },
        "INCREMENTAL_BUCKET_RECOMPUTATIONS": session_inc.stats.incremental_bucket_recomputations,
        "FULL_AGGREGATION_CALLS": session_full.stats.full_5m_reaggregations,
        "FULL_5M_REBUILDS": session_inc.stats.full_5m_rebuilds,
        "RAM_PEAK_MB": round(peak_mb, 2),
        "SESSION_1M_BARS_INCREMENTAL": len(session_inc._1m_by_unix),
        "SESSION_5M_BARS_INCREMENTAL": len(session_inc._5m_by_open_unix),
        "FAILURES_SAMPLE": failures[:30],
        "FILES_CHANGED": [
            "active_snapshot/canonical_candle_session.py",
            "run_phase11g_incremental_candle_poc.py",
            "tests/test_canonical_candle_session.py",
            "tests/test_canonical_candle_session_incremental.py",
        ],
        "READY_FOR_INCREMENTAL_3_WORKER_POC": "YES" if all_ok else "NO",
    }

    out = Path(__file__).with_name(f"phase11g_incremental_candle_{run_id}.json")
    report["REPORT_PATH"] = str(out)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
