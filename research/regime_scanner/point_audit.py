"""CLI point-in-time audit for the backtest-only regime scanner."""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .classifier import build_regime_summary, classify_market_state, summarize_timeframe_regime
from .regime_snapshot import (
    build_regime_snapshot_from_point_audit,
    evaluate_setup_activation,
)
from .config import RegimeScannerConfig, default_regime_scanner_config
from .data_loader import feather_path_for_symbol, load_closed_candles_as_of
from .divergence import (
    detect_confirmed_divergences,
    detect_confirmed_multi_metric_divergences,
    detect_confirmed_price_adx_divergences,
    detect_developing_divergences,
    detect_price_atr_divergences,
    detect_recent_adx_di_pair_scans,
)
from .exhaustion import detect_structural_exhaustion
from .indicators import compute_indicator_frame, latest_indicator_snapshot
from .swings import (
    enrich_pivots,
    filter_pivots_as_of,
    find_confirmed_pivots,
    latest_pivots,
    pivots_by_type,
)
from .timeframes import (
    aggregate_candles,
    parse_timeframes,
    required_5m_history_candles,
)
from .trend_analysis import (
    analyze_ema_bands,
    analyze_ema_slopes,
    analyze_last_bar_changes,
    analyze_overextension,
    build_descriptive_summary,
    build_last_closed_table,
    collect_weakening_signals,
)


def parse_decision_time(value: datetime | pd.Timestamp | str) -> pd.Timestamp:
    if isinstance(value, pd.Timestamp):
        ts = value
    elif isinstance(value, datetime):
        ts = pd.Timestamp(value)
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        ts = pd.Timestamp(text)
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _finite(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return number


def _history_row(frame: pd.DataFrame, index: int) -> dict[str, Any]:
    row = frame.iloc[index]
    ts = row["timestamp"]
    if isinstance(ts, pd.Timestamp):
        ts_out = ts.isoformat()
    else:
        ts_out = str(ts)
    return {
        "offset_candles": int(len(frame) - 1 - index),
        "timestamp": ts_out,
        "close": _finite(row.get("close")),
        "ema_9": _finite(row.get("ema_9")),
        "ema_20": _finite(row.get("ema_20")),
        "ema_59": _finite(row.get("ema_59")),
        "ema_200": _finite(row.get("ema_200")),
        "close_vs_ema_20_pct": _finite(row.get("close_vs_ema_20_pct")),
        "close_vs_ema_200_pct": _finite(row.get("close_vs_ema_200_pct")),
        "atr_pct": _finite(row.get("atr_pct")),
        "plus_di": _finite(row.get("plus_di")),
        "minus_di": _finite(row.get("minus_di")),
        "di_spread": _finite(row.get("di_spread")),
        "adx": _finite(row.get("adx")),
        "ema_9_slope_3_pct": _finite(row.get("ema_9_slope_3_pct")),
        "ema_9_slope_6_pct": _finite(row.get("ema_9_slope_6_pct")),
        "ema_9_slope_12_pct": _finite(row.get("ema_9_slope_12_pct")),
        "ema_20_slope_6_pct": _finite(row.get("ema_20_slope_6_pct")),
        "ema_20_slope_12_pct": _finite(row.get("ema_20_slope_12_pct")),
        "ema_20_slope_48_pct": _finite(row.get("ema_20_slope_48_pct")),
        "ema_59_slope_12_pct": _finite(row.get("ema_59_slope_12_pct")),
        "ema_59_slope_48_pct": _finite(row.get("ema_59_slope_48_pct")),
        "ema_200_slope_48_pct": _finite(row.get("ema_200_slope_48_pct")),
        "ema_200_slope_144_pct": _finite(row.get("ema_200_slope_144_pct")),
        "ema_9_vs_ema_20_pct": _finite(row.get("ema_9_vs_ema_20_pct")),
        "ema_20_vs_ema_59_pct": _finite(row.get("ema_20_vs_ema_59_pct")),
        "ema_59_vs_ema_200_pct": _finite(row.get("ema_59_vs_ema_200_pct")),
    }


def build_history_rows(
    frame: pd.DataFrame,
    *,
    history_candles: int,
    config: RegimeScannerConfig,
) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    max_offset = max(0, int(history_candles))
    offsets = [o for o in config.history_offsets if o <= max_offset]
    rows: list[dict[str, Any]] = []
    last = len(frame) - 1
    for offset in offsets:
        idx = last - offset
        if idx < 0:
            continue
        rows.append(_history_row(frame, idx))
    return rows


def _rollover_flags(weakening_signals: list[dict[str, Any]]) -> dict[str, Any]:
    metrics = {str(s.get("metric") or "") for s in (weakening_signals or [])}
    return {
        "adx_last_bar_rollover": "ADX_LAST_BAR_ROLLOVER" in metrics,
        "plus_di_last_bar_rollover": "PLUS_DI_LAST_BAR_ROLLOVER" in metrics,
        "di_spread_last_bar_rollover": "DI_SPREAD_LAST_BAR_ROLLOVER" in metrics,
        "atr_pct_last_bar_rollover": "ATR_PERCENT_LAST_BAR_ROLLOVER" in metrics,
        "multi_metric_last_bar_rollover": "MULTI_METRIC_LAST_BAR_ROLLOVER" in metrics,
        "signals": [
            s
            for s in (weakening_signals or [])
            if "LAST_BAR_ROLLOVER" in str(s.get("metric") or "")
        ],
    }


def build_timeframe_audit_from_candles(
    closed: pd.DataFrame,
    *,
    timeframe: str,
    decision_time: pd.Timestamp,
    config: RegimeScannerConfig,
    history_candles: int = 144,
    include_classification: bool = True,
) -> dict[str, Any]:
    """Run full indicator/structure audit on already-aggregated closed candles."""
    tf_cfg = config.with_timeframe(timeframe)
    decision_ts = parse_decision_time(decision_time)
    if closed.empty:
        raise ValueError(
            f"no closed {timeframe} candles available before decision_time={decision_ts.isoformat()}"
        )

    indicators = compute_indicator_frame(closed, config=tf_cfg)
    snapshot = latest_indicator_snapshot(indicators, config=tf_cfg)

    pivots = find_confirmed_pivots(indicators, config=tf_cfg)
    # Confirmation must be known by the last closed bar of this timeframe.
    last_ts = pd.Timestamp(indicators.iloc[-1]["timestamp"])
    if last_ts.tzinfo is None:
        last_ts = last_ts.tz_localize("UTC")
    else:
        last_ts = last_ts.tz_convert("UTC")
    pivots = [
        p
        for p in pivots
        if pd.Timestamp(p.confirmation_timestamp) <= last_ts
        and pd.Timestamp(p.confirmation_timestamp) <= decision_ts
    ]
    # Keep candle-open strictness for 5m (confirmation open < decision).
    if timeframe == "5m":
        pivots = filter_pivots_as_of(pivots, decision_ts)

    enriched = enrich_pivots(pivots, indicators, timeframe=timeframe)
    slope_analysis = analyze_ema_slopes(indicators, config=tf_cfg)
    band_analysis = analyze_ema_bands(indicators, config=tf_cfg)
    overextension = analyze_overextension(indicators, config=tf_cfg)
    last_bar_changes = analyze_last_bar_changes(indicators, config=tf_cfg)
    atr_divergences = detect_price_atr_divergences(indicators, pivots, config=tf_cfg)
    recent_pair_scans = detect_recent_adx_di_pair_scans(indicators, pivots, config=tf_cfg)
    multi_metric = detect_confirmed_multi_metric_divergences(
        indicators, pivots, config=tf_cfg
    )
    confirmed_divergences = detect_confirmed_divergences(indicators, pivots, config=tf_cfg)
    adx_focus = detect_confirmed_price_adx_divergences(indicators, pivots, config=tf_cfg)
    developing = detect_developing_divergences(
        indicators, pivots, timeframe=timeframe, config=tf_cfg
    )
    structural = detect_structural_exhaustion(
        indicators, pivots, timeframe=timeframe, config=tf_cfg
    )
    weakening_signals = collect_weakening_signals(
        indicators,
        slope_analysis=slope_analysis,
        band_analysis=band_analysis,
        last_bar_changes=last_bar_changes,
        config=tf_cfg,
    )
    last_two_highs = enrich_pivots(
        latest_pivots(pivots, "high", count=2), indicators, timeframe=timeframe
    )
    last_two_lows = enrich_pivots(
        latest_pivots(pivots, "low", count=2), indicators, timeframe=timeframe
    )
    last_five_high_pairs = multi_metric["pivot_high_pairs"]["recent_pair_results"]
    last_five_low_pairs = multi_metric["pivot_low_pairs"]["recent_pair_results"]
    summary = build_descriptive_summary(
        ema_order=snapshot.get("ema_order") if isinstance(snapshot.get("ema_order"), str) else snapshot.get("ema_order"),
        slope_analysis=slope_analysis,
        band_analysis=band_analysis,
        frame=indicators,
        pivots_high_count=len(pivots_by_type(pivots, "high")),
        pivots_low_count=len(pivots_by_type(pivots, "low")),
        last_two_highs=last_two_highs,
        last_two_lows=last_two_lows,
        confirmed_divergences=confirmed_divergences,
        weakening_signals=weakening_signals,
        overextension=overextension,
        last_bar_changes=last_bar_changes,
        config=tf_cfg,
    )
    history = build_history_rows(
        indicators,
        history_candles=history_candles,
        config=tf_cfg,
    )
    last_closed_table = build_last_closed_table(
        indicators,
        candles=tf_cfg.last_closed_table_candles,
        config=tf_cfg,
    )

    classification = None
    if include_classification:
        classification = classify_market_state(
            candles_used=int(len(closed)),
            warmup_sufficient=bool(snapshot["warmup_sufficient"]),
            ema=snapshot["ema"],
            ema_order=snapshot.get("ema_order") if isinstance(snapshot.get("ema_order"), str) else None,
            close_vs_ema_pct=snapshot["close_vs_ema_pct"],
            atr=snapshot.get("atr"),
            atr_pct=snapshot.get("atr_pct"),
            plus_di=snapshot.get("plus_di"),
            minus_di=snapshot.get("minus_di"),
            di_spread=snapshot.get("di_spread"),
            adx=snapshot.get("adx"),
            ema_slope_comparisons=slope_analysis,
            ema_bands=band_analysis,
            overextension=overextension,
            confirmed_pivots={
                "high_count": len(pivots_by_type(pivots, "high")),
                "low_count": len(pivots_by_type(pivots, "low")),
                "last_two_highs": last_two_highs,
                "last_two_lows": last_two_lows,
            },
            confirmed_divergences=confirmed_divergences,
            weakening_signals=weakening_signals,
            summary=summary,
            config=tf_cfg,
        )

    rollover = _rollover_flags(weakening_signals)
    confirmed_key = f"confirmed_divergences_{timeframe}"
    out = {
        "timeframe": timeframe,
        "candles_loaded": int(len(closed)),
        "warmup_sufficient": bool(snapshot["warmup_sufficient"]),
        "min_warmup_candles": int(snapshot["min_warmup_candles"]),
        "last_closed_candle": snapshot["last_closed_candle"],
        "ema": snapshot["ema"],
        "ema_order": snapshot["ema_order"],
        "close_vs_ema_pct": snapshot["close_vs_ema_pct"],
        "ema_pair_distance_pct": snapshot["ema_pair_distance_pct"],
        "ema_slopes_pct": snapshot["ema_slopes_pct"],
        "ema_slope_comparisons": slope_analysis,
        "ema_bands": band_analysis,
        "overextension": overextension,
        "atr": snapshot["atr"],
        "atr_pct": snapshot["atr_pct"],
        "plus_di": snapshot["plus_di"],
        "minus_di": snapshot["minus_di"],
        "di_spread": snapshot["di_spread"],
        "adx": snapshot["adx"],
        "confirmed_pivots": {
            "high_count": len(pivots_by_type(pivots, "high")),
            "low_count": len(pivots_by_type(pivots, "low")),
            "last_two_highs": last_two_highs,
            "last_two_lows": last_two_lows,
            "last_five_high_pairs": last_five_high_pairs,
            "last_five_low_pairs": last_five_low_pairs,
            "all": enriched,
        },
        confirmed_key: confirmed_divergences,
        "confirmed_divergences": confirmed_divergences,
        "confirmed_multi_metric_divergences": multi_metric,
        "confirmed_price_adx_divergences": adx_focus,
        "price_atr_divergences": atr_divergences,
        "recent_swing_pair_scans": recent_pair_scans,
        "developing_bearish_divergence": developing["developing_bearish_divergence"],
        "developing_bullish_divergence": developing["developing_bullish_divergence"],
        "structural_exhaustion": structural,
        "classic_pivot_divergence": structural.get("classic_pivot_divergence"),
        "equal_high_retest_exhaustion": structural.get("equal_high_retest_exhaustion"),
        "lower_high_momentum_weakness": structural.get("lower_high_momentum_weakness"),
        "developing_structural_exhaustion": structural.get(
            "developing_structural_exhaustion"
        ),
        "retest_high_candidate": structural.get("retest_high_candidate"),
        "last_bar_changes": last_bar_changes,
        "last_bar_rollover": rollover,
        "last_closed_table": last_closed_table,
        "weakening_signals": weakening_signals,
        "history": history,
        "summary": summary,
        "classification": classification,
        "open_interest": snapshot["open_interest"],
    }
    # Simple regime summary for this timeframe (keeps technical signal names intact).
    out["regime_summary"] = summarize_timeframe_regime(out, config=tf_cfg)
    return out


def build_comparison_table(timeframe_payloads: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for tf, payload in timeframe_payloads.items():
        last = payload.get("last_closed_candle") or {}
        developing_bearish = payload.get("developing_bearish_divergence") is not None
        developing_struct = payload.get("developing_structural_exhaustion") is not None
        confirmed_bearish = any(
            str(d.get("status", "")).startswith("confirmed_bearish")
            for d in (payload.get("confirmed_divergences") or [])
        )
        multi = payload.get("confirmed_multi_metric_divergences") or {}
        if multi.get("confirmed_bearish"):
            confirmed_bearish = True
        equal_exh = bool(payload.get("equal_high_retest_exhaustion"))
        lower_weak = bool(payload.get("lower_high_momentum_weakness"))
        rollover = payload.get("last_bar_rollover") or {}
        rows.append(
            {
                "timeframe": tf,
                "last_candle": last.get("timestamp"),
                "ema_orientation": payload.get("ema_order"),
                "adx": payload.get("adx"),
                "atr_pct": payload.get("atr_pct"),
                "confirmed_bearish_div": confirmed_bearish,
                "developing_bearish_div": developing_bearish,
                "equal_high_exhaustion": equal_exh or developing_struct,
                "lower_high_weakness": lower_weak,
                "last_bar_rollover": {
                    k: v for k, v in rollover.items() if k != "signals"
                },
            }
        )
    return rows


def build_point_audit(
    *,
    symbol: str,
    decision_time: datetime | pd.Timestamp | str,
    data_dir: str | Path | None = None,
    config: RegimeScannerConfig | None = None,
    candles: pd.DataFrame | None = None,
    history_candles: int = 144,
    timeframes: str | list[str] | tuple[str, ...] | None = None,
    previous_combined_regime: object | None = None,
    include_setup_activation: bool = True,
) -> dict[str, Any]:
    """Build a causal point audit payload for ``decision_time``.

    When multiple timeframes are requested, indicators/pivots/divergences are
    recomputed independently on each causally aggregated OHLCV frame.

    Phase 1 also attaches ``regime_snapshot`` and optionally ``setup_activation``
    (no entry / TP). Pass ``previous_combined_regime`` for regime-change edges.
    """
    cfg = config or default_regime_scanner_config()
    decision_ts = parse_decision_time(decision_time)
    requested = parse_timeframes(timeframes)

    if candles is None:
        closed_5m = load_closed_candles_as_of(
            symbol,
            decision_ts,
            data_dir=data_dir,
            config=cfg,
        )
    else:
        frame = candles.copy()
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        closed_5m = frame.loc[frame["timestamp"] < decision_ts].copy().reset_index(drop=True)

    if closed_5m.empty:
        raise ValueError(
            f"no closed candles available for {symbol} before decision_time={decision_ts.isoformat()}"
        )

    tf_payloads: dict[str, dict[str, Any]] = {}
    for tf in requested:
        aggregated = aggregate_candles(closed_5m, tf, decision_ts)
        tf_payloads[tf] = build_timeframe_audit_from_candles(
            aggregated,
            timeframe=tf,
            decision_time=decision_ts,
            config=cfg,
            history_candles=history_candles,
            include_classification=True,
        )

    # Backward-compatible single-TF flat payload (default / explicit 5m only).
    if requested == ("5m",):
        payload = dict(tf_payloads["5m"])
        combined = build_regime_summary({"5m": tf_payloads["5m"]}, config=cfg)
        payload.update(
            {
                "symbol": str(symbol).upper(),
                "decision_time": decision_ts.isoformat(),
                "decision_mode": "candle-open",
                "source_feather": str(feather_path_for_symbol(symbol, data_dir=data_dir)),
                "timeframes": ["5m"],
                "combined_regime": combined,
            }
        )
        snapshot = build_regime_snapshot_from_point_audit(
            payload,
            previous_combined_regime=previous_combined_regime,
        )
        payload["regime_snapshot"] = snapshot
        if include_setup_activation:
            payload["setup_activation"] = evaluate_setup_activation(snapshot)
        return payload

    comparison = build_comparison_table(tf_payloads)
    combined = build_regime_summary(tf_payloads, config=cfg)
    out: dict[str, Any] = {
        "symbol": str(symbol).upper(),
        "decision_time": decision_ts.isoformat(),
        "decision_mode": "candle-open",
        "source_feather": str(feather_path_for_symbol(symbol, data_dir=data_dir)),
        "timeframes": list(requested),
        "by_timeframe": tf_payloads,
        "confirmed_divergences_5m": (tf_payloads.get("5m") or {}).get("confirmed_divergences"),
        "confirmed_divergences_15m": (tf_payloads.get("15m") or {}).get("confirmed_divergences"),
        "confirmed_divergences_30m": (tf_payloads.get("30m") or {}).get("confirmed_divergences"),
        "comparison_table": comparison,
        "combined_regime": combined,
        "regime_summary": combined,
    }
    if "5m" in tf_payloads:
        base = tf_payloads["5m"]
        out.update(
            {
                "candles_loaded": base["candles_loaded"],
                "warmup_sufficient": base["warmup_sufficient"],
                "min_warmup_candles": base["min_warmup_candles"],
                "last_closed_candle": base["last_closed_candle"],
                "ema": base["ema"],
                "ema_order": base["ema_order"],
                "atr": base["atr"],
                "atr_pct": base["atr_pct"],
                "adx": base["adx"],
                "plus_di": base["plus_di"],
                "minus_di": base["minus_di"],
                "di_spread": base["di_spread"],
                "classification": base.get("classification"),
                "open_interest": base.get("open_interest"),
                "summary": base.get("summary"),
            }
        )
    snapshot = build_regime_snapshot_from_point_audit(
        out,
        previous_combined_regime=previous_combined_regime,
    )
    out["regime_snapshot"] = snapshot
    if include_setup_activation:
        out["setup_activation"] = evaluate_setup_activation(snapshot)
    return out


# Re-export for tests / callers.
__all_helpers__ = (required_5m_history_candles,)


def json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (int, str)):
        return value
    return value


def _fmt(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def format_timeframe_section(payload: dict[str, Any]) -> list[str]:
    last = payload.get("last_closed_candle") or {}
    lines = [
        f"Timeframe:            {payload.get('timeframe')}",
        f"Candles loaded:       {payload.get('candles_loaded')}",
        f"Warmup sufficient:    {payload.get('warmup_sufficient')} "
        f"(min={payload.get('min_warmup_candles')})",
        "Last closed candle:",
        f"  timestamp: {last.get('timestamp')}",
        f"  open:      {last.get('open')}",
        f"  high:      {last.get('high')}",
        f"  low:       {last.get('low')}",
        f"  close:     {last.get('close')}",
        f"  volume:    {last.get('volume')}",
        f"EMA order:            {payload.get('ema_order')}",
        f"ATR:                  {payload.get('atr')}",
        f"ATR%:                 {payload.get('atr_pct')}",
        f"ADX:                  {payload.get('adx')}",
        f"+DI:                  {payload.get('plus_di')}",
        f"-DI:                  {payload.get('minus_di')}",
        f"DI-spread:            {payload.get('di_spread')}",
    ]
    lines.append("Current slopes (selected):")
    slopes = payload.get("ema_slopes_pct") or {}
    for key in (
        "ema_9_slope_3_pct",
        "ema_20_slope_12_pct",
        "ema_59_slope_48_pct",
        "ema_200_slope_144_pct",
    ):
        lines.append(f"  {key}: {slopes.get(key)}")
    piv = payload.get("confirmed_pivots") or {}
    lines.append(
        f"Confirmed pivots: highs={piv.get('high_count')} lows={piv.get('low_count')}"
    )
    lines.append("Last confirmed pivot highs:")
    for item in (piv.get("last_two_highs") or [])[-2:]:
        lines.append(
            f"  ts={item.get('pivot_timestamp')} price={item.get('price')} "
            f"ADX={_fmt(item.get('adx'))} ATR%={_fmt(item.get('atr_pct'))} "
            f"conf={item.get('confirmation_timestamp')}"
        )
    lines.append("Last confirmed pivot lows:")
    for item in (piv.get("last_two_lows") or [])[-2:]:
        lines.append(
            f"  ts={item.get('pivot_timestamp')} price={item.get('price')} "
            f"ADX={_fmt(item.get('adx'))} ATR%={_fmt(item.get('atr_pct'))} "
            f"conf={item.get('confirmation_timestamp')}"
        )
    lines.append("Confirmed divergences:")
    confirmed = [
        d
        for d in (payload.get("confirmed_divergences") or [])
        if str(d.get("status", "")).startswith("confirmed_")
    ]
    if not confirmed:
        lines.append("  none")
    for item in confirmed:
        lines.append(
            f"  {item.get('status')} indicator={item.get('indicator')} "
            f"price_d={_fmt(item.get('price_change'))} "
            f"ind_d={_fmt(item.get('indicator_change'))}"
        )
    multi = payload.get("confirmed_multi_metric_divergences") or {}
    lines.append(
        f"Multi-metric confirmed bearish pairs: {len(multi.get('confirmed_bearish') or [])}"
    )
    lines.append(
        f"Multi-metric confirmed bullish pairs: {len(multi.get('confirmed_bullish') or [])}"
    )

    def _fmt_exhaustion_block(title: str, items: list[dict[str, Any]] | None) -> None:
        lines.append(f"{title}")
        if not items:
            lines.append("  none")
            return
        for item in items:
            structure = item.get("structure") or {}
            signals = item.get("signals") or []
            codes = ",".join(str(s.get("code")) for s in signals) or "no-signal"
            ref_ts = item.get("reference_pivot_timestamp") or (
                (item.get("first_pivot") or {}).get("pivot_timestamp")
            )
            cand_ts = item.get("candidate_timestamp") or (
                (item.get("second_pivot") or {}).get("pivot_timestamp")
            )
            lines.append(
                f"  {item.get('confirmation_status') or structure.get('structure_type')}: "
                f"{ref_ts} -> {cand_ts} "
                f"dist%={_fmt(structure.get('price_distance_pct') or item.get('price_distance_pct'))} "
                f"signals=[{codes}]"
            )

    _fmt_exhaustion_block(
        "Classic pivot divergence:",
        payload.get("classic_pivot_divergence") or [],
    )
    _fmt_exhaustion_block(
        "Equal-high/retest exhaustion:",
        payload.get("equal_high_retest_exhaustion") or [],
    )
    _fmt_exhaustion_block(
        "Lower-high momentum weakness:",
        payload.get("lower_high_momentum_weakness") or [],
    )
    lines.append("Developing structural exhaustion:")
    developing_struct = payload.get("developing_structural_exhaustion")
    if developing_struct is None:
        lines.append("  none")
    else:
        structure = developing_struct.get("structure") or {}
        comps = developing_struct.get("indicator_comparisons") or {}
        signals = developing_struct.get("signals") or []
        codes = ",".join(str(s.get("code")) for s in signals) or "no-signal"
        lines.append(
            f"  {developing_struct.get('confirmation_status')}: "
            f"ref={developing_struct.get('reference_pivot_timestamp')}@"
            f"{_fmt(developing_struct.get('reference_pivot_price'))} "
            f"cand={developing_struct.get('candidate_timestamp')}@"
            f"{_fmt(developing_struct.get('candidate_price'))} "
            f"dist%={_fmt(developing_struct.get('price_distance_pct'))} "
            f"dir={structure.get('price_direction')} "
            f"tol={developing_struct.get('tolerance_match')} "
            f"right={developing_struct.get('available_right_candles')}/"
            f"{developing_struct.get('required_right_candles')} "
            f"earliest={developing_struct.get('earliest_confirmation_time')} "
            f"signals=[{codes}]"
        )
        for metric in ("adx", "atr", "atr_pct", "plus_di", "di_spread"):
            c = comps.get(metric) or {}
            lines.append(
                f"    {metric}: ref={_fmt(c.get('reference_value'))} "
                f"cand={_fmt(c.get('candidate_value'))} "
                f"d%={_fmt(c.get('percent_change'))} "
                f"weak={c.get('weakening')}"
            )

    lines.append("Last-bar rollover:")
    rollover = payload.get("last_bar_rollover") or {}
    lines.append(
        f"  ADX={rollover.get('adx_last_bar_rollover')} "
        f"+DI={rollover.get('plus_di_last_bar_rollover')} "
        f"DIsp={rollover.get('di_spread_last_bar_rollover')} "
        f"ATR%={rollover.get('atr_pct_last_bar_rollover')} "
        f"MULTI={rollover.get('multi_metric_last_bar_rollover')}"
    )

    for label in ("developing_bearish_divergence", "developing_bullish_divergence"):
        item = payload.get(label)
        if item is None:
            lines.append(f"{label}: none")
        else:
            lines.append(
                f"{label}: candidate={item.get('developing_candidate_timestamp')} "
                f"price={_fmt(item.get('candidate_price'))} "
                f"missing={item.get('missing_confirmation_candles')} "
                f"earliest_conf={item.get('earliest_confirmation_time')}"
            )
    regime = payload.get("regime_summary") or {}
    lines.append(f"Timeframe regime:     {regime.get('regime')}")
    lines.append(f"Regime confidence:    {regime.get('confidence')}")
    return lines


def format_human_report(payload: dict[str, Any]) -> str:
    if payload.get("by_timeframe"):
        lines = [
            "Regime Scanner Multi-Timeframe Point Audit",
            "==========================================",
            f"Symbol:              {payload['symbol']}",
            f"Decision time:       {payload['decision_time']}",
            f"Decision mode:       {payload['decision_mode']}",
            f"Source feather:      {payload['source_feather']}",
            f"Timeframes:          {', '.join(payload.get('timeframes') or [])}",
            "",
        ]
        for tf in payload.get("timeframes") or []:
            section = payload["by_timeframe"].get(tf) or {}
            lines.append(f"--- {tf} ---")
            lines.extend(format_timeframe_section(section))
            lines.append("")
        lines.append("Comparison table:")
        lines.append(
            f"{'TF':<5} {'Last Candle':<26} {'EMA Orient':<28} {'ADX':>8} {'ATR%':>8} "
            f"{'ConfBear':>8} {'DevBear':>8} {'Rollover':<40}"
        )
        for row in payload.get("comparison_table") or []:
            rollover = row.get("last_bar_rollover") or {}
            roll_txt = (
                f"ADX={rollover.get('adx_last_bar_rollover')} "
                f"+DI={rollover.get('plus_di_last_bar_rollover')} "
                f"DIsp={rollover.get('di_spread_last_bar_rollover')}"
            )
            lines.append(
                f"{str(row.get('timeframe')):<5} {str(row.get('last_candle')):<26} "
                f"{str(row.get('ema_orientation')):<28} {_fmt(row.get('adx')):>8} "
                f"{_fmt(row.get('atr_pct')):>8} {str(row.get('confirmed_bearish_div')):>8} "
                f"{str(row.get('developing_bearish_div')):>8} {roll_txt:<40}"
            )
        combined = payload.get("combined_regime") or payload.get("regime_summary") or {}
        lines.extend(
            [
                "",
                "COMBINED REGIME",
                "---------------",
                f"Regime: {combined.get('regime')}",
                f"Confidence: {combined.get('confidence')}",
                "",
                "Per-timeframe regimes:",
            ]
        )
        for tf, summary in (combined.get("by_timeframe") or {}).items():
            lines.append(f"  {tf}: {summary.get('regime')} ({summary.get('confidence')})")
        lines.append("")
        lines.append("Reason codes:")
        for item in combined.get("reason_codes") or []:
            lines.append(f"  {item.get('code')}: {item.get('explanation')}")
        lines.append("")
        lines.append("Primary reasons:")
        for reason in combined.get("primary_reasons") or []:
            lines.append(f"  - {reason}")
        return "\n".join(lines)

    last = payload["last_closed_candle"]
    lines = [
        "Regime Scanner Point Audit",
        "==========================",
        f"Symbol:              {payload['symbol']}",
        f"Decision time:       {payload['decision_time']}",
        f"Decision mode:       {payload['decision_mode']}",
        f"Source feather:      {payload['source_feather']}",
        f"Candles loaded:      {payload['candles_loaded']}",
        f"Warmup sufficient:   {payload['warmup_sufficient']} "
        f"(min={payload['min_warmup_candles']})",
        "",
        "Last closed candle:",
        f"  timestamp: {last.get('timestamp')}",
        f"  open:      {last.get('open')}",
        f"  high:      {last.get('high')}",
        f"  low:       {last.get('low')}",
        f"  close:     {last.get('close')}",
        f"  volume:    {last.get('volume')}",
        "",
        "EMA:",
    ]
    for key, value in payload["ema"].items():
        lines.append(f"  {key}: {value}")
    lines.append(f"EMA order:           {payload.get('ema_order')}")
    lines.append("")
    lines.append("Close vs EMA %:")
    for key, value in payload["close_vs_ema_pct"].items():
        lines.append(f"  {key}: {value}")
    lines.append("")
    lines.append("EMA slope comparisons (current / previous / change / status):")
    for period, windows in (payload.get("ema_slope_comparisons") or {}).items():
        for window, item in windows.items():
            lines.append(
                f"  EMA{period} w{window}: "
                f"cur={_fmt(item.get('current_slope'))} "
                f"prev={_fmt(item.get('previous_slope'))} "
                f"d={_fmt(item.get('slope_change'))} "
                f"{item.get('status')} ({item.get('direction')})"
            )
    lines.append("")
    lines.append("EMA bands:")
    for pair, item in (payload.get("ema_bands") or {}).items():
        lines.append(
            f"  {pair}: orientation={item.get('orientation')} "
            f"abs={_fmt(item.get('current_abs_pct'))}"
        )
        for window, win in (item.get("windows") or {}).items():
            lines.append(
                f"    w{window}: {_fmt(win.get('previous_abs_pct'))} -> "
                f"{_fmt(win.get('current_abs_pct'))} "
                f"d_pp={_fmt(win.get('abs_change_pp'))} {win.get('status')}"
            )
    lines.extend(
        [
            "",
            f"ATR:                 {payload.get('atr')}",
            f"ATR%:                {payload.get('atr_pct')}",
            f"+DI:                 {payload.get('plus_di')}",
            f"-DI:                 {payload.get('minus_di')}",
            f"DI spread (+DI--DI): {payload.get('di_spread')}",
            f"ADX:                 {payload.get('adx')}",
            "",
            "Overextension (ATR units):",
        ]
    )
    for key, value in ((payload.get("overextension") or {}).get("close_vs_ema_atr_units") or {}).items():
        lines.append(f"  close vs {key}: {value}")
    lines.append("")
    piv = payload.get("confirmed_pivots") or {}
    lines.append(
        f"Confirmed pivots: highs={piv.get('high_count')} lows={piv.get('low_count')}"
    )
    lines.append("Last two confirmed pivot highs:")
    for item in piv.get("last_two_highs") or []:
        lines.append(
            f"  idx={item.get('pivot_index')} ts={item.get('pivot_timestamp')} "
            f"price={item.get('price')} confirmed={item.get('confirmation_timestamp')}"
        )
    lines.append("Last two confirmed pivot lows:")
    for item in piv.get("last_two_lows") or []:
        lines.append(
            f"  idx={item.get('pivot_index')} ts={item.get('pivot_timestamp')} "
            f"price={item.get('price')} confirmed={item.get('confirmation_timestamp')}"
        )
    lines.append("")
    lines.append("Confirmed divergences:")
    confirmed = [
        d
        for d in (payload.get("confirmed_divergences") or [])
        if d.get("status") in {"confirmed_bearish_divergence", "confirmed_bullish_divergence"}
    ]
    if not confirmed:
        lines.append("  none")
    for item in confirmed:
        lines.append(
            f"  {item.get('status')} indicator={item.get('indicator')} "
            f"price_d={_fmt(item.get('price_change'))} "
            f"ind_d={_fmt(item.get('indicator_change'))}"
        )
    lines.append("")
    lines.append("Weakening signals (not confirmed divergences):")
    weak = payload.get("weakening_signals") or []
    if not weak:
        lines.append("  none")
    for item in weak[:30]:
        lines.append(
            f"  {item.get('metric')} lb={item.get('lookback')} "
            f"cur={_fmt(item.get('current'))} prev={_fmt(item.get('previous'))}"
        )
    if len(weak) > 30:
        lines.append(f"  ... {len(weak) - 30} more")

    lines.append("")
    lines.append("History:")
    hist = payload.get("history") or []
    if hist:
        header = (
            f"{'off':>4} {'timestamp':<25} {'close':>8} {'ema20':>8} "
            f"{'c/e20%':>8} {'atr%':>7} {'+DI':>7} {'-DI':>7} {'ADX':>7}"
        )
        lines.append(header)
        for row in hist:
            lines.append(
                f"{row.get('offset_candles'):>4} {str(row.get('timestamp')):<25} "
                f"{_fmt(row.get('close')):>8} {_fmt(row.get('ema_20')):>8} "
                f"{_fmt(row.get('close_vs_ema_20_pct')):>8} {_fmt(row.get('atr_pct')):>7} "
                f"{_fmt(row.get('plus_di')):>7} {_fmt(row.get('minus_di')):>7} "
                f"{_fmt(row.get('adx')):>7}"
            )
    else:
        lines.append("  none")

    summary = payload.get("summary") or {}
    lines.extend(
        [
            "",
            "Descriptive summary:",
            f"  EMA orientation:              {summary.get('ema_orientation')}",
            f"  Short-term slope direction:   {summary.get('short_term_slope_direction')}",
            f"  Short-term slope change:      {summary.get('short_term_slope_change')}",
            f"  Medium-term slope direction:  {summary.get('medium_term_slope_direction')}",
            f"  Long-term slope direction:    {summary.get('long_term_slope_direction')}",
            f"  ADX change 3/6/12:            {summary.get('adx_change')}",
            f"  DI-spread change 3/6/12:      {summary.get('di_spread_change')}",
            f"  Confirmed pivot highs/lows:   "
            f"{summary.get('confirmed_pivot_high_count')}/"
            f"{summary.get('confirmed_pivot_low_count')}",
            "",
            "Confirmed divergences:",
        ]
    )
    conf_divs = summary.get("confirmed_divergences") or []
    if not conf_divs:
        lines.append("  none")
    for item in conf_divs:
        lines.append(
            f"  - {item.get('status')} indicator={item.get('indicator')} "
            f"age={item.get('age_candles')}c/{item.get('age_minutes')}m"
        )

    lines.append("Current last-bar weakening:")
    last_bar_weak = summary.get("current_last_bar_weakening") or []
    if not last_bar_weak:
        lines.append("  none")
    for item in last_bar_weak:
        lines.append(
            f"  - {item.get('metric')}: cur={_fmt(item.get('current'))} "
            f"prev={_fmt(item.get('previous'))} d={_fmt(item.get('change'))}"
        )

    lines.append("Medium-term trend:")
    medium_notes = summary.get("medium_term_trend_notes") or []
    if not medium_notes:
        lines.append("  (no medium-term notes)")
    for note in medium_notes:
        lines.append(f"  - {note}")

    lines.append("")
    lines.append("Last-bar changes (delta_1 / trend_12):")
    for key, item in (payload.get("last_bar_changes") or {}).items():
        lines.append(
            f"  {key}: cur={_fmt(item.get('current'))} "
            f"d1={_fmt(item.get('delta_1'))} ({item.get('direction_1')}) "
            f"trend12={item.get('trend_12')}"
        )

    lines.append("")
    lines.append("Last 12 closed candles:")
    table = payload.get("last_closed_table") or []
    if table:
        lines.append(
            f"{'timestamp':<25} {'close':>8} {'ATR%':>7} {'ADX':>7} {'+DI':>7} "
            f"{'-DI':>7} {'DIsp':>7} {'ADXd1':>8} {'+DId1':>8} {'ATRd1':>8}"
        )
        for row in table:
            lines.append(
                f"{str(row.get('timestamp')):<25} {_fmt(row.get('close')):>8} "
                f"{_fmt(row.get('atr_pct')):>7} {_fmt(row.get('adx')):>7} "
                f"{_fmt(row.get('plus_di')):>7} {_fmt(row.get('minus_di')):>7} "
                f"{_fmt(row.get('di_spread')):>7} {_fmt(row.get('adx_delta1')):>8} "
                f"{_fmt(row.get('plus_di_delta1')):>8} {_fmt(row.get('atr_pct_delta1')):>8}"
            )
    else:
        lines.append("  none")

    lines.extend(
        [
            "",
            f"  Disclaimer:                   {summary.get('disclaimer')}",
            "",
        ]
    )

    classification = payload.get("classification") or {}
    lines.extend(
        [
            "REGIME CLASSIFICATION",
            "---------------------",
            f"Regime:                 {classification.get('regime')}",
            f"Confidence:             {classification.get('confidence')}",
            f"Trend strength label:   {classification.get('trend_strength_label')}",
            f"Acceleration label:     {classification.get('acceleration_label')}",
            "",
            "ENTRY RISK",
            "----------",
            f"Long entry risk:        {classification.get('long_entry_risk')} "
            f"(score={_fmt(classification.get('long_entry_risk_score'))})",
            f"Short entry risk:       {classification.get('short_entry_risk')} "
            f"(score={_fmt(classification.get('short_entry_risk_score'))})",
            "",
            "SCORE BREAKDOWN",
            "---------------",
        ]
    )
    for name, bundle in (classification.get("scores") or {}).items():
        if isinstance(bundle, dict):
            lines.append(f"  {name}: {bundle.get('score')}")
            for comp, value in (bundle.get("components") or {}).items():
                lines.append(f"    - {comp}: {value}")
        else:
            lines.append(f"  {name}: {bundle}")

    lines.extend(["", "REASON CODES", "------------"])
    for item in classification.get("reason_codes") or []:
        lines.append(f"  {item.get('code')}: {item.get('explanation')}")

    lines.extend(["", "Primary reasons:"])
    for reason in classification.get("primary_reasons") or []:
        lines.append(f"  - {reason}")
    if classification.get("notes"):
        lines.append(f"  Note: {(classification.get('notes') or {}).get('separation')}")

    lines.extend(
        [
            "",
            "Open interest:       not available / not loaded in version 1 "
            f"({payload['open_interest'].get('note')})",
        ]
    )
    return "\n".join(lines)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Causal point-in-time audit for the research regime scanner.",
    )
    parser.add_argument("--symbol", default="APTUSDT", help="Exchange symbol, e.g. APTUSDT")
    parser.add_argument(
        "--decision-time",
        required=True,
        help="UTC decision timestamp (candle-open semantics), e.g. 2026-01-13T23:00:00+00:00",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Optional override for the Bybit futures feather directory",
    )
    parser.add_argument(
        "--history-candles",
        type=int,
        default=144,
        help="Maximum history lookback in closed candles for the compact table",
    )
    parser.add_argument(
        "--timeframes",
        default="5m",
        help="Comma-separated timeframes aggregated from 5m, e.g. 5m,15m,30m",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON (NaN/Inf -> null)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    payload = build_point_audit(
        symbol=args.symbol,
        decision_time=args.decision_time,
        data_dir=args.data_dir,
        history_candles=args.history_candles,
        timeframes=args.timeframes,
    )
    if args.json:
        print(json.dumps(json_safe(payload), indent=2, allow_nan=False))
    else:
        print(format_human_report(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
