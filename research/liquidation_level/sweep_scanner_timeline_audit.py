"""CLI / exports for Phase A sweep↔scanner causal timeline audit."""

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
from research.liquidation_level.sweep_scanner_join import (
    SOURCE_CONFIG_ID,
    STALE_15M_AGE_MINUTES,
    STALE_30M_AGE_MINUTES,
    EventCountMismatchError,
    decision_time_from_signal_open,
    ensure_utc,
    forming_and_used_htf,
    freeze_snapshot,
    join_all_events,
    join_sweep_event,
    precompute_scanner_feature_store,
    reproduce_winner_events,
    select_timeline_event_indices,
    snapshots_deterministic_hash,
    validation_events_to_triggers,
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


def _flatten_snapshot_row(snap_dict: dict[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "event_id": snap_dict["event_id"],
        "signal_index": snap_dict["signal_index"],
        "signal_timestamp": snap_dict["signal_timestamp"],
        "sample": snap_dict["sample"],
        "decision_time": snap_dict["decision_time"],
        "tf5_timestamp": snap_dict["tf5_timestamp"],
        "tf5_age_minutes": snap_dict["tf5_age_minutes"],
        "tf5_exact_match": snap_dict["tf5_exact_match"],
        "tf15_bucket_start": snap_dict["tf15_bucket_start"],
        "tf15_bucket_end": snap_dict["tf15_bucket_end"],
        "tf15_available_at": snap_dict["tf15_available_at"],
        "tf15_age_minutes": snap_dict["tf15_age_minutes"],
        "tf15_is_closed": snap_dict["tf15_is_closed"],
        "tf30_bucket_start": snap_dict["tf30_bucket_start"],
        "tf30_bucket_end": snap_dict["tf30_bucket_end"],
        "tf30_available_at": snap_dict["tf30_available_at"],
        "tf30_age_minutes": snap_dict["tf30_age_minutes"],
        "tf30_is_closed": snap_dict["tf30_is_closed"],
    }
    for tf_key, prefix in (("features_5m", "f5_"), ("features_15m", "f15_"), ("features_30m", "f30_")):
        for k, v in (snap_dict.get(tf_key) or {}).items():
            row[f"{prefix}{k}"] = v
    for k, v in (snap_dict.get("diagnostics") or {}).items():
        if k == "join_warnings":
            row["join_warnings"] = "|".join(str(x) for x in (v or []))
        else:
            row[f"diag_{k}"] = v
    for k, v in (snap_dict.get("availability_flags") or {}).items():
        row[f"avail_{k}"] = v
    return row


def build_timeline_sample_row(
    *,
    event,
    snap,
    ohlcv: pd.DataFrame,
) -> dict[str, Any]:
    i = int(event.signal_index)
    ts = pd.to_datetime(ohlcv["timestamp"], utc=True)
    decision = snap.decision_time
    prev_ts = ensure_utc(ts.iloc[i - 1]).isoformat() if i > 0 else None
    next_ts = ensure_utc(ts.iloc[i + 1]).isoformat() if i + 1 < len(ts) else None
    used15 = forming_and_used_htf(decision, "15m", snap.tf15_bucket_start)
    used30 = forming_and_used_htf(decision, "30m", snap.tf30_bucket_start)
    return {
        "event_id": event.event_id,
        "signal_index": i,
        "signal_timestamp": ensure_utc(event.signal_timestamp).isoformat(),
        "decision_time": ensure_utc(decision).isoformat(),
        "sample": event.sample,
        "sweep_candle_open_time": ensure_utc(event.signal_timestamp).isoformat(),
        "sweep_candle_close_time": ensure_utc(decision).isoformat(),
        "prev_5m_timestamp": prev_ts,
        "next_5m_timestamp": next_ts,
        "used_15m_bucket_start": snap.tf15_bucket_start.isoformat() if snap.tf15_bucket_start is not None else None,
        "used_15m_bucket_end": snap.tf15_bucket_end.isoformat() if snap.tf15_bucket_end is not None else None,
        "used_15m_available_at": snap.tf15_available_at.isoformat() if snap.tf15_available_at is not None else None,
        "forming_15m_bucket_start": used15["forming_bucket_start"],
        "forming_15m_bucket_end": used15["forming_bucket_end"],
        "forming_15m_excluded_reason": used15["reason_excluded_if_open"],
        "used_30m_bucket_start": snap.tf30_bucket_start.isoformat() if snap.tf30_bucket_start is not None else None,
        "used_30m_bucket_end": snap.tf30_bucket_end.isoformat() if snap.tf30_bucket_end is not None else None,
        "used_30m_available_at": snap.tf30_available_at.isoformat() if snap.tf30_available_at is not None else None,
        "forming_30m_bucket_start": used30["forming_bucket_start"],
        "forming_30m_bucket_end": used30["forming_bucket_end"],
        "forming_30m_excluded_reason": used30["reason_excluded_if_open"],
        "causal_note": (
            "decision_time = signal_timestamp + 5m (sweep close). "
            "HTF buckets require close_time <= decision_time; incomplete buckets are excluded."
        ),
        "join_ok": snap.diagnostics.get("join_ok"),
        "tf5_exact_match": snap.tf5_exact_match,
        "tf15_age_minutes": snap.tf15_age_minutes,
        "tf30_age_minutes": snap.tf30_age_minutes,
    }


def write_timeline_markdown(path: Path, rows: list[dict[str, Any]]) -> None:
    lines = [
        "# Phase A Timeline Audit",
        "",
        "Sweep is **not** an entry. Snapshots freeze scanner state at sweep close.",
        "",
        f"Sampled events: {len(rows)}",
        "",
    ]
    for r in rows:
        lines.extend(
            [
                f"## {r['event_id']} @ {r['signal_timestamp']}",
                "",
                f"- Sample: `{r['sample']}`",
                f"- Sweep open/close: `{r['sweep_candle_open_time']}` → `{r['sweep_candle_close_time']}`",
                f"- Prev/next 5m: `{r['prev_5m_timestamp']}` / `{r['next_5m_timestamp']}`",
                f"- Used 15m: `{r['used_15m_bucket_start']}`–`{r['used_15m_bucket_end']}` (available `{r['used_15m_available_at']}`)",
                f"- Forming 15m (excluded if open): `{r['forming_15m_bucket_start']}`–`{r['forming_15m_bucket_end']}`",
                f"  - Reason: {r['forming_15m_excluded_reason']}",
                f"- Used 30m: `{r['used_30m_bucket_start']}`–`{r['used_30m_bucket_end']}` (available `{r['used_30m_available_at']}`)",
                f"- Forming 30m (excluded if open): `{r['forming_30m_bucket_start']}`–`{r['forming_30m_bucket_end']}`",
                f"  - Reason: {r['forming_30m_excluded_reason']}",
                f"- Ages: 15m={r['tf15_age_minutes']} min, 30m={r['tf30_age_minutes']} min",
                f"- Join ok / 5m exact: {r['join_ok']} / {r['tf5_exact_match']}",
                f"- Note: {r['causal_note']}",
                "",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def feature_availability_rows(snapshots: list) -> list[dict[str, Any]]:
    if not snapshots:
        return []
    n = len(snapshots)
    rows: list[dict[str, Any]] = []
    feature_keys: set[tuple[str, str]] = set()
    flag_keys: set[str] = set()
    for s in snapshots:
        flag_keys.update(s.availability_flags.keys())
        for tf, feats in (("5m", s.features_5m), ("15m", s.features_15m), ("30m", s.features_30m)):
            for k in feats.keys():
                feature_keys.add((tf, k))
    for tf, name in sorted(feature_keys):
        feat_attr = {"5m": "features_5m", "15m": "features_15m", "30m": "features_30m"}[tf]
        count = sum(1 for s in snapshots if getattr(s, feat_attr).get(name) is not None)
        rows.append(
            {
                "feature_key": f"{tf}.{name}",
                "available_count": count,
                "available_pct": 100.0 * count / n,
                "kind": "feature_value",
            }
        )
    for key in sorted(flag_keys):
        count = sum(1 for s in snapshots if bool(s.availability_flags.get(key)))
        rows.append(
            {
                "feature_key": key,
                "available_count": count,
                "available_pct": 100.0 * count / n,
                "kind": "availability_flag",
            }
        )
    return rows


def build_summary(
    *,
    validation: dict[str, Any],
    snapshots: list,
    det_hash: str,
    runtime_s: float,
) -> dict[str, Any]:
    n = len(snapshots)
    ages15 = [s.tf15_age_minutes for s in snapshots if s.tf15_age_minutes is not None]
    ages30 = [s.tf30_age_minutes for s in snapshots if s.tf30_age_minutes is not None]
    avail_rows = feature_availability_rows(snapshots)
    feat_pct = {
        r["feature_key"]: r["available_pct"]
        for r in avail_rows
        if r["kind"] == "feature_value" and r["feature_key"] in {
            "5m.ema_9",
            "5m.ema_200",
            "5m.adx",
            "5m.regime",
            "5m.structure_bias",
            "15m.ema_20",
            "15m.regime",
            "30m.ema_20",
            "30m.regime",
            "5m.volume_ratio",
            "5m.price_action_state",
            "5m.momentum_state",
        }
    }
    join_ok = sum(1 for s in snapshots if s.diagnostics.get("join_ok"))
    return {
        "expected_event_counts": validation.get("expected"),
        "reproduced_event_counts": validation.get("reproduced"),
        "join_success_count": join_ok,
        "join_failure_count": n - join_ok,
        "missing_5m_count": sum(1 for s in snapshots if s.diagnostics.get("missing_5m")),
        "missing_15m_count": sum(1 for s in snapshots if s.diagnostics.get("missing_15m")),
        "missing_30m_count": sum(1 for s in snapshots if s.diagnostics.get("missing_30m")),
        "warmup_incomplete_counts": {
            "5m": sum(1 for s in snapshots if not s.diagnostics.get("warmup_complete_5m")),
            "15m": sum(1 for s in snapshots if not s.diagnostics.get("warmup_complete_15m")),
            "30m": sum(1 for s in snapshots if not s.diagnostics.get("warmup_complete_30m")),
        },
        "exact_5m_match_rate": None if n == 0 else 100.0 * sum(1 for s in snapshots if s.tf5_exact_match) / n,
        "median_15m_age_minutes": None if not ages15 else float(np.median(ages15)),
        "max_15m_age_minutes": None if not ages15 else float(np.max(ages15)),
        "median_30m_age_minutes": None if not ages30 else float(np.median(ages30)),
        "max_30m_age_minutes": None if not ages30 else float(np.max(ages30)),
        "feature_availability_percentages": feat_pct,
        "deterministic_hash": det_hash,
        "stale_thresholds_minutes": {
            "15m": STALE_15M_AGE_MINUTES,
            "30m": STALE_30M_AGE_MINUTES,
            "note": "diagnostic only; not a trading threshold",
        },
        "phase_b_ready": bool(
            validation.get("reproduced", {}).get("full") == validation.get("expected", {}).get("full")
            and join_ok == n
            and n > 0
            and all(s.tf5_exact_match for s in snapshots)
            and all(
                (s.tf15_bucket_end is None or s.tf15_bucket_end <= s.decision_time)
                and (s.tf30_bucket_end is None or s.tf30_bucket_end <= s.decision_time)
                for s in snapshots
            )
        ),
        "runtime_seconds": runtime_s,
        "source_config_id": SOURCE_CONFIG_ID,
    }


def write_readme(path: Path, summary: dict[str, Any]) -> None:
    ready = summary.get("phase_b_ready")
    text = f"""# Phase A Results — Sweep ↔ Scanner Join

## What Phase A does

Phase A reproduces the frozen winner liquidation sweeps and freezes the scanner
5m / 15m / 30m state that is causally available at each sweep candle **close**.

## Sweep is not an entry

The validated upper 50x immediate-reclaim event only opens an analysis context.
No entry, TP, SL, or PnL is computed here.

## What was frozen

- Same closed 5m sweep candle features (EMAs, ADX/DI, ATR, regime label, structure)
- Last fully closed 15m bucket as-of decision_time = signal_timestamp + 5m
- Last fully closed 30m bucket with the same rule
- Liquidation volume_ratio on the sweep 5m bar (SMA13)

## Why 15m/30m can be older than the sweep

Higher-timeframe candles are only visible after their bucket close.
If a sweep closes while a 15m/30m bucket is still forming, Phase A uses the previous
closed bucket. That age is expected and not a join failure.

## Why forming HTF candles are excluded

Using a still-open bucket would be lookahead. Aggregation requires a complete contiguous
set of 5m bars and `close_time <= decision_time`.

## Missing / unavailable features

- Full trend state machine timeline is not forced on; Phase A exposes a structure-bias proxy only
- Price-action and momentum state machines are not armed by the sweep → marked unavailable
- Bollinger bands and 1m data remain absent

## Stale flags

`stale_15m` / `stale_30m` use diagnostic thresholds of {STALE_15M_AGE_MINUTES:.0f} / {STALE_30M_AGE_MINUTES:.0f}
minutes. They are informational only.

## Event counts

Expected: {summary.get('expected_event_counts')}
Reproduced: {summary.get('reproduced_event_counts')}

## Phase B readiness

phase_b_ready = **{ready}**

Deterministic hash: `{summary.get('deterministic_hash')}`
"""
    path.write_text(text + "\n", encoding="utf-8")


def filter_ohlcv_window(
    ohlcv: pd.DataFrame,
    *,
    start_date: str | None,
    end_date: str | None,
) -> pd.DataFrame:
    data = normalize_ohlcv_dataframe(ohlcv)
    if start_date:
        start = ensure_utc(start_date)
        data = data.loc[data["timestamp"] >= start].reset_index(drop=True)
    if end_date:
        end = ensure_utc(end_date)
        data = data.loc[data["timestamp"] <= end].reset_index(drop=True)
    return data


def run_phase_a_audit(
    *,
    feather_file: Path,
    output_dir: Path,
    symbol: str = "APTUSDT",
    optimizer_dir: Path | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    max_events: int | None = None,
    timeline_sample_size: int = 50,
    random_seed: int = 42,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    def progress(msg: str) -> None:
        print(msg, flush=True)

    progress(f"loading {feather_file}")
    raw = load_feather(Path(feather_file).expanduser().resolve())
    data = filter_ohlcv_window(raw, start_date=start_date, end_date=end_date)
    progress(f"Candles geladen: {len(data)} symbol={symbol}")

    expect_counts = start_date is None and end_date is None and max_events is None
    try:
        validation_events, replay, validation = reproduce_winner_events(
            data, expect_counts=expect_counts
        )
    except EventCountMismatchError as exc:
        _atomic_write_json(out / "event_validation.json", json.loads(str(exc)))
        raise

    if max_events is not None:
        validation_events = list(validation_events)[: int(max_events)]
        validation["reproduced"] = {
            "full": len(validation_events),
            "in_sample": sum(1 for e in validation_events if e.sample == "in_sample"),
            "out_of_sample": sum(1 for e in validation_events if e.sample == "out_of_sample"),
            "truncated": True,
        }
        expect_counts = False

    progress(
        f"Sweep-Events reproduziert: full={validation['reproduced']['full']} "
        f"IS={validation['reproduced']['in_sample']} OOS={validation['reproduced']['out_of_sample']}"
    )
    _atomic_write_json(out / "event_validation.json", validation)

    triggers = validation_events_to_triggers(validation_events, replay, data)
    store = precompute_scanner_feature_store(data, progress=progress)
    snapshots = join_all_events(triggers, store, progress=progress)
    snapshots = [freeze_snapshot(s) for s in snapshots]

    # Freeze integrity: mutate store structure after freeze must not affect snapshots
    if store.structure_5m:
        store.structure_5m[0]["structure_bias"] = "__mutated_after_freeze__"

    det_hash = snapshots_deterministic_hash(snapshots)

    # timeline samples
    idxs = select_timeline_event_indices(triggers, seed=int(random_seed))
    if timeline_sample_size is not None:
        idxs = idxs[: int(timeline_sample_size)]
    timeline_rows = [
        build_timeline_sample_row(event=triggers[i], snap=snapshots[i], ohlcv=data)
        for i in idxs
    ]

    joined_rows = [_flatten_snapshot_row(s.to_dict()) for s in snapshots]
    diag_rows = [
        {
            "event_id": s.event_id,
            "signal_index": s.signal_index,
            "signal_timestamp": ensure_utc(s.signal_timestamp).isoformat(),
            **{k: v for k, v in s.diagnostics.items() if k != "join_warnings"},
            "join_warnings": "|".join(str(x) for x in (s.diagnostics.get("join_warnings") or [])),
        }
        for s in snapshots
    ]
    avail_rows = feature_availability_rows(snapshots)
    summary = build_summary(
        validation=validation,
        snapshots=snapshots,
        det_hash=det_hash,
        runtime_s=time.perf_counter() - t0,
    )

    config_payload = {
        "symbol": symbol,
        "feather_file": str(Path(feather_file).expanduser().resolve()),
        "optimizer_dir": str(optimizer_dir) if optimizer_dir else None,
        "source_config_id": SOURCE_CONFIG_ID,
        "start_date": start_date,
        "end_date": end_date,
        "max_events": max_events,
        "timeline_sample_size": timeline_sample_size,
        "random_seed": random_seed,
        "stale_15m_age_minutes": STALE_15M_AGE_MINUTES,
        "stale_30m_age_minutes": STALE_30M_AGE_MINUTES,
        "decision_time_rule": "signal_timestamp_open + 5m = sweep close",
        "candles": len(data),
    }
    _atomic_write_json(out / "config.json", config_payload)
    _write_csv(out / "sweep_events.csv", [t.to_dict() for t in triggers])
    _write_csv(out / "joined_snapshots.csv", joined_rows)
    _write_csv(out / "feature_availability.csv", avail_rows)
    _write_csv(out / "join_diagnostics.csv", diag_rows)
    _write_csv(out / "timeline_samples.csv", timeline_rows)
    write_timeline_markdown(out / "timeline_audit.md", timeline_rows)
    _atomic_write_json(out / "summary.json", summary)
    write_readme(out / "README_results.md", summary)

    progress(f"Join-Fehler: {summary['join_failure_count']}")
    progress(f"Laufzeit: {summary['runtime_seconds']:.1f}s")
    progress(f"phase_b_ready={summary['phase_b_ready']} hash={det_hash[:16]}…")
    return summary


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Phase A: causal sweep↔scanner join + timeline audit")
    p.add_argument("--feather-file", type=Path, default=DEFAULT_FEATHER)
    p.add_argument(
        "--optimizer-dir",
        type=Path,
        default=Path("research/liquidation_level/results/APTUSDT_5m_optimizer_v1"),
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("research/liquidation_level/results/APTUSDT_5m_sweep_scanner_phase_a"),
    )
    p.add_argument("--symbol", type=str, default="APTUSDT")
    p.add_argument("--start-date", type=str, default=None)
    p.add_argument("--end-date", type=str, default=None)
    p.add_argument("--max-events", type=int, default=None)
    p.add_argument("--timeline-sample-size", type=int, default=50)
    p.add_argument("--random-seed", type=int, default=42)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_phase_a_audit(
            feather_file=args.feather_file,
            output_dir=args.output_dir,
            symbol=args.symbol,
            optimizer_dir=args.optimizer_dir,
            start_date=args.start_date,
            end_date=args.end_date,
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
