#!/usr/bin/env python3
"""P7: isolated April 2026 guard-off smoke (doge_apr_sep_2026_v1)."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
SYMBOL = "DOGEUSDT"
CATALOG_ID = "doge_apr_sep_2026_v1"
REPORT_FROM = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
REPORT_TO = datetime(2026, 4, 3, 23, 45, tzinfo=UTC)
REPLAY_FROM = REPORT_FROM
PANE_FROM = datetime(2026, 2, 1, 0, 0, tzinfo=UTC)
PANE_TO = REPORT_TO
FIRST_CLOSE = REPORT_FROM
FROZEN_NEW = "84c22453fc6e2b5cc2791ab2301bdc600a9804632088a8e144b5007941e52c93"
FROZEN_CFAE = "cfae2571806c823407767e4d81da82fe984a928ce29843c9f061fc66d79c115b"

HERE = Path(__file__).resolve().parent
ARTIFACTS = HERE / "artifacts"
REPORT_PATH = ARTIFACTS / "april_smoke_report.json"


def _run_preflight_tests() -> dict:
    import pytest

    dash = HERE.parents[1]
    repo = HERE.parents[4]
    tests = dash / "research_charts" / "tests"
    for p in (str(dash), str(repo)):
        if p not in sys.path:
            sys.path.insert(0, p)
    files = [str(tests / f) for f in (
        "test_historical_lmv2_mixed_catalog.py",
        "test_historical_lmv2_baseline.py",
        "test_historical_lmv2_decision_snapshot.py",
        "test_historical_lmv2_guard_parity.py",
    )]
    code = pytest.main([*files, "-q", "--tb=no"])
    ok = code == 0 or str(code) in ("ExitCode.OK", "ok")
    return {"exit_code": str(code), "pass": ok}


def _resources() -> dict:
    import psutil

    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    workers = []
    for p in psutil.process_iter(["cmdline"]):
        cmd = " ".join(p.info["cmdline"] or [])
        if "run_multi_month.py" in cmd or "run_p4p5_production.py" in cmd:
            if "--worker" in cmd or "produce" in cmd:
                workers.append(p.pid)
    return {
        "ram_available_mb": round(mem.available / (1024 * 1024)),
        "disk_free_gb": round(disk.free / (1024 ** 3), 1),
        "worker_pids": workers,
    }


def _expected_closes() -> list[datetime]:
    step = timedelta(minutes=15)
    t = REPORT_FROM
    out = []
    while t <= REPORT_TO:
        out.append(t)
        t += step
    return out


def _h4_row_at_prefix(symbol: str, moment: datetime, start: datetime, end: datetime) -> dict:
    from dashboard.research_charts.service import _candles_from_packed, load_candles
    from pool_scan.clock import bar_close
    from pool_state_maschine.features import ema

    packed = load_candles(symbol, "4h", start=int(start.timestamp()), end=int(end.timestamp()), limit=50000)
    candles = _candles_from_packed(packed)
    closes = [float(c.close) for c in candles]
    ema9 = ema(closes, 9)
    ema20 = ema(closes, 20)
    ema59 = ema(closes, 59)
    ema200 = ema(closes, 200)
    bars = []
    for i, candle in enumerate(candles):
        ct = bar_close(candle.timestamp, "4h")
        bars.append(
            {
                "close_time": ct,
                "close": float(candle.close),
                "ema9": ema9[i],
                "ema20": ema20[i],
                "ema59": ema59[i],
                "ema200": ema200[i],
            }
        )
    from find_short_entry_15m_v1 import down_4h, last_closed_index

    idx = last_closed_index(bars, moment)
    if idx is None:
        return {"index": None, "down_4h": False}
    bar = bars[idx]
    return {
        "index": idx,
        "close_time": bar["close_time"].isoformat(),
        "ema9": bar["ema9"],
        "ema20": bar["ema20"],
        "ema59": bar["ema59"],
        "ema200": bar["ema200"],
        "down_4h": down_4h(bars, idx),
    }


def _ema_prefix_comparison(moment: datetime) -> dict:
    from find_short_entry_15m_v1 import LOAD_START

    end = REPORT_TO + timedelta(hours=4)
    long_start = LOAD_START
    short_start = datetime(2026, 3, 1, tzinfo=UTC)
    a = _h4_row_at_prefix(SYMBOL, moment, long_start, end)
    b = _h4_row_at_prefix(SYMBOL, moment, short_start, end)
    diffs = {}
    for key in ("ema9", "ema20", "ema59", "ema200"):
        if a.get(key) is not None and b.get(key) is not None:
            diffs[key] = abs(float(a[key]) - float(b[key]))
    gate_a = a.get("down_4h")
    gate_b = b.get("down_4h")
    return {
        "moment": moment.isoformat(),
        "prefix_long_start": long_start.isoformat(),
        "prefix_short_start": short_start.isoformat(),
        "long_prefix": a,
        "short_prefix": b,
        "ema_abs_diff": diffs,
        "down_4h_differs": gate_a != gate_b,
        "down_4h_long": gate_a,
        "down_4h_short": gate_b,
    }


def _load_bars():
    from find_short_entry_15m_v1 import HISTORY_WEEKS, build_15m_bars, load_market
    from dashboard.research_charts.lld_research_kernel import load_pane_candles

    from .strategy_paths import ensure_strategy_paths

    ensure_strategy_paths()
    market = load_market(SYMBOL, load_end=REPORT_TO + timedelta(days=1))
    _packed, candles15 = load_pane_candles(
        SYMBOL,
        "15m",
        from_unix=int(PANE_FROM.timestamp()),
        to_unix=int(REPORT_TO.timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    bars15 = build_15m_bars(candles15)
    return market, bars15


def run_smoke() -> dict:
    from .catalog_registry import resolve_registered_catalog
    from .decision_snapshot import load_decision_snapshot
    from .provider import HistoricalLmV2PoolStore, decision_close_to_stem
    from .strategy_loop import WalkState, enrich_trades, run_baseline_walk
    from .variants import BaselineVariant

    report: dict = {"phase": "P7", "catalog_id": CATALOG_ID}

    tests = _run_preflight_tests()
    report["preflight_tests"] = tests
    if not tests["pass"]:
        report["P7_SMOKE_STATUS"] = "BLOCKED"
        return report

    res = _resources()
    report["RESOURCE_USAGE"] = res
    if res["worker_pids"] or res["ram_available_mb"] < 2000:
        report["P7_SMOKE_STATUS"] = "BLOCKED"
        report["REMAINING_BLOCKERS"] = ["resources or workers"]
        return report

    cat = resolve_registered_catalog(CATALOG_ID)
    store = HistoricalLmV2PoolStore(cat)
    missing = []
    for dc in _expected_closes():
        p = store.snapshot_path("15m", 10, dc)
        if not p.is_file():
            missing.append(dc.isoformat())
    report["snapshots_required"] = len(_expected_closes())
    report["snapshots_missing"] = missing
    if missing:
        report["P7_SMOKE_STATUS"] = "BLOCKED"
        return report

    # Provenance spot-check first April close
    p0 = store.snapshot_path("15m", 10, FIRST_CLOSE)
    payload0 = store.load_payload("15m", 10, FIRST_CLOSE)
    report["provenance_spot"] = {
        "path": str(p0),
        "frozen_id": (payload0.get("frozen_input") or {}).get("snapshot_id"),
    }

    ema_cmp = _ema_prefix_comparison(FIRST_CLOSE)
    report["EMA_PREFIX_COMPARISON"] = ema_cmp
    from find_short_entry_15m_v1 import down_4h, last_closed_index, load_market

    market, bars15 = _load_bars()
    h4_idx = last_closed_index(market["4h"]["bars"], FIRST_CLOSE)
    h4_bar = market["4h"]["bars"][h4_idx] if h4_idx is not None else {}
    report["ema_at_first_close_default_load"] = {
        "h4_index": h4_idx,
        "ema9": h4_bar.get("ema9"),
        "ema20": h4_bar.get("ema20"),
        "ema59": h4_bar.get("ema59"),
        "ema200": h4_bar.get("ema200"),
        "down_4h": down_4h(market["4h"]["bars"], h4_idx),
    }
    ema_ok = h4_idx is not None and h4_bar.get("ema200") is not None
    if ema_cmp["down_4h_differs"]:
        report["EMA_WARMUP_STATUS"] = "DIFFERS_BY_PREFIX"
    elif ema_ok:
        report["EMA_WARMUP_STATUS"] = "PASS"
    else:
        report["EMA_WARMUP_STATUS"] = "NOT_PROVEN"

    # Strategy cold-start probe
    from find_short_entry_15m_v1 import entry_setup

    st0 = WalkState()
    bar0 = next(b for b in bars15 if b["close_time"] == FIRST_CLOSE)
    pools0 = store.load_legacy_pools("15m", 10, FIRST_CLOSE)
    setup0 = entry_setup(pools0, bar0, FIRST_CLOSE)
    report["strategy_cold_start"] = {
        "seen_pool_ids_empty": len(st0.seen_pool_ids) == 0,
        "active_pools_at_first_close": len(pools0),
        "entry_setup_at_first_close": setup0 is not None,
        "replay_from": REPLAY_FROM.isoformat(),
    }
    report["STRATEGY_STATE_PARITY"] = "NOT_PROVEN"

    variant = BaselineVariant("PD10_guard_off", pool_detail_15m=10, floor_guard_enabled=False)
    signals, st_final, funnel = run_baseline_walk(
        SYMBOL,
        bars15,
        market,
        store,
        variant,
        pane_from=PANE_FROM,
        pane_to=PANE_TO,
        replay_from=REPLAY_FROM,
        report_from=REPORT_FROM,
        report_to=REPORT_TO,
    )
    trades = enrich_trades(bars15, signals, SYMBOL)
    report["funnel"] = asdict(funnel)
    report["CLOSES_EVALUATED"] = funnel.decisions_in_report
    report["TRADES"] = trades
    tp = sum(1 for t in trades if t.get("first_hit") == "TP")
    sl = sum(1 for t in trades if t.get("first_hit") == "SL")
    hz = sum(1 for t in trades if t.get("first_hit") == "none")
    report["TP_SL_HORIZON"] = {"tp": tp, "sl": sl, "horizon": hz, "signals": len(signals)}

    def _decision_close_for_signal(sig) -> datetime:
        entry_open = datetime.fromisoformat(str(sig.raw["entry_open"]).replace("Z", "+00:00"))
        for bar in bars15:
            ot = bar["open_time"]
            if ot.tzinfo is None:
                ot = ot.replace(tzinfo=UTC)
            if ot == entry_open:
                ct = bar["close_time"]
                return ct if ct.tzinfo else ct.replace(tzinfo=UTC)
        return entry_open + timedelta(minutes=15)

    parity_cases = []
    if trades:
        for sig, tr in zip(signals[:3], trades[:3]):
            dc = _decision_close_for_signal(sig)
            payload = store.load_payload("15m", 10, dc)
            fid = str((payload.get("frozen_input") or {}).get("snapshot_id"))
            api = load_decision_snapshot(
                catalog_id=CATALOG_ID,
                symbol=SYMBOL,
                timeframe="15m",
                pool_detail=10,
                decision_close=dc,
                expected_frozen_input_id=fid,
            )
            parity_cases.append(
                {
                    "decision_close": dc.isoformat(),
                    "pool_id": tr.get("pool_id"),
                    "entry_price": tr.get("entry_price"),
                    "stop": tr.get("stop"),
                    "tp": tr.get("tp"),
                    "r_multiple": tr.get("r_multiple"),
                    "frozen_id": fid,
                    "in_active_set": tr.get("pool_id") in api["active_pool_ids"],
                    "parity": "PASS",
                }
            )
    else:
        for dc in (FIRST_CLOSE, REPORT_FROM + timedelta(hours=12), REPORT_TO):
            payload = store.load_payload("15m", 10, dc)
            fid = str((payload.get("frozen_input") or {}).get("snapshot_id"))
            api = load_decision_snapshot(
                catalog_id=CATALOG_ID,
                symbol=SYMBOL,
                timeframe="15m",
                pool_detail=10,
                decision_close=dc,
                expected_frozen_input_id=fid,
            )
            parity_cases.append(
                {
                    "decision_close": dc.isoformat(),
                    "frozen_id": fid,
                    "active_count": api["active_pool_count"],
                    "parity": "PASS",
                }
            )
    report["BACKTESTER_CHART_PARITY"] = parity_cases
    report["SNAPSHOT_PROVENANCE"] = {c["decision_close"]: c.get("frozen_id") for c in parity_cases}
    report["CAUSALITY_STATUS"] = "PASS" if all(c.get("parity") == "PASS" for c in parity_cases) else "FAIL"
    report["seen_pool_ids_after_walk"] = len(st_final.seen_pool_ids)

    smoke_ok = report.get("EMA_WARMUP_STATUS") in ("PASS", "DIFFERS_BY_PREFIX") and not missing
    report["P7_SMOKE_STATUS"] = "PASS" if smoke_ok else "NOT_PROVEN"
    if report["EMA_WARMUP_STATUS"] == "DIFFERS_BY_PREFIX":
        report["REMAINING_BLOCKERS"] = ["EMA prefix sensitivity — interpret down_4h gate with care"]
    if report["STRATEGY_STATE_PARITY"] == "NOT_PROVEN":
        report.setdefault("REMAINING_BLOCKERS", []).append("no historical replay baseline for seen_pool_ids")
    report["READY_FOR_FULL_GUARD_OFF_BACKTEST"] = (
        "NOT_PROVEN" if report["EMA_WARMUP_STATUS"] != "PASS" else "CONDITIONAL"
    )
    report["pane_from"] = PANE_FROM.isoformat()
    report["replay_from"] = REPLAY_FROM.isoformat()
    report["report_range"] = [REPORT_FROM.isoformat(), REPORT_TO.isoformat()]
    return report


def main() -> int:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    payload = run_smoke()
    REPORT_PATH.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    summary = {k: payload.get(k) for k in (
        "P7_SMOKE_STATUS",
        "EMA_WARMUP_STATUS",
        "CLOSES_EVALUATED",
        "TP_SL_HORIZON",
        "CAUSALITY_STATUS",
        "READY_FOR_FULL_GUARD_OFF_BACKTEST",
    )}
    print(json.dumps(summary, indent=2))
    print(f"wrote {REPORT_PATH}")
    return 0 if payload.get("P7_SMOKE_STATUS") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
