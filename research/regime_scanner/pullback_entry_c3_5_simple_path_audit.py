"""C3.5 simple path audit: three post-fill moves only (research-only).

Counts only filled LONG/SHORT triggers (next-open fill). No TP/SL, no
profitability scoring, no SM / Pine changes.
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
    apply_pullback_entry,
    config_hash,
    prepare_research_frame,
)
from research.regime_scanner.pullback_entry_c3_5_diagnostics import baseline_a6
from research.regime_scanner.trend_regime_classification_audit import (
    C2_BASELINE_HASH,
    assert_baseline_readonly,
)
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_OUT = Path(
    "research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine/simple_path_audit"
)
DEFAULT_BASELINE_DIR = Path(
    "research/regime_scanner/results/baselines/c2_loose_mar_2026_before_c3"
)
CACHED_APT_FRAME = Path(
    "research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine/research_frame_5m.csv"
)

LOAD_START = "2026-01-01"
LOAD_END = "2026-05-15"
ANALYZE_START = "2026-02-01"
ANALYZE_END = "2026-04-30"
TIMEFRAME = "5m"
HORIZONS: tuple[int, ...] = (6, 12, 24, 48)


def build_research_frame(
    symbol: str,
    *,
    load_start: str = LOAD_START,
    load_end: str = LOAD_END,
    analyze_start: str = ANALYZE_START,
    analyze_end: str = ANALYZE_END,
    include_mtf: bool = True,
    cached_csv: Path | None = None,
) -> pd.DataFrame:
    if cached_csv is not None and cached_csv.exists() and symbol == "APTUSDT":
        frame = pd.read_csv(cached_csv, parse_dates=["timestamp"])
    else:
        full_5m, _ = load_ohlcv_with_warmup(
            symbol, "5m", analyze_start=load_start, analyze_end=load_end
        )
        ohlcv_15m = ohlcv_30m = None
        if include_mtf:
            full_15m, _ = load_ohlcv_with_warmup(
                symbol, "15m", analyze_start=load_start, analyze_end=load_end
            )
            full_30m, _ = load_ohlcv_with_warmup(
                symbol, "30m", analyze_start=load_start, analyze_end=load_end
            )
            ohlcv_15m, ohlcv_30m = full_15m, full_30m
        frame = prepare_research_frame(full_5m, ohlcv_15m=ohlcv_15m, ohlcv_30m=ohlcv_30m)

    a0 = pd.Timestamp(analyze_start, tz="UTC")
    a1 = pd.Timestamp(analyze_end, tz="UTC") + pd.Timedelta(days=1)
    ts = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.loc[(ts >= a0) & (ts < a1)].copy().reset_index(drop=True)
    frame["bar_index"] = np.arange(len(frame))
    frame["symbol"] = symbol
    frame["timeframe"] = TIMEFRAME
    return frame


def _ts(frame: pd.DataFrame, i: int | None) -> Any:
    if i is None or i < 0 or i >= len(frame):
        return None
    return frame.iloc[int(i)]["timestamp"]


def _first_argmax(arr: np.ndarray) -> int:
    """Index of first maximum (stable on ties)."""
    return int(np.argmax(arr))


def _first_argmin(arr: np.ndarray) -> int:
    """Index of first minimum (stable on ties)."""
    return int(np.argmin(arr))


def measure_path_moves(
    *,
    side: int,
    entry_price: float,
    highs: np.ndarray,
    lows: np.ndarray,
    timestamps: Sequence[Any],
    fill_bar: int,
    horizon_bars: int,
    n_bars: int,
) -> dict[str, Any]:
    """Three moves from fill bar over ``horizon_bars`` inclusive candles.

    Path uses bars ``[fill_bar, fill_bar + horizon_bars - 1]`` only (no lookahead).
    Point 3 searches only from the first max-adverse bar index onward.
    """
    if entry_price <= 0 or not math.isfinite(entry_price):
        raise ValueError("entry_price must be finite and > 0")
    if side not in (-1, 1):
        raise ValueError("side must be -1 (short) or +1 (long)")

    last_needed = fill_bar + horizon_bars - 1
    incomplete = last_needed >= n_bars
    end_i = min(last_needed, n_bars - 1)
    if fill_bar >= n_bars or end_i < fill_bar:
        return {
            "incomplete_horizon": True,
            "horizon_end_timestamp": None,
            "valid": False,
        }

    # Relative slice from fill
    h = highs[fill_bar : end_i + 1]
    l = lows[fill_bar : end_i + 1]
    # local indices 0 .. len-1 map to bars fill_bar + local

    if side < 0:
        # SHORT: with-signal = down; against = up
        with_loc = _first_argmin(l)
        against_loc = _first_argmax(h)
        with_px = float(l[with_loc])
        against_px = float(h[against_loc])
        with_pct = (entry_price - with_px) / entry_price * 100.0
        against_pct = (against_px - entry_price) / entry_price * 100.0

        # Point 3: from against_loc onward (inclusive)
        later_l = l[against_loc:]
        later_rel = _first_argmin(later_l)
        later_loc = against_loc + later_rel
        later_px = float(l[later_loc])
        after_pct = (entry_price - later_px) / entry_price * 100.0

        with_key = "max_down_below_entry"
        after_key = "after_against_max_below_entry"
    else:
        # LONG: with-signal = up; against = down
        with_loc = _first_argmax(h)
        against_loc = _first_argmin(l)
        with_px = float(h[with_loc])
        against_px = float(l[against_loc])
        with_pct = (with_px - entry_price) / entry_price * 100.0
        against_pct = (entry_price - against_px) / entry_price * 100.0

        later_h = h[against_loc:]
        later_rel = _first_argmax(later_h)
        later_loc = against_loc + later_rel
        later_px = float(h[later_loc])
        after_pct = (later_px - entry_price) / entry_price * 100.0

        with_key = "max_up_above_entry"
        after_key = "after_against_max_above_entry"

    with_bar = fill_bar + with_loc
    against_bar = fill_bar + against_loc
    later_bar = fill_bar + later_loc

    return {
        "valid": True,
        "incomplete_horizon": incomplete,
        "horizon_end_timestamp": _ts_from_list(timestamps, end_i),
        f"{with_key}_pct": float(with_pct),
        f"{with_key}_price": with_px,
        f"{with_key}_timestamp": _ts_from_list(timestamps, with_bar),
        f"{with_key}_bars_from_entry": int(with_loc),
        "max_against_signal_pct": float(against_pct),
        "max_against_signal_price": against_px,
        "max_against_signal_timestamp": _ts_from_list(timestamps, against_bar),
        "max_against_signal_bars_from_entry": int(against_loc),
        f"{after_key}_pct": float(after_pct),
        f"{after_key}_price": later_px,
        f"{after_key}_timestamp": _ts_from_list(timestamps, later_bar),
        f"{after_key}_bars_from_entry": int(later_loc),
        f"{after_key}_bars_from_against": int(later_loc - against_loc),
        "reclaimed_entry_after_against": bool(after_pct > 0),
    }


def _ts_from_list(timestamps: Sequence[Any], i: int) -> Any:
    if i < 0 or i >= len(timestamps):
        return None
    return timestamps[i]


def collect_filled_entries(entries: Sequence[Mapping[str, Any]], n_bars: int) -> list[dict[str, Any]]:
    """Keep only entries with a next-bar fill (SoT already skips missing next_open)."""
    out: list[dict[str, Any]] = []
    for e in entries:
        side = int(e.get("side") or e.get("entry_side") or 0)
        if side not in (-1, 1):
            continue
        trigger_i = int(e["bar_index"])
        fill_i = trigger_i + 1
        if fill_i >= n_bars:
            continue
        entry_px = e.get("entry_price")
        if entry_px is None or not math.isfinite(float(entry_px)):
            continue
        out.append(
            {
                "setup_id": e.get("setup_id"),
                "side": side,
                "side_name": "short" if side < 0 else "long",
                "trigger_bar": trigger_i,
                "fill_bar": fill_i,
                "trigger_timestamp": e.get("timestamp"),
                "entry_price": float(entry_px),
            }
        )
    return out


def build_cases(
    frame: pd.DataFrame,
    entries: Sequence[Mapping[str, Any]],
    *,
    symbol: str,
    variant: str,
    timeframe: str = TIMEFRAME,
    horizons: Sequence[int] = HORIZONS,
) -> pd.DataFrame:
    n = len(frame)
    highs = frame["high"].astype(float).to_numpy()
    lows = frame["low"].astype(float).to_numpy()
    timestamps = list(frame["timestamp"])
    filled = collect_filled_entries(entries, n)
    # Attach fill timestamps from frame
    rows: list[dict[str, Any]] = []
    for e in filled:
        fill_i = int(e["fill_bar"])
        fill_ts = timestamps[fill_i]
        for h in horizons:
            moves = measure_path_moves(
                side=int(e["side"]),
                entry_price=float(e["entry_price"]),
                highs=highs,
                lows=lows,
                timestamps=timestamps,
                fill_bar=fill_i,
                horizon_bars=int(h),
                n_bars=n,
            )
            if not moves.get("valid"):
                continue
            row = {
                "symbol": symbol,
                "timeframe": timeframe,
                "variant": variant,
                "side": e["side_name"],
                "setup_id": e.get("setup_id"),
                "trigger_timestamp": e.get("trigger_timestamp"),
                "fill_timestamp": fill_ts,
                "entry_price": e["entry_price"],
                "horizon_bars": int(h),
                "horizon_end_timestamp": moves.get("horizon_end_timestamp"),
                "incomplete_horizon": bool(moves.get("incomplete_horizon")),
                "trigger_bar": e["trigger_bar"],
                "fill_bar": fill_i,
            }
            # Normalize movement columns for both sides into shared summary names
            if e["side"] < 0:
                row.update(
                    {
                        "with_signal_pct": moves["max_down_below_entry_pct"],
                        "with_signal_price": moves["max_down_below_entry_price"],
                        "with_signal_timestamp": moves["max_down_below_entry_timestamp"],
                        "with_signal_bars_from_entry": moves["max_down_below_entry_bars_from_entry"],
                        "max_down_below_entry_pct": moves["max_down_below_entry_pct"],
                        "max_down_below_entry_price": moves["max_down_below_entry_price"],
                        "max_down_below_entry_timestamp": moves["max_down_below_entry_timestamp"],
                        "max_down_below_entry_bars_from_entry": moves[
                            "max_down_below_entry_bars_from_entry"
                        ],
                        "max_against_signal_pct": moves["max_against_signal_pct"],
                        "max_against_signal_price": moves["max_against_signal_price"],
                        "max_against_signal_timestamp": moves["max_against_signal_timestamp"],
                        "max_against_signal_bars_from_entry": moves[
                            "max_against_signal_bars_from_entry"
                        ],
                        "after_against_pct": moves["after_against_max_below_entry_pct"],
                        "after_against_price": moves["after_against_max_below_entry_price"],
                        "after_against_timestamp": moves["after_against_max_below_entry_timestamp"],
                        "after_against_bars_from_entry": moves[
                            "after_against_max_below_entry_bars_from_entry"
                        ],
                        "after_against_bars_from_against": moves[
                            "after_against_max_below_entry_bars_from_against"
                        ],
                        "after_against_max_below_entry_pct": moves[
                            "after_against_max_below_entry_pct"
                        ],
                        "after_against_max_below_entry_price": moves[
                            "after_against_max_below_entry_price"
                        ],
                        "after_against_max_below_entry_timestamp": moves[
                            "after_against_max_below_entry_timestamp"
                        ],
                        "after_against_max_below_entry_bars_from_entry": moves[
                            "after_against_max_below_entry_bars_from_entry"
                        ],
                        "after_against_max_below_entry_bars_from_against": moves[
                            "after_against_max_below_entry_bars_from_against"
                        ],
                        "reclaimed_entry_after_against": moves["reclaimed_entry_after_against"],
                    }
                )
            else:
                row.update(
                    {
                        "with_signal_pct": moves["max_up_above_entry_pct"],
                        "with_signal_price": moves["max_up_above_entry_price"],
                        "with_signal_timestamp": moves["max_up_above_entry_timestamp"],
                        "with_signal_bars_from_entry": moves["max_up_above_entry_bars_from_entry"],
                        "max_up_above_entry_pct": moves["max_up_above_entry_pct"],
                        "max_up_above_entry_price": moves["max_up_above_entry_price"],
                        "max_up_above_entry_timestamp": moves["max_up_above_entry_timestamp"],
                        "max_up_above_entry_bars_from_entry": moves[
                            "max_up_above_entry_bars_from_entry"
                        ],
                        "max_against_signal_pct": moves["max_against_signal_pct"],
                        "max_against_signal_price": moves["max_against_signal_price"],
                        "max_against_signal_timestamp": moves["max_against_signal_timestamp"],
                        "max_against_signal_bars_from_entry": moves[
                            "max_against_signal_bars_from_entry"
                        ],
                        "after_against_pct": moves["after_against_max_above_entry_pct"],
                        "after_against_price": moves["after_against_max_above_entry_price"],
                        "after_against_timestamp": moves["after_against_max_above_entry_timestamp"],
                        "after_against_bars_from_entry": moves[
                            "after_against_max_above_entry_bars_from_entry"
                        ],
                        "after_against_bars_from_against": moves[
                            "after_against_max_above_entry_bars_from_against"
                        ],
                        "after_against_max_above_entry_pct": moves[
                            "after_against_max_above_entry_pct"
                        ],
                        "after_against_max_above_entry_price": moves[
                            "after_against_max_above_entry_price"
                        ],
                        "after_against_max_above_entry_timestamp": moves[
                            "after_against_max_above_entry_timestamp"
                        ],
                        "after_against_max_above_entry_bars_from_entry": moves[
                            "after_against_max_above_entry_bars_from_entry"
                        ],
                        "after_against_max_above_entry_bars_from_against": moves[
                            "after_against_max_above_entry_bars_from_against"
                        ],
                        "reclaimed_entry_after_against": moves["reclaimed_entry_after_against"],
                    }
                )
            rows.append(row)
    return pd.DataFrame(rows)


def _agg_block(g: pd.DataFrame) -> dict[str, Any]:
    n = len(g)
    if n == 0:
        return {"n_signals": 0}

    def _med(col: str) -> float | None:
        s = g[col].dropna()
        return float(s.median()) if len(s) else None

    def _mean(col: str) -> float | None:
        s = g[col].dropna()
        return float(s.mean()) if len(s) else None

    return {
        "n_signals": n,
        "n_incomplete_horizon": int(g["incomplete_horizon"].sum()),
        "with_signal_pct_median": _med("with_signal_pct"),
        "with_signal_pct_mean": _mean("with_signal_pct"),
        "max_against_signal_pct_median": _med("max_against_signal_pct"),
        "max_against_signal_pct_mean": _mean("max_against_signal_pct"),
        "after_against_pct_median": _med("after_against_pct"),
        "after_against_pct_mean": _mean("after_against_pct"),
        "share_reclaimed_entry_after_against": float(g["reclaimed_entry_after_against"].mean()),
        "with_signal_bars_median": _med("with_signal_bars_from_entry"),
        "against_bars_median": _med("max_against_signal_bars_from_entry"),
        "after_against_bars_from_against_median": _med("after_against_bars_from_against"),
    }


def build_summaries(cases: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if cases.empty:
        empty = pd.DataFrame()
        return empty, empty
    by_side_rows = []
    for (side, horizon), g in cases.groupby(["side", "horizon_bars"], sort=True):
        by_side_rows.append({"side": side, "horizon_bars": int(horizon), **_agg_block(g)})
    by_side = pd.DataFrame(by_side_rows)

    summary_rows = []
    for horizon, g in cases.groupby("horizon_bars", sort=True):
        summary_rows.append({"side": "both", "horizon_bars": int(horizon), **_agg_block(g)})
    for row in by_side_rows:
        summary_rows.append(dict(row))
    summary = pd.DataFrame(summary_rows)
    return summary, by_side


def write_report(
    *,
    by_side: pd.DataFrame,
    metadata: Mapping[str, Any],
    path: Path,
) -> None:
    lines = [
        "# C3.5 Simple Path Audit",
        "",
        f"- Symbol: `{metadata.get('symbol')}`",
        f"- Variant: `{metadata.get('variant')}`",
        f"- Timeframe: `{metadata.get('timeframe')}`",
        f"- Window: {metadata.get('analyze_start')} → {metadata.get('analyze_end')} (inclusive end day)",
        f"- Fill: trigger on confirmed close → entry at next open",
        f"- Horizons: {', '.join(str(h) for h in metadata.get('horizons', []))} bars",
        f"- Filled signals: **{metadata.get('n_filled_signals')}** "
        f"(short={metadata.get('n_short')}, long={metadata.get('n_long')})",
        "",
        "Only filled triggers counted. Invalidated / never-triggered setups ignored.",
        "",
    ]
    for side in ("short", "long"):
        lines.append(f"## {side.upper()}")
        lines.append("")
        sub = by_side[by_side["side"] == side] if not by_side.empty else by_side
        if sub.empty:
            lines.append("_No signals._")
            lines.append("")
            continue
        for _, r in sub.sort_values("horizon_bars").iterrows():
            h = int(r["horizon_bars"])
            lines.append(f"### Horizon {h} bars")
            lines.append("")
            if side == "short":
                lines.append(
                    f"- durchschnittlich maximal **{r['with_signal_pct_mean']:.3f}%** unter Entry "
                    f"(Median {r['with_signal_pct_median']:.3f}%)"
                )
                lines.append(
                    f"- durchschnittlich maximal **{r['max_against_signal_pct_mean']:.3f}%** gegen das Signal "
                    f"(Median {r['max_against_signal_pct_median']:.3f}%)"
                )
                lines.append(
                    f"- danach durchschnittlich **{r['after_against_pct_mean']:.3f}%** unter Entry "
                    f"(Median {r['after_against_pct_median']:.3f}%; "
                    f"Anteil wieder unter Entry: {100 * r['share_reclaimed_entry_after_against']:.1f}%)"
                )
            else:
                lines.append(
                    f"- durchschnittlich maximal **{r['with_signal_pct_mean']:.3f}%** über Entry "
                    f"(Median {r['with_signal_pct_median']:.3f}%)"
                )
                lines.append(
                    f"- durchschnittlich maximal **{r['max_against_signal_pct_mean']:.3f}%** gegen das Signal "
                    f"(Median {r['max_against_signal_pct_median']:.3f}%)"
                )
                lines.append(
                    f"- danach durchschnittlich **{r['after_against_pct_mean']:.3f}%** über Entry "
                    f"(Median {r['after_against_pct_median']:.3f}%; "
                    f"Anteil wieder über Entry: {100 * r['share_reclaimed_entry_after_against']:.1f}%)"
                )
            lines.append(
                f"- typische Dauer: mit Signal **{r['with_signal_bars_median']:.1f}** Bars, "
                f"Gegenlauf **{r['against_bars_median']:.1f}** Bars, "
                f"danach **{r['after_against_bars_from_against_median']:.1f}** Bars ab Gegenlauf"
            )
            lines.append(f"- n = {int(r['n_signals'])} "
                         f"(incomplete horizons: {int(r['n_incomplete_horizon'])})")
            lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_simple_path_audit(
    *,
    symbol: str = "APTUSDT",
    output_dir: Path = DEFAULT_OUT,
    baseline_dir: Path = DEFAULT_BASELINE_DIR,
    analyze_start: str = ANALYZE_START,
    analyze_end: str = ANALYZE_END,
    cached_csv: Path | None = CACHED_APT_FRAME,
) -> dict[str, Any]:
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    baseline = assert_baseline_readonly(baseline_dir)
    if not baseline.get("hash_matches"):
        raise RuntimeError(
            f"baseline hash mismatch: expected {C2_BASELINE_HASH}, got {baseline.get('baseline_hash')}"
        )

    cfg = baseline_a6()
    frame = build_research_frame(
        symbol,
        analyze_start=analyze_start,
        analyze_end=analyze_end,
        cached_csv=cached_csv if symbol == "APTUSDT" else None,
    )
    _timeline, entries = apply_pullback_entry(frame, cfg)
    cases = build_cases(
        frame,
        entries,
        symbol=symbol,
        variant=cfg.name,
        timeframe=TIMEFRAME,
        horizons=HORIZONS,
    )
    summary, by_side = build_summaries(cases)

    n_filled = cases["fill_bar"].nunique() if not cases.empty else 0
    # Unique signals = unique fill events (one per entry), not horizon rows
    if not cases.empty:
        sig = cases.drop_duplicates(subset=["fill_bar", "side", "entry_price"])
        n_filled = len(sig)
        n_short = int((sig["side"] == "short").sum())
        n_long = int((sig["side"] == "long").sum())
    else:
        n_short = n_long = 0

    metadata = {
        "symbol": symbol,
        "variant": cfg.name,
        "variant_label": cfg.label,
        "config_hash": config_hash(cfg),
        "timeframe": TIMEFRAME,
        "analyze_start": analyze_start,
        "analyze_end": analyze_end,
        "load_start": LOAD_START,
        "load_end": LOAD_END,
        "horizons": list(HORIZONS),
        "n_frame_bars": len(frame),
        "n_raw_entries_from_sm": len(entries),
        "n_filled_signals": n_filled,
        "n_short": n_short,
        "n_long": n_long,
        "baseline_reference_hash": C2_BASELINE_HASH,
        "baseline_hash_matches": True,
        "production_sm_unchanged": True,
        "pine_unchanged": True,
        "no_lookahead": True,
        "fill_mode": "next_open",
        "path_window": "fill_bar inclusive through fill_bar+horizon-1",
        "point3_starts_at": "first max-adverse bar (inclusive)",
    }
    blob = json.dumps(json_safe({k: v for k, v in metadata.items()}), sort_keys=True).encode()
    metadata["content_hash"] = hashlib.sha1(blob).hexdigest()

    cases.to_csv(output_dir / "simple_path_cases.csv", index=False)
    summary.to_csv(output_dir / "simple_path_summary.csv", index=False)
    by_side.to_csv(output_dir / "simple_path_by_side.csv", index=False)
    (output_dir / "metadata.json").write_text(
        json.dumps(json_safe(metadata), indent=2), encoding="utf-8"
    )
    write_report(by_side=by_side, metadata=metadata, path=output_dir / "report.md")
    return metadata


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="C3.5 simple path audit (research-only)")
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = p.parse_args(argv)
    meta = run_simple_path_audit(symbol=args.symbol, output_dir=args.out)
    print(json.dumps(json_safe(meta), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
