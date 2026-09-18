"""V2 pipeline — reuses V1 MP/Silver loaders; V2 FSM/outcomes isolated."""

from __future__ import annotations

import csv
import json
import resource
import time
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from obfull_research_engine.mp_edge_event_study_v1.confluence import zone_to_row
from obfull_research_engine.mp_edge_event_study_v1.mp_levels import (
    build_profiles_for_window,
    profiles_to_levels,
)
from obfull_research_engine.mp_edge_event_study_v1.pipeline import build_zone_timeline
from obfull_research_engine.mp_edge_event_study_v1.schema import MidTick, MpProfile
from obfull_research_engine.mp_edge_event_study_v1.silver_mids import (
    assess_window_chunks,
    estimate_mid_rows,
    load_mid_series,
)
from obfull_research_engine.mp_edge_event_study_v1.util import dt_to_ns
from obfull_research_engine.timeparse import format_utc_z

from . import V1_OUTCOME_INVALIDATION
from .outcomes import compute_outcomes_v2, pair_key
from .params import PilotParamsV2
from .schema import EventV2, OutcomeV2, RejectedTouch, RejectionStats
from .touch_fsm import detect_events_v2
from .v1_audit import audit_v1_sample


def _peak_rss_mib() -> float:
    return float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / 1024.0


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            flat = {
                k: (json.dumps(v, sort_keys=True, default=str) if isinstance(v, (list, dict)) else v)
                for k, v in r.items()
            }
            w.writerow(flat)


def _try_parquet(path: Path, rows: Sequence[dict[str, Any]]) -> bool:
    if not rows:
        return False
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        return False
    clean = []
    for r in rows:
        clean.append(
            {
                k: (json.dumps(v, sort_keys=True, default=str) if isinstance(v, (list, dict)) else v)
                for k, v in r.items()
            }
        )
    pq.write_table(pa.Table.from_pylist(clean), path)
    return True


def run_offline_pipeline_v2(
    *,
    params: PilotParamsV2,
    profiles: Sequence[MpProfile],
    mids: Sequence[MidTick],
    epoch_id: str = "fixture_epoch",
) -> dict[str, Any]:
    start_ns = dt_to_ns(params.start)
    end_ns = dt_to_ns(params.end)
    levels = profiles_to_levels(profiles)
    timeline, zone_rows, levels_by_zone = build_zone_timeline(
        levels,
        start_ns=start_ns,
        end_ns=end_ns,
        confluence_tolerance_bps=params.confluence_tolerance_bps,
        timeframes=params.timeframes,
    )
    events, rejected, stats = detect_events_v2(
        mids,
        zone_timeline=timeline,
        params=params,
        epoch_id=epoch_id,
        end_ns=end_ns,
        levels_by_zone=levels_by_zone,
    )
    outcomes = compute_outcomes_v2(events, mids, params=params, end_ns=end_ns)
    return {
        "profiles": list(profiles),
        "levels": levels,
        "zones": zone_rows,
        "timeline": timeline,
        "events": events,
        "outcomes": outcomes,
        "rejected": rejected,
        "rejection_stats": stats,
        "epoch_id": epoch_id,
        "start_ns": start_ns,
        "end_ns": end_ns,
        "mids_read": len(mids),
        "mids_valid": sum(1 for m in mids if m.valid),
    }


def summarize_v2(result: dict[str, Any], params: PilotParamsV2) -> dict[str, Any]:
    events: list[EventV2] = result["events"]
    outcomes: list[OutcomeV2] = result["outcomes"]
    stats: RejectionStats = result["rejection_stats"]
    by_label = Counter(e.label_price_only for e in events)
    by_role = Counter(e.event_role for e in events)
    by_trade = Counter(e.trade_side for e in events if e.trade_side)
    by_cls = Counter(e.confluence_class for e in events)
    by_pattern = Counter(e.transition_pattern for e in events if e.transition_pattern)
    tpsl: dict[str, dict[str, Counter]] = defaultdict(lambda: defaultdict(Counter))
    for o in outcomes:
        if o.outcome_status != "OK":
            continue
        for k, v in o.tp_sl_results.items():
            tpsl[str(o.horizon_s)][k][v] += 1
    return {
        "rejection_stats": stats.to_dict(),
        "n_events": len(events),
        "events_by_label": dict(by_label),
        "events_by_role": dict(by_role),
        "events_by_trade_side": dict(by_trade),
        "events_by_confluence_class": dict(by_cls),
        "transition_patterns": dict(by_pattern),
        "n_censored": sum(1 for e in events if e.is_censored),
        "tpsl_by_horizon": {
            h: {k: dict(c) for k, c in pairs.items()} for h, pairs in tpsl.items()
        },
        "profiles_by_tf": dict(Counter(p.timeframe for p in result["profiles"])),
        "mids_read": result.get("mids_read"),
        "epoch_id": result.get("epoch_id"),
        "v1_true_break_outcomes_invalid": True,
        "v1_invalidation_note": V1_OUTCOME_INVALIDATION,
    }


def dry_run_v2(client: Any, params: PilotParamsV2, log: list[str]) -> dict[str, Any]:
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
    from obfull_research_engine.mp_edge_event_study_v1.mp_levels import period_ends_in_range

    info = {
        "mode": "dry_run",
        "package": "mp_edge_event_study_v2",
        "database": params.silver_database,
        "window_start": format_utc_z(params.start),
        "window_end": format_utc_z(params.end),
        "replay_epoch": assessment["epoch_id"],
        "estimated_mid_rows": n_est,
        "planned_mp_period_ends": {
            tf: len(period_ends_in_range(params.start, params.end, tf))
            for tf in params.timeframes
        },
        "output_dir": str(params.output_dir),
        "db_mutation": False,
        "v1_true_break_outcomes_invalid": True,
    }
    log.append(json.dumps(info, sort_keys=True, default=str))
    return info


def run_pilot_v2(params: PilotParamsV2) -> dict[str, Any]:
    from obfull_research_engine.clickhouse_research_store_v1.helpers import get_clickhouse_client

    def _mp_client() -> Any:
        import clickhouse_connect
        from research_charts.clickhouse_config import load_clickhouse_config

        return clickhouse_connect.get_client(**load_clickhouse_config().connect_kwargs())

    log: list[str] = []
    t0 = time.time()
    log.append(f"START {format_utc_z(datetime.now(tz=timezone.utc))}")
    silver = get_clickhouse_client(role="mp_edge_pilot_v2_read")
    try:
        if params.dry_run:
            info = dry_run_v2(silver, params, log)
            params.output_dir.mkdir(parents=True, exist_ok=True)
            _write_json(params.output_dir / "dry_run.json", info)
            (params.output_dir / "pilot_v2.log").write_text("\n".join(log) + "\n", encoding="utf-8")
            return {"dry_run": info}

        assessment = assess_window_chunks(
            silver,
            database=params.silver_database,
            symbol=params.symbol,
            start=params.start,
            end=params.end,
        )
        log.append(f"epoch={assessment['epoch_id']} chunks={len(assessment['chunk_keys'])}")

        mp = _mp_client()
        try:
            profiles = build_profiles_for_window(
                symbol=params.symbol,
                start=params.start,
                end=params.end,
                timeframes=params.timeframes,
                client=mp,
                profile_source=params.profile_source,
            )
            log.append(f"profiles={len(profiles)}")
        finally:
            try:
                mp.close()
            except Exception:  # noqa: BLE001
                pass

        mids = load_mid_series(
            silver,
            database=params.silver_database,
            symbol=params.symbol,
            start=params.start,
            end=params.end,
            chunk_keys=assessment["chunk_keys"],
            expected_epoch_id=assessment["epoch_id"],
        )
        log.append(f"mids={len(mids)}")

        result = run_offline_pipeline_v2(
            params=params,
            profiles=profiles,
            mids=mids,
            epoch_id=str(assessment["epoch_id"]),
        )
        runtime = {
            "elapsed_s": time.time() - t0,
            "peak_rss_mib": _peak_rss_mib(),
            "mids_read": len(mids),
        }
        result["runtime"] = runtime
        result["assessment"] = assessment
        result["summary"] = summarize_v2(result, params)

        out = params.output_dir
        out.mkdir(parents=True, exist_ok=True)
        event_rows = [e.to_row() for e in result["events"]]
        outcome_rows = [o.to_row() for o in result["outcomes"]]
        rejected_rows = [r.to_row() for r in result["rejected"]]
        _write_csv(out / "events.csv", event_rows)
        _write_csv(out / "outcomes.csv", outcome_rows)
        _write_csv(out / "rejected_touch_candidates.csv", rejected_rows)
        _try_parquet(out / "events.parquet", event_rows)
        _try_parquet(out / "outcomes.parquet", outcome_rows)
        _write_json(out / "transition_summary.json", result["summary"]["transition_patterns"])
        _write_json(out / "outcome_summary.json", result["summary"])
        _write_json(out / "runtime_metrics.json", runtime)
        _write_json(
            out / "run_manifest.json",
            {
                "package": "mp_edge_event_study_v2",
                "created_at_utc": format_utc_z(datetime.now(tz=timezone.utc)),
                "parameters": params.to_manifest_dict(),
                "silver_assessment": {
                    k: assessment[k]
                    for k in ("status", "epoch_id", "chain_version", "chunk_keys")
                    if k in assessment
                },
                "v1_true_break_outcomes_invalid": True,
                "v1_invalidation_note": V1_OUTCOME_INVALIDATION,
            },
        )

        # V1 audit using same mids
        v1_events = Path(params.v1_run_dir) / "events.csv"
        if v1_events.is_file():
            audit_meta = audit_v1_sample(
                v1_events,
                mids,
                out_csv=out / "V1_EVENT_AUDIT.csv",
                out_md=out / "V1_EVENT_AUDIT.md",
            )
            result["v1_audit"] = audit_meta
            log.append(f"v1_audit_n={audit_meta['n_audited']}")
        else:
            result["v1_audit"] = {"error": f"missing {v1_events}"}
            log.append("v1_audit_missing")

        log.append(
            f"events={len(result['events'])} rejected={len(result['rejected'])} "
            f"elapsed_s={runtime['elapsed_s']:.1f}"
        )
        (out / "pilot_v2.log").write_text("\n".join(log) + "\n", encoding="utf-8")
        return result
    finally:
        try:
            silver.close()
        except Exception:  # noqa: BLE001
            pass
