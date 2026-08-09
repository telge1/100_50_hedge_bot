"""Causal reconstruction and diagnostics for July SL losers."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_all_wave_fade_generalization.annotate import annotate_waves_df
from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import load_mysql_ohlcv_tf
from orderbook_analyse.fractal_dynamic_cluster_upgrade_db.simulate import (
    _mfe_mae_slice,
    tpsl_for_tf,
)
from orderbook_analyse.fractal_parent_lower_tf_quality_db.db_build import build_waves_from_db
from orderbook_analyse.fractal_signal_confluence_db import ENV_FILE, TPSL_BY_TF
from orderbook_analyse.fractal_signal_confluence_db.signals import frozen_eff_edges_all_signal_tfs
from orderbook_analyse.fractal_wave_fade_loser_audit import (
    IMMEDIATE_MFE_FRAC,
    NEAR_TP_MFE_FRAC,
    REF_TRADES,
)
from orderbook_analyse.fractal_wave_fade_trend_filter.analysis import assign_trend_bucket
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import load_env_file


def _utc(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_convert("UTC") if t.tzinfo else t.tz_localize("UTC")


def load_july_trades() -> pd.DataFrame:
    t = pd.read_csv(REF_TRADES)
    for c in ("entry_time", "exit_time", "signal_time"):
        t[c] = pd.to_datetime(t[c], utc=True)
    jul = t[(t["entry_time"] >= "2026-07-01") & (t["entry_time"] < "2026-08-01")].copy()
    jul = jul.sort_values(["entry_time", "trade_id"]).reset_index(drop=True)
    jul["july_n"] = np.arange(1, len(jul) + 1)
    return jul


def build_signal_index(symbols: list[str], tfs: list[str]) -> dict[tuple[str, str], pd.DataFrame]:
    load_env_file(ENV_FILE)
    edges = frozen_eff_edges_all_signal_tfs()
    out: dict[tuple[str, str], pd.DataFrame] = {}
    for sym in symbols:
        for tf in tfs:
            print(f"[sig] {sym} {tf} …", flush=True)
            w = build_waves_from_db(sym, tf)
            if w.empty:
                out[(sym, tf)] = pd.DataFrame()
                continue
            ann = annotate_waves_df(w, symbol=sym, timeframe=tf, quantile_edges=edges)
            ann["trend_bucket"] = assign_trend_bucket(ann)
            ann["is_tier_a"] = (ann["trend_bucket"].astype(str) == "TREND_ALIGNED") & (
                ann["eff_quantile"].astype(str) == "Q4"
            )
            ann["confirmation_available_at"] = pd.to_datetime(
                ann["confirmation_available_at"], utc=True
            )
            out[(sym, tf)] = ann
    return out


def match_signal(tr: pd.Series, index: dict[tuple[str, str], pd.DataFrame]) -> dict[str, Any]:
    sym = str(tr["symbol"])
    tf = str(tr["first_signal_tf"])
    sig_t = _utc(tr["signal_time"])
    side = str(tr["side"])
    wave_dir = "DOWN" if side == "LONG" else "UP"
    base = {
        "signal_name": f"{tf}_WAVE_FADE",
        "signal_type": "BULLISH_WAVE_FADE" if side == "LONG" else "BEARISH_WAVE_FADE",
        "wave_direction": wave_dir,
        "fade_direction": side,
        "tier": "A",
        "q_bucket": "Q4",
        "trend_aligned": "TREND_ALIGNED",
        "ema_context": "UNKNOWN",
        "stoch_k_end": None,
        "stoch_d_end": None,
        "stoch_delta": None,
        "stoch_zone_end": "UNKNOWN",
        "directional_efficiency": None,
        "signed_price_move_pct": None,
        "n_bars_wave": None,
        "rsi_end": None,
        "context_match": "DERIVED_DEFAULT",
        "is_tier_a_matched": None,
    }
    ann = index.get((sym, tf))
    if ann is None or ann.empty:
        return base
    hit = ann.loc[ann["confirmation_available_at"] == sig_t]
    if hit.empty:
        delta = (ann["confirmation_available_at"] - sig_t).abs()
        j = int(delta.idxmin())
        if delta.loc[j] <= pd.Timedelta(minutes=1):
            hit = ann.loc[[j]]
        else:
            return base
    row = hit.iloc[0]
    base.update(
        {
            "wave_direction": str(row.get("direction", wave_dir)),
            "fade_direction": str(row.get("side", side)),
            "ema_context": str(row.get("ema_context", "UNKNOWN")),
            "trend_aligned": str(row.get("trend_bucket", "TREND_ALIGNED")),
            "q_bucket": str(row.get("eff_quantile", "Q4")),
            "stoch_k_end": float(row["stoch_k_end"]) if pd.notna(row.get("stoch_k_end")) else None,
            "stoch_delta": float(row["stoch_delta"]) if pd.notna(row.get("stoch_delta")) else None,
            "stoch_zone_end": str(row.get("stoch_zone_end", "UNKNOWN")),
            "directional_efficiency": float(row["directional_efficiency"])
            if pd.notna(row.get("directional_efficiency"))
            else None,
            "signed_price_move_pct": float(row["signed_price_move_pct"])
            if pd.notna(row.get("signed_price_move_pct"))
            else None,
            "n_bars_wave": int(row["n_bars"]) if pd.notna(row.get("n_bars")) else None,
            "rsi_end": float(row["rsi_end"]) if pd.notna(row.get("rsi_end")) else None,
            "context_match": "MATCHED_WAVE_AT_SIGNAL_TIME",
            "is_tier_a_matched": bool(row.get("is_tier_a", True)),
            "signal_type": (
                "BULLISH_WAVE_FADE"
                if str(row.get("side", side)) == "LONG"
                else "BEARISH_WAVE_FADE"
            ),
        }
    )
    # %D not always present
    if "stoch_d_end" in row.index and pd.notna(row.get("stoch_d_end")):
        base["stoch_d_end"] = float(row["stoch_d_end"])
    return base


def load_tf_frames(symbols: list[str], tfs: list[str]) -> dict[tuple[str, str], pd.DataFrame]:
    load_env_file(ENV_FILE)
    out = {}
    for sym in symbols:
        for tf in tfs:
            print(f"[ohlc] {sym} {tf} …", flush=True)
            df = load_mysql_ohlcv_tf(symbol=sym, timeframe=tf, env_file=ENV_FILE)
            out[(sym, tf)] = df
    return out


def load_1m(symbols: list[str], start: pd.Timestamp, end: pd.Timestamp) -> dict[str, pd.DataFrame]:
    load_env_file(ENV_FILE)
    out = {}
    for sym in symbols:
        print(f"[1m] {sym} …", flush=True)
        c = load_mysql_ohlcv_tf(symbol=sym, timeframe="1m", env_file=ENV_FILE)
        ts = pd.to_datetime(c["timestamp"], utc=True)
        # keep buffer
        c = c.loc[(ts >= start - pd.Timedelta(days=2)) & (ts <= end + pd.Timedelta(days=1))].reset_index(
            drop=True
        )
        out[sym] = c
    return out


def levels(tr: pd.Series) -> dict[str, float]:
    epx = float(tr["entry_price"])
    side = str(tr["side"])
    first = str(tr["first_signal_tf"])
    high = str(tr["highest_tf_reached"])
    itp, isl = tpsl_for_tf(first, extra_4h=False)
    ftp, fsl = tpsl_for_tf(high, extra_4h=False)

    def px(tp, sl):
        if side == "LONG":
            return epx * (1 + tp / 100), epx * (1 - sl / 100)
        return epx * (1 - tp / 100), epx * (1 + sl / 100)

    itp_px, isl_px = px(itp, isl)
    ftp_px, fsl_px = px(ftp, fsl)
    return {
        "initial_tp_pct": itp,
        "initial_sl_pct": isl,
        "final_tp_pct": ftp,
        "final_sl_pct": fsl,
        "final_tp_price": ftp_px,
        "final_sl_price": fsl_px,
        "tpsl_profile": f"TP{ftp:g}/SL{fsl:g}",
    }


def path_diagnostics(tr: pd.Series, c1m: pd.DataFrame, lev: dict[str, float]) -> dict[str, Any]:
    side = str(tr["side"])
    epx = float(tr["entry_price"])
    et = _utc(tr["entry_time"])
    xt = _utc(tr["exit_time"])
    ts = pd.to_datetime(c1m["timestamp"], utc=True)
    mask_e = ts == et
    mask_x = ts == xt
    if not mask_e.any() or not mask_x.any():
        return {
            "mfe_pct": None,
            "mae_pct": None,
            "mfe_to_tp": None,
            "mfe_class": "UNKNOWN",
            "first_1m_dir_ret": None,
            "first_3m_mfe": None,
            "immediate_adverse": None,
            "bars_to_sl": None,
            "holding_minutes": float(tr["holding_minutes"]),
        }
    ei = int(np.where(mask_e.to_numpy())[0][0])
    xi = int(np.where(mask_x.to_numpy())[0][0])
    high = c1m["high"].astype(float).to_numpy()
    low = c1m["low"].astype(float).to_numpy()
    close = c1m["close"].astype(float).to_numpy()
    mfe, mae = _mfe_mae_slice(side, epx, high, low, ei, xi)
    tp = float(lev["final_tp_pct"])
    mfe_to_tp = mfe / tp if tp > 0 else None

    def dir_ret(px):
        if side == "LONG":
            return (px / epx - 1.0) * 100.0
        return (epx - px) / epx * 100.0

    first_1 = dir_ret(float(close[ei]))
    first_3 = None
    if xi >= ei:
        m3, _ = _mfe_mae_slice(side, epx, high, low, ei, min(ei + 2, xi))
        first_3 = m3

    if side == "LONG":
        adverse_1 = float(low[ei]) < epx * 0.9999 and first_1 < 0
    else:
        adverse_1 = float(high[ei]) > epx * 1.0001 and first_1 < 0

    if mfe_to_tp is None:
        mfe_class = "UNKNOWN"
    elif mfe_to_tp < IMMEDIATE_MFE_FRAC:
        mfe_class = "IMMEDIATE_FAILURE"
    elif mfe_to_tp >= NEAR_TP_MFE_FRAC:
        mfe_class = "NEAR_TP_THEN_FAIL"
    else:
        mfe_class = "PARTIAL_FADE_THEN_FAIL"

    return {
        "mfe_pct": float(mfe),
        "mae_pct": float(mae),
        "mfe_to_tp": float(mfe_to_tp) if mfe_to_tp is not None else None,
        "mfe_class": mfe_class,
        "first_1m_dir_ret": float(first_1),
        "first_3m_mfe": float(first_3) if first_3 is not None else None,
        "immediate_adverse": bool(adverse_1),
        "bars_to_sl": int(xi - ei),
        "holding_minutes": float(tr["holding_minutes"]),
    }


def pre_entry_moves(tr: pd.Series, frames: dict[tuple[str, str], pd.DataFrame]) -> dict[str, Any]:
    sym = str(tr["symbol"])
    tf = str(tr["first_signal_tf"])
    et = _utc(tr["entry_time"])
    df = frames.get((sym, tf))
    out = {
        "pre_1_ret_pct": None,
        "pre_3_ret_pct": None,
        "pre_6_ret_pct": None,
        "pre_12_ret_pct": None,
        "consec_dir_candles": None,
        "pre_move_vs_fade": None,
        "extension_bucket": "UNKNOWN",
    }
    if df is None or df.empty:
        return out
    ts = pd.to_datetime(df["timestamp"], utc=True)
    # last closed bar strictly before entry
    prior = df.loc[ts < et].copy()
    if len(prior) < 2:
        return out
    close = prior["close"].astype(float).to_numpy()
    open_ = prior["open"].astype(float).to_numpy()

    def cum_ret(n):
        if len(close) < n + 1:
            return None
        return float((close[-1] / close[-1 - n] - 1.0) * 100.0)

    out["pre_1_ret_pct"] = cum_ret(1)
    out["pre_3_ret_pct"] = cum_ret(3)
    out["pre_6_ret_pct"] = cum_ret(6)
    out["pre_12_ret_pct"] = cum_ret(12)

    # consecutive directional bodies ending at last bar
    bodies = close - open_
    sign = np.sign(bodies[-1])
    consec = 0
    for b in bodies[::-1]:
        if np.sign(b) == sign and sign != 0:
            consec += 1
        else:
            break
    out["consec_dir_candles"] = int(consec)

    side = str(tr["side"])
    # fade SHORT after UP move → pre_ret should be >0; fade LONG after DOWN → pre_ret <0
    pre = out["pre_6_ret_pct"]
    if pre is None:
        out["pre_move_vs_fade"] = "UNKNOWN"
    elif side == "SHORT":
        out["pre_move_vs_fade"] = "EXPECTED_UP_EXT" if pre > 0 else "WEAK_OR_WRONG_EXT"
    else:
        out["pre_move_vs_fade"] = "EXPECTED_DOWN_EXT" if pre < 0 else "WEAK_OR_WRONG_EXT"

    # extension strength by |pre_6|
    if pre is None:
        out["extension_bucket"] = "UNKNOWN"
    else:
        a = abs(pre)
        if a < 0.5:
            out["extension_bucket"] = "WEAK_EXT"
        elif a < 1.5:
            out["extension_bucket"] = "MODERATE_EXT"
        else:
            out["extension_bucket"] = "STRONG_EXT"
    return out


def htf_momentum(tr: pd.Series, frames: dict[tuple[str, str], pd.DataFrame]) -> dict[str, Any]:
    """Recent HTF price momentum vs fade side (not Tier-A H4 EMA — all Tier A are TREND_ALIGNED by def)."""
    sym = str(tr["symbol"])
    et = _utc(tr["entry_time"])
    side = str(tr["side"])
    out = {
        "htf_1h_ret_6": None,
        "htf_4h_ret_3": None,
        "htf_vs_fade": "UNKNOWN",
        "htf_class": "UNKNOWN",
    }
    def ret_n(tf, n):
        df = frames.get((sym, tf))
        if df is None or df.empty:
            return None
        ts = pd.to_datetime(df["timestamp"], utc=True)
        prior = df.loc[ts < et]
        if len(prior) < n + 1:
            return None
        c = prior["close"].astype(float).to_numpy()
        return float((c[-1] / c[-1 - n] - 1.0) * 100.0)

    r1 = ret_n("1h", 6)
    r4 = ret_n("4h", 3)
    out["htf_1h_ret_6"] = r1
    out["htf_4h_ret_3"] = r4
    # Against fade if momentum continues in wave direction (against fade)
    # SHORT fade: against if HTF still rising; LONG fade: against if HTF still falling
    votes = []
    for r in (r1, r4):
        if r is None:
            continue
        if side == "SHORT":
            votes.append("AGAINST" if r > 0.3 else ("WITH" if r < -0.3 else "MIXED"))
        else:
            votes.append("AGAINST" if r < -0.3 else ("WITH" if r > 0.3 else "MIXED"))
    if not votes:
        out["htf_class"] = "UNKNOWN"
    elif votes.count("AGAINST") >= 1 and votes.count("WITH") == 0:
        out["htf_class"] = "AGAINST_HTF_MOMENTUM"
    elif votes.count("WITH") >= 1 and votes.count("AGAINST") == 0:
        out["htf_class"] = "WITH_HTF_MOMENTUM"
    else:
        out["htf_class"] = "MIXED_HTF"
    out["htf_vs_fade"] = out["htf_class"]
    return out


def stoch_failure_flags(sig: dict[str, Any], side: str) -> dict[str, Any]:
    k = sig.get("stoch_k_end")
    delta = sig.get("stoch_delta")
    zone = str(sig.get("stoch_zone_end", "UNKNOWN"))
    pinned = False
    failed_fade = False
    if k is not None:
        if side == "SHORT" and k >= 80:
            pinned = True
        if side == "LONG" and k <= 20:
            pinned = True
    # failed fade: SHORT but stoch still rising into OB; LONG but still falling into OS
    if k is not None and delta is not None:
        if side == "SHORT" and k >= 70 and delta > 0:
            failed_fade = True
        if side == "LONG" and k <= 30 and delta < 0:
            failed_fade = True
    return {
        "stoch_pinned_extreme": pinned,
        "failed_fade_stoch_pattern": failed_fade,
        "stoch_zone_end": zone,
    }


def assign_failure_mode(
    *,
    mfe_class: str,
    htf_class: str,
    ext_bucket: str,
    stoch_failed: bool,
    repeated_fade: bool,
    immediate_adverse: bool | None,
) -> str:
    if mfe_class == "NEAR_TP_THEN_FAIL" or (
        mfe_class == "PARTIAL_FADE_THEN_FAIL" and not immediate_adverse
    ):
        # if had meaningful MFE, more exit-management-ish — but still SL
        primary = "FADE_WORKED_BUT_TP_TOO_FAR" if mfe_class == "NEAR_TP_THEN_FAIL" else None
    else:
        primary = None

    if repeated_fade:
        return "REPEATED_FADE_IN_SAME_MOVE"
    if htf_class == "AGAINST_HTF_MOMENTUM" and (
        mfe_class == "IMMEDIATE_FAILURE" or immediate_adverse
    ):
        return "COUNTERTREND_CONTINUATION"
    if ext_bucket == "STRONG_EXT" and mfe_class == "IMMEDIATE_FAILURE":
        return "VOLATILITY_EXPANSION"
    if stoch_failed and mfe_class == "IMMEDIATE_FAILURE":
        return "BAD_ENTRY"
    if primary:
        return primary
    if mfe_class == "IMMEDIATE_FAILURE" or immediate_adverse:
        return "BAD_ENTRY"
    if mfe_class == "PARTIAL_FADE_THEN_FAIL":
        return "FADE_WORKED_BUT_TP_TOO_FAR"
    if htf_class == "AGAINST_HTF_MOMENTUM":
        return "COUNTERTREND_CONTINUATION"
    return "OTHER"


def mark_repeated_fades(rows: list[dict[str, Any]]) -> None:
    """Flag if prior SL/trade same symbol+side within 12h (same move fade repeat)."""
    for i, r in enumerate(rows):
        r["repeated_fade_same_move"] = False
        t0 = _utc(r["entry_time"])
        for j in range(i - 1, -1, -1):
            prev = rows[j]
            dt_h = (t0 - _utc(prev["entry_time"])).total_seconds() / 3600.0
            if dt_h > 12:
                break
            if prev["symbol"] == r["symbol"] and prev["side"] == r["side"]:
                r["repeated_fade_same_move"] = True
                break
