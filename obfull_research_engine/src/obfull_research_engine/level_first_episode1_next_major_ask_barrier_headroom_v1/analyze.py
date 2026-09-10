"""Per-decision-time causal headroom analysis."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from ..paths import ENGINE_ROOT
from ..timeparse import format_utc_z
from . import (
    ASK_WALL_BREACH,
    CLUSTER_CONTRACT,
    COVERAGE_CENSORED_DETECTION,
    COVERAGE_EPOCH_BOUNDARY,
    COVERAGE_OK,
    EPISODE_ID,
    EPOCH4_COVERAGE_END,
    ESTIMATED_ENTRY_SLIPPAGE_PCT,
    ESTIMATED_EXIT_SLIPPAGE_PCT,
    FEE_CONTRACT,
    FROZEN_BOOK_DIR,
    FROZEN_DEFENSE_CHAIN_RUN,
    FROZEN_HANDOFF_PATH,
    REQUIRED_GROSS_HEADROOM_PCT,
    SCAN_MAX_DISTANCE_PCT,
    SYMBOL,
)
from .barriers import select_barriers
from .book_walk import load_payload, snapshots_at_decisions
from .chain_context import chain_context_for_barrier
from .clusters import build_ask_barrier_clusters
from .costs import conservative_targets, headroom_bundle
from .entry import executable_long_entry
from .outcome_only import outcome_only_barrier_touch
from .walls import major_views, wall_candidates_from_book


def _norm_ts(ts: str) -> str:
    return format_utc_z(_as_dt(ts))


def decision_schedule(handoff: dict[str, Any]) -> list[dict[str, Any]]:
    breach = _norm_ts(ASK_WALL_BREACH)
    bdt = _as_dt(breach)
    rows = [
        {"key": "ZONE_FIRST_TOUCH", "decision_time": _norm_ts(handoff["zone_first_touch_exchange_event_time"])},
        {"key": "WALL_FIRST_TOUCH", "decision_time": _norm_ts(handoff["wall_first_touch_exchange_event_time"])},
        {"key": "FIRST_JOINT_BREACH", "decision_time": breach},
    ]
    for s in (1, 3, 5, 10, 15):
        rows.append(
            {
                "key": f"FIRST_JOINT_BREACH_PLUS_{s}S",
                "decision_time": format_utc_z(bdt + timedelta(seconds=s)),
                "offset_s": s,
            }
        )
    rows.append(
        {
            "key": "DETECTION",
            "decision_time": _norm_ts(handoff["detection_exchange_event_time"]),
            "force_censor": COVERAGE_CENSORED_DETECTION,
        }
    )
    return rows


def _compact_wall(w: dict[str, Any] | None) -> dict[str, Any] | None:
    if not w:
        return None
    keys = (
        "price",
        "qty_base",
        "notional_usdt",
        "distance_pct",
        "distance_ticks",
        "rolling_size_percentile",
        "rolling_notional_percentile",
        "effective_size_percentile",
        "percentile_basis",
        "local_depth_share",
        "rank_by_size",
        "wall_generation_id",
        "first_visible_at",
        "pre_existing_at_decision",
    )
    return {k: w.get(k) for k in keys}


def _compact_cluster(c: dict[str, Any] | None) -> dict[str, Any] | None:
    if not c:
        return None
    skip = {"member_prices"}
    return {k: v for k, v in c.items() if k not in skip}


def analyze_decision(
    *,
    key: str,
    decision_time: str,
    snap: dict[str, Any],
    scored_walls: list[dict[str, Any]],
    chain: dict[str, Any],
    wall_first_touch: str,
    mid_path: list[tuple[str, float]],
    force_censor: str | None = None,
) -> dict[str, Any]:
    base = {
        "decision_key": key,
        "decision_time": decision_time,
        "fee_contract": FEE_CONTRACT,
        "cluster_contract": CLUSTER_CONTRACT,
    }
    if force_censor:
        return {
            **base,
            "coverage_status": force_censor,
            "coverage_ok": False,
            "minimum_0_30_net_met": None,
        }
    if snap.get("censored"):
        return {
            **base,
            "coverage_status": snap.get("censor_reason") or COVERAGE_EPOCH_BOUNDARY,
            "coverage_ok": False,
            "minimum_0_30_net_met": None,
        }

    entry = executable_long_entry(
        best_bid=snap.get("best_bid"),
        best_ask=snap.get("best_ask"),
        microprice=snap.get("microprice"),
        last_event_time=snap.get("last_event_time"),
        decision_time=decision_time,
        replay_epoch=snap.get("replay_epoch"),
    )
    if not entry.get("ok"):
        return {
            **base,
            "coverage_status": "BBO_INVALID",
            "coverage_ok": False,
            "entry": entry,
            "minimum_0_30_net_met": None,
        }

    # Depth sufficiency: need asks extending at least to required gross target
    theo = float(entry["theoretical_long_entry"])
    req_target = theo * (1.0 + REQUIRED_GROSS_HEADROOM_PCT / 100.0)
    ask_max = snap.get("ask_max_price")
    depth_ok = ask_max is not None and float(ask_max) + 1e-12 >= req_target

    cands = wall_candidates_from_book(
        asks=snap["asks"],
        entry_price=theo,
        decision_time=decision_time,
        replay_epoch=snap.get("replay_epoch"),
        scored_walls=scored_walls,
        max_input_available_at=snap.get("max_input_available_at"),
    )
    clusters = build_ask_barrier_clusters(cands, entry_price=theo, decision_time=decision_time)
    views = major_views(cands, entry=theo)
    selected = select_barriers(candidates=cands, clusters=clusters, default_major_q=0.95)

    nearest = selected.get("NEAREST_MAJOR_BARRIER")
    q95 = views["by_percentile"].get("Q95")
    # Table wall price = FIRST_BLOCKING / Q95 research view (never skip nearer Q95).
    barrier_wall = (selected.get("FIRST_BLOCKING_BARRIER") or {}).get("wall") or q95
    barrier_price = float(barrier_wall["price"]) if barrier_wall else None
    barrier_cluster = (nearest or {}).get("cluster") or (selected.get("FIRST_BLOCKING_BARRIER") or {}).get("cluster")
    if barrier_price is None and nearest:
        barrier_price = nearest["price"]
        barrier_wall = nearest.get("wall")
        barrier_cluster = nearest.get("cluster")

    if barrier_price is None:
        return {
            **base,
            "coverage_status": COVERAGE_OK if depth_ok else "DEPTH_INSUFFICIENT",
            "coverage_ok": True,
            "depth_ok": depth_ok,
            "entry": entry,
            "wall_candidates_n": len(cands),
            "clusters_n": len(clusters),
            "major_views": {
                "by_percentile": {k: _compact_wall(v) for k, v in views["by_percentile"].items()},
                "largest_within_pct": {k: _compact_wall(v) for k, v in views["largest_within_pct"].items()},
            },
            "selected": selected,
            "no_past_only_major_barrier": True,
            "minimum_0_30_net_met": False,
            "ob_full_coverage": {
                "barrier_within_available_depth": None,
                "replay_epoch": snap.get("replay_epoch"),
                "last_event_time": snap.get("last_event_time"),
                "n_ask_levels": snap.get("n_ask_levels"),
                "ask_max_price": ask_max,
                "event_available_at_le_decision": True,
                "full_depth_coverage": snap.get("full_depth_coverage"),
            },
        }

    targets = conservative_targets(wall_price=float(barrier_price), spread=entry.get("spread"))
    # Primary headroom: conservative target = 1 tick before wall; also report at-wall
    primary_target = targets["target_1_tick_before_wall"]
    headroom_at_wall = headroom_bundle(
        executable_entry=theo,
        conservative_target=targets["target_at_wall"],
        entry_slippage_pct=ESTIMATED_ENTRY_SLIPPAGE_PCT,
        exit_slippage_pct=ESTIMATED_EXIT_SLIPPAGE_PCT,
    )
    headroom_before = headroom_bundle(
        executable_entry=theo,
        conservative_target=primary_target,
        entry_slippage_pct=ESTIMATED_ENTRY_SLIPPAGE_PCT,
        exit_slippage_pct=ESTIMATED_EXIT_SLIPPAGE_PCT,
    )
    # also with slippage-adjusted entry
    headroom_slip_entry = headroom_bundle(
        executable_entry=float(entry["long_entry_with_slippage"]),
        conservative_target=primary_target,
        entry_slippage_pct=0.0,  # already in entry; don't double-count entry slip
        exit_slippage_pct=ESTIMATED_EXIT_SLIPPAGE_PCT,
    )

    ctx = chain_context_for_barrier(
        barrier_price=float(barrier_price),
        chain=chain,
        decision_time=decision_time,
        wall_first_touch=wall_first_touch,
    )

    # OUTCOME_ONLY — computed after selection, stored separately
    outcome = outcome_only_barrier_touch(
        barrier_price=float(barrier_price),
        decision_time=decision_time,
        mid_path=mid_path,
        coverage_end=EPOCH4_COVERAGE_END,
    )

    cluster = barrier_cluster or (nearest or {}).get("cluster")
    table_row = {
        "decision_time": decision_time,
        "decision_key": key,
        "executable_long_entry": theo,
        "nearest_major_wall_price": barrier_price,
        "nearest_major_cluster_low": (cluster or {}).get("band_low"),
        "nearest_major_cluster_high": (cluster or {}).get("band_high"),
        "wall_percentile": (barrier_wall or {}).get("effective_size_percentile")
        or (barrier_wall or {}).get("rolling_size_percentile"),
        "wall_percentile_basis": (barrier_wall or {}).get("percentile_basis"),
        "cluster_notional": (cluster or {}).get("total_cluster_notional"),
        "distance_pct": ((float(barrier_price) / theo) - 1.0) * 100.0,
        "gross_headroom_pct": headroom_before["gross_headroom_pct"],
        "net_after_0_11_fee_pct": headroom_before["net_headroom_after_fees_pct"],
        "net_after_fees_and_slippage_pct": headroom_before["net_headroom_after_fees_and_slippage_pct"],
        "required_target_price": headroom_before["required_target_price_for_0_30_net"],
        "minimum_0_30_net_met": headroom_before["net_requirement_met_before_slippage"],
        "coverage_status": COVERAGE_OK if depth_ok else "DEPTH_MARGINAL_BUT_BARRIER_VISIBLE",
    }

    around_80k = [
        c
        for c in cands
        if 79950.0 <= float(c["price"]) <= 80100.0
    ]
    cluster_80k = [
        c
        for c in clusters
        if c["band_low"] <= 80000.0 <= c["band_high"]
        or (79950.0 <= float(c["peak_wall_price"]) <= 80100.0)
    ]
    # Dedicated descriptive view for ~80000 (not selected as trading rule)
    wall_80000 = next((c for c in cands if abs(float(c["price"]) - 80000.0) < 1e-9), None)
    headroom_80000 = None
    if wall_80000:
        tg80 = conservative_targets(wall_price=80000.0, spread=entry.get("spread"))
        headroom_80000 = {
            "wall_price": 80000.0,
            "qty_base": wall_80000["qty_base"],
            "notional_usdt": wall_80000["notional_usdt"],
            "distance_pct": wall_80000["distance_pct"],
            "at_wall": headroom_bundle(
                executable_entry=theo,
                conservative_target=tg80["target_at_wall"],
                entry_slippage_pct=ESTIMATED_ENTRY_SLIPPAGE_PCT,
                exit_slippage_pct=ESTIMATED_EXIT_SLIPPAGE_PCT,
            ),
            "one_tick_before": headroom_bundle(
                executable_entry=theo,
                conservative_target=tg80["target_1_tick_before_wall"],
                entry_slippage_pct=ESTIMATED_ENTRY_SLIPPAGE_PCT,
                exit_slippage_pct=ESTIMATED_EXIT_SLIPPAGE_PCT,
            ),
        }

    return {
        **base,
        "coverage_status": table_row["coverage_status"],
        "coverage_ok": True,
        "depth_ok": depth_ok,
        "entry": entry,
        "wall_candidates_n": len(cands),
        "wall_candidates_top": [_compact_wall(w) for w in sorted(cands, key=lambda x: -x["qty_base"])[:15]],
        "clusters_n": len(clusters),
        "clusters_top": [_compact_cluster(c) for c in sorted(clusters, key=lambda x: -x["total_cluster_notional"])[:10]],
        "major_views": {
            "by_percentile": {k: _compact_wall(v) for k, v in views["by_percentile"].items()},
            "largest_within_pct": {k: _compact_wall(v) for k, v in views["largest_within_pct"].items()},
        },
        "selected": {
            "NEAREST_MAJOR_BARRIER": {
                "kind": (nearest or {}).get("kind"),
                "price": barrier_price,
                "wall": _compact_wall(barrier_wall),
                "cluster": _compact_cluster(cluster),
            },
            "STRONGEST_VISIBLE_BARRIER": {
                "wall": _compact_wall((selected.get("STRONGEST_VISIBLE_BARRIER") or {}).get("wall")),
                "cluster": _compact_cluster((selected.get("STRONGEST_VISIBLE_BARRIER") or {}).get("cluster")),
            },
            "FIRST_BLOCKING_BARRIER": {
                "wall": _compact_wall((selected.get("FIRST_BLOCKING_BARRIER") or {}).get("wall")),
                "cluster": _compact_cluster((selected.get("FIRST_BLOCKING_BARRIER") or {}).get("cluster")),
                "research_view_percentile": 0.95,
            },
        },
        "conservative_targets": targets,
        "headroom_at_wall": headroom_at_wall,
        "headroom_1_tick_before_wall": headroom_before,
        "headroom_from_slippage_entry": headroom_slip_entry,
        "chain_context": ctx,
        "table_row": table_row,
        "around_80000": {
            "visible_at_decision": len(around_80k) > 0 or wall_80000 is not None,
            "levels": [_compact_wall(w) for w in sorted(around_80k, key=lambda x: -x["qty_base"])[:10]],
            "clusters": [_compact_cluster(c) for c in cluster_80k[:5]],
            "single_wall_or_cluster": (
                "cluster"
                if cluster_80k and cluster_80k[0]["number_of_price_levels"] > 1
                else ("single_wall" if (around_80k or wall_80000) else "not_visible")
            ),
            "headroom_to_80000": headroom_80000,
        },
        "ob_full_coverage": {
            "barrier_within_available_depth": ask_max is not None and float(ask_max) >= float(barrier_price),
            "required_target_within_depth": depth_ok,
            "replay_epoch": snap.get("replay_epoch"),
            "last_event_time": snap.get("last_event_time"),
            "max_input_available_at": snap.get("max_input_available_at"),
            "n_ask_levels": snap.get("n_ask_levels"),
            "ask_max_price": ask_max,
            "scan_max_distance_pct": SCAN_MAX_DISTANCE_PCT,
            "event_available_at_le_decision": True,
            "full_depth_coverage": snap.get("full_depth_coverage"),
            "no_post_decision_inputs": True,
        },
        "OUTCOME_ONLY": outcome,
        "minimum_0_30_net_met": table_row["minimum_0_30_net_met"],
        "gross_requirement_met": headroom_before["gross_requirement_met"],
    }


def run_episode1_analysis(
    *,
    book_dir: Path | None = None,
    defense_chain_dir: Path | None = None,
    handoff_path: Path | None = None,
) -> dict[str, Any]:
    book_dir = Path(book_dir or (ENGINE_ROOT / FROZEN_BOOK_DIR))
    dch = Path(defense_chain_dir or (ENGINE_ROOT / FROZEN_DEFENSE_CHAIN_RUN))
    handoff = json.loads(Path(handoff_path or (ENGINE_ROOT / FROZEN_HANDOFF_PATH)).read_text(encoding="utf-8"))
    scored = json.loads((dch / "scored_walls_epoch4.json").read_text(encoding="utf-8"))
    scored_walls = scored.get("walls") or []
    chain = json.loads((dch / "defense_chain_epoch4.json").read_text(encoding="utf-8"))

    schedule = decision_schedule(handoff)
    # One book walk for decision times + dense outcome path samples
    breach_t = _as_dt(ASK_WALL_BREACH)
    tend = _as_dt(EPOCH4_COVERAGE_END)
    dense_times: list[str] = []
    t = breach_t
    while t <= tend:
        dense_times.append(format_utc_z(t))
        t = t + timedelta(seconds=1)
    decision_times = [r["decision_time"] for r in schedule if not r.get("force_censor")]
    all_times = sorted(set(decision_times + dense_times), key=_as_dt)
    payload = load_payload(book_dir)
    snaps = snapshots_at_decisions(payload, decision_times=all_times, require_epoch=4)

    mid_path: list[tuple[str, float]] = []
    for ts in all_times:
        s = snaps.get(ts)
        if not s or s.get("censored"):
            continue
        if s.get("best_bid") is None or s.get("best_ask") is None:
            continue
        mid_path.append((ts, 0.5 * (float(s["best_bid"]) + float(s["best_ask"]))))

    results = []
    for r in schedule:
        if r.get("force_censor"):
            results.append(
                analyze_decision(
                    key=r["key"],
                    decision_time=r["decision_time"],
                    snap={"censored": True},
                    scored_walls=scored_walls,
                    chain=chain,
                    wall_first_touch=handoff["wall_first_touch_exchange_event_time"],
                    mid_path=mid_path,
                    force_censor=r["force_censor"],
                )
            )
            continue
        snap = snaps[r["decision_time"]]
        results.append(
            analyze_decision(
                key=r["key"],
                decision_time=r["decision_time"],
                snap=snap,
                scored_walls=scored_walls,
                chain=chain,
                wall_first_touch=handoff["wall_first_touch_exchange_event_time"],
                mid_path=mid_path,
            )
        )

    table = [r["table_row"] for r in results if r.get("table_row")]
    valid = [r for r in results if r.get("coverage_ok")]
    any_barrier = any(not r.get("no_past_only_major_barrier") and r.get("table_row") for r in valid)
    depth_fail = any(r.get("coverage_ok") and r.get("depth_ok") is False and r.get("table_row") for r in valid)

    # Headroom evolution after breach
    breach_rows = [r for r in results if str(r["decision_key"]).startswith("FIRST_JOINT_BREACH")]
    first_fail_key = None
    for r in results:
        tr = r.get("table_row")
        if not tr:
            continue
        if tr.get("minimum_0_30_net_met") is not True:
            first_fail_key = r["decision_key"]
            break
    # If every valid row fails, also note that headroom never met
    any_met = any((r.get("table_row") or {}).get("minimum_0_30_net_met") is True for r in results)

    # Explicit ~80k answers at breach
    breach = next((r for r in results if r["decision_key"] == "FIRST_JOINT_BREACH"), None)
    answers = {
        "barrier_around_80000_causally_known_at_FIRST_JOINT_BREACH": bool(
            (breach or {}).get("around_80000", {}).get("visible_at_decision")
        ),
        "single_wall_or_cluster": (breach or {}).get("around_80000", {}).get("single_wall_or_cluster"),
        "exact_price_or_band": None,
        "distance_from_executable_entry_at_breach_pct": None,
        "gross_at_least_0_41": None,
        "net_after_0_11_at_least_0_30": None,
        "headroom_evolution_after_breach": [
            {
                "decision_key": r["decision_key"],
                "gross_headroom_pct": (r.get("table_row") or {}).get("gross_headroom_pct"),
                "net_after_0_11_fee_pct": (r.get("table_row") or {}).get("net_after_0_11_fee_pct"),
                "minimum_0_30_net_met": (r.get("table_row") or {}).get("minimum_0_30_net_met"),
                "nearest_major_wall_price": (r.get("table_row") or {}).get("nearest_major_wall_price"),
                "coverage_status": r.get("coverage_status"),
            }
            for r in breach_rows
        ],
        "first_decision_where_required_headroom_absent": first_fail_key,
        "any_decision_met_0_30_net": any_met,
        "note_80000_structure": (
            "Ask level 80000.0 is a large single price level; nearby ask mass "
            "forms additional clusters — not one automatic mega-barrier."
        ),
    }
    if breach and breach.get("table_row"):
        tr = breach["table_row"]
        answers["exact_price_or_band"] = {
            "nearest_major_wall_price": tr.get("nearest_major_wall_price"),
            "cluster_low": tr.get("nearest_major_cluster_low"),
            "cluster_high": tr.get("nearest_major_cluster_high"),
            "around_80000_wall": 80000.0 if (breach.get("around_80000") or {}).get("headroom_to_80000") else None,
            "around_80000_peak_in_band": ((breach.get("around_80000") or {}).get("levels") or [{}])[0].get("price"),
            "around_80000_cluster": [
                {"low": c.get("band_low"), "high": c.get("band_high"), "peak": c.get("peak_wall_price"), "notional": c.get("total_cluster_notional")}
                for c in ((breach.get("around_80000") or {}).get("clusters") or [])[:3]
            ],
        }
        answers["distance_from_executable_entry_at_breach_pct"] = tr.get("distance_pct")
        answers["gross_at_least_0_41"] = tr.get("gross_headroom_pct") is not None and tr["gross_headroom_pct"] >= 0.410
        answers["net_after_0_11_at_least_0_30"] = bool(tr.get("minimum_0_30_net_met"))
        h80 = ((breach.get("around_80000") or {}).get("headroom_to_80000") or {})
        if h80:
            answers["headroom_to_visible_80000"] = {
                "distance_pct": h80.get("distance_pct"),
                "gross_at_wall_pct": (h80.get("at_wall") or {}).get("gross_headroom_pct"),
                "net_after_fees_at_wall_pct": (h80.get("at_wall") or {}).get("net_headroom_after_fees_pct"),
                "gross_requirement_met_at_80000": (h80.get("at_wall") or {}).get("gross_requirement_met"),
                "net_requirement_met_at_80000": (h80.get("at_wall") or {}).get("net_requirement_met_before_slippage"),
                "qty_base": h80.get("qty_base"),
                "notional_usdt": h80.get("notional_usdt"),
            }

    return {
        "episode_id": EPISODE_ID,
        "symbol": SYMBOL,
        "fee_contract": FEE_CONTRACT,
        "cluster_contract": CLUSTER_CONTRACT,
        "decisions": results,
        "pflicht_table": table,
        "explicit_answers": answers,
        "n_valid_decisions": len(valid),
        "any_past_only_major_barrier": any_barrier,
        "depth_insufficient_any": depth_fail,
        "detection_censored": any(r.get("coverage_status") == COVERAGE_CENSORED_DETECTION for r in results),
    }
