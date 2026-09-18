"""Run canonical MP→QDH integration for one event (reuses run_qdh_base)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from obfull_research_engine.clickhouse_research_store_v1.helpers import get_clickhouse_client
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.wall_flow_attribution import (
    band_bounds,
)
from obfull_research_engine.mp_ob_feature_enrichment_v1.loaders import (
    assess_chunks,
    chunks_overlapping,
    load_level_changes,
)
from obfull_research_engine.mp_wall_flow_qdh_silver_v1.silver_adapters import dt_to_ns, ns_to_dt
from obfull_research_engine.ob_forschungsengine_v1.event_spec import WallEventSpec, spec_from_mp_event
from obfull_research_engine.ob_forschungsengine_v1.qdh_engine import run_qdh_base
from obfull_research_engine.timeparse import format_utc_z

from . import (
    ALLOW_CLICKHOUSE_WRITES,
    BAND_TICKS,
    BASELINE_S,
    FORENSIC_TAIL_S,
    SILVER_DATABASE,
    SYMBOL,
    TICK_SIZE,
    WARMUP_S,
)
from .map_timeline import decision_snapshot_from_buckets, map_timeline_to_buckets
from .wall_select import select_defense_wall


def _assert_no_writes() -> None:
    if ALLOW_CLICKHOUSE_WRITES:
        raise RuntimeError("ClickHouse writes are forbidden in this pilot")


def _load_lcs_for_wall_select(
    client: Any,
    *,
    database: str,
    symbol: str,
    event: dict[str, Any],
    chain_version: str | None,
    coverage_start: datetime,
    zone_touch: datetime,
) -> tuple[list[Any], dict[str, Any]]:
    start_ns = dt_to_ns(coverage_start)
    end_ns = dt_to_ns(zone_touch) + 1
    assessment: dict[str, Any]
    try:
        assessment = assess_chunks(
            client,
            database=database,
            symbol=symbol,
            start=coverage_start,
            end=zone_touch,
        )
        chunk_keys = list(assessment.get("chunk_keys") or [])
    except RuntimeError as exc:
        assessment = {"status": "FALLBACK", "error": str(exc)}
        chunk_keys = []
    chain = chain_version or str(assessment.get("chain_version") or "")
    if not chunk_keys and chain:
        chunk_keys = chunks_overlapping(
            client,
            database=database,
            chain_version=chain,
            start_ns=start_ns,
            end_ns=end_ns,
        )
        assessment = {**assessment, "chain_version": chain, "chunk_keys": chunk_keys}
    if not chunk_keys:
        return [], {"ok": False, "reason": "NO_SILVER_CHUNKS", "assessment": assessment}
    lo = float(event["confluence_low"])
    hi = float(event["confluence_high"])
    pad = 50.0  # USD pad for candidate scan
    lcs = load_level_changes(
        client,
        database=database,
        symbol=symbol,
        start_ns=start_ns,
        end_ns=end_ns,
        chunk_keys=chunk_keys,
        price_min=min(lo, hi) - pad,
        price_max=max(lo, hi) + pad,
        side=None,
    )
    return list(lcs), {"ok": True, "assessment": assessment, "n_lc": len(lcs), "chunk_keys": chunk_keys}


def build_spec_with_wall(
    event: dict[str, Any],
    *,
    wall_side: str,
    wall_price: float,
    wall_id: str,
    chain_version: str | None,
    forensic_tail_s: float = FORENSIC_TAIL_S,
) -> tuple[WallEventSpec, datetime]:
    """Build WallEventSpec; extend detection_at for forensic tail data load.

    Returns (spec, original_trigger_ts). Decision cutoff remains original trigger.
    """
    base = spec_from_mp_event(
        event,
        symbol=str(event.get("symbol") or SYMBOL),
        tick_size=TICK_SIZE,
        band_ticks=BAND_TICKS,
        warmup_s=WARMUP_S,
        chain_version=chain_version,
    )
    original_trigger = base.detection_at
    forensic_end = original_trigger + timedelta(seconds=float(forensic_tail_s))
    # Rebuild with selected wall + forensic extension (analysis_end == detection_at).
    spec = WallEventSpec(
        event_id=base.event_id,
        symbol=base.symbol,
        wall_side=wall_side,
        wall_price=float(wall_price),
        tick_size=TICK_SIZE,
        band_ticks=BAND_TICKS,
        coverage_start=base.coverage_start,
        zone_available_at=base.zone_available_at,
        zone_touch_at=base.zone_touch_at,
        wall_touch_at=base.zone_touch_at,
        detection_at=forensic_end,
        zone_id=base.zone_id,
        level_id=base.level_id,
        wall_id=wall_id,
        episode_id=base.episode_id,
        chain_version=chain_version or base.chain_version,
        event_role=base.event_role,
        confluence_low=base.confluence_low,
        confluence_high=base.confluence_high,
        window_id=base.window_id,
    )
    return spec, original_trigger


def run_one_event(
    *,
    event: dict[str, Any],
    window: dict[str, Any] | None,
    case_meta: dict[str, Any] | None = None,
    out_dir: Path | None = None,
    database: str = SILVER_DATABASE,
    client: Any | None = None,
    persist_qdh: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Select wall → call existing run_qdh_base → map to canonical schemas."""
    _assert_no_writes()
    case_meta = case_meta or {}
    eid = str(event["event_id"])
    touch_ns = int(event["first_touch_ts_ns"])
    touch_at = ns_to_dt(touch_ns)
    trigger_raw = event.get("trigger_ts_ns")
    if trigger_raw in (None, "", "None"):
        return {
            "ok": False,
            "event_id": eid,
            "coverage_ok": False,
            "blocker_reason": "MISSING_TRIGGER_TS",
        }
    trigger_at = ns_to_dt(int(trigger_raw))
    chain = None
    if window:
        chain = window.get("chain_version") or None
    chain = chain or event.get("chain_version") or None
    epoch = (
        (window or {}).get("replay_epoch")
        or event.get("replay_epoch")
        or event.get("epoch_id")
        or ""
    )

    own = client is None
    client = client or get_clickhouse_client(role="mp_qdh_canonical_integration")
    try:
        # Preliminary warmup window for wall visibility
        profile_avail = event.get("profile_available_ts_ns")
        if profile_avail not in (None, "", "None"):
            zone_available = ns_to_dt(int(profile_avail))
        else:
            zone_available = touch_at - timedelta(seconds=60)
        coverage_start = min(zone_available, touch_at) - timedelta(seconds=float(WARMUP_S))

        lcs, lc_meta = _load_lcs_for_wall_select(
            client,
            database=database,
            symbol=str(event.get("symbol") or SYMBOL),
            event=event,
            chain_version=chain,
            coverage_start=coverage_start,
            zone_touch=touch_at,
        )
        if not lc_meta.get("ok"):
            return {
                "ok": False,
                "event_id": eid,
                "coverage_ok": False,
                "blocker_reason": lc_meta.get("reason") or "NO_SILVER_CHUNKS",
                "lc_meta": lc_meta,
                "case": case_meta,
            }

        touch_price = event.get("touch_price")
        tp = float(touch_price) if touch_price not in (None, "", "None") else None
        wall_sel = select_defense_wall(
            level_changes=lcs,
            event_role=str(event["event_role"]),
            confluence_low=float(event["confluence_low"]),
            confluence_high=float(event["confluence_high"]),
            zone_id=str(event.get("zone_id") or ""),
            touch_price=tp,
            zone_touch_ns=touch_ns,
            tick_size=TICK_SIZE,
        )
        band_low_c, band_high_c = (
            (None, None)
            if not wall_sel.get("ok")
            else band_bounds(float(wall_sel["wall_price"]), tick_size=TICK_SIZE, band_ticks=BAND_TICKS)
        )

        link = {
            "event_id": eid,
            "zone_id": str(event.get("zone_id") or ""),
            "replay_epoch": epoch,
            "chain_version": chain or (lc_meta.get("assessment") or {}).get("chain_version"),
            "event_role": event.get("event_role"),
            "trade_side": event.get("trade_side"),
            "label_price_only": event.get("label_price_only"),
            "confluence_low": float(event["confluence_low"]),
            "confluence_high": float(event["confluence_high"]),
            "first_touch_ts": format_utc_z(touch_at),
            "trigger_ts": format_utc_z(trigger_at),
            "source_window": event.get("window_id"),
            "wall_id": wall_sel.get("wall_id"),
            "wall_side": wall_sel.get("wall_side"),
            "wall_price": wall_sel.get("wall_price"),
            "canonical_band_low": band_low_c,
            "canonical_band_high": band_high_c,
            "band_ticks": BAND_TICKS,
            "tick_size": TICK_SIZE,
            "band_definition_source": "level_first_episode1_wall_flow_qdh_base_v1.band_bounds",
            "legacy_enrichment_band": "LEGACY_ZONE_PROXY_BPS_AGGREGATES",
            "wall_first_visible_ts": (
                format_utc_z(ns_to_dt(int(wall_sel["wall_first_visible_ts_ns"])))
                if wall_sel.get("wall_first_visible_ts_ns")
                else None
            ),
            "wall_visible_at_zone_touch": wall_sel.get("wall_visible_at_zone_touch"),
            "wall_size_at_zone_touch": wall_sel.get("wall_size_at_zone_touch"),
            "wall_rank_at_zone_touch": wall_sel.get("wall_rank_at_zone_touch"),
            "wall_selection_reason": wall_sel.get("wall_selection_reason") or wall_sel.get("detail"),
            "wall_selection_confidence": wall_sel.get("wall_selection_confidence"),
            "coverage_ok": bool(wall_sel.get("ok")),
            "blocker_reason": wall_sel.get("blocker_reason"),
            "baseline_window": {
                "start": format_utc_z(touch_at - timedelta(seconds=BASELINE_S)),
                "end": format_utc_z(touch_at),
            },
            "decision_window": {
                "start": format_utc_z(touch_at),
                "end": format_utc_z(trigger_at),
            },
            "forensic_tail_window": {
                "start": format_utc_z(trigger_at),
                "end": format_utc_z(trigger_at + timedelta(seconds=FORENSIC_TAIL_S)),
                "post_decision": True,
            },
            "case": case_meta,
            "candidates_top": wall_sel.get("candidates") or [],
        }

        dry_payload = {
            "ok": bool(wall_sel.get("ok")),
            "dry_run": True,
            "event_id": eid,
            "link": link,
            "lc_meta": {k: lc_meta.get(k) for k in ("ok", "n_lc", "reason")},
            "planned_qdh_call": "ob_forschungsengine_v1.qdh_engine.run_qdh_base",
            "db_mutation": False,
        }
        if dry_run or not wall_sel.get("ok"):
            if not wall_sel.get("ok"):
                dry_payload["ok"] = False
                dry_payload["coverage_ok"] = False
                dry_payload["blocker_reason"] = wall_sel.get("blocker_reason") or "WALL_SELECTION_UNRESOLVED"
            return dry_payload

        spec, _orig_trig = build_spec_with_wall(
            event,
            wall_side=str(wall_sel["wall_side"]),
            wall_price=float(wall_sel["wall_price"]),
            wall_id=str(wall_sel["wall_id"]),
            chain_version=chain or link.get("chain_version"),
        )
        qdh_out_dir = None
        if persist_qdh and out_dir is not None:
            qdh_out_dir = Path(out_dir) / "per_event" / eid / "qdh_base"
            qdh_out_dir.mkdir(parents=True, exist_ok=True)

        qdh = run_qdh_base(
            spec,
            out_dir=qdh_out_dir,
            database=database,
            client=client,
            persist=bool(persist_qdh and qdh_out_dir is not None),
        )
        if not qdh.get("ok"):
            link["coverage_ok"] = False
            link["blocker_reason"] = str(qdh.get("reason") or qdh.get("verdict") or "QDH_BLOCKED")
            return {
                "ok": False,
                "event_id": eid,
                "coverage_ok": False,
                "blocker_reason": link["blocker_reason"],
                "link": link,
                "qdh": {"ok": False, "verdict": qdh.get("verdict"), "reason": qdh.get("reason")},
                "case": case_meta,
            }

        content = qdh.get("content") or {}
        timeline = qdh.get("timeline") or []
        q_touch = (content.get("wall") or {}).get("queue_band_at_wall_touch")
        buckets = map_timeline_to_buckets(
            event_id=eid,
            wall_id=str(wall_sel["wall_id"]),
            timeline=timeline,
            trigger_ts=trigger_at,
            first_touch_ts=touch_at,
            wall_side=str(wall_sel["wall_side"]),
            queue_at_touch=float(q_touch) if q_touch is not None else wall_sel.get("wall_size_at_zone_touch"),
        )
        # Mechanism flags (descriptive only)
        pre = [b for b in buckets if not b.get("post_decision")]
        last = pre[-1] if pre else None
        mech = {
            "queue_survived": bool(last and (last.get("queue_remaining_qty") or 0) > 0),
            "queue_exhausted": bool(last and last.get("queue_state") == "QUEUE_EXHAUSTED"),
            "refill_observed": bool(any(float(b.get("refill_qty") or 0) > 0 for b in pre)),
            "pull_dominant": False,
            "fill_dominant": False,
            "microprice_returned_to_defender_side": None,
            "attack_accelerating": last.get("attack_accelerating") if last else None,
            "attack_decelerating": last.get("attack_decelerating") if last else None,
        }
        if last:
            fill = float(last.get("attributed_fill_qty") or 0)
            pull = float(last.get("residual_pull_qty") or 0)
            mech["pull_dominant"] = pull > fill
            mech["fill_dominant"] = fill >= pull and fill > 0

        snap = decision_snapshot_from_buckets(
            event_id=eid,
            wall_id=str(wall_sel["wall_id"]),
            trigger_ts=trigger_at,
            buckets=buckets,
            coverage_ok=True,
            extra={"mechanism_flags": mech, "case": case_meta},
        )

        trade_dedup = content.get("trade_dedup") or {}
        band_stats = content.get("band_stats") or {}
        exact_stats = content.get("exact_stats") or {}

        return {
            "ok": True,
            "event_id": eid,
            "coverage_ok": True,
            "blocker_reason": None,
            "link": link,
            "buckets": buckets,
            "decision_snapshot": snap,
            "mechanism_flags": mech,
            "qdh_manifest": qdh.get("manifest"),
            "trade_dedup": trade_dedup,
            "band_stats": band_stats,
            "exact_stats": exact_stats,
            "anchors": content.get("anchors"),
            "counts": content.get("counts"),
            "case": case_meta,
            "microprice_note": "MICROPRICE_PROXY if depth sizes missing in metrics; engine uses bid/ask depth proxies",
            "ofi_reclaim": "NOT_IMPLEMENTED",
        }
    finally:
        if own:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
