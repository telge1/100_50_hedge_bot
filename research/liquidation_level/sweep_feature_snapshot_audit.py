"""CLI / exports for Phase C multi-timeframe feature snapshots."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.liquidation_level.liquidation_audit import DEFAULT_FEATHER
from research.liquidation_level.sweep_analysis_window import parse_window_sizes
from research.liquidation_level.sweep_feature_snapshots import (
    PHASE_B_EXPECTED_HASH,
    PhaseCValidationError,
    assert_no_entry_fields,
    build_phase_c_bundle,
    bundle_hash,
)
from research.liquidation_level.sweep_scanner_join import (
    SOURCE_CONFIG_ID,
    ensure_utc,
    select_timeline_event_indices,
)


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, (np.floating, float)):
        x = float(obj)
        return None if not np.isfinite(x) else x
    if isinstance(obj, (np.integer, int)):
        return int(obj)
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if obj is None or (isinstance(obj, float) and np.isnan(obj)):
        return None
    return obj


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(_jsonable(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _write_csv(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def build_summary(bundle, *, det_hash: str, runtime_s: float) -> dict[str, Any]:
    snaps = bundle.snapshots
    targets = bundle.targets
    path = bundle.path_aggregates
    avail = bundle.feature_availability
    feat_by_tf = {}
    for tf in ("5m", "15m", "30m"):
        feat_by_tf[tf] = int(avail.loc[avail["timeframe"] == tf, "feature_name"].nunique())
    avail_by_tf = {
        tf: float(avail.loc[avail["timeframe"] == tf, "availability_pct"].mean())
        if len(avail.loc[avail["timeframe"] == tf])
        else None
        for tf in ("5m", "15m", "30m")
    }
    missing = int((avail["availability_pct"] == 0).sum())
    target_counts = {}
    for ws, g in targets.groupby("window_size"):
        target_counts[str(int(ws))] = {
            "ended_below": int(g["target_ended_below_level"].sum()),
            "ended_above": int(g["target_ended_above_level"].sum()),
            "majority_below": int(g["target_majority_below"].sum()),
            "majority_above": int(g["target_majority_above"].sum()),
            "mixed": int(g["target_mixed_path"].sum()),
        }
    return {
        "event_counts": bundle.validation.get("reproduced_events"),
        "window_counts": {
            str(int(ws)): int(n) for ws, n in snaps.groupby("window_size").size().items()
        },
        "feature_count_total": int(avail["feature_name"].nunique()) if len(avail) else 0,
        "feature_count_by_timeframe": feat_by_tf,
        "availability_by_timeframe": avail_by_tf,
        "missing_feature_count": missing,
        "overlap_group_count": int(bundle.overlap_groups["overlap_group_id"].nunique())
        if len(bundle.overlap_groups)
        else 0,
        "events_with_overlap": int(
            (bundle.overlap_groups["overlapping_event_count"] > 0).sum()
        )
        if len(bundle.overlap_groups)
        else 0,
        "target_counts_by_window": target_counts,
        "15m_state_changes_by_window": {
            str(int(ws)): float(g["tf15_regime_changes"].mean())
            for ws, g in path.groupby("window_size")
        },
        "30m_state_changes_by_window": {
            str(int(ws)): float(g["tf30_regime_changes"].mean())
            for ws, g in path.groupby("window_size")
        },
        "deterministic_hash": det_hash,
        "leakage_checks_passed": bool(bundle.leakage_checks.get("passed")),
        "leakage_checks": bundle.leakage_checks,
        "phase_c_ready_for_phase_d": bool(
            bundle.leakage_checks.get("passed")
            and len(snaps) > 0
            and bundle.validation.get("ok", True)
        ),
        "runtime_seconds": runtime_s,
        "source_config_id": SOURCE_CONFIG_ID,
        "expected_phase_b_hash": PHASE_B_EXPECTED_HASH,
    }


def write_readme(path: Path, summary: dict[str, Any]) -> None:
    text = f"""# Phase C Results — Feature Snapshots

## What Phase C does

Builds PRE / SWEEP / END multi-timeframe feature snapshots for each
validated sweep event and window size (3/6/12), plus path aggregates,
descriptive target labels, overlap groups, and leakage timing metadata.

## Sweep is still not an entry

No reversal/breakout classifier, no TP/SL, no PnL, no fees.

## Targets

`target_*` columns are mechanical path descriptions for later Phase D.
They must never be used as PRE/SWEEP decision features.

## Readiness

phase_c_ready_for_phase_d = **{summary.get('phase_c_ready_for_phase_d')}**
leakage_checks_passed = **{summary.get('leakage_checks_passed')}**

Hash: `{summary.get('deterministic_hash')}`
"""
    path.write_text(text + "\n", encoding="utf-8")


def write_timeline(path: Path, rows: list[dict[str, Any]]) -> None:
    lines = ["# Phase C Timeline Samples", "", f"n={len(rows)}", ""]
    for r in rows:
        lines.extend(
            [
                f"## {r['event_id']} ws={r['window_size']}",
                f"- sample={r['sample']} signal={r['signal_index']}",
                f"- pre_5m={r.get('pre_5m_timestamp')} sweep={r.get('sweep_5m_timestamp')} end={r.get('end_5m_timestamp')}",
                f"- sweep_adx={r.get('sweep_5m_adx')} end_adx={r.get('end_5m_adx')}",
                f"- targets below/above={r.get('target_ended_below_level')}/{r.get('target_ended_above_level')}",
                "",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_phase_c_audit(
    *,
    feather_file: Path,
    phase_a_dir: Path,
    phase_b_dir: Path,
    output_dir: Path,
    symbol: str = "APTUSDT",
    window_sizes: str = "3,6,12",
    max_events: int | None = None,
    timeline_sample_size: int = 50,
    random_seed: int = 42,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    sizes = parse_window_sizes(window_sizes)

    def progress(msg: str) -> None:
        print(msg, flush=True)

    try:
        bundle = build_phase_c_bundle(
            phase_a_dir=Path(phase_a_dir),
            phase_b_dir=Path(phase_b_dir),
            feather_file=Path(feather_file),
            window_sizes=sizes,
            max_events=max_events,
            progress=progress,
        )
    except PhaseCValidationError as exc:
        _write_json(out / "input_validation.json", json.loads(str(exc)))
        raise

    _write_json(out / "input_validation.json", bundle.validation)
    assert_no_entry_fields(bundle.snapshots)
    assert_no_entry_fields(bundle.targets)

    det_hash = bundle_hash(bundle)
    summary = build_summary(bundle, det_hash=det_hash, runtime_s=time.perf_counter() - t0)
    if max_events is None:
        ec = bundle.validation.get("reproduced_events") or {}
        summary["phase_c_ready_for_phase_d"] = bool(
            summary["phase_c_ready_for_phase_d"]
            and ec.get("full") == 2696
            and summary["leakage_checks_passed"]
        )

    # timeline samples
    ev_ids = bundle.snapshots[["event_id", "sample", "signal_index"]].drop_duplicates("event_id")
    # build fake trigger-like list for selector
    class _E:
        def __init__(self, event_id, sample, signal_index, signal_timestamp):
            self.event_id = event_id
            self.sample = sample
            self.signal_index = signal_index
            self.signal_timestamp = signal_timestamp

    events_list = [
        _E(r.event_id, r.sample, int(r.signal_index), ensure_utc("2026-01-01T00:00:00Z"))
        for r in ev_ids.itertuples()
    ]
    # preserve order by signal_index
    events_list.sort(key=lambda e: e.signal_index)
    idxs = select_timeline_event_indices(events_list, seed=int(random_seed))[: int(timeline_sample_size)]
    pick = {events_list[i].event_id for i in idxs if 0 <= i < len(events_list)}
    tl = bundle.snapshots.loc[bundle.snapshots["event_id"].isin(pick)].merge(
        bundle.targets,
        on=["event_id", "window_size", "sample"],
        how="left",
    )
    timeline_rows = tl.head(200).to_dict(orient="records")

    config = {
        "symbol": symbol,
        "feather_file": str(Path(feather_file).expanduser().resolve()),
        "phase_a_dir": str(phase_a_dir),
        "phase_b_dir": str(phase_b_dir),
        "window_sizes": list(sizes),
        "expected_phase_b_hash": PHASE_B_EXPECTED_HASH,
        "max_events": max_events,
        "timeline_sample_size": timeline_sample_size,
        "random_seed": random_seed,
        "source_config_id": SOURCE_CONFIG_ID,
    }
    _write_json(out / "config.json", config)
    _write_csv(out / "feature_snapshots.csv", bundle.snapshots)
    _write_csv(out / "feature_deltas.csv", bundle.deltas)
    _write_csv(out / "path_aggregates.csv", bundle.path_aggregates)
    _write_csv(out / "target_labels.csv", bundle.targets)
    _write_csv(out / "feature_availability.csv", bundle.feature_availability)
    _write_csv(out / "feature_timing.csv", bundle.feature_timing)
    _write_csv(out / "categorical_transitions.csv", bundle.categorical_transitions)
    _write_csv(out / "descriptive_group_comparison.csv", bundle.descriptive_group_comparison)
    _write_csv(out / "overlap_groups.csv", bundle.overlap_groups)
    _write_csv(out / "overlap_group_comparison.csv", bundle.overlap_group_comparison)
    _write_csv(out / "timeline_samples.csv", pd.DataFrame(timeline_rows))
    write_timeline(out / "timeline_audit.md", timeline_rows)
    _write_json(out / "summary.json", summary)
    write_readme(out / "README_results.md", summary)

    progress(f"Snapshots: {len(bundle.snapshots)} Targets: {len(bundle.targets)}")
    progress(
        f"leakage_passed={summary['leakage_checks_passed']} ready={summary['phase_c_ready_for_phase_d']} hash={det_hash[:16]}…"
    )
    progress(f"Laufzeit: {summary['runtime_seconds']:.1f}s")
    return summary


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Phase C: sweep multi-TF feature snapshots")
    p.add_argument("--feather-file", type=Path, default=DEFAULT_FEATHER)
    p.add_argument(
        "--phase-a-dir",
        type=Path,
        default=Path("research/liquidation_level/results/APTUSDT_5m_sweep_scanner_phase_a"),
    )
    p.add_argument(
        "--phase-b-dir",
        type=Path,
        default=Path("research/liquidation_level/results/APTUSDT_5m_sweep_scanner_phase_b"),
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("research/liquidation_level/results/APTUSDT_5m_sweep_scanner_phase_c"),
    )
    p.add_argument("--symbol", type=str, default="APTUSDT")
    p.add_argument("--window-sizes", type=str, default="3,6,12")
    p.add_argument("--max-events", type=int, default=None)
    p.add_argument("--timeline-sample-size", type=int, default=50)
    p.add_argument("--random-seed", type=int, default=42)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_phase_c_audit(
            feather_file=args.feather_file,
            phase_a_dir=args.phase_a_dir,
            phase_b_dir=args.phase_b_dir,
            output_dir=args.output_dir,
            symbol=args.symbol,
            window_sizes=args.window_sizes,
            max_events=args.max_events,
            timeline_sample_size=args.timeline_sample_size,
            random_seed=args.random_seed,
        )
    except PhaseCValidationError as exc:
        print(str(exc), flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
