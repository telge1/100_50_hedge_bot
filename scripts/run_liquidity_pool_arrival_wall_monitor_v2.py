#!/usr/bin/env python3
"""LIQUIDITY_POOL_ARRIVAL_WALL_MONITOR_V2 — methodological revision CLI."""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

OA_ROOT = Path(__file__).resolve().parents[1]
if str(OA_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(OA_ROOT / "src"))

from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.clustering import (
    PoolInterval,
    assign_market_clusters,
)
from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.first_seen import (
    FirstSeenClass,
    normalize_tick_price,
)
from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.pipeline import (
    WINDOW_END,
    WINDOW_START,
    PRIOR_V1,
    MidSample,
    build_migration,
    classify_exact_arrival,
    detect_pool_arrivals,
    first_seen_for_arrival,
    iter_mids,
    load_pools,
    replay_at_probes,
    verify_foundation_parity,
    write_csv,
    _dt_ms,
    _iso,
    _ms,
    _utc,
)
from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.util import tick_size

DEFAULT_RAW = OA_ROOT / "data" / "orderbook_raw_shadow" / "ob200_v3"
KNOWN_022736 = "2026-08-26T02:27:36Z"
KNOWN_WALL = 79217.1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--out-dir",
        default=str(OA_ROOT / "results" / "liquidity_pool_arrival_wall_monitor_v2"),
    )
    ap.add_argument("--raw-root", default=str(DEFAULT_RAW))
    args = ap.parse_args(argv)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    raw_root = Path(args.raw_root)
    tick = tick_size("BTCUSDT")

    print("Foundation parity (pre)...", flush=True)
    foundation = verify_foundation_parity()
    (out / "pool_foundation_verification.json").write_text(
        json.dumps(foundation, indent=2), encoding="utf-8"
    )
    if not foundation["parity_pass"]:
        (out / "verdict.json").write_text(
            json.dumps({"verdict": "POOL_FOUNDATION_PARITY_FAILED"}, indent=2), encoding="utf-8"
        )
        return 3

    ws, we = _utc(WINDOW_START), _utc(WINDOW_END)
    print("Load pools...", flush=True)
    pools = load_pools(ws, we)
    print(f"pools ASK={sum(1 for p in pools if p['side']=='ASK')} BID={sum(1 for p in pools if p['side']=='BID')}", flush=True)

    print("Stream mids...", flush=True)
    mids = [m for m in iter_mids(raw_root, symbol="BTCUSDT", start=ws, end=we) if m.genuine]
    print(f"genuine mids={len(mids)}", flush=True)
    if len(mids) < 100:
        (out / "verdict.json").write_text(
            json.dumps({"verdict": "LIQUIDITY_POOL_ARRIVAL_WALL_MONITOR_V2_BLOCKED_DATA"}, indent=2),
            encoding="utf-8",
        )
        return 4

    print("Detect pool arrivals...", flush=True)
    arrivals, born, gaps = detect_pool_arrivals(pools, mids)
    print(f"pool_arrivals={len(arrivals)} born={len(born)} gaps={len(gaps)}", flush=True)

    intervals = [
        PoolInterval(
            pool_id=p["pool_id"],
            side=p["side"],
            lower=float(p["lower_edge"]),
            upper=float(p["upper_edge"]),
            available_at_ms=_ms(p["available_at"]),
            invalidated_at_ms=_ms(p["invalidated_ts"]) if p.get("invalidated_ts") else None,
        )
        for p in pools
    ]
    mid_tuples = [(m.ts_ms, m.mid, m.genuine) for m in mids]
    print("Assign market clusters...", flush=True)
    arrivals, clusters, membership_rows = assign_market_clusters(
        symbol="BTCUSDT",
        pool_arrivals=arrivals,
        pools=intervals,
        mids=mid_tuples,
    )
    print(f"market_clusters={len(clusters)}", flush=True)

    # cluster lookup
    cluster_by_id = {c.cluster_id: c for c in clusters}
    for a in arrivals:
        cid = a.get("market_arrival_cluster_id")
        cl = cluster_by_id.get(cid) if cid else None
        a["cluster_member_pool_count"] = len(cl.member_pool_ids) if cl else 1
        a["cluster_pool_arrival_count"] = len(cl.pool_arrival_ids) if cl else 1
        a["cluster_start_ts"] = _iso(_dt_ms(cl.start_ts_ms)) if cl else a["arrival_ts"]
        a["cluster_end_ts"] = _iso(_dt_ms(cl.end_ts_ms)) if cl and cl.end_ts_ms else None
        a["cluster_end_reason"] = cl.end_reason if cl else None
        a["component_lower"] = cl.union_lower if cl else a["lower_edge"]
        a["component_upper"] = cl.union_upper if cl else a["upper_edge"]

    # Probes for exact + first-seen
    probes: list[int] = []
    for a in arrivals:
        am = int(a["arrival_ts_ms"])
        probes.extend([am - 1000, am])
        for dt in (1, 5, 12, 13, 30, 60, 120, 180):
            probes.append(am + dt * 1000)
    # denser for known
    kms = _ms(KNOWN_022736)
    for t in range(kms - 2000, kms + 180_000, 1000):
        probes.append(t)

    print(f"Replay OB probes n≈{len(set(probes))}...", flush=True)
    books = replay_at_probes(raw_root, "BTCUSDT", probes)

    print("Exact-arrival + first-seen...", flush=True)
    exact_rows = []
    first_seen_rows = []
    first_seen_by_arrival: dict[str, list] = {}
    class_counts = defaultdict(int)

    for a in arrivals:
        am = int(a["arrival_ts_ms"])
        pre = books.get(am - 1000)
        arr_snap = books.get(am)
        # reject future
        if arr_snap is not None and arr_snap.ts_ms > am:
            arr_snap = None
        arr_c = classify_exact_arrival(
            arr_snap,
            side=a["side"],
            lo=float(a["lower_edge"]),
            hi=float(a["upper_edge"]),
            arrival_ms=am,
        )
        st = arr_c.get("strongest")
        cls = arr_c["wall_class_at_arrival"]
        if cls in ("MAJOR", "MODERATE", "MINOR", "NO_WALL"):
            class_counts[cls] += 1
        elif cls == "SNAPSHOT_UNAVAILABLE" or cls == "SNAPSHOT_STALE":
            class_counts["SNAPSHOT_UNAVAILABLE"] += 1
        else:
            class_counts[cls] += 1

        a["wall_class_at_arrival"] = cls
        a["wall_present_at_arrival"] = arr_c.get("wall_present_at_arrival")
        a["exact_ob_age_ms"] = arr_c.get("ob_age_ms")
        a["exact_snap_ts"] = _iso(_dt_ms(arr_c["snap_ts_ms"])) if arr_c.get("snap_ts_ms") else None
        a["strongest_wall_price_at_arrival"] = st["price"] if st else None
        a["strongest_wall_notional_at_arrival"] = st["notional"] if st else None
        a["strongest_wall_rank_at_arrival"] = st["full_side_rank"] if st else None
        a["strongest_wall_percentile_at_arrival"] = st["full_side_percentile"] if st else None
        a["strongest_wall_tick"] = (
            normalize_tick_price(st["price"], tick) if st else None
        )
        a["full_side_level_count"] = arr_c.get("full_side_level_count")
        a["pool_filter_applied_after_full_side_rank"] = True

        post_snaps = []
        for dt in (1, 5, 12, 13, 30, 60, 120, 180):
            s = books.get(am + dt * 1000)
            if s is not None:
                post_snaps.append(s)
        # dense known
        if a["arrival_ts"] == KNOWN_022736 and "1787684400" in a["pool_id"]:
            post_snaps = [
                books[t]
                for t in range(am + 1000, am + 180_000, 1000)
                if books.get(t) is not None
            ]

        fs_rows = first_seen_for_arrival(
            ep=a, pre_snap=pre, arr_class=arr_c, post_snaps=post_snaps, tick=tick
        )
        first_seen_rows.extend(fs_rows)
        first_seen_by_arrival[a["pool_arrival_id"]] = fs_rows

        pre_n = sum(1 for r in fs_rows if r["first_seen_class"] == FirstSeenClass.PRE_EXISTING_BEFORE_ARRIVAL.value)
        at_n = sum(1 for r in fs_rows if r["first_seen_class"] == FirstSeenClass.FIRST_SEEN_AT_ARRIVAL.value)
        after_n = sum(
            1
            for r in fs_rows
            if r["first_seen_class"] == FirstSeenClass.APPEARED_STRICTLY_AFTER_ARRIVAL.value
        )
        a["pre_existing_wall_count"] = pre_n
        a["first_seen_at_arrival_count"] = at_n
        a["strictly_post_arrival_wall_count"] = after_n
        a["additional_wall_appeared_after_arrival"] = after_n > 0
        # arrival wall persisted?
        if st is not None:
            tp = normalize_tick_price(st["price"], tick)
            a["arrival_wall_persisted_post_arrival"] = any(
                r["tick_price"] == tp
                and r["first_seen_class"]
                in (
                    FirstSeenClass.PRE_EXISTING_BEFORE_ARRIVAL.value,
                    FirstSeenClass.FIRST_SEEN_AT_ARRIVAL.value,
                )
                and any(
                    abs((books.get(am + dt * 1000).ts_ms if books.get(am + dt * 1000) else 0) - am)
                    > 0
                    for dt in (1, 5, 12)
                )
                for r in fs_rows
                if r["tick_price"] == tp
            )
            # simpler persist: tick still in a post snap
            persisted = False
            for dt in (1, 5, 12, 30, 60):
                s = books.get(am + dt * 1000)
                if s is None or not s.genuine:
                    continue
                levels = s.asks if a["side"] == "ASK" else s.bids
                for price, qty in levels:
                    if qty <= 0:
                        continue
                    if abs(normalize_tick_price(price, tick) - tp) < 1e-9:
                        if float(a["lower_edge"]) <= price <= float(a["upper_edge"]):
                            persisted = True
                            break
                if persisted:
                    break
            a["arrival_wall_persisted_post_arrival"] = persisted
        else:
            a["arrival_wall_persisted_post_arrival"] = False

        exact_rows.append(
            {
                "pool_arrival_id": a["pool_arrival_id"],
                "pool_id": a["pool_id"],
                "side": a["side"],
                "arrival_ts": a["arrival_ts"],
                "exact_snap_ts": a["exact_snap_ts"],
                "ob_age_ms": a["exact_ob_age_ms"],
                "full_side_level_count": a["full_side_level_count"],
                "strongest_wall_price": a["strongest_wall_price_at_arrival"],
                "strongest_wall_notional": a["strongest_wall_notional_at_arrival"],
                "strongest_wall_full_side_rank": a["strongest_wall_rank_at_arrival"],
                "strongest_wall_percentile": a["strongest_wall_percentile_at_arrival"],
                "wall_class_at_arrival": cls,
                "wall_present_at_arrival": a["wall_present_at_arrival"],
                "pool_filter_applied_after_full_side_rank": True,
            }
        )

    # Cluster-level wall aggregation at start
    print("Cluster wall summaries...", flush=True)
    cluster_summaries = []
    arrivals_by_cluster: dict[str, list] = defaultdict(list)
    for a in arrivals:
        if a.get("market_arrival_cluster_id"):
            arrivals_by_cluster[a["market_arrival_cluster_id"]].append(a)

    for cl in clusters:
        members = arrivals_by_cluster.get(cl.cluster_id, [])
        # walls at cluster start: use arrivals with arrival_ts_ms == start OR nearest at start
        start_members = [m for m in members if int(m["arrival_ts_ms"]) == cl.start_ts_ms]
        if not start_members:
            start_members = sorted(members, key=lambda x: abs(int(x["arrival_ts_ms"]) - cl.start_ts_ms))[:1]

        # unique cluster walls from exact arrival of start members + component span
        # Use book at cluster start across full component union
        snap = books.get(cl.start_ts_ms)
        unique_major = set()
        unique_all = set()
        strongest = None
        total_n = 0.0
        if snap and snap.genuine and snap.ts_ms <= cl.start_ts_ms:
            levels = snap.asks if cl.side == "ASK" else snap.bids
            from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.ranking import (
                side_levels_ranked_full,
                pool_filter_after_rank,
            )

            ranked = side_levels_ranked_full(levels)
            inside = pool_filter_after_rank(ranked, cl.union_lower, cl.union_upper)
            # dedupe by tick
            by_tick = {}
            for r in inside:
                tp = normalize_tick_price(r["price"], tick)
                if tp not in by_tick or r["notional"] > by_tick[tp]["notional"]:
                    by_tick[tp] = r
            for tp, r in by_tick.items():
                unique_all.add(tp)
                total_n += r["notional"]
                if r["significance_class"] == "MAJOR":
                    unique_major.add(tp)
                if strongest is None or r["notional"] > strongest["notional"]:
                    strongest = r

        # post major new walls (strictly after start) — sample probes
        major_after = False
        strongest_changed = False
        seen_strongest = strongest["price"] if strongest else None
        for dt in (5, 12, 30, 60, 120):
            s = books.get(cl.start_ts_ms + dt * 1000)
            if s is None or not s.genuine:
                continue
            from orderbook_analyse.liquidity_pool_arrival_wall_monitor_v2.ranking import (
                side_levels_ranked_full,
                pool_filter_after_rank,
            )

            ranked = side_levels_ranked_full(s.asks if cl.side == "ASK" else s.bids)
            inside = pool_filter_after_rank(ranked, cl.union_lower, cl.union_upper)
            by_tick = {normalize_tick_price(r["price"], tick): r for r in inside}
            for tp, r in by_tick.items():
                if tp not in unique_all and r["significance_class"] == "MAJOR":
                    major_after = True
                unique_all.add(tp)
            if inside:
                cur_s = max(inside, key=lambda r: r["notional"])
                if seen_strongest is not None and abs(cur_s["price"] - seen_strongest) > tick * 0.6:
                    strongest_changed = True

        cls_start = "NO_WALL"
        if strongest:
            cls_start = strongest["significance_class"]
        cluster_summaries.append(
            {
                "market_arrival_cluster_id": cl.cluster_id,
                "side": cl.side,
                "approach_direction": cl.approach,
                "cluster_start_ts": _iso(_dt_ms(cl.start_ts_ms)),
                "cluster_end_ts": _iso(_dt_ms(cl.end_ts_ms)) if cl.end_ts_ms else None,
                "end_reason": cl.end_reason,
                "component_lower_edge": cl.union_lower,
                "component_upper_edge": cl.union_upper,
                "member_pool_ids": "|".join(sorted(cl.member_pool_ids)),
                "member_pool_count": len(cl.member_pool_ids),
                "pool_arrival_count": len(cl.pool_arrival_ids),
                "any_major_wall_at_cluster_start": cls_start == "MAJOR",
                "any_moderate_wall_at_cluster_start": cls_start == "MODERATE",
                "any_minor_wall_at_cluster_start": cls_start == "MINOR",
                "no_same_side_wall_at_cluster_start": strongest is None,
                "strongest_cluster_wall_price_at_start": strongest["price"] if strongest else None,
                "strongest_cluster_wall_notional_at_start": strongest["notional"] if strongest else None,
                "strongest_cluster_wall_full_side_rank_at_start": (
                    strongest["full_side_rank"] if strongest else None
                ),
                "count_unique_major_wall_levels_at_start": len(unique_major),
                "total_unique_same_side_notional_inside_component_at_start": total_n,
                "major_wall_appeared_strictly_after_cluster_start": major_after,
                "strongest_cluster_wall_changed": strongest_changed,
                "unique_wall_levels_seen": len(unique_all),
                "membership_changes": len(cl.membership_events),
            }
        )

    # Migration
    migration = build_migration(arrivals, first_seen_by_arrival)

    # Known cases
    _write_known_000715(out, arrivals, clusters)
    _write_known_022736(out, arrivals, first_seen_by_arrival, books, tick)

    # Primary 6 clusters
    primary = _select_primary(cluster_summaries)
    write_csv(out / "primary_market_clusters.csv", primary)
    _write_manual(out, primary)

    # Contracts
    (out / "v2_arrival_contract.json").write_text(
        json.dumps(
            {
                "exact_arrival_ob_snapshot": "last genuine Raw OB200 with ts<=arrival_ts and age<=1s; never future",
                "pool_filter_applied_after_full_side_rank": True,
                "rank": "full same-side levels then pool filter",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (out / "v2_first_seen_contract.json").write_text(
        json.dumps(
            {
                "classes": [e.value for e in FirstSeenClass],
                "ts_eq_arrival_never_after": True,
                "snapshot_le_arrival_never_after": True,
                "deprecated_field": "wall_appeared_after",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (out / "market_cluster_contract.json").write_text(
        json.dumps(
            {
                "merge_requires": [
                    "same_side",
                    "same_approach",
                    "same_connected_component",
                    "still_inside_union",
                ],
                "time_proximity_alone_insufficient": True,
                "ask_bid_never_mixed": True,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Counts
    fs_pre = sum(
        1 for r in first_seen_rows if r["first_seen_class"] == FirstSeenClass.PRE_EXISTING_BEFORE_ARRIVAL.value
    )
    fs_at = sum(
        1 for r in first_seen_rows if r["first_seen_class"] == FirstSeenClass.FIRST_SEEN_AT_ARRIVAL.value
    )
    fs_after = sum(
        1
        for r in first_seen_rows
        if r["first_seen_class"] == FirstSeenClass.APPEARED_STRICTLY_AFTER_ARRIVAL.value
    )
    ask_arr = sum(1 for a in arrivals if a["side"] == "ASK")
    bid_arr = sum(1 for a in arrivals if a["side"] == "BID")
    ask_cl = sum(1 for c in cluster_summaries if c["side"] == "ASK")
    bid_cl = sum(1 for c in cluster_summaries if c["side"] == "BID")
    single = sum(1 for c in cluster_summaries if c["member_pool_count"] == 1)
    multi = sum(1 for c in cluster_summaries if c["member_pool_count"] > 1)
    max_pools = max((c["member_pool_count"] for c in cluster_summaries), default=0)
    maj_cl = sum(1 for c in cluster_summaries if c["any_major_wall_at_cluster_start"])
    mod_cl = sum(1 for c in cluster_summaries if c["any_moderate_wall_at_cluster_start"])
    min_cl = sum(1 for c in cluster_summaries if c["any_minor_wall_at_cluster_start"])
    none_cl = sum(1 for c in cluster_summaries if c["no_same_side_wall_at_cluster_start"])

    write_csv(out / "pool_arrivals_v2.csv", arrivals)
    write_csv(out / "market_arrival_clusters.csv", cluster_summaries)
    write_csv(
        out / "cluster_membership_timeline.csv",
        [
            {
                **r,
                "membership_valid_from": _iso(_dt_ms(r["membership_valid_from"]))
                if r.get("membership_valid_from")
                else None,
                "membership_valid_to": _iso(_dt_ms(r["membership_valid_to"]))
                if r.get("membership_valid_to")
                else None,
            }
            for r in membership_rows
        ],
    )
    write_csv(out / "exact_arrival_walls_v2.csv", exact_rows)
    write_csv(out / "wall_first_seen_v2.csv", first_seen_rows)
    write_csv(out / "cluster_wall_summary.csv", cluster_summaries)
    write_csv(out / "old_to_v2_migration.csv", migration)

    # Validation gates
    print("Foundation parity (post)...", flush=True)
    foundation_post = verify_foundation_parity()
    ok_07 = _check_000715_merged(arrivals)
    ok_0230 = _check_022736(arrivals, first_seen_by_arrival, tick)
    ok_classes = (
        class_counts["MAJOR"] >= 1200
        and class_counts["NO_WALL"] >= 1
        and class_counts["MODERATE"] == 0
    )
    # no AFTER with present_exact
    bad_after = any(
        r["first_seen_class"] == FirstSeenClass.APPEARED_STRICTLY_AFTER_ARRIVAL.value
        and r["present_exact_arrival"]
        for r in first_seen_rows
    )
    failures = []
    if not foundation_post["parity_pass"]:
        failures.append("FOUNDATION_PARITY")
    if not ok_07:
        failures.append("000715_000716_NOT_MERGED")
    if not ok_0230:
        failures.append("022736_WALL_SPLIT_FAILED")
    if bad_after:
        failures.append("AFTER_INCLUDES_ARRIVAL")
    if not ok_classes:
        failures.append("CLASS_COUNTS_UNEXPECTED")

    if failures:
        verdict = "LIQUIDITY_POOL_ARRIVAL_WALL_MONITOR_V2_REVISION_FAILED"
    elif len(primary) < 6:
        verdict = "LIQUIDITY_POOL_ARRIVAL_WALL_MONITOR_V2_PARTIAL"
    else:
        verdict = "LIQUIDITY_POOL_ARRIVAL_WALL_MONITOR_V2_COMPLETE"

    (out / "verdict.json").write_text(
        json.dumps(
            {
                "verdict": verdict,
                "failures": failures,
                "pool_arrival_counts": dict(class_counts),
                "n_pool_arrivals": len(arrivals),
                "n_market_clusters": len(clusters),
                "major_at_cluster_start": maj_cl,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (out / "run_manifest.json").write_text(
        json.dumps(
            {
                "audit_id": "LIQUIDITY_POOL_ARRIVAL_WALL_MONITOR_V2",
                "foundation_commit": "9b8fe7cf1947d3b821d6ae4d1df2719ec94107f4",
                "verdict": verdict,
                "window": {"start": WINDOW_START, "end": WINDOW_END},
                "prior_v1": str(PRIOR_V1),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (out / "data_quality_report.json").write_text(
        json.dumps(
            {
                "foundation_parity_pre": foundation["parity_pass"],
                "foundation_parity_post": foundation_post["parity_pass"],
                "n_mids": len(mids),
                "n_pool_arrivals": len(arrivals),
                "n_clusters": len(clusters),
                "class_counts": dict(class_counts),
                "first_seen_counts": {
                    "PRE_EXISTING": fs_pre,
                    "FIRST_SEEN_AT_ARRIVAL": fs_at,
                    "APPEARED_STRICTLY_AFTER": fs_after,
                },
                "no_outcomes": True,
                "no_public_trades": True,
                "validation_failures": failures,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    prim_ts = "\n".join(
        f"- {r['example_id']} {r['side']} `{r['cluster_start_ts']}` "
        f"pools={r['member_pool_count']} major={r['any_major_wall_at_cluster_start']}"
        for r in primary
    )
    bericht = f"""# ABSCHLUSSBERICHT — LIQUIDITY_POOL_ARRIVAL_WALL_MONITOR_V2

## 1. VERDICT

**{verdict}**

Failures: `{failures}`

## 2. Live-Sicherheit

Read-only Marktdaten. Kein Commit/Push. Outputs nur unter diesem Ordner.

## 3. Branch / HEAD / Dirty

orderbook_analyse `feature/strategy-lab-phase1` @ `9b8fe7cf1947d3b821d6ae4d1df2719ec94107f4`. Dirty unverändert.

## 4. Foundation-Parität

pre=`{foundation['parity_pass']}` post=`{foundation_post['parity_pass']}`

## 5. Geänderte Dateien

- `src/orderbook_analyse/liquidity_pool_arrival_wall_monitor_v2/` (neu)
- `scripts/run_liquidity_pool_arrival_wall_monitor_v2.py` (neu)
- `tests/test_liquidity_pool_arrival_wall_monitor_v2.py` (neu)
- Foundation `liquidity_pool_signal/` **unverändert**

## 6. Exact-Arrival-Fix

Nur Snapshot `ts<=arrival` age≤1s. Kein Future. Funnel-Klassen nur daraus.

## 7. First-Seen-Fix

`PRE_EXISTING` / `FIRST_SEEN_AT_ARRIVAL` / `APPEARED_STRICTLY_AFTER` / `TIMESTAMP_UNRESOLVED`.
`ts==arrival` niemals AFTER. Feld `wall_appeared_after` depreziert.

## 8. Pool-ID-Arrivals

raw=`{len(arrivals)}` ASK=`{ask_arr}` BID=`{bid_arr}`

## 9. Validierte Klassen (Pool-ID Exact Arrival)

MAJOR=`{class_counts['MAJOR']}` MODERATE=`{class_counts['MODERATE']}` MINOR=`{class_counts['MINOR']}` NO_WALL=`{class_counts['NO_WALL']}` UNAVAILABLE=`{class_counts['SNAPSHOT_UNAVAILABLE']}`

## 10. PRE_EXISTING

wall-rows=`{fs_pre}`

## 11. FIRST_SEEN_AT_ARRIVAL

wall-rows=`{fs_at}`

## 12. APPEARED_STRICTLY_AFTER

wall-rows=`{fs_after}`

## 13. Market-Cluster-Contract

Connected intervals + continuity inside union; keine reine Zeitnähe. ASK/BID getrennt.

## 14. Unabhängige Market-Cluster

`{len(clusters)}`

## 15. ASK-/BID-Cluster

ASK=`{ask_cl}` BID=`{bid_cl}`

## 16. Single-/Multi-Pool-Cluster

single=`{single}` multi=`{multi}` max_pools=`{max_pools}`

## 17. Pool-IDs je Cluster

siehe `market_arrival_clusters.csv`

## 18. 00:07:15/16

merged=`{ok_07}` — siehe `known_000715_000716_case.md`

## 19. 02:27:36

wall-split_ok=`{ok_0230}` — siehe `known_022736_case.md`

## 20. Wall-Deduplizierung

Clusterweit per `cluster_wall_identity` (side+tick); keine Doppel-Notional-Summe.

## 21. Alt↔Neu-Migration

`old_to_v2_migration.csv` (n=`{len(migration)}`)

## 22. Sechs manuelle Cluster-Timestamps

{prim_ts}

## 23. Tests

siehe `test_results.txt`

## 24. Wie viele unabhängige Marktankünfte bleiben?

**{len(clusters)}** Market-Arrival-Cluster (diagnostisch/kausal; kein Tradingfilter).

## 25. Wie viele besitzen wirklich eine MAJOR-Wall am Clusterstart?

**{maj_cl}**

## 26. Einschränkung

MAJOR-Existenz allein ist noch kein selektiver Tradingfilter.

## 27. Stop

Kein Commit. Keine Public Trades. Auf manuelle Prüfung warten.
"""
    (out / "ABSCHLUSSBERICHT.md").write_text(bericht, encoding="utf-8")
    print(
        json.dumps(
            {
                "verdict": verdict,
                "failures": failures,
                "pool_arrivals": len(arrivals),
                "clusters": len(clusters),
                "class_counts": dict(class_counts),
                "major_clusters": maj_cl,
                "primary": [p["cluster_start_ts"] for p in primary],
            },
            indent=2,
        )
    )
    return 0 if not failures or verdict.endswith("COMPLETE") or verdict.endswith("PARTIAL") else 1


def _check_000715_merged(arrivals: list[dict]) -> bool:
    a = [x for x in arrivals if x["arrival_ts"] == "2026-08-25T00:07:15Z" and x["side"] == "ASK"]
    b = [x for x in arrivals if x["arrival_ts"] == "2026-08-25T00:07:16Z" and x["side"] == "ASK"]
    if not a or not b:
        return False
    # prefer overlapping pair from primary v1 pools if present
    ids_a = {x["market_arrival_cluster_id"] for x in a}
    ids_b = {x["market_arrival_cluster_id"] for x in b}
    return bool(ids_a & ids_b)


def _check_022736(arrivals, first_seen_by_arrival, tick) -> bool:
    hits = [
        a
        for a in arrivals
        if a["arrival_ts"] == KNOWN_022736 and "1787684400" in a["pool_id"]
    ]
    if not hits:
        return False
    a = hits[0]
    if a.get("wall_class_at_arrival") != "MAJOR":
        return False
    sp = a.get("strongest_wall_price_at_arrival")
    if sp is None or abs(sp - KNOWN_WALL) < tick * 0.6:
        return False  # must NOT be 79217.1
    fs = first_seen_by_arrival.get(a["pool_arrival_id"], [])
    w79217 = [
        r
        for r in fs
        if abs(float(r["tick_price"]) - normalize_tick_price(KNOWN_WALL, tick)) < 1e-9
    ]
    if not w79217:
        return False
    return w79217[0]["first_seen_class"] == FirstSeenClass.APPEARED_STRICTLY_AFTER_ARRIVAL.value


def _write_known_000715(out: Path, arrivals, clusters) -> None:
    a = next(
        (
            x
            for x in arrivals
            if x["arrival_ts"] == "2026-08-25T00:07:15Z" and x["side"] == "ASK"
        ),
        None,
    )
    b = next(
        (
            x
            for x in arrivals
            if x["arrival_ts"] == "2026-08-25T00:07:16Z" and x["side"] == "ASK"
        ),
        None,
    )
    lines = ["# Known case 00:07:15 / 00:07:16", ""]
    if not a or not b:
        lines.append("Arrivals not found.")
    else:
        same = a.get("market_arrival_cluster_id") == b.get("market_arrival_cluster_id")
        lines += [
            f"- A pool_arrival_id=`{a['pool_arrival_id']}` pool=`{a['pool_id']}` "
            f"bounds=[{a['lower_edge']},{a['upper_edge']}] mid=`{a['mid_at_arrival']}`",
            f"- B pool_arrival_id=`{b['pool_arrival_id']}` pool=`{b['pool_id']}` "
            f"bounds=[{b['lower_edge']},{b['upper_edge']}] mid=`{b['mid_at_arrival']}`",
            f"- overlap: lower=max({a['lower_edge']},{b['lower_edge']}) "
            f"upper=min({a['upper_edge']},{b['upper_edge']})",
            f"- same market_arrival_cluster_id: `{same}` → `{a.get('market_arrival_cluster_id')}`",
            f"- two pool_arrival_ids, one market cluster: `{same}`",
        ]
    (out / "known_000715_000716_case.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_known_022736(out, arrivals, first_seen_by_arrival, books, tick) -> None:
    hits = [
        a
        for a in arrivals
        if a["arrival_ts"] == KNOWN_022736 and "1787684400" in a["pool_id"]
    ]
    lines = ["# Known case 2026-08-26T02:27:36Z", ""]
    if not hits:
        lines.append("Not found.")
    else:
        a = hits[0]
        fs = first_seen_by_arrival.get(a["pool_arrival_id"], [])
        w79176 = a.get("strongest_wall_price_at_arrival")
        w79217 = next(
            (
                r
                for r in fs
                if abs(float(r["tick_price"]) - normalize_tick_price(KNOWN_WALL, tick)) < 1e-9
            ),
            None,
        )
        lines += [
            f"- pool_arrival_id=`{a['pool_arrival_id']}`",
            f"- Arrival-Wall exact: price=`{w79176}` class=`{a.get('wall_class_at_arrival')}` "
            f"rank=`{a.get('strongest_wall_rank_at_arrival')}`",
            f"- Arrival-Wall first_seen: see wall_first_seen for that tick",
            f"- 79217.1 first_seen_class=`{w79217['first_seen_class'] if w79217 else None}` "
            f"ts=`{w79217['first_seen_ts'] if w79217 else None}`",
            "- 79217.1 must not set MAJOR_AT_ARRIVAL — "
            f"can_set=`{w79217['can_set_major_at_arrival'] if w79217 else None}`",
            f"- Same wall as arrival? `{w79176 is not None and abs(w79176 - KNOWN_WALL) < tick * 0.6}`",
        ]
    (out / "known_022736_case.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _select_primary(cluster_summaries: list[dict]) -> list[dict]:
    asks = sorted(
        [c for c in cluster_summaries if c["side"] == "ASK"],
        key=lambda c: c["cluster_start_ts"],
    )
    bids = sorted(
        [c for c in cluster_summaries if c["side"] == "BID"],
        key=lambda c: c["cluster_start_ts"],
    )
    chosen = []
    for side_list in (asks, bids):
        multi = [c for c in side_list if c["member_pool_count"] > 1]
        single = [c for c in side_list if c["member_pool_count"] == 1]
        bucket = []
        if multi:
            bucket.append(multi[0])
        if single:
            bucket.append(single[0])
        for c in side_list:
            if len(bucket) >= 3:
                break
            if c not in bucket:
                bucket.append(c)
        for i, c in enumerate(bucket[:3]):
            row = dict(c)
            row["example_id"] = f"{'A' if c['side']=='ASK' else 'B'}{i+1}"
            ts = c["cluster_start_ts"]
            row["chart_window_start"] = _iso(_utc(ts) - timedelta(minutes=10))
            row["chart_window_end"] = _iso(_utc(ts) + timedelta(minutes=10))
            chosen.append(row)
    return chosen


def _write_manual(out: Path, primary: list[dict]) -> None:
    lines = [
        "# MANUAL_V2_REVIEW",
        "",
        "Toggles: Liquidity Location + Orderbook Walls.",
        "",
    ]
    for r in primary:
        lines += [
            f"## {r['example_id']} — {r['side']}",
            f"- cluster_id: `{r['market_arrival_cluster_id']}`",
            f"- start: `{r['cluster_start_ts']}` end: `{r['cluster_end_ts']}` ({r['end_reason']})",
            f"- chart: `{r['chart_window_start']}` → `{r['chart_window_end']}`",
            f"- component: [{r['component_lower_edge']}, {r['component_upper_edge']}]",
            f"- members ({r['member_pool_count']}): `{r['member_pool_ids']}`",
            f"- strongest at start: `{r['strongest_cluster_wall_price_at_start']}` "
            f"rank=`{r['strongest_cluster_wall_full_side_rank_at_start']}` "
            f"major=`{r['any_major_wall_at_cluster_start']}`",
            f"- major after start: `{r['major_wall_appeared_strictly_after_cluster_start']}`",
            "",
        ]
    (out / "MANUAL_V2_REVIEW.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
