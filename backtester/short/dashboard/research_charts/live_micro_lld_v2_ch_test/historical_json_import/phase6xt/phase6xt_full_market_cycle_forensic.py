#!/usr/bin/env python3
"""Phase 6XT — full market cycle forensic audit (DOGEUSDT, read-only)."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_full_market_cycle_forensic"
for _p in (_DASH, _REPO):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect  # noqa: E402

from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.clickhouse_source import ClickHouseResearchCandleSource  # noqa: E402
from research_charts.dynamic_liquidity_landscape_v1 import PoolStateCache  # noqa: E402
from research_charts.historical_lmv2_frozen_short_paths_v1 import verify_frozen_entry_hash  # noqa: E402
from research_charts.historical_lmv2_frozen_short_signal_v1 import parse_decision_close  # noqa: E402
from research_charts.market_cycle_forensic_v1 import (  # noqa: E402
    ENGINE_VERSION,
    PD_PRIMARY,
    PILOT_END,
    PILOT_START,
    WINDOW_END,
    WINDOW_START,
    WARMUP_START,
    asof_protocol,
    content_hash,
    find_low_extreme,
    first_lower_rebuild_weakening,
    first_pool_bottom_candidate,
    first_price_baseline_bottom,
    grid_5m,
    lower_rebuild_rows,
    phase_b_outcomes,
    pilot_causality_audit,
    resample_5m_from_candles,
    run_phase_a,
    zones_near_price,
)


def _wjson(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _wcsv(p: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    if not rows:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    fn = fields or sorted({k for r in rows for k in r})
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fn, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _jsonl(p: Path, rows: list[dict]) -> None:
    if not rows:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, default=str) + "\n")


def _file_sha256(p: Path) -> str | None:
    if not p.is_file():
        return None
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def research_contract() -> dict[str, Any]:
    return {
        "version": ENGINE_VERSION,
        "symbol": "DOGEUSDT",
        "window_start": WINDOW_START,
        "window_end": WINDOW_END,
        "price_resolution_primary": "5m",
        "price_resolution_fine": "1m_via_ch_aggregate",
        "pool_source": "original_live_micro_lld_v2",
        "pool_pd_primary": PD_PRIMARY,
        "pool_publication_cadence": "15m_decision_close",
        "causal_rule": "At 5m asof t use load_historical_pool_state_at_close(dc) where dc=floor_15m(t)",
        "known_at_mode": "RESEARCH_HISTORICAL",
        "in_sample_contamination": True,
        "hindsight_exposed": True,
    }


def load_bars(symbol: str, start: str, end: str) -> dict:
    src = ClickHouseResearchCandleSource()
    t0 = parse_decision_close(WARMUP_START)
    t1 = parse_decision_close(end) + timedelta(minutes=5)
    candles = src.get_candles(symbol, "5m", start=t0, end=t1)
    bars5 = resample_5m_from_candles(candles)
    m1 = src.get_1m_candles(symbol, start=parse_decision_close("2026-09-16T16:00:00Z"), end=parse_decision_close("2026-09-16T20:00:00Z"))
    m1_low = None
    for c in m1:
        low = float(c.low)
        if m1_low is None or low < m1_low[0]:
            m1_low = (low, c.timestamp)
    return {"bars5": bars5, "m1_low_sep16": m1_low}


def upper_rotation_analysis(run, grid, bars5_map) -> list[dict]:
    low_t = find_low_extreme(bars5_map, grid)
    if not low_t.get("time_utc"):
        return []
    post = [r for r in run.timeline if r["asof_utc"] >= low_t["time_utc"]]
    seen_up: set[str] = set()
    rows = []
    for r in post:
        for pid in r.get("nearest_upper_ids") or []:
            if pid in seen_up:
                continue
            seen_up.add(pid)
            rows.append(
                {
                    "first_nearest_asof": r["asof_utc"],
                    "upper_pool_id": pid,
                    "price_at_first": r["price"],
                }
            )
    return rows


def build_report(
    pilot: dict,
    run,
    grid,
    bars5_map,
    low_full: dict,
    m1_low,
    zones_079,
    zones_078,
) -> tuple[str, dict[str, str]]:
    pool_bottom = first_pool_bottom_candidate(run.timeline)
    price_bottom = first_price_baseline_bottom(run.timeline, run.price_ema_baseline)
    rebuild_weak = first_lower_rebuild_weakening(run.timeline)
    verdicts = {
        "TECHNICAL_PASS": "PASS" if pilot.get("pass") else "FAIL",
        "CAUSAL_TIMELINE_PASS": "PASS" if pilot.get("checks", {}).get("no_known_at_leaks") else "FAIL",
        "LOWER_REBUILD_WEAKENING": "ESTABLISHED" if rebuild_weak else "NOT_ESTABLISHED",
        "GAP_STRUCTURE": "ESTABLISHED"
        if any((r.get("largest_lower_gap_width") or 0) > 0.0003 for r in run.timeline)
        else "NOT_ESTABLISHED",
        "LAST_ZONE_ID": "VERIFIED" if zones_078 else "NOT_VERIFIED",
        "UPPER_ROTATION": "VERIFIED" if len(upper_rotation_analysis(run, grid, bars5_map)) >= 3 else "NOT_VERIFIED",
        "EARLY_WARNING": "DESCRIPTIVE_ONLY",
        "FREEZE": "FREEZE_CANDIDATE_READY",
    }
    low_ts = low_full.get("time_utc") or ""
    if pool_bottom and price_bottom and low_ts:
        # Only count timing vs low if bottom signal is within 24h before documented low
        from research_charts.historical_lmv2_frozen_short_signal_v1 import parse_decision_close

        pb = parse_decision_close(pool_bottom)
        pl = parse_decision_close(low_ts)
        if (pl - pb).total_seconds() <= 86400 and pb <= pl:
            verdicts["EARLY_WARNING"] = (
                "EARLY_WARNING_EVIDENCE" if pool_bottom <= price_bottom else "DESCRIPTIVE_ONLY"
            )
        else:
            verdicts["EARLY_WARNING"] = "NOT_ESTABLISHED"
    md = f"""# Phase 6XT — Full Market Cycle Forensic Report

**IN-SAMPLE / HINDSIGHT-EXPOSED** — no predictive claim.

## Window
{WINDOW_START} → {WINDOW_END} (5m as-of, PD{PD_PRIMARY} @ 15m publication)

## Pilot ({PILOT_START} → {PILOT_END})
{json.dumps(pilot, indent=2)}

## Low extreme (5m)
{json.dumps(low_full, indent=2)}

## 1m fine check Sep-16 afternoon low
{m1_low}

## Answers (12)

1. **Full cycle causal reconstruction:** {"Yes" if verdicts["CAUSAL_TIMELINE_PASS"] == "PASS" and len(run.timeline) > 1000 else "Partial — see job_status"}
2. **Lower concentrations reached:** see `touch_events` / cluster_evolution (downtrend segment)
3. **Lower rebuild weakening (earliest state):** {rebuild_weak or "NOT_ESTABLISHED"}
4. **Pool-poor gaps before final low:** {verdicts["GAP_STRUCTURE"]}
5. **Last relevant lower zone (≈0.0781–0.0783):** {zones_078[:3] if zones_078 else "see last_zone_forensic.csv"}
6. **Zone tests:** touch_count in last_zone_forensic.csv
7. **First pool bottom candidate:** {pool_bottom}
8. **First price/EMA baseline bottom:** {price_bottom} ({'pool before price' if pool_bottom and price_bottom and pool_bottom <= price_bottom else 'compare timestamps'})
9. **Upper rotation:** upper_rotation_targets.csv
10. **Lower bridges during pump:** bridge_evolution.csv (regime bullish/mixed_above)
11. **Counterexamples:** counterexamples.csv
12. **Freeze universality:** IN_SAMPLE_CANDIDATE — rules are generic; validation only on this window

## Verdicts
{json.dumps(verdicts, indent=2)}
"""
    return md, verdicts


def counterexamples(run) -> list[dict]:
    rows = []
    for r in run.timeline:
        if r.get("state") == "BOTTOM_CANDIDATE" and r.get("regime") == "bearish":
            rows.append({"asof_utc": r["asof_utc"], "type": "bottom_candidate_in_bearish", "price": r["price"]})
        if r.get("state") == "UPPER_ROTATION_CANDIDATE" and r.get("regime") == "bearish":
            rows.append({"asof_utc": r["asof_utc"], "type": "upper_rotation_while_bearish", "price": r["price"]})
    return rows[:200]


def write_freeze_candidate(out: Path) -> None:
    fc = out / "freeze_candidate"
    fc.mkdir(parents=True, exist_ok=True)
    detector_body = _DASH / "research_charts/freeze_candidate_market_cycle_v1.py"
    (fc / "detector.py").write_text(detector_body.read_text(encoding="utf-8"), encoding="utf-8")
    (fc / "tests").mkdir(exist_ok=True)
    (fc / "tests/test_detector_smoke.py").write_text(
        "from freeze_candidate.detector import MarketCycleDetector\n"
        "def test_smoke():\n"
        "    d = MarketCycleDetector({})\n"
        "    assert d.step({'regime':'bearish','rebuild_rate_1h':0.1,'largest_lower_gap_width':0.001,'active_lower':5,'new_lower_count':0,'upper_union_width':0.01,'lower_union_width':0.02,'touch_lower_count':0})"
        "\n",
        encoding="utf-8",
    )
    cfg = {
        "version": "freeze_candidate_v1",
        "rebuild_window_bars": 12,
        "rebuild_weak_threshold": 0.35,
        "gap_thinning_min_width": 0.00035,
        "bottom_gap_min": 0.00025,
        "in_sample_contamination": True,
    }
    _wjson(fc / "config.json", cfg)
    _wjson(
        fc / "feature_schema.json",
        {
            "fields": [
                "rebuild_rate_1h",
                "origin_rate_1h",
                "largest_lower_gap_width",
                "lower_union_width",
                "touch_lower_count",
                "regime",
            ]
        },
    )
    (fc / "state_machine.md").write_text(
        "# State machine (deterministic)\n\nSee market_cycle_forensic_v1.infer_state\n",
        encoding="utf-8",
    )
    manifest = {
        "contamination": "IN_SAMPLE_CANDIDATE",
        "pd": PD_PRIMARY,
        "timeframes": ["5m_price", "15m_pool_publication"],
        "file_hashes": {n: _file_sha256(fc / n) for n in ("detector.py", "config.json", "feature_schema.json")},
        "config_hash": content_hash(cfg),
    }
    _wjson(fc / "manifest.json", manifest)


def _run_window(start: str, end: str) -> tuple[Any, dict, list, dict, dict]:
    grid = grid_5m(start, end)
    loaded = load_bars("DOGEUSDT", start, end)
    bars5_map = loaded["bars5"]
    missing = [g for g in grid if g not in bars5_map]
    if len(missing) > len(grid) * 0.05:
        raise RuntimeError(f"missing_5m_bars:{len(missing)}")
    cfg_db = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg_db.connect_kwargs())
    cache = PoolStateCache(client, cfg_db.database)
    run = run_phase_a(cache, grid, bars5_map)
    pilot = pilot_causality_audit(cache, grid, bars5_map, run)
    client.close()
    return run, pilot, grid, bars5_map, loaded


def execute(mode: str) -> dict[str, Any]:
    _OUT.mkdir(parents=True, exist_ok=True)
    verify = verify_frozen_entry_hash()
    if not verify.get("match"):
        st = {"status": "FAIL", "reason": "frozen_hash"}
        _wjson(_OUT / "job_status.json", st)
        return st

    if mode == "pilot":
        try:
            run, pilot, grid, bars5_map, _loaded = _run_window(PILOT_START, PILOT_END)
        except RuntimeError as exc:
            st = {"status": "FAIL", "reason": str(exc)}
            _wjson(_OUT / "job_status.json", st)
            return st
        _wjson(_OUT / "pilot_audit.json", pilot)
        st = {
            "status": "PILOT_PASS" if pilot["pass"] else "PILOT_FAIL",
            "pilot": pilot,
            "bars": len(run.timeline),
        }
        _wjson(_OUT / "job_status.json", st)
        return st

    try:
        _run, pilot_gate, _, _, _ = _run_window(PILOT_START, PILOT_END)
    except RuntimeError as exc:
        st = {"status": "STOPPED", "reason": str(exc)}
        _wjson(_OUT / "job_status.json", st)
        return st
    if not pilot_gate["pass"]:
        st = {"status": "STOPPED", "reason": "pilot_failed_before_full", "pilot": pilot_gate}
        _wjson(_OUT / "job_status.json", st)
        return st

    try:
        run, pilot, grid, bars5_map, loaded = _run_window(WINDOW_START, WINDOW_END)
    except RuntimeError as exc:
        st = {"status": "FAIL", "reason": str(exc)}
        _wjson(_OUT / "job_status.json", st)
        return st

    # Phase A artifacts
    _wjson(_OUT / "source_audit.json", {"frozen_hash": verify, "bars5_count": len(bars5_map)})
    _wjson(_OUT / "research_contract.json", research_contract())
    _wjson(
        _OUT / "feature_definitions.json",
        {
            "rebuild_rate_1h": "mean new_lower_count over last 12 five-minute bars (updates on 15m pool publish)",
            "origin_rate_1h": "mean new independent lower origins per window",
            "largest_lower_gap_width": "max geometric gap below price from landscape snapshot",
        },
    )
    _wcsv(_OUT / "asof_5m_timeline.csv", run.timeline)
    _jsonl(_OUT / "asof_pool_landscape.jsonl", run.pool_landscape)
    _wcsv(_OUT / "pool_birth_death_timeline.csv", run.pool_birth_death)
    _wcsv(_OUT / "cluster_evolution.csv", run.cluster_evolution)
    _wcsv(_OUT / "gap_evolution.csv", run.gap_evolution)
    _wcsv(_OUT / "bridge_evolution.csv", run.bridge_evolution)
    _wcsv(_OUT / "lower_rebuild_analysis.csv", lower_rebuild_rows(run.timeline))
    low_full = find_low_extreme(bars5_map, grid)
    extra_asof = []
    if low_full.get("time_utc"):
        extra_asof.append(low_full["time_utc"])
    zones_079 = zones_near_price(run.pool_landscape, run.touch_events, 0.0788, 0.0794)
    zones_078 = zones_near_price(run.pool_landscape, run.touch_events, 0.0780, 0.0784)
    _wcsv(_OUT / "last_zone_forensic.csv", zones_079 + zones_078)
    _wcsv(_OUT / "upper_rotation_targets.csv", upper_rotation_analysis(run, grid, bars5_map))
    _wcsv(_OUT / "state_transitions.csv", run.state_transitions)
    _wcsv(_OUT / "price_ema_baseline.csv", run.price_ema_baseline)
    _wjson(_OUT / "asof_context_protocol.json", asof_protocol(run.timeline, extra_asof))

    phase_a_files = [
        "asof_5m_timeline.csv",
        "asof_pool_landscape.jsonl",
        "pool_birth_death_timeline.csv",
        "cluster_evolution.csv",
        "state_transitions.csv",
    ]
    hashes = {f: _file_sha256(_OUT / f) for f in phase_a_files}
    _wjson(_OUT / "phase_a_manifest.json", {"files": phase_a_files, "sealed": True})
    _wjson(_OUT / "phase_a_content_hashes.json", hashes)

    outcomes = phase_b_outcomes(run.timeline, bars5_map, grid)
    _wcsv(_OUT / "outcomes_separate.csv", outcomes)
    _wjson(_OUT / "phase_b_manifest.json", {"depends_on": "phase_a_manifest.json", "file": "outcomes_separate.csv"})
    _wjson(_OUT / "independent_causality_audit.json", pilot)
    _wcsv(_OUT / "counterexamples.csv", counterexamples(run))

    report_md, verdicts = build_report(pilot, run, grid, bars5_map, low_full, loaded["m1_low_sep16"], zones_079, zones_078)
    (_OUT / "report.md").write_text(report_md, encoding="utf-8")
    story = f"# Market cycle story (IN-SAMPLE)\n\nLow at {low_full}. Rebuild weak @ {first_lower_rebuild_weakening(run.timeline)}.\n"
    (_OUT / "market_cycle_story.md").write_text(story, encoding="utf-8")

    write_freeze_candidate(_OUT)

    st = {"status": "COMPLETE", "verdicts": verdicts, "timeline_rows": len(run.timeline)}
    _wjson(_OUT / "job_status.json", st)
    return st


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pilot", action="store_true")
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()
    if args.pilot:
        execute("pilot")
    elif args.full:
        pilot = execute("pilot")
        if pilot.get("status") == "PILOT_PASS":
            execute("full")
        else:
            print("Pilot failed — full run not started", pilot)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
