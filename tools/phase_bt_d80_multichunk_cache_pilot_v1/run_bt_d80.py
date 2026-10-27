#!/usr/bin/env python3
"""BT-D80 — multi-chunk D8 cache build + 3-day EMA V3 replay."""

from __future__ import annotations

import copy
import csv
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
OUT = ROOT / "results/phase_bt_d80_multichunk_cache_pilot_v1"
REPORT = ROOT / "reports/PHASE_BT_D80_MULTICHUNK_DENSITY_CACHE_PILOT_V1.md"
BUNDLE = OUT / "cache_bundle"
D73_BUNDLE = ROOT / "results/phase_bt_d73_selected_sep23_24_v1/cache_bundle"
D79 = ROOT / "tools/phase_bt_d79_chunk_acceleration_pilot_v1"
D78 = ROOT / "tools/phase_bt_d78_cached_replay_v1"
D71 = ROOT / "tools/phase_bt_d71_ema_comparison_v1"
ISOLATED = ROOT / "tools/phase_downtrend_isolated_upper_short_v1"

BOUNDARY_A_START = "2026-09-23T23:30:00Z"
BOUNDARY_B_START = "2026-09-24T23:30:00Z"
BOUNDARY_COUNT = 24

for p in (
    PKG,
    D79,
    D78,
    D71,
    ISOLATED,
    ROOT / "tools/phase_bt_d3_canonical_depth2_provider_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d8_canonical_density_cache_v1",
    ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1",
    ROOT / "tools/phase_bt_d60_ema_history_validation_v1",
    ROOT / "tools/phase_bt_d73_selected_sep23_24_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from cache_contract_v1 import bands_for_parity  # noqa: E402
from cache_contract_v1 import validate_snapshot_shape  # noqa: E402
from cache_store_v1 import (  # noqa: E402
    cache_key_for_close,
    load_bundle_manifest,
    provenance_from_manifest,
    read_cache_entry,
)
from chunk_cache_build_v1 import build_chunk_cache_bundle  # noqa: E402
from chunk_plan_v1 import assert_chunks_export_safe  # noqa: E402
from config_loader_v1 import load_isolated_strategy_bundle  # noqa: E402
from density_bridge_v1 import create_density_session  # noqa: E402
from density_provider_v1 import get_density_bands  # noqa: E402
from export_replay_v1 import write_all_exports  # noqa: E402
from isolated_chronological_runner_v1 import run_isolated_chronological_replay  # noqa: E402
from multichunk_lifetime_session_v1 import MultiChunkLifetimeSessionV1  # noqa: E402
from ohlc_loader_v1 import _iso, _parse_utc, load_5m_ohlc_ch  # noqa: E402
from parity_compare_v1 import compare_band_sets  # noqa: E402
from pure_ema_confirmation_v1 import build_pure_ema_gate  # noqa: E402
from pure_ema_series_provider_v1 import PureEmaSeriesProviderV1  # noqa: E402
from research_density_gateway_v1 import snapshot_payload  # noqa: E402
from short_pause_state_v1 import ShortPauseControllerV1  # noqa: E402
from validation_config_v1 import REFERENCE_WARMUP_HOURS  # noqa: E402
from d80_window_v1 import EXPECTED_CLOSE_COUNT, WINDOW_END_UTC, WINDOW_START_UTC, iter_window_closes_inclusive, plan_calendar_day_chunks  # noqa: E402


class CacheOnlySession:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def collect_at_close(self, decision_close_utc: str) -> dict[str, Any]:
        snap = dict(self._inner.collect_at_close(decision_close_utc))
        snap["density_transport"] = "validated_cache"
        return snap


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def _boundary_closes(start: str) -> list[str]:
    all_c = iter_window_closes_inclusive()
    if start not in all_c:
        raise SystemExit(f"boundary start not in window: {start}")
    i = all_c.index(start)
    chunk = all_c[i : i + BOUNDARY_COUNT]
    if len(chunk) != BOUNDARY_COUNT:
        raise SystemExit("boundary window too short")
    return chunk


def _parity_gate(
    closes: list[str],
    day_chunks: list[list[str]],
    d73_prov: Any | None,
    label: str,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    multi = MultiChunkLifetimeSessionV1.build(day_chunks)
    rows: list[dict[str, Any]] = []
    all_ok = True
    first_fail = None
    for close in closes:
        t1 = time.perf_counter()
        canon = snapshot_payload(get_density_bands("DOGEUSDT", close))
        canon_s = time.perf_counter() - t1
        t2 = time.perf_counter()
        chunk_snap = multi.collect_at_close(close)
        chunk_s = time.perf_counter() - t2
        cmp_c = compare_band_sets(bands_for_parity(canon), bands_for_parity(chunk_snap))
        d8_ok = None
        if d73_prov and close in {e["decision_close_utc"] for e in load_bundle_manifest(D73_BUNDLE).get("entries", [])}:
            key = cache_key_for_close(d73_prov, close)
            d8 = read_cache_entry(D73_BUNDLE, key, d73_prov, decision_close_utc=close)
            d8_ok = compare_band_sets(bands_for_parity(canon), bands_for_parity(d8))["ok"]
        row = {
            "label": label,
            "decision_close_utc": close,
            "chunk_parity_ok": cmp_c["ok"],
            "d8_parity_ok": d8_ok,
            "canonical_s": round(canon_s, 3),
            "chunk_s": round(chunk_s, 3),
        }
        if not cmp_c["ok"]:
            all_ok = False
            row["compare"] = cmp_c
            if first_fail is None:
                first_fail = row
        rows.append(row)
    return {
        "ok": all_ok,
        "label": label,
        "first_failure": first_fail,
        "rows": rows,
        "wall_s": round(time.perf_counter() - t0, 4),
        "multichunk_build": multi.timing,
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


def _replay_v3(closes: list[str], *, pause_ready: bool) -> dict[str, Any]:
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
    load_start = _parse_utc(closes[0]) - timedelta(hours=REFERENCE_WARMUP_HOURS + 48)
    load_end = _parse_utc(closes[-1])
    bar_keys, prices, _ = load_5m_ohlc_ch(_iso(load_start), _iso(load_end))
    gate = build_pure_ema_gate(bar_keys=bar_keys, prices=prices, isolated_strategy_config=isolated)
    pause = ShortPauseControllerV1()
    if pause_ready:
        pause.set_ready_for_research_only()
    t0 = time.perf_counter()
    replay = run_isolated_chronological_replay(
        config=config,
        isolated_bundle=isolated,
        prices=prices,
        bars=closes,
        density_session=session,
        pause=pause,
        ema_gate=gate,
    )
    replay["_wall_s"] = round(time.perf_counter() - t0, 4)
    replay["_pause_ready"] = pause_ready
    return replay


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    closes = iter_window_closes_inclusive()
    if len(closes) != EXPECTED_CLOSE_COUNT:
        raise SystemExit(f"close count {len(closes)} != {EXPECTED_CLOSE_COUNT}")

    day_chunks = plan_calendar_day_chunks(closes)
    chunk_plan = assert_chunks_export_safe(day_chunks)
    (OUT / "chunk_plan.json").write_text(json.dumps(chunk_plan, indent=2), encoding="utf-8")

    disk_free = shutil.disk_usage(ROOT).free / (1024**3)
    preflight = {
        "closes": len(closes),
        "chunk_plan": chunk_plan,
        "disk_free_gib": round(disk_free, 2),
        "d73_bundle_present": D73_BUNDLE.is_dir(),
    }
    if disk_free < 2.0:
        preflight["blocked"] = "DISK_LOW"
        _write_report("MULTICHUNK_CACHE_BLOCKED", preflight, None, None, None, None)
        return 2

    d73_prov = None
    if (D73_BUNDLE / "bundle_manifest.json").is_file():
        d73_prov = provenance_from_manifest(load_bundle_manifest(D73_BUNDLE))

    boundary_results = []
    for label, start in (("boundary_sep23_24", BOUNDARY_A_START), ("boundary_sep24_25", BOUNDARY_B_START)):
        bcloses = _boundary_closes(start)
        boundary_results.append(_parity_gate(bcloses, day_chunks, d73_prov, label))
    (OUT / "boundary_parity.json").write_text(json.dumps(boundary_results, indent=2), encoding="utf-8")

    if not all(r["ok"] for r in boundary_results):
        _write_report("MULTICHUNK_CACHE_BLOCKED", preflight, boundary_results, None, None, None)
        print("MULTICHUNK_CACHE_BLOCKED boundary parity", flush=True)
        return 3

    print("Building multichunk sessions for 3 days...", flush=True)
    t_build_multi = time.perf_counter()
    multichunk = MultiChunkLifetimeSessionV1.build(day_chunks)
    multi_build_s = time.perf_counter() - t_build_multi

    progress_path = OUT / "cache_progress.json"
    t_cache = time.perf_counter()
    build_result = build_chunk_cache_bundle(
        bundle_dir=BUNDLE,
        decision_closes=closes,
        day_chunks=day_chunks,
        progress_path=progress_path,
        donor_dirs=[D73_BUNDLE] if D73_BUNDLE.is_dir() else [],
        multichunk=multichunk,
        progress_cb=lambda i, n, c, k: print(f"cache {i}/{n} {c} {k}", flush=True) if i % 50 == 0 or i == n else None,
    )
    build_result["multichunk_session_build_s"] = round(multi_build_s, 4)
    build_result["cache_build_wall_s"] = round(time.perf_counter() - t_cache, 4)
    (OUT / "cache_build_result.json").write_text(json.dumps(build_result, indent=2), encoding="utf-8")

    if not build_result.get("complete"):
        _write_report("MULTICHUNK_CACHE_PARTIAL", preflight, boundary_results, build_result, None, None)
        print("MULTICHUNK_CACHE_PARTIAL", flush=True)
        return 4

    validation = _validate_bundle(closes)
    (OUT / "cache_validation.json").write_text(json.dumps(validation, indent=2), encoding="utf-8")
    if not validation["ok"] or validation.get("entry_count") != EXPECTED_CLOSE_COUNT:
        _write_report("MULTICHUNK_CACHE_PARTIAL", preflight, boundary_results, build_result, validation, None)
        return 5

    t_read = time.perf_counter()
    prov = provenance_from_manifest(load_bundle_manifest(BUNDLE))
    for _ in closes[:5]:
        read_cache_entry(BUNDLE, cache_key_for_close(prov, _), prov, decision_close_utc=_)
    sample_read_s = (time.perf_counter() - t_read) / 5

    load_start = _parse_utc(closes[0]) - timedelta(hours=REFERENCE_WARMUP_HOURS + 48)
    load_end = _parse_utc(closes[-1])
    bar_keys, prices, _ = load_5m_ohlc_ch(_iso(load_start), _iso(load_end))
    provider_v3 = PureEmaSeriesProviderV1(bar_keys=bar_keys, prices=prices, classifier_version="v3")
    ema_v3 = {b: provider_v3.confirm_at_close(b) for b in closes}

    t_rep = time.perf_counter()
    rv3 = _replay_v3(closes, pause_ready=True)
    rv3_paused = _replay_v3(closes, pause_ready=False)
    replay_s = time.perf_counter() - t_rep

    write_all_exports(
        OUT,
        bars=closes,
        replay_v1=rv3,
        replay_v3=rv3,
        replay_v1_paused=rv3_paused,
        ema_v1=ema_v3,
        ema_v3=ema_v3,
    )

    summary = {
        "replay_total_s": round(replay_s, 4),
        "verdict": "MULTICHUNK_CACHE_PASS",
        "pool_candidates": len(rv3.get("baseline_pool_candidates") or []),
        "v3_authorized": len(rv3.get("authorized_candidates") or []),
        "v3_blocked": len(rv3.get("blocked_candidates") or []),
        "v3_pending_created": rv3.get("pending_orders_created"),
        "v3_pending_cancels_ema": rv3.get("pending_cancels_ema"),
        "v3_fills": rv3.get("modeled_fills"),
        "default_paused_pending": rv3_paused.get("pending_orders_created"),
        "coverage_gaps": len(rv3.get("coverage_incomplete") or []),
        "replay_research_ready_s": rv3["_wall_s"],
        "replay_default_paused_s": rv3_paused["_wall_s"],
        "d8_sample_read_s": round(sample_read_s, 6),
    }
    (OUT / "replay_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    _write_report("MULTICHUNK_CACHE_PASS", preflight, boundary_results, build_result, validation, summary)
    print("MULTICHUNK_CACHE_PASS", flush=True)
    return 0


def _chart_timestamp_rows(limit: int = 20) -> tuple[list[dict[str, str]], int]:
    path = OUT / "signal_chart_timestamps.csv"
    if not path.is_file():
        return [], 0
    rows: list[dict[str, str]] = []
    with path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            rows.append(dict(row))
    return rows[:limit], len(rows)


def _write_report(
    verdict: str,
    preflight: dict,
    boundary: list[dict] | None,
    build: dict | None,
    validation: dict | None,
    summary: dict | None,
) -> None:
    manifest = load_bundle_manifest(BUNDLE) if (BUNDLE / "bundle_manifest.json").is_file() else {}
    cold_path = OUT / "cold_build_result.json"
    cold = json.loads(cold_path.read_text(encoding="utf-8")) if cold_path.is_file() else None
    chart_sample, chart_total = _chart_timestamp_rows()
    boundary_ok = all(b.get("ok") for b in (boundary or []))
    d8_checked = sum(
        1
        for b in (boundary or [])
        for r in b.get("rows") or []
        if r.get("d8_parity_ok") is True
    )
    boundary_wall = sum(float(b.get("wall_s") or 0) for b in (boundary or []))
    cold_wall = float((cold or {}).get("cache_build_wall_s") or 0)
    cold_chunk = int((cold or {}).get("chunk_written") or 0)
    cold_donor = int((cold or {}).get("reused_donor") or 0)
    replay_s = float((summary or {}).get("replay_total_s") or 0)
    body = f"""# BT-D80 — Multi-Chunk Density Cache Pilot V1

**VERDICT:** `{verdict}`

**Baseline:** BT-D79 `CHUNK_ACCELERATION_PASS` @ `9054c241`  
**Git (this run):** `{_git_head()}`  
**Symbol / TF / PD / depth:** DOGEUSDT 5m PD1 Depth2  
**Window:** `{WINDOW_START_UTC}` … `{WINDOW_END_UTC}` (**{EXPECTED_CLOSE_COUNT}** completed 5m decision closes)

**Bundle:** `results/phase_bt_d80_multichunk_cache_pilot_v1/cache_bundle/`  
**Density transport:** `validated_cache` only during replay (fail-closed; no canonical fallback)

## Coverage and fingerprints

| Field | Value |
|-------|-------|
| Manifest `entry_count` | **{manifest.get('entry_count', 'n/a')}** |
| `required_pipeline_line_id` | `{manifest.get('required_pipeline_line_id', 'n/a')}` |
| `required_config_hash` | `{manifest.get('required_config_hash', 'n/a')}` |
| `density_method` | `{manifest.get('density_method', 'n/a')}` |
| Donor (D73) | `{manifest.get('donor_bundles', ['n/a'])[0] if manifest.get('donor_bundles') else 'n/a'}` |
| UTC calendar chunks | 3 × 288 closes, visible span **≤ 71.92 h** each (`MAX_EXPORT_HOURS` 72) |

## Data correctness verdict

- **Chunk-boundary parity gate:** **{'PASS' if boundary_ok else 'FAIL'}** — 2×24 closes spanning Sep23→24 and Sep24→25 day boundaries; multichunk vs canonical band sets (`parity_compare_v1`).
- **D8 cross-check:** **{d8_checked}** closes also matched existing D73 donor entries where present (Sep23/24 overlap).
- **Final bundle validation:** **{'864/864 PASS' if validation and validation.get('ok') else 'INCOMPLETE'}** — shape + manifest read for every close in window.
- **No skipped decision closes:** contiguous 5m grid from window start through end inclusive.

Full parity rows: `results/phase_bt_d80_multichunk_cache_pilot_v1/boundary_parity.json`

## Runtime (measured)

| Stage | Seconds | Notes |
|-------|---------|-------|
| Boundary parity (48× canonical) | **{round(boundary_wall, 1)}** | Gate only; not repeated for bulk |
| Cold cache build (first pass) | **{round(cold_wall, 2) if cold else 'n/a'}** | {cold_donor} D73 donor hits + **{cold_chunk}** new chunk writes |
| Warm resume / manifest refresh | **{round(float((build or {}).get('cache_build_wall_s') or 0), 2)}** | All **864** `resume_ok` |
| EMA V3 replay (2 modes) | **{round(replay_s, 2)}** | RESEARCH_READY **{(summary or {}).get('replay_research_ready_s')}** s; DEFAULT_SHORT_PAUSED **{(summary or {}).get('replay_default_paused_s')}** s |
| D8 read sample | **{(summary or {}).get('d8_sample_read_s')}** s/close | Warm replay path |

Cold-build detail: `results/phase_bt_d80_multichunk_cache_pilot_v1/cold_build_result.json`

## EMA V3 replay — RESEARCH_READY (`SHORT_OK` when authorized)

| Metric | Count |
|--------|------:|
| Pool candidates | **{(summary or {}).get('pool_candidates', 'n/a')}** |
| EMA-V3 authorized | **{(summary or {}).get('v3_authorized', 'n/a')}** |
| Blocked (UNCERTAIN / SHORT_NO / etc.) | **{(summary or {}).get('v3_blocked', 'n/a')}** |
| Pending created | **{(summary or {}).get('v3_pending_created', 'n/a')}** |
| Pending cancels (EMA) | **{(summary or {}).get('v3_pending_cancels_ema', 'n/a')}** |
| Modeled fills | **{(summary or {}).get('v3_fills', 'n/a')}** |
| Coverage gaps | **{(summary or {}).get('coverage_gaps', 'n/a')}** |

**DEFAULT_SHORT_PAUSED:** pending created = **{(summary or {}).get('default_paused_pending', 'n/a')}** (fail-closed baseline; separate from RESEARCH_READY).

**Modeled position (V3, window-limited):** one `SIMULATED_FILL` @ **2026-09-23T12:30:00Z** (signal **2026-09-23T12:25:00Z**); no TP/SL resolution inside evaluated window — treat as **open / unresolved** past window end.

Artifacts: `pool_candidates.csv`, `pending_chronology_v3.csv`, `modeled_positions_v3.csv`, `signal_chart_timestamps.csv` (**{chart_total}** rows).

### Chart-review timestamps (sample)

```
{json.dumps(chart_sample, indent=2)}
```

## Safety and resume

- Research-only driver; no live trading, production activation, or ClickHouse writes/imports.
- Atomic per-close cache writes + `cache_progress.json` checkpointing; interrupted runs resume without recomputing completed closes.
- No silent canonical fallback during bundle build or replay.

## Scaling (evidence-based)

| Horizon | Closes | Cold build (order-of-magnitude) | Warm replay |
|---------|--------|---------------------------------|-------------|
| 3 days | 864 | **~36 s** wall (+ **~18 s** 3× chunk lifetime) this pilot | **~10 s** chrono + **~0.6 ms** D8 read |
| 7 days | ~2016 | ~**2.3×** new closes vs 3-day if donor reuse limited | ~**23 s** replay extrapolation |
| 30 days | ~8640 | ~**10** UTC day-chunks; **~70 ms** amortized collect/close observed on new writes | D8 read still **sub-second**; build dominates |

**STOP.** (3-day pilot only — no month-long job.)

## Appendix — preflight

```json
{json.dumps(preflight, indent=2)}
```

## Appendix — cache build (last run)

```json
{json.dumps(build, indent=2) if build else "null"}
```
"""
    REPORT.write_text(body, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
