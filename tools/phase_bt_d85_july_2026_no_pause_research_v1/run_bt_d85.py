#!/usr/bin/env python3
"""BT-D85 — July 2026 EMA V3 research replay without post-SL pause lockout."""

from __future__ import annotations

import copy
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
D84_PKG = ROOT / "tools/phase_bt_d84_30day_no_pause_research_v1"
D80_PKG = ROOT / "tools/phase_bt_d80_multichunk_cache_pilot_v1"
D81_PKG = ROOT / "tools/phase_bt_d81_seven_day_cache_replay_v1"
D83_PKG = ROOT / "tools/phase_bt_d83_execution_parity_v1"
OUT = ROOT / "results/phase_bt_d85_july_2026_no_pause_research_v1"
REPORT = ROOT / "reports/PHASE_BT_D85_JULY_2026_NO_PAUSE_RESEARCH_V1.md"
BUNDLE = OUT / "cache_bundle"
D81_BUNDLE = ROOT / "results/phase_bt_d81_seven_day_cache_replay_v1/cache_bundle"
D80_BUNDLE = ROOT / "results/phase_bt_d80_multichunk_cache_pilot_v1/cache_bundle"
D73_BUNDLE = ROOT / "results/phase_bt_d73_selected_sep23_24_v1/cache_bundle"
D79 = ROOT / "tools/phase_bt_d79_chunk_acceleration_pilot_v1"
D71 = ROOT / "tools/phase_bt_d71_ema_comparison_v1"
ISOLATED = ROOT / "tools/phase_downtrend_isolated_upper_short_v1"

BOUNDARY_COUNT = 24
BUNDLE_WINDOW_LABEL = "2026-07-01T00:00:00Z_2026-07-31T23:55:00Z"
BASELINE_COMMITS = "BT-D83 f5fe267f; BT-D84 28cbc5c9 de5098b9"
MIN_DISK_GIB = 12.0

_PATH_DEPS = (
    D84_PKG,
    D83_PKG,
    D81_PKG,
    D80_PKG,
    D79,
    D71,
    ISOLATED,
    ROOT / "tools/phase_bt_d3_canonical_depth2_provider_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d8_canonical_density_cache_v1",
    ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1",
    ROOT / "tools/phase_bt_d60_ema_history_validation_v1",
    ROOT / "tools/phase_bt_d73_selected_sep23_24_v1",
)
for p in _PATH_DEPS:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))

from cache_contract_v1 import bands_for_parity, validate_snapshot_shape  # noqa: E402
from cache_store_v1 import (  # noqa: E402
    cache_key_for_close,
    load_bundle_manifest,
    provenance_from_manifest,
    read_cache_entry,
)
from chunk_plan_v1 import assert_chunks_export_safe  # noqa: E402
from config_loader_v1 import load_isolated_strategy_bundle  # noqa: E402
from d81_chunk_cache_build_v1 import build_chunk_cache_bundle  # noqa: E402
from d85_window_v1 import (  # noqa: E402
    EXPECTED_CLOSE_COUNT,
    REQUIRED_VISIBLE_TO_COVERAGE_UTC,
    WINDOW_END_UTC,
    WINDOW_START_UTC,
    chunk_boundary_starts,
    interior_sample_closes,
    iter_window_closes_inclusive,
    plan_calendar_day_chunks,
)
from window_policy_v1 import causal_window_bounds  # noqa: E402
from density_bridge_v1 import create_density_session  # noqa: E402
from density_provider_v1 import get_density_bands  # noqa: E402
from density_validation_v1 import validate_full_window_density  # noqa: E402
from month_performance_v1 import chart_review_timestamps, summarize_month_replay  # noqa: E402
from multichunk_lifetime_session_v1 import MultiChunkLifetimeSessionV1  # noqa: E402
from ohlc_loader_v1 import _iso, _parse_utc, load_5m_ohlc_ch  # noqa: E402
from parity_compare_v1 import compare_band_sets  # noqa: E402
from pure_ema_confirmation_v1 import build_pure_ema_gate  # noqa: E402
from research_density_gateway_v1 import snapshot_payload  # noqa: E402
from research_execution_runner_v1 import run_research_execution_replay  # noqa: E402
from research_pause_v1 import RESEARCH_PAUSE_CONTRACT, ResearchPauseControllerV1  # noqa: E402
from validation_config_v1 import REFERENCE_WARMUP_HOURS  # noqa: E402


class CacheOnlySession:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def collect_at_close(self, decision_close_utc: str) -> dict[str, Any]:
        snap = dict(self._inner.collect_at_close(decision_close_utc))
        snap["density_transport"] = "validated_cache"
        return snap


def _canonical_density_spot_check(closes: list[str]) -> dict[str, Any]:
    samples = [closes[0], closes[len(closes) // 2], closes[-1]]
    rows = []
    t0 = time.perf_counter()
    for c in samples:
        t1 = time.perf_counter()
        try:
            result = get_density_bands("DOGEUSDT", c)
            rows.append(
                {
                    "decision_close_utc": c,
                    "ok": True,
                    "band_count": len(result.bands),
                    "wall_s": round(time.perf_counter() - t1, 3),
                }
            )
        except Exception as exc:  # noqa: BLE001
            rows.append(
                {
                    "decision_close_utc": c,
                    "ok": False,
                    "error": str(exc)[:200],
                    "wall_s": round(time.perf_counter() - t1, 3),
                }
            )
    _last_from, last_vis_to = causal_window_bounds(closes[-1])
    return {
        "ok": all(r.get("ok") for r in rows),
        "samples": rows,
        "last_close_visible_to_utc": last_vis_to,
        "required_visible_to_coverage_utc": REQUIRED_VISIBLE_TO_COVERAGE_UTC,
        "visible_to_coverage_ok": last_vis_to <= REQUIRED_VISIBLE_TO_COVERAGE_UTC,
        "wall_s": round(time.perf_counter() - t0, 4),
    }


def _streak_stats(perf: dict[str, Any]) -> dict[str, int]:
    resolved = sorted(
        [t for t in (perf.get("trade_ledger") or []) if t.get("exit_reason") in ("SIMULATED_TP", "SIMULATED_SL")],
        key=lambda x: x.get("exit_time_utc") or "",
    )
    max_win = max_loss = 0
    w = l = 0
    for t in resolved:
        if t["exit_reason"] == "SIMULATED_TP":
            w += 1
            l = 0
            max_win = max(max_win, w)
        else:
            l += 1
            w = 0
            max_loss = max(max_loss, l)
    return {"max_winning_streak": max_win, "max_losing_streak": max_loss}


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def _git_status_short() -> str:
    try:
        return subprocess.check_output(["git", "status", "--short"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def _boundary_window(start: str, all_closes: list[str]) -> list[str]:
    if start not in all_closes:
        raise SystemExit(f"boundary start not in window: {start}")
    i = all_closes.index(start)
    chunk = all_closes[i : i + BOUNDARY_COUNT]
    if len(chunk) != BOUNDARY_COUNT:
        raise SystemExit(f"boundary window too short at {start}")
    return chunk


def _donor_close_sets() -> tuple[set[str], set[str], set[str]]:
    d81: set[str] = set()
    d80: set[str] = set()
    d73: set[str] = set()
    if (D81_BUNDLE / "bundle_manifest.json").is_file():
        d81 = {e["decision_close_utc"] for e in load_bundle_manifest(D81_BUNDLE).get("entries", [])}
    if (D80_BUNDLE / "bundle_manifest.json").is_file():
        d80 = {e["decision_close_utc"] for e in load_bundle_manifest(D80_BUNDLE).get("entries", [])}
    if (D73_BUNDLE / "bundle_manifest.json").is_file():
        d73 = {e["decision_close_utc"] for e in load_bundle_manifest(D73_BUNDLE).get("entries", [])}
    return d81, d80, d73


def _compare_close(
    close: str,
    multi: MultiChunkLifetimeSessionV1,
    *,
    d80_prov: Any | None,
    d73_prov: Any | None,
    d80_closes: set[str],
    d73_closes: set[str],
    run_canonical: bool,
) -> dict[str, Any]:
    row: dict[str, Any] = {"decision_close_utc": close}
    if run_canonical:
        t1 = time.perf_counter()
        canon = snapshot_payload(get_density_bands("DOGEUSDT", close))
        row["canonical_s"] = round(time.perf_counter() - t1, 3)
        t2 = time.perf_counter()
        chunk_snap = multi.collect_at_close(close)
        row["chunk_s"] = round(time.perf_counter() - t2, 3)
        cmp_c = compare_band_sets(bands_for_parity(canon), bands_for_parity(chunk_snap))
        row["chunk_parity_ok"] = cmp_c["ok"]
        if not cmp_c["ok"]:
            row["compare"] = cmp_c
    else:
        chunk_snap = multi.collect_at_close(close)
        row["chunk_parity_ok"] = None

    row["d80_parity_ok"] = None
    if d80_prov and close in d80_closes:
        key = cache_key_for_close(d80_prov, close)
        d8 = read_cache_entry(D80_BUNDLE, key, d80_prov, decision_close_utc=close)
        if run_canonical:
            row["d80_parity_ok"] = compare_band_sets(bands_for_parity(canon), bands_for_parity(d8))["ok"]
        else:
            row["d80_parity_ok"] = compare_band_sets(
                bands_for_parity(chunk_snap), bands_for_parity(d8)
            )["ok"]

    row["d73_parity_ok"] = None
    if d73_prov and close in d73_closes and run_canonical:
        key = cache_key_for_close(d73_prov, close)
        d8 = read_cache_entry(D73_BUNDLE, key, d73_prov, decision_close_utc=close)
        row["d73_parity_ok"] = compare_band_sets(bands_for_parity(canon), bands_for_parity(d8))["ok"]
    return row


def _parity_gate_batch(
    label: str,
    closes: list[str],
    multi: MultiChunkLifetimeSessionV1,
    *,
    d80_prov: Any | None,
    d73_prov: Any | None,
    d80_closes: set[str],
    d73_closes: set[str],
) -> dict[str, Any]:
    t0 = time.perf_counter()
    rows: list[dict[str, Any]] = []
    all_ok = True
    first_fail = None
    for close in closes:
        row = _compare_close(
            close,
            multi,
            d80_prov=d80_prov,
            d73_prov=d73_prov,
            d80_closes=d80_closes,
            d73_closes=d73_closes,
            run_canonical=True,
        )
        row["label"] = label
        if not row.get("chunk_parity_ok"):
            all_ok = False
            if first_fail is None:
                first_fail = row
        rows.append(row)
    return {
        "ok": all_ok,
        "label": label,
        "kind": "boundary",
        "first_failure": first_fail,
        "rows": rows,
        "wall_s": round(time.perf_counter() - t0, 4),
    }


def _interior_parity(
    closes: list[str],
    multi: MultiChunkLifetimeSessionV1,
    *,
    d80_prov: Any | None,
    d80_closes: set[str],
) -> dict[str, Any]:
    t0 = time.perf_counter()
    rows = []
    all_ok = True
    first_fail = None
    for close in closes:
        row = _compare_close(
            close,
            multi,
            d80_prov=d80_prov,
            d73_prov=None,
            d80_closes=d80_closes,
            d73_closes=set(),
            run_canonical=True,
        )
        row["label"] = "interior_noon"
        if not row.get("chunk_parity_ok"):
            all_ok = False
            if first_fail is None:
                first_fail = row
        rows.append(row)
    return {
        "ok": all_ok,
        "label": "interior_noon_samples",
        "kind": "interior",
        "first_failure": first_fail,
        "rows": rows,
        "wall_s": round(time.perf_counter() - t0, 4),
    }


def _validate_bundle(closes: list[str]) -> dict[str, Any]:
    manifest = load_bundle_manifest(BUNDLE)
    prov = provenance_from_manifest(manifest)
    missing = []
    failures = []
    for c in closes:
        try:
            key = cache_key_for_close(prov, c)
            snap = read_cache_entry(BUNDLE, key, prov, decision_close_utc=c)
            validate_snapshot_shape(snap, decision_close_utc=c)
        except Exception as exc:  # noqa: BLE001
            if "CACHE_ENTRY_MISSING" in str(exc):
                missing.append(c)
            else:
                failures.append({"close": c, "error": str(exc)[:200]})
    return {
        "ok": not missing and not failures,
        "entry_count": manifest.get("entry_count"),
        "missing": missing[:20],
        "missing_count": len(missing),
        "failures": failures,
    }


def _research_replay(
    closes: list[str],
    *,
    post_sl_pause_enabled: bool,
    label: str,
) -> dict[str, Any]:
    bundle = load_isolated_strategy_bundle()
    config = copy.deepcopy(bundle["baseline_config"])
    config["density"]["density_source"] = "validated_cache"
    config["density"]["validated_cache"] = {"bundle_path": str(BUNDLE.resolve())}
    session = CacheOnlySession(create_density_session(config))
    isolated = dict(bundle["isolated_strategy_v1"])
    isolated["pure_ema_confirmation"] = {
        **(isolated.get("pure_ema_confirmation") or {}),
        "enabled": True,
        "classifier_version": "v3",
    }
    isolated["isolated_upper_eligibility"] = {
        **(isolated.get("isolated_upper_eligibility") or {}),
        "research_pass_on_baseline_signal": True,
    }
    isolated["research_post_sl_pause"] = {
        "contract": RESEARCH_PAUSE_CONTRACT,
        "post_sl_pause_enabled": post_sl_pause_enabled,
        "research_runner_only": True,
    }
    load_start = _parse_utc(closes[0]) - timedelta(hours=REFERENCE_WARMUP_HOURS + 48)
    load_end = _parse_utc(closes[-1])
    bar_keys, prices, _ = load_5m_ohlc_ch(_iso(load_start), _iso(load_end))
    t_gate = time.perf_counter()
    gate = build_pure_ema_gate(bar_keys=bar_keys, prices=prices, isolated_strategy_config=isolated)
    ema_gate_s = round(time.perf_counter() - t_gate, 4)
    pause = ResearchPauseControllerV1(post_sl_pause_enabled=post_sl_pause_enabled)
    pause.set_ready_for_research_only()
    t0 = time.perf_counter()
    replay = run_research_execution_replay(
        config=config,
        isolated_bundle=isolated,
        prices=prices,
        bars=closes,
        density_session=session,
        pause=pause,
        ema_gate=gate,
    )
    replay["_wall_s"] = round(time.perf_counter() - t0, 4)
    replay["_ema_gate_wall_s"] = ema_gate_s
    replay["_label"] = label
    replay["_post_sl_pause_enabled"] = post_sl_pause_enabled
    return replay


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    worktree = _git_status_short()
    (OUT / "worktree_snapshot.txt").write_text(worktree[:8000], encoding="utf-8")

    closes = iter_window_closes_inclusive()
    if len(closes) != EXPECTED_CLOSE_COUNT:
        raise SystemExit(f"close count {len(closes)} != {EXPECTED_CLOSE_COUNT}")

    day_chunks = plan_calendar_day_chunks(closes)
    chunk_plan = assert_chunks_export_safe(day_chunks)
    (OUT / "chunk_plan.json").write_text(json.dumps(chunk_plan, indent=2), encoding="utf-8")

    load_start = _parse_utc(closes[0]) - timedelta(hours=REFERENCE_WARMUP_HOURS + 48)
    load_end = _parse_utc(closes[-1])
    _, ohlc_prices_probe, ohlc_meta = load_5m_ohlc_ch(_iso(load_start), _iso(load_end))
    missing_ohlc = [c for c in closes if c not in ohlc_prices_probe]
    ohlc_meta["window_closes_missing"] = len(missing_ohlc)
    ohlc_meta["window_ohlc_ok"] = not missing_ohlc
    if missing_ohlc:
        ohlc_meta["window_missing_sample"] = missing_ohlc[:5]

    d81_closes, d80_closes, d73_closes = _donor_close_sets()
    disk_free = shutil.disk_usage(ROOT).free / (1024**3)
    preflight = {
        "baseline_commits": BASELINE_COMMITS,
        "git_head": _git_head(),
        "closes": len(closes),
        "chunk_plan_days": len(day_chunks),
        "disk_free_gib": round(disk_free, 2),
        "d81_bundle_present": (D81_BUNDLE / "bundle_manifest.json").is_file(),
        "d81_donor_closes": len(d81_closes),
        "d80_bundle_present": (D80_BUNDLE / "bundle_manifest.json").is_file(),
        "d73_bundle_present": (D73_BUNDLE / "bundle_manifest.json").is_file(),
        "ohlc_meta": ohlc_meta,
        "research_post_sl_pause_enabled": False,
        "execution": "research_execution_runner_v1_d46_exit_parity",
    }
    (OUT / "preflight.json").write_text(json.dumps(preflight, indent=2), encoding="utf-8")

    if disk_free < MIN_DISK_GIB:
        preflight["blocked"] = "DISK_LOW"
        _write_report("JULY_MONTH_RESEARCH_BLOCKED", preflight, None, None, None, None, None)
        return 3
    if not ohlc_meta.get("window_ohlc_ok"):
        preflight["blocked"] = "OHLC_WINDOW_INCOMPLETE"
        _write_report("JULY_MONTH_RESEARCH_BLOCKED", preflight, None, None, None, None, None)
        return 4
    if ohlc_meta.get("out_of_import_bounds"):
        preflight["note"] = (
            "published_decision_close_hi is conservative vs requested window end; "
            "all 8640 decision closes verified in loaded 5m OHLC"
        )

    density_gate = validate_full_window_density(closes, expected_count=EXPECTED_CLOSE_COUNT)
    preflight["density_gate"] = density_gate
    (OUT / "preflight.json").write_text(json.dumps(preflight, indent=2), encoding="utf-8")
    if not density_gate["ok"]:
        preflight["blocked"] = "DENSITY_PUBLICATION_SCOPE"
        _write_report("JULY_MONTH_RESEARCH_BLOCKED", preflight, None, None, None, None, None)
        print("JULY_MONTH_RESEARCH_BLOCKED density scope", flush=True)
        return 9

    t_spot = time.perf_counter()
    spot = _canonical_density_spot_check(closes)
    spot["wall_s"] = round(time.perf_counter() - t_spot, 4)
    preflight["canonical_density_spot"] = spot
    if not spot["ok"] or not spot.get("visible_to_coverage_ok"):
        preflight["blocked"] = "CANONICAL_DENSITY_PREFLIGHT"
        (OUT / "preflight.json").write_text(json.dumps(preflight, indent=2), encoding="utf-8")
        _write_report("JULY_MONTH_RESEARCH_BLOCKED", preflight, None, None, None, None, None)
        print("JULY_MONTH_RESEARCH_BLOCKED canonical preflight", flush=True)
        return 10
    (OUT / "preflight.json").write_text(json.dumps(preflight, indent=2), encoding="utf-8")

    d80_prov = provenance_from_manifest(load_bundle_manifest(D80_BUNDLE)) if (D80_BUNDLE / "bundle_manifest.json").is_file() else None
    d73_prov = provenance_from_manifest(load_bundle_manifest(D73_BUNDLE)) if (D73_BUNDLE / "bundle_manifest.json").is_file() else None

    print("Building multichunk sessions (parity + build)...", flush=True)
    t_multi = time.perf_counter()
    multichunk = MultiChunkLifetimeSessionV1.build(day_chunks)
    multi_build_s = time.perf_counter() - t_multi

    parity_path = OUT / "parity_evidence.json"
    expected_parity_blocks = len(chunk_boundary_starts()) + 1
    parity_results: list[dict[str, Any]] = []
    if parity_path.is_file():
        existing = json.loads(parity_path.read_text(encoding="utf-8"))
        if len(existing) == expected_parity_blocks and all(x.get("ok") for x in existing):
            print("Reusing validated parity_evidence.json", flush=True)
            parity_results = existing
    if not parity_results:
        for i, start in enumerate(chunk_boundary_starts()):
            label = f"boundary_{start[:10]}_{start[11:16]}"
            bcloses = _boundary_window(start, closes)
            print(f"parity boundary {i + 1}/{len(chunk_boundary_starts())} {label}", flush=True)
            parity_results.append(
                _parity_gate_batch(
                    label,
                    bcloses,
                    multichunk,
                    d80_prov=d80_prov,
                    d73_prov=d73_prov,
                    d80_closes=d80_closes,
                    d73_closes=d73_closes,
                )
            )
            parity_path.write_text(json.dumps(parity_results, indent=2), encoding="utf-8")
        parity_results.append(
            _interior_parity(
                interior_sample_closes(),
                multichunk,
                d80_prov=d80_prov,
                d80_closes=d80_closes,
            )
        )
        parity_path.write_text(json.dumps(parity_results, indent=2), encoding="utf-8")

    if not all(r["ok"] for r in parity_results):
        _write_report("JULY_MONTH_RESEARCH_BLOCKED", preflight, parity_results, None, None, None, None)
        print("JULY_MONTH_RESEARCH_BLOCKED parity", flush=True)
        return 5

    progress_path = OUT / "cache_progress.json"
    t_cache = time.perf_counter()
    build_result = build_chunk_cache_bundle(
        bundle_dir=BUNDLE,
        decision_closes=closes,
        day_chunks=day_chunks,
        progress_path=progress_path,
        donor_dirs=[p for p in (D81_BUNDLE, D80_BUNDLE, D73_BUNDLE) if (p / "bundle_manifest.json").is_file()],
        multichunk=multichunk,
        bundle_window_label=BUNDLE_WINDOW_LABEL,
        progress_cb=lambda i, n, c, k: print(f"cache {i}/{n} {c} {k}", flush=True) if i % 200 == 0 or i == n else None,
    )
    build_result["multichunk_session_build_s"] = round(multi_build_s, 4)
    build_result["cache_build_wall_s"] = round(time.perf_counter() - t_cache, 4)
    build_result["donor_order"] = ["d81", "d80", "d73"]
    (OUT / "cache_build_result.json").write_text(json.dumps(build_result, indent=2), encoding="utf-8")

    if not build_result.get("complete"):
        _write_report("JULY_MONTH_RESEARCH_PARTIAL", preflight, parity_results, build_result, None, None, None)
        print("JULY_MONTH_RESEARCH_PARTIAL cache", flush=True)
        return 6

    t_val = time.perf_counter()
    validation = _validate_bundle(closes)
    validation["validate_wall_s"] = round(time.perf_counter() - t_val, 4)
    validation["expected_closes"] = EXPECTED_CLOSE_COUNT
    (OUT / "cache_validation.json").write_text(json.dumps(validation, indent=2), encoding="utf-8")
    if not validation["ok"] or validation.get("entry_count") != EXPECTED_CLOSE_COUNT:
        _write_report("JULY_MONTH_RESEARCH_PARTIAL", preflight, parity_results, build_result, validation, None, None)
        return 7

    t_rep = time.perf_counter()
    replay_no_pause = _research_replay(closes, post_sl_pause_enabled=False, label="no_post_sl_pause")
    ema_prep_s = replay_no_pause.get("_ema_gate_wall_s", 0)
    replay_paused_ref = _research_replay(closes, post_sl_pause_enabled=True, label="short_paused_reference")
    replay_s = time.perf_counter() - t_rep

    perf_no_pause = summarize_month_replay(replay_no_pause, label="no_post_sl_pause", post_sl_pause_enabled=False)
    perf_no_pause.update(_streak_stats(perf_no_pause))
    perf_paused = summarize_month_replay(
        replay_paused_ref, label="short_paused_reference", post_sl_pause_enabled=True
    )
    chart_ts = chart_review_timestamps(replay_no_pause, max_events=50)

    slim_replay = {
        k: v
        for k, v in replay_no_pause.items()
        if k
        not in (
            "gate_decisions",
            "ema_shadow_at_candidates",
            "baseline_pool_candidates",
            "blocked_candidates",
            "authorized_candidates",
        )
    }
    (OUT / "replay_no_pause.json").write_text(json.dumps(slim_replay, indent=2), encoding="utf-8")
    (OUT / "month_performance_no_pause.json").write_text(json.dumps(perf_no_pause, indent=2), encoding="utf-8")
    (OUT / "month_performance_short_paused_ref.json").write_text(json.dumps(perf_paused, indent=2), encoding="utf-8")
    (OUT / "chart_review_timestamps.json").write_text(json.dumps(chart_ts, indent=2), encoding="utf-8")

    (OUT / "trade_ledger_no_pause.json").write_text(
        json.dumps(perf_no_pause.get("trade_ledger") or [], indent=2), encoding="utf-8"
    )
    summary = {
        "verdict": "JULY_MONTH_RESEARCH_PASS",
        "timing_s": {
            "preflight_spot_canonical": (preflight.get("canonical_density_spot") or {}).get("wall_s"),
            "multichunk_session_build": build_result.get("multichunk_session_build_s"),
            "parity_total": round(sum(float(p.get("wall_s") or 0) for p in parity_results), 4),
            "cache_build_wall": build_result.get("cache_build_wall_s"),
            "cache_validation": validation.get("validate_wall_s"),
            "ema_gate_first_replay": ema_prep_s,
            "dual_replay_wall": round(replay_s, 4),
            "no_pause_replay_wall": replay_no_pause.get("_wall_s"),
            "paused_ref_replay_wall": replay_paused_ref.get("_wall_s"),
        },
        "replay_total_s": round(replay_s, 4),
        "no_pause": {k: v for k, v in perf_no_pause.items() if k != "trade_ledger"},
        "short_paused_reference": {k: v for k, v in perf_paused.items() if k != "trade_ledger"},
        "chart_review_sample": chart_ts[:25],
    }
    (OUT / "replay_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if perf_no_pause.get("coverage_incomplete", 0) > 0:
        summary["verdict"] = "JULY_MONTH_RESEARCH_PARTIAL"

    _write_report(
        summary["verdict"],
        preflight,
        parity_results,
        build_result,
        validation,
        summary,
        perf_no_pause,
    )
    print(summary["verdict"], flush=True)
    return 0 if summary["verdict"] == "JULY_MONTH_RESEARCH_PASS" else 8


def _write_report(
    verdict: str,
    preflight: dict,
    parity: list[dict] | None,
    build: dict | None,
    validation: dict | None,
    summary: dict | None,
    perf: dict | None,
) -> None:
    manifest = load_bundle_manifest(BUNDLE) if (BUNDLE / "bundle_manifest.json").is_file() else {}
    parity_ok = all(p.get("ok") for p in (parity or [])) if parity else None
    boundary_wall = sum(float(p.get("wall_s") or 0) for p in (parity or []) if p.get("kind") == "boundary")
    interior_wall = sum(float(p.get("wall_s") or 0) for p in (parity or []) if p.get("kind") == "interior")
    parity_status = "PASS" if parity_ok else ("FAIL" if parity_ok is False else "NOT_RUN")
    np = (summary or {}).get("no_pause") or perf or {}
    ref = (summary or {}).get("short_paused_reference") or {}
    ledger = (perf or {}).get("trade_ledger") or []

    timing = (summary or {}).get("timing_s") or {}

    body = f"""# BT-D85 — July 2026 EMA V3 No Post-SL Pause Research V1

**VERDICT:** `{verdict}`

**Baselines:** {BASELINE_COMMITS}; BT-D83 `EXECUTION_PARITY_PASS`  
**Git HEAD:** `{_git_head()}`  
**Window:** `{WINDOW_START_UTC}` … `{WINDOW_END_UTC}` (**{EXPECTED_CLOSE_COUNT}** closes)  
**Required causal coverage through:** `{REQUIRED_VISIBLE_TO_COVERAGE_UTC}`

## Research mode (BT-D84 reuse)

| Setting | Value |
|---------|-------|
| `post_sl_pause_enabled` | **false** |
| Pause contract | `{RESEARCH_PAUSE_CONTRACT}` |
| Execution | BT-D83 D46 research runner (TP/SL, post-fill same-bar, ambiguous policy) |
| Production | `SHORT_PAUSED` after SL **unchanged** |

## Provenance & cache

| Field | Value |
|-------|-------|
| Manifest entries | **{manifest.get('entry_count', 'n/a')}** / **{EXPECTED_CLOSE_COUNT}** |
| Pipeline | `{manifest.get('required_pipeline_line_id', 'n/a')}` |
| Config hash | `{manifest.get('required_config_hash', 'n/a')}` |
| Donor bundles (provenance only) | D81 → D80 → D73 (no July overlap) |
| Bundle | `results/phase_bt_d85_july_2026_no_pause_research_v1/cache_bundle/` |

## Parity evidence (bounded)

- Day-boundary gates: **{len(chunk_boundary_starts())}** × 24 closes — **{parity_status}**
- Interior noon: **{len(interior_sample_closes())}** closes
- Boundary wall: **{round(boundary_wall, 1)}** s; interior: **{round(interior_wall, 1)}** s
- File: `results/phase_bt_d85_july_2026_no_pause_research_v1/parity_evidence.json`

## Wall-clock stages

| Stage | Seconds |
|-------|--------:|
| Preflight canonical spot | **{timing.get('preflight_spot_canonical', 'n/a')}** |
| Multichunk session build | **{timing.get('multichunk_session_build', 'n/a')}** |
| Parity (total) | **{timing.get('parity_total', 'n/a')}** |
| Cache build | **{timing.get('cache_build_wall', 'n/a')}** |
| Cache validation | **{timing.get('cache_validation', 'n/a')}** |
| EMA gate (first replay) | **{timing.get('ema_gate_first_replay', 'n/a')}** |
| Dual replay wall | **{timing.get('dual_replay_wall', 'n/a')}** |
| Donor reuse | **{(build or {}).get('reused_donor', 'n/a')}** |
| New chunk writes | **{(build or {}).get('chunk_written', 'n/a')}** |

## July performance — no post-SL pause (primary)

| Metric | Value |
|--------|------:|
| Pool candidates | **{np.get('pool_candidates_seen', 'n/a')}** |
| EMA V3 authorized | **{np.get('ema_authorized', 'n/a')}** |
| EMA blocked | **{np.get('ema_blocked', 'n/a')}** |
| Pending created | **{np.get('pending_orders_created', 'n/a')}** |
| Pending EMA cancels | **{np.get('pending_cancels_ema', 'n/a')}** |
| Modeled fills | **{np.get('modeled_fills', 'n/a')}** |
| TP / SL / ambiguous | **{np.get('tp_count')}** / **{np.get('sl_count')}** / **{np.get('ambiguous_count')}** |
| Still open | **{np.get('still_open_trades')}** |
| Gross win rate (resolved) | **{np.get('gross_win_rate_pct_resolved')}%** |
| Avg win / avg loss (gross %) | **{np.get('avg_winning_trade_pct')}** / **{np.get('avg_losing_trade_pct')}** |
| Cumulative gross % | **{np.get('cumulative_gross_return_pct')}** |
| Max win / loss streak | **{np.get('max_winning_streak', 'n/a')}** / **{np.get('max_losing_streak', 'n/a')}** |
| Max drawdown (unit stack) | **{np.get('max_drawdown_pct_under_unit_gross_stack')}** |
| Final pause state | **{np.get('pause_state_final')}** |
| Coverage gaps | **{np.get('coverage_incomplete', 'n/a')}** |

**Not net profitability** — no fees, slippage, or execution uncertainty modeled.

## SHORT_PAUSED reference (separate replay)

| Metric | Value |
|--------|------:|
| Pending created | **{ref.get('pending_orders_created', 'n/a')}** |
| Modeled fills | **{ref.get('modeled_fills', 'n/a')}** |
| TP / SL | **{ref.get('tp_count')}** / **{ref.get('sl_count')}** |
| Final pause state | **{ref.get('pause_state_final')}** |

### Trade ledger

Full JSON: `results/phase_bt_d85_july_2026_no_pause_research_v1/trade_ledger_no_pause.json`

```json
{json.dumps(ledger[:20], indent=2)}
```

*(truncated to 20 trades in report; full ledger in results)*

### Chart review timestamps

Full JSON: `results/phase_bt_d85_july_2026_no_pause_research_v1/chart_review_timestamps.json`

```json
{json.dumps((summary or {}).get('chart_review_sample') or [], indent=2)}
```

## Safety

Research-only. No production activation, no CH writes/imports, no dashboard or BT-D46 changes.

**STOP** after July research.

## Appendix — preflight

```json
{json.dumps(preflight, indent=2)}
```
"""
    REPORT.write_text(body, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
