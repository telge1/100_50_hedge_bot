"""Footprint + AVR context via dashboard pure functions (no formula fork)."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from ..avr.provenance import ensure_avr_import_path
from ..avr_multiscale.candles_5m import build_candle_at
from ..avr_multiscale.footprint import floor_to_5m, footprint_window_from_series
from ..timeparse import format_utc_z
from . import AVR_WINDOWS_S, PRE_FOCUS_WINDOWS_S, TIMELINE_HALF_S


def _ch_client():
    from research_charts.clickhouse_config import load_clickhouse_config
    import clickhouse_connect

    return clickhouse_connect.get_client(**load_clickhouse_config().connect_kwargs())


def load_second_series(
    *,
    symbol: str,
    start_unix: int,
    end_unix: int,
) -> tuple[Any, list[Any], dict[str, Any]]:
    ensure_avr_import_path()
    from footprint_candles.response_engine import SecondSeries
    from footprint_candles.response_service import fetch_second_buckets

    client = _ch_client()
    try:
        buckets = fetch_second_buckets(client, symbol.upper(), int(start_unix), int(end_unix))
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    series = SecondSeries(buckets)
    meta = {
        "n_buckets": len(buckets),
        "load_start_unix": int(start_unix),
        "load_end_unix": int(end_unix),
        "source": "orderbook_analysis.public_trades_canonical via footprint_candles.response_service",
        "empty_second_policy": "NOT_AUTOMATICALLY_SOURCE_GAP",
    }
    return series, buckets, meta


def build_avr_1s(
    *,
    symbol: str,
    series: Any,
    start_unix: int,
    end_unix: int,
) -> pd.DataFrame:
    """AVR states for available_at in (start_unix, end_unix] using dashboard classify."""
    ensure_avr_import_path()
    from footprint_candles.response_baseline import build_baseline_from_feature_rows
    from footprint_candles.response_contracts import (
        BASELINE_LOOKBACK_S,
        DEFAULT_THRESHOLDS,
        PRIMARY_WINDOW_S,
        RESPONSE_ENGINE_VERSION,
        config_hash,
    )
    from footprint_candles.response_engine import (
        classify_features,
        collect_baseline_rows,
    )
    from footprint_candles.response_service import precompute_primary_features

    thr = DEFAULT_THRESHOLDS
    load_start = int(start_unix) - int(BASELINE_LOOKBACK_S) - int(PRIMARY_WINDOW_S)
    feats_map = precompute_primary_features(series, load_start, int(end_unix), thresholds=thr)
    rows: list[dict[str, Any]] = []
    for state_ts in range(int(start_unix), int(end_unix)):
        available_at = state_ts + 1
        feats = feats_map.get(available_at)
        quality_flags: list[str] = []
        if feats is None:
            state = "INSUFFICIENT_DATA"
            confirmation = None
            strength = 0.0
            quality_flags.append("NO_FEATURES")
            direction = "UNCLEAR"
        else:
            bl_rows, valid_secs = collect_baseline_rows(
                series,
                available_at,
                lookback_s=BASELINE_LOOKBACK_S,
                window_s=int(thr.primary_window_s),
                thresholds=thr,
                precomputed_feats=feats_map,
            )
            baseline = build_baseline_from_feature_rows(bl_rows, valid_second_buckets=valid_secs)
            if not baseline.sufficient:
                quality_flags.append("INSUFFICIENT_BASELINE")
            cl = classify_features(feats, baseline, thr)
            state = str(cl.get("state"))
            confirmation = cl.get("confirmation")
            strength = float(cl.get("strength") or 0.0)
            direction = _dir(state)
        rows.append(
            {
                "symbol": symbol.upper(),
                "state_ts": datetime.fromtimestamp(state_ts, tz=timezone.utc),
                "state_ts_unix": state_ts,
                "available_at": datetime.fromtimestamp(available_at, tz=timezone.utc),
                "available_at_unix": available_at,
                "avr_state": state,
                "avr_direction": direction,
                "avr_confirmation": confirmation,
                "avr_strength": strength,
                "avr_config_hash": config_hash(thr),
                "avr_source_contract_version": RESPONSE_ENGINE_VERSION,
                "quality_flags": quality_flags,
            }
        )
    return pd.DataFrame(rows)


def _dir(state: str) -> str:
    s = (state or "").upper()
    if "BUY" in s or s.endswith("_UP") or "VACUUM_UP" in s:
        return "BULLISH"
    if "SELL" in s or s.endswith("_DOWN") or "VACUUM_DOWN" in s:
        return "BEARISH"
    return "UNCLEAR"


def pre_focus_footprint_rows(series: Any, *, focus_unix: int) -> list[dict[str, Any]]:
    rows = []
    for w in PRE_FOCUS_WINDOWS_S:
        fp = footprint_window_from_series(series, available_at=focus_unix, window_s=w, prefix="fp")
        # largest 1s delta in window
        max_delta = None
        max_delta_ts = None
        for sec in range(focus_unix - w, focus_unix):
            feats = series.features_at(sec + 1, 1, min_valid_frac=0.0)
            if feats is None:
                continue
            d = float(feats.get("buy_notional") or 0) - float(feats.get("sell_notional") or 0)
            if max_delta is None or abs(d) > abs(max_delta):
                max_delta = d
                max_delta_ts = sec
        # acceleration: delta(w/2 recent) - delta(w/2 earlier) if w>=30
        accel = None
        if w >= 30:
            half = w // 2
            f1 = series.features_at(focus_unix - half, half, min_valid_frac=0.0)
            f2 = series.features_at(focus_unix, half, min_valid_frac=0.0)
            if f1 and f2:
                d1 = float(f1.get("buy_notional") or 0) - float(f1.get("sell_notional") or 0)
                d2 = float(f2.get("buy_notional") or 0) - float(f2.get("sell_notional") or 0)
                accel = d2 - d1
        row = {
            "available_at_unix": focus_unix,
            "available_at": format_utc_z(datetime.fromtimestamp(focus_unix, tz=timezone.utc)),
            **fp,
            "fp_max_1s_delta_notional": max_delta,
            "fp_max_1s_delta_state_ts_unix": max_delta_ts,
            "fp_delta_acceleration": accel,
        }
        rows.append(row)
    return rows


def five_m_context(series: Any, *, symbol: str, focus_unix: int) -> dict[str, Any]:
    """Partial current 5m ending at focus; previous/prev2 closed fully before focus."""
    cur_start = floor_to_5m(focus_unix)
    # current partial: [cur_start, focus_unix)
    current = build_candle_at(
        symbol=symbol,
        series=series,
        avr_1s=None,
        state_by_ts={},
        candle_start=cur_start,
        candle_end=focus_unix,
        is_complete=False,
        is_partial=True,
    )
    closed_end = floor_to_5m(focus_unix)  # equals cur_start
    prev = None
    prev2 = None
    if closed_end == focus_unix:
        # focus exactly on 5m boundary → previous closed is [focus-300, focus)
        prev_start = focus_unix - 300
        prev = build_candle_at(
            symbol=symbol,
            series=series,
            avr_1s=None,
            state_by_ts={},
            candle_start=prev_start,
            candle_end=focus_unix,
            is_complete=True,
            is_partial=False,
        )
        prev2 = build_candle_at(
            symbol=symbol,
            series=series,
            avr_1s=None,
            state_by_ts={},
            candle_start=prev_start - 300,
            candle_end=prev_start,
            is_complete=True,
            is_partial=False,
        )
    else:
        # previous closed ends at cur_start
        prev = build_candle_at(
            symbol=symbol,
            series=series,
            avr_1s=None,
            state_by_ts={},
            candle_start=cur_start - 300,
            candle_end=cur_start,
            is_complete=True,
            is_partial=False,
        )
        prev2 = build_candle_at(
            symbol=symbol,
            series=series,
            avr_1s=None,
            state_by_ts={},
            candle_start=cur_start - 600,
            candle_end=cur_start - 300,
            is_complete=True,
            is_partial=False,
        )

    def _ser(c: dict[str, Any] | None) -> dict[str, Any] | None:
        if c is None:
            return None
        out = {}
        for k, v in c.items():
            if isinstance(v, datetime):
                out[k] = format_utc_z(v)
            elif isinstance(v, list):
                out[k] = v
            else:
                out[k] = v
        # causality asserts
        out["candle_end_unix"] = int(c["candle_end_unix"])
        out["ends_at_or_before_focus"] = int(c["candle_end_unix"]) <= focus_unix
        return out

    return {
        "current_partial_5m": _ser(current),
        "previous_closed_5m": _ser(prev),
        "previous2_closed_5m": _ser(prev2),
        "focus_unix": focus_unix,
        "contract": "no_complete_5m_with_end_after_focus",
    }


def avr_window_summary(avr_df: pd.DataFrame, *, focus_unix: int, window_s: int) -> dict[str, Any]:
    from ..avr_multiscale.config import map_avr_state

    lo = focus_unix - window_s
    # states with available_at in (lo, focus] i.e. available_at_unix <= focus and > lo
    if avr_df is None or avr_df.empty:
        return {"window_s": window_s, "n": 0, "majority_state": "INSUFFICIENT_DATA"}
    sub = avr_df[(avr_df["available_at_unix"] > lo) & (avr_df["available_at_unix"] <= focus_unix)]
    if sub.empty:
        return {"window_s": window_s, "n": 0, "majority_state": "INSUFFICIENT_DATA"}
    states = [map_avr_state(s) for s in sub["avr_state"].astype(str).tolist()]
    raw_states = sub["avr_state"].astype(str).tolist()
    counts: dict[str, int] = {}
    for s in states:
        counts[s] = counts.get(s, 0) + 1
    n = len(states)
    shares = {k: v / n for k, v in counts.items()}

    def share_prefix(*keys: str) -> float:
        return sum(shares.get(k, 0.0) for k in keys)

    # longest run on mapped states
    longest = 1
    longest_state = states[0]
    cur = 1
    for i in range(1, len(states)):
        if states[i] == states[i - 1]:
            cur += 1
            if cur > longest:
                longest = cur
                longest_state = states[i]
        else:
            cur = 1
    switches = sum(1 for i in range(1, len(states)) if states[i] != states[i - 1])
    clear = [
        s
        for s in states
        if s
        not in {"UNCLEAR", "INSUFFICIENT_DATA", "INSUFFICIENT_BASELINE"}
    ]
    majority = max(counts.items(), key=lambda kv: kv[1])[0]
    return {
        "window_s": window_s,
        "n": n,
        "state_shares": shares,
        "buy_control_share": share_prefix("BUY_CONTROL"),
        "sell_control_share": share_prefix("SELL_CONTROL"),
        "buy_absorption_share": share_prefix("BUY_ABSORPTION"),
        "sell_absorption_share": share_prefix("SELL_ABSORPTION"),
        "vacuum_up_share": share_prefix("VACUUM_UP"),
        "vacuum_down_share": share_prefix("VACUUM_DOWN"),
        "unclear_insufficient_share": share_prefix("UNCLEAR", "INSUFFICIENT_DATA", "INSUFFICIENT_BASELINE"),
        "longest_run_state": longest_state,
        "longest_run_len": longest,
        "n_switches": switches,
        "earliest_clear_state": clear[0] if clear else None,
        "latest_clear_state": clear[-1] if clear else None,
        "majority_state": majority,
        "raw_state_sample": Counter(raw_states).most_common(5),
        "contradiction_note": (
            "MIXED"
            if ("BUY" in majority and any(s.startswith("SELL") for s in clear))
            or ("SELL" in majority and any(s.startswith("BUY") for s in clear))
            else "NONE"
        ),
    }


def footprint_5s_timeline(series: Any, *, focus_unix: int) -> pd.DataFrame:
    rows = []
    for t in range(focus_unix - TIMELINE_HALF_S, focus_unix + TIMELINE_HALF_S, 5):
        end = min(t + 5, focus_unix + TIMELINE_HALF_S)
        # for post-focus buckets still compute but mark side
        feats = series.features_at(end, end - t, min_valid_frac=0.0) if end > t else None
        buy = float(feats.get("buy_notional") or 0) if feats else 0.0
        sell = float(feats.get("sell_notional") or 0) if feats else 0.0
        rows.append(
            {
                "bucket_start_unix": t,
                "bucket_end_unix": end,
                "bucket_start": format_utc_z(datetime.fromtimestamp(t, tz=timezone.utc)),
                "relative_to_focus": "PRE" if end <= focus_unix else ("STRADDLE" if t < focus_unix < end else "POST"),
                "buy_notional": buy,
                "sell_notional": sell,
                "delta_notional": buy - sell,
                "open": None if not feats else feats.get("first_price"),
                "high": None if not feats else feats.get("high_price"),
                "low": None if not feats else feats.get("low_price"),
                "close": None if not feats else feats.get("last_price"),
            }
        )
    return pd.DataFrame(rows)


def price_at_second(series: Any, unix_s: int) -> float | None:
    """Last trade price available strictly before unix_s+1 i.e. within second unix_s or prior."""
    # features_at(available_at=unix_s, window=1) uses [unix_s-1, unix_s)
    feats = series.features_at(unix_s, 1, min_valid_frac=0.0)
    if feats and feats.get("last_price") is not None:
        return float(feats["last_price"])
    # walk back a few seconds
    for back in range(1, 30):
        f = series.features_at(unix_s - back + 1, 1, min_valid_frac=0.0)
        if f and f.get("last_price") is not None:
            return float(f["last_price"])
    return None
