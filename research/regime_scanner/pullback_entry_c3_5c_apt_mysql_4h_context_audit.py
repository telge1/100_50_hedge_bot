"""APTUSDT MySQL 5m → causal 4h context diagnostic (research-only).

Primary data source: MySQL ``regime_scanner_research`` (no silent feather fallback).
A6 / Pine / TP3/SL2 semantics frozen. Diagnostic only — no live veto activation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.candle_sources import load_regime_db_env_file
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicator_feature_store import required_indicator_warmup_bars
from research.regime_scanner.indicators import ema
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5 import (
    apply_pullback_entry,
    config_hash,
    enrich_indicators,
    prepare_research_frame,
)
from research.regime_scanner.pullback_entry_c3_5_diagnostics import baseline_a6
from research.regime_scanner.pullback_entry_c3_5c_c34b_4h_trend_audit import (
    build_c34b_htf_frame,
    lookup_closed_c34b_bar,
)
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
    assign_split,
    fixed_chrono_splits,
)
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

SYMBOL = "APTUSDT"
TIMEFRAME = "15m"
VARIANT = "A6"
EXPECTED_A6_HASH = "aeddc2c9edc5549c0c84d7a2af1e1b08b7f46d23680f58b3ebd08e1ff136b608"
EXPECTED_N_FILLS = 55
ANALYZE_START = "2026-01-26T00:00:00+00:00"
ANALYZE_END_EXCLUSIVE = "2026-06-28T00:00:00+00:00"
EXPECTED_MYSQL_N_5M = 52569
TP_PCT = 3.0
SL_PCT = -2.0
HORIZON_BARS = 192
COST_PCT = COST_ROUNDTRIP_PCT

DEFAULT_REF_PANEL = Path(
    "research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine/"
    "c35c_fill_excursion_audit/fill_excursion_panel.csv"
)
DEFAULT_OUT = Path("research/regime_scanner/results/apt_mysql_4h_context_audit_20260722")
DEFAULT_BASELINE_DIR = Path(
    "research/regime_scanner/results/baselines/c2_loose_mar_2026_before_c3"
)

SUCCESS_CRITERIA_DOC = {
    "net_expectancy_improves_vs_baseline": True,
    "pf_improves": True,
    "oos_positive_or_improves": True,
    "retain_ge_60pct_trades": 0.60,
    "block_more_losers_than_winners": True,
    "long_short_documented_separately": True,
    "no_single_split_only_judgment": True,
    "activation": False,
}


class MySQLRequiredError(RuntimeError):
    """Raised when MySQL load fails or feather fallback would be required."""


def _mean(s: pd.Series) -> float | None:
    s = pd.to_numeric(s, errors="coerce").dropna()
    return None if s.empty else float(s.mean())


def _median(s: pd.Series) -> float | None:
    s = pd.to_numeric(s, errors="coerce").dropna()
    return None if s.empty else float(s.median())


def profit_factor(nets: pd.Series) -> float | None:
    s = pd.to_numeric(nets, errors="coerce").dropna()
    if s.empty:
        return None
    gains = float(s[s > 0].sum())
    losses = float(s[s <= 0].sum())
    if abs(losses) < 1e-15:
        return float("inf") if gains > 0 else None
    return gains / abs(losses)


def max_drawdown_pp(nets: pd.Series) -> float | None:
    s = pd.to_numeric(nets, errors="coerce").dropna()
    if s.empty:
        return None
    eq = s.cumsum()
    return float((eq - eq.cummax()).min())


def max_losing_streak(nets: pd.Series) -> int:
    s = pd.to_numeric(nets, errors="coerce").fillna(0)
    streak = best = 0
    for v in s:
        if v <= 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    return int(best)


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


def load_apt_5m_mysql() -> tuple[pd.DataFrame, dict[str, Any]]:
    """Load APT 5m from MySQL only. Never falls back to feather."""
    load_regime_db_env_file()
    try:
        frame = load_symbol_candles(SYMBOL, data_source="mysql", exchange="bybit")
    except Exception as exc:  # noqa: BLE001
        raise MySQLRequiredError(
            f"MySQL load failed for {SYMBOL}; feather fallback forbidden: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if frame is None or frame.empty:
        raise MySQLRequiredError(f"MySQL returned empty 5m frame for {SYMBOL}")
    meta = {
        "data_source": "mysql",
        "feather_fallback": False,
        "n_5m": int(len(frame)),
        "t0": str(pd.to_datetime(frame["timestamp"], utc=True).iloc[0]),
        "t1": str(pd.to_datetime(frame["timestamp"], utc=True).iloc[-1]),
        "ohlcv_sha1": sha1_ohlcv(frame),
        "n_5m_matches_inventory": int(len(frame)) == EXPECTED_MYSQL_N_5M,
    }
    return frame.reset_index(drop=True), meta


def load_apt_5m_feather_for_parity_only() -> pd.DataFrame:
    return load_symbol_candles(SYMBOL, data_source="feather").reset_index(drop=True)


def compare_mysql_feather_parity(mysql_df: pd.DataFrame, feather_df: pd.DataFrame) -> dict[str, Any]:
    m = mysql_df.copy()
    f = feather_df.copy()
    m["timestamp"] = pd.to_datetime(m["timestamp"], utc=True)
    f["timestamp"] = pd.to_datetime(f["timestamp"], utc=True)
    common = sorted(set(m["timestamp"]) & set(f["timestamp"]))
    mc = m.set_index("timestamp").loc[common]
    fc = f.set_index("timestamp").loc[common]
    max_px = 0.0
    for c in ("open", "high", "low", "close"):
        max_px = max(max_px, float((mc[c] - fc[c]).abs().max())) if len(common) else 0.0
    max_vol = float((mc["volume"] - fc["volume"]).abs().max()) if len(common) else 0.0
    ok = len(m) == len(f) and len(common) == len(m) and max_px == 0.0 and max_vol == 0.0
    return {
        "ok": ok,
        "mysql_n": int(len(m)),
        "feather_n": int(len(f)),
        "n_common": int(len(common)),
        "only_mysql": int(len(set(m["timestamp"]) - set(f["timestamp"]))),
        "only_feather": int(len(set(f["timestamp"]) - set(m["timestamp"]))),
        "max_abs_price_diff": max_px,
        "max_abs_volume_diff": max_vol,
        "classification": "exakt identisch" if ok else "teilweise unterschiedlich",
    }


def slice_5m_with_warmup(
    full_5m: pd.DataFrame,
    *,
    analyze_start: pd.Timestamp,
    analyze_end_exclusive: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Match ``build_extended_tf_frame`` 5m warmup window (no feather load)."""
    warm_bars = max(required_indicator_warmup_bars(), 400)
    load_start = analyze_start - pd.Timedelta(minutes=5 * warm_bars)
    ts = pd.to_datetime(full_5m["timestamp"], utc=True)
    out = full_5m.loc[(ts >= load_start) & (ts < analyze_end_exclusive)].copy().reset_index(drop=True)
    return out, {
        "warmup_5m_bars_requested": warm_bars,
        "load_start": load_start.isoformat(),
        "n_5m_sliced": int(len(out)),
    }


def last_closed_4h_for_fill(frame4h: pd.DataFrame, *, fill_ts: pd.Timestamp) -> dict[str, Any]:
    """Last fully closed 4h bar at fill (``htf_close_decision <= fill_ts``)."""
    fill_ts = pd.Timestamp(fill_ts)
    if fill_ts.tzinfo is None:
        fill_ts = fill_ts.tz_localize("UTC")
    else:
        fill_ts = fill_ts.tz_convert("UTC")
    if frame4h.empty:
        return {"found": False, "missing": True, "context_is_causal": True}
    hit = lookup_closed_c34b_bar(frame4h, trigger_decision=fill_ts)
    hit["missing"] = not bool(hit.get("found"))
    return hit


def ema_pair_class(side: str, a: float | None, b: float | None) -> str:
    if a is None or b is None or not np.isfinite(a) or not np.isfinite(b) or a == b:
        return "neutral"
    if side == "short":
        return "aligned" if a < b else "opposed"
    return "aligned" if a > b else "opposed"


def combined_context_class(
    side: str,
    e9: float | None,
    e20: float | None,
    e59: float | None,
    e200: float | None,
) -> str:
    vals = [e9, e20, e59, e200]
    if any(v is None or not np.isfinite(v) for v in vals):
        return "mixed"
    if side == "short":
        if e9 < e20 < e59 < e200:
            return "strong_aligned"
        # clearly bullish mid/macro
        if e20 > e59 or e59 > e200:
            return "opposed"
        return "mixed"
    if e9 > e20 > e59 > e200:
        return "strong_aligned"
    if e20 < e59 or e59 < e200:
        return "opposed"
    return "mixed"


def structure_class(side: str, major: int | None) -> str | None:
    if major is None or not np.isfinite(major):
        return None
    major_i = int(major)
    if major_i == 0:
        return "neutral"
    want = 1 if side == "long" else -1
    return "aligned" if major_i == want else "opposed"


def enrich_4h_features(ohlcv4: pd.DataFrame) -> pd.DataFrame:
    """Causal 4h indicators including EMA59/200 slopes (audit-local, not A6 change)."""
    feat = enrich_indicators(ohlcv4)
    close = pd.to_numeric(feat["close"], errors="coerce").astype("float64")
    for p in (59, 200):
        col = f"ema_{p}"
        if col not in feat.columns:
            feat[col] = ema(close, p)
        for w in (1, 2, 3):
            slope = f"ema_{p}_slope_{w}"
            if slope not in feat.columns:
                feat[slope] = feat[col] - feat[col].shift(w)
    # direction flags
    feat["ema_9_20_dir"] = np.sign(feat["ema_9"] - feat["ema_20"])
    feat["ema_20_59_dir"] = np.sign(feat["ema_20"] - feat["ema_59"])
    feat["ema_59_200_dir"] = np.sign(feat["ema_59"] - feat["ema_200"])
    return feat


def evaluate_tp_sl_on_fill(
    *,
    side: int,
    entry: float,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    fill_i: int,
    n_bars: int,
    tp: float = TP_PCT,
    sl: float = SL_PCT,
    horizon_bars: int = HORIZON_BARS,
    cost_pct: float = COST_PCT,
) -> dict[str, Any]:
    end_h = min(n_bars - 1, fill_i + int(horizon_bars) - 1)
    truncated = end_h < fill_i + int(horizon_bars) - 1
    tp_t = first_touch_level(side, entry, highs, lows, fill_i, end_h, tp)
    sl_t = first_touch_level(side, entry, highs, lows, fill_i, end_h, sl)
    tp_b, sl_b = tp_t["bar_offset"], sl_t["bar_offset"]
    if tp_t["reached"] and sl_t["reached"]:
        if tp_b < sl_b:
            reason, gross, exit_bar = "TP", float(tp), fill_i + int(tp_b)
        elif sl_b < tp_b:
            reason, gross, exit_bar = "SL", float(sl), fill_i + int(sl_b)
        else:
            reason, gross, exit_bar = "same_bar_conservative_sl", float(sl), fill_i + int(sl_b)
    elif tp_t["reached"]:
        reason, gross, exit_bar = "TP", float(tp), fill_i + int(tp_b)
    elif sl_t["reached"]:
        reason, gross, exit_bar = "SL", float(sl), fill_i + int(sl_b)
    else:
        exit_bar = end_h
        gross = float(signed_return_pct(side, entry, float(closes[exit_bar])))
        reason = "data_end" if truncated else "time_exit"
    path = path_arrays(side, entry, highs, lows, closes, fill_i, exit_bar)
    return {
        "exit_reason": reason,
        "exit_bar": int(exit_bar),
        "bars_held": int(exit_bar - fill_i),
        "gross_pnl_pct": float(gross),
        "cost_pct": float(cost_pct),
        "net_pnl_pct": float(gross) - float(cost_pct),
        "mfe_pct": path.get("maximum_favorable_excursion_pct"),
        "mae_pct": path.get("maximum_adverse_excursion_pct"),
        "truncated": bool(truncated and reason == "data_end"),
    }


def summarize_group(df: pd.DataFrame, *, label: str) -> dict[str, Any]:
    if df is None or df.empty:
        return {"label": label, "n": 0}
    nets = df["net_pnl_pct"]
    er = df["exit_reason"]
    out: dict[str, Any] = {
        "label": label,
        "n": int(len(df)),
        "n_tp": int((er == "TP").sum()),
        "n_sl": int(er.isin(["SL", "same_bar_conservative_sl"]).sum()),
        "n_time_exit": int((er == "time_exit").sum()),
        "n_data_end": int((er == "data_end").sum()),
        "winrate": float((nets > 0).mean()),
        "net_expectancy": _mean(nets),
        "sum_pp": float(nets.sum()),
        "profit_factor": profit_factor(nets),
        "max_drawdown_pp": max_drawdown_pp(nets),
        "max_losing_streak": max_losing_streak(nets),
        "median_bars_held": _median(df["bars_held"]),
    }
    if "split" in df.columns:
        for sp in ("dev", "validation", "oos"):
            gs = df[df["split"] == sp]
            out[f"n_{sp}"] = int(len(gs))
            out[f"net_expectancy_{sp}"] = _mean(gs["net_pnl_pct"]) if len(gs) else None
    return out


def veto_view(trades: pd.DataFrame, *, allow_mask: pd.Series, name: str) -> dict[str, Any]:
    taken = trades.loc[allow_mask]
    blocked = trades.loc[~allow_mask]
    base = summarize_group(taken, label=name)
    bw = blocked[blocked["net_pnl_pct"] > 0]
    bl = blocked[blocked["net_pnl_pct"] <= 0]
    base.update(
        {
            "n_taken": int(len(taken)),
            "n_blocked": int(len(blocked)),
            "n_blocked_winners": int(len(bw)),
            "n_blocked_losers": int(len(bl)),
            "retain_rate": float(len(taken) / len(trades)) if len(trades) else None,
            "blocked_more_losers_than_winners": bool(len(bl) > len(bw)),
        }
    )
    return base


def build_15m_a6_from_5m(
    full_5m: pd.DataFrame,
    *,
    analyze_start: pd.Timestamp,
    analyze_end_exclusive: pd.Timestamp,
) -> tuple[pd.DataFrame, list[dict[str, Any]], dict[str, Any]]:
    sliced, warm_meta = slice_5m_with_warmup(
        full_5m, analyze_start=analyze_start, analyze_end_exclusive=analyze_end_exclusive
    )
    decision = analyze_end_exclusive + pd.Timedelta(hours=1)
    ohlcv15 = aggregate_complete_from_5m(sliced, "15m", decision_time=decision)
    frame = prepare_research_frame(ohlcv15, ohlcv_15m=None, ohlcv_30m=None)
    ts = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.loc[(ts >= analyze_start) & (ts < analyze_end_exclusive)].copy().reset_index(drop=True)
    frame["bar_index"] = np.arange(len(frame))
    cfg = baseline_a6()
    ch = config_hash(cfg)
    if ch != EXPECTED_A6_HASH:
        raise RuntimeError(f"A6 config hash drift: {ch} != {EXPECTED_A6_HASH}")
    _tl, entries, _lives = apply_pullback_entry(frame, cfg, return_lifecycles=True)
    fills = _filled_sorted(frame, entries)
    meta = {
        "n_15m_bars": int(len(frame)),
        "n_fills": int(len(fills)),
        "config_hash": ch,
        "variant": VARIANT,
        **warm_meta,
    }
    return frame, fills, meta


def build_4h_context_frame(
    full_5m: pd.DataFrame,
    *,
    analyze_start: pd.Timestamp,
    analyze_end_exclusive: pd.Timestamp,
) -> pd.DataFrame:
    """C3.4B structure + extended EMA/ADX/ATR on complete UTC 4h buckets."""
    decision = analyze_end_exclusive + pd.Timedelta(hours=1)
    # Use full MySQL history for HTF EMA200 readiness (structure frame), then attach extras.
    frame4h = build_c34b_htf_frame(
        full_5m,
        "4h",
        decision=decision,
        analyze_start=analyze_start,
        analyze_end_exclusive=analyze_end_exclusive,
    )
    if frame4h.empty:
        return frame4h
    ohlcv4 = aggregate_complete_from_5m(full_5m, "4h", decision_time=decision)
    feat4 = enrich_4h_features(ohlcv4)
    feat4["timestamp"] = pd.to_datetime(feat4["timestamp"], utc=True)
    ts_map = feat4.set_index("timestamp")
    for c in (
        "ema_9",
        "ema_20",
        "ema_59",
        "ema_200",
        "ema_9_slope_3",
        "ema_20_slope_3",
        "ema_59_slope_3",
        "ema_200_slope_3",
        "ema_9_20_dir",
        "ema_20_59_dir",
        "ema_59_200_dir",
        "adx",
        "plus_di",
        "minus_di",
        "atr_14",
        "atr",
    ):
        if c in ts_map.columns:
            frame4h[c] = pd.to_datetime(frame4h["timestamp"], utc=True).map(ts_map[c])
    return frame4h


def run_apt_mysql_4h_context_audit(
    *,
    output_dir: Path = DEFAULT_OUT,
    reference_panel: Path = DEFAULT_REF_PANEL,
    baseline_dir: Path = DEFAULT_BASELINE_DIR,
) -> dict[str, Any]:
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    a0 = pd.Timestamp(ANALYZE_START)
    a1 = pd.Timestamp(ANALYZE_END_EXCLUSIVE)

    mysql_5m, mysql_meta = load_apt_5m_mysql()
    feather_5m = load_apt_5m_feather_for_parity_only()
    parity = compare_mysql_feather_parity(mysql_5m, feather_5m)
    pd.DataFrame([parity | mysql_meta]).to_csv(output_dir / "mysql_apt_parity.csv", index=False)
    if not parity["ok"]:
        meta = {
            "ok": False,
            "aborted": True,
            "reason": "mysql_feather_parity_failed",
            "parity": parity,
            "mysql_meta": mysql_meta,
            "pine_unchanged": True,
            "a6_unchanged": True,
        }
        (output_dir / "metadata.json").write_text(
            json.dumps(json_safe(meta), indent=2) + "\n", encoding="utf-8"
        )
        (output_dir / "apt_4h_context_report.md").write_text(
            "# APT MySQL 4h Context Audit — ABORTED\n\nMySQL/Feather parity failed.\n",
            encoding="utf-8",
        )
        return meta

    frame15, fills, a6_meta = build_15m_a6_from_5m(
        mysql_5m, analyze_start=a0, analyze_end_exclusive=a1
    )
    if a6_meta["n_fills"] != EXPECTED_N_FILLS:
        meta = {
            "ok": False,
            "aborted": True,
            "reason": "fill_count_mismatch",
            "n_fills": a6_meta["n_fills"],
            "expected": EXPECTED_N_FILLS,
            "a6": a6_meta,
            "parity": parity,
            "mysql_meta": mysql_meta,
            "pine_unchanged": True,
            "a6_unchanged": True,
        }
        (output_dir / "metadata.json").write_text(
            json.dumps(json_safe(meta), indent=2) + "\n", encoding="utf-8"
        )
        (output_dir / "apt_4h_context_report.md").write_text(
            f"# APT MySQL 4h Context Audit — ABORTED\n\nFill count {a6_meta['n_fills']} != {EXPECTED_N_FILLS}.\n",
            encoding="utf-8",
        )
        return meta

    ref = pd.read_csv(reference_panel)
    ref["fill_time"] = pd.to_datetime(ref["fill_time"], utc=True)
    fill_rows = []
    for i, f in enumerate(fills):
        fill_rows.append(
            {
                "fill_id": f"F{i:04d}",
                "setup_id": f.get("setup_id"),
                "side": f.get("side_name"),
                "side_sign": int(f["side"]),
                "fill_bar": int(f["fill_bar"]),
                "fill_time": pd.Timestamp(f["fill_timestamp"]),
                "fill_price": float(f["entry_price"]),
                "trigger_bar": f.get("trigger_bar"),
                "trigger_time": f.get("trigger_timestamp"),
            }
        )
    fills_df = pd.DataFrame(fill_rows).sort_values("fill_time").reset_index(drop=True)
    ref_s = ref.sort_values("fill_time").reset_index(drop=True)
    if len(ref_s) != len(fills_df):
        meta = {
            "ok": False,
            "aborted": True,
            "reason": "reference_length_mismatch",
            "n_fills": len(fills_df),
            "n_ref": len(ref_s),
        }
        (output_dir / "metadata.json").write_text(
            json.dumps(json_safe(meta), indent=2) + "\n", encoding="utf-8"
        )
        return meta

    time_ok = bool(
        (fills_df["fill_time"].reset_index(drop=True) == ref_s["fill_time"].reset_index(drop=True)).all()
    )
    price_ok = bool(
        np.allclose(
            fills_df["fill_price"].to_numpy(dtype=float),
            ref_s["fill_price"].to_numpy(dtype=float),
            rtol=0,
            atol=1e-12,
        )
    )
    side_ok = bool(
        (fills_df["side"].astype(str).to_numpy() == ref_s["side"].astype(str).to_numpy()).all()
    )
    if not (time_ok and price_ok and side_ok):
        meta = {
            "ok": False,
            "aborted": True,
            "reason": "entry_parity_failed",
            "time_ok": time_ok,
            "price_ok": price_ok,
            "side_ok": side_ok,
            "parity": parity,
            "a6": a6_meta,
            "pine_unchanged": True,
            "a6_unchanged": True,
        }
        (output_dir / "metadata.json").write_text(
            json.dumps(json_safe(meta), indent=2) + "\n", encoding="utf-8"
        )
        (output_dir / "apt_4h_context_report.md").write_text(
            "# APT MySQL 4h Context Audit — ABORTED\n\nEntry parity vs feather APT run failed.\n",
            encoding="utf-8",
        )
        return meta

    highs = frame15["high"].to_numpy(dtype=float)
    lows = frame15["low"].to_numpy(dtype=float)
    closes = frame15["close"].to_numpy(dtype=float)
    n_bars = len(frame15)
    splits = fixed_chrono_splits(a0, a1)
    trade_rows = []
    for _, fr in fills_df.iterrows():
        sim = evaluate_tp_sl_on_fill(
            side=int(fr["side_sign"]),
            entry=float(fr["fill_price"]),
            highs=highs,
            lows=lows,
            closes=closes,
            fill_i=int(fr["fill_bar"]),
            n_bars=n_bars,
        )
        split_raw = assign_split(fr["fill_time"], splits)
        split = {"development": "dev", "validation": "validation", "oos": "oos"}.get(
            split_raw, split_raw
        )
        trade_rows.append(
            {
                **fr.to_dict(),
                **sim,
                "split": split,
                "month": pd.Timestamp(fr["fill_time"]).strftime("%Y-%m"),
            }
        )
    trades = pd.DataFrame(trade_rows)

    frame4h = build_4h_context_frame(mysql_5m, analyze_start=a0, analyze_end_exclusive=a1)

    enriched = []
    for _, tr in trades.iterrows():
        hit = last_closed_4h_for_fill(frame4h, fill_ts=tr["fill_time"])
        row = tr.to_dict()
        row["join_ok"] = bool(hit.get("found"))
        row["context_missing"] = bool(hit.get("missing") or not hit.get("found"))
        if hit.get("found"):
            r4 = hit["row"]
            e9 = float(r4["ema_9"]) if "ema_9" in r4.index and pd.notna(r4["ema_9"]) else None
            e20 = float(r4["ema_20"]) if "ema_20" in r4.index and pd.notna(r4["ema_20"]) else None
            e59 = float(r4["ema_59"]) if "ema_59" in r4.index and pd.notna(r4.get("ema_59")) else None
            e200 = (
                float(r4["ema_200"]) if "ema_200" in r4.index and pd.notna(r4.get("ema_200")) else None
            )
            side = str(tr["side"])
            major = (
                int(r4["major_direction"])
                if "major_direction" in r4.index and pd.notna(r4["major_direction"])
                else None
            )
            row.update(
                {
                    "h4_bucket_open": hit.get("selected_4h_bar_time"),
                    "h4_bucket_close": hit.get("selected_4h_bar_close_time"),
                    "ema_9_4h": e9,
                    "ema_20_4h": e20,
                    "ema_59_4h": e59,
                    "ema_200_4h": e200,
                    "ema_9_slope_3_4h": (
                        float(r4["ema_9_slope_3"])
                        if "ema_9_slope_3" in r4.index and pd.notna(r4.get("ema_9_slope_3"))
                        else None
                    ),
                    "ema_20_slope_3_4h": (
                        float(r4["ema_20_slope_3"])
                        if "ema_20_slope_3" in r4.index and pd.notna(r4.get("ema_20_slope_3"))
                        else None
                    ),
                    "ema_59_slope_3_4h": (
                        float(r4["ema_59_slope_3"])
                        if "ema_59_slope_3" in r4.index and pd.notna(r4.get("ema_59_slope_3"))
                        else None
                    ),
                    "ema_200_slope_3_4h": (
                        float(r4["ema_200_slope_3"])
                        if "ema_200_slope_3" in r4.index and pd.notna(r4.get("ema_200_slope_3"))
                        else None
                    ),
                    "adx_4h": float(r4["adx"]) if "adx" in r4.index and pd.notna(r4.get("adx")) else None,
                    "plus_di_4h": (
                        float(r4["plus_di"]) if "plus_di" in r4.index and pd.notna(r4.get("plus_di")) else None
                    ),
                    "minus_di_4h": (
                        float(r4["minus_di"])
                        if "minus_di" in r4.index and pd.notna(r4.get("minus_di"))
                        else None
                    ),
                    "atr_14_4h": (
                        float(r4["atr_14"])
                        if "atr_14" in r4.index and pd.notna(r4.get("atr_14"))
                        else (
                            float(r4["atr"]) if "atr" in r4.index and pd.notna(r4.get("atr")) else None
                        )
                    ),
                    "ctx_ema_9_20": ema_pair_class(side, e9, e20),
                    "ctx_ema_20_59": ema_pair_class(side, e20, e59),
                    "ctx_ema_59_200": ema_pair_class(side, e59, e200),
                    "ctx_combined": combined_context_class(side, e9, e20, e59, e200),
                    "ctx_structure": structure_class(side, major),
                    "major_direction_4h": major,
                    "structure_available": major is not None,
                }
            )
        else:
            row.update(
                {
                    "h4_bucket_open": None,
                    "h4_bucket_close": None,
                    "ema_9_4h": None,
                    "ema_20_4h": None,
                    "ema_59_4h": None,
                    "ema_200_4h": None,
                    "ctx_ema_9_20": "neutral",
                    "ctx_ema_20_59": "neutral",
                    "ctx_ema_59_200": "neutral",
                    "ctx_combined": "mixed",
                    "ctx_structure": None,
                    "major_direction_4h": None,
                    "structure_available": False,
                }
            )
        enriched.append(row)
    per_trade = pd.DataFrame(enriched)
    per_trade.to_csv(output_dir / "apt_4h_context_per_trade.csv", index=False)

    class_rows = []
    for side in ("both", "long", "short"):
        base = per_trade if side == "both" else per_trade[per_trade["side"] == side]
        for col in ("ctx_ema_9_20", "ctx_ema_20_59", "ctx_ema_59_200", "ctx_combined", "ctx_structure"):
            if col not in base.columns:
                continue
            for cls, g in base.dropna(subset=[col]).groupby(col):
                r = summarize_group(g, label=f"{side}|{col}|{cls}")
                r.update({"side": side, "context_def": col, "context_class": cls})
                class_rows.append(r)
            # missing structure documented
            if col == "ctx_structure":
                miss = base[base[col].isna()]
                if len(miss):
                    r = summarize_group(miss, label=f"{side}|{col}|missing")
                    r.update({"side": side, "context_def": col, "context_class": "missing"})
                    class_rows.append(r)
    by_class = pd.DataFrame(class_rows)
    by_class.to_csv(output_dir / "apt_4h_context_by_class.csv", index=False)

    by_side = pd.DataFrame(
        [
            summarize_group(per_trade, label="both"),
            summarize_group(per_trade[per_trade.side == "long"], label="long"),
            summarize_group(per_trade[per_trade.side == "short"], label="short"),
        ]
    )
    by_side.to_csv(output_dir / "apt_4h_context_by_side.csv", index=False)

    by_split_rows = []
    for side in ("both", "long", "short"):
        base = per_trade if side == "both" else per_trade[per_trade["side"] == side]
        for sp, g in base.groupby("split"):
            r = summarize_group(g, label=f"{side}|{sp}")
            r["side"] = side
            r["split"] = sp
            by_split_rows.append(r)
    by_split = pd.DataFrame(by_split_rows)
    by_split.to_csv(output_dir / "apt_4h_context_by_split.csv", index=False)

    veto_rows = []
    for side_name, subset in (
        ("both", per_trade),
        ("long", per_trade[per_trade.side == "long"]),
        ("short", per_trade[per_trade.side == "short"]),
    ):
        if subset.empty:
            continue
        baseline = veto_view(
            subset, allow_mask=pd.Series(True, index=subset.index), name=f"{side_name}|baseline"
        )
        baseline["side"] = side_name
        baseline["rule"] = "baseline"
        veto_rows.append(baseline)

        allow_opposed = subset["ctx_combined"].isin(["strong_aligned", "mixed"])
        v = veto_view(subset, allow_mask=allow_opposed, name=f"{side_name}|opposed_veto_combined")
        v["side"] = side_name
        v["rule"] = "opposed_veto_combined"
        veto_rows.append(v)

        allow_strict = subset["ctx_ema_9_20"] == "aligned"
        v = veto_view(subset, allow_mask=allow_strict, name=f"{side_name}|strict_alignment_ema920")
        v["side"] = side_name
        v["rule"] = "strict_alignment_ema920"
        veto_rows.append(v)

        allow_strong = subset["ctx_combined"] == "strong_aligned"
        v = veto_view(subset, allow_mask=allow_strong, name=f"{side_name}|strong_alignment")
        v["side"] = side_name
        v["rule"] = "strong_alignment"
        veto_rows.append(v)

        allow_e = subset["ctx_ema_9_20"].isin(["aligned", "neutral"])
        v = veto_view(subset, allow_mask=allow_e, name=f"{side_name}|opposed_veto_ema920")
        v["side"] = side_name
        v["rule"] = "opposed_veto_ema920"
        veto_rows.append(v)

    veto = pd.DataFrame(veto_rows)
    veto.to_csv(output_dir / "apt_4h_context_veto_comparison.csv", index=False)

    candidates = []
    for _, r in veto.iterrows():
        if r["rule"] == "baseline":
            continue
        base = veto[(veto.side == r["side"]) & (veto.rule == "baseline")].iloc[0]
        ne = r.get("net_expectancy")
        bne = base.get("net_expectancy")
        pf = r.get("profit_factor")
        bpf = base.get("profit_factor")
        ok = (
            ne is not None
            and bne is not None
            and float(ne) > float(bne)
            and pf is not None
            and bpf is not None
            and (pf == float("inf") or float(pf) > float(bpf or 0))
            and (r.get("retain_rate") or 0) >= 0.60
            and bool(r.get("blocked_more_losers_than_winners"))
        )
        oos_ok = True
        if r.get("net_expectancy_oos") is not None and base.get("net_expectancy_oos") is not None:
            oos_ok = float(r["net_expectancy_oos"]) >= 0 or float(r["net_expectancy_oos"]) > float(
                base["net_expectancy_oos"]
            )
        candidates.append(
            {
                "side": r["side"],
                "rule": r["rule"],
                "candidate": bool(ok and oos_ok),
                "net_expectancy": ne,
                "baseline_net": bne,
                "profit_factor": pf,
                "baseline_pf": bpf,
                "retain_rate": r.get("retain_rate"),
                "blocked_winners": r.get("n_blocked_winners"),
                "blocked_losers": r.get("n_blocked_losers"),
                "net_expectancy_oos": r.get("net_expectancy_oos"),
                "baseline_oos": base.get("net_expectancy_oos"),
            }
        )

    missing_rate = float(per_trade["context_missing"].mean())
    structure_coverage = (
        float(per_trade["structure_available"].mean()) if "structure_available" in per_trade else 0.0
    )
    any_candidate = any(bool(c["candidate"]) for c in candidates)
    veto_plausible = bool(any_candidate)
    # Multicoin MySQL backfill for *this* 4h-veto track only if a veto candidate emerges.
    # APT MySQL pipeline parity alone does not justify a multicoin OHLCV backfill.
    multicoin_backfill_justified = bool(veto_plausible and missing_rate == 0.0 and parity["ok"])

    report_lines = [
        "# APT MySQL 4h Context Audit",
        "",
        "Diagnostic only. **No A6/Pine change. No veto activated. No feather fallback.**",
        "",
        f"- data_source: `mysql` · n_5m=`{mysql_meta['n_5m']}` · span `{mysql_meta['t0']}` → `{mysql_meta['t1']}`",
        f"- MySQL↔Feather parity: `{parity['classification']}` (ok={parity['ok']})",
        f"- A6 fills: `{EXPECTED_N_FILLS}` · entry parity vs reference: OK",
        f"- TP `{TP_PCT}` / SL `{SL_PCT}` / horizon `{HORIZON_BARS}` / cost `{COST_PCT}`",
        f"- 4h join missing rate: `{missing_rate:.4f}` · structure coverage: `{structure_coverage:.4f}`",
        f"- 4h veto candidate plausible: `{veto_plausible}`",
        f"- Multicoin MySQL backfill justified (for 4h-veto track): `{multicoin_backfill_justified}`",
        "",
        "## By side",
        "",
        "```",
        by_side.to_string(index=False),
        "```",
        "",
        "## By split",
        "",
        "```",
        by_split.to_string(index=False),
        "```",
        "",
        "## Veto comparison",
        "",
        "```",
        veto[
            [
                "label",
                "n_taken",
                "n_blocked",
                "n_blocked_winners",
                "n_blocked_losers",
                "net_expectancy",
                "profit_factor",
                "retain_rate",
                "sum_pp",
                "max_drawdown_pp",
                "max_losing_streak",
            ]
        ].to_string(index=False),
        "```",
        "",
        "## Candidates (not activated)",
        "",
        "```",
        pd.DataFrame(candidates).to_string(index=False) if candidates else "(none)",
        "```",
        "",
        "## 4h aggregation semantics",
        "",
        "- UTC floor buckets; 48 complete 5m candles required",
        "- incomplete / in-progress buckets discarded",
        "- at fill: last closed 4h with `htf_close_decision <= fill_time`",
        "- example: fill 14:15 → context bucket 08:00–12:00 (not 12:00–16:00)",
        "",
    ]
    (output_dir / "apt_4h_context_report.md").write_text(
        "\n".join(report_lines) + "\n", encoding="utf-8"
    )

    meta = {
        "ok": True,
        "aborted": False,
        "symbol": SYMBOL,
        "data_source": "mysql",
        "feather_fallback": False,
        "mysql_meta": mysql_meta,
        "parity": parity,
        "a6": a6_meta,
        "expected_a6_hash": EXPECTED_A6_HASH,
        "n_fills": EXPECTED_N_FILLS,
        "entry_parity_ok": True,
        "tp_pct": TP_PCT,
        "sl_pct": SL_PCT,
        "horizon_bars": HORIZON_BARS,
        "cost_pct": COST_PCT,
        "analyze_start": ANALYZE_START,
        "analyze_end_exclusive": ANALYZE_END_EXCLUSIVE,
        "n_4h_bars": int(len(frame4h)),
        "context_missing_rate": missing_rate,
        "structure_coverage": structure_coverage,
        "structure_source": "C3.4B Protected Structure via build_c34b_htf_frame",
        "success_criteria_frozen": SUCCESS_CRITERIA_DOC,
        "candidates": candidates,
        "veto_plausible": veto_plausible,
        "multicoin_mysql_backfill_justified": multicoin_backfill_justified,
        "pine_unchanged": True,
        "a6_unchanged": True,
        "baseline_dir": str(baseline_dir),
        "reference_panel": str(reference_panel),
        "by_side": by_side.to_dict(orient="records"),
        "veto_summary": veto.to_dict(orient="records"),
    }
    (output_dir / "metadata.json").write_text(
        json.dumps(json_safe(meta), indent=2) + "\n", encoding="utf-8"
    )
    return meta


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="APT MySQL 4h context diagnostic audit")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--reference-panel", type=Path, default=DEFAULT_REF_PANEL)
    p.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE_DIR)
    args = p.parse_args(list(argv) if argv is not None else None)
    meta = run_apt_mysql_4h_context_audit(
        output_dir=args.output_dir,
        reference_panel=args.reference_panel,
        baseline_dir=args.baseline_dir,
    )
    print(
        json.dumps(
            json_safe(
                {
                    "ok": meta.get("ok"),
                    "aborted": meta.get("aborted"),
                    "n_fills": meta.get("n_fills"),
                    "veto_plausible": meta.get("veto_plausible"),
                    "out": str(args.output_dir),
                }
            )
        )
    )
    return 0 if meta.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
