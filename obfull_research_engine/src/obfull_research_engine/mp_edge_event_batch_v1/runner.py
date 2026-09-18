"""Sequential batch runner with resume, warmup, frozen V2 semantics."""

from __future__ import annotations

import csv
import json
import os
import resource
import signal
import time
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from obfull_research_engine.mp_edge_event_study_v1.mp_levels import (
    build_profiles_for_window,
)
from obfull_research_engine.mp_edge_event_study_v1.silver_mids import (
    assess_window_chunks,
    load_mid_series,
)
from obfull_research_engine.mp_edge_event_study_v1.util import format_ns_z, ns_to_dt
from obfull_research_engine.mp_edge_event_study_v2.pipeline import run_offline_pipeline_v2
from obfull_research_engine.timeparse import format_utc_z

from .episodes import POLICIES, build_episode_rows, policy_event_ids
from .outcomes_ext import compute_outcomes_ext
from .params import (
    FROZEN_V2_SEMANTICS,
    SEMANTICS_HASH,
    BatchParams,
    v2_params_for_window,
)
from .stats import build_summaries
from .warmup import plan_warmup
from .windows import (
    BatchWindow,
    build_batch_windows,
    read_batch_windows_csv,
    select_windows,
    write_batch_windows_csv,
)


_ABORT = False


def _on_signal(signum, frame):  # noqa: ANN001
    global _ABORT
    _ABORT = True


def _peak_rss_mib() -> float:
    return float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) / 1024.0


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            flat = {
                k: (json.dumps(v, sort_keys=True, default=str) if isinstance(v, (list, dict)) else v)
                for k, v in r.items()
            }
            w.writerow(flat)
    os.replace(tmp, path)


def _try_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        return
    # Homogenize types: CSV-resumed rows are strings; live rows may be ints.
    clean = []
    for r in rows:
        item = {}
        for k, v in r.items():
            if isinstance(v, (list, dict)):
                item[k] = json.dumps(v, sort_keys=True, default=str)
            elif v is None:
                item[k] = None
            else:
                item[k] = str(v)
        clean.append(item)
    tmp = path.with_suffix(path.suffix + ".tmp")
    pq.write_table(pa.Table.from_pylist(clean), tmp)
    os.replace(tmp, path)


def _append_status(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def _load_complete_ids(status_path: Path) -> set[str]:
    done: set[str] = set()
    if not status_path.is_file():
        return done
    last: dict[str, str] = {}
    for line in status_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        last[obj["window_id"]] = obj["status"]
    for wid, st in last.items():
        if st == "COMPLETE":
            done.add(wid)
    return done


def process_one_window(
    client: Any,
    mp_client: Any,
    win: BatchWindow,
    params: BatchParams,
    window_dir: Path,
    log: list[str],
) -> dict[str, Any]:
    window_dir.mkdir(parents=True, exist_ok=True)
    warmup = plan_warmup(
        window_id=win.window_id,
        analysis_start_ns=win.start_ns,
        analysis_end_ns=win.end_ns,
        epoch_safe_start_ns=win.start_ns,  # single-epoch window == epoch safe bounds
    )
    _write_json(window_dir / "warmup.json", warmup.to_dict())

    assessment = assess_window_chunks(
        client,
        database=params.silver_database,
        symbol=params.symbol,
        start=ns_to_dt(win.start_ns),
        end=ns_to_dt(win.end_ns),
    )
    if assessment["epoch_id"] != win.replay_epoch:
        raise RuntimeError(
            f"EPOCH_MISMATCH:{assessment['epoch_id']}!={win.replay_epoch}"
        )

    # Profiles: build using warmup_start..analysis_end so previous_closed at start is available
    # when warmup covers the prior period; never synthesize missing profiles.
    v2p = v2_params_for_window(
        symbol=params.symbol,
        start_z=win.start_ts if win.start_ts.endswith("Z") else format_ns_z(win.start_ns),
        end_z=win.end_ts if win.end_ts.endswith("Z") else format_ns_z(win.end_ns),
        output_dir=window_dir,
        silver_database=params.silver_database,
    )
    # Ensure params start/end match window exactly
    from obfull_research_engine.timeparse import parse_utc_z

    # rebuild with exact Z strings from window
    v2p = v2_params_for_window(
        symbol=params.symbol,
        start_z=format_ns_z(win.start_ns),
        end_z=format_ns_z(win.end_ns),
        output_dir=window_dir,
        silver_database=params.silver_database,
    )

    profiles = build_profiles_for_window(
        symbol=params.symbol,
        start=ns_to_dt(warmup.mp_warmup_start_ns),
        end=ns_to_dt(win.end_ns),
        timeframes=v2p.timeframes,
        client=mp_client,
        profile_source=v2p.profile_source,
    )
    # Filter: only profiles that can be active during analysis (available_at <= analysis_end)
    # and whose period is within epoch (start >= epoch_safe_start already ensured by build range)
    profiles = [
        p
        for p in profiles
        if p.profile_available_ts_ns <= win.end_ns
        and p.profile_start_ts_ns >= warmup.mp_warmup_start_ns
    ]
    log.append(f"{win.window_id}: profiles={len(profiles)}")

    mids = load_mid_series(
        client,
        database=params.silver_database,
        symbol=params.symbol,
        start=ns_to_dt(win.start_ns),
        end=ns_to_dt(win.end_ns),
        chunk_keys=assessment["chunk_keys"],
        expected_epoch_id=win.replay_epoch,
    )
    # hard: no mid outside analysis window (loader already checks)
    log.append(f"{win.window_id}: mids={len(mids)}")

    result = run_offline_pipeline_v2(
        params=v2p,
        profiles=profiles,
        mids=mids,
        epoch_id=win.replay_epoch,
    )
    events = result["events"]
    # Drop any event before analysis_start (should not happen if mids start at analysis)
    events = [e for e in events if win.start_ns <= e.first_touch_ts_ns < win.end_ns]
    for e in events:
        if e.profile_available_ts_ns is not None and e.profile_available_ts_ns > e.first_touch_ts_ns:
            raise RuntimeError("CAUSALITY_VIOLATION")

    outcomes = compute_outcomes_ext(
        events,
        mids,
        params=v2p,
        end_ns=win.end_ns,
        window_id=win.window_id,
        semantics_hash=params.semantics_hash,
        cost_scenarios_bps=params.cost_scenarios_bps,
    )
    episodes = build_episode_rows(events, [], window_id=win.window_id)

    event_rows = []
    for e in events:
        row = e.to_row()
        row["window_id"] = win.window_id
        row["replay_epoch"] = win.replay_epoch
        row["semantics_hash"] = params.semantics_hash
        row["parameters_hash"] = params.semantics_hash
        event_rows.append(row)
    outcome_rows = [o.to_row() for o in outcomes]
    episode_rows = [ep.to_row() for ep in episodes]

    _write_csv(window_dir / "events.csv", event_rows)
    _write_csv(window_dir / "outcomes.csv", outcome_rows)
    _write_csv(window_dir / "episodes.csv", episode_rows)
    _try_parquet(window_dir / "events.parquet", event_rows)
    _try_parquet(window_dir / "outcomes.parquet", outcome_rows)
    _write_json(
        window_dir / "window_manifest.json",
        {
            "window": win.to_row(),
            "warmup": warmup.to_dict(),
            "semantics_hash": params.semantics_hash,
            "n_events": len(events),
            "n_outcomes": len(outcomes),
            "mids_read": len(mids),
            "profiles": len(profiles),
            "assessment_epoch": assessment["epoch_id"],
        },
    )
    # atomic COMPLETE marker
    _write_json(
        window_dir / "COMPLETE.json",
        {
            "window_id": win.window_id,
            "status": "COMPLETE",
            "completed_at_utc": format_utc_z(datetime.now(tz=timezone.utc)),
            "n_events": len(events),
            "semantics_hash": params.semantics_hash,
        },
    )
    return {
        "events": event_rows,
        "outcomes": outcome_rows,
        "episodes": episode_rows,
        "mids_read": len(mids),
        "n_events": len(events),
        "warmup": warmup.to_dict(),
    }


def run_batch(params: BatchParams) -> dict[str, Any]:
    from obfull_research_engine.clickhouse_research_store_v1.helpers import get_clickhouse_client

    global _ABORT
    _ABORT = False
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    out = params.output_dir
    out.mkdir(parents=True, exist_ok=True)
    log_path = out / "batch.log"
    status_path = out / "window_status.jsonl"
    log: list[str] = []
    t0 = time.time()

    def logline(msg: str) -> None:
        line = f"{format_utc_z(datetime.now(tz=timezone.utc))} {msg}"
        log.append(line)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    _write_json(out / "parameters.json", params.to_dict())
    _write_json(
        out / "run_manifest.json",
        {
            "package": "mp_edge_event_batch_v1",
            "created_at_utc": format_utc_z(datetime.now(tz=timezone.utc)),
            "semantics_hash": params.semantics_hash,
            "frozen_v2_semantics": FROZEN_V2_SEMANTICS,
            "parameters": params.to_dict(),
        },
    )

    silver = get_clickhouse_client(role="mp_edge_batch_read")
    try:
        windows = build_batch_windows(
            silver,
            database=params.silver_database,
            symbol=params.symbol,
            min_duration_s=params.min_epoch_duration_s,
            validate_assessment=True,
        )
        write_batch_windows_csv(out / "batch_windows.csv", windows)
        included = select_windows(
            windows,
            window_id=params.window_id,
            max_windows=params.max_windows,
            included_only=True,
        )
        logline(
            f"windows_total={len(windows)} included={len(included)} "
            f"est_runtime_s={sum(w.estimated_runtime_s for w in included):.1f}"
        )

        if params.check_only:
            logline("CHECK_ONLY — no analysis")
            return {
                "check_only": True,
                "windows_total": len(windows),
                "windows_included": len(included),
                "included_ids": [w.window_id for w in included],
                "semantics_hash": params.semantics_hash,
            }

        done = _load_complete_ids(status_path) if params.resume else set()
        # also treat window dirs with COMPLETE.json as done
        for w in included:
            if (out / "windows" / w.window_id / "COMPLETE.json").is_file():
                done.add(w.window_id)

        import clickhouse_connect
        from research_charts.clickhouse_config import load_clickhouse_config

        mp_client = clickhouse_connect.get_client(**load_clickhouse_config().connect_kwargs())
        all_events: list[dict[str, Any]] = []
        all_outcomes: list[dict[str, Any]] = []
        all_episodes: list[dict[str, Any]] = []
        complete = skipped = failed = 0
        total_mids = 0
        try:
            for idx, win in enumerate(included):
                if _ABORT or (time.time() - t0) > params.runtime_limit_s:
                    logline("RESOURCE_ABORT_OR_SIGNAL")
                    _append_status(
                        status_path,
                        {
                            "window_id": win.window_id,
                            "status": "RESOURCE_ABORT",
                            "ts": format_utc_z(datetime.now(tz=timezone.utc)),
                        },
                    )
                    break
                if win.window_id in done:
                    logline(f"SKIP_COMPLETE {win.window_id}")
                    # reload artifacts for merge
                    wdir = out / "windows" / win.window_id
                    if (wdir / "events.csv").is_file():
                        all_events.extend(list(csv.DictReader((wdir / "events.csv").open())))
                        all_outcomes.extend(list(csv.DictReader((wdir / "outcomes.csv").open())))
                        all_episodes.extend(list(csv.DictReader((wdir / "episodes.csv").open())))
                    skipped += 1
                    complete += 1
                    continue

                _append_status(
                    status_path,
                    {
                        "window_id": win.window_id,
                        "status": "RUNNING",
                        "ts": format_utc_z(datetime.now(tz=timezone.utc)),
                        "index": idx,
                    },
                )
                wdir = out / "windows" / win.window_id
                try:
                    logline(f"START {win.window_id} {win.start_ts}→{win.end_ts}")
                    res = process_one_window(silver, mp_client, win, params, wdir, log)
                    all_events.extend(res["events"])
                    all_outcomes.extend(res["outcomes"])
                    all_episodes.extend(res["episodes"])
                    total_mids += int(res["mids_read"])
                    complete += 1
                    _append_status(
                        status_path,
                        {
                            "window_id": win.window_id,
                            "status": "COMPLETE",
                            "ts": format_utc_z(datetime.now(tz=timezone.utc)),
                            "n_events": res["n_events"],
                        },
                    )
                    elapsed = time.time() - t0
                    remain = len(included) - (idx + 1)
                    avg = elapsed / max(1, idx + 1 - skipped)
                    logline(
                        f"DONE {win.window_id} events={res['n_events']} "
                        f"elapsed={elapsed:.1f}s eta={avg*remain:.1f}s"
                    )
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    logline(f"FAILED {win.window_id}: {exc}")
                    (wdir / "FAILED.txt").write_text(
                        traceback.format_exc(), encoding="utf-8"
                    )
                    _append_status(
                        status_path,
                        {
                            "window_id": win.window_id,
                            "status": "FAILED",
                            "ts": format_utc_z(datetime.now(tz=timezone.utc)),
                            "error": str(exc),
                        },
                    )
        finally:
            try:
                mp_client.close()
            except Exception:  # noqa: BLE001
                pass

        # aggregate (only COMPLETE windows already in all_* )
        _try_parquet(out / "events_all.parquet", all_events)
        _try_parquet(out / "outcomes_all.parquet", all_outcomes)
        _try_parquet(out / "episodes.parquet", all_episodes)
        _write_csv(out / "events_all.csv", all_events)
        _write_csv(out / "outcomes_all.csv", all_outcomes)
        _write_csv(out / "episodes.csv", all_episodes)

        # rebuild typed outcomes for stats from dicts is heavy — summarize from CSV fields
        from .outcomes_ext import OutcomeExt
        from .episodes import EpisodeRow

        def _f(x: Any) -> float | None:
            if x is None or x == "":
                return None
            return float(x)

        typed_out: list[OutcomeExt] = []
        for r in all_outcomes:
            tpsl = {
                k[5:]: v
                for k, v in r.items()
                if k.startswith("tpsl_") and v not in (None, "")
            }
            typed_out.append(
                OutcomeExt(
                    event_id=r["event_id"],
                    window_id=r.get("window_id") or "",
                    label_price_only=r.get("label_price_only") or "",
                    event_role=r.get("event_role") or "",
                    fade_side=r.get("fade_side") or "",
                    break_side=r.get("break_side") or "",
                    trade_side=r.get("trade_side") or "",
                    trade_side_reason=r.get("trade_side_reason") or "",
                    confluence_class=r.get("confluence_class") or "",
                    trigger_ts_ns=int(float(r["trigger_ts_ns"])) if r.get("trigger_ts_ns") else None,
                    trigger_price=_f(r.get("trigger_price")),
                    trigger_reason=r.get("trigger_reason") or "",
                    horizon_s=int(float(r["horizon_s"])),
                    is_censored=str(r.get("is_censored")).lower() in ("1", "true"),
                    censor_reason=r.get("censor_reason") or "",
                    outcome_status=r.get("outcome_status") or "",
                    mfe_bps_gross=_f(r.get("mfe_bps_gross")),
                    mae_bps_gross=_f(r.get("mae_bps_gross")),
                    gross_return_bps=_f(r.get("gross_return_bps")),
                    net_return_bps_0=_f(r.get("net_return_bps_0")),
                    net_return_bps_8=_f(r.get("net_return_bps_8")),
                    net_return_bps_12=_f(r.get("net_return_bps_12")),
                    tp_sl_results=tpsl,
                    semantics_hash=r.get("semantics_hash") or "",
                    session_utc=r.get("session_utc") or "",
                )
            )
        typed_ep = [
            EpisodeRow(
                event_id=r["event_id"],
                window_id=r.get("window_id") or "",
                zone_id=r.get("zone_id") or "",
                profile_version_ids=r.get("profile_version_ids") or "",
                episode_id=r.get("episode_id") or "",
                seconds_since_previous_same_zone_event=_f(r.get("seconds_since_previous_same_zone_event")),
                overlapping_outcome_count=int(float(r.get("overlapping_outcome_count") or 0)),
                is_first_touch_of_zone_version=str(r.get("is_first_touch_of_zone_version")).lower()
                in ("1", "true"),
                is_first_signal_of_episode=str(r.get("is_first_signal_of_episode")).lower()
                in ("1", "true"),
                selected_cooldown_5m=str(r.get("selected_cooldown_5m")).lower() in ("1", "true"),
                selected_cooldown_15m=str(r.get("selected_cooldown_15m")).lower() in ("1", "true"),
                selected_cooldown_30m=str(r.get("selected_cooldown_30m")).lower() in ("1", "true"),
                selected_first_touch_zone_version=str(
                    r.get("selected_first_touch_zone_version")
                ).lower()
                in ("1", "true"),
                selected_non_overlapping_30m=str(r.get("selected_non_overlapping_30m")).lower()
                in ("1", "true"),
            )
            for r in all_episodes
        ]

        summaries = build_summaries(typed_out, typed_ep, horizon_s=1800)
        for name, rows in summaries.items():
            _write_csv(out / f"{name}.csv", rows)

        # selection_policies parquet
        sel_rows = []
        for pol in POLICIES:
            ids = policy_event_ids(typed_ep, pol)
            for eid in sorted(ids):
                sel_rows.append({"policy": pol, "event_id": eid})
        _try_parquet(out / "selection_policies.parquet", sel_rows)
        _write_csv(out / "selection_policies.csv", sel_rows)

        runtime = {
            "elapsed_s": time.time() - t0,
            "peak_rss_mib": _peak_rss_mib(),
            "mids_read": total_mids,
            "windows_complete": complete,
            "windows_failed": failed,
            "windows_skipped_resume": skipped,
            "aborted": _ABORT,
        }
        _write_json(out / "runtime_metrics.json", runtime)
        _write_json(
            out / "data_quality_summary.json",
            {
                "n_events": len(all_events),
                "n_outcomes": len(all_outcomes),
                "unique_event_ids": len({e["event_id"] for e in all_events}),
                "semantics_hash": params.semantics_hash,
                "duplicate_event_ids": len(all_events) - len({e["event_id"] for e in all_events}),
            },
        )

        return {
            "windows_total": len(windows),
            "windows_included": len(included),
            "windows_complete": complete,
            "windows_failed": failed,
            "windows_skipped": skipped,
            "events": all_events,
            "outcomes": typed_out,
            "episodes": typed_ep,
            "summaries": summaries,
            "runtime": runtime,
            "included_windows": included,
            "all_windows": windows,
            "semantics_hash": params.semantics_hash,
            "aborted": _ABORT,
        }
    finally:
        try:
            silver.close()
        except Exception:  # noqa: BLE001
            pass
