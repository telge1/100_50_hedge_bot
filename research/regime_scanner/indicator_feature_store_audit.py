"""Phase C3.2A indicator feature store audit (research-only).

Computes / loads versioned indicator features, writes quality artifacts,
batch/incremental parity, and optional Pine reference plots. Does **not**
change regime classification or production config.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.indicator_feature_store import (
    INDICATOR_FEATURE_VERSION,
    InMemoryIndicatorFeatureRepository,
    ParquetIndicatorFeatureRepository,
    assert_batch_incremental_parity,
    attach_indicator_features_to_context,
    backfill_indicator_features,
    compute_indicator_features,
    features_content_hash,
    load_ohlcv_frame,
    load_ohlcv_with_warmup,
    required_indicator_warmup_bars,
)
from research.regime_scanner.indicators import ema, wilder_rma, true_range, directional_moves
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.trend_audit_shared_replay import (
    attach_c32a_indicator_features,
    build_shared_structure_timeline,
)
from research.regime_scanner.trend_pine_export import (
    AUDIT_ANCHOR_PLOT,
    build_pine_header,
    escape_pine_string,
    validate_pine_script,
)

SAMPLE_COLUMNS = (
    "symbol",
    "timeframe",
    "timestamp",
    "close",
    "ema_9",
    "ema_20",
    "ema_59",
    "ema_200",
    "atr_14",
    "adx_14",
    "plus_di_14",
    "minus_di_14",
    "di_spread",
    "ema_9_20_spread_atr",
    "ema_20_59_spread_atr",
    "ema_9_slope_3_atr",
    "ema_20_slope_3_atr",
    "ema_59_slope_3_atr",
    "ema_fast_cross_count_24",
    "ema_fast_compression_score",
    "ema_fast_expansion_score",
    "features_ready",
)


def _quantile_stats(series: pd.Series) -> dict[str, float | None]:
    s = pd.to_numeric(series, errors="coerce").dropna()
    if s.empty:
        return {"mean": None, "p25": None, "p50": None, "p75": None, "p90": None}
    return {
        "mean": float(s.mean()),
        "p25": float(s.quantile(0.25)),
        "p50": float(s.quantile(0.50)),
        "p75": float(s.quantile(0.75)),
        "p90": float(s.quantile(0.90)),
    }


def _field_quality(features: pd.DataFrame, cols: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for c in cols:
        if c not in features.columns:
            out[c] = {"missing": len(features), "non_finite": 0, "present": False}
            continue
        ser = features[c]
        if ser.dtype == object or ser.dtype == bool:
            missing = int(ser.isna().sum())
            out[c] = {"missing": missing, "non_finite": 0, "present": True}
            continue
        num = pd.to_numeric(ser, errors="coerce")
        missing = int(num.isna().sum())
        finite = num.apply(lambda x: bool(isinstance(x, (int, float, np.floating)) and math.isfinite(float(x))) if pd.notna(x) else False)
        non_finite = int((~finite & num.notna()).sum())
        out[c] = {"missing": missing, "non_finite": non_finite, "present": True}
    return out


def independent_ema_reference(close: pd.Series, period: int) -> pd.Series:
    """Independent recursive EMA (same formula as project ema, explicit loop)."""
    values = pd.to_numeric(close, errors="coerce").astype("float64").to_numpy()
    alpha = 2.0 / (period + 1.0)
    out = np.full(len(values), np.nan, dtype="float64")
    if len(values) == 0:
        return pd.Series(out)
    out[0] = values[0]
    for i in range(1, len(values)):
        if not math.isfinite(values[i]):
            out[i] = out[i - 1]
            continue
        prev = out[i - 1]
        out[i] = alpha * values[i] + (1.0 - alpha) * prev
    return pd.Series(out)


def reference_parity_report(candles: pd.DataFrame, features: pd.DataFrame) -> dict[str, Any]:
    """Compare store features vs independent EMA + Wilder helpers."""
    close = pd.to_numeric(candles["close"], errors="coerce").astype("float64")
    high = pd.to_numeric(candles["high"], errors="coerce").astype("float64")
    low = pd.to_numeric(candles["low"], errors="coerce").astype("float64")
    report: dict[str, Any] = {
        "initialization": {
            "ema": "ewm(span=period, adjust=False) / recursive alpha=2/(period+1), seed=first close",
            "atr_adx": "Wilder RMA via ewm(alpha=1/period, adjust=False); not SMA-seeded",
            "tradingview_note": (
                "TradingView often SMA-seeds the first period bars; series converge "
                "after sufficient warmup. This audit does not claim TradingView identity."
            ),
        },
        "ema_parity": {},
        "atr_adx_parity": {},
    }
    for p in (9, 20, 59, 200):
        ref = independent_ema_reference(close, p)
        store = pd.to_numeric(features[f"ema_{p}"], errors="coerce")
        diff = (ref - store).abs()
        ready = features["features_ready"].astype(bool) if "features_ready" in features.columns else pd.Series([True] * len(features))
        # Align lengths
        n = min(len(diff), len(ready))
        diff = diff.iloc[:n]
        ready = ready.iloc[:n]
        ready_diff = diff.loc[ready.to_numpy()]
        report["ema_parity"][f"ema_{p}"] = {
            "max_abs_all": float(diff.max()) if len(diff) else None,
            "max_abs_ready": float(ready_diff.max()) if len(ready_diff) else None,
            "mean_abs_ready": float(ready_diff.mean()) if len(ready_diff) else None,
        }

    atr_ref = wilder_rma(true_range(high, low, close), 14)
    plus_dm, minus_dm = directional_moves(high, low)
    plus_rma = wilder_rma(plus_dm, 14)
    minus_rma = wilder_rma(minus_dm, 14)
    # DI / ADX match indicators.py path
    from research.regime_scanner.indicators import _safe_div

    plus_di = 100.0 * _safe_div(plus_rma, atr_ref)
    minus_di = 100.0 * _safe_div(minus_rma, atr_ref)
    dx = 100.0 * _safe_div((plus_di - minus_di).abs(), plus_di + minus_di)
    adx_ref = wilder_rma(dx, 14)

    for name, ref, col in (
        ("atr_14", atr_ref, "atr_14"),
        ("adx_14", adx_ref, "adx_14"),
        ("plus_di_14", plus_di, "plus_di_14"),
        ("minus_di_14", minus_di, "minus_di_14"),
    ):
        store = pd.to_numeric(features[col], errors="coerce")
        diff = (ref.reset_index(drop=True) - store.reset_index(drop=True)).abs()
        report["atr_adx_parity"][name] = {
            "max_abs": float(diff.max()) if len(diff) else None,
            "mean_abs": float(diff.mean()) if len(diff) else None,
        }
    return report


def build_distribution_summary(features: pd.DataFrame) -> dict[str, Any]:
    ready = features.loc[features["features_ready"].astype(bool)].copy() if len(features) else features
    n = max(len(ready), 1)

    def pct(col: str) -> float:
        if col not in ready.columns or ready.empty:
            return 0.0
        return float(ready[col].fillna(0).astype(float).sum() / n * 100.0)

    adx = pd.to_numeric(ready.get("adx_14", pd.Series(dtype=float)), errors="coerce")
    return {
        "percent_ema_fast_bullish": pct("ema_fast_bullish"),
        "percent_ema_fast_bearish": pct("ema_fast_bearish"),
        "percent_bullish_ordered": pct("ema_bullish_ordered"),
        "percent_bearish_ordered": pct("ema_bearish_ordered"),
        "percent_adx_below_15": float((adx < 15).sum() / n * 100.0) if len(adx) else 0.0,
        "percent_adx_15_20": float(((adx >= 15) & (adx < 20)).sum() / n * 100.0) if len(adx) else 0.0,
        "percent_adx_20_25": float(((adx >= 20) & (adx < 25)).sum() / n * 100.0) if len(adx) else 0.0,
        "percent_adx_above_25": float((adx >= 25).sum() / n * 100.0) if len(adx) else 0.0,
        "ema_9_20_abs_spread_atr": _quantile_stats(ready.get("ema_9_20_abs_spread_atr", pd.Series(dtype=float))),
        "ema_9_slope_3_atr": _quantile_stats(ready.get("ema_9_slope_3_atr", pd.Series(dtype=float))),
        "ema_20_slope_3_atr": _quantile_stats(ready.get("ema_20_slope_3_atr", pd.Series(dtype=float))),
        "ema_59_slope_3_atr": _quantile_stats(ready.get("ema_59_slope_3_atr", pd.Series(dtype=float))),
        "di_spread": _quantile_stats(ready.get("di_spread", pd.Series(dtype=float))),
    }


def write_indicator_pine(
    features: pd.DataFrame,
    *,
    output_path: Path,
    title: str = "APTUSDT Indicator Feature Audit",
    overlay: bool = True,
) -> Path:
    """Static Pine that plots live TV indicators for visual comparison (not embedded values)."""
    lines = [
        "//@version=6",
        "indicator(",
        f'    "{escape_pine_string(title)}",',
        f"    overlay={'true' if overlay else 'false'},",
        "    max_labels_count=50,",
        "    max_lines_count=50",
        ")",
        "",
        AUDIT_ANCHOR_PLOT,
        "",
        "// Live TradingView references for visual parity review (not embedded audit arrays).",
        "// Project init: EMA adjust=False; ATR/ADX Wilder ewm(alpha=1/period). TV may SMA-seed.",
        "ema9 = ta.ema(close, 9)",
        "ema20 = ta.ema(close, 20)",
        "ema59 = ta.ema(close, 59)",
        "ema200 = ta.ema(close, 200)",
        "[diPlus, diMinus, adx14] = ta.dmi(14, 14)",
        "",
    ]
    if overlay:
        lines.extend(
            [
                'plot(ema9, title="EMA 9", color=color.new(color.teal, 0), linewidth=2)',
                'plot(ema20, title="EMA 20", color=color.new(color.orange, 0), linewidth=2)',
                'plot(ema59, title="EMA 59", color=color.new(color.purple, 0), linewidth=2)',
                'plot(ema200, title="EMA 200", color=color.new(color.gray, 0), linewidth=2)',
                "",
            ]
        )
    else:
        lines.extend(
            [
                'plot(adx14, title="ADX 14", color=color.new(color.yellow, 0), linewidth=2)',
                'plot(diPlus, title="Plus DI 14", color=color.new(color.green, 0), linewidth=1)',
                'plot(diMinus, title="Minus DI 14", color=color.new(color.red, 0), linewidth=1)',
                'hline(20, "ADX 20", color=color.gray)',
                'hline(25, "ADX 25", color=color.gray)',
                "",
            ]
        )
    if len(features):
        first = pd.Timestamp(features["timestamp"].iloc[0]).isoformat()
        last = pd.Timestamp(features["timestamp"].iloc[-1]).isoformat()
        lines.append(f'// Audit window: {first} .. {last}  version={INDICATOR_FEATURE_VERSION}')
        lines.append("")
    text = "\n".join(lines) + "\n"
    validate_pine_script(text)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(text, encoding="utf-8")
    return output_path


def run_audit(
    *,
    symbol: str,
    timeframe: str,
    load_start: str,
    load_end: str,
    analyze_start: str,
    analyze_end: str,
    output_dir: Path,
    feature_version: str = INDICATOR_FEATURE_VERSION,
    force_rebuild: bool = False,
) -> dict[str, Any]:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = output_dir / ".cache"
    repo = ParquetIndicatorFeatureRepository(cache_dir)

    t0 = time.perf_counter()
    features, stats = backfill_indicator_features(
        symbol=symbol,
        timeframe=timeframe,
        start=load_start,
        end=load_end,
        repository=repo,
        feature_version=feature_version,
        force_rebuild=force_rebuild,
    )
    cold_s = time.perf_counter() - t0

    t1 = time.perf_counter()
    reloaded = repo.load(
        symbol=symbol,
        timeframe=timeframe,
        start=pd.Timestamp(analyze_start, tz="UTC")
        if pd.Timestamp(analyze_start).tzinfo is None
        else pd.Timestamp(analyze_start),
        end=pd.Timestamp(analyze_end, tz="UTC") - pd.Timedelta(microseconds=1)
        if pd.Timestamp(analyze_end).tzinfo is None
        else pd.Timestamp(analyze_end) - pd.Timedelta(microseconds=1),
        feature_version=feature_version,
    )
    reload_s = time.perf_counter() - t1

    a0 = pd.Timestamp(analyze_start)
    a1 = pd.Timestamp(analyze_end)
    if a0.tzinfo is None:
        a0 = a0.tz_localize("UTC")
    if a1.tzinfo is None:
        a1 = a1.tz_localize("UTC")
    audit_slice = features.loc[
        (features["timestamp"] >= a0) & (features["timestamp"] < a1)
    ].copy()

    # Full OHLCV used for calculation (with warmup) for parity
    candles = load_ohlcv_frame(symbol, timeframe, start=load_start, end=load_end)
    # Align candle frame used for calc: backfill loads with warmup before start
    warm = required_indicator_warmup_bars()
    from research.regime_scanner.timeframes import TIMEFRAME_MINUTES

    warm_start = a0 - pd.Timedelta(minutes=TIMEFRAME_MINUTES[timeframe] * warm)
    # Use load window for parity on the persisted range
    load_a0 = pd.Timestamp(load_start)
    if load_a0.tzinfo is None:
        load_a0 = load_a0.tz_localize("UTC")
    calc_candles = load_ohlcv_frame(
        symbol,
        timeframe,
        start=load_a0 - pd.Timedelta(minutes=TIMEFRAME_MINUTES[timeframe] * warm),
        end=load_end,
    )
    parity = assert_batch_incremental_parity(
        calc_candles.tail(min(80, len(calc_candles))).reset_index(drop=True),
        symbol=symbol,
        timeframe=timeframe,
    )
    ref_parity = reference_parity_report(
        calc_candles.reset_index(drop=True),
        compute_indicator_features(
            calc_candles, symbol=symbol, timeframe=timeframe, feature_version=feature_version
        ),
    )

    # Shared context attachment smoke (structure still independent)
    t2 = time.perf_counter()
    # Build a lightweight frame from features for attach demo — structure needs indicators on frame
    from research.regime_scanner.indicators import compute_indicator_frame
    from research.regime_scanner.config import default_regime_scanner_config

    # Use 5m for shared context is heavy; only attach features without full structure pass on 30m
    # for audit timing of attach path.
    ctx_stub = type("Ctx", (), {})()
    attach_indicator_features_to_context(ctx_stub, audit_slice)
    shared_attach_s = time.perf_counter() - t2

    quality_cols = [
        "ema_9",
        "ema_20",
        "ema_59",
        "ema_200",
        "atr_14",
        "adx_14",
        "plus_di_14",
        "minus_di_14",
        "di_spread",
        "ema_9_20_spread_atr",
        "ema_fast_compression_score",
        "features_ready",
    ]
    field_q = _field_quality(audit_slice, quality_cols)
    dist = build_distribution_summary(audit_slice)

    dupes = (
        int(audit_slice.duplicated(subset=["symbol", "timeframe", "timestamp"]).sum())
        if len(audit_slice)
        else 0
    )
    from research.regime_scanner.indicator_feature_store import detect_timestamp_gaps

    gaps = detect_timestamp_gaps(audit_slice, timeframe) if len(audit_slice) else []

    persistence_hash = features_content_hash(features)
    audit_hash = features_content_hash(audit_slice)

    # March chart window sample for visual review
    march = audit_slice.copy()
    segments = []
    if len(march):
        mid = march.iloc[len(march) // 2]
        segments.append(
            {
                "label": "audit_mid_bar",
                "timestamp": str(mid["timestamp"]),
                "close": float(mid["close"]),
                "ema_9": float(mid["ema_9"]) if pd.notna(mid["ema_9"]) else None,
                "ema_20": float(mid["ema_20"]) if pd.notna(mid["ema_20"]) else None,
                "ema_59": float(mid["ema_59"]) if pd.notna(mid["ema_59"]) else None,
                "ema_200": float(mid["ema_200"]) if pd.notna(mid["ema_200"]) else None,
                "adx_14": float(mid["adx_14"]) if pd.notna(mid["adx_14"]) else None,
                "plus_di_14": float(mid["plus_di_14"]) if pd.notna(mid["plus_di_14"]) else None,
                "minus_di_14": float(mid["minus_di_14"]) if pd.notna(mid["minus_di_14"]) else None,
                "di_spread": float(mid["di_spread"]) if pd.notna(mid["di_spread"]) else None,
                "ema_fast_cross_count_24": float(mid["ema_fast_cross_count_24"])
                if pd.notna(mid["ema_fast_cross_count_24"])
                else None,
                "ema_fast_compression_score": float(mid["ema_fast_compression_score"])
                if pd.notna(mid["ema_fast_compression_score"])
                else None,
            }
        )

    sample_cols = [c for c in SAMPLE_COLUMNS if c in audit_slice.columns]
    sample_path = output_dir / "indicator_features_sample.csv"
    audit_slice.loc[:, sample_cols].to_csv(sample_path, index=False)

    quality_rows = [
        {"field": k, **v} for k, v in field_q.items()
    ]
    pd.DataFrame(quality_rows).to_csv(output_dir / "indicator_feature_quality.csv", index=False)
    pd.DataFrame(segments).to_csv(output_dir / "indicator_feature_segments.csv", index=False)

    calc_parity = {
        "batch_incremental": parity,
        "reference": ref_parity,
        "cache_reload_rows": len(reloaded),
        "audit_rows": len(audit_slice),
        "persistence_hash": persistence_hash,
        "audit_hash": audit_hash,
        "reload_hash": features_content_hash(reloaded) if len(reloaded) else None,
    }
    (output_dir / "calculation_parity.json").write_text(
        json.dumps(json_safe(calc_parity), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    write_indicator_pine(
        audit_slice,
        output_path=output_dir / "indicator_feature_audit_overlay.pine",
        title=f"{symbol} Indicator Feature Audit",
        overlay=True,
    )
    write_indicator_pine(
        audit_slice,
        output_path=output_dir / "indicator_feature_audit_dmi.pine",
        title=f"{symbol} Indicator Feature Audit DMI",
        overlay=False,
    )

    ready_n = int(audit_slice["features_ready"].sum()) if len(audit_slice) else 0
    summary = {
        "phase": "C3.2A",
        "symbol": symbol,
        "timeframe": timeframe,
        "feature_version": feature_version,
        "load_window": {"start": load_start, "end": load_end},
        "analyze_window": {"start": analyze_start, "end": analyze_end},
        "candles_loaded_backfill": stats.candles_loaded,
        "features_persisted": stats.features_calculated,
        "analyze_candles": len(audit_slice),
        "feature_ready_bars": ready_n,
        "warmup_or_not_ready_bars": len(audit_slice) - ready_n,
        "first_feature_timestamp": stats.first_feature_timestamp,
        "last_feature_timestamp": stats.last_feature_timestamp,
        "first_ready_timestamp": stats.first_ready_timestamp,
        "first_analyze_timestamp": str(audit_slice["timestamp"].iloc[0]) if len(audit_slice) else None,
        "last_analyze_timestamp": str(audit_slice["timestamp"].iloc[-1]) if len(audit_slice) else None,
        "timestamp_gaps_analyze": len(gaps),
        "duplicate_keys": dupes,
        "distributions": dist,
        "field_quality": field_q,
        "batch_incremental_parity_ok": parity.get("parity_ok"),
        "persistence_hash": persistence_hash,
        "audit_hash": audit_hash,
        "performance": {
            "cold_backfill_s": cold_s,
            "cache_reload_s": reload_s,
            "shared_attach_s": shared_attach_s,
            "stats": stats.to_dict(),
        },
        "classification_unchanged": True,
        "production_unchanged": True,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(json_safe(summary), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    metadata = {
        "indicator_feature_version": feature_version,
        "required_warmup_bars": required_indicator_warmup_bars(),
        "formulas": {
            "ema": "pandas ewm(span, adjust=False)",
            "atr": "Wilder RMA of True Range",
            "adx": "Wilder RMA of DX from +DI/-DI",
            "slope": "value[t] - value[t-w]",
            "slope_atr": "(value[t] - value[t-w]) / atr_14[t]",
            "compression_score": "0.45*tight + 0.35*flat + 0.20*crosses24",
            "expansion_score": "0.45*wide + 0.40*directed + 0.15*few_cross",
        },
        "persistence": "parquet research cache (ParquetIndicatorFeatureRepository)",
        "artifacts": [
            "summary.json",
            "metadata.json",
            "indicator_features_sample.csv",
            "indicator_feature_quality.csv",
            "indicator_feature_segments.csv",
            "calculation_parity.json",
            "indicator_feature_audit_overlay.pine",
            "indicator_feature_audit_dmi.pine",
        ],
    }
    (output_dir / "metadata.json").write_text(
        json.dumps(json_safe(metadata), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="C3.2A indicator feature store audit")
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--timeframe", default="30m")
    p.add_argument("--load-start", default="2026-02-01")
    p.add_argument("--load-end", default="2026-03-15")
    p.add_argument("--analyze-start", default="2026-03-01")
    p.add_argument("--analyze-end", default="2026-03-12")
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("research/regime_scanner/results/phase_c3_2a_indicator_features"),
    )
    p.add_argument("--feature-version", default=INDICATOR_FEATURE_VERSION)
    p.add_argument("--force-rebuild", action="store_true")
    args = p.parse_args(argv)
    summary = run_audit(
        symbol=args.symbol,
        timeframe=args.timeframe,
        load_start=args.load_start,
        load_end=args.load_end,
        analyze_start=args.analyze_start,
        analyze_end=args.analyze_end,
        output_dir=args.output_dir,
        feature_version=args.feature_version,
        force_rebuild=args.force_rebuild,
    )
    print(json.dumps(json_safe(summary), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
