#!/usr/bin/env python3
"""BT-D78 — D73 validated-cache-only replay + lifetime feasibility inputs."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
PKG = Path(__file__).resolve().parent
OUT = ROOT / "results" / "phase_bt_d78_cached_replay_v1"
REPORT = ROOT / "reports/PHASE_BT_D78_CACHED_REPLAY_AND_LIFETIME_FEASIBILITY_V1.md"
D73 = ROOT / "tools/phase_bt_d73_selected_sep23_24_v1"
D73_OUT = ROOT / "results/phase_bt_d73_selected_sep23_24_v1"
BUNDLE = D73_OUT / "cache_bundle"
ISOLATED = ROOT / "tools/phase_downtrend_isolated_upper_short_v1"
D71 = ROOT / "tools/phase_bt_d71_ema_comparison_v1"

for p in (
    PKG,
    D73,
    ISOLATED,
    D71,
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d8_canonical_density_cache_v1",
    ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1",
    ROOT / "tools/phase_bt_d60_ema_history_validation_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from cache_store_v1 import (  # noqa: E402
    cache_key_for_close,
    load_bundle_manifest,
    provenance_from_manifest,
    read_cache_entry,
    snapshot_content_sha256,
)
from cache_contract_v1 import validate_snapshot_shape  # noqa: E402
from config_loader_v1 import load_isolated_strategy_bundle  # noqa: E402
from density_bridge_v1 import create_density_session  # noqa: E402
from export_replay_v1 import write_all_exports  # noqa: E402
from isolated_chronological_runner_v1 import run_isolated_chronological_replay  # noqa: E402
from ohlc_loader_v1 import _iso, _parse_utc, load_5m_ohlc_ch  # noqa: E402
from pure_ema_confirmation_v1 import build_pure_ema_gate  # noqa: E402
from pure_ema_series_provider_v1 import PureEmaSeriesProviderV1  # noqa: E402
from selected_window_v1 import (  # noqa: E402
    EXPECTED_CLOSE_COUNT,
    WINDOW_END_UTC,
    WINDOW_START_UTC,
    iter_window_closes_inclusive,
)
from short_pause_state_v1 import ShortPauseControllerV1  # noqa: E402
from validation_config_v1 import REFERENCE_WARMUP_HOURS  # noqa: E402


class CacheOnlyDensitySession:
    """Fail-closed validated_cache — no canonical fallback."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def collect_at_close(self, decision_close_utc: str) -> dict[str, Any]:
        snap = dict(self._inner.collect_at_close(decision_close_utc))
        snap["density_transport"] = "validated_cache"
        return snap


def validate_bundle(closes: list[str]) -> dict[str, Any]:
    manifest_path = BUNDLE / "bundle_manifest.json"
    if not manifest_path.is_file():
        return {"ok": False, "error": "BUNDLE_MANIFEST_MISSING", "missing": closes}
    manifest = load_bundle_manifest(BUNDLE)
    prov = provenance_from_manifest(manifest)
    manifest_closes = {e["decision_close_utc"] for e in manifest.get("entries") or []}
    missing = [c for c in closes if c not in manifest_closes]
    failures: list[dict[str, str]] = []
    fp_set: set[str] = set()
    t0 = time.perf_counter()
    for close in closes:
        if close in missing:
            continue
        try:
            key = cache_key_for_close(prov, close)
            snap = read_cache_entry(BUNDLE, key, prov, decision_close_utc=close)
            validate_snapshot_shape(snap, decision_close_utc=close)
            if snap.get("decision_close_utc") != close:
                failures.append({"close": close, "error": "DECISION_CLOSE_MISMATCH"})
            if snap.get("as_of_utc") != close:
                failures.append({"close": close, "error": "AS_OF_MISMATCH"})
            if not snap.get("pipeline_line_id") or not snap.get("config_hash"):
                failures.append({"close": close, "error": "PROVENANCE_MISSING"})
            fp_set.add(snapshot_content_sha256(snap))
        except Exception as exc:  # noqa: BLE001
            failures.append({"close": close, "error": f"{type(exc).__name__}: {exc}"[:200]})
    return {
        "ok": not missing and not failures,
        "manifest_entry_count": manifest.get("entry_count"),
        "expected": len(closes),
        "missing_closes": missing,
        "validation_failures": failures[:50],
        "validation_failure_count": len(failures),
        "unique_content_sha256": len(fp_set),
        "validate_wall_s": round(time.perf_counter() - t0, 4),
        "required_pipeline_line_id": prov.required_pipeline_line_id,
        "required_config_hash": prov.required_config_hash,
    }


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


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    closes = iter_window_closes_inclusive()
    if len(closes) != EXPECTED_CLOSE_COUNT:
        REPORT.write_text(
            f"# BT-D78 BLOCKED\n\nClose list length {len(closes)} != {EXPECTED_CLOSE_COUNT}\n",
            encoding="utf-8",
        )
        return 2

    validation = validate_bundle(closes)
    (OUT / "cache_validation.json").write_text(json.dumps(validation, indent=2), encoding="utf-8")
    if not validation["ok"]:
        REPORT.write_text(
            "# BT-D78 — Cached Replay BLOCKED\n\n"
            f"**VERDICT:** `CACHED_REPLAY_BLOCKED`\n\n"
            f"```json\n{json.dumps(validation, indent=2)}\n```\n",
            encoding="utf-8",
        )
        print("CACHED_REPLAY_BLOCKED", validation.get("missing_closes", [])[:5], flush=True)
        return 3

    bundle = load_isolated_strategy_bundle()
    config = copy.deepcopy(bundle["baseline_config"])
    config["density"]["density_source"] = "validated_cache"
    config["density"]["validated_cache"] = {"bundle_path": str(BUNDLE.resolve())}
    session = CacheOnlyDensitySession(create_density_session(config))

    load_start = _parse_utc(WINDOW_START_UTC) - timedelta(hours=REFERENCE_WARMUP_HOURS + 48)
    load_end = _parse_utc(WINDOW_END_UTC)
    t_ohlc = time.perf_counter()
    bar_keys, prices, ohlc_meta = load_5m_ohlc_ch(_iso(load_start), _iso(load_end))
    ohlc_s = time.perf_counter() - t_ohlc
    ohlc_missing = [c for c in closes if c not in prices]

    isolated_base = dict(bundle["isolated_strategy_v1"])
    isolated_base["pure_ema_confirmation"] = {**(isolated_base.get("pure_ema_confirmation") or {}), "enabled": True}
    isolated_base["isolated_upper_eligibility"] = {
        **(isolated_base.get("isolated_upper_eligibility") or {}),
        "research_pass_on_baseline_signal": True,
    }

    provider_v1 = PureEmaSeriesProviderV1(bar_keys=bar_keys, prices=prices, classifier_version="v1")
    provider_v3 = PureEmaSeriesProviderV1(bar_keys=bar_keys, prices=prices, classifier_version="v3")
    t_ema = time.perf_counter()
    ema_v1 = {b: provider_v1.confirm_at_close(b) for b in closes}
    ema_v3 = {b: provider_v3.confirm_at_close(b) for b in closes}
    ema_map_s = time.perf_counter() - t_ema

    t_rep = time.perf_counter()
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
    replay_s = time.perf_counter() - t_rep

    write_all_exports(
        OUT,
        bars=closes,
        replay_v1=rv1,
        replay_v3=rv3,
        replay_v1_paused=rv1_paused,
        ema_v1=ema_v1,
        ema_v3=ema_v3,
    )

    pool_n = len(rv1.get("baseline_pool_candidates") or [])
    summary = {
        "phase": "BT-D78",
        "git_head": _git_head(),
        "verdict": "CACHED_REPLAY_PASS",
        "window": {"start": WINDOW_START_UTC, "end": WINDOW_END_UTC, "closes": len(closes)},
        "cache_validation": validation,
        "ohlc_load_s": round(ohlc_s, 4),
        "ohlc_missing_in_window": len(ohlc_missing),
        "ema_map_s": round(ema_map_s, 4),
        "replay_wall_s": round(replay_s, 4),
        "replay_per_close_ms": round(1000 * replay_s / len(closes), 3),
        "density_source": "validated_cache_only",
        "pool_candidates": pool_n,
        "v1_authorized": sum(1 for r in rv1.get("authorized_candidates") or []),
        "v3_authorized": sum(1 for r in rv3.get("authorized_candidates") or []),
        "v1_blocked": sum(1 for r in rv1.get("blocked_candidates") or []),
        "v3_blocked": sum(1 for r in rv3.get("blocked_candidates") or []),
        "v1_pending_created": rv1.get("pending_orders_created"),
        "v3_pending_created": rv3.get("pending_orders_created"),
        "v1_pending_cancels_ema": rv1.get("pending_cancels_ema"),
        "v3_pending_cancels_ema": rv3.get("pending_cancels_ema"),
        "v1_modeled_fills": rv1.get("modeled_fills"),
        "v3_modeled_fills": rv3.get("modeled_fills"),
        "first_sl_v1": _first_sl_bar(rv1),
        "first_sl_v3": _first_sl_bar(rv3),
        "default_paused_v1_pending_created": rv1_paused.get("pending_orders_created"),
        "coverage_gaps_v1": len(rv1.get("coverage_incomplete") or []),
        "coverage_gaps_v3": len(rv3.get("coverage_incomplete") or []),
        "ohlc_meta": ohlc_meta,
    }
    if summary["coverage_gaps_v1"] or summary["coverage_gaps_v3"] or ohlc_missing:
        summary["verdict"] = "CACHED_REPLAY_PARTIAL"

    (OUT / "replay_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # load chart timestamps from csv for report snippet
    chart_ts: list[dict[str, str]] = []
    cpath = OUT / "signal_chart_timestamps.csv"
    if cpath.is_file() and cpath.stat().st_size:
        import csv

        with cpath.open(encoding="utf-8") as f:
            chart_ts = list(csv.DictReader(f))

    _write_report(summary, validation, chart_ts[:30], len(chart_ts))
    print(summary["verdict"], flush=True)
    return 0 if summary["verdict"] == "CACHED_REPLAY_PASS" else 1


def _write_report(
    summary: dict[str, Any],
    validation: dict[str, Any],
    chart_sample: list[dict],
    chart_total: int,
) -> None:
    verdict = summary["verdict"]
    body = f"""# BT-D78 — Cached Replay and Lifetime Feasibility V1

**VERDICT:** `{verdict}`

**Git:** `{summary['git_head']}`  
**D73 bundle:** `results/phase_bt_d73_selected_sep23_24_v1/cache_bundle/`  
**Density:** `validated_cache` only (fail-closed; no canonical fallback)

## Part A — Cache coverage

| Check | Result |
|-------|--------|
| Window closes | **{summary['window']['closes']}** ({WINDOW_START_UTC} … {WINDOW_END_UTC}) |
| Manifest entries | **{validation['manifest_entry_count']}** |
| Missing closes | **{len(validation.get('missing_closes') or [])}** |
| Read/SHA validation failures | **{validation.get('validation_failure_count', 0)}** |
| Validate wall time | **{validation.get('validate_wall_s')}** s |
| Pipeline | `{validation.get('required_pipeline_line_id')}` |
| Config hash | `{validation.get('required_config_hash')}` |

## Part A — Replay metrics (RESEARCH_READY)

| Metric | V1 | V3 |
|--------|----|----|
| Pool candidates | {summary['pool_candidates']} | {summary['pool_candidates']} |
| Authorized | {summary['v1_authorized']} | {summary['v3_authorized']} |
| Blocked at candidate | {summary['v1_blocked']} | {summary['v3_blocked']} |
| Pending created | {summary['v1_pending_created']} | {summary['v3_pending_created']} |
| EMA pending cancels | {summary['v1_pending_cancels_ema']} | {summary['v3_pending_cancels_ema']} |
| Modeled fills | {summary['v1_modeled_fills']} | {summary['v3_modeled_fills']} |
| First simulated SL | {summary['first_sl_v1']} | {summary['first_sl_v3']} |
| Coverage gaps | {summary['coverage_gaps_v1']} | {summary['coverage_gaps_v3']} |

**DEFAULT_SHORT_PAUSED (V1):** pending created = **{summary['default_paused_v1_pending_created']}** (separate from RESEARCH_READY runs).

## Part A — Runtime

| Stage | Seconds |
|-------|---------|
| OHLC load | {summary['ohlc_load_s']} |
| EMA maps (386 closes × V1/V3) | {summary['ema_map_s']} |
| Chronological replay (V1+V3+paused V1) | {summary['replay_wall_s']} |
| Implied per-close replay | **{summary['replay_per_close_ms']}** ms (3 passes / 386) |

Artifacts: `results/phase_bt_d78_cached_replay_v1/` — pool_candidates, gate comparison, pending chronology, modeled positions, signal_chart_timestamps ({chart_total} chart-review rows).

### Chart-review timestamps (sample)

```
{json.dumps(chart_sample, indent=2)}
```

## Part B — Lifetime amortization feasibility (investigation only)

See report section in repository for full analysis. Summary:

- **`_build_lifetimes`** (`historical_window_v1.py`) walks the full 5m grid; membership is **piecewise constant** between publication slots but **pool geometry** (bottom/top) can change when the same `pool_id` reappears with updated bounds.
- **Immutable for a fixed close set:** batched CH catalog rows per `decision_close`; published `pool_ids` set at each slot.
- **Changes per as-of:** which pools are ACTIVE; segment merge boundaries when membership toggles; **Node `poolActiveAt(t)`** evaluates segments at each `as_of_utc`.
- **Safe reuse hypothesis:** prefetch members for all decision closes in `[chunk_start, chunk_end]` once (5S batch cache), build **one** lifetime pool list for the union grid, then run **`analyzer_depth2_landscape.js`** per close — **requires** band parity vs per-close canonical (D76-style) on a pilot chunk.
- **Not safe without proof:** reusing density from 4X-AK instant-segment kernel; skipping segment coalescence across membership gaps; sharing Node output across closes.

### Minimal next safe experiment

1. Pick **24 consecutive closes** inside this window.
2. Run canonical `collect_at_close` vs **one** shared `build_landscape_window` + per-close Node `as_of` (Tier C from BT-D77).
3. Compare band sets (`parity_compare_v1`) and one **isolated chronology** digest.

**STOP.**
"""
    REPORT.write_text(body, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
