"""Orchestrate input audit → pilot → full enrichment → analysis."""

from __future__ import annotations

import csv
import json
import os
import resource
import time
import traceback
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from obfull_research_engine.mp_edge_event_study_v1.util import format_ns_z, ns_to_dt
from obfull_research_engine.mp_ob_feature_enrichment_v1 import PACKAGE_NAME, RUN_ID
from obfull_research_engine.mp_ob_feature_enrichment_v1.analysis import (
    build_discovery_validation_split,
    run_analyses,
)
from obfull_research_engine.mp_ob_feature_enrichment_v1.features import compute_event_features
from obfull_research_engine.mp_ob_feature_enrichment_v1.input_audit import audit_batch_input
from obfull_research_engine.mp_ob_feature_enrichment_v1.loaders import (
    assess_chunks,
    chunks_overlapping,
    load_level_changes,
    load_metrics_enriched,
    load_public_trades,
)
from obfull_research_engine.mp_ob_feature_enrichment_v1.params import (
    BATCH_RUN_REL,
    DEFAULT_RUN_REL,
    EnrichmentParams,
    NS,
    PILOT_MAX_EVENTS,
)
from obfull_research_engine.mp_ob_feature_enrichment_v1.report import write_reports
from obfull_research_engine.mp_ob_feature_enrichment_v1.xray_cases import select_manual_xray_cases
from obfull_research_engine.mp_ob_feature_enrichment_v1.zones import build_zone_bands
from obfull_research_engine.timeparse import parse_utc_z


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
            w.writerow({k: r.get(k) for k in keys})
    os.replace(tmp, path)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "y"}


def select_pilot_events(events: list[dict[str, str]], *, max_n: int = PILOT_MAX_EVENTS) -> list[dict[str, str]]:
    """≤20 events across windows/labels/roles/outcomes."""
    by_key: dict[tuple, list] = defaultdict(list)
    for e in events:
        key = (e.get("window_id"), e.get("label_price_only"), e.get("event_role"), e.get("confluence_class", "")[:2])
        by_key[key].append(e)
    selected: list[dict[str, str]] = []
    # round-robin across strata
    keys = sorted(by_key.keys())
    while len(selected) < max_n and keys:
        progressed = False
        for k in list(keys):
            bucket = by_key[k]
            if not bucket:
                keys.remove(k)
                continue
            selected.append(bucket.pop(0))
            progressed = True
            if len(selected) >= max_n:
                break
        if not progressed:
            break
    # ensure success/fail mix when trigger present
    return selected[:max_n]


def _window_bounds(batch_dir: Path) -> dict[str, dict[str, Any]]:
    rows = _read_csv(batch_dir / "batch_windows.csv")
    out = {}
    for r in rows:
        if str(r.get("included")).lower() != "true":
            continue
        out[r["window_id"]] = {
            "start_ns": int(r["start_ns"]),
            "end_ns": int(r["end_ns"]),
            "start_ts": r["start_ts"],
            "end_ts": r["end_ts"],
            "replay_epoch": r["replay_epoch"],
        }
    return out


def enrich_one(
    client: Any,
    *,
    event: dict[str, str],
    window: dict[str, Any],
    params: EnrichmentParams,
    chunk_cache: dict[str, Any],
) -> dict[str, Any]:
    touch_ns = int(event["first_touch_ts_ns"])
    trigger_raw = event.get("trigger_ts_ns")
    trigger_ns = int(trigger_raw) if trigger_raw not in (None, "", "None") else None
    cutoff_ns = trigger_ns if trigger_ns is not None else touch_ns
    feature_start = touch_ns - int(120 * NS)
    w_start = int(window["start_ns"])
    w_end = int(window["end_ns"])
    feature_start = max(feature_start, w_start)
    cutoff_ns = min(cutoff_ns, w_end)

    bands = build_zone_bands(
        role=str(event["event_role"]),
        low=float(event["confluence_low"]),
        high=float(event["confluence_high"]),
    )
    # pad small; query band already covers ±5bps
    pad = abs(bands.center) * 0.5 / 10_000.0
    price_min = bands.query_min - pad
    price_max = bands.query_max + pad

    cache_key = window["replay_epoch"]
    if cache_key not in chunk_cache:
        assessment = assess_chunks(
            client,
            database=params.silver_database,
            symbol=params.symbol,
            start=ns_to_dt(w_start),
            end=ns_to_dt(w_end),
        )
        chunk_cache[cache_key] = assessment
    assessment = chunk_cache[cache_key]
    chain_version = str(assessment.get("chain_version") or "")
    # Restrict to chunks overlapping the causal feature window (huge speedup vs full epoch).
    if chain_version:
        chunk_keys = chunks_overlapping(
            client,
            database=params.silver_database,
            chain_version=chain_version,
            start_ns=feature_start,
            end_ns=cutoff_ns + 1,
        )
    else:
        chunk_keys = list(assessment.get("chunk_keys") or [])
    if not chunk_keys:
        raise RuntimeError(f"NO_CHUNK_KEYS:{event['event_id']}")

    lcs = load_level_changes(
        client,
        database=params.silver_database,
        symbol=params.symbol,
        start_ns=feature_start,
        end_ns=cutoff_ns + 1,
        chunk_keys=chunk_keys,
        price_min=price_min,
        price_max=price_max,
        # keep both sides: opposing depth features need attack side
        side=None,
    )
    metrics = load_metrics_enriched(
        client,
        database=params.silver_database,
        symbol=params.symbol,
        start_ns=feature_start,
        end_ns=cutoff_ns,
        chunk_keys=chunk_keys,
    )
    trades = None
    trades_available = False
    trades_missing_reason = None
    try:
        trades = load_public_trades(
            client,
            symbol=params.symbol,
            start_ns=feature_start,
            end_ns=cutoff_ns,
        )
        trades_available = True
    except Exception as exc:  # noqa: BLE001
        trades_missing_reason = f"TRADE_LOAD_FAIL:{type(exc).__name__}"
        trades = []
        trades_available = False

    bundle = compute_event_features(
        event=event,
        level_changes=lcs,
        metrics=metrics,
        trades=trades,
        trades_available=trades_available,
        window_start_ns=w_start,
        window_end_ns=w_end,
    )
    row: dict[str, Any] = {
        "event_id": event["event_id"],
        "window_id": event.get("window_id"),
        "replay_epoch": event.get("replay_epoch") or event.get("epoch_id"),
        "first_touch_ts_ns": touch_ns,
        "trigger_ts_ns": trigger_ns,
        "feature_cutoff_ts_ns": cutoff_ns,
        "max_feature_ts_ns": bundle.max_feature_ts_ns,
        "causal_ok": bundle.causal_ok,
        "leakage_flags": "|".join(bundle.leakage_flags),
        "trades_available": trades_available,
        "trades_missing_reason": trades_missing_reason,
        "baseline_warmup_ok": bundle.baseline_warmup_ok,
        "level_changes_available": bundle.level_changes_available,
        "n_level_changes": len(lcs),
        "n_metrics": len(metrics),
        "n_trades": len(trades or []),
        "label_price_only": event.get("label_price_only"),
        "event_role": event.get("event_role"),
        "trade_side": event.get("trade_side"),
        "confluence_class": event.get("confluence_class"),
    }
    for name, fv in bundle.features.items():
        row[name] = fv.value
        row[f"{name}__available"] = fv.available
        row[f"{name}__missing_reason"] = fv.missing_reason
        row[f"{name}__causal_valid"] = fv.causal_valid
    row["_feature_meta_rows"] = [fv.to_row(event["event_id"]) for fv in bundle.features.values()]
    return row


def run_enrichment(
    *,
    repo_root: Path,
    params: EnrichmentParams | None = None,
    client: Any | None = None,
    pilot_only: bool = False,
    skip_full: bool = False,
) -> dict[str, Any]:
    repo_root = Path(repo_root)
    params = params or EnrichmentParams(
        batch_run_dir=repo_root / BATCH_RUN_REL,
        out_dir=repo_root / DEFAULT_RUN_REL,
    )
    out_dir = Path(params.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "enrichment.log"
    t0 = time.time()

    def log(msg: str) -> None:
        line = f"{datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')} {msg}"
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    log(f"start {PACKAGE_NAME} run={RUN_ID}")
    audit = audit_batch_input(params.batch_run_dir)
    _write_json(out_dir / "input_audit.json", audit)
    if not audit.get("ok"):
        verdict = "OB_ENRICHMENT_BLOCKED_INPUT"
        _write_json(
            out_dir / "run_manifest.json",
            {
                "run_id": RUN_ID,
                "verdict": verdict,
                "input_audit": audit,
                "params": params.to_dict(),
            },
        )
        write_reports(out_dir, verdict=verdict, audit=audit, summary={})
        return {"verdict": verdict, "out_dir": str(out_dir)}

    events = _read_csv(params.batch_run_dir / "events_all.csv")
    episodes = _read_csv(params.batch_run_dir / "episodes.csv")
    outcomes = _read_csv(params.batch_run_dir / "outcomes_all.csv")
    windows = _window_bounds(params.batch_run_dir)

    # Fix discovery/validation split BEFORE looking at outcomes for thresholds
    split_rows, split_meta = build_discovery_validation_split(
        windows_csv=params.batch_run_dir / "batch_windows.csv",
        events=events,
        discovery_window_count=params.discovery_window_count,
    )
    _write_csv(out_dir / "discovery_validation_split.csv", split_rows)
    _write_json(out_dir / "discovery_validation_split.json", split_meta)

    own_client = False
    if client is None:
        from obfull_research_engine.clickhouse_research_store_v1.helpers import (
            get_clickhouse_client,
        )

        client = get_clickhouse_client(role="mp_ob_enrichment_read")
        own_client = True

    chunk_cache: dict[str, Any] = {}
    pilot_events = select_pilot_events(events, max_n=params.pilot_max_events)
    assert len(pilot_events) <= params.pilot_max_events

    pilot_rows: list[dict[str, Any]] = []
    pilot_t0 = time.time()
    try:
        for e in pilot_events:
            wid = e["window_id"]
            if wid not in windows:
                raise RuntimeError(f"EVENT_WINDOW_NOT_IN_COMPLETE:{e['event_id']}:{wid}")
            row = enrich_one(client, event=e, window=windows[wid], params=params, chunk_cache=chunk_cache)
            meta = row.pop("_feature_meta_rows", [])
            pilot_rows.append(row)
            log(f"pilot {e['event_id']} causal={row['causal_ok']} lc={row['n_level_changes']} trades={row['n_trades']}")
        pilot_runtime = time.time() - pilot_t0
        _write_csv(out_dir / "enrichment_pilot_events.csv", [{k: v for k, v in r.items() if not str(k).endswith(('__available', '__missing_reason', '__causal_valid')) or k.count('__') < 2} for r in pilot_rows])
        # simpler pilot csv: core cols
        core_cols = [
            "event_id",
            "window_id",
            "label_price_only",
            "event_role",
            "causal_ok",
            "trades_available",
            "baseline_warmup_ok",
            "depth_at_zone_touch",
            "hit_qty",
            "pull_qty",
            "add_qty",
            "refill_ratio",
            "aggression_against_zone",
            "max_feature_ts_ns",
            "feature_cutoff_ts_ns",
        ]
        _write_csv(
            out_dir / "enrichment_pilot_events.csv",
            [{c: r.get(c) for c in core_cols} for r in pilot_rows],
        )
        pilot_causal_viol = sum(1 for r in pilot_rows if not r.get("causal_ok"))
        pilot_ok = pilot_causal_viol == 0 and len(pilot_rows) > 0
        (out_dir / "enrichment_pilot_report.md").write_text(
            "\n".join(
                [
                    "# Enrichment Pilot Report",
                    "",
                    f"- events: {len(pilot_rows)} (max {params.pilot_max_events})",
                    f"- causal violations: {pilot_causal_viol}",
                    f"- trades available: {sum(1 for r in pilot_rows if r.get('trades_available'))}",
                    f"- baseline warmup ok: {sum(1 for r in pilot_rows if r.get('baseline_warmup_ok'))}",
                    f"- runtime_s: {pilot_runtime:.3f}",
                    f"- peak_rss_mib: {_peak_rss_mib():.1f}",
                    f"- pass: {pilot_ok}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        if not pilot_ok:
            verdict = "OB_ENRICHMENT_BLOCKED_IMPLEMENTATION"
            summary = {"pilot_runtime_s": pilot_runtime, "pilot_causal_violations": pilot_causal_viol}
            _write_json(out_dir / "runtime_metrics.json", {"pilot_runtime_s": pilot_runtime, "peak_rss_mib": _peak_rss_mib()})
            write_reports(out_dir, verdict=verdict, audit=audit, summary=summary)
            return {"verdict": verdict, "out_dir": str(out_dir)}

        if pilot_only or skip_full:
            verdict = "OB_ENRICHMENT_SUCCESS_LOW_SAMPLE"
            _write_json(
                out_dir / "run_manifest.json",
                {
                    "run_id": RUN_ID,
                    "verdict": verdict,
                    "mode": "pilot_only",
                    "params": params.to_dict(),
                    "split": split_meta,
                },
            )
            write_reports(out_dir, verdict=verdict, audit=audit, summary={"pilot_only": True})
            return {"verdict": verdict, "out_dir": str(out_dir)}

        # Full enrichment
        all_rows: list[dict[str, Any]] = []
        meta_rows: list[dict[str, Any]] = []
        full_t0 = time.time()
        for i, e in enumerate(events):
            wid = e["window_id"]
            if wid not in windows:
                raise RuntimeError(f"EVENT_WINDOW_NOT_IN_COMPLETE:{e['event_id']}:{wid}")
            row = enrich_one(client, event=e, window=windows[wid], params=params, chunk_cache=chunk_cache)
            meta = row.pop("_feature_meta_rows", [])
            meta_rows.extend(meta)
            all_rows.append(row)
            if (i + 1) % 20 == 0:
                log(f"progress {i+1}/{len(events)} rss={_peak_rss_mib():.1f}")
        full_runtime = time.time() - full_t0

        # Write parquet via pandas if available else csv
        try:
            import pandas as pd

            flat = []
            for r in all_rows:
                flat.append({k: v for k, v in r.items() if "__" not in k})
            pd.DataFrame(flat).to_parquet(out_dir / "event_features.parquet", index=False)
        except Exception:
            _write_csv(out_dir / "event_features.csv", [{k: v for k, v in r.items() if "__" not in k} for r in all_rows])

        # Feature availability summary
        feat_names = sorted({m["feature"] for m in meta_rows})
        avail_rows = []
        for fname in feat_names:
            subset = [m for m in meta_rows if m["feature"] == fname]
            n = len(subset)
            n_av = sum(1 for m in subset if m.get("available"))
            n_causal_bad = sum(1 for m in subset if not m.get("causal_valid"))
            avail_rows.append(
                {
                    "feature": fname,
                    "n": n,
                    "available": n_av,
                    "missing_rate": 1.0 - (n_av / n if n else 0.0),
                    "causal_invalid": n_causal_bad,
                    "source_table": subset[0].get("source_table") if subset else None,
                }
            )
        _write_csv(out_dir / "feature_availability.csv", avail_rows)

        # Feature catalog
        catalog = [
            {
                "feature": r["feature"],
                "source_table": r["source_table"],
                "missing_rate": r["missing_rate"],
            }
            for r in avail_rows
        ]
        _write_csv(out_dir / "feature_catalog.csv", catalog)

        analysis = run_analyses(
            events=events,
            episodes=episodes,
            outcomes=outcomes,
            feature_rows=all_rows,
            split_rows=split_rows,
            out_dir=out_dir,
        )
        xray = select_manual_xray_cases(
            events=events,
            episodes=episodes,
            outcomes=outcomes,
            feature_rows=all_rows,
        )
        _write_csv(out_dir / "manual_xray_cases.csv", xray["rows"])
        (out_dir / "MANUAL_XRAY_REPORT.md").write_text(xray["report_md"], encoding="utf-8")

        total_runtime = time.time() - t0
        runtime = {
            "pilot_runtime_s": pilot_runtime,
            "full_runtime_s": full_runtime,
            "total_runtime_s": total_runtime,
            "peak_rss_mib": _peak_rss_mib(),
            "n_events": len(all_rows),
            "n_pilot": len(pilot_rows),
        }
        _write_json(out_dir / "runtime_metrics.json", runtime)

        causal_viol = sum(1 for r in all_rows if not r.get("causal_ok"))
        trades_cov = sum(1 for r in all_rows if r.get("trades_available"))
        ob_complete = sum(
            1
            for r in all_rows
            if r.get("level_changes_available") and r.get("depth_at_zone_touch") is not None
        )
        missing_rate = sum(r["missing_rate"] for r in avail_rows) / len(avail_rows) if avail_rows else 1.0

        # Verdict
        if causal_viol > 0:
            verdict = "OB_ENRICHMENT_BLOCKED_IMPLEMENTATION"
        elif analysis.get("sample_flag") == "LOW":
            verdict = "OB_ENRICHMENT_SUCCESS_LOW_SAMPLE"
        elif analysis.get("edge_flag") == "LOW_SAMPLE":
            verdict = "OB_ENRICHMENT_SUCCESS_LOW_SAMPLE"
        elif analysis.get("edge_flag") == "NO_EDGE":
            verdict = "OB_ENRICHMENT_SUCCESS_NO_EDGE"
        else:
            verdict = "OB_ENRICHMENT_SUCCESS"

        summary = {
            **analysis.get("summary", {}),
            "input_events": len(events),
            "first_touch_events": sum(1 for e in episodes if _truthy(e.get("is_first_touch_of_zone_version"))),
            "non_overlapping_events": sum(1 for e in episodes if _truthy(e.get("selected_non_overlapping_30m"))),
            "discovery_events": split_meta.get("n_discovery_events"),
            "validation_events": split_meta.get("n_validation_events"),
            "events_with_ob_features": ob_complete,
            "events_with_trade_features": trades_cov,
            "feature_missing_rate": missing_rate,
            "causality_violations": causal_viol,
            "pilot_runtime_s": pilot_runtime,
            "total_runtime_s": total_runtime,
            "peak_rss_mib": _peak_rss_mib(),
        }
        _write_json(
            out_dir / "run_manifest.json",
            {
                "run_id": RUN_ID,
                "package": PACKAGE_NAME,
                "verdict": verdict,
                "created_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "params": params.to_dict(),
                "input_audit": audit,
                "split": split_meta,
                "runtime": runtime,
                "summary": summary,
                "note": "MP batch artifacts read-only; no MP recompute",
            },
        )
        write_reports(out_dir, verdict=verdict, audit=audit, summary=summary, analysis=analysis)
        log(f"done verdict={verdict}")
        return {"verdict": verdict, "out_dir": str(out_dir), "summary": summary}
    except Exception as exc:  # noqa: BLE001
        log("ERROR " + traceback.format_exc())
        if "MEMORY" in str(exc).upper() or "RESOURCE" in str(exc).upper():
            verdict = "OB_ENRICHMENT_RESOURCE_ABORT"
        else:
            verdict = "OB_ENRICHMENT_BLOCKED_DATA"
        _write_json(
            out_dir / "run_manifest.json",
            {"run_id": RUN_ID, "verdict": verdict, "error": str(exc)},
        )
        write_reports(out_dir, verdict=verdict, audit=audit, summary={"error": str(exc)})
        return {"verdict": verdict, "out_dir": str(out_dir), "error": str(exc)}
    finally:
        if own_client and client is not None:
            try:
                client.close()
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="mp_ob_feature_enrichment_v1")
    p.add_argument("--repo-root", type=Path, default=Path.cwd())
    p.add_argument("--batch-dir", type=Path, default=None)
    p.add_argument("--out-dir", type=Path, default=None)
    p.add_argument("--pilot-only", action="store_true")
    p.add_argument("--pilot-max", type=int, default=PILOT_MAX_EVENTS)
    args = p.parse_args(argv)
    repo = args.repo_root.resolve()
    params = EnrichmentParams(
        batch_run_dir=args.batch_dir or (repo / BATCH_RUN_REL),
        out_dir=args.out_dir or (repo / DEFAULT_RUN_REL),
        pilot_max_events=min(args.pilot_max, PILOT_MAX_EVENTS),
    )
    result = run_enrichment(repo_root=repo, params=params, pilot_only=args.pilot_only)
    print(json.dumps({"verdict": result.get("verdict"), "out_dir": result.get("out_dir")}, indent=2))
    return 0 if str(result.get("verdict", "")).startswith("OB_ENRICHMENT_SUCCESS") else 2
