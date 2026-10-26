#!/usr/bin/env python3
"""BT-D40 read-only upper-pool landscape quality review."""

from __future__ import annotations

import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
D39 = ROOT / "results/phase_bt_d39_dynamic_upper_pool_forensics_v1"
RUN_DIR = ROOT / "results/setup_scanner_runs/bt_d26_dynamic_referenz_sep11_24h_v1"
OUT = ROOT / "results/phase_bt_d40_upper_pool_quality_v1"

for p in (
    ROOT / "tools/phase_bt_d26_dynamic_upper_pool_selector_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1",
    ROOT / "tools/phase_bt_d8_canonical_density_cache_v1",
    ROOT / "tools/phase_bt_d23_pool_first_setup_scanner_v1",
    ROOT / "tools/phase4d_pool_landscape_backtester_v2",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from config_yaml_v1 import load_yaml_and_validate  # noqa: E402
from density_bridge_v1 import create_density_session  # noqa: E402
from entry_price_v1 import compute_short_entry_level  # noqa: E402
from pool_first_signal_v1 import _structural_eligible_upper  # noqa: E402
from price_feed_v1 import load_prices_from_ssot_window  # noqa: E402
from setup_scanner_v1 import _bars_in_range, measure_upper_gap_pct  # noqa: E402
from signal_types_v1 import pool_key  # noqa: E402
from strategy_rules import short_sl  # noqa: E402

ORIGIN_TS_RE = re.compile(r":upper:(\d+)$")


def _origin_count(ids: str | None) -> int:
    if not ids:
        return 0
    return len([x for x in str(ids).split("|") if x.strip()])


def _earliest_origin_unix(ids: str | None) -> int | None:
    if not ids:
        return None
    ts = []
    for part in str(ids).split("|"):
        m = ORIGIN_TS_RE.search(part.strip())
        if m:
            ts.append(int(m.group(1)))
    return min(ts) if ts else None


def _intervals_overlap(lo1: float, hi1: float, lo2: float, hi2: float) -> bool:
    return lo1 < hi2 and lo2 < hi1


def _cluster_ids(bands: list[dict[str, Any]]) -> dict[str, int]:
    """Union overlapping price intervals among snapshot upper bands."""
    n = len(bands)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    for i in range(n):
        for j in range(i + 1, n):
            if _intervals_overlap(
                bands[i]["pool_lower"],
                bands[i]["pool_upper"],
                bands[j]["pool_lower"],
                bands[j]["pool_upper"],
            ):
                union(i, j)
    out: dict[str, int] = {}
    for i, b in enumerate(bands):
        out[b["pool_key"]] = find(i)
    return out


def analyze_bar(
    config: dict,
    *,
    bar: str,
    snap: dict,
    candle: dict,
    pool_age: dict[str, int],
    first_seen: dict[str, str],
    prev_bounds: dict[str, tuple[float, float]],
    bt26_pk: str,
    selection_reason: str | None,
) -> list[dict[str, Any]]:
    uppers = snap.get("upper_bands") or []
    close_px = float(candle["c"])
    min_gap = float(config["short_entry"]["upper_gap_min_pct"])
    sl_pct = float(config["risk"]["sl_above_upper_top_pct"])
    se = config["short_entry"]

    band_rows = []
    for b in uppers:
        lo = float(b["lower_price"])
        hi = float(b["upper_price"])
        pk = pool_key(lo, hi)
        band_rows.append(
            {
                "pool_lower": lo,
                "pool_upper": hi,
                "pool_key": pk,
                "origin_set_key": b.get("origin_set_key") or b.get("origin_ids"),
                "upper_origin_ids": b.get("origin_ids"),
                "active_origin_count": _origin_count(b.get("origin_ids")),
            }
        )
    clusters = _cluster_ids(band_rows)
    eligible_rank = 0
    rows: list[dict[str, Any]] = []

    for b in uppers:
        lo = float(b["lower_price"])
        hi = float(b["upper_price"])
        pk = pool_key(lo, hi)
        ok, fail = _structural_eligible_upper(
            config,
            band=b,
            decision_close_utc=bar,
            candle=candle,
            snapshot=snap,
            pool_age_bars=pool_age,
            require_large_upper_gap=True,
        )
        overlap_n = sum(
            1
            for o in band_rows
            if o["pool_key"] != pk
            and _intervals_overlap(lo, hi, o["pool_lower"], o["pool_upper"])
        )
        prev = prev_bounds.get(pk)
        bounds_changed = prev is not None and (prev[0] != lo or prev[1] != hi)
        try:
            entry = compute_short_entry_level(lo, hi, se)
            sl = short_sl(hi, sl_pct)
        except ValueError:
            entry = None
            sl = None

        rank = None
        if ok:
            eligible_rank += 1
            rank = eligible_rank  # provisional; re-sort below

        rows.append(
            {
                "pool_lower": lo,
                "pool_upper": hi,
                "pool_key": pk,
                "structurally_eligible": ok,
                "exclusion_reason": fail or "",
                "bt26_selected": pk == bt26_pk,
                "origin_set_key": b.get("origin_set_key") or b.get("origin_ids"),
                "upper_origin_ids": b.get("origin_ids"),
                "active_origin_count": _origin_count(b.get("origin_ids")),
                "earliest_origin_unix": _earliest_origin_unix(b.get("origin_ids")),
                "first_seen_decision_utc": first_seen.get(pk, ""),
                "pool_age_bars": pool_age.get(pk, 0),
                "bounds_changed_vs_prev_close": bounds_changed,
                "prev_pool_lower": prev[0] if prev else "",
                "prev_pool_upper": prev[1] if prev else "",
                "distance_close_to_pool_lower_pct": (lo - close_px) / close_px * 100.0,
                "upper_gap_pct": measure_upper_gap_pct(uppers, hi, min_gap),
                "overlap_other_upper_count": overlap_n,
                "price_cluster_id": clusters.get(pk, -1),
                "planned_entry_if_band": entry,
                "planned_sl_if_band": sl,
            }
        )

    elig = [r for r in rows if r["structurally_eligible"]]
    elig.sort(key=lambda r: (r["pool_lower"], r["pool_upper"]))
    rank_map = {r["pool_key"]: i + 1 for i, r in enumerate(elig)}
    for r in rows:
        r["eligible_rank_by_pool_lower"] = rank_map.get(r["pool_key"])
    return rows


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    manual = list(csv.DictReader((D39 / "manual_review_10.csv").open(encoding="utf-8")))
    transitions = list(csv.DictReader((D39 / "transitions.csv").open(encoding="utf-8")))
    setups_doc = json.loads((RUN_DIR / "setups.json").read_text(encoding="utf-8"))
    setups = setups_doc["setups"]

    cfg_path = Path((RUN_DIR / "config_path.txt").read_text().strip())
    config = load_yaml_and_validate(cfg_path)
    start = config["backtest"]["start_utc"]
    end = config["backtest"]["end_utc"]
    end_excl = bool(config["backtest"].get("end_utc_exclusive"))
    prices = load_prices_from_ssot_window(start, end, end_utc_exclusive=end_excl)
    bars = _bars_in_range(prices, start, end, end_exclusive=end_excl)
    density = create_density_session(config)

    pool_age: dict[str, int] = {}
    first_seen: dict[str, str] = {}
    snap_cache: dict[str, dict] = {}
    pool_age_at: dict[str, dict[str, int]] = {}
    prev_bounds_at: dict[str, dict[str, tuple[float, float]]] = {}
    last_bounds: dict[str, tuple[float, float]] = {}

    target_bars = {m["decision_utc"] for m in manual}
    target_bars.add("2026-09-11T15:35:00Z")

    for bar in bars:
        try:
            snap = density.collect_at_close(bar)
        except KeyError:
            continue
        prev_bounds_at[bar] = dict(last_bounds)
        for b in snap.get("upper_bands") or []:
            lo = float(b["lower_price"])
            hi = float(b["upper_price"])
            pk = pool_key(lo, hi)
            if pk not in first_seen:
                first_seen[pk] = bar
            pool_age[pk] = pool_age.get(pk, 0) + 1
        pool_age_at[bar] = dict(pool_age)
        if bar in target_bars:
            snap_cache[bar] = snap
        last_bounds = {
            pool_key(float(b["lower_price"]), float(b["upper_price"])): (
                float(b["lower_price"]),
                float(b["upper_price"]),
            )
            for b in snap.get("upper_bands") or []
        }

    ref_cases: list[dict[str, Any]] = []
    landscape_rows: list[dict[str, Any]] = []
    ref1535_rows: list[dict[str, Any]] = []

    for m in manual:
        ep = int(m["episode_index"])
        bar = m["decision_utc"]
        setup = setups[ep - 1]
        bt26_pk = pool_key(float(setup["pool_lower"]), float(setup["pool_upper"]))
        snap = snap_cache[bar]
        candle = prices[bar]
        pa = pool_age_at[bar]
        fs = first_seen
        pb_prev = prev_bounds_at.get(bar, {})

        rows = analyze_bar(
            config,
            bar=bar,
            snap=snap,
            candle=candle,
            pool_age=pa,
            first_seen=fs,
            prev_bounds=pb_prev,
            bt26_pk=bt26_pk,
            selection_reason=setup.get("selection_reason"),
        )
        elig = [r for r in rows if r["structurally_eligible"]]
        ref_cases.append(
            {
                "episode_index": ep,
                "decision_utc": bar,
                "close_price": float(candle["c"]),
                "bt26_pool_lower": setup["pool_lower"],
                "bt26_pool_upper": setup["pool_upper"],
                "selection_reason": setup.get("selection_reason"),
                "snapshot_upper_band_count": len(rows),
                "structurally_eligible_count": len(elig),
                "ineligible_count": len(rows) - len(elig),
                "bt26_eligible_rank": next(
                    (r["eligible_rank_by_pool_lower"] for r in rows if r["bt26_selected"]), None
                ),
            }
        )
        for r in rows:
            landscape_rows.append(
                {
                    "reference_episode_index": ep,
                    "decision_utc": bar,
                    **r,
                }
            )

    # 15:35 full export
    bar = "2026-09-11T15:35:00Z"
    setup = next(s for s in setups if s["decision_utc"] == bar)
    bt26_pk = pool_key(float(setup["pool_lower"]), float(setup["pool_upper"]))
    snap = snap_cache[bar]
    candle = prices[bar]
    pa = pool_age_at[bar]
    fs = first_seen
    pb_prev = prev_bounds_at.get(bar, {})

    rows1535 = analyze_bar(
        config,
        bar=bar,
        snap=snap,
        candle=candle,
        pool_age=pa,
        first_seen=fs,
        prev_bounds=pb_prev,
        bt26_pk=bt26_pk,
        selection_reason=setup.get("selection_reason"),
    )
    elig1535 = [r for r in rows1535 if r["structurally_eligible"]]
    for r in rows1535:
        ref1535_rows.append({"decision_utc": bar, **r})

    # bound drift 12
    drift_rows = []
    for t in transitions:
        if t.get("forensic_class") != "3_POOL_BOUND_DRIFT_OR_SPLIT_MERGE":
            continue
        ep = int(t["episode_index"])
        s = setups[ep - 1]
        prev = s.get("previous_candidate") or {}
        sl_pct = float(config["risk"]["sl_above_upper_top_pct"])
        prev_hi = float(prev["pool_upper"]) if prev.get("pool_upper") is not None else None
        sl_prev = short_sl(prev_hi, sl_pct) if prev_hi is not None else None
        drift_rows.append(
            {
                "episode_index": ep,
                "decision_utc": s["decision_utc"],
                "pool_lower": s["pool_lower"],
                "prev_pool_upper": prev.get("pool_upper"),
                "cur_pool_upper": s["pool_upper"],
                "prev_origin_set_key": prev.get("origin_set_key"),
                "cur_origin_set_key": s.get("origin_set_key"),
                "origin_set_changed": prev.get("origin_set_key") != s.get("origin_set_key"),
                "prev_pool_key": prev.get("pool_key"),
                "cur_pool_key": s.get("upper_entry_pool_id"),
                "same_price_zone_lower": True,
                "planned_sl_from_prev_upper": sl_prev,
                "planned_sl_cur": s["planned_sl"],
                "sl_delta_abs": (
                    float(s["planned_sl"]) - float(sl_prev) if sl_prev is not None else ""
                ),
                "selection_reason": s.get("selection_reason"),
                "diagnosis": (
                    "Same pool_lower; upper bound / origin membership shifted in density reconcile — "
                    "new pool_key; SL recomputed from new upper top."
                ),
            }
        )

    def write_csv(path: Path, rows: list[dict]) -> None:
        if not rows:
            path.write_text("", encoding="utf-8")
            return
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    write_csv(OUT / "reference_cases.csv", ref_cases)
    write_csv(OUT / "eligible_upper_landscapes.csv", landscape_rows)
    write_csv(OUT / "reference_1535_all_uppers.csv", ref1535_rows)
    write_csv(OUT / "bound_drift_12.csv", drift_rows)

    # structural observations
    excl_1535 = defaultdict(int)
    for r in rows1535:
        if not r["structurally_eligible"] and r["exclusion_reason"]:
            excl_1535[r["exclusion_reason"]] += 1
    oldest_elig = sorted(
        elig1535, key=lambda r: (-r["pool_age_bars"], r["pool_lower"])
    )[:5]
    densest = sorted(elig1535, key=lambda r: (-r["active_origin_count"], r["pool_lower"]))[:5]

    obs = f"""# BT-D40 structural observations (auto)

## BT-D26 selection rule (unchanged)
Lowest `pool_lower` among structurally eligible uppers at decision close (`_structural_eligible_upper`).
No use of pool age, origin count, or cluster id in the selector.

## Reference 2026-09-11 15:35 UTC
- Close: {float(candle['c'])}
- Snapshot upper bands: {len(rows1535)} total; **{len(elig1535)}** structurally eligible
- BT-D26 selected rank **1** by `pool_lower` (0.08681–0.086838)
- Next eligible band above: {elig1535[1]['pool_lower'] if len(elig1535) > 1 else 'n/a'} (Δlower ≈ {(elig1535[1]['pool_lower']-elig1535[0]['pool_lower']) if len(elig1535)>1 else 0})

### Ineligible exclusion counts (15:35)
{json.dumps(dict(excl_1535), indent=2)}

### Eligible pools with highest persistence (pool_age_bars)
{json.dumps([{k: oldest_elig[i].get(k) for k in ('pool_lower','pool_upper','pool_age_bars','active_origin_count','eligible_rank_by_pool_lower')} for i in range(len(oldest_elig))], indent=2)}

### Eligible pools with most origins (not used by BT-D26)
{json.dumps([{k: densest[i].get(k) for k in ('pool_lower','pool_upper','pool_age_bars','active_origin_count','eligible_rank_by_pool_lower')} for i in range(len(densest))], indent=2)}

## Bound drift (12 episodes)
Same `pool_lower`, changed `pool_upper` and/or `origin_set_key`; SL moves with `short_sl(upper_top)`.
See `bound_drift_12.csv`.

## Ten reference cases
See `reference_cases.csv` and full band tables in `eligible_upper_landscapes.csv` (all snapshot uppers, not top-3).
"""
    (OUT / "structural_observations.md").write_text(obs, encoding="utf-8")
    print(
        json.dumps(
            {
                "ref_cases": len(ref_cases),
                "landscape_rows": len(landscape_rows),
                "ref1535_eligible": len(elig1535),
                "bound_drift": len(drift_rows),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
