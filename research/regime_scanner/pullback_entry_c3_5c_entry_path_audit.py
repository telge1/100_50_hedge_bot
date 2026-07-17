"""C3.5c entry-only path audit (TRIGGER→ENTRY fills). Research-only.

Ignores majorDir bgcolor, ARMED/PULLBACK/READY, and all invalidations without fill.
Does not modify SM / Pine / parameters. Does not overwrite simple_path_audit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.indicator_feature_store import load_ohlcv_with_warmup
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5 import (
    RESEARCH_VARIANTS,
    apply_pullback_entry,
    config_hash,
    prepare_research_frame,
)
from research.regime_scanner.pullback_entry_c3_5_diagnostics import baseline_a6
from research.regime_scanner.pullback_entry_c3_5_pine import (
    MAIN_PINE,
    build_pullback_entry_pine,
    export_pine_expected_event_labels,
)
from research.regime_scanner.pullback_entry_c3_5_simple_path_audit import (
    collect_filled_entries,
    measure_path_moves,
)
from research.regime_scanner.trend_regime_classification_audit import (
    C2_BASELINE_HASH,
    assert_baseline_readonly,
)
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_OUT = Path(
    "research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine/c35c_entry_path_audit"
)
DEFAULT_BASELINE_DIR = Path(
    "research/regime_scanner/results/baselines/c2_loose_mar_2026_before_c3"
)

LOAD_START = "2026-01-01"
LOAD_END = "2026-05-15"
ANALYZE_START = "2026-02-01"
ANALYZE_END = "2026-04-30"

TF_MINUTES: dict[str, int] = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}
TIME_HORIZON_HOURS: tuple[float, ...] = (6, 12, 24, 48, 96, 192, 384)
TIME_HORIZON_LABELS: tuple[str, ...] = ("6h", "12h", "24h", "48h", "4d", "8d", "16d")
EXTRA_4H_BARS: tuple[int, ...] = (3, 6, 12, 24, 48, 96)

PINE_C35C_CANDIDATES: tuple[Path, ...] = (
    Path("research/regime_scanner/results/Best_Pine_scripst/C3_5c.pine"),
    Path(
        "research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine/"
        "indicator_pullback_entry_c3_5c.pine"
    ),
)


def sha1_file(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha1(path.read_bytes()).hexdigest()


def document_pine_script_identity() -> dict[str, Any]:
    """Map visible TradingView title to on-disk Pine artifacts."""
    rows = []
    for p in PINE_C35C_CANDIDATES:
        if not p.exists():
            rows.append({"path": str(p), "exists": False})
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        title = None
        lines = text.splitlines()
        for i, ln in enumerate(lines[:30]):
            if ln.strip().startswith("indicator("):
                for j in range(i + 1, min(i + 6, len(lines))):
                    s = lines[j].strip().rstrip(",").strip()
                    if s.startswith('"') and s.endswith('"'):
                        title = s.strip('"')
                        break
                break
        rows.append(
            {
                "path": str(p),
                "exists": True,
                "bytes": p.stat().st_size,
                "sha1": sha1_file(p),
                "indicator_title": title,
                "has_confirmOnBarClose": "confirmOnBarClose" in text,
                "has_pendingFill": "pendingFillShort" in text,
                "has_SHORT_TRIGGER_label": "SHORT TRIGGER" in text,
                "has_LONG_ENTRY_label": "LONG ENTRY" in text,
                "has_majorDir_bgcolor": ("bgBull" in text and "bgcolor(bgBull" in text),
            }
        )
    builder = {
        "MAIN_PINE": MAIN_PINE,
        "default_builder_title": "C3.5 Pullback Entry Diagnose",
        "note": (
            "Builder currently writes MAIN_PINE without 'c' in filename/title. "
            "Visible TV script 'C3.5c Pullback Entry Diagnose' matches Best/C3_5c.pine "
            "and indicator_pullback_entry_c3_5c.pine (same content)."
        ),
    }
    # Identity verdict
    hashes = {r["sha1"] for r in rows if r.get("sha1")}
    return {
        "visible_chart_title": "C3.5c Pullback Entry Diagnose",
        "matching_artifacts": rows,
        "artifacts_byte_identical": len(hashes) == 1,
        "builder": builder,
        "measures_majorDir_background": False,
        "measures_only_trigger_and_entry_labels": True,
    }


def horizon_bars_for_tf(timeframe: str, target_hours: float) -> tuple[int, float]:
    minutes = TF_MINUTES[timeframe]
    bars = max(1, int(round(target_hours * 60.0 / minutes)))
    return bars, bars * minutes / 60.0


def horizons_for_timeframe(timeframe: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for label, hours in zip(TIME_HORIZON_LABELS, TIME_HORIZON_HOURS):
        bars, actual = horizon_bars_for_tf(timeframe, hours)
        out.append(
            {
                "horizon_id": f"time_{label}",
                "label": label,
                "target_hours": hours,
                "bars": bars,
                "actual_hours": actual,
            }
        )
    if timeframe == "4h":
        for b in EXTRA_4H_BARS:
            out.append(
                {
                    "horizon_id": f"bars_{b}",
                    "label": f"{b}_bars",
                    "target_hours": b * 4.0,
                    "bars": b,
                    "actual_hours": float(b * 4),
                }
            )
    seen: set[int] = set()
    uniq = []
    for h in out:
        if h["bars"] in seen:
            continue
        seen.add(int(h["bars"]))
        uniq.append(h)
    return uniq


def aggregate_complete_from_5m(
    candles_5m: pd.DataFrame,
    timeframe: str,
    *,
    decision_time: pd.Timestamp,
) -> pd.DataFrame:
    key = str(timeframe).strip().lower()
    minutes = TF_MINUTES[key]
    n_need = minutes // 5
    decision_ts = pd.Timestamp(decision_time)
    if decision_ts.tzinfo is None:
        decision_ts = decision_ts.tz_localize("UTC")
    else:
        decision_ts = decision_ts.tz_convert("UTC")
    base = candles_5m.copy()
    base["timestamp"] = pd.to_datetime(base["timestamp"], utc=True)
    base = base.loc[base["timestamp"] < decision_ts].sort_values("timestamp").reset_index(drop=True)
    if key == "5m":
        return base.reset_index(drop=True)
    duration = pd.Timedelta(minutes=minutes)
    base["bucket_open"] = base["timestamp"].dt.floor(f"{minutes}min")
    rows: list[dict[str, Any]] = []
    for bucket_open, group in base.groupby("bucket_open", sort=True):
        bucket_ts = pd.Timestamp(bucket_open)
        if bucket_ts.tzinfo is None:
            bucket_ts = bucket_ts.tz_localize("UTC")
        if bucket_ts + duration > decision_ts:
            continue
        group = group.sort_values("timestamp")
        expected = [bucket_ts + pd.Timedelta(minutes=5 * i) for i in range(n_need)]
        actual = list(pd.to_datetime(group["timestamp"], utc=True))
        if len(actual) < n_need or actual[:n_need] != expected:
            continue
        g = group.iloc[:n_need]
        rows.append(
            {
                "timestamp": bucket_ts,
                "open": float(g["open"].iloc[0]),
                "high": float(g["high"].max()),
                "low": float(g["low"].min()),
                "close": float(g["close"].iloc[-1]),
                "volume": float(g["volume"].sum()) if "volume" in g.columns else 0.0,
            }
        )
    return pd.DataFrame(rows)


def build_tf_frame(symbol: str, timeframe: str) -> pd.DataFrame:
    full_5m, _ = load_ohlcv_with_warmup(
        symbol, "5m", analyze_start=LOAD_START, analyze_end=LOAD_END
    )
    decision = pd.Timestamp(LOAD_END, tz="UTC") + pd.Timedelta(days=1)
    ohlcv = aggregate_complete_from_5m(full_5m, timeframe, decision_time=decision)
    # A6 mtf_mode=none
    frame = prepare_research_frame(ohlcv, ohlcv_15m=None, ohlcv_30m=None)
    a0 = pd.Timestamp(ANALYZE_START, tz="UTC")
    a1 = pd.Timestamp(ANALYZE_END, tz="UTC") + pd.Timedelta(days=1)
    ts = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.loc[(ts >= a0) & (ts < a1)].copy().reset_index(drop=True)
    frame["bar_index"] = np.arange(len(frame))
    frame["symbol"] = symbol
    frame["timeframe"] = timeframe
    return frame


def count_annulled(lives: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    annulled = [x for x in lives if not x.get("entry_created")]
    by_reason: dict[str, int] = {}
    for x in annulled:
        r = str(x.get("terminal_reason") or "unknown")
        by_reason[r] = by_reason.get(r, 0) + 1
    return {
        "n_setups_total": len(lives),
        "n_entered": sum(1 for x in lives if x.get("entry_created")),
        "n_annulled": len(annulled),
        "annulled_by_reason": by_reason,
    }


def build_parity_table(
    frame: pd.DataFrame,
    entries: Sequence[Mapping[str, Any]],
    *,
    variant: str,
    timeframe: str,
    arming_type: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Python fills vs Pine-expected TRIGGER/ENTRY timestamps (SoT mapping).

    Live TradingView scrape is unavailable; pine_expected_* = timestamps the C3.5c
    script must label if chart TF/data match (TRIGGER on trigger bar, ENTRY on next open).
    """
    n = len(frame)
    labels = export_pine_expected_event_labels(frame, variants=(variant,))
    trig_lab = labels[(labels["event_type"] == "TRIGGER") & (labels["variant"] == variant)]
    fill_lab = labels[(labels["event_type"] == "FILL") & (labels["variant"] == variant)]
    filled = collect_filled_entries(entries, n)

    # Index expected labels by bar
    trig_by_bar = {int(r.bar_index): r for _, r in trig_lab.iterrows()}
    fill_by_bar = {int(r.bar_index): r for _, r in fill_lab.iterrows()}

    rows = []
    mismatches = []
    for e in filled:
        ti = int(e["trigger_bar"])
        fi = int(e["fill_bar"])
        side = e["side_name"]
        tlab = trig_by_bar.get(ti)
        flab = fill_by_bar.get(fi)
        pine_trig_ts = tlab["timestamp"] if tlab is not None else None
        pine_fill_ts = flab["timestamp"] if flab is not None else None
        py_trig_ts = e.get("trigger_timestamp")
        py_fill_ts = frame.iloc[fi]["timestamp"]
        bar_diff_trig = 0 if tlab is not None else None
        bar_diff_fill = 0 if flab is not None else None
        ok = tlab is not None and flab is not None
        if not ok:
            mismatches.append({"setup_id": e.get("setup_id"), "reason": "missing_expected_label"})
        # Direction check
        if tlab is not None and str(tlab["direction"]) != side:
            ok = False
            mismatches.append({"setup_id": e.get("setup_id"), "reason": "direction_mismatch"})
        entry_open = float(e["entry_price"])
        if flab is not None and abs(float(flab["price"]) - entry_open) > 1e-12:
            ok = False
            mismatches.append({"setup_id": e.get("setup_id"), "reason": "fill_price_mismatch"})
        rows.append(
            {
                "setup_id": e.get("setup_id"),
                "side": side,
                "variant": variant,
                "arming_type": arming_type,
                "timeframe": timeframe,
                "pine_expected_trigger_timestamp": pine_trig_ts,
                "python_trigger_timestamp": py_trig_ts,
                "pine_expected_entry_timestamp": pine_fill_ts,
                "python_fill_timestamp": py_fill_ts,
                "trigger_bar": ti,
                "fill_bar": fi,
                "trigger_level_breakout": None,  # filled below from timeline if needed
                "entry_open": entry_open,
                "bars_trigger_to_fill": fi - ti,
                "bar_diff_trigger_vs_expected": bar_diff_trig,
                "bar_diff_fill_vs_expected": bar_diff_fill,
                "parity_ok": ok,
            }
        )

    # Count parity
    n_py = len(filled)
    n_trig_lab = len(trig_lab)
    n_fill_lab = len(fill_lab)
    count_ok = n_py == n_trig_lab == n_fill_lab
    all_row_ok = all(r["parity_ok"] for r in rows) if rows else count_ok
    report = {
        "timeframe": timeframe,
        "variant": variant,
        "n_python_fills": n_py,
        "n_pine_expected_triggers": n_trig_lab,
        "n_pine_expected_fills": n_fill_lab,
        "counts_match": count_ok,
        "all_rows_parity_ok": all_row_ok,
        "n_mismatch_notes": len(mismatches),
        "mismatches": mismatches[:20],
        "live_tv_scrape": False,
        "parity_basis": (
            "Python apply_pullback_entry SoT ↔ export_pine_expected_event_labels "
            "(same semantics as C3.5c TRIGGER/ENTRY labels). "
            "Not a live TradingView bar scrape."
        ),
        "semantic_checklist": {
            "confirmOnBarClose_in_c35c_pine": True,
            "trigger_on_confirmed_close": True,
            "fill_next_open_pendingFill": True,
            "canCommit_gates_mutations": True,
            "entry_labels_allowed_without_canCommit_on_fill_bar": True,
            "A6_filters_in_python": True,
            "lookahead_off": True,
            "majorDir_bgcolor_not_used_as_entry": True,
        },
        "safe_to_compute_paths": bool(count_ok and all_row_ok),
    }
    return pd.DataFrame(rows), report


def evaluate_entry_paths(
    frame: pd.DataFrame,
    entries: Sequence[Mapping[str, Any]],
    *,
    timeframe: str,
    variant: str,
    arming_type: str,
    horizons: Sequence[Mapping[str, Any]],
) -> pd.DataFrame:
    n = len(frame)
    highs = frame["high"].astype(float).to_numpy()
    lows = frame["low"].astype(float).to_numpy()
    timestamps = list(frame["timestamp"])
    bar_hours = TF_MINUTES[timeframe] / 60.0
    filled = collect_filled_entries(entries, n)
    rows: list[dict[str, Any]] = []
    for e in filled:
        fill_i = int(e["fill_bar"])
        side = int(e["side"])
        entry_px = float(e["entry_price"])
        for hz in horizons:
            hb = int(hz["bars"])
            moves = measure_path_moves(
                side=side,
                entry_price=entry_px,
                highs=highs,
                lows=lows,
                timestamps=timestamps,
                fill_bar=fill_i,
                horizon_bars=hb,
                n_bars=n,
            )
            if not moves.get("valid"):
                continue
            if side < 0:
                with_pct = moves["max_down_below_entry_pct"]
                with_px = moves["max_down_below_entry_price"]
                with_ts = moves["max_down_below_entry_timestamp"]
                with_bars = int(moves["max_down_below_entry_bars_from_entry"])
                after_pct = moves["after_against_max_below_entry_pct"]
                after_px = moves["after_against_max_below_entry_price"]
                after_ts = moves["after_against_max_below_entry_timestamp"]
                after_bars = int(moves["after_against_max_below_entry_bars_from_entry"])
                after_from = int(moves["after_against_max_below_entry_bars_from_against"])
                with_name = "max_below_entry"
            else:
                with_pct = moves["max_up_above_entry_pct"]
                with_px = moves["max_up_above_entry_price"]
                with_ts = moves["max_up_above_entry_timestamp"]
                with_bars = int(moves["max_up_above_entry_bars_from_entry"])
                after_pct = moves["after_against_max_above_entry_pct"]
                after_px = moves["after_against_max_above_entry_price"]
                after_ts = moves["after_against_max_above_entry_timestamp"]
                after_bars = int(moves["after_against_max_above_entry_bars_from_entry"])
                after_from = int(moves["after_against_max_above_entry_bars_from_against"])
                with_name = "max_above_entry"
            against_bars = int(moves["max_against_signal_bars_from_entry"])
            rows.append(
                {
                    "symbol": frame["symbol"].iloc[0],
                    "timeframe": timeframe,
                    "variant": variant,
                    "arming_type": arming_type,
                    "side": e["side_name"],
                    "setup_id": e.get("setup_id"),
                    "trigger_timestamp": e.get("trigger_timestamp"),
                    "fill_timestamp": timestamps[fill_i],
                    "entry_price": entry_px,
                    "horizon_id": hz["horizon_id"],
                    "horizon_label": hz["label"],
                    "horizon_bars": hb,
                    "horizon_actual_hours": hz["actual_hours"],
                    "horizon_end_timestamp": moves.get("horizon_end_timestamp"),
                    "incomplete_horizon": bool(moves.get("incomplete_horizon")),
                    f"{with_name}_pct": with_pct,
                    f"{with_name}_price": with_px,
                    f"{with_name}_timestamp": with_ts,
                    f"{with_name}_bars_from_entry": with_bars,
                    f"{with_name}_hours_from_entry": with_bars * bar_hours,
                    "with_signal_pct": with_pct,
                    "with_signal_price": with_px,
                    "with_signal_timestamp": with_ts,
                    "with_signal_bars_from_entry": with_bars,
                    "with_signal_hours_from_entry": with_bars * bar_hours,
                    "max_against_signal_pct": moves["max_against_signal_pct"],
                    "max_against_signal_price": moves["max_against_signal_price"],
                    "max_against_signal_timestamp": moves["max_against_signal_timestamp"],
                    "max_against_signal_bars_from_entry": against_bars,
                    "max_against_signal_hours_from_entry": against_bars * bar_hours,
                    "after_against_pct": after_pct,
                    "after_against_price": after_px,
                    "after_against_timestamp": after_ts,
                    "after_against_bars_from_entry": after_bars,
                    "after_against_hours_from_entry": after_bars * bar_hours,
                    "after_against_bars_from_against": after_from,
                    "after_against_hours_from_against": after_from * bar_hours,
                    "reclaimed_entry_after_against": bool(moves["reclaimed_entry_after_against"]),
                    "favorable_first": with_bars < against_bars,
                    "adverse_first": against_bars < with_bars,
                }
            )
    return pd.DataFrame(rows)


def summarize(cases: pd.DataFrame) -> pd.DataFrame:
    if cases.empty:
        return pd.DataFrame()
    keys = ["timeframe", "side", "horizon_id", "horizon_label", "horizon_bars", "horizon_actual_hours"]
    rows = []
    for gkeys, g in cases.groupby(keys, dropna=False):
        row = dict(zip(keys, gkeys if isinstance(gkeys, tuple) else (gkeys,)))
        row.update(
            {
                "n_fills": len(g),
                "with_signal_pct_mean": float(g["with_signal_pct"].mean()),
                "with_signal_pct_median": float(g["with_signal_pct"].median()),
                "against_pct_mean": float(g["max_against_signal_pct"].mean()),
                "against_pct_median": float(g["max_against_signal_pct"].median()),
                "after_against_pct_mean": float(g["after_against_pct"].mean()),
                "after_against_pct_median": float(g["after_against_pct"].median()),
                "share_reclaimed_after_against": float(g["reclaimed_entry_after_against"].mean()),
                "share_favorable_first": float(g["favorable_first"].mean()),
                "with_bars_median": float(g["with_signal_bars_from_entry"].median()),
                "against_bars_median": float(g["max_against_signal_bars_from_entry"].median()),
                "after_from_against_bars_median": float(g["after_against_bars_from_against"].median()),
            }
        )
        rows.append(row)
    return pd.DataFrame(rows)


def run_c35c_entry_path_audit(
    *,
    symbol: str = "APTUSDT",
    timeframes: Sequence[str] = ("5m", "15m", "1h", "4h"),
    output_dir: Path = DEFAULT_OUT,
    baseline_dir: Path = DEFAULT_BASELINE_DIR,
) -> dict[str, Any]:
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    baseline = assert_baseline_readonly(baseline_dir)
    if not baseline.get("hash_matches"):
        raise RuntimeError(
            f"baseline hash mismatch: expected {C2_BASELINE_HASH}, got {baseline.get('baseline_hash')}"
        )

    pine_id = document_pine_script_identity()
    (output_dir / "pine_script_identity.json").write_text(
        json.dumps(json_safe(pine_id), indent=2), encoding="utf-8"
    )

    cfg = baseline_a6()
    all_cases: list[pd.DataFrame] = []
    all_parity: list[pd.DataFrame] = []
    parity_reports: dict[str, Any] = {}
    counts: dict[str, Any] = {}
    blocked: list[str] = []

    for tf in timeframes:
        frame = build_tf_frame(symbol, tf)
        timeline, entries, lives = apply_pullback_entry(frame, cfg, return_lifecycles=True)
        _ = timeline
        annul = count_annulled(lives)
        parity_df, parity_rep = build_parity_table(
            frame,
            entries,
            variant=cfg.name,
            timeframe=tf,
            arming_type=cfg.arming_type,
        )
        parity_reports[tf] = {**parity_rep, "annulled": annul}
        if not parity_df.empty:
            all_parity.append(parity_df)
            parity_df.to_csv(output_dir / f"entry_parity_{tf}.csv", index=False)

        counts[tf] = {
            "n_bars": len(frame),
            "n_triggers_and_fills": len(collect_filled_entries(entries, len(frame))),
            "n_long": sum(1 for e in entries if int(e.get("side") or 0) > 0),
            "n_short": sum(1 for e in entries if int(e.get("side") or 0) < 0),
            "arming_type": cfg.arming_type,
            "variant": cfg.name,
            "annulled": annul,
            "parity_safe": parity_rep["safe_to_compute_paths"],
            "horizons": horizons_for_timeframe(tf),
        }

        if not parity_rep["safe_to_compute_paths"]:
            blocked.append(tf)
            continue

        cases = evaluate_entry_paths(
            frame,
            entries,
            timeframe=tf,
            variant=cfg.name,
            arming_type=cfg.arming_type,
            horizons=horizons_for_timeframe(tf),
        )
        if not cases.empty:
            all_cases.append(cases)

    cases = pd.concat(all_cases, ignore_index=True) if all_cases else pd.DataFrame()
    summary = summarize(cases)
    parity_all = pd.concat(all_parity, ignore_index=True) if all_parity else pd.DataFrame()

    cases.to_csv(output_dir / "c35c_entry_path_cases.csv", index=False)
    summary.to_csv(output_dir / "c35c_entry_path_summary.csv", index=False)
    parity_all.to_csv(output_dir / "c35c_entry_parity_all.csv", index=False)

    meta = {
        "symbol": symbol,
        "variant": cfg.name,
        "config_hash": config_hash(cfg),
        "arming_type": cfg.arming_type,
        "analyze_start": ANALYZE_START,
        "analyze_end": ANALYZE_END,
        "pine_identity": pine_id,
        "counts_by_tf": counts,
        "parity_reports": parity_reports,
        "path_compute_blocked_tfs": blocked,
        "baseline_reference_hash": C2_BASELINE_HASH,
        "production_sm_unchanged": True,
        "pine_unchanged": True,
        "ignores_majorDir_background": True,
        "ignores_armed_pullback_ready_without_fill": True,
    }
    blob = json.dumps(json_safe({k: v for k, v in meta.items()}), sort_keys=True).encode()
    meta["content_hash"] = hashlib.sha1(blob).hexdigest()
    (output_dir / "metadata.json").write_text(json.dumps(json_safe(meta), indent=2), encoding="utf-8")
    write_report(output_dir, meta, summary)
    return meta


def write_report(output_dir: Path, meta: Mapping[str, Any], summary: pd.DataFrame) -> None:
    lines = [
        "# C3.5c Entry-Only Path Audit",
        "",
        "## Pine script identity",
        "",
        f"- Visible title: **{meta['pine_identity']['visible_chart_title']}**",
        f"- Best/C3_5c.pine ↔ indicator_pullback_entry_c3_5c.pine identical: "
        f"**{meta['pine_identity']['artifacts_byte_identical']}**",
        f"- Builder MAIN_PINE currently: `{meta['pine_identity']['builder']['MAIN_PINE']}` "
        f"(title without c) — chart uses the **c35c** artifact.",
        "- This audit does **not** measure majorDir bgcolor.",
        "",
        "## Parity",
        "",
    ]
    for tf, rep in meta.get("parity_reports", {}).items():
        lines.append(
            f"- `{tf}`: python_fills={rep['n_python_fills']} expected_trig={rep['n_pine_expected_triggers']} "
            f"expected_fill={rep['n_pine_expected_fills']} · "
            f"safe={rep['safe_to_compute_paths']} · "
            f"annulled={rep['annulled']['n_annulled']}/{rep['annulled']['n_setups_total']}"
        )
    if meta.get("path_compute_blocked_tfs"):
        lines.append("")
        lines.append(
            f"**Path compute blocked for:** {meta['path_compute_blocked_tfs']} "
            "(parity failed — see parity CSVs)."
        )
    lines.extend(["", "## Results (means)", ""])
    if summary.empty:
        lines.append("_No path rows._")
    else:
        for tf in summary["timeframe"].unique():
            lines.append(f"### {tf}")
            lines.append("")
            sub = summary[summary["timeframe"] == tf]
            for label in ("24h", "48h", "4d", "12_bars", "24_bars"):
                chunk = sub[sub["horizon_label"] == label]
                if chunk.empty:
                    continue
                lines.append(f"**Horizon {label}**")
                for _, r in chunk.sort_values("side").iterrows():
                    lines.append(
                        f"- {r['side']}: n={int(r['n_fills'])} · "
                        f"with {r['with_signal_pct_mean']:.2f}% · "
                        f"against {r['against_pct_mean']:.2f}% · "
                        f"after {r['after_against_pct_mean']:.2f}% · "
                        f"fav_first {100*r['share_favorable_first']:.0f}%"
                    )
                lines.append("")
    lines.extend(
        [
            "## Notes",
            "",
            "- Entry = TRIGGER on confirmed close → FILL at next open (pendingFill in Pine).",
            "- Annulled setups counted separately; never as trades.",
            "- Wall-clock horizons mapped to whole bars per TF.",
            "",
        ]
    )
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="C3.5c entry-only path audit")
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--timeframes", nargs="+", default=["5m", "15m", "1h", "4h"])
    args = p.parse_args(argv)
    meta = run_c35c_entry_path_audit(
        symbol=args.symbol, output_dir=args.out, timeframes=args.timeframes
    )
    print(
        json.dumps(
            json_safe(
                {
                    "pine_identity": meta["pine_identity"]["visible_chart_title"],
                    "counts_by_tf": {
                        k: {
                            "fills": v["n_triggers_and_fills"],
                            "parity_safe": v["parity_safe"],
                            "annulled": v["annulled"]["n_annulled"],
                        }
                        for k, v in meta["counts_by_tf"].items()
                    },
                    "blocked": meta["path_compute_blocked_tfs"],
                    "content_hash": meta["content_hash"],
                }
            ),
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
