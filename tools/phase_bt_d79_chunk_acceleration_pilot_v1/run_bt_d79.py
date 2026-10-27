#!/usr/bin/env python3
"""BT-D79 — 24-close chunk lifetime acceleration pilot."""

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
OUT = ROOT / "results" / "phase_bt_d79_chunk_acceleration_pilot_v1"
REPORT = ROOT / "reports/PHASE_BT_D79_CHUNK_ACCELERATION_PILOT_V1.md"
BUNDLE = ROOT / "results" / "phase_bt_d73_selected_sep23_24_v1/cache_bundle"
D73 = ROOT / "tools/phase_bt_d73_selected_sep23_24_v1"
ISOLATED = ROOT / "tools/phase_downtrend_isolated_upper_short_v1"

PILOT_START = "2026-09-23T09:30:00Z"
PILOT_COUNT = 24

for p in (
    PKG,
    D73,
    ISOLATED,
    ROOT / "tools/phase_bt_d3_canonical_depth2_provider_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d8_canonical_density_cache_v1",
    ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1",
    ROOT / "tools/phase_bt_d60_ema_history_validation_v1",
    ROOT / "tools/phase_bt_d76_published_ch_binding_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from cache_contract_v1 import bands_for_parity  # noqa: E402
from cache_store_v1 import (  # noqa: E402
    cache_key_for_close,
    load_bundle_manifest,
    provenance_from_manifest,
    read_cache_entry,
)
from chunk_lifetime_density_v1 import ChunkLifetimeSessionV1, benchmark_chunk_collect  # noqa: E402
from config_loader_v1 import load_isolated_strategy_bundle  # noqa: E402
from density_bridge_v1 import create_density_session  # noqa: E402
from density_provider_v1 import get_density_bands  # noqa: E402
from isolated_chronological_runner_v1 import run_isolated_chronological_replay  # noqa: E402
from ohlc_loader_v1 import _iso, _parse_utc, load_5m_ohlc_ch  # noqa: E402
from parity_compare_v1 import compare_band_sets  # noqa: E402
from pure_ema_confirmation_v1 import build_pure_ema_gate  # noqa: E402
from pure_ema_series_provider_v1 import PureEmaSeriesProviderV1  # noqa: E402
from research_density_gateway_v1 import snapshot_payload  # noqa: E402
from selected_window_v1 import iter_window_closes_inclusive  # noqa: E402
from short_pause_state_v1 import ShortPauseControllerV1  # noqa: E402
from validation_config_v1 import REFERENCE_WARMUP_HOURS  # noqa: E402


def _pilot_closes() -> list[str]:
    all_c = iter_window_closes_inclusive()
    if PILOT_START not in all_c:
        raise SystemExit(f"pilot start not in D73 window: {PILOT_START}")
    i = all_c.index(PILOT_START)
    chunk = all_c[i : i + PILOT_COUNT]
    if len(chunk) != PILOT_COUNT:
        raise SystemExit(f"need {PILOT_COUNT} closes, got {len(chunk)}")
    return chunk


def _git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return "unknown"


def _bands_snap(snap: dict[str, Any]) -> list[dict[str, Any]]:
    return bands_for_parity(snap)


def run_parity_and_timing(closes: list[str]) -> dict[str, Any]:
    manifest = load_bundle_manifest(BUNDLE)
    prov = provenance_from_manifest(manifest)

    per_close: list[dict[str, Any]] = []
    canon_times: list[float] = []
    d8_times: list[float] = []

    t_canon_total = time.perf_counter()
    canonical_snaps: dict[str, dict[str, Any]] = {}
    for close in closes:
        t0 = time.perf_counter()
        res = get_density_bands("DOGEUSDT", close)
        canon_times.append(time.perf_counter() - t0)
        canonical_snaps[close] = snapshot_payload(res)
    canon_wall = time.perf_counter() - t_canon_total

    t_d8 = time.perf_counter()
    d8_snaps: dict[str, dict[str, Any]] = {}
    for close in closes:
        t0 = time.perf_counter()
        key = cache_key_for_close(prov, close)
        d8_snaps[close] = read_cache_entry(BUNDLE, key, prov, decision_close_utc=close)
        d8_times.append(time.perf_counter() - t0)
    d8_wall = time.perf_counter() - t_d8

    t_chunk_build = time.perf_counter()
    session = ChunkLifetimeSessionV1.build(closes)
    chunk_build_s = time.perf_counter() - t_chunk_build
    bench = benchmark_chunk_collect(session, closes)
    chunk_snaps = {c: session.collect_at_close(c) for c in closes}

    all_ok = True
    first_fail: dict[str, Any] | None = None
    for close in closes:
        c_bands = _bands_snap(canonical_snaps[close])
        ch_bands = _bands_snap(chunk_snaps[close])
        d8_bands = _bands_snap(d8_snaps[close])
        cmp_chunk = compare_band_sets(c_bands, ch_bands)
        cmp_d8 = compare_band_sets(c_bands, d8_bands)
        row = {
            "decision_close_utc": close,
            "chunk_parity_ok": cmp_chunk["ok"],
            "d8_parity_ok": cmp_d8["ok"],
            "canonical_band_count": len(c_bands),
            "chunk_band_count": len(ch_bands),
        }
        if not cmp_chunk["ok"] or not cmp_d8["ok"]:
            all_ok = False
            row["chunk_compare"] = cmp_chunk
            row["d8_compare"] = cmp_d8
            if first_fail is None:
                first_fail = row
        per_close.append(row)

    chunk_total_s = chunk_build_s + bench["per_close_node_clip_total_s"]
    return {
        "parity_ok": all_ok,
        "first_failure": first_fail,
        "per_close": per_close,
        "timing": {
            "canonical_total_s": round(canon_wall, 4),
            "canonical_mean_s": round(canon_wall / len(closes), 4),
            "canonical_per_close_s": [round(x, 4) for x in canon_times],
            "d8_total_s": round(d8_wall, 4),
            "d8_mean_s": round(d8_wall / len(closes), 6),
            "chunk_lifetime_build_s": round(chunk_build_s, 4),
            "chunk_node_clip": bench,
            "chunk_total_s": round(chunk_total_s, 4),
            "chunk_mean_s": round(chunk_total_s / len(closes), 4),
            "speedup_chunk_vs_canonical": round(canon_wall / chunk_total_s, 2) if chunk_total_s else None,
            "lifetime_timing_ms": session.timing.get("timing_ms"),
        },
        "session": session,
        "canonical_snaps": canonical_snaps,
        "chunk_snaps": chunk_snaps,
    }


def run_ema_chrono(closes: list[str], session: Any, label: str) -> dict[str, Any]:
    bundle = load_isolated_strategy_bundle()
    config = copy.deepcopy(bundle["baseline_config"])
    config["density"]["density_source"] = "canonical_provider"
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
    wall = time.perf_counter() - t0
    replay["_label"] = label
    replay["_wall_s"] = round(wall, 4)
    return replay


def _chrono_digest(replay: dict[str, Any]) -> str:
    return json.dumps(
        {
            "authorized": replay.get("authorized_candidates"),
            "blocked": replay.get("blocked_candidates"),
            "pending_created": replay.get("pending_orders_created"),
            "fills": replay.get("modeled_fills"),
            "positions": replay.get("modeled_positions"),
            "coverage": replay.get("coverage_incomplete"),
        },
        sort_keys=True,
        default=str,
    )


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    closes = _pilot_closes()
    (OUT / "pilot_closes.json").write_text(json.dumps(closes, indent=2), encoding="utf-8")

    result = run_parity_and_timing(closes)
    (OUT / "parity_per_close.json").write_text(json.dumps(result["per_close"], indent=2), encoding="utf-8")
    timing_out = {k: v for k, v in result["timing"].items() if k != "canonical_per_close_s"}
    timing_out["canonical_per_close_s_sample"] = result["timing"]["canonical_per_close_s"][:3]
    (OUT / "timing.json").write_text(json.dumps(timing_out, indent=2), encoding="utf-8")

    if not result["parity_ok"]:
        verdict = "CHUNK_ACCELERATION_FAIL"
        chrono = None
    else:
        class CanonSession:
            def collect_at_close(self, decision_close_utc: str) -> dict[str, Any]:
                return result["canonical_snaps"][decision_close_utc]

        chunk_session = result["session"]
        r_canon = run_ema_chrono(closes, CanonSession(), "canonical_snaps")
        r_chunk = run_ema_chrono(closes, chunk_session, "chunk_session")
        chrono_match = _chrono_digest(r_canon) == _chrono_digest(r_chunk)
        chrono = {
            "match": chrono_match,
            "canon_wall_s": r_canon["_wall_s"],
            "chunk_wall_s": r_chunk["_wall_s"],
        }
        (OUT / "chrono_parity.json").write_text(json.dumps(chrono, indent=2), encoding="utf-8")
        verdict = "CHUNK_ACCELERATION_PASS" if chrono_match else "CHUNK_ACCELERATION_FAIL"

    _write_report(closes, result, chrono, verdict)
    print(verdict, flush=True)
    return 0 if verdict == "CHUNK_ACCELERATION_PASS" else 2


def _write_report(
    closes: list[str],
    result: dict[str, Any],
    chrono: dict[str, Any] | None,
    verdict: str,
) -> None:
    t = result["timing"]
    body = f"""# BT-D79 — Chunk Lifetime Acceleration Pilot V1

**VERDICT:** `{verdict}`

**Git:** `{_git_head()}`  
**Pilot:** {len(closes)} closes `{closes[0]}` … `{closes[-1]}` (inside BT-D73 validated window)

## Density parity (canonical vs chunk vs D8)

- **Band parity chunk:** {'PASS' if result['parity_ok'] else 'FAIL'}
- **First failure:** `{json.dumps(result.get('first_failure'), default=str)[:500]}`

## Performance (measured)

| Workload | Total (s) | Mean / close (s) |
|----------|-----------|------------------|
| Canonical uncached (24× `get_density_bands`) | {t['canonical_total_s']} | {t['canonical_mean_s']} |
| Chunk amortized (1× lifetime + 24× Node/clip) | {t['chunk_total_s']} | {t['chunk_mean_s']} |
| D8 validated read (24×) | {t['d8_total_s']} | {t['d8_mean_s']} |

**Speedup chunk vs canonical:** **{t.get('speedup_chunk_vs_canonical')}×**

Chunk build lifetimes only: **{t['chunk_lifetime_build_s']}** s; Node+clip phase mean **{t['chunk_node_clip'].get('per_close_mean_ms')}** ms/close.

Lifetime builder `timing_ms` (from window): `{json.dumps(t.get('lifetime_timing_ms'), default=str)[:400]}`

## Chronological EMA V3 (after density parity)

{json.dumps(chrono, indent=2) if chrono else 'Skipped (density parity failed).'}

## Feasibility sketch

| Horizon | Closes (5m) | Canonical order-of-magnitude | With chunk amortization (if parity holds) | Replay with D8 |
|---------|-------------|------------------------------|-------------------------------------------|----------------|
| 3 days | ~864 | ~{round(t['canonical_mean_s']*864/3600,1)} h | ~{round(t['chunk_mean_s']*864/3600,1)} h compute | ~{round(t['d8_mean_s']*864,1)} s read |
| 7 days | ~2016 | extrapolate | extrapolate | D8 bundle build still dominates first-time |
| 30 days | ~8640 | extrapolate | needs chunking ≤72h export cap per window slice | same |

**Bottleneck remaining:** per-close **Node** subprocess (~{t['chunk_node_clip'].get('per_close_mean_ms')} ms here); lifetime build once per chunk; canonical still repeats full window per close.

## Smallest next safe step

1. Multi-chunk driver for >72h visible span (tile by `MAX_EXPORT_HOURS`).
2. Optional batched Node (multiple `as_of` in one process) with parity harness.
3. Use **D8** for 30-day **replay** after resumable build; use **chunk** only to accelerate **first-time** bundle generation.

**STOP.**
"""
    REPORT.write_text(body, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
