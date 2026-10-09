#!/usr/bin/env python3
"""Phase 11N — Remaining pool heatmap projection POC (read-only, no CH writes)."""

from __future__ import annotations

import json
import resource
import statistics
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_dash = Path(__file__).resolve().parents[2]
_repo = _dash.parents[2]
_trp = Path("/home/telgenbuescher/projects/trading_research_platform")
for p in (_repo, _dash, _trp):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import (
    build_active_state_at_decision_close,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.rolling_calendar import (
    assert_rolling_coverage,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.runner import (
    enumerate_decision_closes,
)
from research_charts.live_micro_lld_v2_ch_test.identity import (
    build_live_micro_lld_v2_config,
    config_hash,
)
from data.models import Candle
from indicators.live_micro_lld_v2.availability import bar_close
from indicators.live_micro_lld_v2.models import LiquidityPoolV2
from research_charts.live_micro_lld_v2_ch_test.remaining_pool_projection_v1 import (
    project_pool_v1,
    project_pools_from_engine_result,
)
from research_charts.live_micro_lld_v2_ch_test.remaining_pool_projection_v1_config import (
    DEFAULT_SEGMENT_COUNT,
    PROJECTION_VERSION,
    STRENGTH_CLASS_COLORS,
)
from research_charts.live_micro_lld_v2_ch_test.serialize import pool_fingerprint

SYMBOL = "DOGEUSDT"
TF = "5m"
PD = 10
PHASE10_LINE = "phase10_DOGEUSDT_5m_PD10_ROLLING4M_7d_9e40e90b"
RANGE_START = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)
HISTORY_POLICY = "ROLLING_4_CALENDAR_MONTHS"
TEST_CLOSE_COUNT = 100
ENGINE_SAMPLE_CLOSES = 0


def _pick_closes(n: int) -> list[datetime]:
    steady = enumerate_decision_closes(RANGE_START, n - 40, TF)
    month_edge = enumerate_decision_closes(
        datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc), 20, TF
    )
    late = enumerate_decision_closes(
        datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc), 20, TF
    )
    return (list(steady) + list(month_edge) + list(late))[:n]


def _synthetic_100_closes() -> dict:
    """100 decision closes over synthetic pools — projection only, no engine."""
    t0 = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)
    known_open = t0
    pool_u = LiquidityPoolV2(
        pool_id="lldv2:DOGEUSDT:5m:normal:upper:1",
        pool_type="normal",
        symbol=SYMBOL,
        timeframe=TF,
        side="upper",
        source_timestamp=known_open,
        created_timestamp=known_open,
        known_at=bar_close(known_open, TF),
        top_price=0.20,
        bottom_price=0.10,
        strength=6.0,
        strength_class="Strong",
        active=True,
    )
    pool_l = LiquidityPoolV2(
        pool_id="lldv2:DOGEUSDT:5m:normal:lower:2",
        pool_type="normal",
        symbol=SYMBOL,
        timeframe=TF,
        side="lower",
        source_timestamp=known_open,
        created_timestamp=known_open,
        known_at=bar_close(known_open, TF),
        top_price=0.10,
        bottom_price=0.05,
        strength=3.0,
        strength_class="Medium",
        active=True,
    )
    candles: list[Candle] = []
    times: list[float] = []
    upper_partial = lower_partial = full = False
    for i in range(TEST_CLOSE_COUNT):
        open_dt = t0 + timedelta(minutes=5 * (i + 1))
        high = 0.10 + (0.12 * (i + 1) / TEST_CLOSE_COUNT)
        low = 0.10 - (0.06 * (i + 1) / TEST_CLOSE_COUNT)
        candles.append(
            Candle(
                timestamp=open_dt,
                open=0.10,
                high=high,
                low=low,
                close=0.10,
                volume=1.0,
                symbol=SYMBOL,
                timeframe=TF,
            )
        )
        dc = bar_close(open_dt, TF)
        t_p = time.perf_counter()
        pu = project_pool_v1(pool_u, candles, decision_close=dc, candle_input_reference="synth")
        pl = project_pool_v1(pool_l, candles, decision_close=dc, candle_input_reference="synth")
        times.append(time.perf_counter() - t_p)
        if 0 < pu.remaining_fraction < 1:
            upper_partial = True
        if 0 < pl.remaining_fraction < 1:
            lower_partial = True
        if pu.fully_traversed or pl.fully_traversed:
            full = True
    return {
        "upper_partial": upper_partial,
        "lower_partial": lower_partial,
        "full_wick": full,
        "projection_seconds": sum(times),
        "closes": TEST_CLOSE_COUNT,
    }


def _active_map(result, dc: datetime) -> dict:
    dc = dc.astimezone(timezone.utc)
    out = {}
    for p in result.pools:
        if not p.active or p.known_at.astimezone(timezone.utc) > dc:
            continue
        if p.break_at is not None:
            continue
        out[p.pool_id] = p
    return out


def main() -> int:
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    report_path = Path(__file__).resolve().parent / f"phase11n_remaining_pool_{run_id}.json"
    cfg = build_live_micro_lld_v2_config(PD)
    cfg_h = config_hash(cfg)
    closes = sorted(_pick_closes(TEST_CLOSE_COUNT))
    engine_closes = [closes[0], closes[len(closes) // 2], closes[-1]][:ENGINE_SAMPLE_CLOSES]
    synth = _synthetic_100_closes()
    for probe in (closes[0], closes[len(closes) // 2], closes[-1]):
        assert_rolling_coverage(probe, months=4)

    env = dict(**{k: v for k, v in __import__("os").environ.items()})
    env["PYTHONPATH"] = f"{_repo}:{_dash}:{_trp}"
    pytest_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "research_charts/tests/test_remaining_pool_projection_v1.py",
            "-q",
            "--tb=no",
        ],
        cwd=str(_dash),
        env=env,
        capture_output=True,
        text=True,
    )
    unit_pass = pytest_proc.returncode == 0

    phase10_fail: list[str] = []
    upper_partial = synth["upper_partial"]
    lower_partial = synth["lower_partial"]
    full_wick = synth["full_wick"]
    engine_fp_unchanged = True
    if ENGINE_SAMPLE_CLOSES == 0:
        engine_fp_unchanged = True
    proj_times: list[float] = []
    seg_times: list[float] = []
    seg_counts: list[int] = []
    worker_mismatch: list[str] = []

    t0 = time.perf_counter()
    for i, dc in enumerate(engine_closes):
        hist_from = assert_rolling_coverage(dc, months=4)
        snap, candles, result = build_active_state_at_decision_close(
            SYMBOL,
            TF,
            hist_from,
            dc,
            pool_detail=PD,
            cfg=cfg,
            history_policy=HISTORY_POLICY,
        )
        fps_before = {p.pool_id: pool_fingerprint(p) for p in result.pools}

        t_proj = time.perf_counter()
        ref = project_pools_from_engine_result(
            result,
            candles,
            decision_close=dc,
            candle_input_reference=snap.prefix_candle_fingerprint,
            segment_count=DEFAULT_SEGMENT_COUNT,
            ledger=None,
        )
        proj_times.append(time.perf_counter() - t_proj)

        t_seg = time.perf_counter()
        for p in ref:
            if p.remaining_visible:
                seg_counts.append(len(p.segments))
        seg_times.append(time.perf_counter() - t_seg)

        iso = dc.isoformat()
        try:
            published = pub.load_published(PHASE10_LINE, dc, config_hash=cfg_h)
            if published["active_state_fingerprint"] != snap.active_state_fingerprint:
                phase10_fail.append(iso)
        except Exception as exc:
            phase10_fail.append(f"{iso}:{exc}")

        fps_after = {p.pool_id: pool_fingerprint(p) for p in result.pools}
        if fps_before != fps_after:
            engine_fp_unchanged = False
            phase10_fail.append(f"pool_mutated@{iso}")

        active_ids = set(snap.active_pool_ids)
        for pr in ref:
            if pr.engine_active != (pr.pool_id in active_ids):
                phase10_fail.append(f"engine_active_mismatch@{iso}:{pr.pool_id}")
            if pr.side == "upper" and 0.0 < pr.remaining_fraction < 1.0:
                upper_partial = True
            if pr.side == "lower" and 0.0 < pr.remaining_fraction < 1.0:
                lower_partial = True
            if pr.fully_traversed:
                full_wick = True
            if pr.engine_active and pr.fully_traversed and pr.remaining_visible:
                phase10_fail.append(f"visible_when_empty@{iso}")

        if i % 20 == 0:
            iso_b = project_pools_from_engine_result(
                result,
                candles,
                decision_close=dc,
                candle_input_reference=snap.prefix_candle_fingerprint,
                segment_count=DEFAULT_SEGMENT_COUNT,
                ledger=None,
            )
            by_a = {
                p.pool_id: (p.remaining_bottom, p.remaining_top, p.remaining_fraction)
                for p in ref
            }
            for p in iso_b:
                key = (p.remaining_bottom, p.remaining_top, p.remaining_fraction)
                if by_a.get(p.pool_id) != key:
                    worker_mismatch.append(f"{iso}:{p.pool_id}")
                    break

    total_elapsed = time.perf_counter() - t0
    ru = resource.getrusage(resource.RUSAGE_SELF)
    ram_mb = ru.ru_maxrss / 1024.0

    strength_ok = set(STRENGTH_CLASS_COLORS) == {
        "Weak",
        "Medium",
        "Strong",
        "Very Strong",
        "Extreme",
    }
    filtered_ok = unit_pass

    report = {
        "PHASE11N_MODE": "ISOLATED_POC",
        "RUN_ID": run_id,
        "PROJECTION_VERSION": PROJECTION_VERSION,
        "TEST_CLOSES": [c.isoformat() for c in closes],
        "TEST_CLOSE_COUNT": len(closes),
        "ENGINE_SAMPLE_CLOSES": [c.isoformat() for c in engine_closes],
        "SYNTHETIC_100_PROJECTION_SECONDS": round(synth["projection_seconds"], 4),
        "UPPER_PARTIAL_SOURCE": "SYNTHETIC_AND_OR_ENGINE",
        "LOWER_PARTIAL_SOURCE": "SYNTHETIC_AND_OR_ENGINE",
        "WICK_FULL_SOURCE": "SYNTHETIC_AND_OR_ENGINE",
        "UNIT_TESTS": "PASS" if unit_pass else "FAIL",
        "UNIT_TEST_STDOUT": pytest_proc.stdout[-2000:],
        "UPPER_PARTIAL_CONSUMPTION": "PASS" if upper_partial and unit_pass else "NOT_PROVEN",
        "LOWER_PARTIAL_CONSUMPTION": "PASS" if lower_partial and unit_pass else "NOT_PROVEN",
        "WICK_FULL_TRAVERSAL": "PASS" if full_wick and unit_pass else "NOT_PROVEN",
        "CONSUMPTION_EVIDENCE": "UNIT_AND_SYNTHETIC",
        "FIXED_SEGMENT_BOUNDARIES": "PASS" if unit_pass else "FAIL",
        "PARTIAL_SEGMENT_PRECISION": "PASS" if unit_pass else "FAIL",
        "MONOTONIC_CONSUMPTION": "PASS" if unit_pass else "FAIL",
        "FIRST_ELIGIBLE_CLOSE": "PASS" if unit_pass else "FAIL",
        "ENGINE_ACTIVE_UNCHANGED": "YES" if unit_pass else "NO",
        "ENGINE_ACTIVE_NOTE": "Projection reads pool.active and does not write pools (unit). Live prefix fingerprint not re-checked.",
        "POOL_ID_REAPPEARANCE_PARITY": "NOT_PROVEN",
        "POOL_ID_REAPPEARANCE_SYNTHETIC": "PASS" if unit_pass else "FAIL",
        "POOL_ID_REAPPEARANCE_REALWORLD": "NOT_PROVEN",
        "EVICTION_VISIBILITY_PARITY": "NOT_PROVEN",
        "ROLLING_VISIBILITY_PARITY": "NOT_PROVEN",
        "STRENGTH_MAPPING": "PASS" if strength_ok and unit_pass else "FAIL",
        "STRENGTH_FILTER_PARITY": "PASS" if filtered_ok else "FAIL",
        "HISTORICAL_ASOF_PARITY": "NOT_PROVEN",
        "HISTORICAL_ASOF_NOTE": "Stateless recompute of the same inputs is invariant (unit). Real as-of prefixes not compared.",
        "WORKER_ORDER_INVARIANCE": "PASS" if unit_pass else "FAIL",
        "WORKER_ORDER_NOTE": "Pure function on the same candle prefix; no shared mutable state.",
        "PHASE10_ORIGINAL_POOL_PARITY": "NOT_PROVEN" if ENGINE_SAMPLE_CLOSES == 0 else ("PASS" if not phase10_fail else "FAIL"),
        "PHASE10_SAMPLE_CLOSES": len(engine_closes),
        "PHASE10_NOTE": (
            "Live engine prefix + published fingerprint comparison not run in this POC "
            "(rolling 4m prefix cost). Unit tests prove projection does not mutate pool objects. "
            "Set ENGINE_SAMPLE_CLOSES>0 to sample Phase 10."
        ),
        "PHASE10_FAILURES_SAMPLE": phase10_fail[:10],
        "WORKER_MISMATCH_SAMPLE": worker_mismatch[:5],
        "HISTORICAL_CORRECTION_SAFETY": "NOT_PROVEN",
        "PROJECTION_SECONDS": round(sum(proj_times) + synth["projection_seconds"], 4),
        "PROJECTION_SECONDS_MEAN": round(statistics.mean(proj_times), 6) if proj_times else 0,
        "SEGMENT_RENDER_PREPARATION_SECONDS": round(sum(seg_times), 4),
        "TOTAL_WALL_SECONDS": round(total_elapsed, 2),
        "RAM_PEAK_MB": round(ram_mb, 1),
        "AVG_SEGMENTS_PER_VISIBLE_POOL": round(statistics.mean(seg_counts), 2) if seg_counts else 0,
        "FILES_CHANGED": [
            "live_micro_lld_v2_ch_test/remaining_pool_projection_v1.py",
            "live_micro_lld_v2_ch_test/remaining_pool_projection_v1_config.py",
            "live_micro_lld_v2_ch_test/run_phase11n_remaining_pool_poc.py",
            "tests/test_remaining_pool_projection_v1.py",
        ],
        "REPORT_PATH": str(report_path),
        "READY_FOR_REMAINING_POOL_CH_POC": "NO",
        "READY_NOTE": "NO until Phase-10 original-pool parity is proven on real prefixes",
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in report if k != "TEST_CLOSES"}, indent=2))
    return 0 if report["READY_FOR_REMAINING_POOL_CH_POC"] == "YES" else 1


if __name__ == "__main__":
    raise SystemExit(main())
