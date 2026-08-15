"""Build A6 signal / feature / outcome bundles from MySQL 5m (no feather fallback)."""

from __future__ import annotations

import hashlib
import math
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.c35c_signal_store.schema import (
    EXIT_MODEL_TP3_SL2,
    SAME_BAR_POLICY,
    SIGNAL_TYPE_A6_FILL,
)
from research.regime_scanner.candle_sources import load_regime_db_env_file
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicator_feature_store import required_indicator_warmup_bars
from research.regime_scanner.indicators import ema
from research.regime_scanner.pullback_entry_c3_5 import (
    apply_pullback_entry,
    config_hash,
    enrich_indicators,
    prepare_research_frame,
)
from research.regime_scanner.pullback_entry_c3_5_diagnostics import baseline_a6
from research.regime_scanner.pullback_entry_c3_5c_entry_path_audit import (
    aggregate_complete_from_5m,
)
from research.regime_scanner.pullback_entry_c3_5c_fill_excursion_audit import (
    COST_ROUNDTRIP_PCT,
    first_touch_level,
    path_arrays,
    signed_return_pct,
)
from research.regime_scanner.pullback_entry_c3_5c_realized_outcome_audit import _filled_sorted
from research.regime_scanner.pullback_entry_c3_5c_robustness_audit import (
    WARMUP_CALENDAR_DAYS,
    assign_split,
    fixed_chrono_splits,
)

EXPECTED_A6_HASH = "aeddc2c9edc5549c0c84d7a2af1e1b08b7f46d23680f58b3ebd08e1ff136b608"
EXPECTED_N_FILLS = 55
ANALYZE_START = "2026-01-26T00:00:00+00:00"
ANALYZE_END_EXCLUSIVE = "2026-06-28T00:00:00+00:00"
TP_PCT = 3.0
SL_PCT = -2.0
HORIZON_BARS = 192
COST_PCT = COST_ROUNDTRIP_PCT


class MySQLRequiredError(RuntimeError):
    pass


def resolve_analyze_window(
    full_5m: pd.DataFrame,
    *,
    analyze_start: str | pd.Timestamp | None = None,
    analyze_end_exclusive: str | pd.Timestamp | None = None,
    warmup_calendar_days: int = WARMUP_CALENDAR_DAYS,
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Match robustness/multicoin auto-window: data_start+warmup → day after last bar."""
    ts = pd.to_datetime(full_5m["timestamp"], utc=True)
    data_start = ts.iloc[0]
    data_end = ts.iloc[-1]
    if analyze_end_exclusive is None:
        a1 = (data_end.floor("D") + pd.Timedelta(days=1)).tz_convert("UTC")
    else:
        a1 = pd.Timestamp(analyze_end_exclusive)
        if a1.tzinfo is None:
            a1 = a1.tz_localize("UTC")
        else:
            a1 = a1.tz_convert("UTC")
        if isinstance(analyze_end_exclusive, str) and len(str(analyze_end_exclusive)) == 10:
            a1 = pd.Timestamp(analyze_end_exclusive, tz="UTC") + pd.Timedelta(days=1)
    if analyze_start is None:
        a0 = data_start + pd.Timedelta(days=int(warmup_calendar_days))
    else:
        a0 = pd.Timestamp(analyze_start)
        if a0.tzinfo is None:
            a0 = a0.tz_localize("UTC")
        else:
            a0 = a0.tz_convert("UTC")
    if a0 >= a1:
        raise ValueError(f"empty analyze window: {a0} >= {a1}")
    return a0, a1


def sha1_ohlcv(frame: pd.DataFrame) -> str:
    ts = pd.to_datetime(frame["timestamp"], utc=True).astype(str)
    blob = (
        ts
        + "|"
        + frame["open"].astype(str)
        + "|"
        + frame["high"].astype(str)
        + "|"
        + frame["low"].astype(str)
        + "|"
        + frame["close"].astype(str)
        + "|"
        + frame["volume"].astype(str)
    )
    return hashlib.sha1("\n".join(blob.tolist()).encode()).hexdigest()


def load_symbol_5m_mysql(symbol: str) -> tuple[pd.DataFrame, dict[str, Any]]:
    load_regime_db_env_file()
    try:
        frame = load_symbol_candles(symbol, data_source="mysql", exchange="bybit")
    except Exception as exc:  # noqa: BLE001
        raise MySQLRequiredError(
            f"MySQL load failed for {symbol}; feather fallback forbidden: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if frame is None or frame.empty:
        raise MySQLRequiredError(f"MySQL returned empty 5m for {symbol}")
    return frame.reset_index(drop=True), {
        "data_source": "mysql",
        "feather_fallback": False,
        "n_5m": int(len(frame)),
        "t0": str(pd.to_datetime(frame["timestamp"], utc=True).iloc[0]),
        "t1": str(pd.to_datetime(frame["timestamp"], utc=True).iloc[-1]),
        "ohlcv_sha1": sha1_ohlcv(frame),
    }


def build_15m_a6(
    full_5m: pd.DataFrame,
    *,
    analyze_start: pd.Timestamp,
    analyze_end_exclusive: pd.Timestamp,
) -> tuple[pd.DataFrame, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    warm_bars = max(required_indicator_warmup_bars(), 400)
    load_start = analyze_start - pd.Timedelta(minutes=5 * warm_bars)
    ts = pd.to_datetime(full_5m["timestamp"], utc=True)
    sliced = full_5m.loc[(ts >= load_start) & (ts < analyze_end_exclusive)].copy().reset_index(drop=True)
    decision = analyze_end_exclusive + pd.Timedelta(hours=1)
    ohlcv15 = aggregate_complete_from_5m(sliced, "15m", decision_time=decision)
    frame = prepare_research_frame(ohlcv15, ohlcv_15m=None, ohlcv_30m=None)
    fts = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.loc[(fts >= analyze_start) & (fts < analyze_end_exclusive)].copy().reset_index(drop=True)
    frame["bar_index"] = np.arange(len(frame))
    # ensure ema59/200 for feature store
    close = pd.to_numeric(frame["close"], errors="coerce").astype("float64")
    if "ema_59" not in frame.columns:
        frame["ema_59"] = ema(close, 59)
    if "ema_200" not in frame.columns:
        frame["ema_200"] = ema(close, 200)
    for p in (59, 200):
        col = f"ema_{p}"
        for w in (1, 2, 3):
            sc = f"ema_{p}_slope_{w}"
            if sc not in frame.columns:
                frame[sc] = frame[col] - frame[col].shift(w)

    cfg = baseline_a6()
    ch = config_hash(cfg)
    if ch != EXPECTED_A6_HASH:
        raise RuntimeError(f"A6 config hash drift: {ch} != {EXPECTED_A6_HASH}")
    _tl, entries, lives = apply_pullback_entry(frame, cfg, return_lifecycles=True)
    fills = _filled_sorted(frame, entries)
    meta = {
        "config_hash": ch,
        "n_15m": int(len(frame)),
        "n_fills": int(len(fills)),
        "warmup_5m_bars": warm_bars,
        "load_start": load_start.isoformat(),
    }
    return frame, fills, lives, meta


def check_fill_parity(
    fills: list[dict[str, Any]],
    reference_panel: pd.DataFrame,
) -> dict[str, Any]:
    ref = reference_panel.copy()
    ref["fill_time"] = pd.to_datetime(ref["fill_time"], utc=True)
    rows = []
    for f in fills:
        rows.append(
            {
                "side": f.get("side_name"),
                "setup_id": f.get("setup_id"),
                "fill_time": pd.Timestamp(f["fill_timestamp"]),
                "fill_price": float(f["entry_price"]),
                "trigger_time": pd.Timestamp(f["trigger_timestamp"])
                if f.get("trigger_timestamp") is not None
                else None,
            }
        )
    got = pd.DataFrame(rows).sort_values("fill_time").reset_index(drop=True)
    ref_s = ref.sort_values("fill_time").reset_index(drop=True)
    n_ok = len(got) == EXPECTED_N_FILLS == len(ref_s)
    time_ok = bool((got["fill_time"] == ref_s["fill_time"]).all()) if n_ok else False
    price_ok = (
        bool(np.allclose(got["fill_price"], ref_s["fill_price"], rtol=0, atol=1e-12)) if n_ok else False
    )
    side_ok = bool((got["side"].astype(str) == ref_s["side"].astype(str)).all()) if n_ok else False
    setup_ok = True
    if n_ok and "setup_id" in ref_s.columns:
        setup_ok = bool(
            (got["setup_id"].astype(str).to_numpy() == ref_s["setup_id"].astype(str).to_numpy()).all()
        )
    trig_ok = True
    if n_ok and "trigger_time" in ref_s.columns:
        ref_trig = pd.to_datetime(ref_s["trigger_time"], utc=True)
        trig_ok = bool((got["trigger_time"] == ref_trig).all())
    ok = bool(n_ok and time_ok and price_ok and side_ok and setup_ok and trig_ok)
    parity_rows = []
    if n_ok:
        for i in range(len(got)):
            parity_rows.append(
                {
                    "i": i,
                    "side_match": got.loc[i, "side"] == ref_s.loc[i, "side"],
                    "fill_time_match": got.loc[i, "fill_time"] == ref_s.loc[i, "fill_time"],
                    "fill_price_match": abs(float(got.loc[i, "fill_price"]) - float(ref_s.loc[i, "fill_price"]))
                    < 1e-12,
                    "setup_id_got": got.loc[i, "setup_id"],
                    "setup_id_ref": ref_s.loc[i, "setup_id"] if "setup_id" in ref_s.columns else None,
                    "fill_time": str(got.loc[i, "fill_time"]),
                    "fill_price": float(got.loc[i, "fill_price"]),
                }
            )
    return {
        "ok": ok,
        "n_fills": int(len(got)),
        "expected": EXPECTED_N_FILLS,
        "time_ok": time_ok,
        "price_ok": price_ok,
        "side_ok": side_ok,
        "setup_ok": setup_ok,
        "trigger_ok": trig_ok,
        "rows": parity_rows,
    }


def _finite(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def _candle_structure(o: float, h: float, l: float, c: float) -> dict[str, Any]:
    rng = h - l
    body = abs(c - o)
    upper = h - max(o, c)
    lower = min(o, c) - l
    return {
        "entry_candle_return_pct": ((c / o) - 1.0) * 100.0 if o else None,
        "entry_candle_body_pct": (body / o) * 100.0 if o else None,
        "entry_candle_range_pct": (rng / o) * 100.0 if o else None,
        "entry_upper_wick_ratio": (upper / rng) if rng > 0 else None,
        "entry_lower_wick_ratio": (lower / rng) if rng > 0 else None,
        "entry_close_position": ((c - l) / rng) if rng > 0 else None,
        "entry_bullish": bool(c >= o),
    }


def _life_by_setup(lives: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for life in lives:
        sid = life.get("setup_id")
        if sid is None:
            continue
        out[int(sid)] = life
    return out


def build_signal_rows(
    fills: list[dict[str, Any]],
    lives: list[dict[str, Any]],
    frame: pd.DataFrame,
    *,
    splits: dict[str, Any],
    symbol: str = "APTUSDT",
) -> list[dict[str, Any]]:
    life_map = _life_by_setup(lives)
    ts = list(pd.to_datetime(frame["timestamp"], utc=True))
    rows = []
    for f in fills:
        sid = int(f.get("setup_id") or 0)
        life = life_map.get(sid, {})
        fill_i = int(f["fill_bar"])
        trig_i = int(f["trigger_bar"])
        side = str(f["side_name"])
        fill_ts = pd.Timestamp(f["fill_timestamp"])
        trig_ts = pd.Timestamp(f["trigger_timestamp"])
        arm_bar = life.get("armed_bar") or life.get("arm_bar")
        pb_bar = life.get("pullback_bar")
        ready_bar = life.get("ready_bar") or life.get("rejection_bar")
        opp_bar = life.get("opposite_arm_bar")
        split_raw = assign_split(fill_ts, splits)
        split = {"development": "dev", "validation": "validation", "oos": "oos"}.get(
            split_raw, split_raw
        )
        signal_key = f"a6|{symbol.upper()}|{side}|{fill_ts.isoformat()}|{sid}"
        meta = {
            "symbol": symbol.upper(),
            "arm_timestamp": None if arm_bar is None else str(ts[int(arm_bar)]),
            "pullback_timestamp": None if pb_bar is None else str(ts[int(pb_bar)]),
            "ready_timestamp": None if ready_bar is None else str(ts[int(ready_bar)]),
            "trigger_timestamp": str(trig_ts),
            "fill_timestamp": str(fill_ts),
            "arm_bar": arm_bar,
            "pullback_bar": pb_bar,
            "ready_bar": ready_bar,
            "trigger_bar": trig_i,
            "fill_bar": fill_i,
            "setup_age": life.get("setup_age_total") or f.get("setup_age"),
            "ready_age": life.get("ready_age_at_terminal") or f.get("ready_age"),
            "bars_arm_to_trigger": None if arm_bar is None else int(trig_i - int(arm_bar)),
            "bars_ready_to_trigger": None if ready_bar is None else int(trig_i - int(ready_bar)),
            "bars_pullback_to_trigger": None if pb_bar is None else int(trig_i - int(pb_bar)),
            "opposite_arm_seen": bool(life.get("opposite_arm_seen")),
            "opposite_arm_type": life.get("opposite_arm_type"),
            "opposite_arm_bar": opp_bar,
            "opposite_arm_age": None if opp_bar is None else int(trig_i - int(opp_bar)),
            "opposite_arm_since_ready": (
                None
                if opp_bar is None or ready_bar is None
                else bool(int(ready_bar) <= int(opp_bar) <= trig_i)
            ),
            "opposite_arm_on_trigger_bar": bool(opp_bar is not None and int(opp_bar) == trig_i),
            "arming_type": life.get("arming_type"),
            "entry_reason": f.get("entry_reason") or life.get("entry_reason"),
            "split": split,
            "side_sign": int(f["side"]),
        }
        rows.append(
            {
                "signal_key": signal_key,
                "timestamp": trig_ts,
                "direction": side,
                "signal_type": SIGNAL_TYPE_A6_FILL,
                "setup_id": sid,
                "status": "filled",
                "entry_time": fill_ts,
                "entry_price": float(f["entry_price"]),
                "invalidation_time": None,
                "invalidation_price": None,
                "reason": meta.get("entry_reason"),
                "metadata_json": meta,
                "_fill_bar": fill_i,
                "_trigger_bar": trig_i,
                "_side_sign": int(f["side"]),
                "_life": life,
                "_split": split,
                "_symbol": symbol.upper(),
            }
        )
    return rows


def _row_indicators(row: pd.Series, *, side_sign: int) -> dict[str, Any]:
    e9 = _finite(row.get("ema_9"))
    e20 = _finite(row.get("ema_20"))
    e50 = _finite(row.get("ema_50"))
    e59 = _finite(row.get("ema_59"))
    e200 = _finite(row.get("ema_200"))
    adx = _finite(row.get("adx"))
    pdi = _finite(row.get("plus_di"))
    mdi = _finite(row.get("minus_di"))
    atr = _finite(row.get("atr_14") if pd.notna(row.get("atr_14")) else row.get("atr"))
    close = _finite(row.get("close"))
    di_spread = None if pdi is None or mdi is None else pdi - mdi
    di_abs = None if di_spread is None else abs(di_spread)
    # direction-normalized: positive when DI favors trade side
    di_dir = None
    if di_spread is not None:
        di_dir = di_spread if side_sign > 0 else -di_spread
    dist_ema_atr = None
    if close is not None and e20 is not None and atr and atr > 0:
        dist_ema_atr = abs(close - e20) / atr

    def _pct(a: float | None, b: float | None) -> float | None:
        if a is None or b is None or b == 0:
            return None
        return (a / b - 1.0) * 100.0

    return {
        "ema9": e9,
        "ema20": e20,
        "ema50": e50,
        "ema59": e59,
        "ema200": e200,
        "ema9_slope_3": _finite(row.get("ema_9_slope_3")),
        "ema20_slope_3": _finite(row.get("ema_20_slope_3")),
        "ema59_slope_3": _finite(row.get("ema_59_slope_3")),
        "ema200_slope_3": _finite(row.get("ema_200_slope_3")),
        "ema9_20_distance_pct": _pct(e9, e20),
        "ema20_59_distance_pct": _pct(e20, e59),
        "ema59_200_distance_pct": _pct(e59, e200),
        "adx": adx,
        "di_plus": pdi,
        "di_minus": mdi,
        "di_spread_signed": di_spread,
        "di_spread_abs": di_abs,
        "di_spread_dir_norm": di_dir,
        "atr": atr,
        "atr_pct": None if atr is None or close in (None, 0) else (atr / close) * 100.0,
        "dist_ema_atr": dist_ema_atr,
        "major_direction": None
        if row.get("major_direction") is None or (isinstance(row.get("major_direction"), float) and math.isnan(row.get("major_direction")))
        else int(row.get("major_direction")),
        "structure_state": row.get("protected_structure_state") or row.get("structure_state"),
        "protected_high": _finite(row.get("protected_high")),
        "protected_low": _finite(row.get("protected_low")),
    }


def build_feature_rows(
    frame: pd.DataFrame,
    signal_rows: list[dict[str, Any]],
    *,
    feature_version: str,
) -> list[dict[str, Any]]:
    """Two stages: trigger (closed bar) and fill (open-known only)."""
    vol = pd.to_numeric(frame.get("volume"), errors="coerce").astype(float)
    vol_med = vol.rolling(20, min_periods=5).median()
    out: list[dict[str, Any]] = []
    for sig in signal_rows:
        trig_i = int(sig["_trigger_bar"])
        fill_i = int(sig["_fill_bar"])
        side_sign = int(sig["_side_sign"])
        life = sig.get("_life") or {}
        arm_bar = life.get("armed_bar") or life.get("arm_bar")
        trig = frame.iloc[trig_i]
        fill = frame.iloc[fill_i]
        base_ind = _row_indicators(trig, side_sign=side_sign)

        # setup / structure extras from trigger bar
        atr = base_ind.get("atr")
        o, h, l, c = float(trig["open"]), float(trig["high"]), float(trig["low"]), float(trig["close"])
        candle = _candle_structure(o, h, l, c)
        breakout_level = _finite(trig.get("breakout_level") or trig.get("armed_price") or life.get("armed_price"))
        pb_high = _finite(trig.get("pullback_high"))
        pb_low = _finite(trig.get("pullback_low"))
        move_since_arm = None
        if arm_bar is not None and atr and atr > 0:
            arm_close = float(frame.iloc[int(arm_bar)]["close"])
            move_since_arm = abs(c - arm_close) / atr
        breakout_candle_atr = None if not atr or atr <= 0 else (h - l) / atr
        pullback_depth = None
        if atr and atr > 0 and breakout_level is not None:
            if side_sign < 0 and pb_high is not None:
                pullback_depth = abs(pb_high - breakout_level) / atr
            elif side_sign > 0 and pb_low is not None:
                pullback_depth = abs(breakout_level - pb_low) / atr
        dist_breakout = None
        if atr and atr > 0 and breakout_level is not None:
            dist_breakout = abs(c - breakout_level) / atr
        prot = base_ind.get("protected_high") if side_sign < 0 else base_ind.get("protected_low")
        dist_prot = None if atr is None or atr <= 0 or prot is None else abs(c - prot) / atr

        adx_d1 = _finite(trig.get("adx_slope_1"))
        adx_d2 = _finite(trig.get("adx_slope_2"))
        adx_d3 = _finite(trig.get("adx_slope_3"))

        vol_mean = vol.rolling(20, min_periods=5).mean()
        vol_std = vol.rolling(20, min_periods=5).std()
        trig_vol = _finite(trig.get("volume"))
        v_med = _finite(vol_med.iloc[trig_i])
        v_mean = _finite(vol_mean.iloc[trig_i])
        v_std = _finite(vol_std.iloc[trig_i])
        volume_warmup_ok = bool(trig_i >= 19 and v_med is not None)
        body = abs(c - o)
        rng = h - l
        body_to_range = (body / rng) if rng > 0 else None
        signed_body_trade = None
        if side_sign > 0:
            signed_body_trade = ((c - o) / o) * 100.0 if o else None
        else:
            signed_body_trade = ((o - c) / o) * 100.0 if o else None
        candle_dir_match = bool((c >= o and side_sign > 0) or (c <= o and side_sign < 0))
        body_atr = None if not atr or atr <= 0 else body / atr
        breakout_range = rng
        breakout_body_atr = body_atr
        breakout_ext = None
        if atr and atr > 0 and breakout_level is not None:
            if side_sign < 0:
                breakout_ext = max(0.0, (breakout_level - l) / atr)
            else:
                breakout_ext = max(0.0, (h - breakout_level) / atr)
        gap_to_next = None
        if fill_i < len(frame):
            fill_open = float(frame.iloc[fill_i]["open"])
            gap_to_next = ((fill_open / c) - 1.0) * 100.0 if c else None

        ts_trig = pd.Timestamp(trig["timestamp"])
        extra_json = {
            "adx_change_1": adx_d1,
            "adx_change_2": adx_d2,
            "adx_change_3": adx_d3,
            "ema_9_20_dir": None
            if base_ind["ema9"] is None or base_ind["ema20"] is None
            else float(np.sign(base_ind["ema9"] - base_ind["ema20"])),
            "ema_20_59_dir": None
            if base_ind["ema20"] is None or base_ind["ema59"] is None
            else float(np.sign(base_ind["ema20"] - base_ind["ema59"])),
            "ema_59_200_dir": None
            if base_ind["ema59"] is None or base_ind["ema200"] is None
            else float(np.sign(base_ind["ema59"] - base_ind["ema200"])),
            "setup_age": sig["metadata_json"].get("setup_age"),
            "ready_age": sig["metadata_json"].get("ready_age"),
            "bars_arm_to_trigger": sig["metadata_json"].get("bars_arm_to_trigger"),
            "bars_ready_to_trigger": sig["metadata_json"].get("bars_ready_to_trigger"),
            "bars_pullback_to_trigger": sig["metadata_json"].get("bars_pullback_to_trigger"),
            "opposite_arm_seen": sig["metadata_json"].get("opposite_arm_seen"),
            "opposite_arm_type": sig["metadata_json"].get("opposite_arm_type"),
            "opposite_arm_age": sig["metadata_json"].get("opposite_arm_age"),
            "opposite_arm_since_ready": sig["metadata_json"].get("opposite_arm_since_ready"),
            "opposite_arm_on_trigger_bar": sig["metadata_json"].get("opposite_arm_on_trigger_bar"),
            "arming_type": sig["metadata_json"].get("arming_type"),
            "entry_reason": sig["metadata_json"].get("entry_reason"),
            "causal_note": "trigger_snapshot_uses_closed_trigger_bar_only",
            # H1 Entry-Candle-Body (trigger closed bar)
            "entry_candle_body_atr": body_atr,
            "body_to_range_ratio": body_to_range,
            "signed_body_in_trade_direction": signed_body_trade,
            "candle_direction_matches_trade": candle_dir_match,
            "close_location_in_range": candle.get("entry_close_position"),
            # H2 Breakout-Range/ATR
            "breakout_candle_range": breakout_range,
            "breakout_candle_range_atr": breakout_candle_atr,
            "breakout_body_atr": breakout_body_atr,
            "breakout_close_distance_from_level_atr": dist_breakout,
            "breakout_extension_beyond_level_atr": breakout_ext,
            "breakout_gap_to_next_open_pct": gap_to_next,
            # H3 Volume-Ratio (past + closed trigger only)
            "trigger_volume": trig_vol,
            "volume_mean_20": v_mean,
            "volume_median_20": v_med,
            "volume_ratio_mean20": None if not v_mean or not trig_vol else trig_vol / v_mean,
            "volume_ratio_median20": None if not v_med or not trig_vol else trig_vol / v_med,
            "volume_zscore": None
            if not v_std or v_std <= 0 or trig_vol is None or v_mean is None
            else (trig_vol - v_mean) / v_std,
            "volume_warmup_ok": volume_warmup_ok,
            "volume_missing": trig_vol is None,
            "primary_hypotheses": ["H1_entry_candle_body", "H2_breakout_range_atr", "H3_volume_ratio"],
        }

        trig_feat = {
            "signal_key": sig["signal_key"],
            "feature_version": feature_version,
            "feature_stage": "trigger",
            "feature_timestamp": ts_trig,
            **base_ind,
            "move_since_arm_atr": move_since_arm,
            "breakout_candle_atr": breakout_candle_atr,
            "pullback_depth_atr": pullback_depth,
            "dist_breakout_atr": dist_breakout,
            "dist_protected_atr": dist_prot,
            "breakout_level": breakout_level,
            "pullback_high": pb_high,
            "pullback_low": pb_low,
            "lh_confirmed": None,
            "hl_confirmed": None,
            **candle,
            "volume": trig_vol,
            "volume_ratio": None if not v_med or not trig_vol else trig_vol / v_med,
            "hour_utc": int(ts_trig.hour),
            "day_of_week": int(ts_trig.dayofweek),
            "month": int(ts_trig.month),
            "split": sig["_split"],
            "feature_json": extra_json,
        }
        out.append(trig_feat)

        # Fill stage: only open-known candle fields + prior closed indicators
        fill_ts = pd.Timestamp(fill["timestamp"])
        fill_open = float(fill["open"])
        fill_feat = {
            "signal_key": sig["signal_key"],
            "feature_version": feature_version,
            "feature_stage": "fill",
            "feature_timestamp": fill_ts,
            **base_ind,  # last closed = trigger
            "move_since_arm_atr": move_since_arm,
            "breakout_candle_atr": breakout_candle_atr,
            "pullback_depth_atr": pullback_depth,
            "dist_breakout_atr": dist_breakout,
            "dist_protected_atr": dist_prot,
            "breakout_level": breakout_level,
            "pullback_high": pb_high,
            "pullback_low": pb_low,
            "lh_confirmed": None,
            "hl_confirmed": None,
            # no fill high/low/close — unknown at open
            "entry_candle_return_pct": None,
            "entry_candle_body_pct": None,
            "entry_candle_range_pct": None,
            "entry_upper_wick_ratio": None,
            "entry_lower_wick_ratio": None,
            "entry_close_position": None,
            "entry_bullish": None,
            "volume": None,
            "volume_ratio": None,
            "hour_utc": int(fill_ts.hour),
            "day_of_week": int(fill_ts.dayofweek),
            "month": int(fill_ts.month),
            "split": sig["_split"],
            "feature_json": {
                "fill_open": fill_open,
                "entry_price": float(sig["entry_price"]),
                "indicators_from": "last_closed_trigger_bar",
                "causal_note": "fill_snapshot_excludes_fill_candle_hlc_volume",
            },
        }
        out.append(fill_feat)
    return out


def evaluate_outcome_on_fill(
    *,
    side: int,
    entry: float,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    timestamps: list[Any],
    fill_i: int,
    n_bars: int,
) -> dict[str, Any]:
    end_h = min(n_bars - 1, fill_i + int(HORIZON_BARS) - 1)
    truncated = end_h < fill_i + int(HORIZON_BARS) - 1
    tp_t = first_touch_level(side, entry, highs, lows, fill_i, end_h, TP_PCT)
    sl_t = first_touch_level(side, entry, highs, lows, fill_i, end_h, SL_PCT)
    tp_b, sl_b = tp_t["bar_offset"], sl_t["bar_offset"]
    same_bar = bool(tp_t["reached"] and sl_t["reached"] and tp_b == sl_b)
    tp_first = bool(tp_t["reached"] and (not sl_t["reached"] or (tp_b is not None and sl_b is not None and tp_b < sl_b)))
    sl_first = bool(sl_t["reached"] and (not tp_t["reached"] or (tp_b is not None and sl_b is not None and sl_b < tp_b)))
    if tp_t["reached"] and sl_t["reached"]:
        if tp_b < sl_b:
            reason, gross, exit_bar = "TP", float(TP_PCT), fill_i + int(tp_b)
        elif sl_b < tp_b:
            reason, gross, exit_bar = "SL", float(SL_PCT), fill_i + int(sl_b)
        else:
            reason, gross, exit_bar = "same_bar_conservative_sl", float(SL_PCT), fill_i + int(sl_b)
            sl_first = True
            tp_first = False
    elif tp_t["reached"]:
        reason, gross, exit_bar = "TP", float(TP_PCT), fill_i + int(tp_b)
    elif sl_t["reached"]:
        reason, gross, exit_bar = "SL", float(SL_PCT), fill_i + int(sl_b)
    else:
        exit_bar = end_h
        gross = float(signed_return_pct(side, entry, float(closes[exit_bar])))
        reason = "data_end" if truncated else "time_exit"
    path = path_arrays(side, entry, highs, lows, closes, fill_i, exit_bar)
    exit_px = float(closes[exit_bar])
    if reason == "TP":
        exit_px = entry * (1 + TP_PCT / 100.0) if side > 0 else entry * (1 - TP_PCT / 100.0)
    elif reason in ("SL", "same_bar_conservative_sl"):
        exit_px = entry * (1 + SL_PCT / 100.0) if side > 0 else entry * (1 - SL_PCT / 100.0)
    net = float(gross) - float(COST_PCT)
    return {
        "exit_timestamp": timestamps[exit_bar],
        "exit_price": exit_px,
        "exit_reason": reason,
        "gross_pnl_pct": float(gross),
        "net_pnl_pct": net,
        "is_winner": net > 0,
        "tp_first": tp_first,
        "sl_first": sl_first or reason in ("SL", "same_bar_conservative_sl"),
        "same_bar_ambiguous": same_bar,
        "time_exit": reason == "time_exit",
        "data_end": reason == "data_end",
        "bars_held": int(exit_bar - fill_i),
        "bars_to_tp": int(tp_b) if tp_t["reached"] else None,
        "bars_to_sl": int(sl_b) if sl_t["reached"] else None,
        "mfe_pct": path.get("maximum_favorable_excursion_pct"),
        "mae_pct": path.get("maximum_adverse_excursion_pct"),
        "mae_before_tp_pct": None,
        "reclaimed_after_adverse": None,
        "max_underwater_bars": None,
    }


def build_outcome_rows(
    frame: pd.DataFrame,
    signal_rows: list[dict[str, Any]],
    *,
    outcome_version: str,
) -> list[dict[str, Any]]:
    highs = frame["high"].to_numpy(dtype=float)
    lows = frame["low"].to_numpy(dtype=float)
    closes = frame["close"].to_numpy(dtype=float)
    timestamps = list(pd.to_datetime(frame["timestamp"], utc=True))
    n = len(frame)
    rows = []
    for sig in signal_rows:
        sim = evaluate_outcome_on_fill(
            side=int(sig["_side_sign"]),
            entry=float(sig["entry_price"]),
            highs=highs,
            lows=lows,
            closes=closes,
            timestamps=timestamps,
            fill_i=int(sig["_fill_bar"]),
            n_bars=n,
        )
        rows.append(
            {
                "signal_key": sig["signal_key"],
                "outcome_version": outcome_version,
                "exit_model": EXIT_MODEL_TP3_SL2,
                "tp_pct": TP_PCT,
                "sl_pct": SL_PCT,
                "horizon_bars": HORIZON_BARS,
                "cost_pct": COST_PCT,
                "same_bar_policy": SAME_BAR_POLICY,
                **sim,
                "outcome_json": {
                    "exit_model": EXIT_MODEL_TP3_SL2,
                    "same_bar_policy": SAME_BAR_POLICY,
                },
            }
        )
    return rows


def strip_internal(signal_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clean = []
    for s in signal_rows:
        clean.append({k: v for k, v in s.items() if not k.startswith("_")})
    return clean
