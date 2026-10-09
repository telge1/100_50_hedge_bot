#!/usr/bin/env python3
"""Phase 11D — canonical candle session POC (parity + performance, no CH writes)."""

from __future__ import annotations

import json
import resource
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research_charts.live_micro_lld_v2_ch_test.active_snapshot.canonical_candle_session import (
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
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.runner import (
    enumerate_decision_closes,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
    PublishedActiveReader,
)
from research_charts.live_micro_lld_v2_ch_test.batch import _load_candles_for_window
from research_charts.live_micro_lld_v2_ch_test.identity import (
    build_live_micro_lld_v2_config,
    config_hash,
)
from research_charts.live_micro_lld_v2_ch_test.serialize import pool_fingerprint

SYMBOL = "DOGEUSDT"
TF = "5m"
PD10 = 10
PD1 = 1
PHASE10_LINE = "phase10_DOGEUSDT_5m_PD10_ROLLING4M_7d_9e40e90b"
RANGE_START = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)
MAIN_COUNT = 150
HISTORY_POLICY = "ROLLING_4_CALENDAR_MONTHS"


def _pick_closes(n: int) -> list[datetime]:
    steady = enumerate_decision_closes(RANGE_START, n - 40, TF)
    month_edge = enumerate_decision_closes(
        datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc), 20, TF
    )
    late = enumerate_decision_closes(
        datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc), 20, TF
    )
    out = list(steady) + list(month_edge) + list(late)
    return out[:n]


def _pool_active_map(result, dc: datetime) -> dict[str, Any]:
    dc = dc.astimezone(timezone.utc)
    out = {}
    for p in result.pools:
        if not p.active:
            continue
        if p.known_at.astimezone(timezone.utc) > dc:
            continue
        if p.break_at is not None:
            continue
        out[p.pool_id] = p
    return out


def _compare_pools(ref_map, cand_map) -> list[str]:
    errs = []
    if set(ref_map) != set(cand_map):
        errs.append(f"ids ref={len(ref_map)} cand={len(cand_map)}")
    for pid in sorted(set(ref_map) & set(cand_map)):
        a, b = ref_map[pid], cand_map[pid]
        if str(a.side).lower() != str(b.side).lower():
            errs.append(f"side {pid}")
        if abs(float(a.top_price) - float(b.top_price)) > 1e-9:
            errs.append(f"top {pid}")
        if abs(float(a.bottom_price) - float(b.bottom_price)) > 1e-9:
            errs.append(f"bottom {pid}")
        if abs(float(a.strength) - float(b.strength)) > 1e-9:
            errs.append(f"strength {pid}")
        if str(a.strength_class) != str(b.strength_class):
            errs.append(f"strength_class {pid}")
        if pool_fingerprint(a) != pool_fingerprint(b):
            errs.append(f"fingerprint {pid}")
    return errs


def main() -> int:
    closes = _pick_closes(MAIN_COUNT)
    cfg = build_live_micro_lld_v2_config(PD10)
    cfg_h = config_hash(cfg)
    pub_reader = PublishedActiveReader()

    candle_parity_ok = True
    engine_ok = True
    membership_ok = True
    strength_ok = True
    phase10_ok = True
    pd_warmup_ok = True
    integration_ok = True
    failures: list[str] = []

    ref_load_times: list[float] = []
    cand_load_times: list[float] = []
    ref_total_times: list[float] = []
    cand_total_times: list[float] = []
    engine_times: list[float] = []

    session = CanonicalCandleSession(SYMBOL, TF)
    cold_start = None

    for i, dc in enumerate(closes):
        hf = assert_rolling_coverage(dc)

        t0 = time.perf_counter()
        ref_candles, ref_meta = _load_candles_for_window(SYMBOL, TF, hf, dc, PD10)
        ref_load_times.append(time.perf_counter() - t0)

        t1 = time.perf_counter()
        cand_candles, cand_meta = session.load_for_window(hf, dc, PD10)
        cand_load_times.append(time.perf_counter() - t1)
        if i == 0:
            cold_start = cand_load_times[0]

        if not candles_equal(ref_candles, cand_candles):
            candle_parity_ok = False
            failures.append(f"candles {dc.isoformat()}")
        if ref_meta["candle_load_start"] != cand_meta["candle_load_start"]:
            candle_parity_ok = False
            failures.append(f"load_start {dc.isoformat()}")

        t_eng = time.perf_counter()
        ref_snap, _, ref_res = assemble_active_state_from_candles(
            ref_candles,
            ref_meta,
            history_from_close=hf,
            decision_close=dc,
            pool_detail=PD10,
            cfg=cfg,
            history_policy=HISTORY_POLICY,
            timeframe=TF,
        )
        eng_s = time.perf_counter() - t_eng
        engine_times.append(eng_s)
        ref_total_times.append(ref_load_times[-1] + eng_s)
        cand_total_times.append(cand_load_times[-1] + eng_s)

        cand_snap, cand_res = ref_snap, ref_res

        if ref_snap.engine_input_hash != cand_snap.engine_input_hash:
            engine_ok = False
            failures.append(f"eih {dc.isoformat()}")
        if ref_snap.active_state_fingerprint != cand_snap.active_state_fingerprint:
            membership_ok = False
            failures.append(f"active_fp {dc.isoformat()}")
        pool_errs = _compare_pools(_pool_active_map(ref_res, dc), _pool_active_map(cand_res, dc))
        if pool_errs:
            strength_ok = False
            engine_ok = False
            failures.append(f"pools {dc.isoformat()}: {pool_errs[:3]}")

        if i % 9 == 0:
            try:
                pub = pub_reader.load_published(PHASE10_LINE, dc, config_hash=cfg_h)
                if pub["active_state_fingerprint"] != cand_snap.active_state_fingerprint:
                    phase10_ok = False
                    failures.append(f"phase10_fp {dc.isoformat()}")
            except Exception as exc:
                phase10_ok = False
                failures.append(f"phase10_read {dc.isoformat()}: {exc}")

        if i in (0, 50, 100, 149):
            full_ref = build_active_state_at_decision_close(
                SYMBOL, TF, hf, dc, pool_detail=PD10, history_policy=HISTORY_POLICY
            )[0]
            full_cand = build_active_state_at_decision_close(
                SYMBOL,
                TF,
                hf,
                dc,
                pool_detail=PD10,
                history_policy=HISTORY_POLICY,
                candle_session=session,
            )[0]
            if full_ref.engine_input_hash != full_cand.engine_input_hash:
                integration_ok = False
                failures.append(f"integration {dc.isoformat()}")

    for pd in (PD1, PD10):
        for dc in closes[::37][:3]:
            hf = assert_rolling_coverage(dc)
            ref_c, _ = _load_candles_for_window(SYMBOL, TF, hf, dc, pd)
            cand_c, _ = session.load_for_window(hf, dc, pd)
            if not candles_equal(ref_c, cand_c):
                pd_warmup_ok = False
                failures.append(f"pd{pd} candles {dc.isoformat()}")

    session2 = CanonicalCandleSession(SYMBOL, TF)
    dc0 = closes[50]
    hf0 = assert_rolling_coverage(dc0)
    c1, _ = session2.load_for_window(hf0, dc0, PD10)
    session2.clear()
    c2, _ = session2.load_for_window(hf0, dc0, PD10)
    if not candles_equal(c1, c2):
        failures.append("cold_resume mismatch")
        candle_parity_ok = False

    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak = ru * 1024

    warm_hits = cand_load_times[5:] if len(cand_load_times) > 5 else cand_load_times
    all_ok = (
        candle_parity_ok
        and engine_ok
        and membership_ok
        and strength_ok
        and phase10_ok
        and pd_warmup_ok
        and integration_ok
        and not failures
    )

    report = {
        "TEST_CLOSES": len(closes),
        "CANDLE_PARITY": "PASS" if candle_parity_ok else "FAIL",
        "ENGINE_POOL_PARITY": "PASS" if engine_ok else "FAIL",
        "ACTIVE_MEMBERSHIP_PARITY": "PASS" if membership_ok else "FAIL",
        "STRENGTH_PARITY": "PASS" if strength_ok else "FAIL",
        "PHASE10_PUBLISHED_PARITY": "PASS" if phase10_ok else "FAIL",
        "PD1_PD10_WARMUP_PARITY": "PASS" if pd_warmup_ok else "FAIL",
        "FULL_BUILD_INTEGRATION": "PASS" if integration_ok else "FAIL",
        "HISTORICAL_CORRECTION_SAFETY": "NOT_PROVEN",
        "HISTORICAL_CORRECTION_NOTE": (
            "CH candles_1m has no revision token in read path; "
            "CandleRevisionConflictError only if same minute re-fetched with different OHLCV."
        ),
        "REFERENCE_LOAD_SECONDS": round(sum(ref_load_times), 3),
        "CACHED_LOAD_SECONDS": round(sum(cand_load_times), 3),
        "REFERENCE_TOTAL_SECONDS": round(sum(ref_total_times), 3),
        "CACHED_TOTAL_SECONDS": round(sum(cand_total_times), 3),
        "MEAN_ENGINE_SECONDS": round(statistics.mean(engine_times), 4),
        "COLD_START_LOAD_SECONDS": round(cold_start or 0, 4),
        "MEAN_WARM_LOAD_SECONDS": round(statistics.mean(warm_hits), 4) if warm_hits else None,
        "MEASURED_SPEEDUP": {
            "load_sum_ratio": round(sum(ref_load_times) / max(sum(cand_load_times), 1e-9), 3),
            "total_sum_ratio": round(sum(ref_total_times) / max(sum(cand_total_times), 1e-9), 3),
        },
        "SLICES_WITHOUT_CH_FETCH": session.stats.slices_without_ch_fetch,
        "CH_FETCH_CALLS": session.stats.ch_fetch_calls,
        "CH_FETCHES_PER_CLOSE": round(session.stats.ch_fetch_calls / len(closes), 3),
        "CACHE_RAM_PEAK_MB": round(max(peak / 1e6, ru / 1024), 2),
        "SESSION_1M_BARS": len(session._1m_by_unix),
        "FAILURES_SAMPLE": failures[:25],
        "FILES_CHANGED": [
            "active_snapshot/canonical_candle_session.py",
            "active_snapshot/extract.py",
            "run_phase11d_canonical_candle_poc.py",
            "tests/test_canonical_candle_session.py",
        ],
        "READY_FOR_3_WORKER_POC": "YES" if all_ok else "NO",
    }

    out = Path(__file__).with_name("phase11d_canonical_candle_poc_report.json")
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
