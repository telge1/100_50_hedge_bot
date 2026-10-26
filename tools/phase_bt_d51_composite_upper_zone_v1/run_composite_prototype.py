#!/usr/bin/env python3
"""BT-D51 read-only composite upper price zone prototype."""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results/phase_bt_d51_composite_upper_zone_v1"
D39 = ROOT / "results/phase_bt_d39_dynamic_upper_pool_forensics_v1"
RUN_DIR = ROOT / "results/setup_scanner_runs/bt_d26_dynamic_referenz_sep11_24h_v1"

MANUAL_LO = 0.08410
MANUAL_HI = 0.08436

MANUAL_BARS = [
    "2026-09-11T12:05:00Z",
    "2026-09-11T12:25:00Z",
    "2026-09-11T12:30:00Z",
    "2026-09-11T12:35:00Z",
]

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
from gap_detection_v1 import has_upper_large_gap_above_pool  # noqa: E402
from pool_first_signal_v1 import _structural_eligible_upper  # noqa: E402
from price_feed_v1 import load_prices_from_ssot_window  # noqa: E402
from setup_scanner_v1 import _bars_in_range, measure_upper_gap_pct  # noqa: E402
from signal_types_v1 import pool_key  # noqa: E402
from strategy_rules import select_short_tp_depth2, short_sl  # noqa: E402
from setup_scanner_v1 import snapshot_to_intervals  # noqa: E402


def _intervals_overlap(lo1: float, hi1: float, lo2: float, hi2: float) -> bool:
    return lo1 < hi2 and lo2 < hi1


def _overlaps_manual(lo: float, hi: float) -> bool:
    return lo < MANUAL_HI and hi > MANUAL_LO


def _union_find_components(bands: list[dict[str, Any]]) -> dict[int, list[int]]:
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

    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return dict(groups)


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not intervals:
        return []
    iv = sorted(intervals)
    merged = [iv[0]]
    for lo, hi in iv[1:]:
        mlo, mhi = merged[-1]
        if lo <= mhi:
            merged[-1] = (mlo, max(mhi, hi))
        else:
            merged.append((lo, hi))
    return merged


def _internal_gaps(members: list[dict[str, Any]], comp_lo: float, comp_hi: float) -> list[dict[str, float]]:
    covered = _merge_intervals([(m["pool_lower"], m["pool_upper"]) for m in members])
    gaps: list[dict[str, float]] = []
    cursor = comp_lo
    for lo, hi in covered:
        if lo > cursor + 1e-15:
            gaps.append({"gap_lower": cursor, "gap_upper": lo, "width": lo - cursor})
        cursor = max(cursor, hi)
    if cursor < comp_hi - 1e-15:
        gaps.append({"gap_lower": cursor, "gap_upper": comp_hi, "width": comp_hi - cursor})
    return gaps


def _overlap_depth_counts(members: list[dict[str, Any]]) -> Counter[int]:
    c: Counter[int] = Counter()
    for m in members:
        n = sum(
            1
            for o in members
            if o["pool_key"] != m["pool_key"]
            and _intervals_overlap(
                m["pool_lower"], m["pool_upper"], o["pool_lower"], o["pool_upper"]
            )
        )
        c[n] += 1
    return c


def _articulation_pool_keys(members: list[dict[str, Any]]) -> list[str]:
    """Bands whose removal disconnects the member-only overlap graph."""
    if len(members) <= 2:
        return []

    keys = [m["pool_key"] for m in members]
    idx = {m["pool_key"]: i for i, m in enumerate(members)}

    def connected_without(skip_pk: str) -> bool:
        remain = [m for m in members if m["pool_key"] != skip_pk]
        if len(remain) <= 1:
            return True
        sub = _union_find_components(remain)
        return len(sub) == 1

    arts = []
    for m in members:
        if not connected_without(m["pool_key"]):
            arts.append(m["pool_key"])
    return arts


def _composite_id(bar: str, comp_lo: float, comp_hi: float, pool_keys: list[str]) -> str:
    raw = f"{bar}|{comp_lo:.17g}|{comp_hi:.17g}|" + "|".join(sorted(pool_keys))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def analyze_snapshot(
    bar: str,
    *,
    config: dict,
    snap: dict,
    candle: dict,
    pool_age: dict[str, int],
    bt26_pool: tuple[float, float] | None,
) -> dict[str, Any]:
    close_px = float(candle["c"])
    min_gap = float(config["short_entry"]["upper_gap_min_pct"])
    sl_pct = float(config["risk"]["sl_above_upper_top_pct"])
    uppers = snap.get("upper_bands") or []

    bands: list[dict[str, Any]] = []
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
        bands.append(
            {
                "pool_lower": lo,
                "pool_upper": hi,
                "pool_key": pk,
                "origin_ids": b.get("origin_ids") or "",
                "structurally_eligible": ok,
                "exclusion_reason": fail or "",
                "pool_age_bars": pool_age.get(pk, 0),
            }
        )

    groups = _union_find_components(bands)
    components: list[dict[str, Any]] = []

    for _root, indices in groups.items():
        members = [bands[i] for i in indices]
        comp_lo = min(m["pool_lower"] for m in members)
        comp_hi = max(m["pool_upper"] for m in members)
        pks = sorted(m["pool_key"] for m in members)
        origins: set[str] = set()
        for m in members:
            for oid in str(m["origin_ids"]).split("|"):
                if oid.strip():
                    origins.add(oid.strip())
        elig_members = [m for m in members if m["structurally_eligible"]]
        depth_hist = {str(k): v for k, v in sorted(_overlap_depth_counts(members).items())}
        gaps = _internal_gaps(members, comp_lo, comp_hi)
        arts = _articulation_pool_keys(members)

        overlaps_manual = any(_overlaps_manual(m["pool_lower"], m["pool_upper"]) for m in members)
        manual_subset = overlaps_manual and comp_lo >= MANUAL_LO - 1e-12 and comp_hi <= MANUAL_HI + 1e-12

        # Prototype composite eligibility (documented; not canonical strategy)
        comp_above_price = comp_lo > close_px
        has_eligible_member = len(elig_members) > 0
        gap_atomic_style = has_upper_large_gap_above_pool(uppers, comp_hi, min_gap)
        gap_measured = measure_upper_gap_pct(uppers, comp_hi, min_gap)

        lowest_elig_in_comp = None
        if elig_members:
            lowest_elig_in_comp = min(elig_members, key=lambda m: (m["pool_lower"], m["pool_upper"]))

        cid = _composite_id(bar, comp_lo, comp_hi, pks)
        components.append(
            {
                "composite_id": cid,
                "composite_lower": comp_lo,
                "composite_upper": comp_hi,
                "composite_width": comp_hi - comp_lo,
                "band_count": len(members),
                "pool_keys": pks,
                "unique_active_origin_count": len(origins),
                "unique_active_origin_ids": sorted(origins),
                "overlap_depth_histogram": depth_hist,
                "internal_gap_count": len(gaps),
                "internal_gaps": gaps[:20],
                "articulation_pool_keys": arts,
                "min_member_age_bars": min(m["pool_age_bars"] for m in members),
                "max_member_age_bars": max(m["pool_age_bars"] for m in members),
                "overlaps_manual_08410_08436": overlaps_manual,
                "manual_reference_is_exact_subset": manual_subset,
                "prototype_comp_above_price": comp_above_price,
                "prototype_has_eligible_member": has_eligible_member,
                "prototype_gap_pass_envelope_top": gap_atomic_style,
                "prototype_upper_gap_pct_envelope": gap_measured,
                "prototype_lowest_eligible_member": lowest_elig_in_comp,
                "classification": _classify_component(members, gaps, arts),
            }
        )

    components.sort(key=lambda c: (c["composite_lower"], c["composite_upper"]))

    elig_atomic = sorted([b for b in bands if b["structurally_eligible"]], key=lambda b: (b["pool_lower"], b["pool_upper"]))
    atomic_rank1 = elig_atomic[0] if elig_atomic else None

    # Nearest composite above price with >=1 eligible member (prototype D)
    candidates = [
        c
        for c in components
        if c["prototype_comp_above_price"] and c["prototype_has_eligible_member"]
    ]
    candidates.sort(key=lambda c: c["composite_lower"])
    nearest_comp = candidates[0] if candidates else None

    manual_comp = next((c for c in components if c["overlaps_manual_08410_08436"]), None)
    manual_overlap_members = (
        [m for m in bands if _overlaps_manual(m["pool_lower"], m["pool_upper"])] if manual_comp else []
    )

    intervals = snapshot_to_intervals(snap)
    min_tp = float(config["take_profit"]["min_distance_pct"])

    def hypo_levels(comp: dict[str, Any] | None) -> dict[str, Any]:
        if not comp:
            return {}
        lo, hi = comp["composite_lower"], comp["composite_upper"]
        entry = lo
        sl = short_sl(hi, sl_pct)
        tp_iv, tp_reason = select_short_tp_depth2(entry, intervals, min_tp)
        tp = float(tp_iv["top"]) if tp_iv else None
        return {
            "hypo_entry_composite_lower": entry,
            "hypo_sl_composite_upper_x_multiplier": sl,
            "hypo_tp_unchanged_rule": tp,
            "tp_reason": tp_reason,
        }

    bt26_pk = pool_key(*bt26_pool) if bt26_pool else None

    return {
        "decision_close_utc": bar,
        "close_price": close_px,
        "snapshot_upper_band_count": len(bands),
        "overlap_component_count": len(components),
        "atomic_rank1_eligible": atomic_rank1,
        "bt26_selected_pool_key": bt26_pk,
        "nearest_prototype_eligible_composite": nearest_comp,
        "manual_zone_composite_component": manual_comp,
        "manual_zone_atomic_overlap_count": len(manual_overlap_members),
        "manual_zone_eligible_atomic_overlap_count": sum(
            1 for m in manual_overlap_members if m["structurally_eligible"]
        ),
        "hypo_manual_user_reference": {
            "entry": MANUAL_LO,
            "sl": MANUAL_HI * (1.0 + sl_pct),
            **{
                k: v
                for k, v in hypo_levels(
                    {
                        "composite_lower": MANUAL_LO,
                        "composite_upper": MANUAL_HI,
                    }
                ).items()
            },
        },
        "hypo_nearest_composite": hypo_levels(nearest_comp),
        "hypo_manual_component_envelope": hypo_levels(manual_comp),
        "components": components,
    }


def _classify_component(
    members: list[dict[str, Any]], gaps: list[dict[str, float]], articulation: list[str]
) -> str:
    if len(members) == 1:
        return "single_atomic_band"
    if articulation:
        return "overlap_component_with_articulation_bridges"
    if gaps:
        return "overlap_component_with_internal_coverage_gaps"
    return "true_overlapping_composite"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    manual10 = list(csv.DictReader((D39 / "manual_review_10.csv").open(encoding="utf-8")))
    setups_doc = json.loads((RUN_DIR / "setups.json").read_text(encoding="utf-8"))
    setups_by_ep = {int(s.get("episode_index", i + 1)): s for i, s in enumerate(setups_doc["setups"])}

    cfg_path = Path((RUN_DIR / "config_path.txt").read_text().strip())
    config = load_yaml_and_validate(cfg_path)
    start = config["backtest"]["start_utc"]
    end = config["backtest"]["end_utc"]
    end_excl = bool(config["backtest"].get("end_utc_exclusive"))
    prices = load_prices_from_ssot_window(start, end, end_utc_exclusive=end_excl)
    bars = _bars_in_range(prices, start, end, end_exclusive=end_excl)
    density = create_density_session(config)

    target_bars: dict[str, tuple[float, float] | None] = {}
    for b in MANUAL_BARS:
        target_bars[b] = None
    for row in manual10:
        bar = row["decision_utc"]
        ep = int(row["episode_index"])
        setup = setups_by_ep.get(ep) or setups_doc["setups"][ep - 1]
        target_bars[bar] = (float(setup["pool_lower"]), float(setup["pool_upper"]))

    pool_age: dict[str, int] = {}
    snap_cache: dict[str, dict] = {}
    pool_age_at: dict[str, dict[str, int]] = {}

    for bar in bars:
        try:
            snap = density.collect_at_close(bar)
        except KeyError:
            continue
        for b in snap.get("upper_bands") or []:
            pk = pool_key(float(b["lower_price"]), float(b["upper_price"]))
            pool_age[pk] = pool_age.get(pk, 0) + 1
        pool_age_at[bar] = dict(pool_age)
        if bar in target_bars:
            snap_cache[bar] = snap

    analyses: dict[str, Any] = {}
    for bar, bt26 in sorted(target_bars.items()):
        if bar not in snap_cache:
            continue
        analyses[bar] = analyze_snapshot(
            bar,
            config=config,
            snap=snap_cache[bar],
            candle=prices[bar],
            pool_age=pool_age_at[bar],
            bt26_pool=bt26,
        )

    # composite_zones.csv — flat rows
    zone_rows: list[dict[str, Any]] = []
    for bar, doc in analyses.items():
        for c in doc["components"]:
            zone_rows.append(
                {
                    "decision_close_utc": bar,
                    "close_price": doc["close_price"],
                    **{k: v for k, v in c.items() if k not in ("pool_keys", "unique_active_origin_ids", "internal_gaps", "prototype_lowest_eligible_member")},
                    "pool_keys_pipe": "|".join(c["pool_keys"]),
                    "origin_ids_pipe": "|".join(c["unique_active_origin_ids"][:50])
                    + ("|…" if len(c["unique_active_origin_ids"]) > 50 else ""),
                }
            )

    with (OUT / "composite_zones.csv").open("w", encoding="utf-8", newline="") as fh:
        if zone_rows:
            w = csv.DictWriter(fh, fieldnames=list(zone_rows[0].keys()), extrasaction="ignore")
            w.writeheader()
            w.writerows(zone_rows)

    memberships = {
        "contract_version": "bt_d51_composite_upper_zone_v1",
        "overlap_rule": "strict_price_overlap lo1<hi2 and lo2<hi1 among all snapshot upper_bands",
        "analyses": {bar: {"components": doc["components"]} for bar, doc in analyses.items()},
    }
    (OUT / "composite_memberships.json").write_text(json.dumps(memberships, indent=2), encoding="utf-8")

    # manual_reference_timeline.csv
    tl_rows = []
    for bar in MANUAL_BARS:
        if bar not in analyses:
            continue
        d = analyses[bar]
        mc = d.get("manual_zone_composite_component")
        nc = d.get("nearest_prototype_eligible_composite")
        a1 = d.get("atomic_rank1_eligible")
        tl_rows.append(
            {
                "decision_close_utc": bar,
                "close_price": d["close_price"],
                "user_manual_lower": MANUAL_LO,
                "user_manual_upper": MANUAL_HI,
                "manual_overlap_atomic_count": d["manual_zone_atomic_overlap_count"],
                "manual_overlap_eligible_atomic_count": d["manual_zone_eligible_atomic_overlap_count"],
                "manual_zone_full_composite_lower": mc["composite_lower"] if mc else "",
                "manual_zone_full_composite_upper": mc["composite_upper"] if mc else "",
                "manual_zone_composite_band_count": mc["band_count"] if mc else "",
                "manual_is_subset_of_composite": mc["manual_reference_is_exact_subset"] if mc else "",
                "atomic_rank1_lower": a1["pool_lower"] if a1 else "",
                "atomic_rank1_upper": a1["pool_upper"] if a1 else "",
                "nearest_composite_lower": nc["composite_lower"] if nc else "",
                "nearest_composite_upper": nc["composite_upper"] if nc else "",
                "nearest_composite_band_count": nc["band_count"] if nc else "",
                "hypo_entry_nearest_composite": d["hypo_nearest_composite"].get("hypo_entry_composite_lower"),
                "hypo_sl_nearest_composite": d["hypo_nearest_composite"].get("hypo_sl_composite_upper_x_multiplier"),
            }
        )
    with (OUT / "manual_reference_timeline.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(tl_rows[0].keys()))
        w.writeheader()
        w.writerows(tl_rows)

    # atomic_vs_composite.csv for all target bars (10 controls + manual)
    avc_rows = []
    for bar, d in sorted(analyses.items()):
        a1 = d.get("atomic_rank1_eligible")
        nc = d.get("nearest_prototype_eligible_composite")
        mc = d.get("manual_zone_composite_component")
        avc_rows.append(
            {
                "decision_close_utc": bar,
                "close_price": d["close_price"],
                "atomic_rank1_pool_key": a1["pool_key"] if a1 else "",
                "atomic_rank1_lower": a1["pool_lower"] if a1 else "",
                "atomic_rank1_upper": a1["pool_upper"] if a1 else "",
                "atomic_rank1_origins": a1["origin_ids"] if a1 else "",
                "nearest_composite_id": nc["composite_id"] if nc else "",
                "nearest_composite_lower": nc["composite_lower"] if nc else "",
                "nearest_composite_upper": nc["composite_upper"] if nc else "",
                "nearest_composite_width": nc["composite_width"] if nc else "",
                "nearest_composite_band_count": nc["band_count"] if nc else "",
                "nearest_composite_origin_count": nc["unique_active_origin_count"] if nc else "",
                "nearest_composite_classification": nc["classification"] if nc else "",
                "nearest_composite_articulation_count": len(nc["articulation_pool_keys"]) if nc else "",
                "manual_touch_composite_lower": mc["composite_lower"] if mc else "",
                "manual_touch_composite_upper": mc["composite_upper"] if mc else "",
                "manual_touch_composite_band_count": mc["band_count"] if mc else "",
                "delta_nearest_vs_atomic_lower": (
                    (nc["composite_lower"] - a1["pool_lower"]) if nc and a1 else ""
                ),
            }
        )
    with (OUT / "atomic_vs_composite.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(avc_rows[0].keys()))
        w.writeheader()
        w.writerows(avc_rows)

    merge_risks = _write_merge_risks(analyses)
    (OUT / "merge_risks.md").write_text(merge_risks, encoding="utf-8")
    (OUT / "lifecycle_implications.md").write_text(_write_lifecycle(), encoding="utf-8")

    print("Wrote", OUT, "bars", len(analyses))
    return 0


def _write_merge_risks(analyses: dict[str, Any]) -> str:
    lines = ["# BT-D51 merge and bridge risks\n"]
    for bar in MANUAL_BARS:
        if bar not in analyses:
            continue
        d = analyses[bar]
        mc = d.get("manual_zone_composite_component")
        lines.append(f"## {bar} (close {d['close_price']})\n")
        if not mc:
            lines.append("No overlap component intersects user manual 0.08410–0.08436.\n")
            continue
        lines.append(
            f"- Full overlap component touching manual interval: **{mc['composite_lower']} – {mc['composite_upper']}** "
            f"({mc['band_count']} bands, {mc['unique_active_origin_count']} origins).\n"
        )
        lines.append(f"- User box is exact subset of component: **{mc['manual_reference_is_exact_subset']}**\n")
        if mc["articulation_pool_keys"]:
            lines.append(
                f"- Articulation bridges ({len(mc['articulation_pool_keys'])}): "
                f"`{'`, `'.join(mc['articulation_pool_keys'][:8])}`"
                f"{'…' if len(mc['articulation_pool_keys']) > 8 else ''}\n"
            )
        if mc["internal_gap_count"]:
            lines.append(f"- Internal coverage gaps inside envelope: **{mc['internal_gap_count']}**\n")
        # bands below manual in same component?
        below = [pk for pk in mc["pool_keys"] if float(pk.split("|")[0]) < MANUAL_LO - 1e-9]
        if below:
            lines.append(
                f"- **Envelope extension below user lower 0.08410** via overlap chain: "
                f"{len(below)} member band(s) (e.g. includes 0.08408–0.08409 when connected).\n"
            )
    lines.append("\n## Control timestamps (excessive merge watch)\n")
    for bar, d in sorted(analyses.items()):
        if bar in MANUAL_BARS:
            continue
        big = [c for c in d["components"] if c["band_count"] >= 15]
        if big:
            b = max(big, key=lambda c: c["band_count"])
            lines.append(
                f"- {bar}: largest component {b['band_count']} bands, "
                f"{b['composite_lower']}–{b['composite_upper']}, class={b['classification']}\n"
            )
    return "".join(lines)


def _write_lifecycle() -> str:
    return """# BT-D51 composite zone lifecycle (design notes only)

## Identity key (prototype)

Per decision close, a composite is **the connected component** of the strict price-overlap graph among all atomic upper bands. Identity is **not** stable across bars: `composite_id` hashes bar + envelope + member keys.

## Remains active

- At least one member band still appears in the snapshot with the same overlap connectivity to neighbors.
- Prototype trading relevance additionally requires `composite.lower > close` and at least one **atomically** eligible member (eligibility is **not** promoted to the whole envelope).

## Boundary changes

- Envelope lower/upper are **min/max** of member edges; any atomic bound drift changes the composite envelope even if the human label is unchanged.
- Origin set changes on a single member alter `unique_active_origin_ids` without necessarily changing envelope.

## Split / merge

- **Merge:** two composites become one when a new band appears whose interval overlaps both former clusters.
- **Split:** removal or bound shift breaks overlap edges; one physical shelf can become multiple components overnight.

## Loses relevance

- Close rises above `composite.lower` → entire envelope fails above-price for short limit placement (see 12:35 case).
- All eligible members fail gap/TP/age independently → composite may exist visually but has **no** eligible anchor band.

## Replaced by nearer zone

- Atomic BT-D21 rule: global minimum eligible `pool_lower`.
- Prototype composite rule: minimum `composite.lower` among composites with `comp.lower > close` and ≥1 eligible member — can differ when the lowest atomic band sits in a **small** component below a larger manual cluster.

## Pending orders (not implemented)

- Composite identity should be tracked separately from atomic `pool_key` to avoid spurious cancel on micro-bound drift inside the same overlap component.
- Cancellation should require envelope disconnect or loss of all eligible anchor members, not mere rank change among interior bands.

"""


if __name__ == "__main__":
    raise SystemExit(main())
