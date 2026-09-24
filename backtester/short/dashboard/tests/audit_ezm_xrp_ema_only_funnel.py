#!/usr/bin/env python3
"""Read-only Stage-A funnel audit for XRPUSDT ema_only run.

Does NOT write job artifacts or start dashboard jobs.
Run: python dashboard/tests/audit_ezm_xrp_ema_only_funnel.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

OA_SRC = Path("/home/telgenbuescher/projects/orderbook_analyse/src")
if str(OA_SRC) not in sys.path:
    sys.path.insert(0, str(OA_SRC))

DASH_ROOT = Path(__file__).resolve().parents[1]
JOBS = DASH_ROOT.parent / "results" / "stoch_fade_research_jobs"
JOB_ID = "abca5b05abac4fa1a12f941abb993565"
RUN_ID = "ezme4487fef93db"


def _parse_z(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


@dataclass
class Funnel:
    closed_1m: int = 0
    ema_basis_ok: int = 0
    regime_ok: int = 0
    in_proximity: int = 0
    proximity_watches_started: int = 0
    ohlc_geometric_touch: int = 0
    exact_touch_raw: int = 0
    touch_with_watch_or_rearm: int = 0
    touch_blocked_no_rearm: int = 0
    touch_blocked_cooldown: int = 0
    touch_blocked_active: int = 0
    flat_blocked_watch: int = 0
    exported_exact_touch: int = 0
    exported_proximity: int = 0
    exported_flat_block: int = 0
    rearm_events: int = 0
    per_zone_exact_blocked_no_rearm: Counter = field(default_factory=Counter)
    per_zone_rearm: Counter = field(default_factory=Counter)
    watch_opens: list[dict] = field(default_factory=list)
    touch_attempts: list[dict] = field(default_factory=list)


def main() -> None:
    job_dir = JOBS / JOB_ID
    run_dir = job_dir / "coin_runs" / "XRPUSDT" / RUN_ID
    manifest = json.loads((run_dir / "run_manifest.json").read_text())
    candidates = json.loads((run_dir / "candidates.json").read_text())
    coverage_file = json.loads((run_dir / "coverage.json").read_text())

    eff_start = _parse_z(manifest["effective_start"])
    eff_end = _parse_z(manifest["effective_end"])
    req_start = _parse_z(manifest["signal_start"])
    req_end = _parse_z(manifest["signal_end_exclusive"])

    from orderbook_analyse.ema_zone_microstructure_confirmation.continuous_runner import (
        candle_analysis_samples,
        run_symbol,
    )
    from orderbook_analyse.ema_zone_microstructure_confirmation.continuous_engine import (
        _dist_outside,
        _primary_zone_key,
        _zones_at_sample,
        process_symbol_stream,
    )
    from orderbook_analyse.ema_zone_microstructure_confirmation.continuous_defaults import (
        COOLDOWN_S,
        MAX_WATCH_DURATION_S,
        REARM_LEAVE_HALFWIDTH_MULT,
    )
    from orderbook_analyse.ema_zone_microstructure_confirmation.coverage import probe_symbol_coverage
    from orderbook_analyse.ema_zone_microstructure_confirmation.proximity import (
        PROXIMITY_WATCH_MAX_PCT,
        candle_ohlc_intersects_zone,
        classify_zone_approach_from_candle_ohlc,
    )
    from orderbook_analyse.ema_zone_microstructure_confirmation.regime import (
        flat_diagnostics,
        prepare_bars_with_ema200,
        regime_snapshot,
    )
    from orderbook_analyse.ema_zone_microstructure_confirmation.zones_ext import build_zones
    from orderbook_analyse.l2_wall_attack_discovery.models import tick_size
    from orderbook_analyse.oi_liq_impact_l2.aggregate_proxy.loaders import load_candles_1m
    from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client, load_clickhouse_settings

    load_clickhouse_settings()
    client = get_clickhouse_client()
    raw_root = Path("/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_ob200_v3_raw")

    # --- Recompute engine on effective window (read-only) ---
    clamped_cov = dict(probe_symbol_coverage(symbol="XRPUSDT", raw_root=raw_root, computation_mode="ema_only"))
    clamped_cov["discovery_start"] = manifest["effective_start"]
    clamped_cov["discovery_end"] = manifest["effective_end"]
    recomputed = run_symbol(
        symbol="XRPUSDT",
        raw_root=raw_root,
        coverage=clamped_cov,
        computation_mode="ema_only",
    )
    bundles = recomputed["bundles"]
    recomputed_setups = bundles["ema_setup_events"]

    # --- Instrumented funnel pass ---
    candle_start = eff_start - timedelta(hours=240)
    candles = load_candles_1m(client, symbol="XRPUSDT", start=candle_start, end=eff_end + timedelta(hours=4))
    bars = prepare_bars_with_ema200(candles)
    samples = candle_analysis_samples(candles_1m=candles, bars_5m=bars, start=eff_start, end=eff_end)
    start_ms = int(eff_start.timestamp() * 1000)
    end_ms = int(eff_end.timestamp() * 1000)
    tick = tick_size("XRPUSDT")

    genuine = [
        s
        for s in samples
        if s.genuine
        and not s.carried_forward
        and not s.warmup
        and start_ms <= s.ts_ms < end_ms
        and s.bid_levels >= 200
        and s.ask_levels >= 200
    ]

    funnel = Funnel()
    funnel.closed_1m = len(genuine)

    cooldown_until: dict[str, int] = {}
    last_outside: dict[str, bool] = defaultdict(lambda: True)
    watches: dict[str, dict] = {}
    active: set[str] = set()

    regime_cache: dict[int, dict] = {}

    def regime_at(ts_ms: int) -> dict:
        bar_end_ms = (ts_ms // 300_000) * 300_000
        if bar_end_ms not in regime_cache:
            asof = datetime.fromtimestamp(bar_end_ms / 1000.0, tz=timezone.utc)
            regime_cache[bar_end_ms] = regime_snapshot(bars, asof)
        return regime_cache[bar_end_ms]

    # Geometric control counts (independent)
    geo = Counter()
    geo_unique_candles: set[int] = set()
    geo_by_band: dict[str, set[int]] = defaultdict(set)

    for s in genuine:
        reg = regime_at(s.ts_ms)
        if reg.get("regime"):
            funnel.regime_ok += 1
        zones = _zones_at_sample(s, bars, tick, ema200=reg.get("ema200"))
        if not any(zones.values()):
            continue
        funnel.ema_basis_ok += 1

        ema200 = reg.get("ema200")
        all_zones = build_zones(ema20=s.ema20, ema59=s.ema59, ema200=ema200, atr=s.atr)
        lo, hi = float(s.candle_low), float(s.candle_high)
        stacked_touch = False
        for name, z in all_zones.items():
            if z is None:
                continue
            if candle_ohlc_intersects_zone(low=lo, high=hi, zone_low=z.low, zone_high=z.high):
                geo[f"{name}_touch"] += 1
                geo_by_band[name].add(s.ts_ms)
                geo_unique_candles.add(s.ts_ms)
                stacked_touch = True
        if stacked_touch and sum(1 for z in all_zones.values() if z) >= 2:
            overlapping = []
            present = [z for z in all_zones.values() if z]
            for i, a in enumerate(present):
                for b in present[i + 1 :]:
                    from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.zones import (
                        zones_overlap,
                    )

                    if zones_overlap(a, b):
                        overlapping.append((a.name, b.name))
            if overlapping:
                geo["stacked_zone_touch"] += 1

        primary = _primary_zone_key(zones, s.mid)
        if primary is None:
            continue
        zkey, zone = primary
        approach_ev = classify_zone_approach_from_candle_ohlc(
            low=lo,
            high=hi,
            close=float(s.mid),
            zone_low=zone.low,
            zone_high=zone.high,
            max_pct=PROXIMITY_WATCH_MAX_PCT,
        )
        inside = zone.low <= float(s.mid) <= zone.high
        dist = _dist_outside(zone, s.mid)

        for k in list(watches.keys()):
            if s.ts_ms - watches[k]["started_ms"] > MAX_WATCH_DURATION_S * 1000:
                del watches[k]

        if not inside and dist >= zone.half_width * REARM_LEAVE_HALFWIDTH_MULT:
            if not last_outside.get(zkey, True):
                funnel.rearm_events += 1
                funnel.per_zone_rearm[zkey] += 1
            last_outside[zkey] = True

        in_proximity = bool(approach_ev["in_proximity"])
        exact_touch = bool(approach_ev["exact_touch"])
        if in_proximity:
            funnel.in_proximity += 1
        if exact_touch:
            funnel.exact_touch_raw += 1
            if candle_ohlc_intersects_zone(low=lo, high=hi, zone_low=zone.low, zone_high=zone.high):
                funnel.ohlc_geometric_touch += 1

        diag_watch = flat_diagnostics(bars, datetime.fromtimestamp(s.ts_ms / 1000, tz=timezone.utc), snap=reg)
        if in_proximity and diag_watch["flat"] and zkey not in active and cooldown_until.get(zkey, 0) <= s.ts_ms:
            funnel.flat_blocked_watch += 1
            cooldown_until[zkey] = s.ts_ms + COOLDOWN_S * 1000
            watches.pop(zkey, None)
            continue

        if in_proximity and not exact_touch and zkey not in watches and zkey not in active:
            if cooldown_until.get(zkey, 0) <= s.ts_ms and last_outside.get(zkey, True):
                funnel.proximity_watches_started += 1
                watches[zkey] = {"started_ms": s.ts_ms, "zkey": zkey}
                funnel.watch_opens.append(
                    {"ts": s.ts_ms, "zkey": zkey, "regime": reg.get("regime"), "mid": s.mid}
                )

        if exact_touch and zkey not in active:
            blocked = False
            if cooldown_until.get(zkey, 0) > s.ts_ms:
                funnel.touch_blocked_cooldown += 1
                blocked = True
            elif not last_outside.get(zkey, True) and zkey not in watches:
                funnel.touch_blocked_no_rearm += 1
                funnel.per_zone_exact_blocked_no_rearm[zkey] += 1
                blocked = True
                funnel.touch_attempts.append(
                    {
                        "ts": s.ts_ms,
                        "zkey": zkey,
                        "reason": "no_rearm",
                        "last_outside": last_outside.get(zkey),
                        "had_watch": zkey in watches,
                        "mid": s.mid,
                        "zone_low": zone.low,
                        "zone_high": zone.high,
                    }
                )
            if blocked:
                continue
            funnel.touch_with_watch_or_rearm += 1
            had_watch = zkey in watches
            watches.pop(zkey, None)
            funnel.exported_exact_touch += 1
            cooldown_until[zkey] = s.ts_ms + COOLDOWN_S * 1000
            last_outside[zkey] = False
            funnel.touch_attempts.append(
                {
                    "ts": s.ts_ms,
                    "zkey": zkey,
                    "reason": "accepted",
                    "had_watch": had_watch,
                    "regime": reg.get("regime"),
                    "flat": flat_diagnostics(
                        bars, datetime.fromtimestamp(s.ts_ms / 1000, tz=timezone.utc), snap=reg
                    )["flat"],
                }
            )

    # 30d hypothetical window (read-only counterfactual)
    hyp_start = _parse_z("2026-07-27T18:45:00Z")
    hyp_end = _parse_z("2026-08-26T18:45:00Z")
    hyp_cov = dict(clamped_cov)
    hyp_cov["discovery_start"] = hyp_start.isoformat().replace("+00:00", "Z")
    hyp_cov["discovery_end"] = hyp_end.isoformat().replace("+00:00", "Z")
    hyp_result = run_symbol(symbol="XRPUSDT", raw_root=raw_root, coverage=hyp_cov, computation_mode="ema_only")
    hyp_setups = hyp_result["bundles"]["ema_setup_events"]

    report = {
        "identified_run": {
            "job_id": JOB_ID,
            "run_id": RUN_ID,
            "request_start": manifest["signal_start"],
            "request_end": manifest["signal_end_exclusive"],
            "effective_start": manifest["effective_start"],
            "effective_end": manifest["effective_end"],
            "computation_mode": manifest["computation_mode"],
            "touch_price_basis": manifest["touch_price_basis"],
            "orderbook_loaded": manifest["orderbook_loaded"],
            "candles_1m_loaded_total": len(candles),
            "bars_5m": len(bars),
            "genuine_samples_effective": len(genuine),
            "ema_setup_events_artifact": len(candidates.get("ema_setup_events") or []),
            "micro_events_artifact": len(candidates.get("microstructure_confirmation_events") or []),
            "coverage_discovery_span": {
                "start": coverage_file.get("discovery_start"),
                "end": coverage_file.get("discovery_end"),
            },
            "coverage_candles_rows": coverage_file["sources"][0]["n_rows"],
            "ema200_warmup_needed_before": coverage_file.get("ema200_warmup_needed_before"),
        },
        "dashboard_vs_engine": {
            "artifact_setups": candidates.get("ema_setup_events"),
            "recomputed_setups_count": len(recomputed_setups),
            "recomputed_matches_artifact": len(recomputed_setups) == len(candidates.get("ema_setup_events") or []),
        },
        "funnel_effective_window": {
            "closed_1m": funnel.closed_1m,
            "ema_basis_ok": funnel.ema_basis_ok,
            "regime_ok": funnel.regime_ok,
            "in_proximity": funnel.in_proximity,
            "proximity_watches_started": funnel.proximity_watches_started,
            "ohlc_geometric_touch_primary_zone": funnel.ohlc_geometric_touch,
            "exact_touch_raw_primary": funnel.exact_touch_raw,
            "touch_accepted": funnel.touch_with_watch_or_rearm,
            "touch_blocked_no_rearm": funnel.touch_blocked_no_rearm,
            "touch_blocked_cooldown": funnel.touch_blocked_cooldown,
            "flat_blocked_watch": funnel.flat_blocked_watch,
            "exported_exact_touch_instrumented": funnel.exported_exact_touch,
            "rearm_events": funnel.rearm_events,
            "per_zone_exact_blocked_no_rearm": dict(funnel.per_zone_exact_blocked_no_rearm),
            "per_zone_rearm": dict(funnel.per_zone_rearm),
        },
        "geometric_control": {
            "counts": dict(geo),
            "unique_touch_candles_1m": len(geo_unique_candles),
            "unique_by_band": {k: len(v) for k, v in geo_by_band.items()},
        },
        "hypothetical_30d_window": {
            "start": hyp_start.isoformat().replace("+00:00", "Z"),
            "end": hyp_end.isoformat().replace("+00:00", "Z"),
            "ema_setup_events": len(hyp_setups),
            "exact_touch_events": sum(1 for e in hyp_setups if e.get("zone_event") == "exact_touch"),
        },
        "watch_rearm_sample": {
            "first_10_watch_opens": funnel.watch_opens[:10],
            "first_accepted_touch": next((t for t in funnel.touch_attempts if t["reason"] == "accepted"), None),
            "blocked_touch_sample": [t for t in funnel.touch_attempts if t["reason"] == "no_rearm"][:5],
            "total_blocked_no_rearm": funnel.touch_blocked_no_rearm,
        },
    }
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
