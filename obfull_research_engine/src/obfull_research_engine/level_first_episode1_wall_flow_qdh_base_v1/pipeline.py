"""Orchestrate Episode-1 wall-flow / QDH_base research from persisted OB + trades."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.persist import atomic_write_json, atomic_write_text
from ..drilldown.aggregation_100ms import _as_dt
from ..level_first_episode1_corrected_sms1_persist_v1.io_zst import body_rows, read_jsonl_zst
from ..level_first_episode1_corrected_sms1_persist_v1.persist import pairs_to_map
from ..market_profile_lld_shared_event_materialization_v1.hashing import file_sha256, sha256_hex
from ..timeparse import format_utc_z
from . import (
    ALLOW_CLICKHOUSE_WRITES,
    BAND_TICKS_DEFAULT,
    EPISODE_ID,
    FORBIDDEN_OUTCOME_SUBSTRINGS,
    LEVEL_ID,
    M_LIQ,
    M_OI,
    SCHEMA_VERSION,
    SYMBOL,
    TICK_SIZE,
    VERDICT_EVENT_TIME,
    VERDICT_LIVE_CAUSAL,
    WALL_ID,
    WALL_PRICE,
    WALL_SIDE,
    WALL_STATE_NOT_CLASSIFIED,
    ZONE_ID,
)
from .canonical_trades import build_canonical_trades
from .timeline_100ms import build_feature_timeline
from .wall_flow_attribution import (
    attribute_intervals,
    band_bounds,
    build_band_nodes,
    build_exact_price_nodes,
    wall_flow_event_to_row,
)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_trades(path: Path) -> tuple[list[dict[str, Any]], str]:
    if path.suffix == ".zst" or str(path).endswith(".jsonl.zst"):
        rows = body_rows(read_jsonl_zst(path))
    else:
        rows = []
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    rows.append(json.loads(line))
    return rows, str(path)


def assert_no_outcome_reads(paths: list[Path]) -> None:
    for p in paths:
        name = p.name.lower()
        for bad in FORBIDDEN_OUTCOME_SUBSTRINGS:
            if bad in name:
                raise RuntimeError(f"outcome-like path opened: {p}")


def queue_at_price(asks: dict[float, float], price: float) -> float:
    for px, qty in asks.items():
        if abs(float(px) - float(price)) <= 1e-9:
            return float(qty)
    return 0.0


def band_queue(asks: dict[float, float], band_low: float, band_high: float) -> float:
    return float(
        sum(float(q) for px, q in asks.items() if q and band_low - 1e-12 <= float(px) <= band_high + 1e-12)
    )


def _anchor_row(rows: list[dict[str, Any]], *, available_at: datetime, mode: str) -> dict[str, Any] | None:
    """Pick feature row relative to an anchor availability instant."""
    if not rows:
        return None
    if mode == "at_or_after":
        for r in rows:
            if _as_dt(r["feature_available_at"]) >= available_at:
                return r
        return rows[-1]
    if mode == "last_before":
        last = None
        for r in rows:
            if _as_dt(r["feature_available_at"]) < available_at:
                last = r
            else:
                break
        return last
    if mode == "at_or_before":
        last = None
        for r in rows:
            if _as_dt(r["feature_available_at"]) <= available_at:
                last = r
            else:
                break
        return last
    raise ValueError(mode)


def compute_wall_flow_bundle(
    *,
    persist_dir: Path,
    trades_path: Path,
    zone_touch: dict[str, Any],
    wall_observation: dict[str, Any],
    wall_touch: dict[str, Any],
    detection: dict[str, Any],
    band_ticks: int = BAND_TICKS_DEFAULT,
    tick_size: float = TICK_SIZE,
    wall_price: float = WALL_PRICE,
    level_changes_override: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if ALLOW_CLICKHOUSE_WRITES:
        raise RuntimeError("CH writes forbidden")
    persist_dir = Path(persist_dir)
    trades_path = Path(trades_path)
    assert_no_outcome_reads([persist_dir, trades_path])

    states = body_rows(read_jsonl_zst(persist_dir / "states_100ms.jsonl.zst"))
    level_changes = (
        level_changes_override
        if level_changes_override is not None
        else body_rows(read_jsonl_zst(persist_dir / "level_changes.jsonl.zst"))
    )
    initial = body_rows(read_jsonl_zst(persist_dir / "initial_book.jsonl.zst"))[0]
    asks0 = pairs_to_map(initial.get("asks"))
    band_low, band_high = band_bounds(wall_price, tick_size=tick_size, band_ticks=band_ticks)

    raw_trades, src = _load_trades(trades_path)
    trades, dedup, dropped = build_canonical_trades(raw_trades, symbol=SYMBOL, source_file=src)

    zone_touch_at = _as_dt(zone_touch["exchange_event_time"])
    wall_visible_at = _as_dt(wall_observation.get("wall_visible_at") or zone_touch_at)
    wall_touch_at = _as_dt(wall_touch["exchange_event_time"])
    detection_at = _as_dt(detection.get("detected_at") or detection["exchange_event_time"])
    analysis_end = detection_at

    # Initial book time = window start semantics
    init_et = _as_dt(initial.get("window_start") or states[0]["bucket_start"])
    init_avail = _as_dt(states[0]["available_at"])
    init_epoch = initial.get("replay_epoch")

    q0_exact = queue_at_price(asks0, wall_price)
    exact_nodes, exact_meta = build_exact_price_nodes(
        initial_queue=q0_exact,
        initial_event_time=init_et,
        initial_available_at=init_avail,
        initial_replay_epoch=None if init_epoch is None else int(init_epoch),
        level_changes=level_changes,
        wall_price=wall_price,
        wall_side=WALL_SIDE,
    )
    band_nodes, band_meta = build_band_nodes(
        initial_asks=asks0,
        initial_event_time=init_et,
        initial_available_at=init_avail,
        initial_replay_epoch=None if init_epoch is None else int(init_epoch),
        level_changes=level_changes,
        band_low=band_low,
        band_high=band_high,
        wall_side=WALL_SIDE,
    )

    exact_events, exact_stats = attribute_intervals(
        nodes=exact_nodes,
        trades=trades,
        view="exact_price",
        wall_price=wall_price,
        band_low=band_low,
        band_high=band_high,
        wall_visible_at=wall_visible_at,
        analysis_end_exclusive=analysis_end,
    )
    band_events, band_stats = attribute_intervals(
        nodes=band_nodes,
        trades=trades,
        view="defended_band",
        wall_price=wall_price,
        band_low=band_low,
        band_high=band_high,
        wall_visible_at=wall_visible_at,
        analysis_end_exclusive=analysis_end,
    )

    # Queues at wall touch (from nodes as-of exclusive)
    def queue_asof(nodes, ts: datetime) -> float:
        q = nodes[0].queue
        for n in nodes:
            if n.exchange_event_time <= ts:
                q = n.queue
            else:
                break
        return q

    q_exact_touch = queue_asof(exact_nodes, wall_touch_at)
    q_band_touch = queue_asof(band_nodes, wall_touch_at)

    timeline, tl_meta = build_feature_timeline(
        states=states,
        exact_events=exact_events,
        band_events=band_events,
        wall_touch_at=wall_touch_at,
        zone_touch_at=zone_touch_at,
        detection_at=detection_at,
        queue_exact_at_wall_touch=q_exact_touch,
        queue_band_at_wall_touch=q_band_touch,
        wall_price=wall_price,
    )

    # Receive-time coverage for live-causal feature verdict
    book_recv_missing = int(exact_meta["book_receive_missing"]) + int(band_meta["book_receive_missing"])
    trade_recv_missing = int(dedup.receive_time_missing_count)
    live_causal_ok = book_recv_missing == 0 and trade_recv_missing == 0 and tl_meta["look_ahead_violations_bucket_contract"] == 0

    zone_avail = _as_dt(zone_touch.get("event_available_at") or zone_touch_at)
    wall_touch_avail = _as_dt(wall_touch.get("event_available_at") or wall_touch_at)
    det_avail = _as_dt(detection.get("event_available_at") or detection_at)

    anchors = {
        "zone_touch": _anchor_row(timeline, available_at=zone_avail, mode="at_or_after"),
        "wall_touch": _anchor_row(timeline, available_at=wall_touch_avail, mode="at_or_after"),
        "first_after_wall_touch": _anchor_row(timeline, available_at=wall_touch_avail, mode="at_or_after"),
        "immediately_before_episode_detection": _anchor_row(timeline, available_at=det_avail, mode="last_before"),
        "episode_detection": _anchor_row(timeline, available_at=det_avail, mode="at_or_before"),
    }

    wall_meta = {
        "wall_id": WALL_ID,
        "wall_side": WALL_SIDE,
        "wall_price": wall_price,
        "tick_size": tick_size,
        "band_ticks": band_ticks,
        "band_low": band_low,
        "band_high": band_high,
        "wall_visible_at": format_utc_z(wall_visible_at),
        "zone_touch_at": format_utc_z(zone_touch_at),
        "wall_touch_at": format_utc_z(wall_touch_at),
        "coverage_start": format_utc_z(init_et),
        "coverage_end": format_utc_z(analysis_end),
        "qty_at_zone_touch_reported": wall_observation.get("qty"),
        "queue_exact_at_wall_touch": q_exact_touch,
        "queue_band_at_wall_touch": q_band_touch,
        "WALL_STATE": WALL_STATE_NOT_CLASSIFIED,
    }

    # Phase A documentation snapshot
    phase_a = {
        "canonical_public_trade_source": str(trades_path),
        "trade_id_unique_stable": True,
        "multiple_records_same_trade": "only if duplicate trade_id rows; collapsed by canonical_trade_key",
        "dedup_rule": "canonical_trade_key = symbol|trade_id; first by (trade_ts, trade_id)",
        "public_trade_time_fields": ["trade_ts/exchange_event_time", "collector_received_at/ingest_timestamp"],
        "full_ob_time_fields": ["event_time", "event_available_at(research proxy)", "optional received_at if provided"],
        "cts_T_seq_u_merge": "ordering by event_time + apply_order; u/seq carried as metadata; cts not required for attribution intervals",
        "existing_refill_semantics": "drilldown.refill.detect_refills sign-based candidates; refill_confirm gross-depletion confirm — NOT reused as net_refill here",
        "reuse": [
            "public_trade_index identity",
            "persist level_changes/states/initial_book",
            "independent zone/wall/detection anchors",
            "event_available_at research proxy",
        ],
        "double_count_hazards_avoided": [
            "no second public-trade flow",
            "no adding footprint volume on top of wall hits",
            "exact and band views separate consumed sets",
            "existing refill_confirm not mixed into mass-balance net_refill",
        ],
    }

    content = {
        "schema_version": SCHEMA_VERSION,
        "episode_id": EPISODE_ID,
        "symbol": SYMBOL,
        "zone_id": ZONE_ID,
        "level_id": LEVEL_ID,
        "wall": wall_meta,
        "phase_a": phase_a,
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
        "live_causal": {
            "ok": live_causal_ok,
            "book_receive_missing": book_recv_missing,
            "trade_receive_missing": trade_recv_missing,
            "note": (
                "Persisted level_changes typically lack collector_received_at; "
                "exchange event_available_at is a research proxy and is never silently treated as receive time."
            ),
        },
        "verdict_candidate": VERDICT_LIVE_CAUSAL if live_causal_ok else VERDICT_EVENT_TIME,
        "WALL_STATE": WALL_STATE_NOT_CLASSIFIED,
        "anchors": anchors,
        "n_exact_events": len(exact_events),
        "n_band_events": len(band_events),
        "n_timeline_rows": len(timeline),
        "n_states": len(states),
        "golden_states_expected": 4178,
    }

    return {
        "content": content,
        "exact_events": exact_events,
        "band_events": band_events,
        "timeline": timeline,
        "trades": trades,
        "dropped_duplicates": dropped,
        "anchors": anchors,
    }


def write_wall_flow_outputs(out_dir: Path, bundle: dict[str, Any], *, run_key: str) -> dict[str, str]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}

    exact_rows = [wall_flow_event_to_row(e) for e in bundle["exact_events"]]
    band_rows = [wall_flow_event_to_row(e) for e in bundle["band_events"]]
    # Full trade-id reference table
    refs = []
    for e in list(bundle["exact_events"]) + list(bundle["band_events"]):
        if e.attributed_trade_ids:
            refs.append(
                {
                    "view": e.view,
                    "interval_end_exchange_time": e.interval_end_exchange_time,
                    "attributed_trade_ids_hash": e.attributed_trade_ids_hash,
                    "attributed_trade_ids": e.attributed_trade_ids,
                }
            )

    def dump_json(name: str, obj: Any) -> None:
        path = out_dir / name
        atomic_write_json(path, obj)
        hashes[name] = file_sha256(path)

    dump_json("wall_flow_summary.json", bundle["content"])
    dump_json("wall_flow_events_exact.json", exact_rows)
    dump_json("wall_flow_events_band.json", band_rows)
    dump_json("wall_flow_trade_id_refs.json", refs)
    dump_json("feature_anchors.json", bundle["anchors"])
    dump_json(
        "canonical_trades_audit.json",
        {
            "n_kept": len(bundle["trades"]),
            "n_dropped_duplicates": len(bundle["dropped_duplicates"]),
            "sample_kept": [t.__dict__ for t in bundle["trades"][:5]],
        },
    )

    # CSV timeline
    tl = bundle["timeline"]
    csv_path = out_dir / "feature_timeline.csv"
    if tl:
        # Flatten evidence flags already present; drop nested dict for CSV
        fieldnames = [k for k in tl[0].keys() if k != "evidence_flags"]
        with csv_path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            for row in tl:
                w.writerow({k: row.get(k) for k in fieldnames})
    else:
        csv_path.write_text("", encoding="utf-8")
    hashes["feature_timeline.csv"] = file_sha256(csv_path)

    # Parquet optional
    try:
        import pandas as pd

        pq = out_dir / "feature_timeline.parquet"
        df = pd.DataFrame([{k: v for k, v in r.items() if k != "evidence_flags"} for r in tl])
        df.to_parquet(pq, index=False)
        hashes["feature_timeline.parquet"] = file_sha256(pq)
    except Exception as exc:  # noqa: BLE001
        dump_json("feature_timeline_parquet_skipped.json", {"reason": str(exc)})

    semantic = {
        "n_exact_events": len(exact_rows),
        "n_band_events": len(band_rows),
        "n_timeline": len(tl),
        "trade_dedup": bundle["content"]["trade_dedup"],
        "exact_stats": bundle["content"]["exact_stats"],
        "band_stats": bundle["content"]["band_stats"],
        "timeline_meta_aggressor": bundle["content"]["timeline_meta"].get("aggressor_band"),
        "timeline_meta_qdh_band": bundle["content"]["timeline_meta"].get("qdh_band"),
        "M_OI": M_OI,
        "M_LIQ": M_LIQ,
        "WALL_STATE": WALL_STATE_NOT_CLASSIFIED,
        "verdict_candidate": bundle["content"]["verdict_candidate"],
    }
    dump_json("semantic_fingerprint.json", semantic)
    hashes["semantic_fingerprint"] = sha256_hex(semantic)
    atomic_write_text(out_dir / "STATUS", bundle["content"]["verdict_candidate"] + "\n")
    return hashes


def run_from_persist_dir(
    *,
    persist_dir: Path,
    out_dir: Path,
    run_key: str,
    trades_path: Path | None = None,
    band_ticks: int = BAND_TICKS_DEFAULT,
) -> dict[str, Any]:
    persist_dir = Path(persist_dir)
    zone_touch = _load_json(persist_dir / "independent_zone_first_touch.json")
    wall_obs = _load_json(persist_dir / "independent_wall_observation_at_zone_touch.json")
    wall_touch = _load_json(persist_dir / "independent_wall_first_touch.json")
    detection = _load_json(persist_dir / "independent_detection.json")
    if trades_path is None:
        from ..level_first_episode1_corrected_sms1_persist_v1.independent_derivation import INPUT_FREEZE_DIR

        trades_path = INPUT_FREEZE_DIR / "public_trades_zone_window.jsonl"
    bundle = compute_wall_flow_bundle(
        persist_dir=persist_dir,
        trades_path=Path(trades_path),
        zone_touch=zone_touch,
        wall_observation=wall_obs,
        wall_touch=wall_touch,
        detection=detection,
        band_ticks=band_ticks,
    )
    hashes = write_wall_flow_outputs(out_dir, bundle, run_key=run_key)
    return {"ok": True, "run_key": run_key, "out_dir": str(out_dir), "hashes": hashes, "content": bundle["content"]}
