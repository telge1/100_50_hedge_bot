"""Silver → wall-flow / QDH_base orchestration (reuses proven Episode-1 modules)."""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from obfull_research_engine.bounded_level_first_analyzer_pilot_v1.persist import (
    atomic_write_json,
    atomic_write_text,
)
from obfull_research_engine.clickhouse_research_store_v1.helpers import get_clickhouse_client
from obfull_research_engine.drilldown.aggregation_100ms import _as_dt
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1 import (
    M_LIQ,
    M_OI,
    WALL_STATE_NOT_CLASSIFIED,
)
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.pipeline import (
    _anchor_row,
    queue_at_price,
)
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.timeline_100ms import (
    build_feature_timeline,
)
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.wall_flow_attribution import (
    attribute_intervals,
    band_bounds,
    build_band_nodes,
    build_exact_price_nodes,
    wall_flow_event_to_row,
)
from obfull_research_engine.mp_ob_feature_enrichment_v1.loaders import (
    assess_chunks,
    load_level_changes,
    load_metrics_enriched,
    load_public_trades,
)
from obfull_research_engine.timeparse import format_utc_z

from . import (
    ALLOW_CLICKHOUSE_WRITES,
    ANALYSIS_END_UTC,
    AUDIT_ID,
    BAND_TICKS_DEFAULT,
    BTC_CHAIN_VERSION,
    CONTRACT_VERSION,
    COVERAGE_START_UTC,
    DETECTION_UTC,
    EPISODE_ID,
    LEVEL_ID,
    SCHEMA_VERSION,
    SILVER_DATABASE,
    SYMBOL,
    TICK_SIZE,
    VERDICT_BLOCKED,
    VERDICT_EVENT_TIME,
    WALL_ID,
    WALL_PRICE,
    WALL_SIDE,
    WALL_TOUCH_UTC,
    ZONE_AVAILABLE_UTC,
    ZONE_ID,
    ZONE_TOUCH_UTC,
)
from .silver_adapters import (
    build_trades_for_qdh,
    dt_to_ns,
    level_changes_to_qdh_rows,
    metrics_to_qdh_states,
    seed_initial_asks_from_level_changes,
)


def _parse_z(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _queue_asof(nodes: list[Any], ts: datetime) -> float:
    q = nodes[0].queue
    for n in nodes:
        if n.exchange_event_time <= ts:
            q = n.queue
        else:
            break
    return float(q)


def run_episode1_silver_qdh(
    *,
    out_dir: Path,
    database: str = SILVER_DATABASE,
    band_ticks: int = BAND_TICKS_DEFAULT,
    client: Any | None = None,
) -> dict[str, Any]:
    if ALLOW_CLICKHOUSE_WRITES:
        raise RuntimeError("CH writes forbidden")
    t0 = time.monotonic()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    coverage_start = _parse_z(COVERAGE_START_UTC)
    zone_touch_at = _parse_z(ZONE_TOUCH_UTC)
    wall_touch_at = _parse_z(WALL_TOUCH_UTC)
    detection_at = _parse_z(DETECTION_UTC)
    analysis_end = _parse_z(ANALYSIS_END_UTC)
    zone_available = _parse_z(ZONE_AVAILABLE_UTC)

    start_ns = dt_to_ns(coverage_start)
    end_ns = dt_to_ns(analysis_end)
    band_low, band_high = band_bounds(WALL_PRICE, tick_size=TICK_SIZE, band_ticks=band_ticks)

    own = client is None
    client = client or get_clickhouse_client(role="mp_wall_flow_qdh_silver_v1")
    try:
        try:
            assessment = assess_chunks(
                client,
                database=database,
                symbol=SYMBOL,
                start=coverage_start,
                end=analysis_end,
            )
            chunk_keys = list(assessment.get("chunk_keys") or [])
        except RuntimeError as exc:
            assessment = {"status": "FALLBACK", "error": str(exc)}
            chunk_keys = []
        if not chunk_keys:
            from obfull_research_engine.mp_ob_feature_enrichment_v1.loaders import (
                chunks_overlapping,
            )

            chunk_keys = chunks_overlapping(
                client,
                database=database,
                chain_version=BTC_CHAIN_VERSION,
                start_ns=start_ns,
                end_ns=end_ns,
            )
        if not chunk_keys:
            payload = {
                "ok": False,
                "verdict": VERDICT_BLOCKED,
                "reason": "NO_SILVER_CHUNKS",
                "assessment": assessment,
            }
            atomic_write_json(out_dir / "run_manifest.json", payload)
            return payload

        # Price band slightly wider for LC seed walk; attribution uses band_ticks.
        price_pad = float(band_ticks + 2) * float(TICK_SIZE)
        lc_events = load_level_changes(
            client,
            database=database,
            symbol=SYMBOL,
            start_ns=start_ns,
            end_ns=end_ns,
            chunk_keys=chunk_keys,
            price_min=WALL_PRICE - price_pad,
            price_max=WALL_PRICE + price_pad,
            side=WALL_SIDE,
        )
        metrics = load_metrics_enriched(
            client,
            database=database,
            symbol=SYMBOL,
            start_ns=dt_to_ns(zone_available),
            end_ns=end_ns - 1,
            chunk_keys=chunk_keys,
        )
        trades_xray = load_public_trades(
            client, symbol=SYMBOL, start_ns=start_ns, end_ns=end_ns
        )
    finally:
        if own:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    lc_rows_all = level_changes_to_qdh_rows(lc_events)
    # Nodes consume LCs at/after coverage_start; seed uses pre-coverage too.
    initial_asks = seed_initial_asks_from_level_changes(
        lc_rows_all,
        band_low=band_low,
        band_high=band_high,
        wall_side=WALL_SIDE,
        coverage_start=coverage_start,
    )
    lc_rows = [
        r for r in lc_rows_all if _as_dt(r["event_time"]) >= coverage_start
    ]
    states = metrics_to_qdh_states(metrics)
    if not states:
        payload = {
            "ok": False,
            "verdict": VERDICT_BLOCKED,
            "reason": "NO_SILVER_METRICS",
            "n_lc": len(lc_rows),
            "n_trades": len(trades_xray),
        }
        atomic_write_json(out_dir / "run_manifest.json", payload)
        return payload

    trades, dedup, _dropped = build_trades_for_qdh(
        trades_xray, symbol=SYMBOL, source_file="orderbook_analysis.public_trades_canonical"
    )

    init_et = coverage_start
    init_avail = states[0]["available_at"]
    q0_exact = float(initial_asks.get(WALL_PRICE) or queue_at_price(initial_asks, WALL_PRICE))

    exact_nodes, exact_meta = build_exact_price_nodes(
        initial_queue=q0_exact,
        initial_event_time=init_et,
        initial_available_at=_as_dt(init_avail),
        initial_replay_epoch=None,
        level_changes=lc_rows,
        wall_price=WALL_PRICE,
        wall_side=WALL_SIDE,
    )
    band_nodes, band_meta = build_band_nodes(
        initial_asks=initial_asks,
        initial_event_time=init_et,
        initial_available_at=_as_dt(init_avail),
        initial_replay_epoch=None,
        level_changes=lc_rows,
        band_low=band_low,
        band_high=band_high,
        wall_side=WALL_SIDE,
    )

    wall_visible_at = zone_available
    exact_events, exact_stats = attribute_intervals(
        nodes=exact_nodes,
        trades=trades,
        view="exact_price",
        wall_price=WALL_PRICE,
        band_low=band_low,
        band_high=band_high,
        wall_visible_at=wall_visible_at,
        analysis_end_exclusive=analysis_end,
    )
    band_events, band_stats = attribute_intervals(
        nodes=band_nodes,
        trades=trades,
        view="defended_band",
        wall_price=WALL_PRICE,
        band_low=band_low,
        band_high=band_high,
        wall_visible_at=wall_visible_at,
        analysis_end_exclusive=analysis_end,
    )

    q_exact_touch = _queue_asof(exact_nodes, wall_touch_at)
    q_band_touch = _queue_asof(band_nodes, wall_touch_at)

    timeline, tl_meta = build_feature_timeline(
        states=states,
        exact_events=exact_events,
        band_events=band_events,
        wall_touch_at=wall_touch_at,
        zone_touch_at=zone_touch_at,
        detection_at=detection_at,
        queue_exact_at_wall_touch=q_exact_touch,
        queue_band_at_wall_touch=q_band_touch,
        wall_price=WALL_PRICE,
    )

    zone_avail = zone_available
    wall_touch_avail = wall_touch_at
    det_avail = detection_at
    anchors = {
        "zone_touch": _anchor_row(timeline, available_at=zone_touch_at, mode="at_or_after"),
        "wall_touch": _anchor_row(timeline, available_at=wall_touch_avail, mode="at_or_after"),
        "first_after_wall_touch": _anchor_row(
            timeline, available_at=wall_touch_avail, mode="at_or_after"
        ),
        "immediately_before_episode_detection": _anchor_row(
            timeline, available_at=det_avail, mode="last_before"
        ),
        "episode_detection": _anchor_row(timeline, available_at=det_avail, mode="at_or_before"),
    }

    content = {
        "schema_version": SCHEMA_VERSION,
        "audit_id": AUDIT_ID,
        "contract_version": CONTRACT_VERSION,
        "episode_id": EPISODE_ID,
        "symbol": SYMBOL,
        "zone_id": ZONE_ID,
        "level_id": LEVEL_ID,
        "data_source": {
            "silver_database": database,
            "level_changes": f"{database}.ob_level_changes_v1_3",
            "metrics": f"{database}.ob_metrics_100ms_v1_3",
            "public_trades": "orderbook_analysis.public_trades_canonical",
            "chunk_keys": chunk_keys,
            "n_chunks": len(chunk_keys),
        },
        "wall": {
            "wall_id": WALL_ID,
            "wall_side": WALL_SIDE,
            "wall_price": WALL_PRICE,
            "tick_size": TICK_SIZE,
            "band_ticks": band_ticks,
            "band_low": band_low,
            "band_high": band_high,
            "wall_visible_at": format_utc_z(wall_visible_at),
            "zone_touch_at": format_utc_z(zone_touch_at),
            "wall_touch_at": format_utc_z(wall_touch_at),
            "coverage_start": format_utc_z(coverage_start),
            "coverage_end": format_utc_z(analysis_end),
            "queue_exact_at_wall_touch": q_exact_touch,
            "queue_band_at_wall_touch": q_band_touch,
            "initial_queue_exact": q0_exact,
            "initial_asks_band": initial_asks,
            "WALL_STATE": WALL_STATE_NOT_CLASSIFIED,
        },
        "trade_dedup": {
            "raw_count": dedup.raw_count,
            "unique_count": dedup.unique_count,
            "duplicate_count": dedup.duplicate_count,
            "rejected_missing_trade_id": dedup.rejected_missing_trade_id,
            "identity_rule": dedup.identity_rule,
            "receive_time_present_count": dedup.receive_time_present_count,
            "receive_time_missing_count": dedup.receive_time_missing_count,
        },
        "exact_meta": exact_meta,
        "band_meta": band_meta,
        "exact_stats": exact_stats,
        "band_stats": band_stats,
        "timeline_meta": tl_meta,
        "M_OI": M_OI,
        "M_LIQ": M_LIQ,
        "anchors": anchors,
        "counts": {
            "n_level_changes": len(lc_rows),
            "n_metrics_buckets": len(states),
            "n_raw_trades": len(trades_xray),
            "n_timeline_rows": len(timeline),
            "n_exact_events": len(exact_events),
            "n_band_events": len(band_events),
        },
        "elapsed_s": round(time.monotonic() - t0, 3),
    }

    # Persist artifacts
    atomic_write_json(out_dir / "wall_flow_summary.json", content)
    atomic_write_json(
        out_dir / "wall_flow_events_exact.json",
        [wall_flow_event_to_row(e) for e in exact_events],
    )
    atomic_write_json(
        out_dir / "wall_flow_events_band.json",
        [wall_flow_event_to_row(e) for e in band_events],
    )
    if timeline:
        with (out_dir / "feature_timeline.csv").open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(timeline[0].keys()))
            writer.writeheader()
            writer.writerows(timeline)

    look_ahead_ok = int(tl_meta.get("look_ahead_violations_bucket_contract") or 0) == 0
    mass_ok = int(exact_stats.get("mass_balance_violations") or 0) == 0 and int(
        band_stats.get("mass_balance_violations") or 0
    ) == 0
    ok = look_ahead_ok and mass_ok and len(timeline) > 0
    verdict = VERDICT_EVENT_TIME if ok else VERDICT_BLOCKED
    manifest = {
        "ok": ok,
        "verdict": verdict,
        "audit_id": AUDIT_ID,
        "schema_version": SCHEMA_VERSION,
        "episode_id": EPISODE_ID,
        "symbol": SYMBOL,
        "look_ahead_ok": look_ahead_ok,
        "mass_balance_ok": mass_ok,
        "counts": content["counts"],
        "anchors_present": {k: v is not None for k, v in anchors.items()},
        "anchor_qdh": {
            k: (None if v is None else v.get("qdh_base"))
            for k, v in anchors.items()
        },
        "elapsed_s": content["elapsed_s"],
        "created_at": format_utc_z(datetime.now(timezone.utc)),
    }
    atomic_write_json(out_dir / "run_manifest.json", manifest)
    md = _markdown_report(manifest, content)
    atomic_write_text(out_dir / "EPISODE1_SILVER_QDH_REPORT.md", md)
    return {"ok": ok, "verdict": verdict, "out_dir": str(out_dir), "manifest": manifest, "content": content}


def _markdown_report(manifest: dict[str, Any], content: dict[str, Any]) -> str:
    anchors = content.get("anchors") or {}
    lines = [
        f"# Episode-1 Silver QDH Bridge Report",
        "",
        f"**Verdict:** `{manifest.get('verdict')}`",
        "",
        f"- Audit: `{AUDIT_ID}`",
        f"- Episode: `{EPISODE_ID}`",
        f"- Wall: {WALL_SIDE} @ {WALL_PRICE}",
        f"- Silver: `{content['data_source']['silver_database']}`",
        f"- Chunks: {content['data_source']['n_chunks']}",
        f"- LC / metrics / trades: {content['counts']['n_level_changes']} / "
        f"{content['counts']['n_metrics_buckets']} / {content['counts']['n_raw_trades']}",
        f"- Timeline rows: {content['counts']['n_timeline_rows']}",
        f"- Look-ahead OK: {manifest.get('look_ahead_ok')}",
        f"- Mass-balance OK: {manifest.get('mass_balance_ok')}",
        "",
        "## Anchor QDH_base",
        "",
        "| Anchor | qdh_base | qdh_toxic | persistence | hit_qty |",
        "|---|---:|---:|---:|---:|",
    ]
    for name in (
        "wall_touch",
        "immediately_before_episode_detection",
        "episode_detection",
    ):
        row = anchors.get(name) or {}
        lines.append(
            f"| {name} | {row.get('qdh_base')} | {row.get('qdh_toxic_base_only')} | "
            f"{row.get('persistence_ratio')} | {row.get('hit_qty')} |"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Reuses `level_first_episode1_wall_flow_qdh_base_v1` attribution / QDH / aggressor.",
            "- Silver metrics lack receive-time; `event_available_at` is research ceil-100ms proxy.",
            "- `WALL_STATE = NOT_CLASSIFIED` (no Signal V2 yet).",
            "- OI/Liq multipliers fixed at M_OI=M_LIQ=1.0.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"
