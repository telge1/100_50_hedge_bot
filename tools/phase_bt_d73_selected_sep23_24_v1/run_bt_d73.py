#!/usr/bin/env python3
"""BT-D73 — selected Sep-23/24 cache build + EMA V1/V3 chronological replay."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
OUT = ROOT / "results/phase_bt_d73_selected_sep23_24_v1"
REPORT = ROOT / "reports/PHASE_BT_D73_SELECTED_SEP23_24_CACHE_REPLAY_V1.md"
ISOLATED = ROOT / "tools/phase_downtrend_isolated_upper_short_v1"
D67 = ROOT / "tools/phase_bt_d67_canonical_ema_replay_v1"
D71 = ROOT / "tools/phase_bt_d71_ema_comparison_v1"
D60_MANIFEST = ROOT / "results/phase_bt_d60_ema_history_v1/ema_history_manifest.json"

for p in (
    PKG,
    ISOLATED,
    D67,
    D71,
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d60_ema_history_validation_v1",
    ROOT / "tools/phase_bt_d4_enforced_depth2_backtester_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from cache_build_resumable_v1 import build_or_resume_bundle, discover_donor_bundles  # noqa: E402
from config_loader_v1 import load_isolated_strategy_bundle  # noqa: E402
from export_replay_v1 import write_all_exports  # noqa: E402
from isolated_chronological_runner_v1 import run_isolated_chronological_replay  # noqa: E402
from ohlc_loader_v1 import _iso, _parse_utc, load_5m_ohlc_ch  # noqa: E402
from pilot_density_session_v1 import build_pilot_density_session, preflight_coverage  # noqa: E402
from pure_ema_confirmation_v1 import build_pure_ema_gate  # noqa: E402
from pure_ema_series_provider_v1 import PureEmaSeriesProviderV1  # noqa: E402
from research_backtester_v1 import ResearchDensityRunV1  # noqa: E402
from short_pause_state_v1 import ShortPauseControllerV1  # noqa: E402
from validation_config_v1 import REFERENCE_WARMUP_HOURS  # noqa: E402
from selected_window_v1 import (  # noqa: E402
    EXPECTED_CLOSE_COUNT,
    WINDOW_END_UTC,
    WINDOW_START_UTC,
    iter_window_closes_inclusive,
)


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def _disk_free_gib(path: Path) -> float:
    try:
        usage = shutil.disk_usage(path)
        return round(usage.free / (1024**3), 2)
    except Exception:
        return -1.0


def _replay(
    *,
    config: dict,
    isolated: dict,
    bar_keys: list[str],
    prices: dict,
    bars: list[str],
    classifier_version: str,
    pause_ready: bool,
    session: Any,
) -> dict[str, Any]:
    iso = dict(isolated)
    iso["pure_ema_confirmation"] = {
        **(isolated.get("pure_ema_confirmation") or {}),
        "enabled": True,
        "classifier_version": classifier_version,
    }
    gate = build_pure_ema_gate(bar_keys=bar_keys, prices=prices, isolated_strategy_config=iso)
    pause = ShortPauseControllerV1()
    if pause_ready:
        pause.set_ready_for_research_only()
    return run_isolated_chronological_replay(
        config=config,
        isolated_bundle=iso,
        prices=prices,
        bars=bars,
        density_session=session,
        pause=pause,
        ema_gate=gate,
    )


def _first_sl_bar(replay: dict[str, Any]) -> str | None:
    for ev in replay.get("modeled_positions") or []:
        if ev.get("event") == "SIMULATED_SL":
            return str(ev.get("decision_close_utc"))
    return None


def run_preflight(closes: list[str]) -> dict[str, Any]:
    pf: dict[str, Any] = {
        "git_head": _git_head(),
        "window_start": WINDOW_START_UTC,
        "window_end": WINDOW_END_UTC,
        "close_count": len(closes),
        "expected_close_count": EXPECTED_CLOSE_COUNT,
        "blockers": [],
    }
    if len(closes) != EXPECTED_CLOSE_COUNT:
        pf["blockers"].append("CLOSE_COUNT_MISMATCH")

    pf["disk_free_gib"] = _disk_free_gib(ROOT)
    if pf["disk_free_gib"] >= 0 and pf["disk_free_gib"] < 2.0:
        pf["blockers"].append("DISK_LOW")

    rcfg = ResearchDensityRunV1.from_config_path().config
    pf["density_warmup_hours"] = rcfg.warmup_hours
    if rcfg.warmup_hours < 168:
        pf["blockers"].append("WARMUP_BELOW_168H")

    if not D60_MANIFEST.is_file():
        pf["blockers"].append("BT_D60_MANIFEST_MISSING")
    else:
        d60 = json.loads(D60_MANIFEST.read_text())
        pf["d60_verdict"] = d60.get("verdict")
        if d60.get("verdict") != "EMA_HISTORY_VALIDATED":
            pf["blockers"].append("BT_D60_NOT_VALIDATED")

    donors = discover_donor_bundles()
    pf["donor_bundle_count"] = len(donors)
    donor_hits = 0
    for d in donors:
        try:
            man = json.loads((d / "bundle_manifest.json").read_text())
            have = {e["decision_close_utc"] for e in man.get("entries") or []}
            donor_hits += sum(1 for c in closes if c in have)
        except Exception:
            continue
    pf["donor_hits_in_window"] = donor_hits
    pf["donor_misses_in_window"] = len(closes) - donor_hits

    load_start = _parse_utc(WINDOW_START_UTC) - timedelta(hours=REFERENCE_WARMUP_HOURS + 48)
    load_end = _parse_utc(WINDOW_END_UTC)
    t0 = time.perf_counter()
    bar_keys, prices, ohlc_meta = load_5m_ohlc_ch(_iso(load_start), _iso(load_end))
    pf["ohlc_load_s"] = round(time.perf_counter() - t0, 4)
    pf["ohlc_meta"] = ohlc_meta
    missing = [c for c in closes if c not in prices]
    pf["ohlc_missing_for_window"] = len(missing)
    if missing:
        pf["blockers"].append("OHLC_INCOMPLETE")

    sample = [closes[0], closes[len(closes) // 2], closes[-1]]
    t1 = time.perf_counter()
    run = ResearchDensityRunV1.from_config_path()
    for s in sample[:1]:
        run.collect_at_close(s)
    pf["bounded_sample_density_s"] = round(time.perf_counter() - t1, 4)

    bundle = load_isolated_strategy_bundle()
    pilot = {
        "pilot_id": "d73_preflight",
        "start_utc": WINDOW_START_UTC,
        "end_utc": WINDOW_END_UTC,
        "density_mode": "validated_cache",
        "cache_bundle": str((OUT / "cache_bundle").relative_to(ROOT)),
    }
    session = build_pilot_density_session(bundle["baseline_config"], pilot)
    cov = preflight_coverage(session, sample)
    pf["preflight_density_sample"] = cov
    if any(r["coverage_status"] != "OK" for r in cov) and (OUT / "cache_bundle" / "bundle_manifest.json").is_file():
        pf["blockers"].append("DENSITY_SAMPLE_INCOMPLETE")

    pf["verdict"] = "PREFLIGHT_BLOCKED" if pf["blockers"] else "PREFLIGHT_PASS"
    pf["bar_keys"] = bar_keys
    pf["prices"] = prices
    return pf


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    closes = iter_window_closes_inclusive()
    pf = run_preflight(closes)
    coverage_manifest = {
        "phase": "BT-D73",
        "git_head": pf["git_head"],
        "preflight": {k: v for k, v in pf.items() if k not in ("bar_keys", "prices")},
    }
    (OUT / "coverage_manifest.json").write_text(json.dumps(coverage_manifest, indent=2), encoding="utf-8")

    if pf["blockers"]:
        REPORT.write_text(
            f"# BT-D73 — Selected Sep-23/24 (BLOCKED at preflight)\n\n"
            f"**VERDICT:** `SELECTED_WINDOW_REPLAY_BLOCKED`\n\n"
            f"Blockers: {pf['blockers']}\n",
            encoding="utf-8",
        )
        print("PREFLIGHT BLOCKED", pf["blockers"], flush=True)
        return 2

    bundle_dir = OUT / "cache_bundle"
    progress_path = OUT / "cache_progress.json"
    t_build = time.perf_counter()

    def _cb(i: int, n: int, close: str, kind: str) -> None:
        if i % 20 == 0 or i == n:
            print(f"cache {i}/{n} {close} {kind}", flush=True)

    build_result = build_or_resume_bundle(
        bundle_dir=bundle_dir,
        decision_closes=closes,
        progress_path=progress_path,
        progress_cb=_cb,
    )
    build_result["wall_s"] = round(time.perf_counter() - t_build, 4)
    (OUT / "cache_build_result.json").write_text(json.dumps(build_result, indent=2), encoding="utf-8")

    if not build_result.get("complete"):
        REPORT.write_text(
            f"# BT-D73 — Selected Sep-23/24 (BLOCKED at cache)\n\n"
            f"**VERDICT:** `SELECTED_WINDOW_REPLAY_BLOCKED`\n\n"
            f"Missing: {build_result.get('missing_count')}\n",
            encoding="utf-8",
        )
        return 3

    bundle = load_isolated_strategy_bundle()
    isolated_base = dict(bundle["isolated_strategy_v1"])
    isolated_base["pure_ema_confirmation"] = {**(isolated_base.get("pure_ema_confirmation") or {}), "enabled": True}
    isolated_base["isolated_upper_eligibility"] = {
        **(isolated_base.get("isolated_upper_eligibility") or {}),
        "research_pass_on_baseline_signal": True,
    }
    config = bundle["baseline_config"]
    bar_keys = pf["bar_keys"]
    prices = pf["prices"]

    pilot = {
        "pilot_id": "selected_sep23_24",
        "start_utc": WINDOW_START_UTC,
        "end_utc": WINDOW_END_UTC,
        "density_mode": "validated_cache",
        "cache_bundle": str(bundle_dir.relative_to(ROOT)),
    }
    session = build_pilot_density_session(config, pilot)
    cov_all = preflight_coverage(session, closes)
    if any(r["coverage_status"] != "OK" for r in cov_all):
        REPORT.write_text(
            "# BT-D73 — BLOCKED (incomplete cache at replay)\n\n**VERDICT:** `SELECTED_WINDOW_REPLAY_BLOCKED`\n",
            encoding="utf-8",
        )
        return 4

    provider_v1 = PureEmaSeriesProviderV1(bar_keys=bar_keys, prices=prices, classifier_version="v1")
    provider_v3 = PureEmaSeriesProviderV1(bar_keys=bar_keys, prices=prices, classifier_version="v3")
    ema_v1 = {b: provider_v1.confirm_at_close(b) for b in closes}
    ema_v3 = {b: provider_v3.confirm_at_close(b) for b in closes}

    rv1 = _replay(
        config=config,
        isolated=isolated_base,
        bar_keys=bar_keys,
        prices=prices,
        bars=closes,
        classifier_version="v1",
        pause_ready=True,
        session=session,
    )
    rv3 = _replay(
        config=config,
        isolated=isolated_base,
        bar_keys=bar_keys,
        prices=prices,
        bars=closes,
        classifier_version="v3",
        pause_ready=True,
        session=session,
    )
    rv1_paused = _replay(
        config=config,
        isolated=isolated_base,
        bar_keys=bar_keys,
        prices=prices,
        bars=closes,
        classifier_version="v1",
        pause_ready=False,
        session=session,
    )

    write_all_exports(
        OUT,
        bars=closes,
        replay_v1=rv1,
        replay_v3=rv3,
        replay_v1_paused=rv1_paused,
        ema_v1=ema_v1,
        ema_v3=ema_v3,
    )

    sl_v1 = _first_sl_bar(rv1)
    sl_v3 = _first_sl_bar(rv3)
    pool_n = len(rv1.get("baseline_pool_candidates") or [])
    v1_allow = sum(1 for r in rv1.get("authorized_candidates") or [])
    v3_allow = sum(1 for r in rv3.get("authorized_candidates") or [])
    v1_block = sum(1 for r in rv1.get("blocked_candidates") or [])
    v3_block = sum(1 for r in rv3.get("blocked_candidates") or [])

    summary = {
        "phase": "BT-D73",
        "git_head": pf["git_head"],
        "verdict": "SELECTED_WINDOW_REPLAY_PASS",
        "window": {"start": WINDOW_START_UTC, "end": WINDOW_END_UTC, "closes": len(closes)},
        "cache": build_result,
        "cache_hits_donor": build_result.get("reused_entries", 0),
        "cache_cold_computed": build_result.get("cold_computed_entries", 0),
        "validated_snapshots": build_result.get("entry_count"),
        "pool_candidates": pool_n,
        "v1_authorized": v1_allow,
        "v3_authorized": v3_allow,
        "v1_blocked_at_candidate": v1_block,
        "v3_blocked_at_candidate": v3_block,
        "v1_pending_created": rv1.get("pending_orders_created"),
        "v3_pending_created": rv3.get("pending_orders_created"),
        "v1_modeled_fills": rv1.get("modeled_fills"),
        "v3_modeled_fills": rv3.get("modeled_fills"),
        "first_sl_v1": sl_v1,
        "first_sl_v3": sl_v3,
        "default_paused_v1_pending_created": rv1_paused.get("pending_orders_created"),
        "pause_state_final_research_v1": rv1.get("pause_state_final"),
        "pause_state_final_research_v3": rv3.get("pause_state_final"),
        "coverage_gaps_v1": len(rv1.get("coverage_incomplete") or []),
        "coverage_gaps_v3": len(rv3.get("coverage_incomplete") or []),
    }
    (OUT / "replay_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    verdict = "SELECTED_WINDOW_REPLAY_PASS"
    if summary["coverage_gaps_v1"] or summary["coverage_gaps_v3"]:
        verdict = "SELECTED_WINDOW_REPLAY_PARTIAL"
    if build_result.get("entry_count") != EXPECTED_CLOSE_COUNT:
        verdict = "SELECTED_WINDOW_REPLAY_BLOCKED"

    REPORT.write_text(
        _render_report(pf=pf, summary=summary, build_result=build_result, verdict=verdict),
        encoding="utf-8",
    )
    print(f"VERDICT {verdict}", flush=True)
    return 0 if verdict == "SELECTED_WINDOW_REPLAY_PASS" else 1


def _render_report(*, pf: dict, summary: dict, build_result: dict, verdict: str) -> str:
    return f"""# BT-D73 — Selected Sep-23/24 Canonical Cache + EMA V1/V3 Replay

**VERDICT:** `{verdict}`

## Window (UTC, inclusive 5m decision closes)

- Start: `{WINDOW_START_UTC}`
- End: `{WINDOW_END_UTC}`
- Closes: **{summary['window']['closes']}** (expected {EXPECTED_CLOSE_COUNT})

## Preflight

- Git HEAD: `{pf['git_head']}`
- Disk free (GiB): **{pf['disk_free_gib']}**
- Density warmup hours (BT-D4): **{pf['density_warmup_hours']}** (≥168 required)
- OHLC load: **{pf['ohlc_load_s']}** s; missing window bars: **{pf['ohlc_missing_for_window']}**
- Donor cache hits in window: **{pf['donor_hits_in_window']}** / misses **{pf['donor_misses_in_window']}**
- Bounded sample density (1 close): **{pf['bounded_sample_density_s']}** s
- BT-D60: `{pf.get('d60_verdict')}`

## Cache build

- Wall time: **{build_result.get('wall_s')}** s
- Validated snapshots: **{build_result.get('entry_count')}** / {EXPECTED_CLOSE_COUNT}
- Donor reuse: **{build_result.get('reused_entries', 0)}**
- Cold computed: **{build_result.get('cold_computed_entries', 0)}**
- Cold compute total: **{build_result.get('cold_compute_total_s', 0)}** s
- Mean cold (this run): **{build_result.get('cold_compute_mean_s', 0)}** s

Bundle: `results/phase_bt_d73_selected_sep23_24_v1/cache_bundle/`

## Chronological replay (RESEARCH_READY)

| Metric | V1 | V3 |
|--------|----|----|
| Pool candidates | {summary['pool_candidates']} | {summary['pool_candidates']} |
| Authorized at candidate | {summary['v1_authorized']} | {summary['v3_authorized']} |
| Blocked at candidate | {summary['v1_blocked_at_candidate']} | {summary['v3_blocked_at_candidate']} |
| Pending created | {summary['v1_pending_created']} | {summary['v3_pending_created']} |
| Modeled fills | {summary['v1_modeled_fills']} | {summary['v3_modeled_fills']} |
| First simulated SL bar | {summary['first_sl_v1']} | {summary['first_sl_v3']} |

**Fail-closed default:** `DEFAULT_SHORT_PAUSED` V1 pending created = **{summary['default_paused_v1_pending_created']}** (no silent pause disable).

**Disclaimer:** Simulated limit touches are not confirmed exchange executions.

## Artifacts

`results/phase_bt_d73_selected_sep23_24_v1/` — coverage_manifest, cache_progress, pool_candidates, ema_v1_v3_gate_comparison, pending chronology, modeled positions, signal_chart_timestamps, replay_summary.

## Chart review

See `signal_chart_timestamps.csv` for EMA diffs, pending-state diffs, and modeled fill/SL/TP events.
"""


if __name__ == "__main__":
    raise SystemExit(main())
