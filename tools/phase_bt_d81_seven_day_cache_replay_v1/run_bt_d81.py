#!/usr/bin/env python3
"""BT-D81 — seven-day multichunk D8 cache + EMA V3 replay."""

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
D80_PKG = ROOT / "tools/phase_bt_d80_multichunk_cache_pilot_v1"
OUT = ROOT / "results/phase_bt_d81_seven_day_cache_replay_v1"
REPORT = ROOT / "reports/PHASE_BT_D81_SEVEN_DAY_CACHE_REPLAY_V1.md"
BUNDLE = OUT / "cache_bundle"
D80_BUNDLE = ROOT / "results/phase_bt_d80_multichunk_cache_pilot_v1/cache_bundle"
D73_BUNDLE = ROOT / "results/phase_bt_d73_selected_sep23_24_v1/cache_bundle"
D79 = ROOT / "tools/phase_bt_d79_chunk_acceleration_pilot_v1"
D71 = ROOT / "tools/phase_bt_d71_ema_comparison_v1"
ISOLATED = ROOT / "tools/phase_downtrend_isolated_upper_short_v1"

BOUNDARY_COUNT = 24
BUNDLE_WINDOW_LABEL = "2026-09-20T00:00:00Z_2026-09-26T23:55:00Z"

_PATH_DEPS = (
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
from d81_chunk_cache_build_v1 import build_chunk_cache_bundle  # noqa: E402
from chunk_plan_v1 import assert_chunks_export_safe  # noqa: E402
from config_loader_v1 import load_isolated_strategy_bundle  # noqa: E402
from d81_window_v1 import (  # noqa: E402
    EXPECTED_CLOSE_COUNT,
    WINDOW_END_UTC,
    WINDOW_START_UTC,
    chunk_boundary_starts,
    interior_sample_closes,
    iter_window_closes_inclusive,
    plan_calendar_day_chunks,
)
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
from verify_d80_fill_v1 import verify_d80_v3_fill  # noqa: E402


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


def _boundary_window(start: str, all_closes: list[str]) -> list[str]:
    if start not in all_closes:
        raise SystemExit(f"boundary start not in window: {start}")
    i = all_closes.index(start)
    chunk = all_closes[i : i + BOUNDARY_COUNT]
    if len(chunk) != BOUNDARY_COUNT:
        raise SystemExit(f"boundary window too short at {start}")
    return chunk


def _donor_close_sets() -> tuple[set[str], set[str]]:
    d80: set[str] = set()
    d73: set[str] = set()
    if (D80_BUNDLE / "bundle_manifest.json").is_file():
        d80 = {e["decision_close_utc"] for e in load_bundle_manifest(D80_BUNDLE).get("entries", [])}
    if (D73_BUNDLE / "bundle_manifest.json").is_file():
        d73 = {e["decision_close_utc"] for e in load_bundle_manifest(D73_BUNDLE).get("entries", [])}
    return d80, d73


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


def _trade_ledger_v3() -> list[dict[str, str]]:
    path = OUT / "modeled_positions_v3.csv"
    if not path.is_file():
        return []
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    ledger = []
    for r in rows:
        ledger.append(
            {
                "event": r.get("event"),
                "signal_utc": r.get("signal_close_utc"),
                "fill_or_event_utc": r.get("decision_close_utc"),
                "entry": r.get("entry_price"),
                "exit": r.get("exit_price") or "",
                "sl": r.get("sl_price"),
                "tp": r.get("tp_price"),
                "pool_key": r.get("pool_key"),
                "unresolved": "yes" if r.get("event") == "SIMULATED_FILL" and not r.get("exit_price") else "no",
                "fees_slippage": "none_modeled_isolated_touch_fill",
            }
        )
    return ledger


def _chart_events(limit: int = 15) -> list[dict[str, str]]:
    path = OUT / "signal_chart_timestamps.csv"
    if not path.is_file():
        return []
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    return rows[:limit]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    fill_check = verify_d80_v3_fill()
    (OUT / "d80_fill_verification.json").write_text(json.dumps(fill_check, indent=2), encoding="utf-8")
    if not fill_check.get("ok"):
        _write_report("SEVEN_DAY_CACHE_REPLAY_BLOCKED", {"fill_check": fill_check}, None, None, None, None, None)
        print("SEVEN_DAY_CACHE_REPLAY_BLOCKED fill verification", flush=True)
        return 2

    closes = iter_window_closes_inclusive()
    if len(closes) != EXPECTED_CLOSE_COUNT:
        raise SystemExit(f"close count {len(closes)} != {EXPECTED_CLOSE_COUNT}")

    day_chunks = plan_calendar_day_chunks(closes)
    chunk_plan = assert_chunks_export_safe(day_chunks)
    (OUT / "chunk_plan.json").write_text(json.dumps(chunk_plan, indent=2), encoding="utf-8")

    load_start = _parse_utc(closes[0]) - timedelta(hours=REFERENCE_WARMUP_HOURS + 48)
    load_end = _parse_utc(closes[-1])
    _, _, ohlc_meta = load_5m_ohlc_ch(_iso(load_start), _iso(load_end))

    disk_free = shutil.disk_usage(ROOT).free / (1024**3)
    preflight = {
        "closes": len(closes),
        "chunk_plan": chunk_plan,
        "disk_free_gib": round(disk_free, 2),
        "d80_bundle_present": D80_BUNDLE.is_dir(),
        "d73_bundle_present": D73_BUNDLE.is_dir(),
        "ohlc_meta": ohlc_meta,
        "fill_verification_ok": True,
    }
    if disk_free < 3.0:
        preflight["blocked"] = "DISK_LOW"
        _write_report("SEVEN_DAY_CACHE_REPLAY_BLOCKED", preflight, None, None, None, None, fill_check)
        return 3
    if ohlc_meta.get("out_of_import_bounds"):
        preflight["blocked"] = "OHLC_OUT_OF_BOUNDS"
        _write_report("SEVEN_DAY_CACHE_REPLAY_BLOCKED", preflight, None, None, None, None, fill_check)
        return 4

    d80_prov = provenance_from_manifest(load_bundle_manifest(D80_BUNDLE)) if (D80_BUNDLE / "bundle_manifest.json").is_file() else None
    d73_prov = provenance_from_manifest(load_bundle_manifest(D73_BUNDLE)) if (D73_BUNDLE / "bundle_manifest.json").is_file() else None
    d80_closes, d73_closes = _donor_close_sets()

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
        for start in chunk_boundary_starts():
            label = f"boundary_{start[:10]}_{start[11:16]}"
            bcloses = _boundary_window(start, closes)
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
        _write_report("SEVEN_DAY_CACHE_REPLAY_BLOCKED", preflight, parity_results, None, None, None, fill_check)
        print("SEVEN_DAY_CACHE_REPLAY_BLOCKED parity", flush=True)
        return 5

    progress_path = OUT / "cache_progress.json"
    t_cache = time.perf_counter()
    build_result = build_chunk_cache_bundle(
        bundle_dir=BUNDLE,
        decision_closes=closes,
        day_chunks=day_chunks,
        progress_path=progress_path,
        donor_dirs=[p for p in (D80_BUNDLE, D73_BUNDLE) if p.is_dir()],
        multichunk=multichunk,
        bundle_window_label=BUNDLE_WINDOW_LABEL,
        progress_cb=lambda i, n, c, k: print(f"cache {i}/{n} {c} {k}", flush=True) if i % 100 == 0 or i == n else None,
    )
    build_result["multichunk_session_build_s"] = round(multi_build_s, 4)
    build_result["cache_build_wall_s"] = round(time.perf_counter() - t_cache, 4)
    (OUT / "cache_build_result.json").write_text(json.dumps(build_result, indent=2), encoding="utf-8")

    if not build_result.get("complete"):
        _write_report("SEVEN_DAY_CACHE_REPLAY_PARTIAL", preflight, parity_results, build_result, None, None, fill_check)
        print("SEVEN_DAY_CACHE_REPLAY_PARTIAL", flush=True)
        return 6

    t_val = time.perf_counter()
    validation = _validate_bundle(closes)
    validation["validate_wall_s"] = round(time.perf_counter() - t_val, 4)
    (OUT / "cache_validation.json").write_text(json.dumps(validation, indent=2), encoding="utf-8")
    if not validation["ok"] or validation.get("entry_count") != EXPECTED_CLOSE_COUNT:
        _write_report("SEVEN_DAY_CACHE_REPLAY_PARTIAL", preflight, parity_results, build_result, validation, None, fill_check)
        return 7

    t_rep = time.perf_counter()
    rv3 = _replay_v3(closes, pause_ready=True)
    rv3_paused = _replay_v3(closes, pause_ready=False)
    replay_s = time.perf_counter() - t_rep

    bar_keys, prices, _ = load_5m_ohlc_ch(_iso(load_start), _iso(load_end))
    provider_v3 = PureEmaSeriesProviderV1(bar_keys=bar_keys, prices=prices, classifier_version="v3")
    ema_v3 = {b: provider_v3.confirm_at_close(b) for b in closes}
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
        "verdict": "SEVEN_DAY_CACHE_REPLAY_PASS",
        "replay_total_s": round(replay_s, 4),
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
        "trade_ledger_v3": _trade_ledger_v3(),
        "chart_events_sample": _chart_events(15),
    }
    (OUT / "replay_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    _write_report(
        "SEVEN_DAY_CACHE_REPLAY_PASS",
        preflight,
        parity_results,
        build_result,
        validation,
        summary,
        fill_check,
    )
    print("SEVEN_DAY_CACHE_REPLAY_PASS", flush=True)
    return 0


def _write_report(
    verdict: str,
    preflight: dict,
    parity: list[dict] | None,
    build: dict | None,
    validation: dict | None,
    summary: dict | None,
    fill_check: dict | None,
) -> None:
    manifest = load_bundle_manifest(BUNDLE) if (BUNDLE / "bundle_manifest.json").is_file() else {}
    parity_ok = all(p.get("ok") for p in (parity or []))
    boundary_wall = sum(float(p.get("wall_s") or 0) for p in (parity or []) if p.get("kind") == "boundary")
    interior_wall = sum(float(p.get("wall_s") or 0) for p in (parity or []) if p.get("kind") == "interior")
    body = f"""# BT-D81 — Seven-Day Multichunk Cache & EMA V3 Replay V1

**VERDICT:** `{verdict}`

**Baseline:** BT-D80 `MULTICHUNK_CACHE_PASS` @ `5deb2a5e`  
**Git:** `{_git_head()}`  
**Window:** `{WINDOW_START_UTC}` … `{WINDOW_END_UTC}` (**{EXPECTED_CLOSE_COUNT}** closes)

## A — BT-D80 fill verification (gate)

```json
{json.dumps(fill_check, indent=2) if fill_check else "null"}
```

## Coverage / fingerprints

| Field | Value |
|-------|-------|
| Manifest entries | **{manifest.get('entry_count', 'n/a')}** / **{EXPECTED_CLOSE_COUNT}** |
| Pipeline | `{manifest.get('required_pipeline_line_id', 'n/a')}` |
| Config hash | `{manifest.get('required_config_hash', 'n/a')}` |
| Chunks | 7 × 288 UTC days, span ≤ 72 h export cap |

## C — Parity evidence

- Boundary gates: **{len([p for p in (parity or []) if p.get('kind')=='boundary'])}** × 24 closes — **{'PASS' if parity_ok else 'FAIL'}**
- Interior noon samples: **{len(interior_sample_closes())}** closes — canonical vs multichunk
- Boundary canonical wall: **{round(boundary_wall, 1)}** s; interior: **{round(interior_wall, 1)}** s
- Detail: `results/phase_bt_d81_seven_day_cache_replay_v1/parity_evidence.json`

## Build & replay timing

| Stage | Seconds |
|-------|--------:|
| Multichunk session build | **{(build or {}).get('multichunk_session_build_s', 'n/a')}** |
| Cache build wall | **{(build or {}).get('cache_build_wall_s', 'n/a')}** |
| Donor reuse | **{(build or {}).get('reused_donor', 'n/a')}** |
| New chunk writes | **{(build or {}).get('chunk_written', 'n/a')}** |
| Validation | **{(validation or {}).get('validate_wall_s', 'n/a')}** |
| Replay (2 modes) | **{(summary or {}).get('replay_total_s', 'n/a')}** |

## D–E — EMA V3 replay (validated_cache)

| Metric | RESEARCH_READY |
|--------|---------------:|
| Pool candidates | **{(summary or {}).get('pool_candidates', 'n/a')}** |
| EMA-V3 authorized | **{(summary or {}).get('v3_authorized', 'n/a')}** |
| Blocked | **{(summary or {}).get('v3_blocked', 'n/a')}** |
| Pending created | **{(summary or {}).get('v3_pending_created', 'n/a')}** |
| Pending EMA cancels | **{(summary or {}).get('v3_pending_cancels_ema', 'n/a')}** |
| Modeled fills | **{(summary or {}).get('v3_fills', 'n/a')}** |
| Coverage gaps | **{(summary or {}).get('coverage_gaps', 'n/a')}** |

**DEFAULT_SHORT_PAUSED pending created:** **{(summary or {}).get('default_paused_pending', 'n/a')}**

**Candidate vs executable:** pool candidates include every baseline upper signal bar; only **SHORT_OK** at signal close creates Pending; UNCERTAIN/SHORT_NO blocks authorization (see `ema_v1_v3_gate_comparison.csv`).

### Trade ledger (V3)

```json
{json.dumps((summary or {}).get('trade_ledger_v3'), indent=2) if summary else "[]"}
```

### Chart review events (sample)

```json
{json.dumps((summary or {}).get('chart_events_sample'), indent=2) if summary else "[]"}
```

## Limitations before 30-day

- Parity is **bounded** (boundaries + noon samples), not full-window canonical per close.
- Isolated runner models **SL exit** only; **TP touches do not close** positions in current BT-D46 contract.
- Single continuous chrono replay — no per-day state reset.
- **Pause semantics:** RESEARCH_READY sets `READY` only at replay start; modeled SL on **2026-09-20T14:05:00Z** re-enters `SHORT_PAUSED` with no auto-unlock. That suppresses later Sep-23 activity seen in the narrower BT-D80 window (which did not include Sep-20).

**STOP.** No 30-day launch.

## Appendix — preflight

```json
{json.dumps(preflight, indent=2)}
```
"""
    REPORT.write_text(body, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
