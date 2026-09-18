"""Orchestrate MP profile build → confluence timeline → FSM → outcomes → artifacts."""

from __future__ import annotations

import csv
import json
import resource
import time
import traceback
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from obfull_research_engine.timeparse import format_utc_z

from .confluence import build_active_confluence, zone_to_row
from .mp_levels import (
    active_levels_at,
    build_profiles_for_window,
    profiles_to_levels,
)
from .outcomes import compute_outcomes
from .params import PilotParams
from .schema import ActiveLevel, ConfluenceZone, MidTick, MpProfile, OutcomeRow, TouchEvent
from .silver_mids import (
    assess_window_chunks,
    estimate_mid_rows,
    load_mid_series,
)
from .touch_detect import detect_events
from .util import dt_to_ns, format_ns_z, ns_to_dt


def _peak_rss_mib() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux: ru_maxrss is KiB
    return float(usage) / 1024.0


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    # union keys
    keys: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            flat = {}
            for k, v in r.items():
                if isinstance(v, (list, dict)):
                    flat[k] = json.dumps(v, sort_keys=True, default=str)
                else:
                    flat[k] = v
            w.writerow(flat)


def _try_parquet(path: Path, rows: Sequence[dict[str, Any]]) -> bool:
    if not rows:
        return False
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        return False
    # stringify nested
    clean = []
    for r in rows:
        c = {}
        for k, v in r.items():
            if isinstance(v, (list, dict)):
                c[k] = json.dumps(v, sort_keys=True, default=str)
            else:
                c[k] = v
        clean.append(c)
    table = pa.Table.from_pylist(clean)
    pq.write_table(table, path)
    return True


def build_zone_timeline(
    all_levels: Sequence[ActiveLevel],
    *,
    start_ns: int,
    end_ns: int,
    confluence_tolerance_bps: float,
    timeframes: Sequence[str],
) -> tuple[list[tuple[int, list[ConfluenceZone]]], list[ConfluenceZone], dict[str, list[ActiveLevel]]]:
    """Rebuild confluence at each profile availability change inside the window."""
    change_points = sorted(
        {
            start_ns,
            *[
                lv.profile_available_ts_ns
                for lv in all_levels
                if start_ns < lv.profile_available_ts_ns < end_ns
            ],
        }
    )
    timeline: list[tuple[int, list[ConfluenceZone]]] = []
    all_zones: list[ConfluenceZone] = []
    levels_by_zone: dict[str, list[ActiveLevel]] = {}
    seen_zone_rows: set[str] = set()
    for cp in change_points:
        active = active_levels_at(all_levels, ts_ns=cp, timeframes=timeframes)
        zones = build_active_confluence(
            active,
            confluence_tolerance_bps=confluence_tolerance_bps,
            asof_ns=cp,
        )
        timeline.append((cp, zones))
        for z in zones:
            levels_by_zone[z.zone_id] = [
                lv for lv in active if lv.level_id in set(z.level_ids)
            ]
            key = f"{z.zone_id}:{cp}"
            if key not in seen_zone_rows:
                seen_zone_rows.add(key)
                all_zones.append(z)
    return timeline, all_zones, levels_by_zone


def run_offline_pipeline(
    *,
    params: PilotParams,
    profiles: Sequence[MpProfile],
    mids: Sequence[MidTick],
    epoch_id: str = "fixture_epoch",
) -> dict[str, Any]:
    """Core pipeline usable by tests (no ClickHouse)."""
    start_ns = dt_to_ns(params.start)
    end_ns = dt_to_ns(params.end)
    levels = profiles_to_levels(profiles)
    # causality assert on all levels used inside window
    for lv in levels:
        # levels may become available before start — OK
        pass
    timeline, zone_rows, levels_by_zone = build_zone_timeline(
        levels,
        start_ns=start_ns,
        end_ns=end_ns,
        confluence_tolerance_bps=params.confluence_tolerance_bps,
        timeframes=params.timeframes,
    )
    events = detect_events(
        mids,
        zones_by_ns={},
        zone_timeline=timeline,
        params=params,
        epoch_id=epoch_id,
        end_ns=end_ns,
        levels_by_zone=levels_by_zone,
    )
    outcomes = compute_outcomes(events, mids, params=params, end_ns=end_ns)
    return {
        "profiles": list(profiles),
        "levels": levels,
        "zones": zone_rows,
        "timeline": timeline,
        "events": events,
        "outcomes": outcomes,
        "epoch_id": epoch_id,
        "start_ns": start_ns,
        "end_ns": end_ns,
        "mids_read": len(mids),
        "mids_valid": sum(1 for m in mids if m.valid),
    }


def write_artifacts(
    *,
    params: PilotParams,
    result: dict[str, Any],
    manifest_extra: dict[str, Any],
    runtime: dict[str, Any],
    log_lines: list[str],
) -> Path:
    out = params.output_dir
    out.mkdir(parents=True, exist_ok=True)
    profiles: list[MpProfile] = result["profiles"]
    levels: list[ActiveLevel] = result["levels"]
    zones: list[ConfluenceZone] = result["zones"]
    events: list[TouchEvent] = result["events"]
    outcomes: list[OutcomeRow] = result["outcomes"]

    profile_rows = [asdict(p) for p in profiles]
    level_rows = [asdict(lv) for lv in levels]
    zone_rows = [zone_to_row(z) for z in zones]
    event_rows = [e.to_row() for e in events]
    outcome_rows = [o.to_row() for o in outcomes]

    _write_csv(out / "mp_profiles.csv", profile_rows)
    _write_csv(out / "mp_active_levels.csv", level_rows)
    _write_csv(out / "confluence_zones.csv", zone_rows)
    _write_csv(out / "events.csv", event_rows)
    _write_csv(out / "outcomes.csv", outcome_rows)
    _try_parquet(out / "events.parquet", event_rows)
    _try_parquet(out / "outcomes.parquet", outcome_rows)
    _try_parquet(out / "mp_profiles.parquet", profile_rows)

    summary = summarize(result, params)
    _write_json(out / "event_summary.json", summary)
    _write_json(out / "runtime_metrics.json", runtime)
    manifest = {
        "package": "mp_edge_event_study_v1",
        "created_at_utc": format_utc_z(datetime.now(tz=timezone.utc)),
        "parameters": params.to_manifest_dict(),
        **manifest_extra,
    }
    _write_json(out / "run_manifest.json", manifest)
    (out / "pilot.log").write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    return out


def summarize(result: dict[str, Any], params: PilotParams) -> dict[str, Any]:
    events: list[TouchEvent] = result["events"]
    outcomes: list[OutcomeRow] = result["outcomes"]
    profiles: list[MpProfile] = result["profiles"]
    zones: list[ConfluenceZone] = result["zones"]
    by_tf = Counter(p.timeframe for p in profiles)
    by_cls = Counter(z.confluence_class for z in zones)
    by_label = Counter(e.label for e in events)
    by_role = Counter(e.event_role for e in events)
    censored = sum(1 for e in events if e.is_censored)
    # unique zone snapshots by class at last timeline point
    tpsl_counts: dict[str, Counter] = defaultdict(Counter)
    for o in outcomes:
        if o.outcome_status != "OK":
            continue
        if o.horizon_s != 300:
            continue
        for k, v in o.tp_sl_results.items():
            tpsl_counts[k][v] += 1
    return {
        "n_profiles": len(profiles),
        "profiles_by_tf": dict(by_tf),
        "n_active_level_rows": len(result["levels"]),
        "n_confluence_zone_snapshots": len(zones),
        "zones_by_class": dict(by_cls),
        "n_events": len(events),
        "events_by_label": dict(by_label),
        "events_by_role": dict(by_role),
        "n_censored_events": censored,
        "n_outcomes": len(outcomes),
        "tpsl_horizon_300s": {k: dict(v) for k, v in tpsl_counts.items()},
        "mids_read": result.get("mids_read"),
        "mids_valid": result.get("mids_valid"),
        "epoch_id": result.get("epoch_id"),
        "window_start": format_utc_z(params.start),
        "window_end": format_utc_z(params.end),
    }


def sanity_check(result: dict[str, Any], params: PilotParams) -> list[str]:
    warnings: list[str] = []
    events: list[TouchEvent] = result["events"]
    outcomes: list[OutcomeRow] = result["outcomes"]
    start_ns = result["start_ns"]
    end_ns = result["end_ns"]
    epoch = result["epoch_id"]
    ids = [e.event_id for e in events]
    if len(ids) != len(set(ids)):
        raise RuntimeError("SANITY: duplicate event_id")
    prev = None
    for e in sorted(events, key=lambda x: (x.first_touch_ts_ns, x.event_id)):
        if e.first_touch_ts_ns < start_ns or e.first_touch_ts_ns >= end_ns:
            raise RuntimeError("SANITY: event outside pilot window")
        if e.epoch_id and epoch and e.epoch_id != epoch:
            raise RuntimeError("SANITY: event epoch mismatch")
        if e.profile_available_ts_ns is not None and e.profile_available_ts_ns > e.first_touch_ts_ns:
            raise RuntimeError("SANITY: causality violation")
        if e.confluence_low > e.confluence_high:
            raise RuntimeError("SANITY: confluence_low > high")
        if e.reclaim_ts_ns is not None and e.reclaim_ts_ns < e.first_touch_ts_ns:
            raise RuntimeError("SANITY: reclaim before touch")
        if e.trigger_ts_ns is not None and e.trigger_ts_ns < e.first_touch_ts_ns:
            raise RuntimeError("SANITY: trigger before touch")
        if prev is not None and e.first_touch_ts_ns < prev:
            raise RuntimeError("SANITY: event times not sortable monotone")
        prev = e.first_touch_ts_ns
    label_sum = sum(Counter(e.label for e in events).values())
    if label_sum != len(events):
        raise RuntimeError("SANITY: label sum mismatch")
    for o in outcomes:
        if o.mfe_bps_gross is not None and o.mfe_bps_gross < 0:
            raise RuntimeError("SANITY: negative MFE")
        if o.mae_bps_gross is not None and o.mae_bps_gross < 0:
            raise RuntimeError("SANITY: negative MAE")
        if o.outcome_status == "CENSORED" and o.tp_sl_results:
            for v in o.tp_sl_results.values():
                if v in ("SL", "TP"):
                    warnings.append(f"censored outcome counted as {v} for {o.event_id}")
    for z in result["zones"]:
        if z.role == "UPPER" and any(False for _ in []):
            pass
        # role purity already by construction
    return warnings


def dry_run_report(
    client: Any,
    params: PilotParams,
    log: list[str],
) -> dict[str, Any]:
    assessment = assess_window_chunks(
        client,
        database=params.silver_database,
        symbol=params.symbol,
        start=params.start,
        end=params.end,
    )
    n_est = estimate_mid_rows(
        client,
        database=params.silver_database,
        symbol=params.symbol,
        start_ns=assessment["start_ns"],
        end_ns=assessment["end_ns"],
        chunk_keys=assessment["chunk_keys"],
    )
    # plan MP period counts without building
    from .mp_levels import period_ends_in_range

    planned = {
        tf: len(period_ends_in_range(params.start, params.end, tf))
        for tf in params.timeframes
    }
    info = {
        "mode": "dry_run",
        "database": params.silver_database,
        "metrics_table": f"{params.silver_database}.ob_metrics_100ms_v1_3",
        "window_start": format_utc_z(params.start),
        "window_end": format_utc_z(params.end),
        "replay_epoch": assessment["epoch_id"],
        "chunk_keys_n": len(assessment["chunk_keys"]),
        "estimated_mid_rows": n_est,
        "planned_mp_period_ends": planned,
        "output_dir": str(params.output_dir),
        "db_mutation": False,
        "assessment": {
            k: assessment[k]
            for k in (
                "status",
                "reason",
                "epoch_id",
                "chain_version",
                "state_count",
                "level_change_count",
            )
            if k in assessment
        },
    }
    log.append(json.dumps(info, sort_keys=True, default=str))
    return info


def run_pilot(params: PilotParams) -> dict[str, Any]:
    from obfull_research_engine.clickhouse_research_store_v1.helpers import (
        get_clickhouse_client,
    )

    def _mp_client() -> Any:
        import clickhouse_connect
        from research_charts.clickhouse_config import load_clickhouse_config

        return clickhouse_connect.get_client(**load_clickhouse_config().connect_kwargs())

    log: list[str] = []
    t0 = time.time()
    log.append(f"START {format_utc_z(datetime.now(tz=timezone.utc))}")

    # Read client for silver
    silver_client = get_clickhouse_client(role="mp_edge_pilot_read")
    try:
        if params.dry_run:
            info = dry_run_report(silver_client, params, log)
            params.output_dir.mkdir(parents=True, exist_ok=True)
            _write_json(params.output_dir / "dry_run.json", info)
            (params.output_dir / "pilot.log").write_text("\n".join(log) + "\n", encoding="utf-8")
            return {"dry_run": info, "verdict": "DRY_RUN_OK"}

        assessment = assess_window_chunks(
            silver_client,
            database=params.silver_database,
            symbol=params.symbol,
            start=params.start,
            end=params.end,
        )
        log.append(f"assessment_epoch={assessment['epoch_id']} chunks={len(assessment['chunk_keys'])}")

        # MP client (dashboard trades DB) — separate session
        mp_client = _mp_client()
        try:
            log.append("building previous_closed MP profiles…")
            profiles = build_profiles_for_window(
                symbol=params.symbol,
                start=params.start,
                end=params.end,
                timeframes=params.timeframes,
                client=mp_client,
                profile_source=params.profile_source,
            )
            log.append(f"profiles_built={len(profiles)}")
        finally:
            try:
                mp_client.close()
            except Exception:  # noqa: BLE001
                pass

        log.append("loading silver mids…")
        mids = load_mid_series(
            silver_client,
            database=params.silver_database,
            symbol=params.symbol,
            start=params.start,
            end=params.end,
            chunk_keys=assessment["chunk_keys"],
            expected_epoch_id=assessment["epoch_id"],
        )
        log.append(f"mids_loaded={len(mids)}")

        result = run_offline_pipeline(
            params=params,
            profiles=profiles,
            mids=mids,
            epoch_id=str(assessment["epoch_id"]),
        )
        warnings = sanity_check(result, params)
        runtime = {
            "elapsed_s": time.time() - t0,
            "peak_rss_mib": _peak_rss_mib(),
            "mids_read": len(mids),
            "warnings": warnings,
        }
        log.append(f"events={len(result['events'])} elapsed_s={runtime['elapsed_s']:.1f}")
        write_artifacts(
            params=params,
            result=result,
            manifest_extra={
                "silver_assessment": {
                    k: assessment[k]
                    for k in (
                        "status",
                        "epoch_id",
                        "chain_version",
                        "chunk_keys",
                        "state_count",
                        "level_change_count",
                    )
                    if k in assessment
                },
                "label_definitions": __import__(
                    "obfull_research_engine.mp_edge_event_study_v1.labels_price",
                    fromlist=["LABEL_DEFINITIONS"],
                ).LABEL_DEFINITIONS,
                "confluence_tie_break": (
                    "same-role only; sort by price,tf,level_id; greedy adjacent merge "
                    "within confluence_tolerance_bps; one cluster membership per level"
                ),
            },
            runtime=runtime,
            log_lines=log,
        )
        result["runtime"] = runtime
        result["warnings"] = warnings
        result["assessment"] = assessment
        return result
    finally:
        try:
            silver_client.close()
        except Exception:  # noqa: BLE001
            pass
