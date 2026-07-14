"""CLI / exports for Phase B sweep analysis windows."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.liquidation_level.liquidation_audit import DEFAULT_FEATHER, load_feather
from research.liquidation_level.liquidation_levels import normalize_ohlcv_dataframe
from research.liquidation_level.sweep_analysis_window import (
    DEFAULT_WINDOW_SIZES,
    AnalysisWindowBundle,
    build_analysis_windows_for_events,
    bundle_deterministic_hash,
    load_or_build_phase_a_inputs,
    parse_window_sizes,
)
from research.liquidation_level.sweep_scanner_join import (
    SOURCE_CONFIG_ID,
    EventCountMismatchError,
    ensure_utc,
    select_timeline_event_indices,
)


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, pd.Timestamp):
        return ensure_utc(obj).isoformat()
    if isinstance(obj, (np.floating, float)):
        x = float(obj)
        return None if not np.isfinite(x) else x
    if isinstance(obj, (np.integer, int)):
        return int(obj)
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if obj is None:
        return None
    return obj


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(_jsonable(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    pd.DataFrame(rows).to_csv(path, index=False)


def feature_delta_rows(bundle: AnalysisWindowBundle) -> list[dict[str, Any]]:
    rows = []
    for b in bundle.bars:
        rows.append(
            {
                "event_id": b.event_id,
                "window_size": b.window_size,
                "window_offset": b.window_offset,
                "candle_index": b.candle_index,
                "timestamp": ensure_utc(b.timestamp).isoformat(),
                **{f"delta_{k}": v for k, v in b.deltas.items()},
                "current_regime": b.current_5m.get("regime"),
                "frozen_regime": b.frozen_5m.get("regime"),
                "current_structure_bias": b.current_5m.get("structure_bias"),
                "frozen_structure_bias": b.frozen_5m.get("structure_bias"),
            }
        )
    return rows


def build_summary(bundle: AnalysisWindowBundle, *, det_hash: str, runtime_s: float) -> dict[str, Any]:
    sizes = sorted({w.window_size for w in bundle.windows})
    by_size = {s: [w for w in bundle.windows if w.window_size == s] for s in sizes}
    metrics_by_size = {s: [m for m in bundle.path_metrics if m.window_size == s] for s in sizes}

    def _mean(xs: list[float | None]) -> float | None:
        vals = [float(x) for x in xs if x is not None and np.isfinite(float(x))]
        return None if not vals else float(np.mean(vals))

    def _path_mean(key: str, s: int) -> float | None:
        return _mean([m.metrics.get(key) for m in metrics_by_size[s]])

    avail = [w.available_candle_count for w in bundle.windows]
    overlap = bundle.validation.get("overlap_summary") or {}
    feat_cov = {
        "bars_with_ema_9": 100.0
        * sum(1 for b in bundle.bars if b.current_5m.get("ema_9") is not None)
        / max(1, len(bundle.bars)),
        "bars_with_regime": 100.0
        * sum(1 for b in bundle.bars if b.current_5m.get("regime") is not None)
        / max(1, len(bundle.bars)),
        "bars_with_volume_ratio": 100.0
        * sum(1 for b in bundle.bars if b.current_5m.get("volume_ratio") is not None)
        / max(1, len(bundle.bars)),
        "bars_with_pa_state": 100.0
        * sum(1 for b in bundle.bars if b.current_5m.get("price_action_state") is not None)
        / max(1, len(bundle.bars)),
    }
    n15 = sum(1 for u in bundle.htf_updates if u.get("timeframe") == "15m")
    n30 = sum(1 for u in bundle.htf_updates if u.get("timeframe") == "30m")

    ready = bool(len(bundle.windows) > 0)
    if bundle.bars:
        for b in bundle.bars:
            sig = next(
                w.signal_index
                for w in bundle.windows
                if w.event_id == b.event_id and w.window_size == b.window_size
            )
            if b.candle_index == sig or b.window_offset < 1:
                ready = False
                break
            if b.available_at <= b.timestamp:
                ready = False
                break
    for w in bundle.windows:
        if w.start_index != w.signal_index + 1:
            ready = False
            break

    return {
        "expected_event_counts": bundle.validation.get("expected"),
        "reproduced_event_counts": bundle.validation.get("reproduced"),
        "window_sizes": list(sizes),
        "windows_created_by_size": {str(s): len(by_size[s]) for s in sizes},
        "complete_windows_by_size": {
            str(s): sum(1 for w in by_size[s] if w.complete) for s in sizes
        },
        "incomplete_windows_by_size": {
            str(s): sum(1 for w in by_size[s] if not w.complete) for s in sizes
        },
        "average_available_candles": None if not avail else float(np.mean(avail)),
        "median_available_candles": None if not avail else float(np.median(avail)),
        "events_with_overlaps": overlap.get("events_with_overlaps"),
        "maximum_concurrent_windows": overlap.get("maximum_concurrent_windows"),
        "mean_closes_above_level_by_window": {
            str(s): _path_mean("candles_closed_above_level", s) for s in sizes
        },
        "mean_closes_below_level_by_window": {
            str(s): _path_mean("candles_closed_below_level", s) for s in sizes
        },
        "mean_level_crosses_by_window": {
            str(s): _path_mean("number_of_level_crosses", s) for s in sizes
        },
        "mean_reclaims_by_window": {
            str(s): _path_mean("number_of_reclaims_below", s) for s in sizes
        },
        "15m_update_count_inside_windows": n15,
        "30m_update_count_inside_windows": n30,
        "feature_availability": feat_cov,
        "deterministic_hash": det_hash,
        "phase_b_ready_for_phase_c": ready,
        "runtime_seconds": runtime_s,
        "source_config_id": SOURCE_CONFIG_ID,
        "bar_count": len(bundle.bars),
        "window_count": len(bundle.windows),
    }


def write_timeline_markdown(path: Path, rows: list[dict[str, Any]]) -> None:
    lines = [
        "# Phase B Timeline Audit",
        "",
        "Sweep is **not** an entry. Windows observe follow candles only.",
        "",
        f"Sampled rows: {len(rows)}",
        "",
    ]
    for r in rows:
        lines.extend(
            [
                f"## {r['event_id']} window={r['window_size']}",
                "",
                f"- Sample: `{r['sample']}` status=`{r['status']}` complete=`{r['complete']}`",
                f"- Sweep signal_index={r['signal_index']} → start_index={r['start_index']}",
                f"- Frozen level={r['initial_sweep_level']}",
                f"- Available bars={r['available_candle_count']}/{r['expected_candle_count']}",
                f"- Follow offsets: {r['follow_offsets']}",
                f"- Level path: closes_above={r['candles_closed_above_level']} "
                f"closes_below={r['candles_closed_below_level']} "
                f"crosses={r['number_of_level_crosses']} reclaims={r['number_of_reclaims_below']}",
                f"- HTF updates in window: 15m={r['htf15_updates']} 30m={r['htf30_updates']}",
                f"- State path: {r['state_path']}",
                "",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_readme(path: Path, summary: dict[str, Any]) -> None:
    text = f"""# Phase B Results — Sweep Analysis Windows

## What Phase B does

For each validated upper 50x immediate-reclaim sweep, Phase B opens causal
analysis windows of the next **3 / 6 / 12** closed 5m candles.

## Sweep is still not an entry

No reversal/breakout classification, momentum entry, TP/SL, fees, or PnL.

## Frozen vs dynamic

- Frozen: Phase-A 5m/15m/30m snapshot at sweep close (never overwritten)
- Dynamic: features as each follow candle closes
- HTF: last fully closed 15m/30m bucket as-of that follow candle's close

## Overlaps

Overlapping windows are kept separately. Diagnostics report concurrent coverage.

## Completeness

Incomplete windows near end-of-data are kept with
`status=INCOMPLETE_END_OF_DATA` and `complete=false`.

## Readiness

phase_b_ready_for_phase_c = **{summary.get('phase_b_ready_for_phase_c')}**

Hash: `{summary.get('deterministic_hash')}`
"""
    path.write_text(text + "\n", encoding="utf-8")


def build_timeline_rows(
    bundle: AnalysisWindowBundle,
    *,
    event_indices: list[int],
    events,
) -> list[dict[str, Any]]:
    by_event = {e.event_id: e for e in events}
    selected_ids = [events[i].event_id for i in event_indices if 0 <= i < len(events)]
    rows = []
    for w in bundle.windows:
        if w.event_id not in selected_ids:
            continue
        bars = [b for b in bundle.bars if b.event_id == w.event_id and b.window_size == w.window_size]
        metrics = next(
            (
                m
                for m in bundle.path_metrics
                if m.event_id == w.event_id and m.window_size == w.window_size
            ),
            None,
        )
        h15 = sum(
            1
            for u in bundle.htf_updates
            if u["event_id"] == w.event_id and u["window_size"] == w.window_size and u["timeframe"] == "15m"
        )
        h30 = sum(
            1
            for u in bundle.htf_updates
            if u["event_id"] == w.event_id and u["window_size"] == w.window_size and u["timeframe"] == "30m"
        )
        if bars:
            states = [bars[0].state_before] + [b.state_after for b in bars]
        else:
            states = [w.status]
        rows.append(
            {
                "event_id": w.event_id,
                "sample": w.sample,
                "window_size": w.window_size,
                "status": w.status,
                "complete": w.complete,
                "signal_index": w.signal_index,
                "start_index": w.start_index,
                "end_index": w.end_index,
                "initial_sweep_level": w.initial_sweep_level,
                "available_candle_count": w.available_candle_count,
                "expected_candle_count": w.expected_candle_count,
                "follow_offsets": ",".join(str(b.window_offset) for b in bars),
                "candles_closed_above_level": None
                if metrics is None
                else metrics.metrics.get("candles_closed_above_level"),
                "candles_closed_below_level": None
                if metrics is None
                else metrics.metrics.get("candles_closed_below_level"),
                "number_of_level_crosses": None
                if metrics is None
                else metrics.metrics.get("number_of_level_crosses"),
                "number_of_reclaims_below": None
                if metrics is None
                else metrics.metrics.get("number_of_reclaims_below"),
                "htf15_updates": h15,
                "htf30_updates": h30,
                "state_path": " → ".join(states),
                "signal_timestamp": ensure_utc(by_event[w.event_id].signal_timestamp).isoformat()
                if w.event_id in by_event
                else None,
            }
        )
    return rows


def run_phase_b_audit(
    *,
    feather_file: Path,
    phase_a_dir: Path,
    output_dir: Path,
    symbol: str = "APTUSDT",
    window_sizes: str | None = "3,6,12",
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

    progress(f"loading {feather_file}")
    raw = load_feather(Path(feather_file).expanduser().resolve())
    data = normalize_ohlcv_dataframe(raw)
    progress(f"Candles geladen: {len(data)} symbol={symbol}")

    expect_counts = max_events is None
    try:
        events, snaps, store, validation = load_or_build_phase_a_inputs(
            data, expect_counts=expect_counts, progress=progress
        )
    except EventCountMismatchError as exc:
        _atomic_write_json(out / "event_validation.json", json.loads(str(exc)))
        raise

    if max_events is not None:
        events = list(events)[: int(max_events)]
        snaps = list(snaps)[: int(max_events)]
        validation["reproduced"] = {
            "full": len(events),
            "in_sample": sum(1 for e in events if e.sample == "in_sample"),
            "out_of_sample": sum(1 for e in events if e.sample == "out_of_sample"),
            "truncated": True,
        }
    else:
        validation["reproduced"] = {
            "full": len(events),
            "in_sample": sum(1 for e in events if e.sample == "in_sample"),
            "out_of_sample": sum(1 for e in events if e.sample == "out_of_sample"),
        }

    _atomic_write_json(
        out / "event_validation.json",
        {
            **validation,
            "phase_a_dir": str(phase_a_dir),
            "expected": {"full": 2696, "in_sample": 1824, "out_of_sample": 872},
        },
    )

    bundle = build_analysis_windows_for_events(
        events,
        store=store,
        frozen_snapshots=snaps,
        window_sizes=sizes,
        progress=progress,
    )
    bundle.validation["reproduced"] = validation["reproduced"]
    bundle.validation["expected"] = {
        "full": 2696,
        "in_sample": 1824,
        "out_of_sample": 872,
    }

    det_hash = bundle_deterministic_hash(bundle)
    idxs = select_timeline_event_indices(events, seed=int(random_seed))[: int(timeline_sample_size)]
    timeline_rows = build_timeline_rows(bundle, event_indices=idxs, events=events)

    summary = build_summary(bundle, det_hash=det_hash, runtime_s=time.perf_counter() - t0)
    # tighten readiness with event counts when full run
    if max_events is None:
        ok_counts = (
            validation["reproduced"]["full"] == 2696
            and validation["reproduced"]["in_sample"] == 1824
            and validation["reproduced"]["out_of_sample"] == 872
        )
        summary["phase_b_ready_for_phase_c"] = bool(
            summary["phase_b_ready_for_phase_c"] and ok_counts
        )

    config = {
        "symbol": symbol,
        "feather_file": str(Path(feather_file).expanduser().resolve()),
        "phase_a_dir": str(phase_a_dir),
        "window_sizes": list(sizes),
        "source_config_id": SOURCE_CONFIG_ID,
        "max_events": max_events,
        "timeline_sample_size": timeline_sample_size,
        "random_seed": random_seed,
        "candles": len(data),
    }
    _atomic_write_json(out / "config.json", config)
    _write_csv(out / "analysis_windows.csv", [w.to_dict() for w in bundle.windows])
    _write_csv(out / "analysis_bars.csv", [b.to_dict() for b in bundle.bars])
    _write_csv(out / "window_path_metrics.csv", [m.to_dict() for m in bundle.path_metrics])
    _write_csv(out / "window_feature_deltas.csv", feature_delta_rows(bundle))
    _write_csv(out / "htf_updates.csv", bundle.htf_updates)
    _write_csv(out / "overlap_diagnostics.csv", bundle.overlap_rows)
    _write_csv(out / "incomplete_windows.csv", bundle.incomplete_windows)
    _write_csv(out / "timeline_samples.csv", timeline_rows)
    write_timeline_markdown(out / "timeline_audit.md", timeline_rows)
    _atomic_write_json(out / "summary.json", summary)
    write_readme(out / "README_results.md", summary)

    progress(f"Windows: {summary['window_count']} Bars: {summary['bar_count']}")
    progress(f"Overlaps events={summary['events_with_overlaps']} max_concurrent={summary['maximum_concurrent_windows']}")
    progress(f"phase_b_ready_for_phase_c={summary['phase_b_ready_for_phase_c']} hash={det_hash[:16]}…")
    progress(f"Laufzeit: {summary['runtime_seconds']:.1f}s")
    return summary


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Phase B: sweep-activated analysis windows")
    p.add_argument("--feather-file", type=Path, default=DEFAULT_FEATHER)
    p.add_argument(
        "--phase-a-dir",
        type=Path,
        default=Path("research/liquidation_level/results/APTUSDT_5m_sweep_scanner_phase_a"),
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("research/liquidation_level/results/APTUSDT_5m_sweep_scanner_phase_b"),
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
        run_phase_b_audit(
            feather_file=args.feather_file,
            phase_a_dir=args.phase_a_dir,
            output_dir=args.output_dir,
            symbol=args.symbol,
            window_sizes=args.window_sizes,
            max_events=args.max_events,
            timeline_sample_size=args.timeline_sample_size,
            random_seed=args.random_seed,
        )
    except EventCountMismatchError as exc:
        print(str(exc), flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
