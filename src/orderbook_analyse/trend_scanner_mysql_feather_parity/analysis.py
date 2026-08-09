"""Run APT MySQL↔Feather parity smoke (scanner unchanged)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.trend_scanner_mysql_feather_parity import AUDIT_VERSION
from orderbook_analyse.trend_scanner_mysql_feather_parity.compare import (
    causality_checks,
    compare_ohlcv,
    events_to_frame,
    match_break_events,
)
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import (
    DEFAULT_ENV_FILE,
    DEFAULT_FEATHER_DIR,
    clip_ohlcv,
    comparison_window,
    load_feather_5m_ohlcv,
    load_mysql_5m_ohlcv,
    mysql_quality_checks,
)
from orderbook_analyse.trend_scanner_multitimeframe import (
    DEFAULT_SCANNER_ROOT,
    aggregate_ohlcv_from_5m,
    enumerate_structure_breaks,
    run_structure_for_timeframe,
)


def _collect_breaks(structure: pd.DataFrame, *, drop_warmup: bool) -> list[dict[str, Any]]:
    work = structure
    if drop_warmup and "in_warmup" in work.columns:
        work = work.loc[~work["in_warmup"].astype(bool)].copy()
    events: list[dict[str, Any]] = []
    for side in ("high", "low"):
        ev, _stats = enumerate_structure_breaks(work, side=side, require_choch=True)
        events.extend(ev)
    return events


def run_parity_smoke(
    *,
    symbol: str = "APTUSDT",
    warmup_bars: int = 72,
    drop_warmup_events: bool = True,
    candle_dir: Path = DEFAULT_FEATHER_DIR,
    scanner_root: Path = DEFAULT_SCANNER_ROOT,
    env_file: Path = DEFAULT_ENV_FILE,
) -> dict[str, Any]:
    mysql_raw = load_mysql_5m_ohlcv(symbol=symbol, env_file=env_file)
    feather_raw = load_feather_5m_ohlcv(candle_dir=candle_dir)
    mysql_q = mysql_quality_checks(mysql_raw)

    win = comparison_window(mysql_raw, feather_raw)
    start, end = win["comparison_start"], win["comparison_end"]

    mysql_5m = clip_ohlcv(mysql_raw, start=start, end=end)
    feather_5m = clip_ohlcv(feather_raw, start=start, end=end)
    # Scanner schema only (drop helper cols)
    need = ["timestamp", "open", "high", "low", "close", "volume"]
    mysql_5m = mysql_5m[need].copy()
    feather_5m = feather_5m[need].copy()

    raw_parity = compare_ohlcv(mysql_5m, feather_5m)
    raw_rows = _flatten_examples(raw_parity, stage="5m")

    if not raw_parity["raw_ok"] and not raw_parity["volume_only_diff"]:
        # Hard stop on OHLC / timestamp mismatch
        decision = "PARITY_FAILED_RAW_CANDLES"
        return _package(
            decision=decision,
            win=win,
            mysql_q=mysql_q,
            raw_parity=raw_parity,
            raw_rows=raw_rows,
            extras={"stop_reason": "raw_5m_ohlc_or_timestamp_mismatch"},
        )

    mysql_1h = aggregate_ohlcv_from_5m(mysql_5m, "1h", require_complete=True)
    feather_1h = aggregate_ohlcv_from_5m(feather_5m, "1h", require_complete=True)
    mysql_4h = aggregate_ohlcv_from_5m(mysql_5m, "4h", require_complete=True)
    feather_4h = aggregate_ohlcv_from_5m(feather_5m, "4h", require_complete=True)

    resample_1h = compare_ohlcv(
        mysql_1h[need], feather_1h[need], left_name="mysql", right_name="feather"
    )
    resample_4h = compare_ohlcv(
        mysql_4h[need], feather_4h[need], left_name="mysql", right_name="feather"
    )
    r1_rows = _flatten_examples(resample_1h, stage="1h")
    r4_rows = _flatten_examples(resample_4h, stage="4h")

    if (not resample_1h["raw_ok"] and not resample_1h["volume_only_diff"]) or (
        not resample_4h["raw_ok"] and not resample_4h["volume_only_diff"]
    ):
        decision = "PARITY_FAILED_RESAMPLE"
        return _package(
            decision=decision,
            win=win,
            mysql_q=mysql_q,
            raw_parity=raw_parity,
            raw_rows=raw_rows,
            resample_1h=resample_1h,
            resample_4h=resample_4h,
            r1_rows=r1_rows,
            r4_rows=r4_rows,
            extras={"stop_reason": "htf_ohlc_or_timestamp_mismatch"},
        )

    # Same scanner path as origin_fix (1h/4h, warmup=72, protected_medium via run_c34b)
    m_s1 = run_structure_for_timeframe(
        mysql_1h, timeframe="1h", scanner_root=scanner_root, symbol=symbol, warmup_bars=warmup_bars
    )
    f_s1 = run_structure_for_timeframe(
        feather_1h, timeframe="1h", scanner_root=scanner_root, symbol=symbol, warmup_bars=warmup_bars
    )
    m_s4 = run_structure_for_timeframe(
        mysql_4h, timeframe="4h", scanner_root=scanner_root, symbol=symbol, warmup_bars=warmup_bars
    )
    f_s4 = run_structure_for_timeframe(
        feather_4h, timeframe="4h", scanner_root=scanner_root, symbol=symbol, warmup_bars=warmup_bars
    )

    m_events = _collect_breaks(m_s1, drop_warmup=drop_warmup_events) + _collect_breaks(
        m_s4, drop_warmup=drop_warmup_events
    )
    f_events = _collect_breaks(f_s1, drop_warmup=drop_warmup_events) + _collect_breaks(
        f_s4, drop_warmup=drop_warmup_events
    )
    mysql_ev_df = events_to_frame(m_events, source="mysql")
    feather_ev_df = events_to_frame(f_events, source="feather")
    parity_df, parity_stats = match_break_events(mysql_ev_df, feather_ev_df)
    # Enrich counts from source frames (clearer than status inference)
    by: dict[str, Any] = {}
    for tf in ("1h", "4h"):
        for side in ("PH_break", "PL_break"):
            nm = int(
                ((mysql_ev_df.get("timeframe") == tf) & (mysql_ev_df.get("side") == side)).sum()
            ) if not mysql_ev_df.empty else 0
            nf = int(
                ((feather_ev_df.get("timeframe") == tf) & (feather_ev_df.get("side") == side)).sum()
            ) if not feather_ev_df.empty else 0
            sub = parity_df[
                (parity_df["timeframe"] == tf) & (parity_df["side"] == side)
            ] if not parity_df.empty else parity_df
            by[f"{tf}|{side}"] = {
                "mysql": nm,
                "feather": nf,
                "exact": int((sub["status"] == "EXACT_MATCH").sum()) if len(sub) else 0,
                "mysql_only": int((sub["status"] == "MYSQL_ONLY").sum()) if len(sub) else 0,
                "feather_only": int((sub["status"] == "FEATHER_ONLY").sum()) if len(sub) else 0,
                "level_mismatch": int((sub["status"] == "LEVEL_MISMATCH").sum()) if len(sub) else 0,
            }
    parity_stats = {**parity_stats, "by_tf_side": by}

    causal = causality_checks(m_s1, m_s4, mysql_1h, mysql_4h, mysql_ev_df)

    only_m = parity_stats["counts"]["MYSQL_ONLY"]
    only_f = parity_stats["counts"]["FEATHER_ONLY"]
    level_mm = parity_stats["counts"]["LEVEL_MISMATCH"]
    exact = parity_stats["counts"]["EXACT_MATCH"]

    volume_diff = (
        int(raw_parity.get("volume_mismatch") or 0) > 0
        or int(resample_1h.get("volume_mismatch") or 0) > 0
        or int(resample_4h.get("volume_mismatch") or 0) > 0
    )

    if only_m or only_f or level_mm:
        decision = "PARITY_FAILED_SCANNER"
    elif volume_diff:
        decision = "PARITY_GREEN_WITH_MINOR_SOURCE_DIFFERENCES"
    else:
        decision = "PARITY_GREEN"

    # unused but keep for summary clarity
    _ = exact

    return _package(
        decision=decision,
        win=win,
        mysql_q=mysql_q,
        raw_parity=raw_parity,
        raw_rows=raw_rows,
        resample_1h=resample_1h,
        resample_4h=resample_4h,
        r1_rows=r1_rows,
        r4_rows=r4_rows,
        mysql_ev_df=mysql_ev_df,
        feather_ev_df=feather_ev_df,
        parity_df=parity_df,
        parity_stats=parity_stats,
        causal=causal,
        extras={
            "symbol": symbol,
            "warmup_bars": warmup_bars,
            "drop_warmup_events": drop_warmup_events,
            "n_mysql_1h": int(len(mysql_1h)),
            "n_feather_1h": int(len(feather_1h)),
            "n_mysql_4h": int(len(mysql_4h)),
            "n_feather_4h": int(len(feather_4h)),
            "scanner_root": str(scanner_root),
            "audit_version": AUDIT_VERSION,
        },
    )


def _flatten_examples(parity: dict[str, Any], *, stage: str) -> list[dict[str, Any]]:
    rows = []
    for ex in parity.get("examples") or []:
        rows.append({"stage": stage, **ex})
    # always emit a summary row for CSV convenience
    rows.insert(
        0,
        {
            "stage": stage,
            "timestamp": None,
            "summary_n_mysql": parity.get("n_mysql"),
            "summary_n_feather": parity.get("n_feather"),
            "summary_n_both": parity.get("n_both"),
            "summary_open_mismatch": parity.get("open_mismatch"),
            "summary_high_mismatch": parity.get("high_mismatch"),
            "summary_low_mismatch": parity.get("low_mismatch"),
            "summary_close_mismatch": parity.get("close_mismatch"),
            "summary_volume_mismatch": parity.get("volume_mismatch"),
            "summary_missing_in_feather": parity.get("missing_in_feather"),
            "summary_missing_in_mysql": parity.get("missing_in_mysql"),
            "summary_raw_ok": parity.get("raw_ok"),
            "summary_volume_only_diff": parity.get("volume_only_diff"),
        },
    )
    return rows


def _package(**kwargs: Any) -> dict[str, Any]:
    return kwargs
