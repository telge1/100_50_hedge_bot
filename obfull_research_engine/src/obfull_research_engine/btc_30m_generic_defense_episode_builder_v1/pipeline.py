"""Orchestrate: zones→visits→clusters→enrich→funnel→write features/ and outcomes/."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.episodes import parse_utc
from ..bounded_level_first_analyzer_pilot_v1.persist import (
    atomic_write_json,
    atomic_write_jsonl,
)
from ..timeparse import format_utc_z
from . import BOOK_SOURCE, CONTRACT_HASH, RUN_PREFIX, SCHEMA_VERSION
from .attack_clusters import group_attack_clusters
from .config import BuilderConfig, assert_no_forbidden_calc_inputs
from .decision_snapshots import build_decision_snapshots
from .defense_chain_stage import run_defense_chain_stage
from .exclusions import (
    BOOK_COVERAGE_GAP,
    BOOK_REPLAY_FAILED,
    HANDOFF_VALIDATION_FAILED,
    NO_WALL_CANDIDATE,
    NO_WALL_TOUCH,
    NO_ZONE_TOUCH,
    WALL_FLOW_FROZEN_ASK_ONLY_V1,
    exclusion_row,
)
from .funnel import FunnelCounters
from .handoff import HandoffError, build_defense_handoff
from .hashing import sha256_json, source_manifest_hash
from .oracle import build_oracle_report, rediscover_episode1_visit
from .outcomes_apply import apply_outcomes_for_episode, verify_contract_hash
from .persist_book import persist_episode_book
from .price_response_stage import run_price_response_stage
from .report import write_reports
from .touches import enrich_cluster_touches
from .visits import detect_all_visits_for_zones, load_jsonl, trades_as_events
from .wall_flow_stage import run_wall_flow_stage
from .walls_past_only import select_walls_at_zone_touch
from .zones import load_zones


def _price_marks_from_trades(trades: list[dict[str, Any]]) -> list[tuple[datetime, float]]:
    marks: list[tuple[datetime, float]] = []
    for t in trades:
        ts = t.get("trade_ts") or t.get("ts")
        if ts is None or t.get("price") is None:
            continue
        marks.append((parse_utc(ts), float(t["price"])))
    marks.sort(key=lambda x: x[0])
    return marks


def _zone_by_id(zones: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(z["zone_id"]): z for z in zones}


def cluster_is_valid_for_enrichment(result: dict[str, Any]) -> bool:
    """Valid iff zone_touch and ≥1 wall_touch within continuous book coverage."""
    if not result.get("zone_touch"):
        return False
    wts = result.get("wall_touches") or {}
    if not wts:
        return False
    if result.get("book_ok") is False:
        return False
    return True


def run_builder(
    cfg: BuilderConfig,
    *,
    dry_discovery_only: bool = False,
    synthetic: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Full pipeline. Features written under features/, outcomes under outcomes/.

    If synthetic is provided (tests), skip disk IO for zones/trades/candles/replay
    and use the injected objects.
    """
    assert_no_forbidden_calc_inputs(cfg)
    contract_hash = verify_contract_hash()

    window_start = parse_utc(cfg.window_start)
    window_end = parse_utc(cfg.window_end)

    if synthetic:
        zones = list(synthetic.get("zones") or [])
        trades = list(synthetic.get("trades") or [])
        candles = list(synthetic.get("candles") or [])
        mid_events = list(synthetic.get("mid_events") or [])
    else:
        zones = load_zones(
            mp_events_path=cfg.mp_events_path,
            level_clusters_path=cfg.level_clusters_path,
        )
        trades = load_jsonl(cfg.trades_path)
        candles = load_jsonl(cfg.candles_path)
        mid_events = []

    funnel = FunnelCounters()
    funnel.n_closed_30m_zones = len(zones)
    exclusions: list[dict[str, Any]] = []

    visits = detect_all_visits_for_zones(
        zones=zones,
        trade_events=trades,
        candles_1m=candles,
        window_start=window_start,
        window_end=window_end,
        mid_events=mid_events,
    )
    funnel.n_visits = len(visits)

    clusters = group_attack_clusters(visits, gap_ms=cfg.attack_cluster_gap_ms)
    funnel.n_attack_clusters = len(clusters)

    cfg_dict = cfg.to_dict()
    run_key = cfg.run_key
    if not run_key:
        run_key = f"{RUN_PREFIX}{sha256_json({**cfg_dict, 'schema': SCHEMA_VERSION})[:16]}"
    elif not str(run_key).startswith(RUN_PREFIX):
        run_key = f"{RUN_PREFIX}{run_key}"
    cfg.run_key = run_key
    out_dir = cfg.out_dir()
    features_dir = out_dir / "features"
    outcomes_dir = out_dir / "outcomes"
    features_dir.mkdir(parents=True, exist_ok=True)
    outcomes_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_key": run_key,
        "book_source": BOOK_SOURCE,
        "outcome_contract_hash": contract_hash,
        "source_manifest": cfg.source_manifest(),
        "source_manifest_hash": source_manifest_hash(cfg.source_manifest()),
        "window_start": cfg.window_start,
        "window_end": cfg.window_end,
        "max_pilot_clusters": cfg.max_pilot_clusters,
        "note_no_visit_count_selector": cfg.note_no_visit_count_selector,
        "features_dir": str(features_dir),
        "outcomes_dir": str(outcomes_dir),
        "created_at": format_utc_z(datetime.now(timezone.utc)),
    }
    atomic_write_json(out_dir / "run_manifest.json", manifest)
    atomic_write_jsonl(features_dir / "zones.jsonl", zones)
    atomic_write_jsonl(features_dir / "visits.jsonl", visits)
    atomic_write_jsonl(
        features_dir / "attack_clusters.jsonl",
        [{k: v for k, v in c.items() if k != "visits"} | {"episode_ids": c["episode_ids"]} for c in clusters],
    )

    zone_map = _zone_by_id(zones)
    price_marks = _price_marks_from_trades(trades)
    trade_events = trades_as_events(trades)

    # Episode-1 rediscovery (assert-only, after auto discovery listing exists)
    rediscovery = None
    ep1_zone = next(
        (
            z
            for z in zones
            if z.get("persistent_cluster_id") == "pc_7775a856ab22f006"
            or z.get("zone_id") == "pc_7775a856ab22f006"
        ),
        None,
    )
    if ep1_zone is not None:
        rediscovery = rediscover_episode1_visit(
            zone=ep1_zone,
            trade_events=trade_events,
            candles_1m=candles,
            window_start=window_start,
            window_end=window_end,
            mid_events=mid_events,
        )

    if dry_discovery_only:
        oracle = build_oracle_report(snapshots=[], rediscovery=rediscovery)
        write_reports(
            out_dir,
            funnel=funnel.to_dict(),
            oracle=oracle,
            exclusions=exclusions,
            manifest=manifest,
        )
        return {
            "ok": True,
            "run_key": run_key,
            "out_dir": str(out_dir),
            "funnel": funnel.to_dict(),
            "oracle": oracle,
            "n_clusters": len(clusters),
            "dry_discovery_only": True,
        }

    enriched_summary: list[dict[str, Any]] = []
    all_snapshots: list[dict[str, Any]] = []
    valid_taken = 0

    for cluster in clusters:
        if valid_taken >= int(cfg.max_pilot_clusters):
            break
        zid = str(cluster["zone_id"])
        zone = zone_map.get(zid)
        if zone is None:
            # reconstruct minimal zone from cluster
            zone = {
                "zone_id": zid,
                "zone_low": cluster.get("zone_low"),
                "zone_high": cluster.get("zone_high"),
                "cluster_price_low": cluster.get("zone_low"),
                "cluster_price_high": cluster.get("zone_high"),
                "zone_available_at": cluster.get("zone_available_at"),
                "level_cluster_id": cluster.get("persistent_cluster_id") or zid,
                "persistent_cluster_id": cluster.get("persistent_cluster_id") or zid,
                "tpo_type": None,
                "member_level_types": [],
            }
        primary = cluster.get("primary_visit")
        if not primary:
            exclusions.append(
                exclusion_row(
                    reason=NO_ZONE_TOUCH,
                    subject_id=cluster["attack_cluster_id"],
                    stage="validity",
                )
            )
            funnel.bump_exclusion(NO_ZONE_TOUCH)
            continue

        # Persist / replay
        det_ts = None
        persist_out = out_dir / "book" / cluster["attack_cluster_id"]
        replay = None
        book_ok = True
        if synthetic and synthetic.get("replay") is not None:
            replay = synthetic["replay"]
            persist_meta = {
                "ok": True,
                "reused": False,
                "persist_dir": str(persist_out),
                "replay": replay,
            }
        else:
            persist_meta = persist_episode_book(
                symbol=cfg.symbol,
                out_dir=persist_out,
                zone_touch_ts=primary["first_touch_ts"],
                detection_ts=det_ts,
                archive_root=cfg.full_ob_archive,
                trades=trades,
                existing_persist_fallback=cfg.existing_persist_fallback,
                allow_reuse=True,
            )
            if not persist_meta.get("ok"):
                exclusions.append(
                    exclusion_row(
                        reason=BOOK_REPLAY_FAILED,
                        subject_id=cluster["attack_cluster_id"],
                        detail=str(persist_meta.get("error")),
                        stage="persist",
                    )
                )
                funnel.bump_exclusion(BOOK_REPLAY_FAILED)
                continue
            replay = persist_meta.get("replay")
            if replay is None and persist_meta.get("reused"):
                # Reused persist without in-memory replay — wall selection needs replay.
                # Fail closed unless synthetic injected.
                exclusions.append(
                    exclusion_row(
                        reason=BOOK_COVERAGE_GAP,
                        subject_id=cluster["attack_cluster_id"],
                        detail="reused persist without in-memory replay for wall selection",
                        stage="persist",
                    )
                )
                funnel.bump_exclusion(BOOK_COVERAGE_GAP)
                continue

        from .visits import build_zone_touch_from_visit

        zone_touch = build_zone_touch_from_visit(primary, zone=zone, trades=trades)
        walls = select_walls_at_zone_touch(
            replay,
            zone_touch_exchange_time=zone_touch["exchange_event_time"],
            zone_low=float(zone["zone_low"]),
            zone_high=float(zone["zone_high"]),
            tick_size=cfg.tick_size,
            distance_cap_ticks=cfg.wall_distance_cap_ticks,
        )
        if not walls.get("ok"):
            exclusions.append(
                exclusion_row(
                    reason=BOOK_REPLAY_FAILED,
                    subject_id=cluster["attack_cluster_id"],
                    detail=str(walls.get("detail")),
                    stage="walls",
                )
            )
            funnel.bump_exclusion(BOOK_REPLAY_FAILED)
            continue
        if not walls.get("has_any_wall"):
            exclusions.append(
                exclusion_row(
                    reason=NO_WALL_CANDIDATE,
                    subject_id=cluster["attack_cluster_id"],
                    stage="walls",
                )
            )
            funnel.bump_exclusion(NO_WALL_CANDIDATE)
            continue

        touch_pack = enrich_cluster_touches(
            attack_cluster=cluster,
            zone=zone,
            trades=trades,
            candles_1m=candles,
            price_marks=price_marks,
            window_end=window_end,
            replay=replay,
            ask_wall=walls.get("ask_wall"),
            bid_wall=walls.get("bid_wall"),
        )
        touch_pack["book_ok"] = book_ok and walls.get("ok", False)
        if not cluster_is_valid_for_enrichment(touch_pack):
            reason = NO_WALL_TOUCH if touch_pack.get("zone_touch") else NO_ZONE_TOUCH
            exclusions.append(
                exclusion_row(
                    reason=reason,
                    subject_id=cluster["attack_cluster_id"],
                    detail=str(touch_pack.get("errors")),
                    stage="validity",
                )
            )
            funnel.bump_exclusion(reason)
            continue

        # Take this valid cluster (chronological order already)
        valid_taken += 1
        funnel.n_valid_clusters += 1
        funnel.n_enriched += 1

        # Prefer ask wall for primary enrichment path; also process bid discovery/outcomes.
        sides_to_process: list[tuple[str, dict[str, Any]]] = []
        if "ask" in touch_pack["wall_touches"]:
            sides_to_process.append(("ask", touch_pack["wall_touches"]["ask"]))
        if "bid" in touch_pack["wall_touches"]:
            sides_to_process.append(("bid", touch_pack["wall_touches"]["bid"]))

        cluster_feat_dir = features_dir / cluster["attack_cluster_id"]
        cluster_out_dir = outcomes_dir / cluster["attack_cluster_id"]
        cluster_feat_dir.mkdir(parents=True, exist_ok=True)
        cluster_out_dir.mkdir(parents=True, exist_ok=True)

        atomic_write_json(cluster_feat_dir / "zone_touch.json", touch_pack["zone_touch"])
        atomic_write_json(cluster_feat_dir / "detection.json", touch_pack["detection"])
        atomic_write_json(cluster_feat_dir / "wall_candidates.json", walls)
        atomic_write_json(cluster_feat_dir / "wall_touches.json", touch_pack["wall_touches"])

        for side, wall_touch in sides_to_process:
            side_feat = cluster_feat_dir / side
            side_out = cluster_out_dir / side
            side_feat.mkdir(parents=True, exist_ok=True)
            side_out.mkdir(parents=True, exist_ok=True)
            try:
                handoff = build_defense_handoff(
                    symbol=cfg.symbol,
                    zone=zone,
                    zone_touch=touch_pack["zone_touch"],
                    wall_touch=wall_touch,
                    detection=touch_pack["detection"],
                )
            except HandoffError as exc:
                exclusions.append(
                    exclusion_row(
                        reason=HANDOFF_VALIDATION_FAILED,
                        subject_id=cluster["attack_cluster_id"],
                        detail=str(exc),
                        stage="handoff",
                    )
                )
                continue

            atomic_write_json(side_feat / "handoff.json", handoff.to_dict())

            persist_dir = Path(persist_meta.get("persist_dir") or persist_out)
            wf_result: dict[str, Any]
            if side == "ask":
                wf_result = run_wall_flow_stage(
                    handoff=handoff,
                    persist_dir=persist_dir,
                    trades_path=Path(cfg.trades_path),
                    out_dir=side_feat / "wall_flow",
                    run_key=f"{run_key}_{cluster['attack_cluster_id']}_ask",
                    band_ticks=cfg.band_ticks,
                    tick_size=cfg.tick_size,
                )
                if wf_result.get("ok"):
                    funnel.n_ask_wall_flow += 1
            else:
                wf_result = {
                    "ok": False,
                    "skipped": True,
                    "exclusion": exclusion_row(
                        reason=WALL_FLOW_FROZEN_ASK_ONLY_V1,
                        subject_id=handoff.episode_id,
                        stage="wall_flow",
                    ),
                }
                funnel.n_bid_wall_flow_skipped += 1
                exclusions.append(wf_result["exclusion"])

            pr_result = run_price_response_stage(
                handoff=handoff,
                persist_dir=persist_dir,
                out_dir=side_feat / "price_response",
                run_key=f"{run_key}_{cluster['attack_cluster_id']}_{side}",
                wall_flow_timeline=Path(wf_result["timeline_path"])
                if wf_result.get("timeline_path")
                else None,
            )
            if pr_result.get("ok"):
                funnel.n_price_response_ok += 1

            # Defense chain best-effort: build generation rows from replay when possible
            generations_for_chain: list[dict[str, Any]] | None = None
            if side == "ask" and replay is not None:
                try:
                    from ..level_first_episode1_touch_detection_independent_v1.wall_generations import (
                        build_wall_generations,
                    )

                    gens = build_wall_generations(
                        level_changes=replay.get("level_changes") or [],
                        book_resets=replay.get("book_resets"),
                        initial_asks=replay.get("initial_asks") or {},
                        initial_bids=replay.get("initial_bids") or {},
                        initial_replay_epoch=replay.get("initial_replay_epoch"),
                        window_start=parse_utc(replay.get("window_start") or cfg.window_start),
                        wall_price=float(handoff.wall_price),
                        wall_side="ask",
                    )
                    generations_for_chain = [
                        {
                            "start_time": format_utc_z(g.generation_start_exchange_time),
                            "end_time": None
                            if g.generation_end_exchange_time is None
                            else format_utc_z(g.generation_end_exchange_time),
                            "price": float(handoff.wall_price),
                            "generation_index": g.generation_index,
                            "initial_qty": float(handoff.qty_at_zone_touch),
                            "wall_generation_id": g.generation_id(),
                            "replay_epoch": g.replay_epoch,
                        }
                        for g in gens
                    ]
                except Exception:  # noqa: BLE001
                    generations_for_chain = None

            dc_result = run_defense_chain_stage(
                wall_side=side,
                generations=generations_for_chain,
                wall_flow_rows=None,
                liquidity_rows=None,
                replay_epoch=int(handoff.replay_epoch),
                coverage_start=cfg.window_start,
                coverage_end=cfg.window_end,
                wall_price=float(handoff.wall_price),
                episode_id=handoff.episode_id,
            )
            if dc_result.get("ok"):
                funnel.n_defense_chain_ok += 1
            elif dc_result.get("exclusion"):
                exclusions.append(dc_result["exclusion"])

            # Timeline rows for snapshots/outcomes (prefer price-response / wall-flow if present)
            timeline_rows: list[dict[str, Any]] = []
            if pr_result.get("ok"):
                # Try load written timeline if any
                pr_dir = Path(pr_result.get("out_dir") or (side_feat / "price_response"))
                for name in ("price_response_timeline.csv", "feature_timeline.csv"):
                    p = pr_dir / name
                    if p.exists():
                        import csv

                        with p.open(encoding="utf-8") as fh:
                            timeline_rows = list(csv.DictReader(fh))
                        break

            snapshots = build_decision_snapshots(
                episode_id=handoff.episode_id,
                attack_cluster_id=cluster["attack_cluster_id"],
                zone_touch=touch_pack["zone_touch"],
                wall_touch=wall_touch,
                detection=touch_pack["detection"],
                timeline_rows=timeline_rows,
                wall_side=side,
                replay_epoch=int(handoff.replay_epoch),
                source_manifest_hash=manifest["source_manifest_hash"],
                defense_chain_id=(dc_result.get("chain") or {}).get("chain_id")
                if isinstance(dc_result.get("chain"), dict)
                else None,
                book_checkpoint_id=(walls.get("book") or {}).get("checkpoint_id"),
                wall_candidates_reference=sha256_json(
                    {"ask": walls.get("ask_wall"), "bid": walls.get("bid_wall")}
                )[:16],
            )
            atomic_write_jsonl(side_feat / "decision_snapshots.jsonl", snapshots)
            all_snapshots.extend(snapshots)
            funnel.n_feature_snapshots += len(snapshots)

            coverage_end = persist_meta.get("window_end") or cfg.window_end
            outcomes = apply_outcomes_for_episode(
                episode_id=handoff.episode_id,
                symbol=cfg.symbol,
                zone_id=str(zone["zone_id"]),
                wall_side=side,
                wall_price=float(handoff.wall_price),
                zone_touch=touch_pack["zone_touch"],
                wall_touch=wall_touch,
                detection=touch_pack["detection"],
                timeline_rows=timeline_rows,
                coverage_end=coverage_end,
                attack_cluster_id=cluster["attack_cluster_id"],
                chain=dc_result.get("chain"),
                wall_generation_id=handoff.wall_generation_id,
                replay_epoch=int(handoff.replay_epoch),
            )
            atomic_write_jsonl(side_out / "outcomes_complete.jsonl", outcomes.get("complete") or [])
            atomic_write_jsonl(side_out / "outcomes_censored.jsonl", outcomes.get("censored") or [])
            atomic_write_json(side_out / "outcomes_summary.json", {
                "ok": outcomes.get("ok"),
                "n_complete": outcomes.get("n_complete"),
                "n_censored": outcomes.get("n_censored"),
                "outcome_contract_hash": outcomes.get("outcome_contract_hash") or CONTRACT_HASH,
            })
            funnel.n_outcome_complete += int(outcomes.get("n_complete") or 0)
            funnel.n_outcome_censored += int(outcomes.get("n_censored") or 0)

            enriched_summary.append(
                {
                    "attack_cluster_id": cluster["attack_cluster_id"],
                    "episode_id": handoff.episode_id,
                    "wall_side": side,
                    "wall_price": handoff.wall_price,
                    "wall_flow_ok": bool(wf_result.get("ok")),
                    "price_response_ok": bool(pr_result.get("ok")),
                    "defense_chain_ok": bool(dc_result.get("ok")),
                    "n_snapshots": len(snapshots),
                    "n_outcomes_complete": outcomes.get("n_complete"),
                    "n_outcomes_censored": outcomes.get("n_censored"),
                }
            )

    oracle = build_oracle_report(snapshots=all_snapshots, rediscovery=rediscovery)
    write_reports(
        out_dir,
        funnel=funnel.to_dict(),
        oracle=oracle,
        exclusions=exclusions,
        manifest=manifest,
        enriched_summary=enriched_summary,
    )
    atomic_write_jsonl(out_dir / "exclusions.jsonl", exclusions)

    return {
        "ok": True,
        "run_key": run_key,
        "out_dir": str(out_dir),
        "features_dir": str(features_dir),
        "outcomes_dir": str(outcomes_dir),
        "funnel": funnel.to_dict(),
        "oracle": oracle,
        "n_valid_enriched": valid_taken,
        "outcome_contract_hash": contract_hash,
    }
